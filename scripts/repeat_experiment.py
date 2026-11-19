#!/usr/bin/env python3
"""Repeat the complete experiment protocol and aggregate only its evidence.

Every run is delegated to ``run_experiment.py``.  This script never supplies
expected timings or improvements; the summary is calculated from the JSON
returned by the running API.  The per-run records remain in the output so an
aggregate cannot hide a failed or inconclusive repetition.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _feedback(record: dict[str, Any]) -> dict[str, Any]:
    response = record.get("feedback") or {}
    return response.get("feedback", {}).get("outcome", {}) or {}


def _scenario_summary(records: list[dict[str, Any]], scenario: str) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    for record in records:
        item = record.get(scenario) or {}
        baseline = item.get("baseline") or {}
        after = item.get("after") or {}
        outcome = _feedback(item)
        # The HTTP workload response exposes observations at its top level;
        # feedback rows retain the same fields inside the persisted outcome.
        before_obs = baseline.get("observations") or baseline
        after_obs = after.get("observations") or after
        observations.append(
            {
                "run_index": record.get("repeat_index"),
                "baseline_run_id": baseline.get("run_id"),
                "after_run_id": after.get("run_id"),
                "baseline_median_ms": before_obs.get("median_query_time_ms"),
                "after_median_ms": after_obs.get("median_query_time_ms"),
                "baseline_p95_ms": before_obs.get("p95_query_time_ms"),
                "after_p95_ms": after_obs.get("p95_query_time_ms"),
                "median_improvement_pct": outcome.get("median_improvement_pct"),
                "p95_improvement_pct": outcome.get("p95_improvement_pct"),
                "observed_effect": outcome.get("observed_effect"),
                "effective": outcome.get("effective"),
                "plan_change": outcome.get("plan_change"),
                "rollback_status": (item.get("rollback") or {}).get("status"),
                "rollback_verification_passed": (
                    item.get("rollback_verification") or {}
                ).get("passed"),
                "error": item.get("note"),
            }
        )

    def values(key: str) -> list[float]:
        return [
            number
            for number in (_number(row.get(key)) for row in observations)
            if number is not None
        ]

    def aggregate(key: str) -> dict[str, float | int | None]:
        numbers = values(key)
        if not numbers:
            return {"count": 0, "mean": None, "min": None, "max": None}
        return {
            "count": len(numbers),
            "mean": sum(numbers) / len(numbers),
            "min": min(numbers),
            "max": max(numbers),
        }

    return {
        "scenario": scenario,
        "repetitions": len(observations),
        "effective_count": sum(row.get("effective") is True for row in observations),
        "rollback_completed_count": sum(
            row.get("rollback_status") == "rolled_back" for row in observations
        ),
        "rollback_verification_passed_count": sum(
            row.get("rollback_verification_passed") is True for row in observations
        ),
        "median_improvement_pct": aggregate("median_improvement_pct"),
        "p95_improvement_pct": aggregate("p95_improvement_pct"),
        "observations": observations,
    }


def _run_once(
    script: Path,
    base_url: str,
    repetitions: int,
    warmups: int,
    customer_id: int | None,
    baseline_work_mem: str | None,
    baseline_parallel_workers: int | None,
    output: Path,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(script),
        "--base-url",
        base_url,
        "--repetitions",
        str(repetitions),
        "--warmups",
        str(warmups),
        "--output",
        str(output),
    ]
    if customer_id is not None:
        command.extend(["--customer-id", str(customer_id)])
    if baseline_work_mem is not None:
        command.extend(["--baseline-work-mem", baseline_work_mem])
    if baseline_parallel_workers is not None:
        command.extend(["--baseline-parallel-workers", str(baseline_parallel_workers)])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if output.exists():
        try:
            loaded = json.loads(output.read_text())
            record = loaded if isinstance(loaded, dict) else {}
            if not isinstance(loaded, dict):
                record["error"] = "experiment output JSON was not an object"
        except json.JSONDecodeError as exc:
            record = {"error": f"experiment output was not valid JSON: {exc}"}
    else:
        record = {}
    if completed.returncode != 0:
        record.update(
            {
                "protocol_status": "failed",
                "error": record.get("error")
                or (
                    "experiment repetition failed with exit code "
                    f"{completed.returncode}"
                ),
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        )
    else:
        record["protocol_status"] = "completed"
    return record


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", default="experiment-repeated-results.json")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--repetitions", type=int, default=7)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--customer-id", type=int, default=None)
    parser.add_argument("--baseline-work-mem", default=None)
    parser.add_argument("--baseline-parallel-workers", type=int, default=None)
    args = parser.parse_args()
    if not 1 <= args.runs <= 20:
        parser.error("--runs must be between 1 and 20")
    if not 1 <= args.repetitions <= 50:
        parser.error("--repetitions must be between 1 and 50")
    if not 0 <= args.warmups <= 10:
        parser.error("--warmups must be between 0 and 10")

    records: list[dict[str, Any]] = []
    script = Path(__file__).with_name("run_experiment.py")
    with tempfile.TemporaryDirectory(prefix="keeninsight-repeat-") as temporary:
        temporary_dir = Path(temporary)
        for repeat_index in range(1, args.runs + 1):
            raw_output = temporary_dir / f"run-{repeat_index}.json"
            record = _run_once(
                script,
                args.base_url,
                args.repetitions,
                args.warmups,
                args.customer_id,
                args.baseline_work_mem,
                args.baseline_parallel_workers,
                raw_output,
            )
            record["repeat_index"] = repeat_index
            records.append(record)
            if record.get("protocol_status") == "completed":
                print(f"completed experiment repetition {repeat_index}/{args.runs}")
            else:
                print(
                    f"failed experiment repetition {repeat_index}/{args.runs}: "
                    f"{record.get('error', 'unknown error')}"
                )

    result = {
        "protocol": {
            "complete_protocol_repetitions": args.runs,
            "timed_repetitions_per_phase": args.repetitions,
            "warmup_repetitions_per_phase": args.warmups,
            "customer_id": args.customer_id,
            "baseline_work_mem": args.baseline_work_mem,
            "baseline_parallel_workers": args.baseline_parallel_workers,
        },
        "summary": {
            "missing_index": _scenario_summary(records, "missing_index"),
            "hash_join_spill": _scenario_summary(records, "hash_join_spill"),
            "aggregate_spill": _scenario_summary(records, "aggregate_spill"),
            "sort_spill": _scenario_summary(records, "sort_spill"),
            "parallel_query_underprovisioned": _scenario_summary(
                records, "parallel_query_underprovisioned"
            ),
        },
        "failures": [
            {
                "repeat_index": record.get("repeat_index"),
                "error": record.get("error"),
                "stderr": record.get("stderr"),
            }
            for record in records
            if record.get("protocol_status") != "completed"
        ],
        "runs": records,
    }
    target = Path(args.output)
    target.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n")
    print(f"wrote repeated measured observations to {target}")


if __name__ == "__main__":
    main()
