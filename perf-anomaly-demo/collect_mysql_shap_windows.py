#!/usr/bin/env python3
"""Collect real MySQL TPS/perf pairs for the six demo pressure scenarios.

Each sample measures a normal control workload first, then keeps that control
workload running while the scenario's external workload is applied.  The perf
capture covers the pressure window on the MySQL server process, and the target
is the measured normal-control TPS retention ratio.  Nothing is tuned and no
synthetic features or labels are produced.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from mysql_database import MySQLDatabaseClient, _password
from mysql_lab_controller import MYSQL_SCENARIOS
from mysql_live_chain import _capture_perf, _source_environment
from mysql_workload import MySQLLabWorkload, WorkloadSpec
from sysinsight_shap import sha


SCHEMA = "keeninsight.mysql-shap-window.v1"


def _mean(values):
    return sum(values) / len(values) if values else 0.0


def collect(args):
    destination = Path(args.output).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    scenarios = [case for case in MYSQL_SCENARIOS if case.get("focus")]
    if args.scenarios:
        wanted = set(args.scenarios.split(","))
        scenarios = [case for case in scenarios if case["scenario_id"] in wanted]
    if not scenarios:
        raise SystemExit("no MySQL focus scenarios selected")

    db = MySQLDatabaseClient(args.db, args.db_user, args.host, args.port, "root", timeout=30.0)
    server_pid = db._server_pid()
    if not server_pid:
        raise SystemExit("could not identify the mysqld process for perf capture")
    environment = _source_environment()
    report = {
        "schemaVersion": SCHEMA,
        "startedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "scope": "Six MySQL focus scenarios; paired real control TPS windows and server perf profiles; no tuning",
        "rounds": args.rounds,
        "serverPid": server_pid,
        "rows": [],
        "failures": [],
    }

    def save():
        (destination / "audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    save()
    for repeat in range(args.rounds):
        ordered = scenarios[repeat % len(scenarios):] + scenarios[:repeat % len(scenarios)]
        for case in ordered:
            rowdir = destination / "round-{}-{}".format(repeat + 1, case["scenario_id"])
            rowdir.mkdir(parents=True, exist_ok=True)
            row = {
                "schemaVersion": SCHEMA,
                "scenarioId": case["scenario_id"],
                "scenario": case["scenario"],
                "title": case["title"],
                "round": repeat + 1,
                "baselineClients": args.baseline_clients,
                "pressureClients": case["clients"],
                "baselineTargetTps": args.baseline_target_tps,
                "pressureTargetTps": case["pressure_target_tps"],
                "directory": str(rowdir),
                "startedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
            print("START round={} scenario={}".format(repeat + 1, case["scenario_id"]), flush=True)
            workload = MySQLLabWorkload(
                args.host, args.port, args.db_user, _password(), database=args.db,
                timeout=float(args.timeout),
            )
            try:
                workload.start([WorkloadSpec("control", "normal", args.baseline_clients,
                                             args.baseline_target_tps, {})])
                time.sleep(4.0)
                baseline_samples = []
                for _ in range(6):
                    time.sleep(1.0)
                    sample = workload.sample()
                    baseline_samples.append({
                        "tps": float(sample["business"].get("tps_live") or 0.0),
                        "intervalSeconds": float(sample.get("elapsed_seconds") or 1.0),
                    })
                baseline_tps = _mean([item["tps"] for item in baseline_samples])
                if baseline_tps <= 0:
                    raise ValueError("baseline control TPS was not measurable")
                workload.stop()

                workload.start([
                    WorkloadSpec("control", "normal", args.baseline_clients,
                                 args.baseline_target_tps, {}),
                    WorkloadSpec("external", case["scenario"], case["clients"],
                                 case["pressure_target_tps"], {}),
                ])
                time.sleep(3.0)
                phase_dir = rowdir / "anomaly"
                holder = {}

                def capture():
                    holder["value"] = _capture_perf(
                        phase_dir, [server_pid], args.perf_seconds, args.perf_frequency, "", environment,
                    )

                thread = threading.Thread(target=capture, daemon=True)
                capture_started = time.monotonic()
                thread.start()
                pressure_samples = []
                for _ in range(int(args.perf_seconds) + 3):
                    time.sleep(1.0)
                    elapsed = time.monotonic() - capture_started
                    sample = workload.sample()
                    pressure_samples.append({
                        "elapsedSeconds": elapsed,
                        "businessTps": float(sample["business"].get("tps_live") or 0.0),
                        "externalTps": float(sample["pressure"].get("tps_live") or 0.0),
                        "intervalSeconds": float(sample.get("elapsed_seconds") or 1.0),
                    })
                thread.join(timeout=30.0)
                workload.stop()
                capture_result = holder.get("value") or {}
                if (capture_result.get("perf") or {}).get("status") != "completed":
                    raise ValueError("perf capture did not complete")
                if (capture_result.get("postprocess") or {}).get("status") != "sysinsight_counts_generated":
                    raise ValueError("perf postprocess did not produce a complete counts profile")
                complete = [
                    item for item in pressure_samples
                    if 1.0 <= item["elapsedSeconds"] <= float(args.perf_seconds)
                ]
                if len(complete) < 5:
                    raise ValueError("not enough samples inside the perf window")
                weight = sum(item["intervalSeconds"] for item in complete)
                pressure_tps = sum(item["businessTps"] * item["intervalSeconds"] for item in complete) / weight
                external_tps = sum(item["externalTps"] * item["intervalSeconds"] for item in complete) / weight
                if pressure_tps <= 0:
                    raise ValueError("pressure-window control TPS was not measurable")
                counts_path = Path(capture_result["postprocess"]["counts_path"])
                row.update({
                    "status": "completed",
                    "finishedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "baselineTps": round(baseline_tps, 3),
                    "pressureTps": round(pressure_tps, 3),
                    "externalPressureTps": round(external_tps, 3),
                    "target": pressure_tps / baseline_tps,
                    "baselineSamples": baseline_samples,
                    "pressureSamples": pressure_samples,
                    "countsPath": str(counts_path.resolve()),
                    "countsSha256": sha(counts_path),
                    "capture": capture_result,
                })
                (rowdir / "measurement.json").write_text(
                    json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
                report["rows"].append(row)
                print("DONE round={} scenario={} baseline={:.1f} pressure={:.1f} retained={:.1%}".format(
                    repeat + 1, case["scenario_id"], baseline_tps, pressure_tps, row["target"]), flush=True)
            except Exception as exc:
                row.update(status="failed", error="{}: {}".format(type(exc).__name__, exc))
                report["failures"].append(row)
                (rowdir / "measurement.json").write_text(
                    json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
                print("FAILED", row["error"], flush=True)
            finally:
                workload.stop()
                save()
    report["finishedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
    save()
    print("DATASET", str(destination / "audit.json"), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", default=str(ROOT / "results" / "mysql_shap_windows"))
    parser.add_argument("--scenarios", default="", help="comma-separated scenario IDs")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--db", default="keeninsight_tpcc")
    parser.add_argument("--db-user", default="keeninsight")
    parser.add_argument("--baseline-clients", type=int, default=2)
    parser.add_argument("--baseline-target-tps", type=float, default=500.0)
    parser.add_argument("--perf-seconds", type=int, default=8)
    parser.add_argument("--perf-frequency", type=int, default=300)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 4:
        parser.error("rounds must be between 1 and 4")
    collect(args)


if __name__ == "__main__":
    main()
