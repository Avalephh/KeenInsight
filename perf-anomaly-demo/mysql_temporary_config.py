#!/usr/bin/env python3
"""Apply a bounded MySQL tuning candidate, then restore the original values."""
from __future__ import annotations

import copy
import re
import time
from typing import Any, Dict, Mapping, Optional

import pymysql
from pymysql.cursors import DictCursor


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Values that are unsafe to leave changed or cannot be changed online are
# audited but skipped by the demo.  This is intentionally conservative.
UNSAFE_VARIABLES = frozenset({
    "basedir", "character_set_server", "collation_server", "datadir",
    "innodb_log_file_size", "innodb_log_files_in_group", "log_bin",
    "log_error", "pid_file", "port", "server_id", "socket", "secure_file_priv",
    "skip_networking", "tmpdir", "version", "version_comment",
})
DYNAMIC_GLOBAL = frozenset({
    "innodb_buffer_pool_size", "innodb_io_capacity", "innodb_io_capacity_max",
    "innodb_flush_log_at_trx_commit", "innodb_adaptive_hash_index",
    "innodb_adaptive_flushing", "innodb_max_dirty_pages_pct",
    "table_open_cache", "table_definition_cache", "thread_cache_size",
    "tmp_table_size", "max_heap_table_size", "sort_buffer_size",
    "join_buffer_size", "read_buffer_size", "read_rnd_buffer_size",
    "long_query_time", "slow_query_log", "max_connections",
})
SESSION_VARIABLES = frozenset({
    "sort_buffer_size", "join_buffer_size", "tmp_table_size",
    "max_heap_table_size", "read_buffer_size", "read_rnd_buffer_size",
})


class MySQLTemporaryConfiguration:
    def __init__(self, host: str, port: int, user: str, password: str,
                 database: str, config: Mapping[str, Any], label: str = "mysql-lab") -> None:
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.database = database
        self.config = {str(k): v for k, v in config.items()}
        self.label = label
        self.state: Dict[str, Any] = {
            "requested_configuration": dict(self.config),
            "status": "not_started",
            "global_configuration": {},
            "session_configuration": {},
            "skipped_configuration": {},
            "unsafe_configuration": {},
            "pre_settings": {},
            "post_settings": {},
            "restore": None,
        }
        self._original: Dict[str, Any] = {}

    def _connect(self) -> pymysql.Connection:
        return pymysql.connect(host=self.host, port=self.port, user=self.user,
                               password=self.password, database=self.database,
                               charset="utf8mb4", cursorclass=DictCursor,
                               autocommit=True, connect_timeout=10, read_timeout=60)

    def _global_snapshot(self, names: list[str]) -> Dict[str, Any]:
        if not names:
            return {}
        placeholders = ",".join(["%s"] * len(names))
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT VARIABLE_NAME AS name, VARIABLE_VALUE AS value "
                    "FROM performance_schema.global_variables WHERE VARIABLE_NAME IN ({})".format(placeholders),
                    tuple(names),
                )
                return {str(row["name"]): row["value"] for row in cursor.fetchall()}
        finally:
            connection.close()

    @staticmethod
    def _coerce(value: Any) -> Any:
        if isinstance(value, str):
            text = value.strip()
            if re.fullmatch(r"-?\d+", text):
                return int(text)
            if re.fullmatch(r"-?\d+\.\d+", text):
                return float(text)
        return value

    def _buffer_pool_state(self) -> tuple[int, str]:
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT @@GLOBAL.innodb_buffer_pool_size AS size")
                size = int(cursor.fetchone()["size"])
                cursor.execute("SELECT VARIABLE_VALUE AS value FROM performance_schema.global_status "
                               "WHERE VARIABLE_NAME='Innodb_buffer_pool_resize_status'")
                row = cursor.fetchone() or {}
                return size, str(row.get("value") or "")
        finally:
            connection.close()

    def _wait_buffer_pool(self, target: int, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            size, status = self._buffer_pool_state()
            lowered = status.lower()
            if size == int(target) and not any(word in lowered for word in ("resiz", "in progress")):
                return
            time.sleep(0.5)
        raise RuntimeError("timed out resizing innodb_buffer_pool_size to {}".format(target))

    def _set_global(self, name: str, value: Any) -> None:
        coerced = self._coerce(value)
        last_error: Optional[Exception] = None
        for attempt in range(5):
            connection = self._connect()
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SET GLOBAL `{}` = %s".format(name), (coerced,))
            except Exception as exc:
                last_error = exc
                time.sleep(1.0)
                continue
            finally:
                connection.close()
            if name != "innodb_buffer_pool_size":
                return
            try:
                self._wait_buffer_pool(int(coerced), timeout=20.0)
                return
            except RuntimeError as exc:
                last_error = exc
                # Wait for the previous asynchronous resize to finish, then
                # retry the assignment; an in-progress resize can silently
                # ignore a new target without raising from SET GLOBAL.
                deadline = time.monotonic() + 20.0
                while time.monotonic() < deadline:
                    _, status = self._buffer_pool_state()
                    if not any(word in status.lower() for word in ("resiz", "in progress")):
                        break
                    time.sleep(0.5)
        raise RuntimeError("could not set MySQL {}: {}".format(name, last_error))

    def _restore_global(self, values: Mapping[str, Any]) -> Dict[str, Any]:
        errors: Dict[str, str] = {}
        for name, value in values.items():
            try:
                self._set_global(name, value)
            except Exception as exc:
                errors[name] = "{}: {}".format(type(exc).__name__, exc)
        return errors

    def __enter__(self) -> Dict[str, Any]:
        global_settings: Dict[str, Any] = {}
        session_settings: Dict[str, Any] = {}
        for name, value in self.config.items():
            if not _IDENTIFIER.fullmatch(name):
                self.state["unsafe_configuration"][name] = {"value": value, "reason": "unsafe identifier"}
                continue
            if name in UNSAFE_VARIABLES:
                self.state["skipped_configuration"][name] = {"value": value, "reason": "unsafe or restart-only in this demo"}
                continue
            if name in SESSION_VARIABLES:
                session_settings[name] = value
            elif name in DYNAMIC_GLOBAL:
                global_settings[name] = value
            else:
                self.state["skipped_configuration"][name] = {"value": value, "reason": "not in the MySQL live-lab whitelist"}
        self.state["global_configuration"] = dict(global_settings)
        self.state["session_configuration"] = dict(session_settings)
        self._original = self._global_snapshot(list(global_settings))
        self.state["pre_settings"] = dict(self._original)
        applied: Dict[str, Any] = {}
        try:
            for name, value in global_settings.items():
                self._set_global(name, value)
                applied[name] = value
            self.state["post_settings"] = self._global_snapshot(list(global_settings))
            self.state["status"] = "applied"
            return self.state
        except Exception:
            self._restore_global(self._original)
            self.state["status"] = "apply_failed"
            raise

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        errors = self._restore_global(self._original)
        self.state["restore"] = {"previous": dict(self._original), "errors": errors}
        if errors:
            self.state["status"] = "restore_failed"
        else:
            self.state["status"] = "applied_and_restored" if exc_value is None else "restore_after_error"
        return False
