#!/usr/bin/env python3
"""Explicit TP fixture creation and TPC-DS generation/loading for NEW schemas.

Defaults to plan-only. Never creates/drops a database, overwrites a schema or
resets pg_stat_statements. PostgreSQL connection uses ordinary PG* environment.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
GIB = 1024 ** 3


def sha(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def psql(database):
    # Disallow libpq conninfo/URI so confirmation cannot name a different DB.
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,62}", database):
        raise ValueError("Use a plain database name, not a URI/connection string")
    return ["psql", "-X", "--no-password", "-v", "ON_ERROR_STOP=1", "-d", database]


def check_database(args, schema):
    if not args.apply:
        return
    if args.confirm_database != args.database:
        raise ValueError("--confirm-database must exactly match --database")
    query = "SELECT count(*) FROM pg_namespace WHERE nspname='" + schema + "'"
    exists = subprocess.check_output([*psql(args.database), "-At", "-c", query], text=True).strip()
    if exists != "0":
        raise ValueError("Refusing existing schema " + schema + "; use a NEW dedicated database")


def tp(args):
    if not 5 <= args.warehouses <= 50 or not 1 <= args.seed <= 1000000:
        raise ValueError("Use 5..50 warehouses and seed 1..1000000; workloads reference warehouses 1..5")
    psql(args.database)
    print("TP plan:", args.database, args.warehouses, "warehouses; schema keeninsight_tpcc")
    print("Deterministic synthetic fixture, 10 lines/order; NOT a certified TPC-C generator.")
    check_database(args, "keeninsight_tpcc")
    if args.apply:
        subprocess.run([*psql(args.database), "--single-transaction",
                        "-v", "warehouses=" + str(args.warehouses),
                        "-v", "seed=" + str(args.seed),
                        "-f", str(ROOT / "scripts/tpcc_schema.sql")], check=True)


def generate_ap(args):
    tools = Path(args.tools).resolve()
    executable = tools / "dsdgen"
    output = Path(args.output).absolute()
    if not 1 <= args.scale <= 1000 or not 1 <= args.seed <= 1000000:
        raise ValueError("Scale must be 1..1000; seed 1..1000000")
    if not executable.is_file() or not (tools / "tpcds.idx").is_file():
        raise ValueError("Build pinned TPC-DS tools first; dsdgen/tpcds.idx are required")
    if output.exists():
        raise ValueError("Refusing any existing output directory")
    command = [str(executable), "-SCALE", str(args.scale), "-RNGSEED", str(args.seed),
               "-DIR", str(output), "-TERMINATE", "Y"]
    print("AP generation plan:", " ".join(command))
    print("Single generator process. Reserve space for .dat, DB, indexes, WAL and temporary files.")
    if not args.apply:
        return
    parent = output.parent
    if not parent.is_dir():
        raise ValueError("Output parent must already exist")
    required = args.min_free_gib if args.min_free_gib is not None else max(4, args.scale * 3)
    if shutil.disk_usage(parent).free < required * GIB:
        raise ValueError("Insufficient free space; requested headroom " + str(required) + " GiB")
    output.mkdir()
    # No FORCE flag: never overwrite existing generated data.
    subprocess.run(command, cwd=tools, check=True)
    metadata = {
        "schema": "keeninsight.tpcds-generation.v1", "scale": args.scale, "seed": args.seed,
        "generatorSha256": sha(executable), "ddlSha256": sha(tools / "tpcds.sql"),
        "command": command,
        "files": [{"path": p.name, "bytes": p.stat().st_size, "sha256": sha(p)}
                  for p in sorted(output.glob("*.dat"))],
    }
    (output / "generation.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print("Generated and hashed data:", len(metadata["files"]), "files")


def table_names(ddl):
    tables = re.findall(r"(?im)^\s*create\s+table\s+([a-z][a-z_0-9]*)\s*\(", ddl)
    if not tables or len(tables) != len(set(tables)):
        raise ValueError("Unexpected TPC-DS schema")
    return tables


def dat_record(line):
    # dsdgen adds one trailing separator. Do NOT strip other empty columns.
    if not line.endswith(b"|\n"):
        raise ValueError("Unexpected dsdgen record terminator (expected pipe + LF)")
    return line[:-2] + b"\n"


def load_ap(args):
    psql(args.database)
    tools, data = Path(args.tools).resolve(), Path(args.data_dir).resolve()
    ddl_path = tools / "tpcds.sql"
    ddl = ddl_path.read_text()
    tables = table_names(ddl)
    required = [t for t in tables if t != "dbgen_version"]
    metadata_path = data / "generation.json"
    if not metadata_path.is_file():
        raise ValueError("Missing generation.json; use ap-generate so input integrity is traceable")
    metadata = json.loads(metadata_path.read_text())
    if (metadata.get("schema") != "keeninsight.tpcds-generation.v1"
            or metadata.get("ddlSha256") != sha(ddl_path)):
        raise ValueError("Generation provenance/DDL mismatch")
    entries = {e["path"]: e for e in metadata["files"]}
    if len(entries) != len(metadata["files"]):
        raise ValueError("Duplicate generation entries")
    for table in required:
        name = table + ".dat"
        entry, path = entries.get(name, {}), data / name
        if (path.is_symlink() or not path.is_file() or path.stat().st_size != entry.get("bytes")
                or sha(path) != entry.get("sha256")):
            raise ValueError("Missing/corrupt TPC-DS input: " + name)
    print("AP load plan:", len(required), "tables into", args.database, "schema tpcds")
    print("Atomic single-connection COPY; no PG parameter changes, services or old data touched.")
    check_database(args, "tpcds")
    if not args.apply:
        return
    # psql consumes COPY data from this process; input paths need not be postgres-readable.
    process = subprocess.Popen(psql(args.database), stdin=subprocess.PIPE)
    try:
        process.stdin.write(b"BEGIN;\nSELECT pg_advisory_xact_lock(184201,2);\n"
                            b"CREATE SCHEMA tpcds;\nSET LOCAL search_path=tpcds,pg_catalog;\n")
        process.stdin.write(ddl.encode("utf-8") + b"\n")
        for table in required:
            command = ("COPY tpcds." + table +
                       " FROM STDIN WITH (FORMAT csv, DELIMITER '|', NULL '', QUOTE E'\\b');\n")
            process.stdin.write(command.encode("ascii"))
            with (data / (table + ".dat")).open("rb") as stream:
                for line in stream:
                    process.stdin.write(dat_record(line))
            process.stdin.write(b"\\.\n")
        process.stdin.write(b"ANALYZE;\nCOMMIT;\n")
        process.stdin.close()
        if process.wait() != 0:
            raise ValueError("TPC-DS import failed; inspect server logs; uncommitted work rolls back")
    except BaseException:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=30)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, handler in (("tp", tp), ("ap-generate", generate_ap), ("ap-load", load_ap)):
        command = commands.add_parser(name)
        command.add_argument("--apply", action="store_true", help="Execute; without this only validate/print a plan")
        command.set_defaults(handler=handler)
        if name in {"tp", "ap-load"}:
            command.add_argument("--database", required=True)
            command.add_argument("--confirm-database")
        if name == "tp":
            command.add_argument("--warehouses", type=int, default=5)
            command.add_argument("--seed", type=int, default=42)
        else:
            command.add_argument("--tools", default=str(ROOT / "third_party/tpcds-kit/tools"))
        if name == "ap-generate":
            command.add_argument("--output", required=True)
            command.add_argument("--scale", type=int, default=10)
            command.add_argument("--seed", type=int, default=42)
            command.add_argument("--min-free-gib", type=int)
        if name == "ap-load":
            command.add_argument("--data-dir", required=True)
    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print("Workload preparation failed:", str(exc), file=sys.stderr)
        raise SystemExit(1)
