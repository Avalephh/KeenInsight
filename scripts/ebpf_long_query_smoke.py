#!/usr/bin/env python3
"""Verify immediate eBPF emission for a real long-running SQL statement."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import psycopg


BASE_URL = os.getenv("KI_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
DSN = os.getenv(
    "KI_DATABASE_URL",
    "postgresql://keeninsight:keeninsight@localhost:5432/keeninsight",
)
OUTPUT = Path(os.getenv("KI_EBPF_LONG_QUERY_ARTIFACT", "artifacts/ebpf-long-query-strict.json"))


def request(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}") from exc
    if not isinstance(body, dict):
        raise RuntimeError(f"{path} did not return a JSON object")
    return body


def main() -> None:
    started = time.time()
    if request("/api/ebpf/status").get("running"):
        request("/api/ebpf/stop", method="POST")
    # Open the client backend before the monitor starts so its initial target
    # PID snapshot includes it.  This removes a race with the monitor's
    # periodic PID refresh and is still a normal external PostgreSQL session.
    with psycopg.connect(DSN, autocommit=True, application_name="keeninsight-long-query-smoke") as conn:
        monitor = request(
            "/api/ebpf/start",
            method="POST",
            payload={
                "functions": ["PortalRun", "standard_ExecutorRun"],
                "adaptive": False,
            },
        )
        time.sleep(1.5)
        result = conn.execute("SELECT pg_sleep(3.0), 1").fetchone()
    stopped = request("/api/ebpf/stop", method="POST")
    session_id = int(stopped.get("session_id") or monitor.get("session_id"))
    timeline = request(
        "/api/ebpf/timelines?" + urllib.parse.urlencode({"session_id": session_id, "limit": 2000})
    )
    items = timeline.get("items") or []
    reasons = sorted(
        {
            str(reason)
            for item in items
            for reason in ((item.get("metadata") or {}).get("immediate_emission_reasons") or [])
        }
    )
    artifact = {
        "format": "keeninsight-ebpf-long-query-strict-v1",
        "base_url": BASE_URL,
        "started_at": started,
        "finished_at": time.time(),
        "session_id": session_id,
        "query_result": result,
        "monitor_start": monitor,
        "monitor_stop": stopped,
        "timeline": {"items": items, "immediate_emission_reasons": reasons},
        "checks": {
            "session_stopped": stopped.get("running") is False and not stopped.get("last_error"),
            "real_long_query_completed": list(result or ()) in ([None, 1], ["", 1]),
            "real_events_observed": int(stopped.get("event_count") or 0) > 0,
            "no_perf_buffer_loss": int(stopped.get("lost_count") or 0) == 0,
            "long_query_immediate_emission": "long_query" in reasons,
            "lifecycle_contains_anomaly": any(int(item.get("anomaly_count") or 0) > 0 for item in items),
        },
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(artifact, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(json.dumps(artifact["checks"], indent=2, ensure_ascii=False))
    print("session", session_id, "events", stopped.get("event_count"), "reasons", reasons)
    print("Wrote long-query eBPF artifact to", OUTPUT)
    if not all(artifact["checks"].values()):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
