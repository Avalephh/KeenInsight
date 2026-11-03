"""Separate probe and reporting overhead using real, persistent libpq calls.

All PostgreSQL work runs in the lab's disposable cluster. No SQL tracing
extension, LLM, presentation service or production configuration is involved.
"""
import argparse
import ctypes as ct
import ctypes.util
import json
import math
from pathlib import Path
import random
import re
import statistics
import time

from .collector import atomic_json
from .lab import Capture, cluster, cpu_seconds, read_events

# Each successive control changes one factor. "events" changes probe scope
# intentionally; it is not asserted to preserve the full profile's signals.
MODES = {
    "baseline": None,
    "full": ("selected", ["--detail-level", "full"]),
    "aggregate": ("selected", ["--detail-level", "full", "--query-output", "aggregate"]),
    "counts": ("selected", ["--detail-level", "full", "--query-output", "aggregate",
                            "--metric-mode", "counts"]),
    "no_sched": ("selected", ["--detail-level", "lightweight"]),
    "events": ("events", ["--detail-level", "lightweight"]),
    "boundary": ("boundary", ["--detail-level", "lightweight"]),
    "system": ("events", ["--detail-level", "lightweight", "--scope", "system"]),
}
WORKLOADS = {
    "short": "SELECT 1",
    "cpu": "SELECT sum(g) FROM generate_series(1,20000) AS g",
    "spill": "SELECT count(*) FROM (SELECT * FROM spill_fixture ORDER BY payload) AS s",
    "wal": "INSERT INTO wal_fixture VALUES (1)",
}


class LibpqSession:
    """Use installed libpq; no extra Python driver or per-query psql output."""
    def __init__(self, connection, database="postgres", user="postgres"):
        library = ctypes.util.find_library("pq")
        if not library:
            raise RuntimeError("libpq is required for the latency/throughput experiment")
        self.lib = ct.CDLL(library)
        self.lib.PQconnectdb.argtypes, self.lib.PQconnectdb.restype = [ct.c_char_p], ct.c_void_p
        self.lib.PQstatus.argtypes, self.lib.PQstatus.restype = [ct.c_void_p], ct.c_int
        self.lib.PQerrorMessage.argtypes, self.lib.PQerrorMessage.restype = [ct.c_void_p], ct.c_char_p
        self.lib.PQexec.argtypes, self.lib.PQexec.restype = [ct.c_void_p, ct.c_char_p], ct.c_void_p
        self.lib.PQresultStatus.argtypes, self.lib.PQresultStatus.restype = [ct.c_void_p], ct.c_int
        self.lib.PQgetvalue.argtypes = [ct.c_void_p, ct.c_int, ct.c_int]
        self.lib.PQgetvalue.restype = ct.c_char_p
        self.lib.PQclear.argtypes, self.lib.PQfinish.argtypes = [ct.c_void_p], [ct.c_void_p]
        # Values originate in the locally generated cluster path, not from SQL.
        host = connection[connection.index("-h") + 1]
        port = connection[connection.index("-p") + 1]
        if "'" in host or "\\" in host:
            raise ValueError("unsupported temporary socket path")
        if any(re.search(r"[^A-Za-z0-9_.-]", value) for value in (database, user)):
            raise ValueError("unsafe database/user name")
        dsn = "host='{}' port={} user={} dbname={} application_name=ebpf-overhead".format(
            host, port, user, database)
        self.handle = self.lib.PQconnectdb(dsn.encode())
        if not self.handle or self.lib.PQstatus(self.handle):
            error = self.lib.PQerrorMessage(self.handle).decode() if self.handle else "NULL connection"
            self.close()
            raise RuntimeError(error)
        self.pid = int(self.execute("SELECT pg_backend_pid()", value=True))

    def execute(self, sql, value=False):
        result = self.lib.PQexec(self.handle, sql.encode() if isinstance(sql, str) else sql)
        if not result:
            raise RuntimeError("PQexec returned NULL")
        try:
            if self.lib.PQresultStatus(result) not in (1, 2):
                raise RuntimeError(self.lib.PQerrorMessage(self.handle).decode())
            return self.lib.PQgetvalue(result, 0, 0).decode() if value else None
        finally:
            self.lib.PQclear(result)

    def close(self):
        if self.handle:
            self.lib.PQfinish(self.handle)
            self.handle = None


def percentile(samples, fraction):
    ordered = sorted(samples)
    if not ordered:
        raise ValueError("empty latency sample")
    return ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))]


def summarize(trials):
    result = {}
    for row in trials:
        baseline = next(t for t in trials if t["mode"] == "baseline"
                        and t["workload"] == row["workload"] and t["repeat"] == row["repeat"])
        row["paired_qps_loss_percent"] = 100 * (1 - row["qps"] / baseline["qps"])
        row["paired_mean_latency_increase_percent"] = 100 * (
            row["mean_latency_ns"] / baseline["mean_latency_ns"] - 1)
        row["paired_p99_latency_increase_percent"] = 100 * (
            row["p99_latency_ns"] / baseline["p99_latency_ns"] - 1)
    for workload in sorted({r["workload"] for r in trials}):
        result[workload] = {}
        for mode in dict.fromkeys(row["mode"] for row in trials):
            rows = [r for r in trials if r["workload"] == workload and r["mode"] == mode]
            if not rows:
                continue
            losses = [r["paired_qps_loss_percent"] for r in rows]
            result[workload][mode] = {
                "repeats": len(rows), "median_qps": statistics.median(r["qps"] for r in rows),
                "median_paired_qps_loss_percent": statistics.median(losses),
                "range_paired_qps_loss_percent": [min(losses), max(losses)],
                "median_paired_mean_latency_increase_percent": statistics.median(
                    r["paired_mean_latency_increase_percent"] for r in rows),
                "median_paired_p99_latency_increase_percent": statistics.median(
                    r["paired_p99_latency_increase_percent"] for r in rows),
                "median_collector_cpu_percent": statistics.median(
                    r["collector_cpu_percent"] for r in rows),
                "median_output_bytes_per_query": statistics.median(
                    r.get("output_bytes", 0) / (r["queries"] + r["warmup_queries"]) for r in rows),
                "all_captures_valid": all(r.get("capture_valid", True) for r in rows)}
    return result


def rss_kb(pid):
    for line in (Path("/proc") / str(pid) / "status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return None


def run(args):
    if not math.isfinite(args.seconds) or not .2 <= args.seconds <= 10:
        raise ValueError("use 0.2..10 seconds per trial")
    if not 3 <= args.repeats <= 10:
        raise ValueError("use 3..10 independent trial repeats")
    workloads, modes = args.workloads.split(","), args.modes.split(",")
    if (set(workloads) - set(WORKLOADS) or set(modes) - set(MODES)
            or "baseline" not in modes or len(modes) != len(set(modes))
            or len(workloads) != len(set(workloads))):
        raise ValueError("unique known workloads/modes and a baseline are required")
    directory = Path(args.output).resolve()
    directory.mkdir(parents=True, mode=0o700, exist_ok=False)
    trials = []
    report = {"schema": "keeninsight.ebpf-overhead.v1", "status": "running",
              "arguments": vars(args), "trial_order": [],
              "workloads": {w: WORKLOADS[w] for w in workloads},
              "scope": "single client closed-loop libpq over local Unix socket; QPS is not TPS",
              "environment": "disposable PG12 cluster on a shared host; existing services untouched",
              "caveats": "CPU times are /proc tick-resolution; p99 is client round-trip latency; "
                         "no confidence claim, no multi-client or open-loop coordinated-omission correction; "
                         "counts/events intentionally omit some detailed signals"}
    rng = random.Random(args.seed)
    with cluster(args.pg_config) as (server, connection):
        report["server"] = server
        session = LibpqSession(connection)
        try:
            session.execute("CREATE TEMP TABLE spill_fixture AS SELECT g, repeat(md5(g::text),4) AS payload "
                            "FROM generate_series(1,8000) AS g")
            session.execute("CREATE TABLE wal_fixture(value int)")
            for repeat in range(args.repeats):
                order = [(workload, mode) for workload in workloads for mode in modes]
                rng.shuffle(order)
                for workload, mode in order:
                    identity = "{}-{}-{}".format(repeat, workload, mode)
                    report["trial_order"].append(identity)
                    capture = None
                    location = directory / identity
                    try:
                        # Avoid unintentionally turning the nominal CPU
                        # workload into generate_series materialization spill.
                        work_mem = "64kB" if workload == "spill" else "16MB"
                        session.execute("SET work_mem='{}'".format(work_mem))
                        if MODES[mode] is not None:
                            profile, options = MODES[mode]
                            capture = Capture(args.pg_config, session.pid, location, profile, extra=options)
                        query = WORKLOADS[workload].encode()
                        warmup = 5
                        for _ in range(warmup):
                            session.execute(query)
                        backend_cpu = cpu_seconds(session.pid)
                        collector_cpu = cpu_seconds(capture.process.pid) if capture else 0
                        latencies = []
                        start = time.perf_counter_ns()
                        deadline = start + int(args.seconds * 1e9)
                        while True:
                            began = time.perf_counter_ns()
                            session.execute(query)
                            ended = time.perf_counter_ns()
                            latencies.append(ended - began)
                            if ended >= deadline or len(latencies) >= 500000:
                                break
                        elapsed = (time.perf_counter_ns() - start) / 1e9
                        backend_delta = cpu_seconds(session.pid) - backend_cpu
                        collector_delta = cpu_seconds(capture.process.pid) - collector_cpu if capture else 0
                        row = {"trial_id": identity, "mode": mode, "workload": workload,
                               "work_mem": work_mem,
                               "repeat": repeat, "queries": len(latencies), "warmup_queries": warmup,
                               "wall_seconds": elapsed, "qps": len(latencies) / elapsed,
                               "mean_latency_ns": statistics.mean(latencies),
                               "p50_latency_ns": percentile(latencies, .50),
                               "p95_latency_ns": percentile(latencies, .95),
                               "p99_latency_ns": percentile(latencies, .99),
                               "backend_cpu_seconds": backend_delta,
                               "backend_cpu_ms_per_1000_queries": 1e6 * backend_delta / len(latencies),
                               "collector_cpu_seconds": collector_delta,
                               "collector_cpu_percent": 100 * collector_delta / elapsed,
                               "collector_rss_kb": rss_kb(capture.process.pid) if capture else 0}
                        if capture:
                            capture.close()
                            capture = None
                            manifest = json.loads((location / "manifest.json").read_text())
                            totals = [e for e in read_events(location) if e["type"] == "query_aggregate_final"]
                            observed = sum(e["completed_spans"] for e in totals)
                            row.update(output_bytes=manifest["output_bytes"],
                                       output_records=manifest["output_records"],
                                       collection=manifest["collection"],
                                       attached_probes=manifest["attached_probes"],
                                       observed_completed_spans=observed,
                                       message_stats=manifest["message_stats"],
                                       perf_lost_notifications=manifest["perf_lost_notifications"],
                                       bpf_health=manifest["bpf_health"],
                                       source_sha256=manifest["source_sha256"],
                                       pruned_probes=manifest.get("pruned_at_ns", {}),
                                       capture_valid=manifest["status"] == "completed"
                                       and not manifest["perf_lost_notifications"]
                                       and not any(manifest["bpf_health"].values())
                                       and not manifest.get("pruned_at_ns")
                                       and (observed == len(latencies) + warmup
                                            if manifest["collection"]["scope"] == "query"
                                            else observed == 0))
                        trials.append(row)
                        report["trials"] = trials
                        atomic_json(directory / "overhead.json", report)
                        print(identity, "qps={:.1f}".format(row["qps"]), flush=True)
                    finally:
                        if capture:
                            capture.close(check=False)
            report.update(status="completed", summary=summarize(trials))
            atomic_json(directory / "overhead.json", report)
            print(json.dumps(report["summary"], indent=2))
        finally:
            session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pg-config", default="/usr/lib/postgresql/12/bin/pg_config")
    parser.add_argument("--output", required=True)
    parser.add_argument("--seconds", type=float, default=1.5)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--workloads", default="short,cpu,spill")
    parser.add_argument("--modes", default="baseline,full,aggregate,counts,no_sched,events,boundary,system")
    parser.add_argument("--seed", type=int, default=20261103)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
