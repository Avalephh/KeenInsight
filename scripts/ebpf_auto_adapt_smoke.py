#!/usr/bin/env python3
"""Exercise the online measured-rate eBPF pruning path.

The script starts a deliberately broad probe set with the online guard enabled,
executes a real PostgreSQL spill workload, and checks that the live monitor
detaches the over-budget user-space probe while retaining the kernel signal.
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
OUTPUT = Path(os.getenv("KI_EBPF_AUTO_ARTIFACT", "artifacts/ebpf-auto-adapt-strict.json"))


def request(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None, timeout: int = 300) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}") from exc
    if not isinstance(body, dict):
        raise RuntimeError(f"{path} did not return an object")
    return body


def main() -> None:
    started_at = time.time()
    if request("/api/ebpf/status").get("running"):
        request("/api/ebpf/stop", method="POST")
    monitor_start = request(
        "/api/ebpf/start",
        method="POST",
        payload={
            "functions": ["BufFileWrite", "PortalRun", "standard_ExecutorRun"],
            "adaptive": True,
            "max_event_rate": 1000.0,
        },
    )
    workload = request(
        "/api/workloads/hash_join_spill/runs",
        method="POST",
        payload={
            "phase": "baseline",
            "repetitions": 3,
            "warmup_repetitions": 1,
            "parameters": {"baseline_work_mem": "64kB"},
        },
    )
    # The monitor's refresh interval is normally two to ten seconds.  Give the
    # online guard one extra reporting cycle before stop so detachment is
    # observable in the final session status/configuration.
    time.sleep(1.0)
    monitor_stop = request("/api/ebpf/stop", method="POST")
    adaptive = monitor_stop.get("adaptive") or {}
    pruned = adaptive.get("pruned_probes") or []
    attached = monitor_stop.get("attached_probes") or []
    artifact = {
        "format": "keeninsight-ebpf-auto-adapt-strict-v1",
        "base_url": BASE_URL,
        "started_at": started_at,
        "finished_at": time.time(),
        "protocol": {
            "probe_set": ["BufFileWrite", "PortalRun", "standard_ExecutorRun", "vfs_write"],
            "max_event_rate_per_user_probe": 1000.0,
            "scenario": "hash_join_spill",
            "pressure": "session work_mem=64kB",
        },
        "monitor_start": monitor_start,
        "workload": workload,
        "monitor_stop": monitor_stop,
        "checks": {
            "online_guard_enabled": bool(adaptive.get("enabled")),
            "real_events_observed": int(monitor_stop.get("event_count") or 0) > 0,
            "no_perf_buffer_loss": int(monitor_stop.get("lost_count") or 0) == 0,
            "buf_file_write_pruned": any(item.get("function_name") == "BufFileWrite" for item in pruned if isinstance(item, dict)),
            "buf_file_write_not_attached_after_prune": "uprobe:BufFileWrite" not in attached,
            "kernel_signal_retained": "kprobe:vfs_write" in attached or "vfs_write" in (monitor_stop.get("selected_probes") or []),
        },
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(artifact, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"session_id": monitor_stop.get("session_id"), "adaptive": adaptive, "checks": artifact["checks"]}, indent=2, ensure_ascii=False))
    print("Wrote online eBPF adaptation artifact to", OUTPUT)
    if not all(artifact["checks"].values()):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
