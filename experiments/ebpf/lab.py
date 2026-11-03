"""Opt-in real-kernel smoke/AB tests on a disposable PostgreSQL cluster.

Never connects to the running demo database or starts/stops monitoring services.
Requires root for BPF and a pre-existing non-root postgres OS account.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import pwd
import random
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from .collector import atomic_json, inspect_server

REPOSITORY = Path(__file__).resolve().parents[2]


@contextmanager
def cluster(pg_config, max_connections=12, statistics=False):
    if not isinstance(max_connections, int) or not 12 <= max_connections <= 32:
        raise ValueError("disposable cluster max_connections must be 12..32")
    info = inspect_server(pg_config)
    bindir = Path(info["binary"]).parent
    account = pwd.getpwnam("postgres")
    with tempfile.TemporaryDirectory(prefix="keen-ebpf-pg-") as temporary:
        root = Path(temporary)
        os.chown(str(root), account.pw_uid, account.pw_gid)
        data = root / "database"
        socket = root / "socket"
        socket.mkdir()
        os.chown(str(socket), account.pw_uid, account.pw_gid)
        base = ["runuser", "-u", account.pw_name, "--"]
        log = root / "server.log"
        started = False
        try:
            subprocess.run(base + [str(bindir / "initdb"), "-D", str(data), "--no-locale",
                                   "--encoding=UTF8", "--auth-local=trust", "--auth-host=reject"],
                           cwd="/", check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=60)
            with (data / "postgresql.conf").open("a") as handle:
                handle.write("\nlisten_addresses = ''\nport = 55432\n")
                handle.write("unix_socket_directories = '{}'\n".format(socket))
                handle.write("shared_buffers = '32MB'\nmax_connections = {}\n".format(max_connections))
                handle.write("max_parallel_workers_per_gather = 0\njit = off\n")
                if statistics:
                    handle.write("shared_preload_libraries = 'pg_stat_statements'\ntrack_io_timing = on\n")
            subprocess.run(base + [str(bindir / "pg_ctl"), "-D", str(data), "-l", str(log),
                                   "-w", "start"], cwd="/", check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
            started = True
            yield info, [str(bindir / "psql"), "-X", "-A", "-t", "-q", "-h", str(socket),
                         "-p", "55432", "-U", "postgres", "-d", "postgres"]
        finally:
            if started:
                subprocess.run(base + [str(bindir / "pg_ctl"), "-D", str(data), "-m", "immediate",
                                       "-w", "stop"], cwd="/", check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30)


class Session:
    def __init__(self, args, directory, label):
        self.errors = (directory / (label + ".stderr.log")).open("w")
        self.process = subprocess.Popen(args, cwd="/", stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=self.errors,
                                        text=True, bufsize=1)
        self.process.stdin.write("SELECT pg_backend_pid();\n")
        self.process.stdin.flush()
        self.pid = int(self.process.stdout.readline().strip())

    def batch(self, statements):
        marker = "done_" + uuid.uuid4().hex
        text = "\n".join(statements) + "\nSELECT '{}';\n".format(marker)
        # Limit outstanding output/input so a psql pipe cannot deadlock.
        if len(text.encode()) > 60000:
            raise ValueError("batch too large; reduce --queries")
        started = time.perf_counter()
        self.process.stdin.write(text)
        self.process.stdin.flush()
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("psql exited before the completion marker")
            if line.strip() == marker:
                return time.perf_counter() - started

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.communicate("\\q\n", timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.communicate()
        self.errors.close()


class Capture:
    def __init__(self, pg_config, pid, directory, profile, selector=None, alert_smoke=False,
                 extra=None):
        self.directory = directory
        self.log = directory.with_suffix(".log").open("w")
        args = [sys.executable, "-m", "experiments.ebpf", "collect", "--pg-config", pg_config,
                "--pid", str(pid), "--output", str(directory), "--profile", profile,
                "--detail-level", "full", "--scope", "query",
                "--duration", "120", "--collector-cpu-budget", "100000"]
        if selector:
            args += ["--selector", selector]
        if alert_smoke:
            args += ["--event-bytes", "1"]
        args += extra or []
        self.process = subprocess.Popen(args, cwd=str(REPOSITORY),
                                        stdout=self.log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                manifest = directory / "manifest.json"
                if manifest.exists():
                    status = json.loads(manifest.read_text())["status"]
                    if status == "ready":
                        return
                    if status == "failed":
                        break
                if self.process.poll() is not None:
                    break
                time.sleep(.05)
            raise RuntimeError("collector did not become ready; see " + str(directory.with_suffix(".log")))
        except BaseException:
            self.close(check=False)
            raise

    def close(self, check=True):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            raise RuntimeError("collector did not detach in time")
        finally:
            self.log.close()
        if check and self.process.returncode:
            raise RuntimeError("collector failed; see " + str(self.directory.with_suffix(".log")))


def read_events(directory):
    return [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]


def cpu_seconds(pid):
    value = (Path("/proc") / str(pid) / "stat").read_text()
    fields = value[value.rfind(")") + 2:].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def smoke(args, output):
    with cluster(args.pg_config) as (server, connection):
        session = Session(connection, output, "target")
        other = Session(connection, output, "untargeted")
        capture = None
        try:
            capture = Capture(args.pg_config, session.pid, output / "capture", "selected",
                              alert_smoke=True)
            other.batch(["SELECT pg_sleep(0.01);"])
            session.batch([
                "SET work_mem='64kB';",
                "CREATE TABLE persistent_sample(id int);",
                "INSERT INTO persistent_sample VALUES(1);",
                "CREATE TEMP TABLE sample AS SELECT g, repeat(md5(g::text),8) AS text "
                "FROM generate_series(1,16000) AS g;",
                "SELECT count(*) FROM (SELECT * FROM sample ORDER BY text) AS sorted;",
                "SELECT pg_sleep(0.08);",
                "SELECT 1/(g-2) FROM generate_series(1,3) AS g;",
                "SELECT 42;",
                "BEGIN;",
                "DECLARE cursor_test CURSOR FOR SELECT generate_series(1,10);",
                "FETCH 3 FROM cursor_test;",
                "FETCH 3 FROM cursor_test;",
                "CLOSE cursor_test;",
                "COMMIT;",
            ])
            time.sleep(.2)
            capture.close()
            capture = None
            events = read_events(output / "capture")
            ends = [e for e in events if e["type"] == "query_end"]
            aborted = [e for e in events if e["type"] == "query_aborted"]
            spill = sum(e["metrics"].get("temp_file_write", {}).get("calls", 0) for e in ends)
            alerts = sum(e["type"] == "metric_alert" for e in events)
            backend = [e for e in events if e["type"] in ("backend_final", "backend_end")]
            checks = {
                "has_executor_completions": len(ends) >= 5,
                "has_runtime_error_abort": bool(aborted),
                "has_real_temp_write_calls": spill > 0,
                "has_immediate_resource_alerts": alerts > 0,
                "untargeted_backend_excluded": all(e.get("pid") in (None, session.pid) for e in events),
                "no_plaintext_sql_by_default": not any("sql_prefix" in e for e in events),
                "unique_execution_ids": len({e["execution_id"] for e in ends}) == len(ends),
                "cpu_not_greater_than_wall": all(0 <= e["on_cpu_ns"] <= e["duration_ns"] for e in ends),
                "query_after_error_completes": bool(aborted) and any(
                    e["timestamp_ns"] > aborted[0]["timestamp_ns"] for e in ends),
                "system_wal_events_outside_executor_retained": any(
                    e["metrics"].get("wal_flush", {}).get("calls", 0) > 0 for e in backend),
                "backend_totals_not_mislabeled_as_queries": bool(backend) and all(
                    "execution_id" not in e and e["query_start_ns"] is None for e in backend),
            }
            manifest = json.loads((output / "capture" / "manifest.json").read_text())
            checks["no_reported_loss_or_capacity_errors"] = (
                not manifest["perf_lost_notifications"] and not any(manifest["bpf_health"].values()))
            capture = Capture(args.pg_config, session.pid, output / "adaptive", "selected",
                              extra=["--collector-cpu-budget", "0.001", "--report-seconds", ".1"])
            session.batch(["SELECT pg_sleep(.05);"] * 14)
            session.close()
            time.sleep(.2)
            capture.close()
            capture = None
            adaptive = read_events(output / "adaptive")
            pruned = [e for e in adaptive if e["type"] == "collector_health" and e["pruned_probe"]]
            checks["real_dynamic_detach"] = bool(pruned)
            checks["core_probes_never_pruned"] = all(
                e["pruned_probe"] not in ("vfs_read", "vfs_write") for e in pruned)
            checks["pruned_probe_not_reported_as_measured_zero"] = any(
                "not_observed_pruned" in e.get("probe_coverage", {}).values()
                for e in adaptive if e["type"] == "query_end")
            checks["backend_exit_flushes_system_totals"] = any(
                e["type"] == "backend_end" and e["reason"] == "backend_exit" for e in adaptive)
            capture = Capture(args.pg_config, other.pid, output / "sql-opt-in", "core",
                              extra=["--capture-sql"])
            other.batch(["SELECT 424242;"])
            capture.close()
            capture = None
            literal = read_events(output / "sql-opt-in")
            checks["explicit_sql_capture_works"] = any(
                e["type"] == "query_begin" and "424242" in e.get("sql_prefix", "")
                for e in literal)
            other.batch([
                "SET work_mem='64kB';",
                "CREATE TEMP TABLE light_fixture AS SELECT g, repeat(md5(g::text),4) AS payload "
                "FROM generate_series(1,8000) AS g;",
            ])
            spill_sql = "SELECT count(*) FROM (SELECT * FROM light_fixture ORDER BY payload) AS s;"
            capture = Capture(args.pg_config, other.pid, output / "light-query", "events",
                              extra=["--detail-level", "lightweight"])
            other.batch(["SELECT 1;"] * 50)
            other.batch([spill_sql])
            capture.close()
            capture = None
            light = read_events(output / "light-query")
            totals = [e for e in light if e["type"] == "query_aggregate_final"]
            checks["light_aggregation_keeps_exact_completion_count"] = (
                len(totals) == 1 and totals[0]["completed_spans"] == 53
                and sum(totals[0]["latency_log2_us"]) == 53)
            checks["light_omits_normal_query_boundary_messages"] = (
                not any(e["type"] == "query_begin" for e in light)
                and sum(e["type"] == "query_end" for e in light) < 5)
            checks["light_still_detects_real_temp_file_creation"] = any(
                e["type"] == "metric_alert" and e.get("probe") == "temp_file_create" for e in light)
            checks["count_only_does_not_invent_resource_timing_or_cpu"] = all(
                "on_cpu_ns" not in e and "off_cpu_ns" not in e
                and all("inclusive_duration_ns" not in v for v in e.get("metrics", {}).values())
                for e in light if e["type"] not in ("collector_health", "query_aggregate_final"))
            capture = Capture(args.pg_config, other.pid, output / "light-system", "events",
                              extra=["--detail-level", "lightweight", "--scope", "system"])
            other.batch(["SELECT 1;"] * 50)
            other.batch([spill_sql])
            capture.close()
            capture = None
            system = read_events(output / "light-system")
            checks["system_only_has_no_per_query_records"] = not any(
                e["type"].startswith("query_") or e.get("execution_id") for e in system)
            checks["system_only_preserves_targeted_anomaly_signal"] = any(
                e["type"] == "metric_alert" and e.get("probe") == "temp_file_create"
                and e["scope"] == "authorized_backend_collection_window" for e in system)
            for name in ("light-query", "light-system"):
                health = json.loads((output / name / "manifest.json").read_text())
                checks[name + "_no_reported_errors"] = (
                    health["status"] == "completed" and not health["perf_lost_notifications"]
                    and not any(health["bpf_health"].values()))
            report = {"status": "passed" if all(checks.values()) else "failed",
                      "checks": checks, "server": server, "completed_spans": len(ends),
                      "aborted_spans": len(aborted), "temp_write_calls": spill,
                      "alert_notifications": alerts,
                      "adaptive_detach_count": len(pruned),
                      "scope": "real PG12/kernel smoke; not a paper effectiveness/overhead result"}
            atomic_json(output / "smoke.json", report)
            print(json.dumps(report, indent=2))
            if not all(checks.values()):
                raise RuntimeError("real-kernel smoke validation failed")
        finally:
            if capture:
                capture.close(check=False)
            session.close()
            other.close()


def benchmark(args, output):
    if (not 3 <= args.repeats <= 20 or not 10 <= args.queries <= 300
            or not 1 <= args.batches <= 100):
        raise ValueError("use 3..20 repeats, 10..300 queries/batch, 1..100 batches")
    trials = []
    rng = random.Random(args.seed)
    query = "SELECT sum(g) FROM generate_series(1,20000) AS g;"
    with cluster(args.pg_config) as (server, connection):
        session = Session(connection, output, "benchmark")
        try:
            for repeat in range(args.repeats):
                modes = ["baseline", "core", "selected", "all"]
                rng.shuffle(modes)
                for mode in modes:
                    capture = None
                    trial = "{}-{}".format(repeat, mode)
                    directory = output / trial
                    try:
                        if mode != "baseline":
                            capture = Capture(args.pg_config, session.pid, directory, mode, args.selector)
                        session.batch([query] * 5)
                        before = cpu_seconds(session.pid)
                        elapsed = sum(session.batch([query] * args.queries)
                                      for _ in range(args.batches))
                        cpu = cpu_seconds(session.pid) - before
                        if capture:
                            capture.close()
                            capture = None
                        record = {"trial_id": trial, "repeat": repeat, "mode": mode,
                                  "query_count": args.queries * args.batches, "wall_seconds": elapsed,
                                  "backend_cpu_seconds": cpu,
                                  "queries_per_second": args.queries * args.batches / elapsed}
                        if mode != "baseline":
                            manifest = json.loads((directory / "manifest.json").read_text())
                            record["valid_capture"] = (manifest["status"] == "completed"
                                and not manifest["perf_lost_notifications"]
                                and not any(manifest["bpf_health"].values()))
                            record["selection"] = manifest["selection"]
                        trials.append(record)
                    finally:
                        if capture:
                            capture.close(check=False)
            for row in trials:
                baseline = next(r for r in trials if r["repeat"] == row["repeat"]
                                and r["mode"] == "baseline")
                row["paired_wall_overhead_percent"] = 100 * (
                    row["wall_seconds"] / baseline["wall_seconds"] - 1)
            report = {"schema": "keeninsight.ebpf-benchmark.v1", "server": server, "trials": trials,
                      "seed": args.seed, "query": query, "parallel_workers": 0,
                      "measurement": "persistent psql client wall time; parse/plan/protocol included",
                      "caveat": "shared host; background services left untouched; not an isolated-host "
                                "low-overhead claim; selected without model is bootstrap, not Lasso"}
            atomic_json(output / "benchmark.json", report)
            print(json.dumps(report, indent=2))
        finally:
            session.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("smoke", "benchmark"))
    parser.add_argument("--output", required=True, help="new run directory; never overwrites")
    parser.add_argument("--pg-config", default="/usr/lib/postgresql/12/bin/pg_config")
    parser.add_argument("--selector")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--queries", type=int, default=100)
    parser.add_argument("--batches", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20261103)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("this isolated test harness requires root for BPF and runuser")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    if args.mode == "smoke":
        smoke(args, output)
    else:
        benchmark(args, output)


if __name__ == "__main__":
    main()
