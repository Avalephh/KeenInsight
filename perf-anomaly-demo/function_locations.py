#!/usr/bin/env python3
"""Index PostgreSQL function definitions for Function Analysis display.

The live profiler reports function names but not their source locations.  The
pinned PostgreSQL source tree is already retained for source evidence, so this
module builds a small name -> file:line index from it.  The index is a display
aid, not a call graph or proof that a function is on the hot path.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
from typing import Dict, Iterable, Optional

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "results" / "function_locations.json"
SCHEMA = 1

DEFINITION = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(")
SKIP_NAMES = {
    "if", "for", "while", "switch", "return", "sizeof", "else", "do", "case",
    "defined", "assert", "ereport", "elog", "offsetof", "likely", "unlikely",
    "PG_FUNCTION_INFO_V1", "PG_MODULE_MAGIC", "PGDLLEXPORT",
}


def source_root(configured: str = "") -> Optional[Path]:
    candidates = [Path(configured)] if configured else []
    candidates.extend([
        Path(os.environ.get("POSTGRES_SOURCE_ROOT", "")).expanduser()
        if os.environ.get("POSTGRES_SOURCE_ROOT") else None,
        Path("/root/keeninsight-postgres/third_party/postgresql-12.22"),
        ROOT.parent / "third_party" / "postgresql-12.22",
    ])
    for candidate in candidates:
        if candidate and (candidate / "src" / "backend").is_dir():
            return candidate.resolve()
    return None


def _priority(relative: str) -> int:
    if relative.startswith("src/backend/"):
        return 0
    if relative.startswith("src/common/"):
        return 1
    if relative.startswith("src/port/") or relative.startswith("src/include/"):
        return 2
    if relative.startswith("contrib/"):
        return 3
    return 4


def _is_definition(lines: list[str], index: int, name: str) -> bool:
    line = lines[index]
    if line.rstrip().endswith(";"):
        return False
    if "{" in line:
        return True
    # PostgreSQL puts the opening brace on its own line, but multi-line
    # argument lists can push it several lines down.
    for cursor in range(index + 1, min(len(lines), index + 40)):
        stripped = lines[cursor].strip()
        if not stripped:
            continue
        if ";" in stripped:
            return False
        if "{" in stripped:
            return True
    return False


def build_index(root: Path) -> Dict[str, Dict[str, object]]:
    index: Dict[str, Dict[str, object]] = {}
    paths = sorted(list(root.rglob("*.c")) + list(root.rglob("*.h")))
    for path in paths:
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if "/test/" in relative or relative.startswith("src/test/"):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        priority = _priority(relative)
        for number, line in enumerate(lines, 1):
            match = DEFINITION.match(line)
            if not match:
                continue
            name = match.group(1)
            if name in SKIP_NAMES or name.startswith("#"):
                continue
            if not _is_definition(lines, number - 1, name):
                continue
            previous = index.get(name)
            if previous and int(previous.get("priority", 99)) <= priority:
                continue
            index[name] = {"file": relative, "line": number, "priority": priority}
    for value in index.values():
        value.pop("priority", None)
    return dict(sorted(index.items()))


def load_index(root: Optional[Path] = None, rebuild: bool = False) -> Dict[str, Dict[str, object]]:
    root = root or source_root()
    if root is None:
        return {}
    if not rebuild and CACHE.is_file():
        try:
            payload = json.loads(CACHE.read_text(encoding="utf-8"))
            if payload.get("schemaVersion") == SCHEMA and payload.get("sourceRoot") == str(root):
                functions = payload.get("functions")
                if isinstance(functions, dict):
                    return functions
        except (OSError, ValueError):
            pass
    functions = build_index(root)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({
        "schemaVersion": SCHEMA,
        "sourceRoot": str(root),
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "functionCount": len(functions),
        "functions": functions,
    }, ensure_ascii=False), encoding="utf-8")
    return functions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", default="")
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()
    root = source_root(args.source_root)
    if root is None:
        print("PostgreSQL source tree not found", file=sys.stderr)
        return 2
    functions = load_index(root, rebuild=args.rebuild or True)
    print("source", root)
    print("functions", len(functions))
    for name in ("ExecComputeStoredGenerated", "pg_analyze_and_rewrite", "PortalRun"):
        print(name, functions.get(name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
