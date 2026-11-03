"""Audit and bounded collection smoke for the ORIGINAL six TP console cases.

No new model call, candidate replay, production DB or tuning-gain claim.
The lab executes the original workload files on a disposable five-warehouse
fixture. CPU diagnosis uses short eBPF task-clock samples, not hot-loop probes.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time

from .catalog import BY_NAME, choose_probes
from .collector import atomic_json, inspect_server
from .cpu_sample import sample_window
from .lab import cluster
from .overhead import LibpqSession
from .runtime import CollectorProcess

ROOT = Path(__file__).resolve().parents[2]
PLANS = {
    "tp_payment_hot": ("lock_and_commit", "diagnostic-lock"),
    "tp_stock_level_hot": ("read_CPU_or_IO", None),
    "tp_stock_level_burst": ("read_CPU_or_IO", None),
    "tp_full_mix_surge_high": ("mixed_CPU_wait_WAL", "diagnostic-lock"),
    "tp_wal_checkpoint": ("WAL_and_checkpoint", "diagnostic-wal"),
    "tp_order_status_hot": ("read_CPU_or_IO", None),
}


def original_cases(source=None):
    source = Path(source or ROOT / "app/tpcc_transaction_cases.py")
    values = {}
    for node in ast.parse(source.read_text()).body:
        if (isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id in {"FOCUS_SCENARIO_IDS", "CASE_DEFINITIONS"}):
            values[node.targets[0].id] = ast.literal_eval(node.value)
    focus = values["FOCUS_SCENARIO_IDS"]
    if len(focus) != 6 or set(focus) != set(PLANS):
        raise ValueError("original focus catalog changed; review six-scenario plans explicitly")
    by_id = {case["id"]: case for case in values["CASE_DEFINITIONS"]}
    return [by_id[name] for name in focus]


def association_matches(functions, association):
    if not association:
        return {"status": "unavailable", "reason": "explicit source association asset not supplied"}
    records = json.loads(Path(association).read_text())
    names = set(functions)
    return {"status": "completed", "matches": [
        {"knob": record["knob_name"],
         "functions": sorted(names & set(record.get("data_flow_functions", [])
                                        + record.get("control_flow_functions", [])))}
        for record in records if names & set(record.get("data_flow_functions", [])
                                             + record.get("control_flow_functions", []))],
        "semantics": "static identifier association, not a proven cause or performance direction"}


def audit(server):
    cases = []
    for case in original_cases():
        family, detail = PLANS[case["id"]]
        sql = ROOT / "app/tpcc_cases" / case["sql"]
        probes = choose_probes(detail)[0] if detail else []
        cases.append({
            "id": case["id"], "title": case["title"], "original_sql": str(sql.relative_to(ROOT)),
            "original_sql_sha256": hashlib.sha256(sql.read_bytes()).hexdigest(),
            "old_pressure_evidence": case["pressure_evidence"],
            "diagnosis_family": family, "resident_profile": "six-resident",
            "bounded_profile": detail, "bounded_symbols": [BY_NAME[n].symbol for n in probes],
            "bounded_CPU_sampler": family in {"read_CPU_or_IO", "mixed_CPU_wait_WAL"},
            "available_function_probes": all(server["candidate_symbols"][n] for n in probes),
            "automatic_trigger_limit": "lock/spill/checkpoint only; CPU-only saturation needs explicit "
                                       "selection or a separately validated low-rate sentinel",
            "current_tuning_acceptance": "not_migrated_to_six_case_business_TPS_controller",
        })
    return {"schema": "keeninsight.ebpf-six-audit.v1", "cases": cases,
            "resident_symbols": [BY_NAME[n].symbol for n in choose_probes("six-resident")[0]],
            "resident_not_attached": ["ReadBufferExtended", "ExecScan", "MemoryContextAlloc",
                                      "LockAcquireExtended", "XLogFlush", "sched_switch"],
            "required_before_equal_effect_claim": [
                "same original data/pressure and target-business TPS objective",
                "fresh model candidates from eBPF evidence, not old candidate replay",
                "same durability, restart and memory safety policy on both sides",
                "source matching and uncertainty for CPU/wait samples",
                "multi-backend and background-checkpointer identity coverage",
                "repeat diagnosis/tuning/apply/retest/restore for every original case",
            ]}


def process_ids(session, application):
    return json.loads(session.execute(
        "SELECT coalesce(json_agg(pid),'[]') FROM pg_stat_activity "
        "WHERE application_name='" + application + "'", value=True))


def counts_summary(directory):
    # Each backend snapshot is cumulative; never sum overlapping snapshots.
    final = {}
    for line in (directory / "events.jsonl").read_text().splitlines():
        event = json.loads(line)
        if event.get("type") not in {"backend_snapshot", "backend_end", "backend_final"}:
            continue
        for name, metric in event.get("metrics", {}).items():
            key = (event["pid"], event["process_start_ticks"], name)
            if metric.get("calls", 0) >= final.get(key, {}).get("calls", 0):
                final[key] = metric
    result = {}
    for (_, _, name), metric in final.items():
        row = result.setdefault(name, {"calls": 0})
        row["calls"] += metric["calls"]
        if "inclusive_duration_ns" in metric:
            row["inclusive_duration_ns"] = row.get("inclusive_duration_ns", 0) + metric["inclusive_duration_ns"]
            row["maximum_duration_ns"] = max(row.get("maximum_duration_ns", 0),
                                             metric["maximum_duration_ns"])
    return result


def start_worker(server, connection, case, directory, role):
    sql = ROOT / "app/tpcc_cases" / (case["sql"] if role == "pressure" else case["normal_sql"])
    application = "ebpf-six-" + role + "-" + case["id"]
    clients = int(case["clients"]) if role == "pressure" else 2
    args = [str(Path(server["binary"]).parent / "pgbench"), "-n", "-M", "simple",
            "-h", connection[connection.index("-h") + 1],
            "-p", connection[connection.index("-p") + 1], "-U", "postgres",
            "-f", str(sql), "-c", str(clients), "-j", "2",
            "-R", "500" if role == "pressure" else "200", "-T", "12", "postgres"]
    log = (directory / (role + ".log")).open("w")
    try:
        process = subprocess.Popen(args, env={**os.environ, "PGAPPNAME": application},
                                   stdout=log, stderr=subprocess.STDOUT)
    except BaseException:
        log.close()
        raise
    return {"process": process, "log": log, "application": application,
            "clients": clients, "offered_tps": 500 if role == "pressure" else 200}


def stop_worker(worker):
    process = worker["process"]
    try:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    finally:
        worker["log"].close()


def run_case(case, root, server, connection, admin, association):
    directory = root / case["id"]
    directory.mkdir(mode=0o700)
    workers = []
    resident = detail = None
    result = {"id": case["id"], "status": "running", "model_called": False,
              "candidate_applied": False, "tuning_gain_evaluated": False,
              "trigger": "operator_selected_original_scenario",
              "purpose": "collection feasibility smoke, NOT tuning/TPS/overhead acceptance"}
    try:
        workers = []
        for role in ("business", "pressure"):
            workers.append(start_worker(server, connection, case, directory, role))
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            ids = [process_ids(admin, worker["application"]) for worker in workers]
            if all(len(pids) == worker["clients"] for pids, worker in zip(ids, workers)):
                break
            time.sleep(.05)
        else:
            raise RuntimeError("not all original workload backends connected")
        checkpointer = int(admin.execute(
            "SELECT pid FROM pg_stat_activity WHERE backend_type='checkpointer'", value=True))
        result["target_roles"] = {"business": ids[0], "pressure": ids[1], "checkpointer": checkpointer}
        targets = sorted(set(ids[0] + ids[1] + [checkpointer]))
        resident = CollectorProcess(directory / "resident", targets, server["pg_config"],
                                    "system", "six-resident", 15, report_seconds=1)
        time.sleep(.25)
        family, profile = PLANS[case["id"]]
        if family in {"read_CPU_or_IO", "mixed_CPU_wait_WAL"}:
            cpu = sample_window([ids[1][0]], server["binary"], seconds=2, frequency=49)
            atomic_json(directory / "cpu_samples.json", cpu)
            result["cpu"] = {k: cpu[k] for k in ("sample_count", "health", "active_seconds", "status")}
            result["sampled_function_associations"] = association_matches(
                [row["function"] for row in cpu["functions"]], association)
        if profile:
            # Checkpointer must be included: observing client backends alone
            # cannot measure background checkpoints.
            detail_targets = [ids[1][0], checkpointer] if profile == "diagnostic-wal" else [ids[1][0]]
            detail = CollectorProcess(directory / "detail", detail_targets, server["pg_config"],
                                      "system", profile, 2, report_seconds=.2, metric_mode="timed")
            time.sleep(1)
            result["detail_manifest"] = detail.close()
            result["detail_events"] = counts_summary(directory / "detail")
        result["resident_manifest"] = resident.close()
        result["resident_events"] = counts_summary(directory / "resident")
        for worker in workers:
            worker["process"].wait(timeout=18)
            if worker["process"].returncode:
                raise RuntimeError("original workload failed; inspect " + worker["log"].name)
        healthy = all(not m.get("perf_lost_notifications") and not any(m.get("bpf_health", {}).values())
                      for m in [result["resident_manifest"]] + (
                          [result["detail_manifest"]] if profile else []))
        result.update(status="collection_smoke_completed" if healthy else "collection_health_failed",
                      original_sql_unchanged=True, workers_completed=True, diagnosis_family=family,
                      diagnostic_sampler_stopped=True, all_collectors_stopped=True)
    except Exception as exc:
        result.update(status="failed", error=type(exc).__name__ + ": " + str(exc))
    finally:
        for child in (detail, resident):
            if child:
                try:
                    child.close(check=False)
                except Exception as exc:
                    result.update(status="failed", cleanup_error=str(exc))
        for worker in workers:
            try:
                stop_worker(worker)
            except Exception as exc:
                result.update(status="failed", cleanup_error=str(exc))
        atomic_json(directory / "result.json", result)
    return result


def controls(options, root):
    """Probe liveness controls, NOT reproductions of an original TP incident."""
    with cluster(options.pg_config) as (_, connection):
        admin, holder, waiter = [LibpqSession(connection) for _ in range(3)]
        child = worker = None
        errors = []
        try:
            admin.execute("CREATE TABLE probe_control(id int PRIMARY KEY, value int)")
            admin.execute("INSERT INTO probe_control VALUES(1,0)")
            holder.execute("BEGIN; UPDATE probe_control SET value=1 WHERE id=1")
            checkpointer = int(admin.execute(
                "SELECT pid FROM pg_stat_activity WHERE backend_type='checkpointer'", value=True))
            child = CollectorProcess(root / "control", [waiter.pid, checkpointer], options.pg_config,
                                     "system", "six-resident", 5, report_seconds=.2, metric_mode="timed")

            def blocked_update():
                try:
                    waiter.execute("BEGIN; SET LOCAL statement_timeout='2s'; "
                                   "UPDATE probe_control SET value=2 WHERE id=1; ROLLBACK")
                except Exception as exc:
                    errors.append(str(exc))

            worker = threading.Thread(target=blocked_update)
            worker.start()
            time.sleep(.2)
            holder.execute("ROLLBACK")
            worker.join(timeout=3)
            if worker.is_alive() or errors:
                raise RuntimeError("lock control failed: " + "; ".join(errors))
            admin.execute("CHECKPOINT")
            manifest = child.close()
            observed = counts_summary(root / "control")
            checks = {
                "actual_lock_wait_observed": observed.get("lock_wait", {}).get("calls", 0) >= 1,
                "lock_wait_duration_over_100ms": observed.get("lock_wait", {}).get(
                    "maximum_duration_ns", 0) >= 100_000_000,
                "checkpointer_not_client_checkpoint_observed": observed.get(
                    "checkpoint", {}).get("calls", 0) >= 1,
                "collector_completed_and_stopped": manifest.get("status") == "completed",
                "no_reported_collection_errors": not manifest.get("perf_lost_notifications")
                    and not any(manifest.get("bpf_health", {}).values()),
            }
            result = {"status": "passed" if all(checks.values()) else "failed",
                      "checks": checks, "observed": observed,
                      "scope": "manual liveness controls in disposable DB, not original-case diagnosis"}
            atomic_json(root / "controls.json", result)
            return result
        finally:
            holder.execute("ROLLBACK")
            if worker:
                worker.join(timeout=3)
            if child:
                child.close(check=False)
            for session in (waiter, holder, admin):
                session.close()


def lab(options, root):
    if shutil.disk_usage(root).free < 3 * 1024 ** 3:
        raise ValueError("reserve at least 3GiB for the disposable full five-warehouse fixture")
    with cluster(options.pg_config, max_connections=20) as (server, connection):
        server["pg_config"] = options.pg_config
        with (root / "fixture.log").open("w") as log:
            subprocess.run(connection + ["--single-transaction", "-v", "warehouses=5", "-v", "seed=1",
                           "-f", str(ROOT / "scripts/tpcc_schema.sql")], stdout=log,
                           stderr=subprocess.STDOUT, timeout=180, check=True)
        admin = LibpqSession(connection)
        try:
            rows = [run_case(case, root, server, connection, admin, options.association)
                    for case in original_cases() if not options.scenario or case["id"] == options.scenario]
        finally:
            admin.close()
    result = {"schema": "keeninsight.ebpf-six-smoke.v1", "cases": rows,
              "status": "collection_smoke_completed" if all(
                  row["status"] == "collection_smoke_completed" for row in rows) else "failed",
              "fixture": "original deterministic five-warehouse SQL; not certified TPC-C",
              "original_workload_files_used": True, "model_calls": 0, "tuning_acceptance": False,
              "old_gain_reproduced": False, "production_services_changed": False,
              "pressure_policy": "bounded 500 offered TPS + 200 business TPS, not old full pressure",
              "warning": "symbols/samples/waits are not proof of equal diagnosis quality, low DB overhead "
                         "or original six-case business-TPS recovery"}
    atomic_json(root / "summary.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("audit", "lab", "controls"))
    parser.add_argument("--pg-config", default="/usr/lib/postgresql/12/bin/pg_config")
    parser.add_argument("--output", required=True)
    parser.add_argument("--association", help="optional original function/parameter association JSON")
    parser.add_argument("--scenario", choices=tuple(PLANS))
    options = parser.parse_args()
    root = Path(options.output).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = audit(inspect_server(options.pg_config))
    atomic_json(root / "audit.json", report)
    if options.command == "audit":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif options.command == "lab":
        result = lab(options, root)
        print(json.dumps({"status": result["status"], "output": str(root),
                          "tuning_acceptance": False}, indent=2))
        raise SystemExit(0 if result["status"] == "collection_smoke_completed" else 1)
    else:
        result = controls(options, root)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
