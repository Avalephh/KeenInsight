#!/usr/bin/env python3
"""Compact a stopped SysInsight/DREAM SQLite state database once.

The bridge must be stopped before ``--replace`` is used.  The replacement is
created and integrity-checked in the same directory before an atomic rename;
the old sparse/freelist-heavy file is not retained so the operation actually
returns disk space to the host.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Dict


def database_stats(path: Path) -> Dict[str, Any]:
    with sqlite3.connect(str(path), timeout=30.0) as conn:
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        free_pages = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
        return {
            "path": str(path),
            "file_bytes": path.stat().st_size,
            "page_size": page_size,
            "page_count": page_count,
            "free_pages": free_pages,
            "free_bytes": free_pages * page_size,
            "auto_vacuum": int(conn.execute("PRAGMA auto_vacuum").fetchone()[0]),
            "integrity_check": str(conn.execute("PRAGMA integrity_check").fetchone()[0]),
        }


def compact(path: Path) -> Dict[str, Any]:
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    source_stat = path.stat()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".{}-compact-".format(path.name),
        suffix=".sqlite3",
        dir=str(path.parent),
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink()
    before = database_stats(path)
    try:
        conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            checkpoint = list(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
            # Fail fast if another bridge still holds a write transaction.
            conn.execute("BEGIN EXCLUSIVE")
            conn.execute("ROLLBACK")
            conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
            conn.execute("VACUUM INTO ?", (str(temporary),))
        finally:
            conn.close()

        with sqlite3.connect(str(temporary), timeout=30.0, isolation_level=None) as target:
            if int(target.execute("PRAGMA auto_vacuum").fetchone()[0]) != 2:
                target.execute("PRAGMA auto_vacuum = INCREMENTAL")
                target.execute("VACUUM")
            integrity = str(target.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity.lower() != "ok":
                raise RuntimeError("compacted database integrity check failed: {}".format(integrity))

        os.chmod(str(temporary), source_stat.st_mode & 0o7777)
        try:
            os.chown(str(temporary), source_stat.st_uid, source_stat.st_gid)
        except PermissionError:
            pass
        os.replace(str(temporary), str(path))
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        after = database_stats(path)
        return {
            "status": "completed",
            "checkpoint": checkpoint,
            "before": before,
            "after": after,
            "reclaimed_bytes": max(0, int(before["file_bytes"]) - int(after["file_bytes"])),
        }
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-db", required=True, help="bridge SQLite state database")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace the stopped database after creating and checking a compact copy",
    )
    args = parser.parse_args()
    path = Path(args.state_db).resolve()
    if not args.replace:
        print(json.dumps({"status": "check_only", "database": database_stats(path)}, indent=2))
        return 0
    print(json.dumps(compact(path), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
