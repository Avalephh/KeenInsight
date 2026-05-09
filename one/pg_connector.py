"""PostgreSQL 连接器 — 采集监控指标 + 应用 knob。

支持：
- 通过 pg_settings 读取当前所有 knob 值
- 通过 pg_stat_database / pg_stat_bgwriter 采集性能指标
- 通过 ALTER SYSTEM SET + pg_reload_conf() 在线应用 sighup 级 knob
- 通过修改 postgresql.conf 应用 postmaster 级 knob（需要重启）
"""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any

import psycopg2
import psycopg2.extras


# ── 默认连接参数（可通过环境变量覆盖）────────────────────────────────────────
PG_HOST     = os.environ.get("PG_HOST",     "127.0.0.1")
PG_PORT     = int(os.environ.get("PG_PORT", "5432"))
PG_USER     = os.environ.get("PG_USER",     "admin")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "password")
PG_DBNAME   = os.environ.get("PG_DBNAME",   "benchbase")
PG_CONF     = os.environ.get("PG_CONF",     "/etc/postgresql/12/main/postgresql.conf")


class PGConnector:
    """PostgreSQL 连接器。"""

    def __init__(
        self,
        host: str = PG_HOST,
        port: int = PG_PORT,
        user: str = PG_USER,
        password: str = PG_PASSWORD,
        dbname: str = PG_DBNAME,
        conf_path: str = PG_CONF,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.dbname = dbname
        self.conf_path = conf_path
        self._conn: Any = None

    # ── 连接管理 ──────────────────────────────────────────────────────────────

    def connect(self) -> None:
        self._conn = psycopg2.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            dbname=self.dbname,
        )
        self._conn.autocommit = True

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _cursor(self):
        if not self._conn or self._conn.closed:
            self.connect()
        return self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def query(self, sql: str, params=None) -> list[dict]:
        with self._cursor() as cur:
            cur.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    # ── 监控指标采集 ──────────────────────────────────────────────────────────

    def collect_metrics(self) -> dict[str, Any]:
        """采集当前数据库性能快照。"""
        rows = self.query("""
            SELECT
                blks_read,
                blks_hit,
                CASE WHEN (blks_read + blks_hit) > 0
                     THEN round(100.0 * blks_hit / (blks_read + blks_hit), 4)
                     ELSE 0 END                          AS cache_hit_pct,
                xact_commit,
                xact_rollback,
                tup_returned,
                tup_fetched,
                tup_inserted,
                tup_updated,
                tup_deleted,
                deadlocks,
                temp_files,
                temp_bytes
            FROM pg_stat_database
            WHERE datname = current_database()
        """)
        db_stats = rows[0] if rows else {}

        bgwriter = self.query("""
            SELECT
                checkpoints_timed,
                checkpoints_req,
                checkpoint_write_time,
                checkpoint_sync_time,
                buffers_checkpoint,
                buffers_clean,
                maxwritten_clean,
                buffers_backend,
                buffers_backend_fsync,
                buffers_alloc
            FROM pg_stat_bgwriter
        """)
        bg_stats = bgwriter[0] if bgwriter else {}

        # Active connections
        conn_rows = self.query("""
            SELECT count(*) AS active_connections
            FROM pg_stat_activity
            WHERE state = 'active'
        """)
        active_conn = conn_rows[0].get("active_connections", 0) if conn_rows else 0

        # Lock waits
        lock_rows = self.query("""
            SELECT count(*) AS lock_waits
            FROM pg_stat_activity
            WHERE wait_event_type = 'Lock'
        """)
        lock_waits = lock_rows[0].get("lock_waits", 0) if lock_rows else 0

        return {
            **{str(k): v for k, v in db_stats.items()},
            **{str(k): v for k, v in bg_stats.items()},
            "active_connections": active_conn,
            "lock_waits": lock_waits,
        }

    def collect_knobs(self) -> dict[str, Any]:
        """读取当前所有可调 knob 的值和元数据。"""
        rows = self.query("""
            SELECT name, setting, unit, vartype, context, min_val, max_val, short_desc
            FROM pg_settings
            WHERE context IN ('postmaster','sighup','superuser','user')
              AND vartype IN ('integer','real','bool')
              AND name NOT LIKE 'log_%'
              AND name NOT LIKE 'debug_%'
              AND name NOT LIKE 'trace_%'
              AND name NOT LIKE 'ssl_%'
            ORDER BY name
        """)
        return {r["name"]: r for r in rows}

    # ── Knob 应用 ─────────────────────────────────────────────────────────────

    def apply_knobs_online(self, knobs: dict[str, Any]) -> dict[str, str]:
        """在线应用 knob（ALTER SYSTEM SET + pg_reload_conf）。

        只能应用 context=sighup/superuser/user 的参数。
        返回 {knob_name: 'ok'|'skip:reason'} 的结果字典。
        """
        all_meta = self.collect_knobs()
        results: dict[str, str] = {}

        for name, value in knobs.items():
            meta = all_meta.get(name)
            if meta is None:
                results[name] = "skip:unknown_knob"
                continue
            if meta["context"] == "postmaster":
                results[name] = "skip:requires_restart"
                continue
            try:
                self.query(f"ALTER SYSTEM SET {name} = %s", (str(value),))
                results[name] = "ok"
            except Exception as e:
                results[name] = f"error:{e}"

        # Reload config to activate sighup-level changes
        applied = [k for k, v in results.items() if v == "ok"]
        if applied:
            self.query("SELECT pg_reload_conf()")

        return results

    def apply_knobs_offline(self, knobs: dict[str, Any]) -> dict[str, str]:
        """离线应用 knob（写入 postgresql.conf，需要重启生效）。

        返回 {knob_name: 'ok'|'skip:reason'} 的结果字典。
        """
        if not os.path.exists(self.conf_path):
            return {k: f"skip:conf_not_found:{self.conf_path}" for k in knobs}

        try:
            with open(self.conf_path, "r") as f:
                content = f.read()
        except PermissionError:
            return {k: "skip:no_read_permission" for k in knobs}

        results: dict[str, str] = {}
        for name, value in knobs.items():
            pattern = re.compile(
                rf"^(\s*#?\s*{re.escape(name)}\s*=\s*)(.+?)(\s*(?:#.*)?)$",
                re.MULTILINE,
            )
            new_line = f"{name} = {value}"
            if pattern.search(content):
                content = pattern.sub(new_line, content)
            else:
                content += f"\n{new_line}\n"
            results[name] = "ok"

        try:
            with open(self.conf_path, "w") as f:
                f.write(content)
        except PermissionError:
            return {k: "skip:no_write_permission" for k in knobs}

        return results
