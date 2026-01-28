import json
import os
import socket
import ssl
import subprocess
import time
from decimal import Decimal

import psutil
import pymysql


class TiDB:
    def __init__(self, db_config):
        """
        Initialize TiDB connector

        Args:
            host: TiDB server address
            port: Port number
            user: Username
            password: Password
            database: Database name
            db_type: Database type
            ssl_mode: SSL mode (DISABLED, REQUIRED, VERIFY_IDENTITY)
            ssl_ca: SSL certificate path
        """
        self.host = db_config["host"]
        self.port = db_config["port"]
        self.user = db_config["user"]
        self.password = db_config["password"]
        self.database = db_config["dbname"]
        self.ssl_mode = db_config.get("ssl_mode", "VERIFY_IDENTITY")
        self.ssl_ca = db_config.get("ssl_ca", "/etc/ssl/certs/ca-certificates.crt")
        self.db_type = db_config.get("db_type", "tidb")
        self.connection = None

    def _check_tidb_running(self):
        """Check if TiDB is running on the specified port"""
        try:
            # Try to connect to the port
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((self.host, self.port))
            sock.close()
            return result == 0
        except Exception:
            return False

    def _check_tiup_playground_running(self):
        """Check if tiup playground process is running"""
        try:
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    cmdline = proc.info["cmdline"]
                    if cmdline and "tiup" in cmdline and "playground" in cmdline and "DREAM" in cmdline:
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return False
        except Exception:
            return False

    def _start_tiup_playground(self):
        """Start tiup playground"""
        try:
            print("Starting tiup playground --tag DREAM...")
            # use subprocess to start tiup playground, set to background
            process = subprocess.Popen(
                ["tiup", "playground", "--tag", "DREAM"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid,  # create new process group
            )

            # wait for service to start
            time.sleep(10)

            # check if successfully started
            if self._check_tidb_running():
                print("tiup playground started successfully!")
                return True
            else:
                print("tiup playground startup failed or timed out")
                return False

        except Exception as e:
            print(f"Error starting tiup playground: {e}")
            return False

    def _ensure_tidb_running(self):
        # ensure TiDB is running, if not, start it
        if not self._check_tidb_running():
            print("TiDB is not running, checking tiup playground status...")

            if not self._check_tiup_playground_running():
                print("tiup playground process not found, starting...")
                if not self._start_tiup_playground():
                    raise Exception("Unable to start TiDB service")
            else:
                print("Found tiup playground process, waiting for service to start...")
                max_wait = 30
                wait_time = 0
                while not self._check_tidb_running() and wait_time < max_wait:
                    time.sleep(2)
                    wait_time += 2

                if not self._check_tidb_running():
                    raise Exception("TiDB service startup timeout")

        print("TiDB service is ready")

    def _get_ssl_config(self):
        # get SSL config
        if self.ssl_mode == "VERIFY_IDENTITY":
            return {
                "ssl": {
                    "ca": self.ssl_ca,
                    "check_hostname": True,
                    "verify_mode": ssl.CERT_REQUIRED,
                }
            }
        elif self.ssl_mode == "REQUIRED":
            return {
                "ssl": {
                    "ca": self.ssl_ca,
                    "check_hostname": False,
                    "verify_mode": ssl.CERT_REQUIRED,
                }
            }
        elif self.ssl_mode == "DISABLED":
            return {}
        else:
            return {}

    def connect(self):
        # ensure TiDB service is running
        self._ensure_tidb_running()

        if self.connection is None or not self.connection.open:
            ssl_config = self._get_ssl_config()
            self.connection = pymysql.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                **ssl_config,
            )
        return self.connection

    def execute(self, sql, conn=None):
        if conn is None:
            conn = self.connect()

        try:
            start_time = time.time()
            with conn.cursor() as cursor:
                cursor.execute(sql)
                results = cursor.fetchall()
                conn.commit()
            end_time = time.time()
            return True, results, end_time - start_time, ""
        except pymysql.Error as e:
            return False, None, -1, str(e)
        except Exception as e:
            return False, None, -1, str(e)

    def fetch_results(self, sql, as_json=False):
        conn = self.connect()
        with conn.cursor() as cursor:
            cursor.execute(sql)
            results = cursor.fetchall()

            if as_json:
                return json.dumps(results, indent=4, ensure_ascii=False, default=self._decimal_default)

            # if only one column, return single column result
            if results and len(results[0]) == 1:
                return [list(row.values())[0] for row in results]

            return results

    def get_tables(self):
        sql = """
        SELECT table_name FROM information_schema.tables 
        WHERE table_schema = %s AND table_type = 'BASE TABLE'
        """
        conn = self.connect()
        with conn.cursor() as cursor:
            cursor.execute(sql, (self.database,))
            results = cursor.fetchall()
            return [row["table_name"] for row in results]

    def get_views(self):
        sql = """
        SELECT table_name as viewname, view_definition as definition 
        FROM information_schema.views 
        WHERE table_schema = %s
        """
        conn = self.connect()
        with conn.cursor() as cursor:
            cursor.execute(sql, (self.database,))
            results = cursor.fetchall()
            return [(row["viewname"], row["definition"]) for row in results]

    def get_columns(self):
        sql = """
        SELECT column_name FROM information_schema.columns 
        WHERE table_schema = %s
        """
        conn = self.connect()
        with conn.cursor() as cursor:
            cursor.execute(sql, (self.database,))
            results = cursor.fetchall()
            return [row["column_name"] for row in results]

    def fetch_schema_info(self):
        sql = """
        SELECT 
            c.table_name,
            c.column_name,
            c.data_type,
            c.is_nullable,
            c.column_default,
            c.column_key,
            c.extra
        FROM information_schema.columns c
        WHERE c.table_schema = %s
        ORDER BY c.table_name, c.ordinal_position
        """

        conn = self.connect()
        with conn.cursor() as cursor:
            cursor.execute(sql, (self.database,))
            rows = cursor.fetchall()

        schema_dict = {}
        for row in rows:
            table_name = row["table_name"]
            if table_name not in schema_dict:
                schema_dict[table_name] = {"columns": []}

            schema_dict[table_name]["columns"].append(
                {
                    "column_name": row["column_name"],
                    "data_type": row["data_type"],
                    "is_nullable": row["is_nullable"],
                    "column_default": row["column_default"],
                    "column_key": row["column_key"],
                    "extra": row["extra"],
                }
            )

        return schema_dict

    def get_db_size(self):
        sql = """
        SELECT 
            ROUND(SUM(data_length + index_length) / 1024 / 1024, 2) as size_mb
        FROM information_schema.tables 
        WHERE table_schema = %s
        """
        conn = self.connect()
        with conn.cursor() as cursor:
            cursor.execute(sql, (self.database,))
            result = cursor.fetchone()
            return float(result["size_mb"]) if result["size_mb"] else 0.0

    def get_plan(self, sql):
        sql = f"""
        EXPLAIN {sql};
        """
        return self.fetch_results(sql)

    def compare_sql_results(self, sql1, sql2):
        sql1 = sql1.strip().rstrip(";")
        sql2 = sql2.strip().rstrip(";")

        conn = self.connect()
        with conn.cursor() as cursor:
            try:
                cursor.execute(sql1)
                results1 = cursor.fetchall()

                cursor.execute(sql2)
                results2 = cursor.fetchall()

                if len(results1) != len(results2):
                    return False

                def normalize_results(results):
                    normalized = []
                    for row in results:
                        if isinstance(row, dict):
                            normalized.append(tuple(sorted(row.items())))
                        else:
                            normalized.append(tuple(row))
                    return sorted(normalized)

                normalized1 = normalize_results(results1)
                normalized2 = normalize_results(results2)

                return normalized1 == normalized2

            except Exception as e:
                print(f"Error comparing SQL results: {e}")
                return False

    def close(self):
        if self.connection and self.connection.open:
            self.connection.close()

    def _decimal_default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        raise TypeError(f"Object of type {type(obj)} is not JSON serializable")
