#!/usr/bin/env python3
"""MySQL-backed implementation of the bridge database client.

The PG demo remains the default.  This client exposes the same small
collection/validation surface used by the bridge and the MySQL lab, using
performance_schema instead of pg_stat_statements.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pymysql
from pymysql.cursors import DictCursor


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _password() -> str:
    value = os.environ.get("SYSINSIGHT_MYSQL_PASSWORD", "").strip()
    if value:
        return value
    path = Path(os.environ.get("SYSINSIGHT_MYSQL_ENV", "/etc/keeninsight/mysql.env"))
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("SYSINSIGHT_MYSQL_PASSWORD="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _canonical_sql(sql: str) -> str:
    """Same visible-shape normalization used by the PG demo."""
    text = re.sub(r"--[^\n]*", " ", str(sql or ""))
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"'([^']|'')*'", "?", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\b", "?", text)
    return re.sub(r"\s+", " ", text).strip().rstrip(";")


class MySQLDatabaseClient:
    def __init__(self, db: str, user: str, host: str, port: int, run_as: str, timeout: float = 30.0) -> None:
        self.db = db
        self.user = user
        self.host = host
        self.port = int(port)
        self.run_as = run_as
        self.timeout = float(timeout)
        self._server_pid_cache: Optional[int] = None

    def _connect(self, database: Optional[str] = None) -> pymysql.Connection:
        return pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=_password(),
            database=database if database is not None else self.db,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
            connect_timeout=min(10.0, self.timeout),
            read_timeout=self.timeout,
            write_timeout=self.timeout,
        )

    def _rows(self, sql: str, params: Sequence[Any] = (), database: Optional[str] = None) -> List[Dict[str, Any]]:
        connection = self._connect(database)
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                return list(cursor.fetchall())
        finally:
            connection.close()

    def execute(self, sql: str, application_name: str = "sysinsight-mysql-bridge") -> str:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql)
                return str(cursor.rowcount)
        finally:
            connection.close()

    def execute_timed(
        self,
        sql: str,
        application_name: str = "sysinsight-mysql-bridge-lab",
        search_path: Optional[Sequence[str]] = None,
        session_settings: Optional[Mapping[str, Any]] = None,
        startup_settings: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        statement = str(sql or "").strip().rstrip(";")
        if not statement:
            raise ValueError("empty SQL")
        effective_timeout = float(timeout if timeout is not None else self.timeout)
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                if effective_timeout > 0:
                    cursor.execute("SET SESSION max_execution_time = %s", (int(effective_timeout * 1000),))
                for name, value in (session_settings or {}).items():
                    if not _IDENTIFIER.fullmatch(str(name)):
                        raise ValueError("unsafe MySQL session parameter: {!r}".format(name))
                    cursor.execute("SET SESSION `{}` = %s".format(name), (value,))
                started = time.perf_counter()
                try:
                    cursor.execute(statement)
                except Exception as exc:
                    return {"status": "failed", "duration_ms": (time.perf_counter() - started) * 1000.0,
                            "error": "{}: {}".format(type(exc).__name__, exc), "returncode": 1}
                return {"status": "completed", "duration_ms": (time.perf_counter() - started) * 1000.0,
                        "returncode": 0}
        finally:
            connection.close()

    def settings_snapshot(self, names: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        safe = sorted({str(name) for name in names if _IDENTIFIER.fullmatch(str(name))})
        if not safe:
            return {}
        placeholders = ",".join(["%s"] * len(safe))
        rows = self._rows(
            "SELECT VARIABLE_NAME AS name, VARIABLE_VALUE AS setting "
            "FROM performance_schema.global_variables WHERE VARIABLE_NAME IN ({})".format(placeholders),
            tuple(safe),
        )
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            name = str(row.get("name"))
            result[name] = {
                "name": name,
                "setting": row.get("setting"),
                "unit": "",
                "context": "global",
                "source": "global",
                "sourcefile": "",
                "pending_restart": False,
                "reset_val": row.get("setting"),
            }
        return result

    def normalize_session_settings(self, settings: Mapping[str, Any]) -> Dict[str, str]:
        values = {str(name).lower(): str(value) for name, value in settings.items()}
        if not values:
            return {}
        for name in values:
            if not _IDENTIFIER.fullmatch(name):
                raise ValueError("unsafe MySQL session parameter: {!r}".format(name))
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                for name, value in values.items():
                    cursor.execute("SET SESSION `{}` = %s".format(name), (value,))
                normalized: Dict[str, str] = {}
                for name in values:
                    cursor.execute("SELECT @@SESSION.`{}` AS value".format(name))
                    normalized[name] = str(cursor.fetchone().get("value"))
                return normalized
        finally:
            connection.close()

    def reset_statement_stats(self) -> Dict[str, Any]:
        try:
            self.execute("TRUNCATE TABLE performance_schema.events_statements_summary_by_digest")
            return {"status": "completed"}
        except Exception as exc:
            return {"status": "failed", "error": "{}: {}".format(type(exc).__name__, exc)}

    def collect_statements(self, limit: int = 500) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 5000))
        rows = self._rows(
            """
            SELECT SCHEMA_NAME AS database_name,
                   DIGEST AS queryid,
                   DIGEST_TEXT AS query,
                   COUNT_STAR AS calls,
                   SUM_TIMER_WAIT / 1000000000.0 AS total_time_ms,
                   MIN_TIMER_WAIT / 1000000000.0 AS min_time_ms,
                   MAX_TIMER_WAIT / 1000000000.0 AS max_time_ms,
                   AVG_TIMER_WAIT / 1000000000.0 AS mean_time_ms,
                   (SUM_ROWS_AFFECTED + SUM_ROWS_SENT) AS rows_count,
                   SUM_NO_INDEX_USED AS no_index_used
            FROM performance_schema.events_statements_summary_by_digest
            WHERE SCHEMA_NAME = %s AND DIGEST_TEXT IS NOT NULL
              AND DIGEST_TEXT NOT LIKE '%%performance_schema%%'
            ORDER BY SUM_TIMER_WAIT DESC LIMIT %s
            """,
            (self.db, limit),
        )
        for row in rows:
            row["database"] = self.db
            row["username"] = ""
            row["userid"] = ""
            row["dbid"] = ""
            row["query_text"] = row.get("query")
            row["rows"] = row.get("rows_count", 0)
            row["shared_blks_hit"] = 0
            row["shared_blks_read"] = int(row.get("no_index_used") or 0)
            row["shared_blks_dirtied"] = 0
            row["shared_blks_written"] = 0
            row["temp_blks_read"] = 0
            row["temp_blks_written"] = 0
            row["blk_read_time_ms"] = 0.0
            row["blk_write_time_ms"] = 0.0
            row["canonical_sql"] = _canonical_sql(str(row.get("query") or ""))
            row["hint_pattern"] = row["canonical_sql"]
        return rows

    def _server_pid(self) -> int:
        if self._server_pid_cache:
            return self._server_pid_cache
        pid = 0
        try:
            row = self._rows("SELECT @@pid_file AS path")[0]
            path = Path(str(row.get("path") or ""))
            if path.is_file():
                pid = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError, IndexError):
            pid = 0
        if not pid:
            try:
                for line in Path("/proc").glob("[0-9]*/comm"):
                    if line.read_text(encoding="utf-8", errors="ignore").strip() == "mysqld":
                        pid = int(line.parent.name)
                        break
            except OSError:
                pid = 0
        self._server_pid_cache = pid
        return pid

    def collect_active(self, limit: int = 200, include_recent_idle: bool = False) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 2000))
        command_filter = "t.PROCESSLIST_COMMAND IS NOT NULL AND t.PROCESSLIST_ID IS NOT NULL"
        if not include_recent_idle:
            command_filter += " AND t.PROCESSLIST_COMMAND <> 'Sleep'"
        rows = self._rows(
            """
            SELECT t.PROCESSLIST_ID AS thread_id,
                   t.THREAD_OS_ID AS os_thread_id,
                   t.PROCESSLIST_USER AS username,
                   t.PROCESSLIST_HOST AS client_addr,
                   t.PROCESSLIST_DB AS database_name,
                   t.PROCESSLIST_COMMAND AS command,
                   t.PROCESSLIST_TIME AS duration_s,
                   t.PROCESSLIST_STATE AS state,
                   COALESCE(e.SQL_TEXT, t.PROCESSLIST_INFO) AS query
            FROM performance_schema.threads t
            LEFT JOIN performance_schema.events_statements_current e ON e.THREAD_ID = t.THREAD_ID
            WHERE {command_filter}
              AND t.PROCESSLIST_USER NOT IN ('event_scheduler','system user')
              AND COALESCE(e.SQL_TEXT, t.PROCESSLIST_INFO) NOT LIKE '%performance_schema%'
              AND COALESCE(e.SQL_TEXT, t.PROCESSLIST_INFO) NOT LIKE '%sysinsight_mysql_bridge%'
            ORDER BY t.PROCESSLIST_TIME DESC
            LIMIT {limit}
            """.format(command_filter=command_filter, limit=limit),
        )
        server_pid = self._server_pid()
        result = []
        for row in rows:
            query = str(row.get("query") or "")
            if not query:
                continue
            result.append({
                "pid": str(server_pid or row.get("os_thread_id") or row.get("thread_id") or ""),
                "thread_id": row.get("thread_id"),
                "os_thread_id": row.get("os_thread_id"),
                "username": row.get("username"),
                "application_name": row.get("command"),
                "client_addr": row.get("client_addr"),
                "state": row.get("command"),
                "wait_event_type": None,
                "wait_event": row.get("state"),
                "query": query,
                "query_start": None,
                "backend_start": None,
                "duration_ms": float(row.get("duration_s") or 0) * 1000.0,
                "canonical_sql": _canonical_sql(query),
            })
        return result

    def database_snapshot(self) -> Dict[str, Any]:
        rows = self._rows("SHOW GLOBAL STATUS")
        values = {str(row.get("Variable_name")): row.get("Value") for row in rows}
        def number(name: str) -> float:
            try:
                return float(values.get(name, 0) or 0)
            except (TypeError, ValueError):
                return 0.0
        return {
            "database": self.db,
            "numbackends": int(number("Threads_connected")),
            "xact_commit": int(number("Com_commit")),
            "xact_rollback": int(number("Com_rollback")),
            "blks_read": int(number("Innodb_buffer_pool_reads")),
            "blks_hit": int(number("Innodb_buffer_pool_read_requests")),
            "tup_returned": int(number("Innodb_rows_read")),
            "tup_fetched": int(number("Innodb_rows_read")),
            "temp_files": int(number("Created_tmp_files")),
            "temp_bytes": int(number("Created_tmp_disk_tables")),
            "deadlocks": int(number("Innodb_deadlocks")),
            "blk_read_time": 0.0,
            "blk_write_time": 0.0,
        }

    def database_setting(self, name: str) -> str:
        if not _IDENTIFIER.fullmatch(str(name)):
            raise ValueError("unsafe MySQL variable name: {!r}".format(name))
        rows = self._rows("SELECT @@GLOBAL.`{}` AS value".format(name))
        return str(rows[0].get("value")) if rows else ""
