#!/usr/bin/env python3
"""Join the live SysInsight alert path with asynchronous DREAM SQL tuning.

The bridge has four deliberately separate responsibilities:

* poll Prometheus and PostgreSQL without changing the workload;
* persist every ``pg_stat_statements`` observation, not only slow statements;
* start one SysInsight incident analysis when an alert fires;
* send slow, replayable read-only SQL to DREAM in a worker and publish only a
  validated PostgreSQL plan hint to ``hint_plan.hints``.

``pg_hint_plan`` is the automatic next-execution hook.  A hint-table row is
matched by the normalized SQL text, so applications do not need to be
modified for plan hints.  SQL rewrites, DDL, and session-only knobs are kept
as candidates because PostgreSQL has no generic database-side mechanism to
rewrite arbitrary future client SQL safely.

The default mode is safe with respect to the tuning action: DREAM may measure
session-local candidates, but a global hint is published only after DREAM
reports an improvement and the candidate is a read-only plan hint.  Use
``--configure-hint-table`` once to enable pg_hint_plan for new connections.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path
import pwd
import re
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
DEFAULT_API_BASE = "http://35.212.195.134:28317/v1"
DEFAULT_MODEL = "gpt-5.6-sol"
LOGGER = logging.getLogger("sysinsight-dream-bridge")

sys.path.insert(0, str(ROOT))
from db_profile import profile_summary, resolve_profile  # noqa: E402
from sysinsight_prometheus import (  # noqa: E402
    PrometheusClient,
    PrometheusError,
    build_sysinsight_input,
    collect_window,
)


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PARAMETER = re.compile(r"\$[0-9]+")
_HINT_COMMENT = re.compile(r"/\*\+([\s\S]*?)\*/")
_SQL_STRING = re.compile(r"'(?:''|[^'])*'")
_SQL_NUMBER = re.compile(r"(?<![A-Za-z0-9_])[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?![A-Za-z0-9_])")
_SQL_COMMENT = re.compile(r"--[^\n]*|/\*(?!\+)[\s\S]*?\*/")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def _safe_identifier(value: str, label: str = "identifier") -> str:
    value = str(value)
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("unsafe PostgreSQL {}: {!r}".format(label, value))
    return '"{}"'.format(value.replace('"', '""'))


def _sql_literal(value: Any) -> str:
    return "'{}'".format(str(value).replace("'", "''"))


def _canonical_sql(sql: str) -> str:
    """Build a stable key while retaining the query's structure."""

    text = _HINT_COMMENT.sub(" ", str(sql or ""))
    text = _SQL_COMMENT.sub(" ", text)
    text = _SQL_STRING.sub("?", text)
    text = _PARAMETER.sub("?", text)
    text = _SQL_NUMBER.sub("?", text)
    return " ".join(text.lower().split()).strip().rstrip(";").strip()


def _hint_table_pattern(sql: str) -> str:
    """Convert a live SQL text to pg_hint_plan's ``?`` pattern.

    pg_hint_plan intentionally treats whitespace as significant.  Therefore
    this function only substitutes constants and leaves the original spacing
    intact.  ``pg_stat_statements`` normally already replaces constants with
    ``$1``; activity samples may still contain literal values.
    """

    text = _HINT_COMMENT.sub("", str(sql or "")).strip()
    text = _PARAMETER.sub("?", text)
    text = _SQL_STRING.sub("?", text)
    text = _SQL_NUMBER.sub("?", text)
    # pg_hint_plan's hint-table matcher includes the statement terminator in
    # its normalized query string, even though pg_stat_statements omits it.
    if text and not text.endswith(";"):
        text += ";"
    return text


def _hint_inner(value: str) -> str:
    match = _HINT_COMMENT.search(str(value or ""))
    text = match.group(1).strip() if match else str(value or "").strip()
    if not text:
        raise ValueError("empty pg_hint_plan hint")
    # The bridge only forwards a hint phrase, never arbitrary SQL.  This also
    # prevents a malformed model response from escaping into the hint table.
    if any(token in text for token in ("/*", "*/", "--", ";", "'", '"')):
        raise ValueError("unsafe pg_hint_plan hint")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\s+[^(){};]+)?(?:\([^(){};]*\))?(?:\s+[A-Za-z][A-Za-z0-9_]*(?:\s+[^(){};]+)?(?:\([^(){};]*\))?)*", text):
        raise ValueError("unrecognized pg_hint_plan hint syntax")
    return text


def _read_api_key() -> str:
    for name in ("SYSINSIGHT_GPT_API_KEY", "SYSINSIGHT_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(name, "")
        if value:
            return value
    return ""


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name, "")
        if value:
            return value
    return default


def _read_json_text(raw: str) -> Any:
    value = raw.strip()
    if not value:
        return []
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        # psql can emit a final notice line in a few configurations.  The
        # JSON query is still recoverable from the last non-empty line.
        for line in reversed(value.splitlines()):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    raise ValueError("psql returned invalid JSON: {}".format(value[:500]))


class DatabaseClient:
    """Small psql-backed client usable from both system Python and DREAM."""

    def __init__(
        self,
        db: str,
        user: str,
        host: str,
        port: int,
        run_as: str,
        timeout: float = 30.0,
    ) -> None:
        self.db = db
        self.user = user
        self.host = host
        self.port = int(port)
        self.run_as = run_as
        self.timeout = float(timeout)
        self._stats_time_column: Optional[str] = None

    def _command(self, sql: str, application_name: str) -> List[str]:
        psql = [
            "psql",
            "-X",
            "-A",
            "-t",
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
            "-h",
            self.host,
            "-U",
            self.user,
            "-p",
            str(self.port),
            "-d",
            self.db,
            "-c",
            sql,
        ]
        if os.geteuid() == 0 and self.run_as:
            return [
                "runuser",
                "-u",
                self.run_as,
                "--",
                "env",
                "PGAPPNAME={}".format(application_name),
            ] + psql
        return ["env", "PGAPPNAME={}".format(application_name)] + psql

    def _psql(self, sql: str, application_name: str = "sysinsight-dream-bridge") -> str:
        completed = subprocess.run(
            self._command(sql, application_name),
            cwd="/",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.timeout,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("psql failed ({}): {}".format(completed.returncode, completed.stderr.strip()[:1200]))
        return completed.stdout.strip()

    def _rows(self, sql: str, application_name: str = "sysinsight-dream-bridge") -> List[Dict[str, Any]]:
        wrapped = "SELECT COALESCE(json_agg(row_to_json(x)), '[]'::json)::text FROM ({}) x".format(sql.rstrip(";"))
        value = _read_json_text(self._psql(wrapped, application_name))
        return value if isinstance(value, list) else []

    def execute(self, sql: str, application_name: str = "sysinsight-dream-bridge") -> str:
        return self._psql(sql, application_name)

    def _resolve_stats_columns(self) -> Tuple[str, str, str, str]:
        if self._stats_time_column:
            return self._stats_time_column, self._stats_time_column.replace("total", "min"), self._stats_time_column.replace("total", "max"), self._stats_time_column.replace("total", "mean")
        rows = self._rows(
            "SELECT EXISTS (SELECT 1 FROM pg_attribute WHERE attrelid='pg_stat_statements'::regclass AND attname='total_exec_time' AND NOT attisdropped) AS new_columns",
            "sysinsight-dream-bridge-capability",
        )
        new_columns = bool(rows and rows[0].get("new_columns"))
        prefix = "exec_" if new_columns else ""
        self._stats_time_column = "total_{}time".format(prefix)
        return (
            self._stats_time_column,
            "min_{}time".format(prefix),
            "max_{}time".format(prefix),
            "mean_{}time".format(prefix),
        )

    def collect_statements(self, limit: int = 500) -> List[Dict[str, Any]]:
        total, minimum, maximum, mean = self._resolve_stats_columns()
        limit = max(1, min(int(limit), 5000))
        query = """
            SELECT
                s.userid::text AS userid,
                COALESCE(r.rolname, '') AS username,
                s.dbid::text AS dbid,
                s.queryid::text AS queryid,
                s.query AS query,
                s.calls::bigint AS calls,
                s.{total}::double precision AS total_time_ms,
                s.{minimum}::double precision AS min_time_ms,
                s.{maximum}::double precision AS max_time_ms,
                s.{mean}::double precision AS mean_time_ms,
                s.rows::bigint AS rows,
                s.shared_blks_hit::bigint AS shared_blks_hit,
                s.shared_blks_read::bigint AS shared_blks_read,
                s.shared_blks_dirtied::bigint AS shared_blks_dirtied,
                s.shared_blks_written::bigint AS shared_blks_written,
                s.temp_blks_read::bigint AS temp_blks_read,
                s.temp_blks_written::bigint AS temp_blks_written,
                s.blk_read_time::double precision AS blk_read_time_ms,
                s.blk_write_time::double precision AS blk_write_time_ms
            FROM pg_stat_statements s
            JOIN pg_database d ON d.oid = s.dbid
            LEFT JOIN pg_roles r ON r.oid = s.userid
            WHERE d.datname = current_database()
              AND s.query NOT ILIKE '%pg_stat_statements%'
              AND s.query NOT ILIKE '%hint_plan.hints%'
              AND s.query NOT ILIKE '%sysinsight_dream_bridge%'
            ORDER BY s.{total} DESC
            LIMIT {limit}
        """.format(total=total, minimum=minimum, maximum=maximum, mean=mean, limit=limit)
        rows = self._rows(query, "sysinsight-dream-bridge-statements")
        for row in rows:
            row["database"] = self.db
            row["canonical_sql"] = _canonical_sql(str(row.get("query", "")))
            row["hint_pattern"] = _hint_table_pattern(str(row.get("query", "")))
        return rows

    def collect_active(self, limit: int = 200) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 2000))
        query = """
            SELECT pid::text AS pid,
                   usename AS username,
                   application_name,
                   client_addr::text AS client_addr,
                   state,
                   wait_event_type,
                   wait_event,
                   query,
                   query_start::text AS query_start,
                   EXTRACT(EPOCH FROM (clock_timestamp() - query_start)) * 1000.0 AS duration_ms
            FROM pg_stat_activity
            WHERE datname = current_database()
              AND state = 'active'
              AND pid <> pg_backend_pid()
              AND backend_type = 'client backend'
              AND query NOT ILIKE '%pg_stat_activity%'
              AND query NOT ILIKE '%pg_stat_statements%'
            ORDER BY duration_ms DESC
            LIMIT {limit}
        """.format(limit=limit)
        rows = self._rows(query, "sysinsight-dream-bridge-active")
        for row in rows:
            row["canonical_sql"] = _canonical_sql(str(row.get("query", "")))
        return rows

    def database_snapshot(self) -> Dict[str, Any]:
        rows = self._rows(
            """
            SELECT datname AS database,
                   numbackends,
                   xact_commit,
                   xact_rollback,
                   blks_read,
                   blks_hit,
                   tup_returned,
                   tup_fetched,
                   temp_files,
                   temp_bytes,
                   deadlocks,
                   blk_read_time,
                   blk_write_time
            FROM pg_stat_database
            WHERE datname = current_database()
            """,
            "sysinsight-dream-bridge-database",
        )
        return rows[0] if rows else {"database": self.db}

    def extension_status(self) -> Dict[str, Any]:
        rows = self._rows(
            """
            SELECT json_build_object(
                'extensions', (SELECT json_agg(json_build_object('name', extname, 'version', extversion))
                               FROM pg_extension WHERE extname IN ('pg_hint_plan', 'pg_stat_statements')),
                'hint_table_exists', to_regclass('hint_plan.hints') IS NOT NULL,
                'hint_table_enabled', current_setting('pg_hint_plan.enable_hint_table', true),
                'session_preload_libraries', current_setting('session_preload_libraries', true)
            )::text AS status
            """,
            "sysinsight-dream-bridge-extension",
        )
        if not rows:
            return {}
        value = rows[0].get("status", {})
        return json.loads(value) if isinstance(value, str) else (value or {})

    def database_setting(self, name: str) -> str:
        """Read a database-wide setting without confusing it with session state."""

        rows = self._rows(
            """
            SELECT setting
            FROM pg_db_role_setting s
            CROSS JOIN LATERAL unnest(s.setconfig) AS value(setting)
            WHERE s.setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database())
              AND s.setrole = 0
              AND value.setting LIKE {} || '=%'
            ORDER BY value.setting
            """.format(_sql_literal(name)),
            "sysinsight-dream-bridge-db-setting",
        )
        if not rows:
            return ""
        setting = str(rows[-1].get("setting", ""))
        return setting.split("=", 1)[1] if "=" in setting else ""

    def configure_hint_table(self) -> Dict[str, Any]:
        """Enable hint-table loading for new connections to this database."""

        before = self.extension_status()
        previous_preload = self.database_setting("session_preload_libraries")
        preload_values = [item.strip() for item in previous_preload.split(",") if item.strip()]
        if "pg_hint_plan" not in preload_values:
            preload_values.append("pg_hint_plan")
        preload = ",".join(preload_values)
        database = _safe_identifier(self.db, "database name")
        commands = [
            "CREATE EXTENSION IF NOT EXISTS pg_hint_plan",
            "ALTER DATABASE {} SET session_preload_libraries = {}".format(database, _sql_literal(preload)),
            "ALTER DATABASE {} SET pg_hint_plan.enable_hint_table = 'on'".format(database),
        ]
        for command in commands:
            self.execute(command, "sysinsight-dream-bridge-hint-setup")
        after = self.extension_status()
        return {
            "before": before,
            "before_database_session_preload_libraries": previous_preload,
            "after": after,
            "commands": commands,
            "new_connections_only": True,
        }

    def reset_hint_runtime(self) -> Dict[str, Any]:
        database = _safe_identifier(self.db, "database name")
        commands = [
            "ALTER DATABASE {} RESET session_preload_libraries".format(database),
            "ALTER DATABASE {} RESET pg_hint_plan.enable_hint_table".format(database),
        ]
        for command in commands:
            self.execute(command, "sysinsight-dream-bridge-hint-reset")
        return {"commands": commands, "status": self.extension_status()}

    def upsert_hint(self, norm_query: str, hints: str, application_name: str = "") -> Dict[str, Any]:
        phrase = _hint_inner(hints)
        if not norm_query.strip():
            raise ValueError("empty normalized query pattern")
        insert_sql = """
            INSERT INTO hint_plan.hints(norm_query_string, application_name, hints)
            VALUES ({query}, {app}, {hints})
            ON CONFLICT (norm_query_string, application_name)
            DO UPDATE SET hints = EXCLUDED.hints
        """.format(
            query=_sql_literal(norm_query),
            app=_sql_literal(application_name),
            hints=_sql_literal(phrase),
        )
        self.execute(insert_sql, "sysinsight-dream-bridge-publish")
        rows = self._rows(
            "SELECT id, norm_query_string, application_name, hints FROM hint_plan.hints WHERE norm_query_string = {} AND application_name = {}".format(
                _sql_literal(norm_query), _sql_literal(application_name)
            ),
            "sysinsight-dream-bridge-publish-readback",
        )
        return rows[0] if rows else {"norm_query_string": norm_query, "hints": phrase}

    def remove_hint(self, norm_query: str, application_name: str = "") -> None:
        sql = "DELETE FROM hint_plan.hints WHERE norm_query_string = {} AND application_name = {}".format(
            _sql_literal(norm_query), _sql_literal(application_name)
        )
        self.execute(sql, "sysinsight-dream-bridge-rollback")

    def explain(self, sql: str, hinted: bool = False) -> Any:
        statement = str(sql).strip().rstrip(";")
        if not statement:
            return None
        explain_sql = "EXPLAIN (FORMAT JSON) {}".format(statement)
        raw = self._psql(explain_sql, "sysinsight-dream-bridge-explain")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}


class StateStore:
    """Durable queue/audit state shared by the watcher and worker threads."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init(self) -> None:
        with self._lock, self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS incidents (
                    incident_id TEXT PRIMARY KEY,
                    alert_name TEXT,
                    alert_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    directory TEXT,
                    sysinsight_json TEXT
                );
                CREATE TABLE IF NOT EXISTS sql_observations (
                    sql_key TEXT PRIMARY KEY,
                    database_name TEXT NOT NULL,
                    username TEXT,
                    queryid TEXT,
                    query_text TEXT NOT NULL,
                    canonical_sql TEXT NOT NULL,
                    hint_pattern TEXT NOT NULL,
                    replay_sql TEXT,
                    calls INTEGER NOT NULL DEFAULT 0,
                    total_time_ms REAL NOT NULL DEFAULT 0,
                    min_time_ms REAL NOT NULL DEFAULT 0,
                    max_time_ms REAL NOT NULL DEFAULT 0,
                    mean_time_ms REAL NOT NULL DEFAULT 0,
                    rows_count INTEGER NOT NULL DEFAULT 0,
                    shared_blks_hit INTEGER NOT NULL DEFAULT 0,
                    shared_blks_read INTEGER NOT NULL DEFAULT 0,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    observation_count INTEGER NOT NULL DEFAULT 0,
                    active_sample_count INTEGER NOT NULL DEFAULT 0,
                    last_sample_json TEXT
                );
                CREATE INDEX IF NOT EXISTS sql_observations_slow_idx
                    ON sql_observations(mean_time_ms, max_time_ms, calls);
                CREATE TABLE IF NOT EXISTS sql_observation_samples (
                    sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sql_key TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    calls INTEGER NOT NULL,
                    total_time_ms REAL NOT NULL,
                    min_time_ms REAL NOT NULL,
                    max_time_ms REAL NOT NULL,
                    mean_time_ms REAL NOT NULL,
                    interval_calls INTEGER NOT NULL,
                    interval_time_ms REAL NOT NULL,
                    FOREIGN KEY(sql_key) REFERENCES sql_observations(sql_key)
                );
                CREATE INDEX IF NOT EXISTS sql_observation_samples_lookup_idx
                    ON sql_observation_samples(sql_key, observed_at);
                CREATE TABLE IF NOT EXISTS live_sql_samples (
                    canonical_sql TEXT PRIMARY KEY,
                    query_text TEXT NOT NULL,
                    username TEXT,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dream_jobs (
                    job_id TEXT PRIMARY KEY,
                    sql_key TEXT NOT NULL,
                    incident_id TEXT,
                    status TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    input_path TEXT,
                    output_path TEXT,
                    result_json TEXT,
                    error TEXT,
                    FOREIGN KEY(sql_key) REFERENCES sql_observations(sql_key)
                );
                CREATE INDEX IF NOT EXISTS dream_jobs_lookup_idx
                    ON dream_jobs(sql_key, status, requested_at);
                CREATE TABLE IF NOT EXISTS improvements (
                    improvement_id TEXT PRIMARY KEY,
                    sql_key TEXT NOT NULL,
                    norm_query_string TEXT,
                    application_name TEXT,
                    hints TEXT,
                    rewrite_sql TEXT,
                    fix_action TEXT,
                    root_causes TEXT,
                    status TEXT NOT NULL,
                    old_time REAL,
                    new_time REAL,
                    improvement_ratio REAL,
                    validation_json TEXT,
                    created_at TEXT NOT NULL,
                    activated_at TEXT,
                    hit_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS improvements_lookup_idx
                    ON improvements(sql_key, status, created_at);
                """
            )

    def recover_jobs(self) -> List[str]:
        """Recover jobs left behind by a terminated bridge process.

        The bridge is intended to have one owner per state database.  If the
        process is killed while a DREAM subprocess is running, SQLite cannot
        receive the normal ``finished_job`` update.  Re-queue those jobs on
        the next owner startup and return all queued work so it can be
        submitted immediately.
        """

        with self._lock, self._connection() as conn:
            conn.execute(
                """
                UPDATE dream_jobs
                SET status='queued', started_at=NULL, finished_at=NULL,
                    error='requeued after bridge restart'
                WHERE status='running'
                """
            )
            rows = conn.execute(
                "SELECT job_id FROM dream_jobs WHERE status='queued' ORDER BY requested_at"
            ).fetchall()
            return [str(row["job_id"]) for row in rows]

    @staticmethod
    def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        return dict(row) if row is not None else None

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, _json(value)),
            )

    def start_incident(self, alert: Mapping[str, Any], directory: Path) -> str:
        digest = hashlib.sha1((_json(alert) + str(time.time_ns())).encode()).hexdigest()[:12]
        incident_id = "incident-{}-{}".format(dt.datetime.now().strftime("%Y%m%d%H%M%S"), digest)
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT INTO incidents(incident_id,alert_name,alert_json,started_at,status,directory) VALUES(?,?,?,?,?,?)",
                (incident_id, (alert.get("labels", {}) or {}).get("alertname", ""), _json(alert), utc_now(), "running", str(directory)),
            )
        return incident_id

    def finish_incident(self, incident_id: str, status: str, sysinsight: Optional[Mapping[str, Any]] = None) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                "UPDATE incidents SET finished_at=?, status=?, sysinsight_json=? WHERE incident_id=?",
                (utc_now(), status, _json(sysinsight) if sysinsight is not None else None, incident_id),
            )

    def incident_directory(self, incident_id: Optional[str]) -> Optional[Path]:
        if not incident_id:
            return None
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT directory FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
            if row is None or not row["directory"]:
                return None
            return Path(str(row["directory"]))

    def upsert_observations(self, rows: Sequence[Mapping[str, Any]], active_rows: Sequence[Mapping[str, Any]]) -> List[str]:
        now = utc_now()
        active_by_canonical: Dict[str, Mapping[str, Any]] = {}
        for active in active_rows:
            canonical = str(active.get("canonical_sql") or _canonical_sql(str(active.get("query", ""))))
            if canonical and canonical not in active_by_canonical:
                active_by_canonical[canonical] = active
        keys: List[str] = []
        with self._lock, self._connection() as conn:
            for active in active_rows:
                canonical = str(active.get("canonical_sql") or _canonical_sql(str(active.get("query", ""))))
                active_query = str(active.get("query", "")).strip()
                if canonical and active_query and "$" not in active_query and "?" not in active_query:
                    conn.execute(
                        """
                        INSERT INTO live_sql_samples(canonical_sql,query_text,username,first_seen,last_seen)
                        VALUES(?,?,?,?,?)
                        ON CONFLICT(canonical_sql) DO UPDATE SET
                            query_text=excluded.query_text,
                            username=excluded.username,
                            last_seen=excluded.last_seen
                        """,
                        (canonical, active_query, str(active.get("username", "")), now, now),
                    )
            for row in rows:
                query = str(row.get("query", "")).strip()
                canonical = str(row.get("canonical_sql") or _canonical_sql(query))
                if not canonical:
                    continue
                queryid = str(row.get("queryid", ""))
                username = str(row.get("username", ""))
                key = "{}:{}:{}:{}".format(row.get("database", ""), username, queryid, hashlib.sha256(canonical.encode()).hexdigest()[:20])
                active = active_by_canonical.get(canonical)
                saved_sample = conn.execute(
                    "SELECT query_text, username FROM live_sql_samples WHERE canonical_sql=?",
                    (canonical,),
                ).fetchone()
                replay_sql = None
                active_sample_count = 0
                if query and "$" not in query and "?" not in query:
                    replay_sql = query
                if active:
                    active_query = str(active.get("query", "")).strip()
                    if active_query and "$" not in active_query and "?" not in active_query:
                        replay_sql = active_query
                        active_sample_count = 1
                if replay_sql is None and saved_sample is not None:
                    replay_sql = str(saved_sample["query_text"])
                    active_sample_count = 1
                existing = conn.execute("SELECT replay_sql, active_sample_count FROM sql_observations WHERE sql_key=?", (key,)).fetchone()
                previous = conn.execute(
                    "SELECT calls, total_time_ms FROM sql_observations WHERE sql_key=?",
                    (key,),
                ).fetchone()
                if replay_sql is None and existing is not None:
                    replay_sql = existing["replay_sql"]
                if existing is not None:
                    active_sample_count += int(existing["active_sample_count"] or 0)
                conn.execute(
                    """
                    INSERT INTO sql_observations(
                        sql_key,database_name,username,queryid,query_text,canonical_sql,hint_pattern,
                        replay_sql,calls,total_time_ms,min_time_ms,max_time_ms,mean_time_ms,rows_count,
                        shared_blks_hit,shared_blks_read,first_seen,last_seen,observation_count,
                        active_sample_count,last_sample_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(sql_key) DO UPDATE SET
                        query_text=excluded.query_text,
                        hint_pattern=excluded.hint_pattern,
                        replay_sql=COALESCE(excluded.replay_sql, sql_observations.replay_sql),
                        calls=excluded.calls,
                        total_time_ms=excluded.total_time_ms,
                        min_time_ms=excluded.min_time_ms,
                        max_time_ms=excluded.max_time_ms,
                        mean_time_ms=excluded.mean_time_ms,
                        rows_count=excluded.rows_count,
                        shared_blks_hit=excluded.shared_blks_hit,
                        shared_blks_read=excluded.shared_blks_read,
                        last_seen=excluded.last_seen,
                        observation_count=sql_observations.observation_count + 1,
                        active_sample_count=excluded.active_sample_count,
                        last_sample_json=excluded.last_sample_json
                    """,
                    (
                        key,
                        str(row.get("database", "")),
                        username,
                        queryid,
                        query,
                        canonical,
                        str(row.get("hint_pattern") or _hint_table_pattern(query)),
                        replay_sql,
                        int(row.get("calls", 0) or 0),
                        float(row.get("total_time_ms", 0) or 0),
                        float(row.get("min_time_ms", 0) or 0),
                        float(row.get("max_time_ms", 0) or 0),
                        float(row.get("mean_time_ms", 0) or 0),
                        int(row.get("rows", 0) or 0),
                        int(row.get("shared_blks_hit", 0) or 0),
                        int(row.get("shared_blks_read", 0) or 0),
                        now,
                        now,
                        1,
                        active_sample_count,
                        _json(active) if active else None,
                    ),
                )
                previous_calls = int(previous["calls"] or 0) if previous is not None else 0
                previous_total = float(previous["total_time_ms"] or 0) if previous is not None else 0.0
                current_calls = int(row.get("calls", 0) or 0)
                current_total = float(row.get("total_time_ms", 0) or 0)
                conn.execute(
                    """
                    INSERT INTO sql_observation_samples(
                        sql_key,observed_at,calls,total_time_ms,min_time_ms,max_time_ms,mean_time_ms,
                        interval_calls,interval_time_ms
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        key,
                        now,
                        current_calls,
                        current_total,
                        float(row.get("min_time_ms", 0) or 0),
                        float(row.get("max_time_ms", 0) or 0),
                        float(row.get("mean_time_ms", 0) or 0),
                        max(0, current_calls - previous_calls),
                        max(0.0, current_total - previous_total),
                    ),
                )
                keys.append(key)
        return keys

    def slow_observations(self, mean_ms: float, max_ms: float, min_calls: int, limit: int) -> List[Dict[str, Any]]:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM sql_observations
                WHERE calls >= ? AND (mean_time_ms >= ? OR max_time_ms >= ?)
                ORDER BY CASE WHEN mean_time_ms >= ? THEN mean_time_ms ELSE max_time_ms END DESC
                LIMIT ?
                """,
                (int(min_calls), float(mean_ms), float(max_ms), float(mean_ms), int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_observation(self, sql_key: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connection() as conn:
            return self._row(conn.execute("SELECT * FROM sql_observations WHERE sql_key=?", (sql_key,)).fetchone())

    def enqueue_job(self, sql_key: str, incident_id: Optional[str], cooldown: float) -> Optional[str]:
        now = time.time()
        with self._lock, self._connection() as conn:
            active = conn.execute(
                "SELECT 1 FROM improvements WHERE sql_key=? AND status='active' LIMIT 1", (sql_key,)
            ).fetchone()
            if active:
                return None
            existing = conn.execute(
                "SELECT status, requested_at FROM dream_jobs WHERE sql_key=? ORDER BY requested_at DESC LIMIT 1",
                (sql_key,),
            ).fetchone()
            if existing and existing["status"] in {"queued", "running"}:
                return None
            if existing and existing["status"] in {"completed", "candidate", "blocked", "failed"}:
                replayable = conn.execute(
                    "SELECT replay_sql FROM sql_observations WHERE sql_key=?",
                    (sql_key,),
                ).fetchone()
                if existing["status"] == "blocked" and replayable and replayable["replay_sql"]:
                    pass
                else:
                    try:
                        requested = dt.datetime.fromisoformat(existing["requested_at"]).timestamp()
                        if now - requested < cooldown:
                            return None
                    except (TypeError, ValueError):
                        pass
            job_id = "dream-{}-{}".format(dt.datetime.now().strftime("%Y%m%d%H%M%S"), hashlib.sha1((sql_key + str(now)).encode()).hexdigest()[:10])
            conn.execute(
                "INSERT INTO dream_jobs(job_id,sql_key,incident_id,status,requested_at) VALUES(?,?,?,?,?)",
                (job_id, sql_key, incident_id, "queued", utc_now()),
            )
            return job_id

    def start_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connection() as conn:
            conn.execute(
                "UPDATE dream_jobs SET status='running', started_at=?, attempts=attempts+1 WHERE job_id=? AND status='queued'",
                (utc_now(), job_id),
            )
            row = conn.execute("SELECT * FROM dream_jobs WHERE job_id=?", (job_id,)).fetchone()
            return self._row(row)

    def finish_job(
        self,
        job_id: str,
        status: str,
        result: Optional[Mapping[str, Any]] = None,
        error: str = "",
        input_path: Optional[Path] = None,
        output_path: Optional[Path] = None,
    ) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                "UPDATE dream_jobs SET status=?, finished_at=?, result_json=?, error=?, input_path=?, output_path=? WHERE job_id=?",
                (status, utc_now(), _json(result) if result is not None else None, error[:4000], str(input_path) if input_path else None, str(output_path) if output_path else None, job_id),
            )

    def add_improvement(self, value: Mapping[str, Any]) -> str:
        improvement_id = str(value.get("improvement_id") or "improvement-{}".format(hashlib.sha1((_json(value) + str(time.time_ns())).encode()).hexdigest()[:16]))
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO improvements(
                    improvement_id,sql_key,norm_query_string,application_name,hints,rewrite_sql,fix_action,
                    root_causes,status,old_time,new_time,improvement_ratio,validation_json,created_at,activated_at,hit_count
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    improvement_id,
                    value.get("sql_key", ""),
                    value.get("norm_query_string"),
                    value.get("application_name", ""),
                    value.get("hints"),
                    value.get("rewrite_sql"),
                    value.get("fix_action"),
                    _json(value.get("root_causes", [])),
                    value.get("status", "candidate"),
                    value.get("old_time"),
                    value.get("new_time"),
                    value.get("improvement_ratio"),
                    _json(value.get("validation", {})),
                    value.get("created_at", utc_now()),
                    value.get("activated_at"),
                    int(value.get("hit_count", 0) or 0),
                ),
            )
        return improvement_id

    def recent(self, limit: int = 20) -> Dict[str, Any]:
        with self._lock, self._connection() as conn:
            def all_rows(sql: str) -> List[Dict[str, Any]]:
                return [dict(row) for row in conn.execute(sql, (int(limit),)).fetchall()]

            return {
                "database": str(self.path),
                "incidents": all_rows("SELECT * FROM incidents ORDER BY started_at DESC LIMIT ?"),
                "jobs": all_rows("SELECT * FROM dream_jobs ORDER BY requested_at DESC LIMIT ?"),
                "improvements": all_rows("SELECT * FROM improvements ORDER BY created_at DESC LIMIT ?"),
            }


def _profile_for(dbms: str, version: str) -> Dict[str, Any]:
    try:
        return profile_summary(resolve_profile(dbms, version))
    except Exception as exc:
        return {"name": "{}/{}".format(dbms, version), "status": "unavailable", "error": str(exc)}


def _live_case(
    database: str,
    schema: str,
    workload: str,
    alert: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    slow_rows: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    now = utc_now()
    return {
        "id": "live-{}".format(dt.datetime.now().strftime("%Y%m%d%H%M%S")),
        "title": "live Prometheus alert",
        "workload": workload,
        "database_schema": schema,
        "mode": "live_alert",
        "external_event": "prometheus_alert",
        "observed_at": now,
        "slow_queries": [
            {
                "queryid": row.get("queryid"),
                "username": row.get("username"),
                "query": row.get("query"),
                "mean_time_ms": row.get("mean_time_ms"),
                "max_time_ms": row.get("max_time_ms"),
                "calls": row.get("calls"),
                "replayable": bool(row.get("replay_sql")),
            }
            for row in slow_rows
        ],
        "anomaly": {
            "trigger": alert,
            "samples": {"first": dict(snapshot), "last": dict(snapshot), "trigger": alert},
            "metrics": {"slow_query_count": len(slow_rows)},
        },
    }


def _api_json(api_base: str, api_key: str, path: str, payload: Mapping[str, Any], timeout: float = 120.0) -> Dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        api_base.rstrip("/") + path,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", errors="replace")
        raise RuntimeError("{} HTTP {}: {}".format(path, exc.code, detail)) from exc
    except URLError as exc:
        raise RuntimeError("{} unavailable: {}".format(path, exc.reason)) from exc
    if not isinstance(result, dict):
        raise RuntimeError("{} returned a non-object response".format(path))
    return result


class Bridge:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.store = StateStore(Path(args.state_db))
        self.db = DatabaseClient(args.db, args.db_user, args.host, args.port, args.run_as, args.db_timeout)
        self.prometheus = PrometheusClient(args.prometheus_url, timeout=args.prometheus_timeout)
        self.output_root = Path(args.output).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.dream_workers))
        self.sysinsight_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.futures: List[concurrent.futures.Future[Any]] = []
        self.last_alert_key: Optional[str] = None
        self.current_incident_id: Optional[str] = None
        self.current_incident_dir: Optional[Path] = None
        self._sysinsight_lock = threading.Lock()
        self._closed = False
        self._forced_alert: Dict[str, Any] = {
            "state": "firing",
            "labels": {"alertname": self.args.alert_name or "ForcedSysInsightAlert"},
            "annotations": {"description": "forced local bridge test"},
            "startsAt": utc_now(),
            "source": "--force-alert",
        }
        recovered_jobs: List[str] = []
        if not getattr(args, "reset_hint_runtime", False):
            recovered_jobs = self.store.recover_jobs()
            for job_id in recovered_jobs:
                self.futures.append(self.executor.submit(self._run_dream_job, job_id))
            if recovered_jobs:
                LOGGER.warning("recovered %d queued DREAM job(s) from the previous bridge run", len(recovered_jobs))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        wait = bool(self.args.wait_for_jobs)
        for executor in (self.executor, self.sysinsight_executor):
            try:
                executor.shutdown(wait=wait, cancel_futures=False)
            except TypeError:
                # Python 3.8 does not expose cancel_futures.
                executor.shutdown(wait=wait)

    def configure(self) -> Dict[str, Any]:
        result = self.db.configure_hint_table()
        _write_json(self.output_root / "hint_table_setup.json", result)
        return result

    def reset_hint_runtime(self) -> Dict[str, Any]:
        result = self.db.reset_hint_runtime()
        _write_json(self.output_root / "hint_table_reset.json", result)
        return result

    def _alert_key(self, alert: Mapping[str, Any]) -> str:
        labels = alert.get("labels", {}) if isinstance(alert, Mapping) else {}
        return _json({"labels": labels, "startsAt": alert.get("startsAt") if isinstance(alert, Mapping) else None})

    def _current_alert(self) -> Optional[Dict[str, Any]]:
        if self.args.force_alert:
            return self._forced_alert
        try:
            return self.prometheus.firing_alert(self.args.alert_name)
        except PrometheusError as exc:
            LOGGER.warning("Prometheus alert query failed: %s", exc)
            return None

    def _collect_prometheus(self, seconds: float) -> Dict[str, Any]:
        end = time.time()
        start = end - max(1.0, seconds)
        try:
            return collect_window(
                self.prometheus,
                self.args.db,
                start,
                end,
                self.args.prometheus_step,
                self.args.alert_name,
            )
        except Exception as exc:
            return {
                "status": "failed",
                "source": {"type": "prometheus", "url": self.args.prometheus_url},
                "error": "{}: {}".format(type(exc).__name__, exc),
                "queries": {},
                "alerts": {"status": "failed", "selected": None, "all": []},
                "window": {"start": start, "end": end, "step": self.args.prometheus_step},
            }

    def _launch_sysinsight(self, incident_id: str, incident_dir: Path, alert: Mapping[str, Any], snapshot: Mapping[str, Any], slow_rows: Sequence[Mapping[str, Any]]) -> None:
        # Keep alert analysis independent from the DREAM pool: a slow API call
        # must not delay a queued SQL tuning job (or vice versa).
        future = self.sysinsight_executor.submit(self._run_sysinsight, incident_id, incident_dir, alert, dict(snapshot), list(slow_rows))
        self.futures.append(future)

    def _run_sysinsight(self, incident_id: str, incident_dir: Path, alert: Mapping[str, Any], snapshot: Mapping[str, Any], slow_rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {"status": "running", "incident_id": incident_id, "started_at": utc_now()}
        try:
            prometheus = self._collect_prometheus(self.args.sysinsight_window)
            case = _live_case(self.args.db, self.args.db_schema, self.args.workload, alert, snapshot, slow_rows)
            profile = _profile_for(self.args.dbms, self.args.pg_version)
            input_payload = build_sysinsight_input(
                case,
                prometheus,
                profile,
                database_name=self.args.db,
                database_schema=self.args.db_schema,
            )
            input_payload["bridge"] = {
                "incident_id": incident_id,
                "sql_observation_store": str(self.store.path),
                "dream_queue": "asynchronous",
                "auto_apply": "pg_hint_plan.hints",
            }
            _write_json(incident_dir / "prometheus_capture.json", prometheus)
            _write_json(incident_dir / "case_result.json", case)
            _write_json(incident_dir / "sysinsight_input.json", input_payload)

            api_key = _read_api_key()
            if self.args.no_api:
                analysis = {"status": "skipped", "reason": "--no-api"}
            elif not api_key:
                analysis = {"status": "skipped", "reason": "SysInsight API key environment variable is not set"}
            else:
                prompt = (
                    "你是 SysInsight 的在线告警分析器。只基于下面的真实观测做系统级根因分析，"
                    "不要执行任何数据库变更。返回 JSON，字段为 severity、root_cause、evidence、"
                    "recommended_next_checks、confidence。\n\n"
                    + json.dumps(input_payload, ensure_ascii=False, indent=2, default=str)
                )
                response = _api_json(
                    self.args.api_base,
                    api_key,
                    "/chat/completions",
                    {
                        "model": self.args.model,
                        "temperature": 0,
                        "messages": [
                            {"role": "system", "content": "你是一个严谨的 PostgreSQL OLAP 性能告警分析器。"},
                            {"role": "user", "content": prompt},
                        ],
                    },
                    timeout=self.args.api_timeout,
                )
                content = ""
                choices = response.get("choices", [])
                if choices and isinstance(choices[0], dict):
                    message = choices[0].get("message", {})
                    content = message.get("content", "") if isinstance(message, dict) else ""
                analysis = {"status": "completed", "model": self.args.model, "response": response, "content": content}
            result = {"status": "completed", "incident_id": incident_id, "input": str(incident_dir / "sysinsight_input.json"), "analysis": analysis, "finished_at": utc_now()}
            _write_json(incident_dir / "sysinsight_analysis.json", result)
            if self.args.sysinsight_case_result:
                result["existing_pipeline"] = self._launch_existing_sysinsight(incident_dir)
            self.store.finish_incident(incident_id, "completed", result)
            return result
        except Exception as exc:
            result = {"status": "failed", "incident_id": incident_id, "error": "{}: {}".format(type(exc).__name__, exc), "traceback": traceback.format_exc(), "finished_at": utc_now()}
            _write_json(incident_dir / "sysinsight_analysis.json", result)
            self.store.finish_incident(incident_id, "failed", result)
            LOGGER.exception("SysInsight incident %s failed", incident_id)
            return result

    def _launch_existing_sysinsight(self, incident_dir: Path) -> Dict[str, Any]:
        command = [
            sys.executable,
            str(ROOT / "sysinsight_pipeline.py"),
            "--case-result",
            str(Path(self.args.sysinsight_case_result).resolve()),
            "--prometheus-capture",
            str(incident_dir / "prometheus_capture.json"),
            "--db",
            self.args.db,
            "--dbms",
            self.args.dbms,
            "--pg-version",
            self.args.pg_version,
            "--api-base",
            self.args.api_base,
            "--model",
            self.args.model,
            "--output",
            str(incident_dir / "sysinsight_pipeline"),
        ]
        log_path = incident_dir / "sysinsight_pipeline.log"
        with log_path.open("w", encoding="utf-8") as log:
            completed = subprocess.run(command, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, text=True, check=False)
        return {"status": "completed" if completed.returncode == 0 else "failed", "returncode": completed.returncode, "log": str(log_path)}

    def _new_incident_if_needed(self, alert: Optional[Mapping[str, Any]], snapshot: Mapping[str, Any], slow_rows: Sequence[Mapping[str, Any]]) -> None:
        if not alert:
            self.last_alert_key = None
            self.current_incident_id = None
            self.current_incident_dir = None
            return
        key = self._alert_key(alert)
        if key == self.last_alert_key:
            return
        self.last_alert_key = key
        suffix = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
        incident_dir = self.output_root / "incidents" / "{}_{}".format(dt.datetime.now().strftime("%Y%m%d_%H%M%S"), suffix)
        incident_dir.mkdir(parents=True, exist_ok=False)
        incident_id = self.store.start_incident(alert, incident_dir)
        self.current_incident_id = incident_id
        self.current_incident_dir = incident_dir
        _write_json(incident_dir / "alert.json", dict(alert))
        self._launch_sysinsight(incident_id, incident_dir, alert, snapshot, slow_rows)
        LOGGER.info("Alert %s triggered SysInsight incident %s", (alert.get("labels", {}) or {}).get("alertname"), incident_id)

    def _job_config(self, job_dir: Path) -> Path:
        config_path = Path(self.args.dream_config).resolve()
        configs = json.loads(config_path.read_text(encoding="utf-8"))
        runtime_root = Path(self.args.dream_runtime_root).resolve()
        source_root = (ROOT.parent / "dream").resolve()

        def relocate(value: Any) -> Any:
            if isinstance(value, str) and value.startswith(str(source_root)):
                return str(runtime_root) + value[len(str(source_root)):]
            if isinstance(value, dict):
                return {key: relocate(item) for key, item in value.items()}
            if isinstance(value, list):
                return [relocate(item) for item in value]
            return value

        configs = relocate(configs)
        database = configs.setdefault("DATABASE_CONFIG", {})
        database.update({
            "user": self.args.db_user,
            "host": self.args.host,
            "port": self.args.port,
            "dbname": self.args.db,
            "schema": self.args.db_schema,
            "search_path": [self.args.db_schema, "public"] if self.args.db_schema != "public" else ["public"],
        })
        output = job_dir / "dream_config.json"
        _write_json(output, configs)
        return output

    def _dream_python(self) -> str:
        if self.args.dream_python:
            return self.args.dream_python
        candidate = Path(self.args.dream_runtime_root).resolve() / ".venv" / "bin" / "python"
        return str(candidate) if candidate.exists() else sys.executable

    def _run_dream_job(self, job_id: str) -> Dict[str, Any]:
        job = self.store.start_job(job_id)
        if not job:
            return {"status": "missing"}
        observation = self.store.get_observation(str(job["sql_key"]))
        if not observation:
            error = "SQL observation disappeared"
            self.store.finish_job(job_id, "failed", error=error)
            return {"status": "failed", "error": error}
        replay_sql = str(observation.get("replay_sql") or "").strip()
        if not replay_sql or "$" in replay_sql or "?" in replay_sql:
            error = "no replayable SQL sample; pg_stat_statements text is parameterized"
            self.store.finish_job(job_id, "blocked", result={"status": "blocked", "reason": error})
            LOGGER.info("DREAM job %s waiting for a literal SQL sample", job_id)
            return {"status": "blocked", "reason": error}

        incident_dir = self.store.incident_directory(job.get("incident_id")) or self.current_incident_dir or self.output_root / "jobs"
        job_dir = incident_dir / "dream_jobs" / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        if self.args.dream_run_as and os.geteuid() == 0:
            try:
                account = pwd.getpwnam(self.args.dream_run_as)
                os.chown(str(job_dir), account.pw_uid, account.pw_gid)
                os.chmod(str(job_dir), 0o750)
            except (KeyError, OSError) as exc:
                LOGGER.warning("could not grant DREAM worker access to %s: %s", job_dir, exc)
        output_path = job_dir / "result.json"
        config_path = self._job_config(job_dir)
        input_payload = {
            "schema": "sysinsight.dream.live-job.v1",
            "job_id": job_id,
            "sql_key": observation["sql_key"],
            "queryid": observation.get("queryid"),
            "query": replay_sql,
            "query_text_from_pg_stat_statements": observation.get("query_text"),
            "mean_time_ms": observation.get("mean_time_ms"),
            "max_time_ms": observation.get("max_time_ms"),
            "calls": observation.get("calls"),
            "hint_pattern": observation.get("hint_pattern"),
            "requested_at": utc_now(),
        }
        input_path = job_dir / "input.json"
        _write_json(input_path, input_payload)
        adapter_path = Path(self.args.dream_runtime_root).resolve() / "perf-anomaly-demo" / "dream_live_adapter.py"
        if not adapter_path.exists():
            adapter_path = ROOT / "dream_live_adapter.py"
        command = [
            self._dream_python(),
            str(adapter_path),
            "--config",
            str(config_path),
            "--sql",
            replay_sql,
            "--query-id",
            str(observation.get("sql_key")),
            "--queryid",
            str(observation.get("queryid") or ""),
            "--mean-time-ms",
            str(observation.get("mean_time_ms") or 0),
            "--max-time-ms",
            str(observation.get("max_time_ms") or 0),
            "--calls",
            str(observation.get("calls") or 1),
            "--output",
            str(output_path),
        ]
        if self.args.dream_root_cause:
            command.extend(["--root-cause", self.args.dream_root_cause])
        env = os.environ.copy()
        if self.args.dream_offline:
            env["DREAM_OFFLINE"] = "1"
        if self.args.dream_offline_hint:
            env["DREAM_OFFLINE_HINT"] = self.args.dream_offline_hint
        runtime_root = Path(self.args.dream_runtime_root).resolve()
        venv_site_packages = [
            str(path)
            for path in (runtime_root / ".venv" / "lib").glob("python*/site-packages")
            if path.is_dir()
        ]
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(runtime_root), str(runtime_root / "perf-anomaly-demo"), *venv_site_packages, str(ROOT), env.get("PYTHONPATH", "")]))
        if self.args.dream_run_as and os.geteuid() == 0:
            command = ["runuser", "-u", self.args.dream_run_as, "--preserve-environment", "--"] + command
        stdout_path = job_dir / "dream.log"
        try:
            with stdout_path.open("w", encoding="utf-8") as log:
                completed = subprocess.run(
                    command,
                    cwd=str(Path(self.args.dream_runtime_root).resolve() / "dream"),
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=self.args.dream_timeout,
                    check=False,
                )
            if output_path.exists():
                result = json.loads(output_path.read_text(encoding="utf-8"))
            else:
                result = {"status": "failed", "error": "DREAM produced no result.json"}
            result["returncode"] = completed.returncode
            result["log"] = str(stdout_path)
            if completed.returncode != 0 or result.get("status") == "failed":
                self.store.finish_job(job_id, "failed", result=result, error=str(result.get("error", "DREAM failed")), input_path=input_path, output_path=output_path)
                return result
            publication = self._validate_and_publish(job_id, observation, result)
            result["bridge_publication"] = publication
            status = "completed" if publication.get("status") in {"active", "candidate", "no_action"} else "failed"
            self.store.finish_job(job_id, status, result=result, error=publication.get("reason", ""), input_path=input_path, output_path=output_path)
            return result
        except subprocess.TimeoutExpired:
            error = "DREAM timed out after {} seconds".format(self.args.dream_timeout)
            self.store.finish_job(job_id, "failed", error=error, input_path=input_path, output_path=output_path)
            return {"status": "failed", "error": error}
        except Exception as exc:
            error = "{}: {}".format(type(exc).__name__, exc)
            self.store.finish_job(job_id, "failed", error=error, input_path=input_path, output_path=output_path)
            LOGGER.exception("DREAM job %s failed", job_id)
            return {"status": "failed", "error": error}

    def _validate_and_publish(self, job_id: str, observation: Mapping[str, Any], result: Mapping[str, Any]) -> Dict[str, Any]:
        evaluation_status = result.get("evaluation_status")
        old_time = float(result.get("old_time") or observation.get("mean_time_ms", 0) / 1000.0 or 0)
        new_time_value = result.get("new_time")
        try:
            new_time = float(new_time_value) if new_time_value is not None else 0.0
        except (TypeError, ValueError):
            new_time = 0.0
        ratio = (old_time - new_time) / old_time if old_time > 0 and new_time >= 0 else 0.0
        rewrite_sql = str(result.get("rewrite_sql") or "")
        fix_action = str(result.get("fix_action") or "")
        hint_match = _HINT_COMMENT.search(rewrite_sql) or _HINT_COMMENT.search(fix_action)
        validation: Dict[str, Any] = {
            "job_id": job_id,
            "evaluation_status": evaluation_status,
            "old_time_seconds": old_time,
            "new_time_seconds": new_time,
            "improvement_ratio": ratio,
            "read_only": bool(result.get("read_only")),
            "hint_table_runtime": self.db.extension_status(),
        }
        if hint_match:
            try:
                phrase = _hint_inner(hint_match.group(0))
            except ValueError as exc:
                phrase = ""
                validation["hint_error"] = str(exc)
        else:
            phrase = ""

        if not phrase:
            status = "candidate" if rewrite_sql or fix_action else "no_action"
            reason = "DREAM returned no publishable pg_hint_plan hint"
            self.store.add_improvement({
                "sql_key": observation["sql_key"],
                "norm_query_string": observation.get("hint_pattern"),
                "rewrite_sql": rewrite_sql,
                "fix_action": fix_action,
                "root_causes": result.get("root_causes", []),
                "status": status,
                "old_time": old_time,
                "new_time": new_time,
                "improvement_ratio": ratio,
                "validation": {**validation, "reason": reason, "auto_apply": "not_supported_for_rewrite_or_session_action"},
            })
            return {"status": status, "reason": reason, "auto_apply": False}

        validation["hint_phrase"] = phrase
        validation["norm_query_string"] = observation.get("hint_pattern")
        validation["plan_validation"] = "not_run"
        replay_sql = str(observation.get("replay_sql") or "")
        if replay_sql and "$" not in replay_sql and "?" not in replay_sql:
            try:
                baseline_plan = self.db.explain(replay_sql)
                hinted_sql = _HINT_COMMENT.search(rewrite_sql)
                hinted_statement = rewrite_sql if hinted_sql else "/*+ {} */ {}".format(phrase, replay_sql)
                hinted_plan = self.db.explain(hinted_statement)
                validation["plan_validation"] = {
                    "baseline": baseline_plan,
                    "hinted": hinted_plan,
                    "changed": _json(baseline_plan) != _json(hinted_plan),
                }
            except Exception as exc:
                validation["plan_validation"] = {"status": "failed", "error": str(exc)}

        accepted = (
            int(evaluation_status) == 1
            if isinstance(evaluation_status, (int, float, str)) and str(evaluation_status).lstrip("-").isdigit()
            else False
        ) and bool(result.get("read_only")) and ratio >= self.args.min_improvement
        if not accepted:
            reason = "DREAM candidate did not pass read-only and improvement policy"
            self.store.add_improvement({
                "sql_key": observation["sql_key"],
                "norm_query_string": observation.get("hint_pattern"),
                "application_name": self.args.hint_application_name,
                "hints": phrase,
                "rewrite_sql": rewrite_sql,
                "fix_action": fix_action,
                "root_causes": result.get("root_causes", []),
                "status": "candidate",
                "old_time": old_time,
                "new_time": new_time,
                "improvement_ratio": ratio,
                "validation": {**validation, "reason": reason},
            })
            return {"status": "candidate", "reason": reason, "auto_apply": False, "validation": validation}

        if not self.args.auto_apply:
            reason = "--no-auto-apply"
            status = "candidate"
        else:
            runtime = validation.get("hint_table_runtime", {})
            enabled = str(runtime.get("hint_table_enabled", "")).lower() in {"on", "true", "1"}
            exists = bool(runtime.get("hint_table_exists"))
            if not exists or not enabled:
                status = "candidate"
                reason = "pg_hint_plan hint table is not enabled for this database"
            else:
                published = self.db.upsert_hint(
                    str(observation.get("hint_pattern") or _hint_table_pattern(str(observation.get("query_text", "")))),
                    phrase,
                    self.args.hint_application_name,
                )
                status = "active"
                reason = "published to hint_plan.hints; new matching executions are automatically hinted"
                validation["published_row"] = published

        self.store.add_improvement({
            "sql_key": observation["sql_key"],
            "norm_query_string": observation.get("hint_pattern"),
            "application_name": self.args.hint_application_name,
            "hints": phrase,
            "rewrite_sql": rewrite_sql,
            "fix_action": fix_action,
            "root_causes": result.get("root_causes", []),
            "status": status,
            "old_time": old_time,
            "new_time": new_time,
            "improvement_ratio": ratio,
            "validation": validation,
            "activated_at": utc_now() if status == "active" else None,
        })
        return {"status": status, "reason": reason, "auto_apply": status == "active", "validation": validation}

    def collect_and_schedule(self, alert: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        stats = self.db.collect_statements(self.args.stats_limit)
        active = self.db.collect_active(self.args.active_limit)
        keys = self.store.upsert_observations(stats, active)
        slow = self.store.slow_observations(
            self.args.slow_mean_ms,
            self.args.slow_max_ms,
            self.args.min_calls,
            max(self.args.slow_limit, self.args.stats_limit) if self.args.query_regex else self.args.slow_limit,
        )
        if self.args.query_regex:
            matcher = re.compile(self.args.query_regex, re.IGNORECASE)
            slow = [row for row in slow if matcher.search(str(row.get("query_text", "")))]
            slow = slow[: self.args.slow_limit]
        self._new_incident_if_needed(alert, self.db.database_snapshot(), slow)
        queued: List[str] = []
        if alert or self.args.tune_without_alert:
            for row in slow:
                job_id = self.store.enqueue_job(str(row["sql_key"]), self.current_incident_id, self.args.job_cooldown)
                if job_id:
                    queued.append(job_id)
                    self.futures.append(self.executor.submit(self._run_dream_job, job_id))
        payload = {
            "collected_at": utc_now(),
            "statement_count": len(stats),
            "active_count": len(active),
            "persisted_count": len(keys),
            "slow_count": len(slow),
            "queued_jobs": queued,
            "alert_firing": bool(alert),
            "store": str(self.store.path),
        }
        _write_json(self.output_root / "latest_collection.json", payload)
        LOGGER.info("collected SQL=%d active=%d slow=%d queued=%d alert=%s", len(stats), len(active), len(slow), len(queued), bool(alert))
        return payload

    def run_once(self) -> Dict[str, Any]:
        alert = self._current_alert()
        payload = self.collect_and_schedule(alert)
        if self.args.wait_for_jobs > 0:
            deadline = time.time() + self.args.wait_for_jobs
            while time.time() < deadline:
                pending = [future for future in self.futures if not future.done()]
                if not pending:
                    break
                time.sleep(min(1.0, max(0.05, deadline - time.time())))
        status = self.store.recent(limit=20)
        result = {"status": "completed", "collection": payload, "state": status}
        _write_json(self.output_root / "run_once.json", result)
        return result

    def run(self) -> None:
        LOGGER.info("starting SysInsight/DREAM bridge; state=%s output=%s", self.store.path, self.output_root)
        try:
            while True:
                alert = self._current_alert()
                try:
                    self.collect_and_schedule(alert)
                except Exception:
                    LOGGER.exception("bridge collection cycle failed")
                if self.args.once:
                    break
                time.sleep(max(0.2, self.args.poll_interval))
        except KeyboardInterrupt:
            LOGGER.info("stopping bridge")
        finally:
            self.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.environ.get("SYSINSIGHT_DB", "keeninsight"))
    parser.add_argument("--db-user", default=os.environ.get("SYSINSIGHT_DB_USER", "postgres"))
    parser.add_argument("--run-as", default=os.environ.get("SYSINSIGHT_RUN_AS", "postgres"))
    parser.add_argument("--host", default=os.environ.get("SYSINSIGHT_DB_HOST", "/var/run/postgresql"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SYSINSIGHT_DB_PORT", "5432")))
    parser.add_argument("--db-schema", default=os.environ.get("SYSINSIGHT_DB_SCHEMA", "keeninsight_tpcc"))
    parser.add_argument("--dbms", default=os.environ.get("SYSINSIGHT_DBMS", "postgresql"))
    parser.add_argument("--pg-version", default=os.environ.get("SYSINSIGHT_DB_VERSION", "12"))
    parser.add_argument("--workload", default=os.environ.get("SYSINSIGHT_WORKLOAD", "olap"))
    parser.add_argument("--db-timeout", type=float, default=30.0)
    parser.add_argument("--prometheus-url", default=os.environ.get("SYSINSIGHT_PROMETHEUS_URL", "http://127.0.0.1:9090"))
    parser.add_argument("--prometheus-step", type=float, default=15.0)
    parser.add_argument("--prometheus-timeout", type=float, default=5.0)
    parser.add_argument("--alert-name", default=os.environ.get("SYSINSIGHT_ALERT_NAME", "SysInsightDemoAnomaly"))
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--stats-limit", type=int, default=500)
    parser.add_argument("--active-limit", type=int, default=200)
    parser.add_argument("--slow-mean-ms", type=float, default=float(os.environ.get("SYSINSIGHT_SLOW_MEAN_MS", "1000")))
    parser.add_argument("--slow-max-ms", type=float, default=float(os.environ.get("SYSINSIGHT_SLOW_MAX_MS", "5000")))
    parser.add_argument("--min-calls", type=int, default=1)
    parser.add_argument("--slow-limit", type=int, default=10)
    parser.add_argument("--query-regex", default="", help="optional SQL text filter; empty means every slow statement")
    parser.add_argument("--job-cooldown", type=float, default=300.0)
    parser.add_argument("--dream-workers", type=int, default=1)
    parser.add_argument("--dream-config", default=str(ROOT.parent / "dream" / "config" / "tpcds_local_config.json"))
    parser.add_argument("--dream-python", default="")
    parser.add_argument("--dream-runtime-root", default=str(ROOT.parent / "dream"), help="DREAM checkout/runtime root visible to the worker user")
    parser.add_argument("--dream-run-as", default=os.environ.get("SYSINSIGHT_DREAM_RUN_AS", ""), help="run DREAM as this OS user (needed for peer-authenticated local PostgreSQL)")
    parser.add_argument("--dream-timeout", type=float, default=1800.0)
    parser.add_argument("--dream-root-cause", default="")
    parser.add_argument("--dream-offline", action="store_true")
    parser.add_argument("--dream-offline-hint", default="", help="test-only deterministic hint passed to DREAM_OFFLINE")
    parser.add_argument("--auto-apply", dest="auto_apply", action="store_true", default=True)
    parser.add_argument("--no-auto-apply", dest="auto_apply", action="store_false")
    parser.add_argument("--hint-application-name", default="", help="empty matches every application_name")
    parser.add_argument("--min-improvement", type=float, default=0.10)
    parser.add_argument("--state-db", default="/tmp/sysinsight_dream_bridge.sqlite3")
    parser.add_argument("--output", default="/tmp/sysinsight_dream_bridge")
    parser.add_argument("--api-base", default=_env_first("SYSINSIGHT_GPT_BASE_URL", "SYSINSIGHT_API_BASE", "OPENAI_BASE_URL", default=DEFAULT_API_BASE))
    parser.add_argument("--model", default=_env_first("SYSINSIGHT_GPT_MODEL", "SYSINSIGHT_API_MODEL", "OPENAI_MODEL", default=DEFAULT_MODEL))
    parser.add_argument("--api-timeout", type=float, default=180.0)
    parser.add_argument("--sysinsight-window", type=float, default=300.0)
    parser.add_argument("--sysinsight-case-result", default="", help="optional existing case_result.json for the full source-aware SysInsight pipeline")
    parser.add_argument("--no-api", action="store_true", help="skip the live SysInsight API call")
    parser.add_argument("--configure-hint-table", action="store_true", help="enable pg_hint_plan hint-table loading for new DB connections")
    parser.add_argument("--reset-hint-runtime", action="store_true")
    parser.add_argument("--force-alert", action="store_true", help="treat this cycle as a firing alert for local verification")
    parser.add_argument("--tune-without-alert", action="store_true", help="enqueue slow SQL even when Prometheus has no firing alert")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--wait-for-jobs", type=float, default=0.0, help="seconds to wait after --once for background jobs")
    args = parser.parse_args()
    if args.reset_hint_runtime:
        return args
    if args.port <= 0 or args.poll_interval <= 0 or args.min_calls < 1 or args.slow_limit < 1:
        parser.error("invalid database/poll/slow-query settings")
    if args.min_improvement < 0 or args.min_improvement >= 1:
        parser.error("--min-improvement must be in [0,1)")
    return args


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = parse_args()
    bridge = Bridge(args)
    try:
        if args.reset_hint_runtime:
            result = bridge.reset_hint_runtime()
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.configure_hint_table:
            bridge.configure()
        if args.once:
            result = bridge.run_once()
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        else:
            bridge.run()
        return 0
    except Exception as exc:
        LOGGER.exception("bridge failed")
        return 2
    finally:
        # run() already closes its executor; close() is idempotent for the
        # no-loop CLI paths and ensures no worker is leaked on setup failure.
        try:
            bridge.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
