#!/usr/bin/env python3
"""Run the six focus TPCC scenarios and audit the resulting alert display."""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
FOCUS_SCENARIOS = [
    "tp_payment_hot",
    "tp_stock_level_hot",
    "tp_stock_level_burst",
    "tp_full_mix_surge_high",
    "tp_wal_checkpoint",
    "tp_order_status_hot",
]
POLL_SECONDS = 10


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def log(message: str) -> None:
    print("[{}] {}".format(dt.datetime.now().strftime("%H:%M:%S"), message), flush=True)


def request(base: str, path: str, payload=None, timeout: float = 60.0):
    url = base.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read(3000).decode("utf-8", errors="replace")
        try:
            parsed = json.loads(detail)
        except ValueError:
            parsed = {"error": detail}
        return exc.code, parsed


def wait_idle(base: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, data = request(base, "/api/v1/lab/status")
        if status == 200 and not data.get("current"):
            return True
        log("waiting for the previous lab run to finish")
        time.sleep(POLL_SECONDS)
    return False


def wait_run(base: str, run_id: str, timeout: float):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, data = request(base, "/api/v1/lab/runs/" + urllib.parse.quote(run_id))
        if status == 200 and data.get("status") not in {"queued", "running"}:
            return data
        log("  lab run {} is {}".format(run_id, data.get("status") if status == 200 else status))
        time.sleep(POLL_SECONDS)
    return {"run_id": run_id, "status": "timeout"}


def find_new_incidents(base: str, run_started: str):
    _, payload = request(base, "/api/v1/keeninsight/alerts?limit=100")
    values = []
    for item in payload.get("items") or []:
        if str(item.get("time") or "") >= run_started:
            values.append(item)
    return values


def wait_incident(base: str, incident_id: str, timeout: float):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        status, data = request(base, "/api/v1/keeninsight/alerts/" + urllib.parse.quote(incident_id))
        if status == 200:
            last = data
            if data.get("rawStatus") not in {"queued", "running"}:
                return data
        log("  incident {} is {}".format(incident_id, data.get("rawStatus") if status == 200 else status))
        time.sleep(POLL_SECONDS)
    return last


def ensure_descriptions(base: str, incident: dict):
    analysis = incident.get("detailAnalysis") or {}
    functions = analysis.get("hotFunctions") or []
    counts = {"source_comment": 0, "llm": 0, "missing": 0, "error": 0}
    errors = []
    for item in functions:
        name = item.get("Function")
        if not name:
            continue
        path = "/api/v1/keeninsight/functionDetails/" + urllib.parse.quote(str(name), safe="")
        status, detail = request(base, path, timeout=120)
        if status != 200:
            counts["error"] += 1
            errors.append({"function": name, "error": detail.get("error") or status})
            continue
        source = detail.get("descriptionSource")
        if detail.get("description"):
            counts[source if source in counts else "llm"] += 1
        else:
            counts["missing"] += 1
            errors.append({"function": name, "error": "no description"})
    return counts, errors


def audit_incident(base: str, incident: dict):
    analysis = incident.get("detailAnalysis") or {}
    profiling = (analysis.get("differentialProfiling") or {}).get("key_functions") or []
    shap = analysis.get("shapAnalysis") or {}
    comparison = analysis.get("comparison") or {}
    hot = analysis.get("hotFunctions") or []
    descriptions, errors = ensure_descriptions(base, incident)
    complete = bool(
        incident.get("rawStatus") == "completed"
        and hot
        and profiling
        and shap.get("status") == "completed"
        and (analysis.get("runtimeCallChains") or [])
        and (analysis.get("functionCodeExamples") or [])
        and descriptions["missing"] == 0
        and descriptions["error"] == 0
    )
    return {
        "incident_id": incident.get("id"),
        "status": incident.get("rawStatus"),
        "model": (incident.get("diagnosis") or {}).get("model"),
        "hot_functions": len(hot),
        "differential_functions": len(profiling),
        "shap_status": shap.get("status"),
        "runtime_chains": len(analysis.get("runtimeCallChains") or []),
        "source_examples": len(analysis.get("functionCodeExamples") or []),
        "comparison_keys": sorted(comparison.keys()),
        "descriptions": descriptions,
        "description_errors": errors[:10],
        "complete": complete,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8005")
    parser.add_argument("--scenarios", default=",".join(FOCUS_SCENARIOS))
    parser.add_argument("--baseline-seconds", type=int, default=60)
    parser.add_argument("--pressure-seconds", type=int, default=180)
    parser.add_argument("--run-timeout", type=float, default=900)
    parser.add_argument("--incident-timeout", type=float, default=1200)
    parser.add_argument("--stop-on-incomplete", action="store_true", default=True)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    scenarios = [value.strip() for value in args.scenarios.split(",") if value.strip()]
    base = args.base_url.rstrip("/")
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output = Path(args.output) if args.output else ROOT / "results" / "focus_scenario_runs" / (stamp + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"started_at": now(), "scenarios": []}

    for scenario in scenarios:
        log("scenario {}: waiting for an idle lab".format(scenario))
        if not wait_idle(base, args.run_timeout):
            report["scenarios"].append({"scenario_id": scenario, "error": "lab did not become idle"})
            break
        payload = {"scenario_id": scenario,
                   "baseline_seconds": args.baseline_seconds,
                   "pressure_seconds": args.pressure_seconds}
        status, started = request(base, "/api/v1/actions/lab/tpcc/start", payload)
        if status not in {200, 202}:
            report["scenarios"].append({"scenario_id": scenario, "error": started.get("error") or status})
            break
        run = started.get("run") or {}
        run_id = run.get("run_id")
        run_started = run.get("requested_at") or run.get("started_at") or now()
        log("scenario {}: started {} ({})".format(scenario, run_id, started.get("status")))
        run_result = wait_run(base, run_id, args.run_timeout)
        log("scenario {}: lab {} ({})".format(scenario, run_id, run_result.get("status")))

        incidents = []
        deadline = time.time() + 300
        while time.time() < deadline and not incidents:
            incidents = find_new_incidents(base, run_started)
            if not incidents:
                log("  waiting for the TPS-drop alert")
                time.sleep(POLL_SECONDS)
        audited = []
        for item in incidents:
            incident = wait_incident(base, item["id"], args.incident_timeout)
            audited.append(audit_incident(base, incident))
            log("  incident {} {} complete={}".format(
                item["id"], audited[-1].get("status"), audited[-1].get("complete")))
        entry = {
            "scenario_id": scenario,
            "run_id": run_id,
            "started_at": run_started,
            "run_status": run_result.get("status"),
            "comparison": (run_result.get("result") or {}).get("comparison"),
            "tuning": ((run_result.get("result") or {}).get("tuning") or {}).get("status"),
            "incidents": audited,
            "complete": bool(audited) and all(value.get("complete") for value in audited),
        }
        report["scenarios"].append(entry)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        if not entry["complete"] and args.stop_on_incomplete:
            log("scenario {} is incomplete; stopping for repair".format(scenario))
            break

    report["finished_at"] = now()
    report["summary"] = {
        "scenarios": len(report["scenarios"]),
        "complete": sum(1 for item in report["scenarios"] if item.get("complete")),
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report.get("summary"), ensure_ascii=False))
    print("report", output)
    return 0 if report["summary"]["complete"] == len(scenarios) else 1


if __name__ == "__main__":
    raise SystemExit(main())
