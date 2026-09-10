#!/usr/bin/env python3
"""Repeat the external TPCC cases and select materially recoverable cases."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import re
import statistics
import subprocess
import sys
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parent
RUNNER = ROOT / "tpcc_external_cases.py"
DEFAULT_CASES = "d01_work_mem_sort,d02_parallel_worker_contention,d03_parallel_scan_threshold,d07_temp_buffer_pressure,d08_parallel_worker_cap,d11_jit_high_concurrency"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--only", default=DEFAULT_CASES)
    parser.add_argument("--baseline-duration", type=int, default=8)
    parser.add_argument("--case-duration", type=int, default=22)
    parser.add_argument("--tuned-duration", type=int, default=22)
    parser.add_argument("--normal-clients", type=int, default=1)
    parser.add_argument("--perf-frequency", type=int, default=300)
    return parser.parse_args()


def ratio_values(items: List[Dict[str, Any]], key: str) -> List[float]:
    values: List[float] = []
    for item in items:
        value = item.get("improvement", {}).get(key)
        if isinstance(value, (int, float)):
            values.append(float(value))
    return values


def recoverability(case_result: Dict[str, Any]) -> Dict[str, Any]:
    baseline = case_result.get("baseline_metrics", {})
    anomaly_control = None
    tuned_control = None
    for worker in case_result.get("anomaly", {}).get("all_process_metrics", {}).get("workers", []):
        if "control" in worker.get("app_name", ""):
            anomaly_control = worker.get("metrics", {}).get("tps")
            break
    for worker in case_result.get("tuned", {}).get("all_process_metrics", {}).get("workers", []):
        if "control" in worker.get("app_name", ""):
            tuned_control = worker.get("metrics", {}).get("tps")
            break
    baseline_tps = baseline.get("tps")
    pressure_ratio = (
        anomaly_control / baseline_tps
        if isinstance(anomaly_control, (int, float)) and isinstance(baseline_tps, (int, float)) and baseline_tps
        else None
    )
    repair_ratio = (
        tuned_control / anomaly_control
        if isinstance(tuned_control, (int, float)) and isinstance(anomaly_control, (int, float)) and anomaly_control
        else None
    )
    # The protected workload is always the normal TPCC pgbench control.
    # The injected external workload may be pgbench or psql, but both phases
    # still produce a control TPS that must satisfy the same user criterion.
    if (
        isinstance(baseline_tps, (int, float))
        and isinstance(anomaly_control, (int, float))
        and isinstance(tuned_control, (int, float))
    ):
        return {
            "baseline_control_tps": baseline_tps,
            "pressure_control_tps": anomaly_control,
            "repaired_control_tps": tuned_control,
            "pressure_ratio_to_baseline": pressure_ratio,
            "repair_ratio_to_pressure": repair_ratio,
            "pressure_drop_over_20pct": isinstance(pressure_ratio, (int, float)) and pressure_ratio <= 0.80,
            "repair_rise_over_20pct": isinstance(repair_ratio, (int, float)) and repair_ratio >= 1.20,
            "complete_case": (
                isinstance(pressure_ratio, (int, float))
                and isinstance(repair_ratio, (int, float))
                and pressure_ratio <= 0.80
                and repair_ratio >= 1.20
            ),
            "metric": "normal_control_tps",
        }
    improvement = case_result.get("improvement", {})
    mode = case_result.get("mode")
    if mode == "psql":
        ratio = improvement.get("timing_ratio")
        large = isinstance(ratio, (int, float)) and ratio <= 0.80
        direction = isinstance(ratio, (int, float)) and ratio < 1.0
        metric = "timing_ratio"
    else:
        tps = improvement.get("tps_ratio")
        latency = improvement.get("latency_ratio")
        large = (
            isinstance(tps, (int, float)) and tps >= 1.20
        ) or (
            isinstance(latency, (int, float)) and latency <= 0.80
        )
        direction = (
            isinstance(tps, (int, float)) and tps > 1.0
        ) or (
            isinstance(latency, (int, float)) and latency < 1.0
        )
        metric = "tps_ratio_or_latency_ratio"
    return {"large_recovery": large, "direction_improved": direction, "metric": metric}


def run_one(args: argparse.Namespace, cases: str, output_path: Path) -> Optional[Path]:
    command = [
        sys.executable,
        str(RUNNER),
        "--only",
        cases,
        "--baseline-duration",
        str(args.baseline_duration),
        "--case-duration",
        str(args.case_duration),
        "--tuned-duration",
        str(args.tuned_duration),
        "--normal-clients",
        str(args.normal_clients),
        "--perf-frequency",
        str(args.perf_frequency),
    ]
    completed = subprocess.run(
        command,
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output_path.write_text(completed.stdout, encoding="utf-8")
    matches = re.findall(r"结果目录：([^\r\n]+)", completed.stdout)
    if completed.returncode != 0 or not matches:
        return None
    return Path(matches[-1].strip())


def compact_result(case_result: Dict[str, Any]) -> Dict[str, Any]:
    anomaly = case_result.get("anomaly", {})
    tuned = case_result.get("tuned", {})
    detection = case_result.get("sysinsight_source_detection", {})
    source = detection.get("source_compare", {})
    decision = recoverability(case_result)
    return {
        "id": case_result.get("id"),
        "mode": case_result.get("mode"),
        "repair_candidate": case_result.get("repair_candidate"),
        "repair_candidate_source": case_result.get("repair_candidate_source", "legacy_runner_preset"),
        "gpt_generated_configuration": bool(case_result.get("gpt_generated_configuration", False)),
        "anomaly_metrics": anomaly.get("metrics"),
        "tuned_metrics": tuned.get("metrics"),
        "improvement": case_result.get("improvement"),
        "normal_control": case_result.get("baseline_metrics", {}),
        "prometheus_triggered": bool(anomaly.get("samples", {}).get("trigger")),
        "perf_status": anomaly.get("perf_postprocess", {}).get("status"),
        "source_anomaly_functions": source.get("key_function_count"),
        "global_settings_unchanged": case_result.get("improvement", {}).get(
            "global_settings_unchanged"
        ),
        "decision": decision,
    }


def main() -> int:
    args = parse_args()
    if args.repetitions < 2:
        raise SystemExit("repetitions must be at least 2")
    cases = ",".join(item.strip() for item in args.only.split(",") if item.strip())
    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = ROOT / "results" / "tpcc_repeated" / run_id
    output_root.mkdir(parents=True, exist_ok=False)
    all_results: Dict[str, List[Dict[str, Any]]] = {}
    run_dirs: List[str] = []

    for index in range(1, args.repetitions + 1):
        log_path = output_root / "repeat_{}.log".format(index)
        print("开始第 {}/{} 次配对复测...".format(index, args.repetitions), flush=True)
        run_dir = run_one(args, cases, log_path)
        if run_dir is None:
            print("第 {} 次测试失败，详见 {}".format(index, log_path), flush=True)
            continue
        run_dirs.append(str(run_dir))
        for case_id in cases.split(","):
            result_path = run_dir / case_id / "case_result.json"
            if not result_path.exists():
                continue
            result = json.loads(result_path.read_text(encoding="utf-8"))
            compact = compact_result(result)
            compact["repeat"] = index
            compact["run_dir"] = str(run_dir)
            all_results.setdefault(case_id, []).append(compact)
        print("第 {}/{} 次完成：{}".format(index, args.repetitions, run_dir), flush=True)

    summary: Dict[str, Any] = {
        "run_id": run_id,
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "cases": cases.split(","),
        "repetitions_requested": args.repetitions,
        "runner_runs": run_dirs,
        "criteria": {
            "repetitions_minimum": 3,
            "pressure_drop": "normal TPCC control TPS during external pressure <= 0.80 of the no-pressure baseline",
            "repair_rise": "normal TPCC control TPS after repair >= 1.20 of the pressure-stage TPS",
            "parameter_uniqueness": "final six cases must use six different repair parameter names",
            "direction_consistency": "all 3 repetitions must satisfy both conditions",
            "gpt_evidence": "this legacy repeat runner is preset-only; it cannot establish GPT evidence",
        },
        "results": all_results,
        "selection": {},
    }
    for case_id, results in all_results.items():
        decisions = [item["decision"] for item in results]
        pressure_count = sum(1 for item in decisions if item.get("pressure_drop_over_20pct"))
        repair_count = sum(1 for item in decisions if item.get("repair_rise_over_20pct"))
        summary["selection"][case_id] = {
            "repetitions_observed": len(results),
            "pressure_drop_count": pressure_count,
            "repair_rise_count": repair_count,
            "baseline_control_tps": statistics.median(
                [item["decision"]["baseline_control_tps"] for item in results if isinstance(item["decision"].get("baseline_control_tps"), (int, float))]
            ) if any(isinstance(item["decision"].get("baseline_control_tps"), (int, float)) for item in results) else None,
            "median_pressure_control_tps": statistics.median(
                [item["decision"]["pressure_control_tps"] for item in results if isinstance(item["decision"].get("pressure_control_tps"), (int, float))]
            ) if any(isinstance(item["decision"].get("pressure_control_tps"), (int, float)) for item in results) else None,
            "median_repaired_control_tps": statistics.median(
                [item["decision"]["repaired_control_tps"] for item in results if isinstance(item["decision"].get("repaired_control_tps"), (int, float))]
            ) if any(isinstance(item["decision"].get("repaired_control_tps"), (int, float)) for item in results) else None,
            "median_pressure_ratio_to_baseline": statistics.median(
                [item["decision"]["pressure_ratio_to_baseline"] for item in results if isinstance(item["decision"].get("pressure_ratio_to_baseline"), (int, float))]
            ) if any(isinstance(item["decision"].get("pressure_ratio_to_baseline"), (int, float)) for item in results) else None,
            "median_repair_ratio_to_pressure": statistics.median(
                [item["decision"]["repair_ratio_to_pressure"] for item in results if isinstance(item["decision"].get("repair_ratio_to_pressure"), (int, float))]
            ) if any(isinstance(item["decision"].get("repair_ratio_to_pressure"), (int, float)) for item in results) else None,
            "median_tps_ratio": statistics.median(
                ratio_values(results, "tps_ratio")
            ) if ratio_values(results, "tps_ratio") else None,
            "median_latency_ratio": statistics.median(
                ratio_values(results, "latency_ratio")
            ) if ratio_values(results, "latency_ratio") else None,
            "median_timing_ratio": statistics.median(
                ratio_values(results, "timing_ratio")
            ) if ratio_values(results, "timing_ratio") else None,
            "complete_case": len(results) >= 3 and pressure_count == 3 and repair_count == 3,
        }
    summary["complete_case_count"] = sum(
        1 for item in summary["selection"].values() if item["complete_case"]
    )
    (output_root / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    print("重复验证完成，结果目录：{}".format(output_root), flush=True)
    print("满足完整标准的案例数：{}".format(summary["complete_case_count"]), flush=True)
    return 0 if len(run_dirs) == args.repetitions else 1


if __name__ == "__main__":
    raise SystemExit(main())
