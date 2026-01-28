import json
import os
import re
import tempfile
import time
from collections import defaultdict
from decimal import Decimal

import psycopg
from plumbum import local
from psycopg.rows import dict_row

from .resource_monitor import ResourceMonitor


class PostgresDB:
    def __init__(self, db_config):
        self.user = db_config["user"]
        self.password = db_config["password"]
        self.host = db_config["host"]
        self.port = db_config["port"]
        self.dbname = db_config["dbname"]
        self.db_type = "postgresql"
        self.workload_type = db_config.get("workload_type", "OLAP")
        self.postgres_path = db_config.get("postgres_path")
        self.postgres_data = db_config.get("postgres_data")
        self.log_path = db_config.get("log_path")
        self.query_timeout = db_config.get("query_timeout")

        self.conn_str = self._connect_str()
        self.connection = None

    def _connect_str(self):
        return "host={host} port={port} dbname={dbname} user={user} password={password}".format(
            host=self.host,
            port=int(self.port),
            dbname=self.dbname,
            user=self.user,
            password=self.password,
        )

    def close(self):
        if self.connection:
            self.connection.close()
            self.connection = None

    def connect(self):
        """Establish and return a database connection"""
        if self.connection is None or self.connection.closed:
            self.connection = psycopg.connect(self.conn_str, autocommit=True, prepare_threshold=None)
        return self.connection

    def execute(self, sql, conn=None, timeout=None):
        if conn is None:
            conn = self.connect()

        if timeout is None:
            timeout = self.query_timeout

        with conn.cursor() as cur:
            try:
                cur.execute(f"SET statement_timeout = {timeout * 1000}")  # Set timeout
                start_time = time.time()
                cur.execute(sql)
                end_time = time.time()
                if cur.description is not None:
                    result = cur.fetchall()
                else:
                    result = None
                return True, result, end_time - start_time, ""
            except psycopg.errors.SyntaxError as e:
                return False, None, timeout, str(e)
            except psycopg.errors.QueryCanceled as e:
                return False, None, timeout, str(e)
            except Exception as e:
                return False, None, timeout, str(e)

    def fetch_results(self, sql, as_json=False, timeout=None):
        if timeout is None:
            timeout = self.query_timeout

        conn = self.connect()
        with conn.cursor() as cursor:
            try:
                statements = self.extract_sql_statements(sql)
                if not statements:
                    statements = [sql]

                select_results = None

                for i, stmt in enumerate(statements):
                    if self.is_select_statement(stmt):
                        cursor.execute(f"SET statement_timeout = {timeout * 1000}")
                        cursor.execute(stmt)

                        select_results = cursor.fetchall()
                        if len(cursor.description) == 1:
                            select_results = [row[0] for row in select_results]

                    else:
                        # Execute non-SELECT statements (e.g., CREATE VIEW, DROP VIEW)
                        cursor.execute(stmt)
                        conn.commit()

                if select_results is not None:
                    if as_json:
                        columns = [col.name for col in cursor.description]
                        results = [dict(zip(columns, row)) for row in select_results]
                        return json.dumps(
                            results,
                            indent=4,
                            ensure_ascii=False,
                            default=self.decimal_default,
                        )
                    if select_results and isinstance(select_results[0], (dict, list)):
                        return json.dumps(select_results, ensure_ascii=False)
                    return select_results
                else:
                    return []

            except psycopg.errors.QueryCanceled as e:
                print(f"Query timeout (>{timeout}s): {str(e)}")
                return []
            except Exception as e:
                print(f"Query execution failed: {str(e)}")
                return []

    def extract_sql_statements(self, sql_text):
        return [stmt.strip() for stmt in sql_text.split(";") if stmt.strip()]

    def is_select_statement(self, sql):
        sql = sql.strip()
        return bool(
            re.match(
                r"^\s*(?:WITH\s+.*?\s+)?(?:SELECT|EXPLAIN)\s+",
                sql,
                re.IGNORECASE | re.DOTALL,
            )
        )

    def get_size(self):
        sql = f"""
        SELECT pg_size_pretty(pg_database_size('{self.dbname}'));
        """
        return self.fetch_results(sql)

    def get_views(self):
        sql = """
        SELECT viewname, definition FROM pg_views WHERE schemaname = 'public' AND viewname NOT IN ('pg_stat_statements', 'hypopg_list_indexes', 'hypopg_hidden_indexes', 'pg_stat_statements_info');
        """
        return self.fetch_results(sql)

    def get_tables(self):
        sql = """
        SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name NOT IN ('pg_stat_statements', 'hypopg_list_indexes', 'hypopg_hidden_indexes', 'pg_stat_statements_info');
        """
        return self.fetch_results(sql)

    def get_columns(self):
        sql = """
        SELECT column_name FROM information_schema.columns WHERE table_name NOT IN ('pg_stat_statements', 'hypopg_list_indexes', 'hypopg_hidden_indexes', 'pg_stat_statements_info') AND table_schema NOT IN ('information_schema', 'pg_catalog');
        """
        return self.fetch_results(sql)

    def get_indexes(self):
        sql = """
        SELECT indexname FROM pg_indexes 
        WHERE schemaname = 'public'
        AND indexname NOT LIKE 'pg_%';
        """
        return self.fetch_results(sql)

    def get_current_knob_values(self, knob_config):
        current_values = {"system_knobs": {}, "query_knobs": {}}

        if "system_knobs" in knob_config:
            system_knobs = list(knob_config["system_knobs"].keys())
            if system_knobs:
                sql = f"SELECT name, setting FROM pg_settings WHERE name IN {tuple(system_knobs)};"
                results = self.fetch_results(sql, as_json=True)
                if results:
                    if isinstance(results, str):
                        results = json.loads(results)
                    for row in results:
                        current_values["system_knobs"][row["name"]] = row["setting"]

        if "query_knobs" in knob_config:
            query_knobs = list(knob_config["query_knobs"].keys())
            if query_knobs:
                sql = f"SELECT name, setting FROM pg_settings WHERE name IN {tuple(query_knobs)};"
                results = self.fetch_results(sql, as_json=True)
                if results:
                    if isinstance(results, str):
                        results = json.loads(results)
                    for row in results:
                        current_values["query_knobs"][row["name"]] = row["setting"]

        return current_values

    def fetch_schema_info(self):
        sql = """
        WITH
        pk_cols AS (
            SELECT kc.table_name, kc.column_name, 'YES' AS is_primary_key
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kc
            ON kc.constraint_name = tc.constraint_name
            AND kc.table_schema = tc.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY'
            AND kc.table_schema = 'public'
        ),
        fk_cols AS (
            SELECT tc.table_name, kcu.column_name,
                ccu.table_name AS referenced_table,
                ccu.column_name AS referenced_column
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
            ON kcu.constraint_name = tc.constraint_name
            AND kcu.table_schema = tc.table_schema
            JOIN information_schema.constraint_column_usage ccu
            ON ccu.constraint_name = tc.constraint_name
            AND ccu.table_schema = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
            AND tc.table_schema = 'public'
        )
        SELECT c.table_name, c.column_name, c.data_type,
            COALESCE(pk.is_primary_key, 'NO') AS is_primary_key,
            fk.referenced_table, fk.referenced_column
        FROM information_schema.columns c
        LEFT JOIN pk_cols pk
        ON c.table_name = pk.table_name AND c.column_name = pk.column_name
        LEFT JOIN fk_cols fk
        ON c.table_name = fk.table_name AND c.column_name = fk.column_name
        WHERE c.table_schema = 'public'
        AND c.table_name NOT IN (
            'hypopg_hidden_indexes', 'hypopg_list_indexes',
            'pg_stat_statements', 'pg_stat_statements_info'
        )
        ORDER BY c.table_name, c.ordinal_position;
        """

        rows = self.fetch_results(sql)
        if not rows:
            return {}

        schema_dict = defaultdict(lambda: {"columns": []})
        for table_name, column_name, data_type, is_pk, ref_table, ref_col in rows:
            fk_info = None
            if ref_table and ref_col:
                fk_info = {"referenced_table": ref_table, "referenced_column": ref_col}
            schema_dict[table_name]["columns"].append(
                {
                    "column_name": column_name,
                    "data_type": data_type,
                    "is_primary_key": is_pk,
                    "foreign_key": fk_info,
                }
            )

        return dict(schema_dict)

    def start_db(self):
        if not all([self.postgres_path, self.postgres_data, self.log_path]):
            raise ValueError("postgres_path, postgres_data, and log_path must be configured to start the database.")
        pg_ctl_cmd = [
            f"{self.postgres_path}/pg_ctl",
            "-D",
            self.postgres_data,
            "--wait",
            "-t",
            "180",
            "-l",
            f"{self.log_path}/pg.log",
            "start",
        ]
        cmd = ["-u", "postgres"] + pg_ctl_cmd
        retcode, _, stderr = local["sudo"][cmd].run(retcode=None)
        if retcode != 0:
            raise Exception(f"Failed to start PostgreSQL: {stderr}")

        for _ in range(10):
            pg_isready_cmd = [
                f"{self.postgres_path}/pg_isready",
                "--host",
                self.host,
                "--port",
                str(self.port),
                "--dbname",
                self.dbname,
            ]
            cmd = ["-u", "postgres"] + pg_isready_cmd
            retcode, _, _ = local["sudo"][cmd].run(retcode=None)
            if retcode == 0:
                return True
            time.sleep(5)
        raise Exception("PostgreSQL did not become ready in time.")

    def shutdown_db(self):
        if not all([self.postgres_path, self.postgres_data]):
            raise ValueError("postgres_path and postgres_data must be configured to shut down the database.")
        pg_ctl_cmd = [
            f"{self.postgres_path}/pg_ctl",
            "stop",
            "--wait",
            "-t",
            "180",
            "-D",
            self.postgres_data,
        ]
        cmd = ["-u", "postgres"] + pg_ctl_cmd
        local["sudo"][cmd].run(retcode=None)

        for _ in range(10):
            pg_isready_cmd = [
                f"{self.postgres_path}/pg_isready",
                "--host",
                self.host,
                "--port",
                str(self.port),
                "--dbname",
                self.dbname,
            ]
            cmd = ["-u", "postgres"] + pg_isready_cmd
            retcode, _, _ = local["sudo"][cmd].run(retcode=None)
            if retcode != 0:
                return True
            time.sleep(2)
        raise Exception("Failed to shut down PostgreSQL.")

    def get_pid(self):
        pid = self.fetch_results("SELECT pg_backend_pid();")
        return pid[0]

    def get_metrics(self, sql, interval=1, warmup=0, duration=20):
        pid = self.get_pid()

        monitor = ResourceMonitor(pid, interval, warmup, duration)
        monitor.run()

        self.execute(sql)

        monitor.terminate()
        (
            cpu_usage,
            read_io,
            write_io,
            virtual_memory,
            physical_memory,
            net_recv,
            net_sent,
        ) = monitor.get_monitor_data()
        return (
            cpu_usage,
            read_io,
            write_io,
            virtual_memory,
            physical_memory,
            net_recv,
            net_sent,
        )

    def get_plan(self, sql):
        """
        Get SQL execution plan. Supports complex SQL with CREATE VIEW / DROP VIEW.
        Execution flow:
        1. Execute CREATE related statements first (without EXPLAIN)
        2. Find first SELECT, generate plan and return
        3. Execute DROP statements for cleanup
        """
        sql = sql.strip()

        # Use parser to count statements
        statements = self.extract_sql_statements(sql)
        if len(statements) > 1:
            plan_result = None

            try:
                # 1. Execute CREATE/ALTER/SET preparation statements
                for stmt in statements:
                    if stmt.upper().startswith(("CREATE", "ALTER", "SET")):
                        try:
                            self.execute(stmt)
                        except Exception as e:
                            print(f"Failed to execute {stmt}: {e}")

                # 2. Find first EXPLAIN-able query statement (WITH/SELECT/INSERT/UPDATE/DELETE)
                for stmt in statements:
                    if stmt.upper().startswith("WITH") or stmt.upper().startswith("SELECT") or stmt.upper().startswith("/*+") or stmt.upper().startswith("INSERT") or stmt.upper().startswith("UPDATE") or stmt.upper().startswith("DELETE"):
                        try:
                            plan_sql = f"EXPLAIN (FORMAT JSON) {stmt}"
                            plan_result = self.fetch_results(plan_sql)
                            break
                        except Exception as e:
                            print(f"Failed to get execution plan: {e}")

            finally:
                # 3. Cleanup DROP statements
                for stmt in statements:
                    if stmt.upper().startswith("DROP"):
                        try:
                            self.execute(stmt)
                        except Exception as e:
                            print(f"Failed to execute {stmt}: {e}")

            return plan_result

        else:
            # Single statement, get plan directly
            try:
                single_stmt = statements[0] if statements else sql
                plan_sql = f"EXPLAIN (FORMAT JSON) {single_stmt}"
                return self.fetch_results(plan_sql)
            except Exception as e:
                print(f"Failed to get execution plan: {e}")
                return None

    def get_metrics_specification(self):
        return {
            "pg_stat_database": [
                "tup_returned",
                "blks_hit",
                "blks_read",
                "tup_fetched",
            ],
            "pg_stat_user_tables": [
                "idx_tup_fetch",
                "seq_tup_read",
                "seq_scan",
                "idx_scan",
            ],
            "pg_statio_user_tables": [
                "heap_blks_hit",
                "heap_blks_read",
                "idx_blks_hit",
                "idx_blks_read",
            ],
        }

    def collect_metrics(self, conn):
        data = {}
        METRICS_SPECIFICATION = self.get_metrics_specification()
        with conn.cursor(row_factory=dict_row) as cursor:
            for table, keys in METRICS_SPECIFICATION.items():
                try:
                    # Only collect current database
                    if table == "pg_stat_database":
                        cursor.execute(f"SELECT * FROM {table} WHERE datname = current_database()")
                    else:
                        cursor.execute(f"SELECT * FROM {table}")
                    records = cursor.fetchall()
                    # Only keep fields of interest
                    filtered = []
                    for r in records:
                        filtered.append({k: r[k] for k in keys if k in r})
                    data[table] = filtered
                except Exception as e:
                    data[table] = f"Error: {e}"
        return data

    def get_log(self, sql=None):
        conn = self.connect()

        metrics_before = self.collect_metrics(conn)
        if sql is not None:
            _, _, execution_time, _ = self.execute(sql)
        metrics_after = self.collect_metrics(conn)

        diff = self.get_log_diff(metrics_before, metrics_after)
        log_list = self.get_metrics_list(diff, execution_time)

        return log_list

    def get_metrics_list(self, diff, execution_time):
        metrics_list = []
        # 1. pg_stat_database
        db_metrics = diff.get("pg_stat_database", [{}])
        metrics_list.append(db_metrics[0].get("tup_returned", 0))
        metrics_list.append(db_metrics[0].get("blks_hit", 0))
        metrics_list.append(db_metrics[0].get("blks_read", 0))
        metrics_list.append(db_metrics[0].get("tup_fetched", 0))
        # 2. pg_stat_user_tables
        user_table_metrics = diff.get("pg_stat_user_tables", {})
        metrics_list.append(user_table_metrics.get("idx_tup_fetch", 0))
        metrics_list.append(user_table_metrics.get("seq_tup_read", 0))
        metrics_list.append(user_table_metrics.get("seq_scan", 0))
        metrics_list.append(user_table_metrics.get("idx_scan", 0))
        # 3. pg_statio_user_tables
        statio_metrics = diff.get("pg_statio_user_tables", {})
        metrics_list.append(statio_metrics.get("heap_blks_hit", 0))
        metrics_list.append(statio_metrics.get("heap_blks_read", 0))
        metrics_list.append(statio_metrics.get("idx_blks_hit", 0))
        metrics_list.append(statio_metrics.get("idx_blks_read", 0))
        # 4. execution_time
        metrics_list.append(execution_time)
        return metrics_list

    def get_log_diff(self, log_before, log_after):
        diff = {}
        for table in log_before:
            before = log_before.get(table, [])
            after = log_after.get(table, [])
            if isinstance(before, str) or isinstance(after, str):
                diff[table] = before if isinstance(before, str) else after
                continue
            if table in ["pg_stat_user_tables", "pg_statio_user_tables"]:
                # sum up all the metrics
                METRICS_SPECIFICATION = self.get_metrics_specification()
                sum_row = {k: 0 for k in METRICS_SPECIFICATION[table]}
                for i in range(min(len(before), len(after))):
                    row_before = before[i]
                    row_after = after[i]
                    for k in sum_row.keys():
                        v_before = row_before.get(k, 0)
                        v_after = row_after.get(k, 0)
                        try:
                            sum_row[k] += (v_after if v_after is not None else 0) - (v_before if v_before is not None else 0)
                        except Exception:
                            pass
                diff[table] = sum_row
            else:
                # other tables (like pg_stat_database) still return single row
                table_diff = []
                for i in range(min(len(before), len(after))):
                    row_before = before[i]
                    row_after = after[i]
                    row_diff = {}
                    for k in before[i]:
                        v_before = row_before.get(k, 0)
                        v_after = row_after.get(k, 0)
                        try:
                            row_diff[k] = (v_after if v_after is not None else 0) - (v_before if v_before is not None else 0)
                        except Exception:
                            row_diff[k] = None
                    for id_key in ["relname", "datname", "table_name"]:
                        if id_key in row_before:
                            row_diff[id_key] = row_before[id_key]
                    table_diff.append(row_diff)
                diff[table] = table_diff
        return diff

    def run_sql_and_collect_all(self, sql, interval=1, warmup=0, timeout=None):
        if timeout is None:
            timeout = self.query_timeout

        conn = self.connect()

        log_before = self.collect_metrics(conn)
        plan = self.get_plan(sql)
        pid = self.get_pid()
        monitor = ResourceMonitor(pid, interval, warmup, timeout)
        monitor.run()

        try:
            _, _, execution_time, _ = self.execute(sql, timeout=timeout)
        finally:
            monitor.terminate()
        
        try:
            (
                cpu_usage,
                read_io,
                write_io,
                virtual_memory,
                physical_memory,
                net_recv,
                net_sent,
            ) = monitor.get_monitor_data(timeout=10)
        except Exception as e:
            print(f"Warning: Failed to get monitor data: {e}")
            cpu_usage = [0.0] * 9
            read_io = [0.0] * 9
            write_io = [0.0] * 9
            virtual_memory = [0.0] * 9
            physical_memory = [0.0] * 9
            net_recv = [0.0] * 9
            net_sent = [0.0] * 9
        
        metrics = [
            cpu_usage,
            read_io,
            write_io,
            virtual_memory,
            physical_memory,
            net_recv,
            net_sent,
        ]

        log_after = self.collect_metrics(conn)

        diff = self.get_log_diff(log_before, log_after)
        log_list = self.get_metrics_list(diff, execution_time)

        return {
            "query": sql,
            "plan_json": plan,
            "internal_metrics": log_list,
            "external_metrics": metrics,
            "duration": execution_time,
        }

    def decimal_default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        raise TypeError

    def check_sql_equivalence(self, sql1, sql2):

        schema_info = self.fetch_schema_info()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False, dir="/tmp", encoding="utf-8") as temp_file:
            temp_sql_path = temp_file.name

            # CREATE TABLE SQL
            for table_name, table_info in schema_info.items():
                columns = []
                seen_columns = set()
                for col in table_info["columns"]:
                    column_name = col["column_name"]
                    if column_name in seen_columns:
                        continue
                    seen_columns.add(column_name)

                    col_def = f"{column_name} {col['data_type']}"
                    # Remove PostgreSQL-specific types (e.g., SERIAL), replace with INTEGER
                    col_def = col_def.replace("SERIAL", "INTEGER")
                    col_def = col_def.replace("serial", "INTEGER")
                    columns.append(col_def)

                # Generate QED-compatible CREATE TABLE statement
                create_table_sql = f"CREATE TABLE {table_name} (\n"
                create_table_sql += ",\n".join(f"  {col}" for col in columns)
                create_table_sql += "\n);\n\n"
                temp_file.write(create_table_sql)

            sql1_cleaned = self.clean_sql(sql1)
            sql2_cleaned = self.clean_sql(sql2)

            temp_file.write(f"-- Query 1\n{sql1_cleaned};\n\n")
            temp_file.write(f"-- Query 2\n{sql2_cleaned};\n")

        temp_dir = os.path.dirname(temp_sql_path)
        temp_basename = os.path.splitext(os.path.basename(temp_sql_path))[0]
        temp_json_path = os.path.join(temp_dir, f"{temp_basename}.json")
        temp_result_path = os.path.join(temp_dir, f"{temp_basename}.result")

        try:
            # qed-parser
            parser_cmd = [
                "shell",
                "github:qed-solver/parser",
                "github:qed-solver/prover",
                "--command",
                "qed-parser",
                temp_sql_path,
            ]
            parser_retcode, parser_stdout, parser_stderr = local["nix"][parser_cmd].run(retcode=None)

            if not os.path.exists(temp_json_path):
                print("No JSON file generated by qed-parser")
                return False

            # qed-prover
            prover_cmd = [
                "shell",
                "github:qed-solver/parser",
                "github:qed-solver/prover",
                "--command",
                "qed-prover",
                temp_json_path,
            ]
            prover_retcode, prover_stdout, prover_stderr = local["nix"][prover_cmd].run(retcode=None)

            result_file_path = temp_result_path if os.path.exists(temp_result_path) else None
            if result_file_path:
                with open(result_file_path, "r") as f:
                    content = f.read().strip()
                    result_json = json.loads(content)
                    if result_json.get("provable", False):
                        return True
                    if result_json.get("panicked", False):
                        if prover_retcode != 0:
                            print(f"QED prover panicked but result file exists (return code: {prover_retcode})")
                        return False
                    return False

            if prover_retcode != 0:
                print(f"QED prover failed (return code: {prover_retcode})")
                if prover_stderr:
                    print(f"Prover stderr: {prover_stderr}")
                return False

            print("No result file generated by qed-prover")
            return False
        finally:
            if os.path.exists(temp_sql_path):
                os.unlink(temp_sql_path)
            if os.path.exists(temp_json_path):
                os.unlink(temp_json_path)
            if os.path.exists(temp_result_path):
                os.unlink(temp_result_path)
            temp_rkt_path = os.path.join(temp_dir, f"{temp_basename}.rkt")
            if os.path.exists(temp_rkt_path):
                os.unlink(temp_rkt_path)

    def clean_sql(self, sql):
        lines = sql.split("\n")
        cleaned_lines = []
        for line in lines:
            if "--" in line:
                line = line.split("--")[0]
            cleaned_lines.append(line.strip())
        sql = " ".join(cleaned_lines)
        sql = " ".join(sql.split())
        return sql.strip().rstrip(";")

    def compare_sql_results(self, sql1, sql2):
        """
        Determine if two SQL statements produce exactly the same output (including content and duplicate rows).
        Supports SQL containing CREATE VIEW + SELECT + DROP VIEW.
        If both SQLs have CREATE VIEW, they will be executed separately and results compared.
        """

        try:
            sql1 = self.clean_sql(sql1)
            sql2 = self.clean_sql(sql2)

            result1 = self.fetch_results(sql1, timeout=20)
            result2 = self.fetch_results(sql2, timeout=20)

            # print(f"result1: {result1}")
            # print(f"result2: {result2}")

            from collections import Counter

            return Counter(result1) == Counter(result2)

        except Exception as e:
            print(f"Error comparing SQL results: {e}")
            return False