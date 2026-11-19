#!/usr/bin/env python3
"""Measure high-frequency probe overhead and adaptive probe pruning.

The script runs the same PostgreSQL hash-spill workload in three measured
arms: no eBPF probe, an intentionally high-frequency ``BufFileWrite`` probe,
and the Lasso-plus-event-rate-budget selection.  The adaptive arm is not
allowed to use the high-frequency probe when its persisted rate exceeds the
configured budget.  All timings and event counts are returned by the live
service and written verbatim to the artifact.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


BASE_URL = os.getenv("KI_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
OUTPUT = Path(os.getenv("KI_EBPF_OVERHEAD_ARTIFACT", "artifacts/ebpf-overhead-strict.json"))


def request(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None, timeout: int = 300) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
    if not isinstance(body, dict):
        raise RuntimeError(f"{path} did not return a JSON object")
    return body


def workload() -> dict[str, Any]:
    return request(
        "/api/workloads/hash_join_spill/runs",
        method="POST",
        payload={
            "phase": "baseline",
            "repetitions": 7,
            "warmup_repetitions": 2,
            "parameters": {"baseline_work_mem": "64kB"},
        },
    )


def monitored(functions: list[str], *, repetitions: int = 7, warmups: int = 2) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    start = request(
        "/api/ebpf/start",
        method="POST",
        payload={"functions": functions, "target_pids": []},
    )
    run = request(
        "/api/workloads/hash_join_spill/runs",
        method="POST",
        payload={
            "phase": "baseline",
            "repetitions": repetitions,
            "warmup_repetitions": warmups,
            "parameters": {"baseline_work_mem": "64kB"},
        },
    )
    stop = request("/api/ebpf/stop", method="POST")
    return start, run, stop


def pct(after: Any, before: Any) -> float | None:
    try:
        return 100.0 * (float(after) - float(before)) / max(1e-9, float(before))
    except (TypeError, ValueError):
        return None


def main() -> None:
    started_at = time.time()
    if request("/api/ebpf/status").get("running"):
        request("/api/ebpf/stop", method="POST")
    clean = workload()
    high_start, high_run, high_stop = monitored(["BufFileWrite", "PortalRun"], repetitions=3, warmups=1)
    adaptive = request(
        "/api/ebpf/adapt",
        method="POST",
        payload={
            "min_sessions": 4,
            "max_probes": 8,
            "alpha": 0.001,
            "max_event_rate": 1000.0,
        },
    )
    adaptive_run = workload()
    adaptive_stop = request("/api/ebpf/stop", method="POST")

    clean_median = clean.get("median_query_time_ms")
    clean_p95 = clean.get("p95_query_time_ms")
    artifact = {
        "format": "keeninsight-ebpf-overhead-strict-v1",
        "base_url": BASE_URL,
        "started_at": started_at,
        "finished_at": time.time(),
        "protocol": {
            "scenario": "hash_join_spill",
            "pressure": "session work_mem=64kB",
            "clean_repetitions": 7,
            "clean_warmups": 2,
            "high_frequency_repetitions": 3,
            "adaptive_repetitions": 7,
            "adaptive_max_event_rate_per_user_probe": 1000.0,
        },
        "clean": clean,
        "high_frequency": {
            "start": high_start,
            "run": high_run,
            "stop": high_stop,
        },
        "adaptive": {
            "selection": adaptive,
            "run": adaptive_run,
            "stop": adaptive_stop,
        },
        "comparison": {
            "high_frequency_median_overhead_pct": pct(high_run.get("median_query_time_ms"), clean_median),
            "high_frequency_p95_overhead_pct": pct(high_run.get("p95_query_time_ms"), clean_p95),
            "adaptive_median_overhead_pct": pct(adaptive_run.get("median_query_time_ms"), clean_median),
            "adaptive_p95_overhead_pct": pct(adaptive_run.get("p95_query_time_ms"), clean_p95),
        },
        "checks": {
            "high_frequency_observed_real_events": int(high_stop.get("event_count") or 0) > 0,
            "high_frequency_no_loss": int(high_stop.get("lost_count") or 0) == 0,
            "adaptive_rejected_buf_file_write": "BufFileWrite" in (adaptive.get("selection_budget") or {}).get("budget_rejected_probes", []),
            "adaptive_not_attached_buf_file_write": "BufFileWrite" not in (adaptive.get("selected_probes") or []),
            "adaptive_no_loss": int(adaptive_stop.get("lost_count") or 0) == 0,
            "same_result_clean_adaptive": clean.get("result") == adaptive_run.get("result"),
            "same_plan_signal_clean_adaptive": (clean.get("plan_summary") or {}).get("join_nodes") == (adaptive_run.get("plan_summary") or {}).get("join_nodes") and (clean.get("plan_summary") or {}).get("has_temp_blocks") == (adaptive_run.get("plan_summary") or {}).get("has_temp_blocks"),
        },
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(artifact, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"comparison": artifact["comparison"], "checks": artifact["checks"]}, indent=2, ensure_ascii=False))
    print("clean run", clean.get("run_id"), "high run", high_run.get("run_id"), "adaptive run", adaptive_run.get("run_id"))
    print("Wrote eBPF overhead artifact to", OUTPUT)
    if not all(artifact["checks"].values()):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
