from __future__ import annotations

import json
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from psycopg import sql
from psycopg.errors import (
    InsufficientPrivilege,
    ObjectNotInPrerequisiteState,
    UndefinedColumn,
    UndefinedFile,
    UndefinedTable,
)

from .db import Database


DATABASE_METRICS_SQL = """
SELECT
    now() AS collected_at,
    datname AS database_name,
    numbackends,
    xact_commit,
    xact_rollback,
    blks_read,
    blks_hit,
    tup_returned,
    tup_fetched,
    tup_inserted,
    tup_updated,
    tup_deleted,
    temp_files,
    temp_bytes,
    deadlocks,
    conflicts,
    stats_reset
FROM pg_stat_database
WHERE datname = current_database()
"""

SERVER_META_SQL = """
SELECT
    current_database() AS database_name,
    current_user AS current_user,
    inet_server_addr()::text AS server_address,
    inet_server_port() AS server_port,
    current_setting('server_version') AS server_version,
    current_setting('server_version_num') AS server_version_num,
    current_setting('track_io_timing', true) AS track_io_timing,
    current_setting('compute_query_id', true) AS compute_query_id,
    pg_postmaster_start_time() AS postmaster_start_time
"""

ACTIVITY_SQL = """
SELECT
    pid,
    usename,
    application_name,
    client_addr::text AS client_addr,
    backend_type,
    state,
    wait_event_type,
    wait_event,
    query_id::text AS query_id,
    query_start,
    xact_start,
    state_change,
    clock_timestamp() - query_start AS query_age,
    left(query, 2000) AS query
FROM pg_stat_activity
WHERE datname = current_database()
  AND pid <> pg_backend_pid()
  AND COALESCE(application_name, '') <> 'keeninsight-api'
ORDER BY query_start NULLS LAST
LIMIT %s
"""

ACTIVITY_FALLBACK_SQL = """
SELECT
    pid,
    usename,
    application_name,
    client_addr::text AS client_addr,
    backend_type,
    state,
    wait_event_type,
    wait_event,
    NULL::text AS query_id,
    query_start,
    xact_start,
    state_change,
    clock_timestamp() - query_start AS query_age,
    left(query, 2000) AS query
FROM pg_stat_activity
WHERE datname = current_database()
  AND pid <> pg_backend_pid()
  AND COALESCE(application_name, '') <> 'keeninsight-api'
ORDER BY query_start NULLS LAST
LIMIT %s
"""

LOCKS_SQL = """
SELECT
    waiting.pid AS waiting_pid,
    waiting.usename AS waiting_user,
    waiting.query AS waiting_query,
    waiting.query_start AS waiting_query_start,
    waiting.wait_event_type,
    waiting.wait_event,
    blocked.locktype,
    blocked.mode AS waiting_mode,
    blocked.relation::regclass::text AS relation,
    blocker.pid AS blocking_pid,
    blocker.usename AS blocking_user,
    blocker.state AS blocking_state,
    blocker.query AS blocking_query,
    blocker.query_start AS blocking_query_start
FROM pg_locks blocked
JOIN pg_stat_activity waiting ON waiting.pid = blocked.pid
LEFT JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS blockers(pid) ON TRUE
LEFT JOIN pg_stat_activity blocker ON blocker.pid = blockers.pid
WHERE NOT blocked.granted
  AND waiting.datname = current_database()
  AND COALESCE(waiting.application_name, '') <> 'keeninsight-api'
ORDER BY waiting.query_start NULLS LAST
"""

SETTINGS_SQL = """
SELECT
    name,
    setting,
    COALESCE(unit, '') AS unit,
    vartype,
    context,
    min_val,
    max_val,
    enumvals,
    source,
    boot_val,
    reset_val,
    pending_restart,
    short_desc,
    extra_desc
FROM pg_settings
WHERE name = ANY(%s)
ORDER BY name
"""

SETTINGS_FALLBACK_SQL = """
SELECT
    name,
    setting,
    COALESCE(unit, '') AS unit,
    vartype,
    context,
    min_val,
    max_val,
    enumvals,
    source,
    boot_val,
    reset_val,
    FALSE AS pending_restart,
    short_desc,
    extra_desc
FROM pg_settings
WHERE name = ANY(%s)
ORDER BY name
"""

BGWRITER_SQL = """
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
    buffers_alloc,
    stats_reset
FROM pg_stat_bgwriter
"""

WAL_SQL = """
SELECT wal_records, wal_fpi, wal_bytes::text AS wal_bytes,
       wal_buffers_full, wal_write, wal_sync, wal_write_time,
       wal_sync_time, stats_reset
FROM pg_stat_wal
"""

TABLES_SQL = """
SELECT
    s.schemaname AS schema_name,
    s.relname AS table_name,
    s.seq_scan,
    s.seq_tup_read,
    s.idx_scan,
    s.n_live_tup,
    s.n_dead_tup,
    s.last_analyze,
    s.last_autoanalyze,
    pg_total_relation_size(format('%I.%I', s.schemaname, s.relname)::regclass) AS total_bytes
FROM pg_stat_user_tables s
ORDER BY pg_total_relation_size(format('%I.%I', s.schemaname, s.relname)::regclass) DESC
LIMIT %s
"""

INDEXES_SQL = """
SELECT
    schemaname AS schema_name,
    relname AS table_name,
    indexrelname AS index_name,
    idx_scan,
    idx_tup_read,
    idx_tup_fetch,
    pg_relation_size(indexrelid) AS bytes
FROM pg_stat_user_indexes
ORDER BY pg_relation_size(indexrelid) DESC
LIMIT %s
"""

PGSS_SQL = """
SELECT
    s.queryid::text AS query_id,
    s.query,
    s.calls,
    s.total_exec_time::double precision AS total_exec_time_ms,
    s.mean_exec_time::double precision AS mean_exec_time_ms,
    s.min_exec_time::double precision AS min_exec_time_ms,
    s.max_exec_time::double precision AS max_exec_time_ms,
    s.rows,
    s.shared_blks_hit,
    s.shared_blks_read,
    s.temp_blks_read,
    s.temp_blks_written,
    s.wal_bytes::text AS wal_bytes,
    s.plans,
    s.total_plan_time::double precision AS total_plan_time_ms
FROM pg_stat_statements s
WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND s.query NOT ILIKE '%%keeninsight.snapshots%%'
  AND s.query NOT ILIKE '%%pg_stat_activity%%'
  AND s.query NOT ILIKE '%%pg_stat_database%%'
  AND s.query NOT ILIKE '%%pg_stat_bgwriter%%'
  AND s.query NOT ILIKE '%%pg_stat_wal%%'
  AND s.query NOT ILIKE '%%pg_settings%%'
  AND s.query NOT ILIKE '%%pg_catalog%%'
  AND s.query NOT ILIKE '%%pg_db_role_setting%%'
  AND s.query NOT ILIKE '%%current_database()%%'
  AND s.query NOT ILIKE '%%pg_postmaster_start_time%%'
  AND s.query NOT ILIKE '%%keeninsight.%%'
ORDER BY s.total_exec_time DESC
LIMIT %s
"""

PGSS_FALLBACK_SQL = """
SELECT
    s.queryid::text AS query_id,
    s.query,
    s.calls,
    s.total_time::double precision AS total_exec_time_ms,
    (s.total_time / NULLIF(s.calls, 0))::double precision AS mean_exec_time_ms,
    NULL::double precision AS min_exec_time_ms,
    NULL::double precision AS max_exec_time_ms,
    s.rows,
    s.shared_blks_hit,
    s.shared_blks_read,
    s.temp_blks_read,
    s.temp_blks_written,
    NULL::text AS wal_bytes,
    NULL::bigint AS plans,
    NULL::double precision AS total_plan_time_ms
FROM pg_stat_statements s
WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND s.query NOT ILIKE '%%keeninsight.snapshots%%'
  AND s.query NOT ILIKE '%%pg_stat_activity%%'
  AND s.query NOT ILIKE '%%pg_stat_database%%'
  AND s.query NOT ILIKE '%%pg_stat_bgwriter%%'
  AND s.query NOT ILIKE '%%pg_stat_wal%%'
  AND s.query NOT ILIKE '%%pg_settings%%'
  AND s.query NOT ILIKE '%%pg_catalog%%'
  AND s.query NOT ILIKE '%%pg_db_role_setting%%'
  AND s.query NOT ILIKE '%%current_database()%%'
  AND s.query NOT ILIKE '%%pg_postmaster_start_time%%'
  AND s.query NOT ILIKE '%%keeninsight.%%'
ORDER BY s.total_time DESC
LIMIT %s
"""

PGSS_COUNT_SQL = """
SELECT count(*)::integer AS query_count
FROM pg_stat_statements s
WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
  AND s.query NOT ILIKE '%%keeninsight.snapshots%%'
  AND s.query NOT ILIKE '%%pg_stat_activity%%'
  AND s.query NOT ILIKE '%%pg_stat_database%%'
  AND s.query NOT ILIKE '%%pg_stat_bgwriter%%'
  AND s.query NOT ILIKE '%%pg_stat_wal%%'
  AND s.query NOT ILIKE '%%pg_settings%%'
  AND s.query NOT ILIKE '%%pg_catalog%%'
  AND s.query NOT ILIKE '%%pg_db_role_setting%%'
  AND s.query NOT ILIKE '%%current_database()%%'
  AND s.query NOT ILIKE '%%pg_postmaster_start_time%%'
  AND s.query NOT ILIKE '%%keeninsight.%%'
"""


def server_meta(db: Database) -> dict[str, Any]:
    return db.fetch_one(SERVER_META_SQL) or {}


def database_metrics(db: Database) -> dict[str, Any]:
    return db.fetch_one(DATABASE_METRICS_SQL) or {}


def activity(db: Database, limit: int = 100) -> list[dict[str, Any]]:
    try:
        return db.fetch_all(ACTIVITY_SQL, (max(1, min(limit, 500)),))
    except UndefinedColumn:
        return db.fetch_all(ACTIVITY_FALLBACK_SQL, (max(1, min(limit, 500)),))


def locks(db: Database) -> list[dict[str, Any]]:
    return db.fetch_all(LOCKS_SQL)


def settings(db: Database, names: Iterable[str]) -> list[dict[str, Any]]:
    try:
        return db.fetch_all(SETTINGS_SQL, (list(names),))
    except UndefinedColumn:
        return db.fetch_all(SETTINGS_FALLBACK_SQL, (list(names),))


def background_writer(db: Database) -> dict[str, Any]:
    try:
        return db.fetch_one(BGWRITER_SQL) or {}
    except (UndefinedColumn, UndefinedTable):
        return {}


def wal_stats(db: Database) -> dict[str, Any]:
    try:
        return db.fetch_one(WAL_SQL) or {}
    except (UndefinedColumn, UndefinedTable):
        return {}


def tables(db: Database, limit: int = 100) -> list[dict[str, Any]]:
    return db.fetch_all(TABLES_SQL, (max(1, min(limit, 500)),))


def indexes(db: Database, limit: int = 100) -> list[dict[str, Any]]:
    return db.fetch_all(INDEXES_SQL, (max(1, min(limit, 500)),))


def top_queries(db: Database, limit: int = 50) -> tuple[list[dict[str, Any]], str | None]:
    bounded_limit = max(1, min(limit, 200))
    try:
        return db.fetch_all(PGSS_SQL, (bounded_limit,)), None
    except (UndefinedTable, UndefinedColumn) as exc:
        try:
            # Older PostgreSQL releases expose total_time instead of
            # total_exec_time.  A successful compatibility query is usable
            # evidence, so it must not be reported as unavailable.
            return db.fetch_all(PGSS_FALLBACK_SQL, (bounded_limit,)), None
        except Exception as fallback_exc:  # pragma: no cover - depends on server setup
            return [], f"pg_stat_statements unavailable: {fallback_exc}"
    except (
        InsufficientPrivilege,
        ObjectNotInPrerequisiteState,
        UndefinedFile,
    ) as exc:  # pragma: no cover - depends on server setup
        return [], f"pg_stat_statements unavailable: {exc}"


def query_stats_count(db: Database) -> int | None:
    """Return the count covered by the filtered query-evidence view."""
    try:
        row = db.fetch_one(PGSS_COUNT_SQL)
        return int(row["query_count"]) if row else None
    except Exception:  # pragma: no cover - depends on server setup
        return None


def relation_columns(
    db: Database, schema_name: str, table_name: str
) -> list[dict[str, Any]]:
    return db.fetch_all(
        """
        SELECT a.attname AS column_name, format_type(a.atttypid, a.atttypmod) AS data_type,
               a.attnum AS ordinal_position
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relname = %s
          AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        (schema_name, table_name),
    )


def relation_exists(db: Database, schema_name: str, table_name: str) -> bool:
    row = db.fetch_one(
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s
              AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
        ) AS exists
        """,
        (schema_name, table_name),
    )
    return bool(row and row["exists"])


def relation_kind(db: Database, schema_name: str, table_name: str) -> str | None:
    row = db.fetch_one(
        """
        SELECT c.relkind
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relname = %s
        """,
        (schema_name, table_name),
    )
    return row.get("relkind") if row else None


def index_exists(db: Database, schema_name: str, index_name: str) -> bool:
    row = db.fetch_one(
        """
        SELECT EXISTS (
            SELECT 1 FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s AND c.relkind = 'i'
        ) AS exists
        """,
        (schema_name, index_name),
    )
    return bool(row and row["exists"])


def current_settings_map(db: Database) -> dict[str, dict[str, Any]]:
    wanted = [
        "max_connections",
        "work_mem",
        "shared_buffers",
        "effective_cache_size",
        "random_page_cost",
        "max_parallel_workers_per_gather",
        "maintenance_work_mem",
        "hash_mem_multiplier",
        "track_io_timing",
        "compute_query_id",
    ]
    return {row["name"]: row for row in settings(db, wanted)}


def jsonable(value: Any) -> Any:
    """Convert psycopg values into JSON-compatible values for API/storage."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, (date, datetime_time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", "replace")
    if isinstance(value, Decimal):
        # Keep counters exact when possible, while making them JSON-safe.
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (set, frozenset)):
        return [jsonable(v) for v in sorted(value, key=str)]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def jsonable_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [jsonable(row) for row in rows]


def safe_identifier(name: str) -> bool:
    import re

    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))


def quoted_identifier(name: str) -> sql.Identifier:
    if not safe_identifier(name):
        raise ValueError(f"unsafe PostgreSQL identifier: {name!r}")
    return sql.Identifier(name)
