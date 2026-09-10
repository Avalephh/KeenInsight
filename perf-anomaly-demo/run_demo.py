#!/usr/bin/env python3
"""Run a safe PostgreSQL anomaly and exercise the perf/SysInsight hand-off.

The workload only reads generated values from PostgreSQL. It does not create,
alter, update, or delete any database object. The anomaly perf session starts
only after the configured Prometheus alert is observed.
"""

from __future__ import annotations

import argparse
import ast
import csv
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from urllib.error import URLError, HTTPError
from urllib.request import Request, urlopen
from typing import Any


ROOT = Path(__file__).resolve().parent
SYSINSIGHT_SOURCE_ROOT = Path(
    os.environ.get(
        "SYSINSIGHT_SOURCE_ROOT",
        str(ROOT.parent / "repositories/Avalephh-KeenInsight/branch-sources/WorkloadTune"),
    )
)
SYSINSIGHT_ANALYZER = SYSINSIGHT_SOURCE_ROOT / "DBTuner/utils/analyzeException.py"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_int_file(path: str) -> int | None:
    try:
        return int(Path(path).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="keeninsight")
    parser.add_argument("--user", default="postgres", help="PostgreSQL role")
    parser.add_argument("--run-as", default="postgres", help="OS user used for psql")
    parser.add_argument("--host", default="/var/run/postgresql")
    parser.add_argument("--baseline-duration", type=float, default=8.0)
    parser.add_argument("--anomaly-duration", type=float, default=15.0)
    parser.add_argument("--post-duration", type=float, default=4.0)
    parser.add_argument("--baseline-workers", type=int, default=1)
    parser.add_argument("--anomaly-workers", type=int, default=2)
    parser.add_argument("--baseline-rows", type=int, default=80_000)
    parser.add_argument("--anomaly-rows", type=int, default=260_000)
    parser.add_argument("--sample-interval", type=float, default=1.0)
    parser.add_argument("--perf-frequency", type=int, default=300)
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:9090")
    parser.add_argument("--alert-name", default="SysInsightDemoAnomaly")
    parser.add_argument("--alert-poll-interval", type=float, default=1.0)
    parser.add_argument(
        "--stackcollapse",
        default="",
        help="Path to FlameGraph/stackcollapse-perf.pl; optional",
    )
    parser.add_argument(
        "--keep-workers-on-error",
        action="store_true",
        help="Do not terminate only our worker psql processes after an error",
    )
    args = parser.parse_args()
    if args.baseline_duration <= 0 or args.anomaly_duration <= 0:
        parser.error("durations must be positive")
    if args.post_duration < 0 or args.sample_interval <= 0:
        parser.error("post duration must be non-negative and sample interval positive")
    if args.alert_poll_interval <= 0:
        parser.error("alert poll interval must be positive")
    if args.baseline_workers < 1 or args.anomaly_workers < 1:
        parser.error("worker counts must be positive")
    if args.baseline_rows < 1 or args.anomaly_rows < 1:
        parser.error("row counts must be positive")
    return args


def base_psql_command(args: argparse.Namespace, query: str) -> list[str]:
    return [
        "psql",
        "-X",
        "-A",
        "-t",
        "-q",
        "-v",
        "ON_ERROR_STOP=1",
        "-h",
        args.host,
        "-U",
        args.user,
        "-d",
        args.db,
        "-c",
        query,
    ]


def psql_command(args: argparse.Namespace, query: str, application_name: str) -> list[str]:
    command = base_psql_command(args, query)
    if os.geteuid() == 0 and args.run_as and args.run_as != "root":
        return [
            "sudo",
            "-n",
            "-u",
            args.run_as,
            "--",
            "env",
            f"PGAPPNAME={application_name}",
            *command,
        ]
    return command


def run_psql(args: argparse.Namespace, query: str, application_name: str, timeout: float = 10.0) -> str:
    completed = subprocess.run(
        psql_command(args, query, application_name),
        cwd="/",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=True,
    )
    return completed.stdout.strip()


def snapshot(args: argparse.Namespace, phase: str) -> dict[str, Any]:
    query = """
SELECT json_build_object(
  'ts', extract(epoch FROM clock_timestamp()),
  'database', current_database(),
  'active_total', (SELECT count(*)::int FROM pg_stat_activity WHERE state = 'active'),
  'demo_active', (SELECT count(*)::int FROM pg_stat_activity
                  WHERE application_name LIKE 'perf-anomaly-demo-%' AND state = 'active'),
  'demo_waiting', (SELECT count(*)::int FROM pg_stat_activity
                   WHERE application_name LIKE 'perf-anomaly-demo-%'
                     AND wait_event IS NOT NULL),
  'xact_commit', xact_commit,
  'xact_rollback', xact_rollback,
  'tup_returned', tup_returned,
  'tup_fetched', tup_fetched,
  'blks_read', blks_read,
  'blks_hit', blks_hit,
  'temp_files', temp_files,
  'temp_bytes', temp_bytes,
  'deadlocks', deadlocks,
  'database_size', pg_database_size(current_database())
)::text
FROM pg_stat_database
WHERE datname = current_database();
"""
    raw = run_psql(args, query, f"perf-anomaly-monitor-{phase}")
    data = json.loads(raw)
    data["phase"] = phase
    data["observed_at"] = utc_now()
    return data


def demo_pids(args: argparse.Namespace) -> list[int]:
    query = """
SELECT pid
FROM pg_stat_activity
WHERE application_name LIKE 'perf-anomaly-demo-%'
  AND state = 'active'
ORDER BY pid;
"""
    raw = run_psql(args, query, "perf-anomaly-pid-reader")
    pids: list[int] = []
    for line in raw.splitlines():
        try:
            pids.append(int(line.strip()))
        except ValueError:
            continue
    return pids


def process_cpu(pids: list[int]) -> float:
    if not pids:
        return 0.0
    output = subprocess.run(
        ["ps", "-p", ",".join(str(pid) for pid in pids), "-o", "pcpu="],
        cwd="/",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    ).stdout
    total = 0.0
    for line in output.splitlines():
        try:
            total += float(line.strip())
        except ValueError:
            continue
    return round(total, 2)


def read_prometheus_alert(args: argparse.Namespace) -> dict[str, Any] | None:
    """Read a firing alert from Prometheus' own alerts API."""
    url = args.prometheus_url.rstrip("/") + "/api/v1/alerts"
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, HTTPError, URLError):
        return None
    if payload.get("status") != "success":
        return None
    for alert in payload.get("data", {}).get("alerts", []):
        labels = alert.get("labels", {})
        if labels.get("alertname") == args.alert_name and alert.get("state") == "firing":
            return alert
    return None


def worker_sql(duration: float, rows: int, anomaly: bool) -> str:
    seconds = f"{duration:.3f}"
    if anomaly:
        expression = (
            f"sum(length(md5(i::text))::double precision) "
            f"FROM generate_series(1, {rows}) AS g(i)"
        )
    else:
        expression = f"sum(sqrt(i::double precision)) FROM generate_series(1, {rows}) AS g(i)"
    return f"""
DO $$
DECLARE
  deadline timestamptz := clock_timestamp() + interval '{seconds} seconds';
  result_value double precision;
BEGIN
  WHILE clock_timestamp() < deadline LOOP
    SELECT {expression} INTO result_value;
  END LOOP;
END
$$;
"""


def start_workers(
    args: argparse.Namespace,
    result_dir: Path,
    phase: str,
    workers: int,
    duration: float,
    rows: int,
    anomaly: bool,
) -> list[dict[str, Any]]:
    processes: list[dict[str, Any]] = []
    for index in range(workers):
        app_name = f"perf-anomaly-demo-{phase}-{index}"
        log_path = result_dir / f"{phase}_worker_{index}.log"
        log_handle = log_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            psql_command(args, worker_sql(duration, rows, anomaly), app_name),
            cwd="/",
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        processes.append({"process": process, "log": log_handle, "app_name": app_name})
    return processes


def close_workers(processes: list[dict[str, Any]], terminate: bool) -> None:
    for item in processes:
        process = item["process"]
        if terminate and process.poll() is None:
            process.terminate()
    for item in processes:
        process = item["process"]
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        item["log"].close()


def wait_for_demo_pids(args: argparse.Namespace, count: int, timeout: float = 10.0) -> list[int]:
    deadline = time.monotonic() + timeout
    last: list[int] = []
    while time.monotonic() < deadline:
        try:
            last = demo_pids(args)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            last = []
        if len(last) >= count:
            return last
        time.sleep(0.2)
    return last


def start_perf(
    args: argparse.Namespace,
    result_dir: Path,
    phase: str,
    pids: list[int],
    duration: float,
) -> dict[str, Any]:
    perf_path = shutil.which("perf")
    info: dict[str, Any] = {
        "phase": phase,
        "requested_at": utc_now(),
        "perf_path": perf_path,
        "pids": pids,
        "frequency": args.perf_frequency,
    }
    if not perf_path:
        info["status"] = "unavailable"
        info["reason"] = "perf executable was not found in the current environment"
        return info

    data_path = result_dir / f"{phase}.perf.data"
    log_path = result_dir / f"{phase}_perf.log"
    command = [
        perf_path,
        "record",
        "-F",
        str(args.perf_frequency),
        "-g",
        "-p",
        ",".join(str(pid) for pid in pids),
        "-o",
        str(data_path),
        "--",
        "sleep",
        str(max(1, int(duration))),
    ]
    log_handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd="/",
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    time.sleep(0.5)
    if process.poll() is not None:
        log_handle.close()
        info["status"] = "failed_to_start"
        info["returncode"] = process.returncode
        info["data_path"] = str(data_path.relative_to(ROOT))
        return info
    info.update(
        {
            "status": "running",
            "started_at": utc_now(),
            "data_path": str(data_path.relative_to(ROOT)),
            "command": command,
            "process": process,
            "log_handle": log_handle,
        }
    )
    return info


def stop_perf(info: dict[str, Any]) -> None:
    process = info.get("process")
    if process is None:
        return
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
    info["returncode"] = process.returncode
    info["stopped_at"] = utc_now()
    data_path = ROOT / info["data_path"] if info.get("data_path") else None
    has_data = data_path is not None and data_path.exists() and data_path.stat().st_size > 0
    info["status"] = "completed" if process.returncode == 0 or has_data else "failed"
    if has_data and process.returncode != 0:
        info["finalized_with_signal"] = True
    info.pop("process", None)
    log_handle = info.pop("log_handle", None)
    if log_handle is not None:
        log_handle.close()


def collect_samples(
    args: argparse.Namespace,
    result_dir: Path,
    phase: str,
    duration: float,
    processes: list[dict[str, Any]],
    perf_info: dict[str, Any] | None = None,
    perf_starter: Any | None = None,
    alert_reader: Any | None = None,
) -> dict[str, Any]:
    output_path = result_dir / f"{phase}_samples.jsonl"
    samples: list[dict[str, Any]] = []
    trigger: dict[str, Any] | None = None
    last_alert_poll = 0.0
    alert: dict[str, Any] | None = None
    start = time.monotonic()
    deadline = start + duration
    while time.monotonic() < deadline or any(item["process"].poll() is None for item in processes):
        try:
            pids = demo_pids(args)
            item = snapshot(args, phase)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            item = {"phase": phase, "observed_at": utc_now(), "error": str(exc)}
            pids = []
        item["demo_pids"] = pids
        item["demo_cpu_pct"] = process_cpu(pids)
        item["elapsed_seconds"] = round(time.monotonic() - start, 3)
        samples.append(item)
        with output_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")

        elapsed = time.monotonic() - start
        if alert_reader is not None and elapsed - last_alert_poll >= args.alert_poll_interval:
            alert = alert_reader()
            last_alert_poll = elapsed
        if trigger is None and alert is not None:
            trigger_pids = pids or demo_pids(args)
            trigger = {
                "triggered": True,
                "at": utc_now(),
                "elapsed_seconds": round(elapsed, 3),
                "source": "prometheus",
                "alert_name": alert.get("labels", {}).get("alertname"),
                "alert": alert,
                "pids": trigger_pids,
                "backend_cpu_pct_at_trigger": float(item.get("demo_cpu_pct", 0.0)),
            }
            if perf_info is not None:
                perf_info["trigger_observed_at"] = trigger["at"]
            if perf_starter is not None and perf_info is None and trigger_pids:
                perf_info = perf_starter(trigger_pids)
                perf_info["trigger_observed_at"] = trigger["at"]
        remaining = max(0.0, min(args.sample_interval, deadline - time.monotonic()))
        if remaining:
            time.sleep(remaining)

    return {
        "phase": phase,
        "samples_path": str(output_path.relative_to(ROOT)),
        "sample_count": len(samples),
        "first": samples[0] if samples else None,
        "last": samples[-1] if samples else None,
        "trigger": trigger,
        "perf": perf_info,
    }


def write_normal_profile(counts_path: Path, profile_path: Path) -> int:
    rows: list[tuple[str, float]] = []
    with counts_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        next(reader, None)
        for row in reader:
            if len(row) < 3:
                continue
            try:
                rows.append((row[1], float(row[2].rstrip("%"))))
            except ValueError:
                continue
    with profile_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "Function",
                "Min Sampling Rate (%)",
                "Max Sampling Rate (%)",
                "Average Sampling Rate (%)",
            ]
        )
        for function, value in rows:
            writer.writerow([function, value, value, value])
    return len(rows)


def run_original_perf_function_range(folded_path: Path) -> Path | None:
    """Execute DBEnv.get_perf_function_range from the checked-out source.

    Importing the whole original DBEnv module would require its benchmark
    environment and optional dependencies. The method itself only uses os,
    so this loads that exact method definition from the original source file
    and calls it without constructing DBEnv.
    """
    source = SYSINSIGHT_ANALYZER.parent.parent / "dbenv.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    db_env = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DBEnv"
    )
    method = next(
        node
        for node in db_env.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "get_perf_function_range"
    )
    module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"os": os}
    exec(compile(module, str(source), "exec"), namespace)
    output = namespace["get_perf_function_range"](None, None, 2, str(folded_path))
    return Path(output) if output else None


def postprocess_perf(args: argparse.Namespace, result_dir: Path, perf_info: dict[str, Any]) -> dict[str, Any]:
    if perf_info.get("status") != "completed":
        return {"status": perf_info.get("status", "not_run"), "reason": perf_info.get("reason", "")}
    data_path = ROOT / perf_info["data_path"]
    perf_path = perf_info.get("perf_path") or shutil.which("perf")
    if not data_path.exists() or not perf_path:
        return {"status": "raw_data_missing", "data_path": perf_info.get("data_path")}

    raw_path = result_dir / f"{perf_info['phase']}.perf.script"
    with raw_path.open("w", encoding="utf-8") as raw_handle:
        command = [perf_path, "script", "-i", str(data_path)]
        completed = subprocess.run(
            command,
            cwd="/",
            stdout=raw_handle,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    result: dict[str, Any] = {
        "status": "script_generated" if completed.returncode == 0 else "script_failed",
        "script_path": str(raw_path.relative_to(ROOT)),
        "script_returncode": completed.returncode,
    }
    if completed.returncode != 0:
        result["stderr"] = completed.stderr[-2000:]
        return result

    stackcollapse = args.stackcollapse
    if not stackcollapse:
        candidates = [
            "/root/RUC/FlameGraph/stackcollapse-perf.pl",
            "/root/FlameGraph/stackcollapse-perf.pl",
            str(ROOT / "vendor" / "FlameGraph" / "stackcollapse-perf.pl"),
        ]
        stackcollapse = next((candidate for candidate in candidates if Path(candidate).exists()), "")
    if not stackcollapse:
        result.update(
            {
                "status": "script_generated_no_stackcollapse",
                "reason": "provide FlameGraph/stackcollapse-perf.pl to create folded stacks",
            }
        )
        return result

    folded_path = result_dir / f"{perf_info['phase']}.folded"
    with raw_path.open(encoding="utf-8", errors="replace") as raw_handle, folded_path.open(
        "w", encoding="utf-8"
    ) as folded_handle:
        completed = subprocess.run(
            ["perl", stackcollapse],
            cwd="/",
            stdin=raw_handle,
            stdout=folded_handle,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    if completed.returncode != 0:
        result.update({"status": "stackcollapse_failed", "stderr": completed.stderr[-2000:]})
        return result

    counts_path = run_original_perf_function_range(folded_path)
    if counts_path is None or not counts_path.exists():
        result.update(
            {
                "status": "sysinsight_function_range_failed",
                "source": str(SYSINSIGHT_ANALYZER.parent.parent / "dbenv.py"),
            }
        )
        return result
    with counts_path.open(encoding="utf-8") as handle:
        function_count = max(0, sum(1 for _ in handle) - 1)
    result.update(
        {
            "status": "sysinsight_counts_generated",
            "folded_path": str(folded_path.relative_to(ROOT)),
            "counts_path": str(counts_path.relative_to(ROOT)),
            "function_count": function_count,
        }
    )
    return result


def compare_with_sysinsight_source(counts_path: Path, normal_profile_path: Path, result_dir: Path) -> dict[str, Any]:
    """Run the original source comparison on the generated PG sample files."""
    if not SYSINSIGHT_ANALYZER.exists():
        return {"status": "source_missing", "source": str(SYSINSIGHT_ANALYZER)}
    spec = importlib.util.spec_from_file_location("sysinsight_analyze_exception", SYSINSIGHT_ANALYZER)
    if spec is None or spec.loader is None:
        return {"status": "source_load_failed", "source": str(SYSINSIGHT_ANALYZER)}
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output_path, functions = module.compare_file_sample_rate(str(counts_path), str(normal_profile_path))
    output = Path(output_path)
    try:
        relative_output = str(output.relative_to(ROOT))
    except ValueError:
        relative_output = str(output)
    return {
        "status": "completed",
        "source": str(SYSINSIGHT_ANALYZER),
        "input_counts": str(counts_path.relative_to(ROOT)),
        "normal_profile": str(normal_profile_path.relative_to(ROOT)),
        "key_function_file": relative_output,
        "key_function_count": len(functions),
        "key_functions_top10": functions[:10],
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def numeric_delta(rows: list[dict[str, Any]], key: str) -> float | int | None:
    values = [row[key] for row in rows if isinstance(row.get(key), (int, float))]
    if len(values) < 2:
        return None
    return values[-1] - values[0]


def main() -> int:
    args = parse_args()
    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    result_dir = ROOT / "results" / run_id
    result_dir.mkdir(parents=True, exist_ok=False)

    preflight = {
        "started_at": utc_now(),
        "db": args.db,
        "db_user": args.user,
        "host": args.host,
        "psql_path": shutil.which("psql"),
        "perf_path": shutil.which("perf"),
        "perf_event_paranoid": read_int_file("/proc/sys/kernel/perf_event_paranoid"),
        "kptr_restrict": read_int_file("/proc/sys/kernel/kptr_restrict"),
        "cpu_count": os.cpu_count(),
        "prometheus_url": args.prometheus_url,
        "alert_name": args.alert_name,
        "alert_poll_interval": args.alert_poll_interval,
    }
    (result_dir / "preflight.json").write_text(json.dumps(preflight, indent=2), encoding="utf-8")

    try:
        initial = snapshot(args, "preflight")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        (result_dir / "error.txt").write_text(f"database preflight failed: {exc}\n", encoding="utf-8")
        print(f"数据库预检查失败：{exc}", file=sys.stderr)
        return 2

    baseline_workers: list[dict[str, Any]] = []
    anomaly_workers: list[dict[str, Any]] = []
    perf_sessions: list[dict[str, Any]] = []
    anomaly_perf_holder: dict[str, Any] = {"info": None}
    summary: dict[str, Any] = {"run_id": run_id, "preflight": preflight, "initial": initial}
    try:
        print("[1/4] 运行基线负载（只读 generate_series）...")
        baseline_workers = start_workers(
            args,
            result_dir,
            "baseline",
            args.baseline_workers,
            args.baseline_duration,
            args.baseline_rows,
            anomaly=False,
        )
        baseline_pids = wait_for_demo_pids(args, args.baseline_workers)
        baseline_perf = start_perf(
            args,
            result_dir,
            "baseline",
            baseline_pids,
            args.baseline_duration + 2,
        )
        perf_sessions.append(baseline_perf)
        summary["baseline"] = collect_samples(
            args,
            result_dir,
            "baseline",
            args.baseline_duration + 1.5,
            baseline_workers,
            perf_info=baseline_perf,
        )
        close_workers(baseline_workers, terminate=False)
        stop_perf(baseline_perf)
        baseline_postprocess = postprocess_perf(args, result_dir, baseline_perf)
        summary["baseline_perf"] = baseline_postprocess
        normal_profile_path: Path | None = None
        if baseline_postprocess.get("status") == "sysinsight_counts_generated":
            baseline_counts = ROOT / baseline_postprocess["counts_path"]
            normal_profile_path = result_dir / "normal_profile_postgresql_demo.csv"
            normal_function_count = write_normal_profile(baseline_counts, normal_profile_path)
            summary["normal_profile"] = {
                "path": str(normal_profile_path.relative_to(ROOT)),
                "function_count": normal_function_count,
                "source": "generated from this run's baseline perf window",
            }

        print("[2/4] 触发异常负载（更多并发 + MD5 计算）...")
        anomaly_workers = start_workers(
            args,
            result_dir,
            "anomaly",
            args.anomaly_workers,
            args.anomaly_duration,
            args.anomaly_rows,
            anomaly=True,
        )
        def start_anomaly_perf(pids: list[int]) -> dict[str, Any]:
            info = start_perf(
                args,
                result_dir,
                "anomaly",
                pids,
                args.anomaly_duration + args.post_duration + 3,
            )
            anomaly_perf_holder["info"] = info
            perf_sessions.append(info)
            return info

        summary["anomaly"] = collect_samples(
            args,
            result_dir,
            "anomaly",
            args.anomaly_duration + args.post_duration,
            anomaly_workers,
            perf_starter=start_anomaly_perf,
            alert_reader=lambda: read_prometheus_alert(args),
        )
        close_workers(anomaly_workers, terminate=False)
        anomaly_perf = anomaly_perf_holder["info"]
        if anomaly_perf is None:
            raise RuntimeError("anomaly detector did not produce a perf session record")
        stop_perf(anomaly_perf)
        anomaly_postprocess = postprocess_perf(args, result_dir, anomaly_perf)
        summary["anomaly_perf"] = anomaly_postprocess
        if (
            normal_profile_path is not None
            and anomaly_postprocess.get("status") == "sysinsight_counts_generated"
        ):
            anomaly_counts = ROOT / anomaly_postprocess["counts_path"]
            summary["sysinsight_source_compare"] = compare_with_sysinsight_source(
                anomaly_counts, normal_profile_path, result_dir
            )

        print("[3/4] 汇总数据库计数器和触发结果...")
        anomaly_rows = read_jsonl(result_dir / "anomaly_samples.jsonl")
        summary["anomaly_counter_delta"] = {
            key: numeric_delta(anomaly_rows, key)
            for key in [
                "xact_commit",
                "xact_rollback",
                "tup_returned",
                "tup_fetched",
                "blks_read",
                "blks_hit",
                "temp_files",
                "temp_bytes",
            ]
        }
        summary["completed_at"] = utc_now()
        summary["safety"] = {
            "database_objects_changed": False,
            "writes_issued": False,
            "load_type": "CPU-only generated reads; no application table touched",
        }
        (result_dir / "perf_sessions.json").write_text(
            json.dumps(clean_for_json(perf_sessions), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        (result_dir / "summary.json").write_text(
            json.dumps(clean_for_json(summary), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print("[4/4] 测试完成。")
        print(f"结果目录：{result_dir}")
        print(f"异常触发：{summary['anomaly'].get('trigger')}")
        print(f"perf 状态：{summary['anomaly_perf'].get('status')}")
        return 0
    except KeyboardInterrupt:
        print("收到中断，正在清理本脚本启动的 worker...", file=sys.stderr)
        return 130
    except Exception as exc:  # keep cleanup in finally and leave diagnostic files
        (result_dir / "error.txt").write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        print(f"测试失败：{exc}", file=sys.stderr)
        return 1
    finally:
        if not args.keep_workers_on_error:
            close_workers(baseline_workers, terminate=True)
            close_workers(anomaly_workers, terminate=True)
        for perf_info in perf_sessions:
            if perf_info.get("process") is not None:
                stop_perf(perf_info)


def clean_for_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: clean_for_json(item) for key, item in value.items() if key not in {"process", "log_handle"}}
    if isinstance(value, list):
        return [clean_for_json(item) for item in value]
    return value


if __name__ == "__main__":
    raise SystemExit(main())
