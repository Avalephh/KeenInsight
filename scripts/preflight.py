#!/usr/bin/env python3
"""Fail-fast environment checks for a reproducible KeenInsight run."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row


DEFAULT_DATABASE_URL = (
    "postgresql://keeninsight:keeninsight@localhost:5432/keeninsight"
)


def api_request(base_url: str, path: str) -> dict[str, Any]:
    request = urllib.request.Request(base_url.rstrip("/") + path)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"{path} -> HTTP {exc.code}: {detail}") from exc
    if not isinstance(body, dict):
        raise RuntimeError(f"{path} did not return a JSON object")
    return body


def direct_checks(database_url: str) -> dict[str, Any]:
    with psycopg.connect(database_url, row_factory=dict_row, autocommit=True) as conn:
        meta = conn.execute(
            """
            SELECT current_database() AS database_name,
                   current_user AS current_user,
                   current_setting('server_version') AS server_version,
                   current_setting('server_version_num') AS server_version_num
            """
        ).fetchone() or {}
        # PostgreSQL intentionally restricts the shared_preload_libraries GUC
        # to superusers (or pg_read_all_settings).  The demo role should not
        # need either privilege, so treat an unreadable value as an expected
        # deployment condition and verify the extension through its view.
        try:
            preload = conn.execute(
                "SELECT current_setting('shared_preload_libraries', true) AS value"
            ).fetchone() or {}
            meta["shared_preload_libraries"] = preload.get("value")
            meta["shared_preload_libraries_readable"] = True
        except psycopg.errors.InsufficientPrivilege:
            meta["shared_preload_libraries"] = None
            meta["shared_preload_libraries_readable"] = False

        extension = conn.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'
            ) AS installed,
            (SELECT extversion FROM pg_extension
             WHERE extname = 'pg_stat_statements') AS version
            """
        ).fetchone() or {}
        stat_view_queryable = False
        try:
            conn.execute("SELECT 1 FROM pg_stat_statements LIMIT 1").fetchone()
            stat_view_queryable = True
        except psycopg.errors.InsufficientPrivilege:
            # Keep the failure reason in the report; the caller below turns
            # this into a concise actionable preflight error.
            stat_view_queryable = False
        except psycopg.errors.ObjectNotInPrerequisiteState:
            # The extension exists but PostgreSQL was not started with the
            # required preload library.
            stat_view_queryable = False
        relations = {}
        for table in ("customers", "orders", "accounts", "stats_probe"):
            row = conn.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'keeninsight_demo' AND table_name = %s
                ) AS present
                """,
                (table,),
            ).fetchone() or {}
            relations[f"keeninsight_demo.{table}"] = bool(row["present"])
        counts = {}
        for table in ("customers", "orders", "accounts", "stats_probe"):
            if relations[f"keeninsight_demo.{table}"]:
                row = conn.execute(
                    f"SELECT count(*)::bigint AS row_count FROM keeninsight_demo.{table}"
                ).fetchone() or {}
                counts[f"keeninsight_demo.{table}"] = int(row["row_count"])
        settings = conn.execute(
            """
            SELECT name, setting, unit, source
            FROM pg_settings
            WHERE name IN ('work_mem', 'max_parallel_workers_per_gather')
            ORDER BY name
            """
        ).fetchall()
        locks = conn.execute(
            """
            SELECT count(*)::int AS waiting
            FROM pg_locks blocked
            JOIN pg_stat_activity waiting ON waiting.pid = blocked.pid
            WHERE NOT blocked.granted AND waiting.datname = current_database()
            """
        ).fetchone() or {}
    return {
        "server": dict(meta),
        "pg_stat_statements_extension": bool(extension.get("installed")),
        "pg_stat_statements_version": extension.get("version"),
        "pg_stat_statements_queryable": stat_view_queryable,
        "relations": relations,
        "row_counts": counts,
        "settings": [dict(row) for row in settings],
        "waiting_locks": int(locks.get("waiting") or 0),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--database-url",
        default=os.getenv("KI_DATABASE_URL", DEFAULT_DATABASE_URL),
    )
    parser.add_argument("--min-major", type=int, default=12)
    parser.add_argument("--expected-major", type=int, default=None)
    parser.add_argument("--min-customers", type=int, default=20_000)
    parser.add_argument("--min-orders", type=int, default=800_000)
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    health = api_request(args.base_url, "/api/health")
    if not health.get("ok"):
        raise SystemExit(f"API/PostgreSQL health check failed: {health}")
    meta = direct_checks(args.database_url)
    version_num = int(meta["server"]["server_version_num"])
    major = version_num // 10_000 if version_num >= 100_000 else version_num // 10
    if major < args.min_major:
        raise SystemExit(f"PostgreSQL {major} is below required major {args.min_major}")
    if args.expected_major is not None and major != args.expected_major:
        raise SystemExit(
            f"PostgreSQL major {major} does not match expected {args.expected_major}"
        )
    if not meta["pg_stat_statements_extension"]:
        raise SystemExit("pg_stat_statements extension is not installed")
    preload = meta["server"].get("shared_preload_libraries") or ""
    if meta["server"].get("shared_preload_libraries_readable") and (
        "pg_stat_statements" not in preload
    ):
        raise SystemExit("pg_stat_statements is not present in shared_preload_libraries")
    if not meta["pg_stat_statements_queryable"]:
        raise SystemExit(
            "pg_stat_statements is not queryable; check shared_preload_libraries "
            "and the demo role's pg_read_all_stats access"
        )
    missing = [name for name, present in meta["relations"].items() if not present]
    if missing:
        raise SystemExit(f"demo relations are missing: {missing}")
    if meta["row_counts"].get("keeninsight_demo.customers", 0) < args.min_customers:
        raise SystemExit("customer fixture is smaller than the requested minimum")
    if meta["row_counts"].get("keeninsight_demo.orders", 0) < args.min_orders:
        raise SystemExit("order fixture is smaller than the requested minimum")

    workloads = api_request(args.base_url, "/api/workloads")
    locks = api_request(args.base_url, "/api/locks")
    applied = api_request(args.base_url, "/api/actions?status=applied&limit=500")
    rag = api_request(args.base_url, "/api/rag/status")
    manifest = api_request(args.base_url, "/api/demo/manifest")
    export = api_request(args.base_url, "/api/export?limit=1")
    if args.require_clean:
        state = workloads.get("state") or {}
        if meta["waiting_locks"] or locks.get("items"):
            raise SystemExit("clean preflight failed: waiting locks remain")
        if applied.get("items"):
            raise SystemExit("clean preflight failed: applied actions remain")
        if state.get("missing_index_exists"):
            raise SystemExit("clean preflight failed: demo index remains")

    static_manifest = (manifest.get("rag") or {}).get("static_manifest") or {}
    # Do not copy the manifest's self-sensitive digest into the preflight
    # artifact: collecting that artifact is itself part of the static corpus
    # and would otherwise make the recorded digest stale after recollection.
    manifest_summary = {
        key: static_manifest.get(key)
        for key in ("available", "path", "format", "record_count", "source_type_counts")
    }
    report = {
        "status": "ready",
        "api": {
            "base_url": args.base_url,
            "collector": health.get("collector"),
            "rag_indexer": health.get("rag"),
        },
        "database": meta,
        "demo_state": workloads.get("state"),
        "waiting_locks_api": len(locks.get("items") or []),
        "applied_actions": len(applied.get("items") or []),
        "rag": {
            "documents": (rag.get("database") or {}).get("documents"),
            "chunks": (rag.get("database") or {}).get("chunks"),
        },
        "manifest": {
            "format": manifest.get("format"),
            "static_sources": manifest_summary,
            "evaluation": ((manifest.get("rag") or {}).get("fixed_evaluation") or {}).get("available"),
        },
        "export_format": export.get("format"),
    }
    rendered = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, psycopg.Error) as exc:
        print(f"preflight failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
