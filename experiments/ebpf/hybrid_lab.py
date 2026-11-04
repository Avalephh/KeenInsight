"""Real eBPF sentinel -> original perf/PG/SQL evidence -> original TPS tuning.

Only disposable socket-only PG12 clusters. Does not connect to production
Prometheus, change existing services, use historical candidates, or claim that
resource-demand alerts prove SLO regressions. Known-healthy calibration is
explicit and bounded; normal monitoring after calibration has no perf.
"""
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

from .collector import atomic_json, inspect_server
from .equivalence import LIMITS, Workers, invariant_state, native_settings
from .equivalent_profiles import compare, normal_profile
from .gain_audit import prepare_fixture
from .hybrid_controller import HybridController
from .hybrid_perf import capture
from .hybrid_policy import RateDetector, rates
from .hybrid_sentinel import StatsSentinel, receiver_target
from .lab import cluster
from .overhead import LibpqSession
from .paired_tps import (cloned_cluster, database_snapshot, host_snapshot, make_input,
                         one_trial, query_snapshot, read, retest)
from .pipeline import Journal
from .six_scenarios import ROOT, original_cases


def attach_trigger_metadata(payload, case_result, alert):
    """Keep the original canonical alert field truthful, not operator-selected.

    The reference input helper normally has no automatic alert. Update BOTH
    its canonical field and compatibility step/capture metadata. Do not replace
    any perf, resource, global-counter or SQL measurement with sentinel values.
    """
    trigger = {"triggered": True, "source": alert["source"],
               "alert_name": "sustained_resource_or_activity_change",
               "alert": {"signals": alert["signals"], "not_claimed": alert["not_claimed"]}}
    if alert.get("observed_at_unix_ns"):
        trigger["at"] = alert["observed_at_unix_ns"] / 1e9
    case_result["anomaly"]["samples"]["trigger"] = trigger
    payload["alert"] = trigger
    payload["prometheus_capture"]["alerts"] = {"status": "completed", "selected": trigger,
                                              "transport": "eBPF; no Prometheus alert query"}
    for step in payload["steps"]:
        if step["step"] == 4:
            # Keep the original canonical compatibility name; source is eBPF.
            step.update(status="completed", data={"selected": trigger, "alert_status": "completed"})
    return trigger


def rich_capture(args, pids, directory, server, admin):
    """Exactly the reference collector and exporter-source/SQL observations."""
    directory.mkdir(mode=0o700)
    before, host_before = database_snapshot(admin), host_snapshot()
    profile = capture(directory / "profile", pids, server, args.profile_seconds, args.frequency,
                      args.stackcollapse, args.max_output_mb, args.max_pids)
    after = database_snapshot(admin)
    queries = query_snapshot(admin)
    host_after = host_snapshot()
    result = {"profile": profile, "counts": profile["counts_path"], "resource": None,
              "before": before, "after": after, "host_before": host_before,
              "host_after": host_after, "authorized_PIDs": pids, "SQL_observations": queries,
              "no_PortalRun_or_ExecutorRun_uprobes": True,
              "triggered_by": "eBPF received-native-statistics sentinel"}
    atomic_json(directory / "phase.json", result)
    return result


def baseline_profiles(args, case, directory, server, connection, admin, journal):
    profiles = []
    for index in range(2):
        phase = directory / ("normal-" + str(index))
        phase.mkdir(mode=0o700)
        workers = Workers(case, phase, server, connection, admin,
                          args.profile_seconds + 55, args.business_rate, args.pressure_rate, False)
        try:
            time.sleep(args.warmup_seconds)
            journal.stage("healthy_perf_calibration_started", index=index,
                          explanation="one-time explicit healthy reference, NOT resident collection")
            item = rich_capture(args, workers.ids["business"], phase / "capture", server, admin)
            if not workers.running():
                raise RuntimeError("healthy workload exited during perf reference")
            profiles.append(item)
            journal.stage("healthy_perf_calibration_stopped", index=index,
                          all_perf_processes_stopped=True)
        finally:
            workers.close()
    normal = directory / "normal_profile.csv"
    normal_profile([row["counts"] for row in profiles], normal)
    return normal


def next_snapshot(sentinel, seconds, workers):
    time.sleep(seconds)
    if not all(worker.running() for worker in workers if worker is not None):
        raise RuntimeError("controlled workload exited before incident completed")
    return sentinel.snapshot()


def detect_and_capture(args, case, directory, server, connection, admin, sentinel, journal, diagnose):
    """Keep the SAME business connections alive while adding original pressure."""
    healthy = pressure = None
    detector = RateDetector(args.surge_ratio, args.drop_ratio, args.consecutive_windows,
                            args.drop_sustain_seconds)
    controller = HybridController(detector, journal, args.max_incidents, args.cooldown_seconds)
    duration = math.ceil(args.warmup_seconds + (args.calibration_windows + args.guard_windows)
                         * args.window_seconds + args.trigger_timeout + args.profile_seconds + 60)
    def stop_workloads():
        nonlocal healthy, pressure
        pressure_owner, healthy_owner = pressure, healthy
        pressure, healthy = None, None
        try:
            if pressure_owner:
                pressure_owner.close()
        finally:
            if healthy_owner:
                healthy_owner.close()

    try:
        healthy_dir = directory / "resident-business"
        healthy_dir.mkdir(mode=0o700)
        healthy = Workers(case, healthy_dir, server, connection, admin, duration,
                          args.business_rate, args.pressure_rate, False)
        time.sleep(args.warmup_seconds)
        previous = sentinel.snapshot()
        windows = []
        for _ in range(args.calibration_windows):
            current = next_snapshot(sentinel, args.window_seconds, [healthy])
            windows.append(rates(previous, current))
            previous = current
        baseline = detector.fit(windows)
        atomic_json(directory / "sentinel-baseline.json", {"windows": windows, "baseline": baseline})
        journal.stage("resident_calibrated", windows=len(windows), perf_running=False)
        normal_alerts = []
        for _ in range(args.guard_windows):
            current = next_snapshot(sentinel, args.window_seconds, [healthy])
            unexpected = controller.observe(
                previous, current,
                lambda alert: (_ for _ in ()).throw(RuntimeError("alarm during healthy guard")),
                lambda targets: None)
            if unexpected:
                normal_alerts.append(unexpected)
            previous = current
        journal.stage("healthy_resident_guard_completed", windows=args.guard_windows,
                      automatic_alerts=len(normal_alerts), perf_running=False)
        # This is a workload-controller action, NOT an alarm. Detector receives
        # no notification that pressure started. Business connections survive.
        pressure_dir = directory / "resident-pressure"
        pressure_dir.mkdir(mode=0o700)
        pressure = Workers(case, pressure_dir, server, connection, admin,
                           math.ceil(args.trigger_timeout + args.profile_seconds + 50),
                           args.business_rate, args.pressure_rate, True, roles=("pressure",))
        pids = healthy.ids["business"] + pressure.ids["pressure"]
        journal.stage("controlled_workload_transition", semantics="NOT a detection signal",
                      business_connections_unchanged=True)
        deadline = time.monotonic() + args.trigger_timeout
        incident = None
        while time.monotonic() < deadline:
            current = next_snapshot(sentinel, args.window_seconds, [healthy, pressure])
            incident = controller.observe(
                previous, current, lambda alert: pids,
                lambda targets: rich_capture(args, targets, directory / "anomaly", server, admin),
                lambda bundle, alert: diagnose(bundle, alert, stop_workloads))
            previous = current
            if incident:
                break
        if not incident:
            raise RuntimeError("no sustained real eBPF alarm before deadline; no selected-case fallback")
        # Model/TPS scoring may have closed finite diagnostic workloads.
        # Start real normal traffic again to verify return to the SAME BPF
        # sentinel, rather than claiming "resident" from a state flag alone.
        if not healthy:
            resumed_dir = directory / "resumed-business"
            resumed_dir.mkdir(mode=0o700)
            healthy = Workers(case, resumed_dir, server, connection, admin, 15,
                              args.business_rate, args.pressure_rate, False)
        previous = sentinel.snapshot()
        current = next_snapshot(sentinel, args.window_seconds, [healthy, pressure])
        repeated = controller.observe(
            previous, current, lambda alert: pids,
            lambda targets: (_ for _ in ()).throw(RuntimeError("overlapping/repeated perf burst")))
        if repeated or controller.gate.active:
            raise RuntimeError("incident gate did not return to resident")
        receiving = current["recv_calls"] > previous["recv_calls"]
        if not receiving:
            raise RuntimeError("sentinel did not receive new native statistics after incident")
        journal.stage("post_incident_resident_verified", perf_running=False,
                      repeated_burst=False, sentinel_still_receiving=receiving)
        return incident, {"healthy_guard_alerts": len(normal_alerts),
                          "incidents": controller.gate.incidents, "state": controller.gate.state,
                          "baseline": baseline, "business_connections_unchanged": True}
    finally:
        stop_workloads()


def tune_original(args, case, directory, template, server, declared, result):
    """Fresh scoring clone, original model/candidate/apply/TPS/restore path."""
    from sysinsight_live_chain import _api_result, _candidate_and_validation
    with cloned_cluster(template, server) as connection:
        args.host, args.port = connection[connection.index("-h") + 1], 55432
        admin = LibpqSession(connection)
        try:
            case_path, input_path = directory / "case_result.json", directory / "sysinsight_input.json"
            baseline = one_trial(args, case, directory, server, connection, admin, "model-baseline")
            score = baseline["business"]["steady_tps"]
            payload, case_result = read(input_path), read(case_path)
            payload["tuning_context"]["baseline_metrics"] = {"tps": score}
            case_result["baseline_metrics"] = {"tps": score}
            atomic_json(input_path, payload)
            atomic_json(case_path, case_result)
            print(case["id"], "eBPF->perf evidence ready; starting original model", flush=True)
            api = _api_result(case_path, input_path, directory / "sysinsight_api", args, os.environ.copy())
            validated = _candidate_and_validation(api, args, args)
            atomic_json(directory / "candidate_validation.json", validated)
            selected = validated["selected_candidate"]
            if not selected:
                raise RuntimeError("original model returned no executable candidate")
            configuration = selected["validation"]["configuration"]
            if not configuration or set(configuration) - set(result["allowed_knobs"]):
                raise RuntimeError("candidate exceeds unchanged safe permission domain")
            if any(key in LIMITS and not LIMITS[key][0] <= float(value) <= LIMITS[key][1]
                   for key, value in configuration.items()):
                raise RuntimeError("candidate exceeds unchanged numeric budget")
            result.update(configuration=configuration, candidate_id=selected["candidate_id"],
                          API_call_count=len(api.get("llm_calls", [])))
        finally:
            admin.close()
    with cloned_cluster(template, server) as connection:
        args.host = connection[connection.index("-h") + 1]
        admin = LibpqSession(connection)
        try:
            if native_settings(admin, result["allowed_knobs"])[0] != result["initial_configuration"]:
                raise RuntimeError("scoring clone has different initial configuration")
            if invariant_state(admin) != result["initial_business_aggregate"]:
                raise RuntimeError("scoring clone has different initial business data")
            one_trial(args, case, directory, server, connection, admin, "common-scoring-warmup")
            score_dir = directory / "score"
            score_dir.mkdir(mode=0o700)
            scored = retest(args, case, score_dir, server, connection, admin,
                            configuration, result["allowed_knobs"])
            result.update(retest=scored, pipeline_completed=True)
        finally:
            admin.close()


def run_case(options, case, root, template, server, declared):
    from db_profile import resolve_profile
    directory = root / case["id"]
    directory.mkdir(mode=0o700)
    journal = Journal(directory)
    args = SimpleNamespace(**vars(options))
    args.arm = "hybrid"
    args.db, args.db_user, args.run_as = "postgres", "postgres", "postgres"
    args.dbms, args.pg_version, args.pg_cluster, args.candidate_count = "postgresql", "12", "", 1
    result = {"id": case["id"], "status": "running", "collection_chain_completed": False,
              "pipeline_completed": False, "uses_prometheus": False,
              "uses_original_perf": True, "trigger_source": "eBPF",
              "historical_candidates_used": False, "controlled_workload": declared}
    sentinel = None
    try:
        with cloned_cluster(template, server) as connection:
            args.host, args.port = connection[connection.index("-h") + 1], 55432
            admin = LibpqSession(connection)
            try:
                result["initial_business_aggregate"] = invariant_state(admin)
                normal = baseline_profiles(args, case, directory, server, connection, admin, journal)
                target = receiver_target(admin, server)
                sentinel = StatsSentinel(target, server, directory / "sentinel", args.pg_config)
                journal.stage("resident_started", target=target, per_query_probes=False,
                              stacks=False, SQL_text=False)
                def diagnose(anomaly, alert, stop_workloads):
                    detection = compare(anomaly["counts"], normal, directory,
                                        resolve_profile("postgresql", "12"), 2)
                    case_path, payload, allowed, current = make_input(
                        args, case, "perf", directory, admin, detection, anomaly, declared)
                    case_result = read(case_path)
                    collection = {"type": "perf", "trigger": "eBPF_native_PG12_statistics",
                                  "mode": "bounded_on_demand", "semantics": "original perf hardware cycles",
                                  "resident_query_probes": False}
                    case_result.update(collection=collection, ebpf_alert=alert)
                    attach_trigger_metadata(payload, case_result, alert)
                    payload.update(collection=collection, ebpf_observations={
                        "sentinel_alert": alert,
                        "heavy_collection": "original perf + PG exporter-source views + pg_stat_statements",
                        "resident": "native statistics recv only; no per-query or stack probes"})
                    atomic_json(case_path, case_result)
                    atomic_json(directory / "sysinsight_input.json", payload)
                    result.update(collection_chain_completed=True, alert=alert,
                                  allowed_knobs=allowed, initial_configuration=current,
                                  active_SQL_rows=len(anomaly["SQL_observations"]["active_queries"]),
                                  SQL_stat_rows=len(anomaly["SQL_observations"]["slow_queries"]),
                                  global_counter_fields=len(anomaly["before"]),
                                  top_functions=detection["source_compare"]["key_functions"][:20],
                                  raw_perf_bytes=Path(anomaly["profile"]["capture"]["perf"]["data_path"]).stat().st_size)
                    journal.stage("original_evidence_restored", SQL_stat_rows=result["SQL_stat_rows"],
                                  global_counter_fields=result["global_counter_fields"])
                    if args.tune:
                        stop_workloads()
                        journal.stage("original_model_and_TPS_retest_started", perf_running=False)
                        tune_original(args, case, directory, template, server, declared, result)
                        journal.stage("original_model_and_TPS_retest_completed", settings_restored=True)
                    return {"original_differential_completed": True, "model_and_TPS_executed": args.tune}

                incident, monitor = detect_and_capture(
                    args, case, directory, server, connection, admin, sentinel, journal, diagnose)
                result["monitor"] = monitor
            finally:
                try:
                    if sentinel:
                        result["sentinel_cleanup"] = sentinel.close()
                        sentinel = None
                finally:
                    admin.close()
        if args.tune:
            result["status"] = "completed"
        else:
            result["status"] = "collection_and_diagnosis_completed"
    except BaseException as exc:
        result.update(status="failed" if isinstance(exc, Exception) else "cancelled",
                      error=type(exc).__name__ + ": " + str(exc))
        journal.stage(result["status"], error=result["error"])
        if not isinstance(exc, Exception):
            raise
    finally:
        if sentinel:
            result["sentinel_cleanup"] = sentinel.close()
        atomic_json(directory / "result.json", result)
    print(case["id"], result["status"], result.get("error", ""), flush=True)
    return result


def validate(args):
    finite = (args.window_seconds, args.trigger_timeout, args.max_output_mb, args.business_rate,
              args.pressure_rate, args.surge_ratio, args.drop_ratio, args.warmup_seconds,
              args.cooldown_seconds, args.api_timeout, args.drop_sustain_seconds)
    if not all(math.isfinite(value) for value in finite):
        raise ValueError("all budgets must be finite")
    if (not 1 <= args.profile_seconds <= 60 or not 1 <= args.frequency <= 1000
            or not 1 <= args.window_seconds <= 10 or not 4 <= args.calibration_windows <= 20
            or not 2 <= args.guard_windows <= 20 or not 4 <= args.trigger_timeout <= 120
            or not 1 <= args.max_output_mb <= 512 or not 1 <= args.max_pids <= 16
            or not 1 <= args.max_incidents <= 10 or not 0 <= args.cooldown_seconds <= 600
            or not 8 <= args.measure_seconds <= 60 or not 1 <= args.measure_repeats <= 5
            or not 1 <= args.warmup_seconds <= 20
            or not 2 <= args.consecutive_windows <= 10 or not 1 <= args.api_timeout <= 600
            or not 1 <= args.business_rate <= 10000 or not 1 <= args.pressure_rate <= 10000):
        raise ValueError("hybrid experiment exceeds explicit resource budgets")
    RateDetector(args.surge_ratio, args.drop_ratio, args.consecutive_windows, args.drop_sustain_seconds)
    if args.tune and (not args.api_base or not args.model):
        raise ValueError("--tune requires private API environment; no preset candidate fallback")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--pg-config", default="/usr/lib/postgresql/12/bin/pg_config")
    parser.add_argument("--scenario", choices=[case["id"] for case in original_cases()])
    parser.add_argument("--template", help="existing stopped physical snapshot; never a running PGDATA")
    parser.add_argument("--stackcollapse", required=True)
    parser.add_argument("--tune", action="store_true", help="real original API + apply/TPS/restore")
    parser.add_argument("--profile-seconds", type=int, default=15)
    parser.add_argument("--frequency", type=int, default=300)
    parser.add_argument("--window-seconds", type=float, default=2)
    parser.add_argument("--calibration-windows", type=int, default=6)
    parser.add_argument("--guard-windows", type=int, default=3)
    parser.add_argument("--consecutive-windows", type=int, default=2)
    parser.add_argument("--surge-ratio", type=float, default=1.6)
    parser.add_argument("--drop-ratio", type=float, default=.60)
    parser.add_argument("--drop-sustain-seconds", type=float, default=10)
    parser.add_argument("--trigger-timeout", type=float, default=30)
    parser.add_argument("--warmup-seconds", type=float, default=4)
    parser.add_argument("--cooldown-seconds", type=float, default=30)
    parser.add_argument("--max-incidents", type=int, default=1)
    parser.add_argument("--max-output-mb", type=float, default=128)
    parser.add_argument("--max-pids", type=int, default=16)
    parser.add_argument("--business-rate", type=float, default=1600)
    parser.add_argument("--pressure-rate", type=float, default=2500)
    parser.add_argument("--measure-seconds", type=int, default=12)
    parser.add_argument("--measure-repeats", type=int, default=3)
    parser.add_argument("--api-timeout", type=float, default=240)
    parser.add_argument("--api-base", default=os.environ.get("SYSINSIGHT_GPT_BASE_URL", ""))
    parser.add_argument("--model", default=os.environ.get("SYSINSIGHT_GPT_MODEL", ""))
    args = parser.parse_args(argv)
    validate(args)
    root = Path(args.output).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    server = inspect_server(args.pg_config)
    if args.template:
        template = Path(args.template).resolve(strict=True)
        if ((template / "postmaster.pid").exists() or not (template / "PG_VERSION").is_file()
                or (template / "PG_VERSION").read_text().strip() != "12"):
            raise ValueError("template must be a stopped PG12 snapshot, not a running cluster")
    else:
        template = root / "physical-template"
        with cluster(args.pg_config, max_connections=20, statistics=True) as (_, connection):
            prepare_fixture(root, connection)
            admin = LibpqSession(connection)
            try:
                admin.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
                admin.execute("SELECT pg_stat_statements_reset()")
            finally:
                admin.close()
            subprocess.run([str(Path(server["binary"]).parent / "pg_basebackup"),
                            "-h", connection[connection.index("-h") + 1], "-p", "55432",
                            "-U", "postgres", "-D", str(template), "-X", "stream", "-c", "fast"],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    declared = {"warehouses": 5, "business_clients": 2, "pressure_clients": "original per-case",
                "business_offered_tps": args.business_rate, "pressure_offered_tps": args.pressure_rate,
                "seed": 1729, "snapshot": str(template), "SQL": "unmodified original six-case files"}
    results = [run_case(args, case, root, template, server, dict(declared, pressure_clients=int(case["clients"])))
               for case in original_cases() if not args.scenario or args.scenario == case["id"]]
    complete = sum(row["collection_chain_completed"] for row in results)
    summary = {"status": "completed" if all(row["status"] != "failed" for row in results) else "incomplete",
               "cases": results, "collection_chain_completed_count": complete,
               "pipeline_completed_count": sum(row["pipeline_completed"] for row in results),
               "validated_gain_count": sum(bool(row.get("retest", {}).get("validated_gain")) for row in results),
               "no_production_database_or_service_changed": True,
               "not_claimed": "SLO detection accuracy, proven low overhead, equal TPS gains or full-paper equivalence"}
    atomic_json(root / "summary.json", summary)
    print(json.dumps({key: value for key, value in summary.items() if key != "cases"}, indent=2))
    raise SystemExit(0 if summary["status"] == "completed" else 1)


if __name__ == "__main__":
    main()
