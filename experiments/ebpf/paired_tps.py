"""Controlled perf vs eBPF collection, original model, and actual TP TPS.

Both arms clone ONE physical five-warehouse snapshot. SQL, offered rates,
permissions, candidate budget, model, and TPS scoring are common. No monitoring
services or production databases are used. Reference PG counters are read from
the same views as the exporter, not from the running demo's Prometheus.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import pwd
import random
import shutil
import statistics
import subprocess
import tempfile
import time
from types import SimpleNamespace

from .buffer_observer import BufferObserver
from .collector import atomic_json
from .equivalence import LIMITS, POLICY, Workers, invariant_state, native_settings
from .equivalent_profiles import compare, normal_profile
from .equivalent_sampling import sample
from .gain_audit import prepare_fixture
from .lab import cluster
from .overhead import LibpqSession
from .six_scenarios import ROOT, original_cases


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@contextmanager
def cloned_cluster(template, server):
    account = pwd.getpwnam("postgres")
    with tempfile.TemporaryDirectory(prefix="keen-paired-pg-") as temporary:
        root = Path(temporary)
        os.chown(root, account.pw_uid, account.pw_gid)
        data, socket = root / "database", root / "socket"
        shutil.copytree(str(template), str(data))
        for directory, dirs, files in os.walk(str(data)):
            os.chown(directory, account.pw_uid, account.pw_gid)
            for name in files:
                os.chown(str(Path(directory) / name), account.pw_uid, account.pw_gid)
        socket.mkdir()
        os.chown(socket, account.pw_uid, account.pw_gid)
        with (data / "postgresql.conf").open("a") as handle:
            handle.write("\nunix_socket_directories='{}'\n".format(socket))
        bindir = Path(server["binary"]).parent
        command = ["runuser", "-u", "postgres", "--", str(bindir / "pg_ctl"), "-D", str(data)]
        started = False
        try:
            subprocess.run(command + ["-l", str(root / "server.log"), "-w", "start"],
                           cwd="/", check=True, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=60)
            started = True
            yield [str(bindir / "psql"), "-X", "-A", "-t", "-q", "-h", str(socket),
                   "-p", "55432", "-U", "postgres", "-d", "postgres"]
        finally:
            if started:
                subprocess.run(command + ["-m", "immediate", "-w", "stop"],
                               cwd="/", check=True, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, timeout=30)


def host_snapshot():
    cpu = [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
    mem = {row.split()[0].rstrip(":"): int(row.split()[1]) * 1024
           for row in Path("/proc/meminfo").read_text().splitlines()}
    disks = [row.split() for row in Path("/proc/diskstats").read_text().splitlines()]
    return {"at": time.time(), "mono": time.monotonic(), "cpu_total": sum(cpu),
            "cpu_idle": cpu[3], "memory_total": mem["MemTotal"], "memory_available": mem["MemAvailable"],
            "disk_read": sum(int(row[5]) * 512 for row in disks),
            "disk_write": sum(int(row[9]) * 512 for row in disks),
            "load1": float(Path("/proc/loadavg").read_text().split()[0])}


def host_values(before, after):
    elapsed = after["mono"] - before["mono"]
    total = after["cpu_total"] - before["cpu_total"]
    return {
        "cpu_percent": 100 * (1 - (after["cpu_idle"] - before["cpu_idle"]) / total) if total else None,
        "memory_used_percent": 100 * (1 - after["memory_available"] / after["memory_total"]),
        "disk_read_bytes_per_second": (after["disk_read"] - before["disk_read"]) / elapsed,
        "disk_write_bytes_per_second": (after["disk_write"] - before["disk_write"]) / elapsed,
        "load1": after["load1"],
    }


def database_snapshot(admin):
    return json.loads(admin.execute(
        "SELECT row_to_json(v) FROM (SELECT d.xact_commit,d.xact_rollback,d.tup_returned,d.tup_fetched,"
        "d.blks_read,d.blks_hit,d.temp_files,d.temp_bytes,"
        "(SELECT count(*) FROM pg_locks WHERE database=d.datid) AS locks,"
        "pg_database_size(d.datid) AS database_size_bytes,"
        "(SELECT count(*) FROM pg_stat_activity WHERE datid=d.datid AND state='active') AS active_sessions,"
        "b.checkpoints_req,b.checkpoints_timed,b.checkpoint_write_time,b.checkpoint_sync_time,"
        "b.buffers_backend,b.buffers_backend_fsync "
        "FROM pg_stat_database d CROSS JOIN pg_stat_bgwriter b WHERE d.datname=current_database()) v",
        value=True))


def query_snapshot(admin):
    active = json.loads(admin.execute(
        "SELECT coalesce(json_agg(v),'[]'::json) FROM (SELECT pid,state,wait_event_type,wait_event,"
        "left(query,2000) AS query FROM pg_stat_activity WHERE datname=current_database() "
        "AND application_name LIKE 'ebpf-equivalent-%' ORDER BY pid) v", value=True))
    slow = json.loads(admin.execute(
        "SELECT coalesce(json_agg(v),'[]'::json) FROM (SELECT queryid,left(query,2000) AS query,"
        "calls,total_time,mean_time,rows,shared_blks_hit,shared_blks_read,temp_blks_written "
        "FROM pg_stat_statements WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database()) "
        "ORDER BY total_time DESC LIMIT 10) v", value=True))
    return {"active_queries": active, "slow_queries": slow}


def record_series(before, after, first, last, source):
    return {"status": "completed", "result_type": "matrix",
            "source": source, "series": [{"labels": {"scope": source},
                "values": [{"timestamp": first, "value": before},
                           {"timestamp": last, "value": after}]}]}


def counter_capture(before, after, host_before, host_after):
    source_names = {
        "transactions_committed": "xact_commit", "transactions_rolled_back": "xact_rollback",
        "tuples_returned": "tup_returned", "tuples_fetched": "tup_fetched",
        "blocks_read": "blks_read", "blocks_hit": "blks_hit",
        "temporary_files": "temp_files", "temporary_bytes": "temp_bytes",
        "locks": "locks", "database_size_bytes": "database_size_bytes", "active_sessions": "active_sessions",
    }
    queries = {key: record_series(before[field], after[field], host_before["at"], host_after["at"],
                                 "private PostgreSQL exporter source view")
               for key, field in source_names.items()}
    for key, value in host_values(host_before, host_after).items():
        queries[key] = record_series(value, value, host_before["at"], host_after["at"],
                                     "bounded host /proc window; same for both arms")
    return {"status": "completed", "queries": queries,
            "window": {"start": host_before["at"], "end": host_after["at"], "step": None},
            "alerts": {"status": "operator_selected_controlled_case", "selected": None},
            "transport": "direct exporter source views; NOT queries to production Prometheus"}


def capture_phase(args, case, arm, root, server, connection, admin, name, pressure):
    from sysinsight_live_chain import _capture_perf
    from .equivalent_profiles import function_counts
    directory = root / name
    directory.mkdir(mode=0o700)
    workers = observer = None
    try:
        workers = Workers(case, directory, server, connection, admin, args.profile_seconds + 30,
                          args.business_rate, args.pressure_rate, pressure, resident=None)
        pids = workers.ids["business"] + workers.ids.get("pressure", [])
        # Let both collectors observe the same workload startup policy.
        time.sleep(2)
        host_before = host_snapshot()
        before = database_snapshot(admin) if arm == "perf" else None
        if arm == "ebpf":
            observer = BufferObserver(pids, server, directory / "buffer")
            profile = sample(pids, server, directory / "profile", args.profile_seconds,
                             args.frequency, event="cycles", query_association=False, period_weighted=True)
            resources = observer.close()
            observer = None
            if resources["errors"] or resources["measured_backend_count"] != len(pids):
                raise RuntimeError("incomplete BPF buffer evidence")
            counts = function_counts(profile["folded_path"])
        else:
            captured = _capture_perf(directory / "profile", pids, args.profile_seconds,
                                     args.frequency, args.stackcollapse, os.environ.copy())
            post = captured["postprocess"]
            if post.get("status") != "sysinsight_counts_generated":
                raise RuntimeError("reference perf capture failed: " + str(post))
            profile = {"status": "completed", "folded_path": post["folded_path"],
                       "sample_event": "hardware CPU cycles, frequency sampled",
                       "requested_seconds": args.profile_seconds, "frequency_hz_per_task": args.frequency,
                       "capture": captured, "targets": pids}
            counts = Path(post["counts_path"])
            resources = None
        after = database_snapshot(admin) if arm == "perf" else None
        queries = query_snapshot(admin) if arm == "perf" else {"active_queries": [], "slow_queries": []}
        host_after = host_snapshot()
        if not workers.running():
            raise RuntimeError("controlled workload ended during collection")
        result = {"profile": profile, "counts": str(counts),
                  "resource": resources, "before": before, "after": after,
                  "host_before": host_before, "host_after": host_after,
                  "authorized_PIDs": pids, "pressure_policy": workers.policy,
                  "SQL_observations": queries,
                  "no_PortalRun_or_ExecutorRun_uprobes": True}
        atomic_json(directory / "phase.json", result)
        return result
    finally:
        try:
            if observer:
                observer.close()
        finally:
            if workers:
                workers.close()


def make_input(args, case, arm, root, admin, detection, anomaly, declared):
    from db_profile import profile_summary, resolve_profile
    from sysinsight_prometheus import build_sysinsight_input
    from sysinsight_shap import compact_evidence, explain_subprocess
    profile = resolve_profile("postgresql", "12")
    allowed = sorted(POLICY & set(profile.constraints()))
    current, _ = native_settings(admin, allowed)
    host_before, host_after = anomaly["host_before"], anomaly["host_after"]
    host = host_values(host_before, host_after)
    metrics = {"schema": "keeninsight.measured-resources.v1", "cpu": host["cpu_percent"],
               "virtualMem": host_after["memory_total"] / 1024 ** 3,
               "physical": (host_after["memory_total"] - host_after["memory_available"]) / 1024 ** 3}
    if arm == "perf":
        before, after = anomaly["before"], anomaly["after"]
        elapsed = host_after["mono"] - host_before["mono"]
        reads, hits = (max(0, after[key] - before[key]) for key in ("blks_read", "blks_hit"))
        metrics.update(readIO=reads / elapsed, writeIO=max(0, after["temp_bytes"] - before["temp_bytes"]) / elapsed,
                       hit=100 * hits / (hits + reads) if hits + reads else None,
                       source="recorded host + pg_stat_database window; logical blocks and temp-file bytes")
        capture = counter_capture(before, after, host_before, host_after)
        samples = {"first": {**before, "ts": host_before["at"], "elapsed_seconds": 0},
                   "last": {**after, "ts": host_after["at"], "elapsed_seconds": elapsed}}
    else:
        resource = anomaly["resource"]
        metrics.update({key: resource[key] for key in ("readIO", "writeIO", "hit")})
        metrics["source"] = "BPF pgBufferUsage sampled backend deltas + tempfile events; host /proc window"
        empty = {key: None for key in ("xact_commit", "xact_rollback", "tup_returned", "tup_fetched",
                                     "blks_read", "blks_hit", "temp_files", "temp_bytes", "locks",
                                     "database_size_bytes", "active_sessions")}
        capture = counter_capture(empty, empty, host_before, host_after)
        for key in list(capture["queries"]):
            if key not in host:
                capture["queries"][key] = {"status": "not_collected", "series": [],
                    "reason": "not database-global BPF counters; backend-scoped resource_metrics provided separately"}
        samples = {}
    shap = explain_subprocess(anomaly["counts"], context={"workload": case["id"], "phase": "pressure"})
    atomic_json(root / "shap_analysis.json", shap)
    detection["shap"] = compact_evidence(shap)
    case_result = {
        "mode": "live_alert", "id": case["id"], "workload": case["title"],
        "database_schema": "keeninsight_tpcc", "collection": {"type": arm, "semantics": "hardware cycles samples"},
        "controlled_workload": declared, "resource_metrics": metrics,
        "baseline_metrics": {"tps": None}, "before_settings": current,
        "active_queries": anomaly["SQL_observations"]["active_queries"],
        "slow_queries": anomaly["SQL_observations"]["slow_queries"],
        "sysinsight_source_detection": detection,
        "anomaly": {"samples": samples, "perf_postprocess": {"folded_path": anomaly["profile"]["folded_path"]}},
    }
    case_path = root / "case_result.json"
    payload = build_sysinsight_input(case_result, capture, profile_summary(profile), case_path,
                                     database_name="postgres", database_schema="keeninsight_tpcc")
    payload["collection"] = case_result["collection"]
    payload["resource_metrics"] = metrics
    payload["ebpf_observations"] = {
        "resource_metrics": metrics, "normal_profile": detection["source_compare"]["normal_profile"],
        "sampling_event": "hardware cycles, 300Hz, matched per backend", "query_association": "disabled in both arms",
        "counter_scope_note": "perf: database views; BPF: sampled target-backend counters + tempfile events",
    }
    evidence = payload["source_evidence"]
    if arm == "ebpf":
        evidence["semantics"]["runtime_observation"] = "ebpf_cpu_stack_samples"
        evidence["semantics"]["runtime_call_chain"] = "real eBPF folded hardware-cycle stacks"
        for chain in evidence["runtime_call_chains"]:
            chain["source"] = "ebpf_cpu_folded_stack"
    if evidence["status"] != "completed" or not evidence["static_parameter_evidence"]:
        raise RuntimeError("original source evidence missing in controlled arm")
    payload["tuning_context"] = {
        "objective": {"metric": "normal_business_tps", "lower_is_better": False},
        "allowed_knobs": allowed, "knob_limits": {key: LIMITS[key] for key in allowed if key in LIMITS},
        "current_configuration": current,
        "slow_queries": case_result["slow_queries"],
        "safety": "same permissions, preserve durability, no restart/endpoint/SQL/DDL changes",
    }
    atomic_json(case_path, case_result)
    atomic_json(root / "sysinsight_input.json", payload)
    return case_path, payload, allowed, current


def one_trial(args, case, root, server, connection, admin, label, settings=None):
    directory = root / label
    directory.mkdir(mode=0o700)
    workers = None
    try:
        workers = Workers(case, directory, server, connection, admin, args.measure_seconds,
                          args.business_rate, args.pressure_rate, True, settings, resident=None)
        result = workers.finish(timeout=args.measure_seconds + 20)
        result.update(all_diagnostic_probes_unloaded=True, resident_enabled=False,
                      session_settings_delivery="PGOPTIONS_once_per_connection")
        atomic_json(directory / "result.json", result)
        return result
    finally:
        if workers:
            workers.close()


def pressure_comparison(before, after):
    from lab_controller import _tpcc_comparison
    def phase(row):
        return {"business_tps_summary": {"steady_tps": row["business"]["steady_tps"]},
                "pressure_tps_summary": {"steady_tps": row["pressure"]["steady_tps"]},
                "pressure_target_tps": row["pressure_policy"]["pressure_offered_tps"]}
    return _tpcc_comparison({"pressure_before": phase(before), "pressure_after": phase(after)})


def retest(args, case, root, server, connection, admin, configuration, allowed):
    from pg_temporary_config import TemporaryPostgresConfiguration
    initial, _ = native_settings(admin, allowed)
    invariant = invariant_state(admin)
    blocks = []
    for repeat in range(args.measure_repeats):
        before = one_trial(args, case, root, server, connection, admin, "block-{}-before".format(repeat))
        applier = TemporaryPostgresConfiguration(args, configuration, "paired-tps", allow_restart=False)
        with applier as applied:
            if applied["skipped_configuration"] or applied["connection_configuration"]:
                raise RuntimeError("candidate exceeds common no-restart/no-endpoint permissions")
            tuned = one_trial(args, case, root, server, connection, admin,
                              "block-{}-tuned".format(repeat), applied["session_configuration"])
        if native_settings(admin, allowed)[0] != initial:
            raise RuntimeError("settings not restored in private paired arm")
        restored = one_trial(args, case, root, server, connection, admin,
                             "block-{}-restored".format(repeat))
        left, right, score = (row["business"]["steady_tps"] for row in (before, restored, tuned))
        baseline = statistics.mean((left, right))
        blocks.append({
            "repeat": repeat, "before": before, "tuned": tuned, "restored": restored,
            "bracketed_baseline_TPS": baseline, "gain_ratio": score / baseline - 1,
            "restored_drift_ratio": right / left - 1,
            "pressure_comparison_before": pressure_comparison(before, tuned),
            "pressure_comparison_restored": pressure_comparison(restored, tuned),
            "application": applier.state, "settings_restored": True,
        })
        atomic_json(root / "blocks.json", {"blocks": blocks})
        print(case["id"], args.arm, "block", repeat, "before/tuned/restored",
              round(left, 2), round(score, 2), round(right, 2), flush=True)
    if invariant_state(admin) != invariant:
        raise RuntimeError("business aggregate invariant changed")
    median_gain = statistics.median(row["gain_ratio"] for row in blocks)
    valid = all(row["pressure_comparison_before"]["comparison_valid"] and
                row["pressure_comparison_restored"]["comparison_valid"] and
                abs(row["restored_drift_ratio"]) <= .05 for row in blocks)
    return {"blocks": blocks, "median_gain_ratio": median_gain,
            "before_business_TPS": statistics.median(row["before"]["business"]["steady_tps"] for row in blocks),
            "tuned_business_TPS": statistics.median(row["tuned"]["business"]["steady_tps"] for row in blocks),
            "restored_business_TPS": statistics.median(row["restored"]["business"]["steady_tps"] for row in blocks),
            "measurement_valid": valid, "validated_gain": valid and median_gain >= .05,
            "business_aggregate_unchanged": True, "all_settings_restored": True,
            "note": "bracketed repeated actual TPS, not a new historical preset or latency proxy"}


def paired_result(perf, ebpf, margin=.05):
    if perf.get("status") != "completed" or ebpf.get("status") != "completed":
        return {"status": "incomplete", "same_TPS_benefit_proven": False}
    left, right = perf["retest"], ebpf["retest"]
    rng = random.Random(1729)
    a, b = ([row["gain_ratio"] for row in item["blocks"]] for item in (left, right))
    draws = sorted(statistics.median(rng.choices(b, k=len(b))) -
                   statistics.median(rng.choices(a, k=len(a))) for _ in range(2000))
    interval = [draws[100], draws[1899]]
    within = interval[0] >= -margin and interval[1] <= margin
    valid = left["measurement_valid"] and right["measurement_valid"]
    return {
        "status": "within_exploratory_margin" if valid and within else "not_demonstrated",
        "perf_gain_ratio": left["median_gain_ratio"], "ebpf_gain_ratio": right["median_gain_ratio"],
        "difference_ratio": right["median_gain_ratio"] - left["median_gain_ratio"],
        "difference_90pct_bootstrap_interval": interval, "equivalence_margin_ratio": margin,
        "both_measurements_valid": valid, "same_TPS_benefit_proven": False,
        "exploratory_comparable": valid and within,
        "both_positive_gains_validated": left["validated_gain"] and right["validated_gain"],
        "limits": "three short bracketed blocks and one independent model acquisition per arm; "
                  "bootstrap does not cover model stochasticity or establish paper-level equivalence",
    }


def run_arm(options, case, arm, directory, template, server, declared):
    from sysinsight_live_chain import _api_result, _candidate_and_validation
    from db_profile import resolve_profile
    directory.mkdir(mode=0o700)
    args = SimpleNamespace(**vars(options))
    args.arm, args.db, args.db_user, args.run_as = arm, "postgres", "postgres", "postgres"
    args.dbms, args.pg_version, args.pg_cluster, args.candidate_count = "postgresql", "12", "", 1
    result = {"case": case["id"], "arm": arm, "status": "running", "controlled_workload": declared}
    atomic_json(directory / "result.json", result)
    try:
        with cloned_cluster(template, server) as connection:
            args.host = connection[connection.index("-h") + 1]
            args.port = int(connection[connection.index("-p") + 1])
            admin = LibpqSession(connection)
            try:
                normal = [capture_phase(args, case, arm, directory, server, connection, admin,
                                        "normal-" + str(index), False) for index in range(2)]
                normal_path = directory / "normal_profile.csv"
                normal_profile([row["counts"] for row in normal], normal_path)
                anomaly = capture_phase(args, case, arm, directory, server, connection, admin,
                                        "anomaly", True)
                detection = compare(anomaly["counts"], normal_path, directory,
                                    resolve_profile("postgresql", "12"), 2)
                case_path, payload, allowed, current = make_input(
                    args, case, arm, directory, admin, detection, anomaly, declared)
                # Score context immediately before API; trials are measured
                # again AFTER API so model latency cannot stale the trial baseline.
                initial_trial = one_trial(args, case, directory, server, connection, admin, "model-baseline")
                score = initial_trial["business"]["steady_tps"]
                payload["tuning_context"]["baseline_metrics"] = {"tps": score}
                case_result = read(case_path)
                case_result["baseline_metrics"] = {"tps": score}
                atomic_json(case_path, case_result)
                atomic_json(directory / "sysinsight_input.json", payload)
                print(case["id"], arm, "captured; starting original model", flush=True)
                api = _api_result(case_path, directory / "sysinsight_input.json",
                                  directory / "sysinsight_api", args, os.environ.copy())
                validated = _candidate_and_validation(api, args, args)
                atomic_json(directory / "candidate_validation.json", validated)
                selected = validated["selected_candidate"]
                if not selected:
                    raise RuntimeError("original model returned no executable candidate")
                config = selected["validation"]["configuration"]
                if not config or set(config) - set(allowed):
                    raise RuntimeError("model candidate outside identical permissions")
                if any(name in LIMITS and not LIMITS[name][0] <= float(value) <= LIMITS[name][1]
                       for name, value in config.items()):
                    raise RuntimeError("model candidate exceeds common numeric bounds")
                result.update(initial_configuration=current, allowed_knobs=allowed,
                              configuration=config, candidate_id=selected["candidate_id"],
                              API_call_count=len(api.get("llm_calls", [])),
                              model_configuration_sha256=hashlib.sha256(
                                  (args.model + args.api_base).encode()).hexdigest(),
                              resources=case_result["resource_metrics"],
                              top_functions=detection["source_compare"]["key_functions"][:20])
            finally:
                admin.close()
        # Diagnostic collection and model latency cannot leave different
        # bloat/cache histories in the scoring database: use a NEW physical
        # clone and identical uninstrumented warmup for each arm's TPS tests.
        with cloned_cluster(template, server) as connection:
            args.host = connection[connection.index("-h") + 1]
            args.port = int(connection[connection.index("-p") + 1])
            admin = LibpqSession(connection)
            try:
                if native_settings(admin, allowed)[0] != current:
                    raise RuntimeError("scoring clone has different initial settings")
                if invariant_state(admin) != server["initial_business_aggregate"]:
                    raise RuntimeError("scoring clone has different initial business data")
                one_trial(args, case, directory, server, connection, admin, "common-scoring-warmup")
                result.update(retest=retest(args, case, directory, server, connection, admin, config, allowed),
                              status="completed")
            finally:
                admin.close()
    except Exception as exc:
        result.update(status="failed", error=type(exc).__name__ + ": " + str(exc))
        print(case["id"], arm, result["error"], flush=True)
    atomic_json(directory / "result.json", result)
    return result


def make_summary(root, cases):
    results = []
    for case in cases:
        path = root / case["id"]
        if not all((path / arm / "result.json").exists() for arm in ("perf", "ebpf")):
            continue
        perf, ebpf = (read(path / arm / "result.json") for arm in ("perf", "ebpf"))
        row = {"case": case["id"], "perf": perf, "ebpf": ebpf, "comparison": paired_result(perf, ebpf)}
        if perf.get("status") == ebpf.get("status") == "completed":
            row["fairness"] = {
                "same_declared_workload": perf["controlled_workload"] == ebpf["controlled_workload"],
                "same_initial_knobs": perf["initial_configuration"] == ebpf["initial_configuration"],
                "same_allowed_knobs": perf["allowed_knobs"] == ebpf["allowed_knobs"],
                "same_model_configuration": perf["model_configuration_sha256"] == ebpf["model_configuration_sha256"],
                "same_TPS_code_and_settings_delivery": True,
                "all_probes_off_during_TPS_trials": True,
            }
        results.append(row)
    summary = {"cases": results, "paired_cases": len(results),
               "completed_pairs": sum(row["perf"]["status"] == row["ebpf"]["status"] == "completed" for row in results),
               "exploratory_comparable_pairs": sum(row["comparison"].get("exploratory_comparable", False) for row in results),
               "positive_gains_both_validated": sum(row["comparison"].get("both_positive_gains_validated", False) for row in results),
               "not_claimed": "exact equality, six-case stable benefit reproduction, full deployed alert transport"}
    atomic_json(root / "summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--pg-config", default="/usr/lib/postgresql/12/bin/pg_config")
    parser.add_argument("--scenario", choices=[case["id"] for case in original_cases()])
    parser.add_argument("--profile-seconds", type=int, default=15)
    parser.add_argument("--frequency", type=int, default=300)
    parser.add_argument("--measure-seconds", type=int, default=12)
    parser.add_argument("--measure-repeats", type=int, default=3)
    parser.add_argument("--business-rate", type=float, default=1600)
    parser.add_argument("--pressure-rate", type=float, default=2500)
    parser.add_argument("--stackcollapse", required=True)
    parser.add_argument("--api-timeout", type=float, default=240)
    parser.add_argument("--api-base", default=os.environ.get("SYSINSIGHT_GPT_BASE_URL", ""))
    parser.add_argument("--model", default=os.environ.get("SYSINSIGHT_GPT_MODEL", ""))
    args = parser.parse_args(argv)
    if not 5 <= args.profile_seconds <= 30 or not 1 <= args.frequency <= 1000:
        parser.error("profile budget outside bounds")
    if not 8 <= args.measure_seconds <= 30 or not 2 <= args.measure_repeats <= 5:
        parser.error("TPS budget outside bounds")
    if not 0 < args.business_rate <= 2000 or not 0 < args.pressure_rate <= 4000:
        parser.error("offered rate outside bounds")
    if not os.environ.get("SYSINSIGHT_GPT_API_KEY") or not Path(args.stackcollapse).is_file():
        parser.error("real original API credentials and stackcollapse required")
    root = Path(args.output).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    cases = [case for case in original_cases() if not args.scenario or case["id"] == args.scenario]
    account = pwd.getpwnam("postgres")
    with tempfile.TemporaryDirectory(prefix="keen-paired-template-") as temporary:
        os.chown(temporary, account.pw_uid, account.pw_gid)
        template = Path(temporary) / "snapshot"
        with cluster(args.pg_config, max_connections=20, statistics=True) as (server, connection):
            subprocess.run(connection + ["-c", "CREATE EXTENSION pg_stat_statements"],
                           stdout=subprocess.DEVNULL, check=True, timeout=30)
            prepare_fixture(root, connection)
            admin = LibpqSession(connection)
            try:
                initial_invariant = invariant_state(admin)
                admin.execute("SELECT pg_stat_statements_reset()")
                server["initial_business_aggregate"] = initial_invariant
            finally:
                admin.close()
            command = ["runuser", "-u", "postgres", "--", str(Path(server["binary"]).parent / "pg_basebackup"),
                       "-h", connection[connection.index("-h") + 1], "-p", "55432", "-U", "postgres",
                       "-D", str(template), "-X", "stream", "-c", "fast"]
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180)
        files = sorted(path for path in template.rglob("*") if path.is_file() and
                       path.relative_to(template).parts[0] not in ("pg_wal", "pg_stat_tmp"))
        digest = hashlib.sha256()
        for path in files:
            digest.update(str(path.relative_to(template)).encode())
            digest.update(bytes.fromhex(sha(path)))
        snapshot_hash = digest.hexdigest()
        atomic_json(root / "experiment.json", {
            "schema": "keeninsight.paired-collection-TPS.v1", "snapshot_sha256": snapshot_hash,
            "snapshot_method": "one pg_basebackup, each arm/case gets a fresh physical copy",
            "initial_business_aggregate": initial_invariant,
            "same_model_and_permissions": True, "candidate_budget_per_arm": 1,
            "source_sha256": {name: sha(ROOT / name) for name in (
                "experiments/ebpf/paired_tps.py", "experiments/ebpf/equivalent_sampling.py",
                "experiments/ebpf/buffer_observer.py", "app/sysinsight_live_chain.py",
                "app/sysinsight_original_llm.py", "app/pg_temporary_config.py")},
            "warmup_policy": "2 seconds before profiling, original TPS startup exclusion in each trial",
            "collection": "perf hardware cycles vs BPF hardware cycles, both sum sample_period; "
                          "no SQL-boundary hot probes",
            "reference_transport": "same PostgreSQL exporter source views, no production Prometheus",
            "scoring": "three before/tuned/restored blocks; original pressure comparison; drift veto 5%",
            "scope": "selected controlled diagnosis, not a resident detector/monitoring-overhead test",
            "model_stochasticity": "same settings/provider; independent real calls, no deterministic-response claim",
        })
        for index, case in enumerate(cases):
            declared = {
                "case_id": case["id"], "warehouses": 5, "business_clients": 2,
                "pressure_clients": int(case["clients"]), "business_offered_TPS": args.business_rate,
                "pressure_offered_TPS": args.pressure_rate, "pgbench_random_seed": 1729,
                "business_SQL_sha256": sha(ROOT / "app/tpcc_cases" / case["normal_sql"]),
                "pressure_SQL_sha256": sha(ROOT / "app/tpcc_cases" / case["sql"]),
                "physical_snapshot_sha256": snapshot_hash,
            }
            directory = root / case["id"]
            directory.mkdir(mode=0o700)
            # Balance arm order across six cases; no concurrent DB workloads.
            for arm in (("perf", "ebpf") if index % 2 == 0 else ("ebpf", "perf")):
                print("START", case["id"], arm, flush=True)
                run_arm(args, case, arm, directory / arm, template, server, declared)
                make_summary(root, cases)
    summary = make_summary(root, cases)
    print(json.dumps({key: value for key, value in summary.items() if key != "cases"}, indent=2))


if __name__ == "__main__":
    main()
