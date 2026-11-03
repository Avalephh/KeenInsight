"""Functional-parity eBPF -> original differential/LLM -> original TP TPS lab.

15s/300Hz multi-backend profiles by default; optimize overhead only afterwards.
No production endpoint CLI, Prometheus, perf subprocess or candidate fallback.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import sys
import time
from types import SimpleNamespace

from .collector import atomic_json
from .equivalent_profiles import compare, function_counts, normal_profile
from .equivalent_resident import Resident
from .equivalent_sampling import sample
from .lab import cluster
from .overhead import LibpqSession
from .pipeline import Journal, sql_literal
from .runtime import CollectorProcess
from .six_scenarios import ROOT, original_cases, process_ids

sys.path.insert(0, str(ROOT / "app"))

# Explicit safety limits, NOT proposed repairs. Preserve durability/endpoints.
LIMITS = {
    "work_mem": [64, 65536], "effective_cache_size": [16384, 1048576],
    "random_page_cost": [1, 8], "seq_page_cost": [.1, 4],
    "cpu_tuple_cost": [.001, .1], "cpu_index_tuple_cost": [.001, .1],
    "cpu_operator_cost": [.0001, .1], "parallel_setup_cost": [0, 10000],
    "parallel_tuple_cost": [0, 1], "max_parallel_workers_per_gather": [0, 2],
    "jit_above_cost": [0, 1000000], "jit_inline_above_cost": [0, 1000000],
    "jit_optimize_above_cost": [0, 1000000], "commit_delay": [0, 1000],
    "commit_siblings": [0, 20], "max_wal_size": [1024, 4096],
    "checkpoint_completion_target": [.5, .9], "checkpoint_timeout": [30, 900],
}
BOOLS = {"jit", "jit_expressions", "jit_tuple_deforming", "enable_hashjoin", "enable_hashagg",
         "enable_nestloop", "enable_mergejoin", "enable_indexscan", "enable_indexonlyscan",
         "enable_bitmapscan", "enable_seqscan", "enable_sort", "enable_material"}
POLICY = set(LIMITS) | BOOLS


def startup_options(settings):
    """Set bounded experiment GUCs once per connection, never per transaction.

    PGOPTIONS uses PostgreSQL's command-line token syntax, not SQL quoting.
    The lab only permits the numeric/boolean policy above, so no whitespace,
    quoting, endpoint options or inherited PGOPTIONS are needed.
    """
    options = []
    for name, value in sorted((settings or {}).items()):
        if name not in POLICY:
            raise ValueError("startup setting outside experiment policy: " + name)
        text = str(value).lower()
        if name in BOOLS:
            if text not in ("on", "off", "true", "false"):
                raise ValueError("invalid boolean startup setting: " + name)
        else:
            if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?", text):
                raise ValueError("invalid numeric startup setting: " + name)
            if not math.isfinite(float(text)) or not LIMITS[name][0] <= float(text) <= LIMITS[name][1]:
                raise ValueError("startup setting outside numeric limits: " + name)
        options.extend(("-c", name + "=" + text))
    return " ".join(options)


def parse_worker(text, offered, duration=None):
    processed = re.search(r"number of transactions actually processed:\s*(\d+)", text)
    tps = re.findall(r"^tps\s*=\s*([0-9]+(?:\.[0-9]+)?).*$", text, re.M)
    failed = re.search(r"number of failed transactions:\s*(\d+)", text)
    errors = re.search(r"(?:ERROR:|FATAL:|client \d+ aborted|connection to database .* failed)", text)
    if not processed or not tps or errors or failed and int(failed.group(1)):
        raise RuntimeError("original pgbench workload failed or produced no completed TPS")
    result = {"completed_transactions": int(processed.group(1)),
            "tps": float(tps[-1]), "offered_tps": offered,
            "metric": "pgbench completed original workload transactions, not SQL spans",
            "connection_time_excluded": "excluding connections establishing" in text}
    if duration is not None:
        from lab_controller import _tpcc_tps_summary
        progress = re.findall(r"progress:\s*([0-9.]+)\s*s,\s*([0-9.]+)\s*tps", text)
        rows = [{"phase": "trial", "elapsed_seconds": float(elapsed),
                 "metrics": {"business": {"tps_live": float(tps)}}} for elapsed, tps in progress]
        result["steady_summary"] = _tpcc_tps_summary(rows, "trial", "business", duration)
        if result["steady_summary"].get("status") != "ok":
            raise RuntimeError("original TPS summarizer has insufficient progress observations")
        result["steady_tps"] = result["steady_summary"]["steady_tps"]
    return result


class Workers:
    def __init__(self, case, directory, server, connection, admin, seconds, business_rate,
                 pressure_rate, pressure, settings=None, resident=None):
        self.directory, self.processes, self.ids = directory, [], {}
        self.resident, self.registered = resident, []
        self.seconds = seconds
        self.policy = {"business_offered_tps": business_rate,
                       "pressure_offered_tps": pressure_rate if pressure else 0,
                       "pressure_clients": int(case["clients"]) if pressure else 0,
                       "business_clients": 2}
        try:
            for role in (("business", "pressure") if pressure else ("business",)):
                path = directory / (role + ".sql")
                original = ROOT / "app/tpcc_cases" / case["normal_sql" if role == "business" else "sql"]
                # A pgbench script is repeated for EVERY transaction.  SET
                # prefixes biased candidates with extra SQL and PortalRun
                # probe hits.  Pass settings at connection startup instead.
                path.write_text(original.read_text())
                application = "ebpf-equivalent-" + role + "-" + directory.name
                rate = business_rate if role == "business" else pressure_rate
                clients = 2 if role == "business" else int(case["clients"])
                args = [str(Path(server["binary"]).parent / "pgbench"), "-n", "-M", "simple",
                        "-h", connection[connection.index("-h") + 1],
                        "-p", connection[connection.index("-p") + 1], "-U", "postgres",
                        "-c", str(clients), "-j", "2", "-f", str(path),
                        "-T", str(seconds), "-P", "1", "--random-seed=1729"]
                if rate > 0:
                    args += ["-R", str(rate)]
                log = (directory / (role + ".log")).open("w")
                try:
                    process = subprocess.Popen(args + ["postgres"],
                                               env={**os.environ, "PGAPPNAME": application,
                                                    "PGOPTIONS": startup_options(settings)},
                                               stdout=log, stderr=subprocess.STDOUT)
                except BaseException:
                    log.close()
                    raise
                self.processes.append({"role": role, "process": process, "log": log,
                                       "application": application, "rate": rate})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                for worker in self.processes:
                    self.ids[worker["role"]] = process_ids(admin, worker["application"])
                if all(len(self.ids[w["role"]]) == (2 if w["role"] == "business" else int(case["clients"]))
                       for w in self.processes):
                    if self.resident:
                        self.registered = [pid for ids in self.ids.values() for pid in ids]
                        self.resident.add(self.registered)
                    return
                if any(w["process"].poll() is not None for w in self.processes):
                    raise RuntimeError("workload exited before collection was ready")
                time.sleep(.05)
            raise RuntimeError("not all original workload backends connected")
        except BaseException:
            self.close()
            raise

    def running(self):
        return all(worker["process"].poll() is None for worker in self.processes)

    def finish(self, timeout=60):
        result = {"pressure_policy": self.policy}
        deadline = time.monotonic() + timeout
        while any(w["process"].poll() is None for w in self.processes):
            if time.monotonic() >= deadline:
                raise TimeoutError("workload trial exceeded wall-clock budget")
            if self.resident:
                self.resident.poll()
            time.sleep(.1)
        for worker in self.processes:
            worker["process"].wait(timeout=timeout)
            worker["log"].flush()
            if worker["process"].returncode:
                raise RuntimeError("workload nonzero exit; inspect " + worker["log"].name)
            result[worker["role"]] = parse_worker(
                Path(worker["log"].name).read_text(), worker["rate"], self.seconds)
        return result

    def close(self):
        detach_error = None
        try:
            if self.resident and self.registered and not self.resident.closed:
                self.resident.remove(self.registered)
                self.registered = []
        except Exception as exc:
            detach_error = exc
        for worker in self.processes:
            process = worker["process"]
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            worker["log"].close()
        if detach_error:
            raise detach_error


def trigger(events, clients, mode, active_threshold=3, executor_ms=100):
    if mode == "selected":
        return {"reason": "operator_selected_original_scenario", "source": "explicit_selection"}
    active = {}
    for event in events:
        if (event.get("pid") in clients and event.get("type") == "query_snapshot"
                and event.get("depth") == 1):
            active[event["pid"]] = event
    if len(active) >= active_threshold:
        return {"reason": "ebpf_active_executor_threshold", "source": "ebpf",
                "observed_active_backends": len(active), "threshold": active_threshold,
                "semantics": "PortalRun occupancy, not exact pg_stat_activity state"}
    slow = [event for event in active.values() if event.get("elapsed_ns", 0) >= executor_ms * 1e6]
    if slow:
        return {"reason": "ebpf_long_executor_threshold", "source": "ebpf",
                "threshold_ms": executor_ms, "observed_pid": slow[0]["pid"],
                "semantics": "coarse anomaly trigger; not a root-cause rule"}
    return None


def collect_phase(args, case, root, server, connection, admin, name, pressure, journal):
    directory = root / name
    directory.mkdir(mode=0o700)
    workers = detail = None
    try:
        resource_seconds = min(5, args.profile_seconds)
        seconds = math.ceil(args.profile_seconds + args.trigger_timeout + resource_seconds + 15)
        workers = Workers(case, directory, server, connection, admin, seconds,
                          args.business_rate, args.pressure_rate, pressure, resident=args.resident)
        clients = workers.ids["business"] + workers.ids.get("pressure", [])
        checkpointer = int(admin.execute(
            "SELECT pid FROM pg_stat_activity WHERE backend_type='checkpointer'", value=True))
        journal.stage(name + "_resident", targets=clients + [checkpointer],
                      query_boundaries=True, explanation="functional parity first, not low-overhead claim")
        cause = {"reason": "normal_reference"}
        if pressure:
            deadline = time.monotonic() + args.trigger_timeout
            cause = None
            while time.monotonic() < deadline and workers.running():
                cause = trigger(args.resident.snapshot(), clients, args.trigger_mode)
                if cause:
                    break
                time.sleep(.025)
            if not cause:
                raise RuntimeError("no generic eBPF anomaly before deadline; no spill-only fallback")
            journal.stage("trigger", **cause)
        sampled = sample(clients, server, directory / "profile", args.profile_seconds, args.frequency,
                         on_ready=lambda: journal.stage(name + "_detail_attached", pids=clients),
                         on_stopped=lambda: journal.stage(name + "_sampling_disarmed",
                                                         reason="profile_window_complete"),
                         on_tick=args.resident.poll)
        journal.stage(name + "_detail_detached", all_sampling_FDs_closed=sampled["all_probe_FDs_closed"])
        if sampled["sample_count"] < args.min_samples:
            raise RuntimeError("insufficient profile samples; no synthetic evidence/fallback")
        if not workers.running():
            raise RuntimeError("workload exited during diagnostic capture")
        sampled["comparison_instrumentation"] = {
            "resident_hooks": [list(hook) for hook in Resident.HOOKS],
            "resource_probes_during_CPU_profile": [],
            "resource_capture_order": "after_CPU_profile_unloaded",
        }
        atomic_json(directory / "profile/manifest.json", sampled)
        if pressure:
            # Do not profile the collector's own timed resource uprobes only
            # on the anomaly side. Both differential profiles use the same
            # instrumentation. Resources are a separate bounded window.
            detail = CollectorProcess(directory / "detail", clients + [checkpointer], args.pg_config,
                                      "query", "diagnostic-system", resource_seconds,
                                      max_mb=64, report_seconds=.5, query_output="aggregate",
                                      metric_mode="timed")
            journal.stage("targeted_resource_probes_attached", pids=clients + [checkpointer],
                          CPU_sampling_already_unloaded=True, window_seconds=resource_seconds)
            deadline = time.monotonic() + resource_seconds
            while time.monotonic() < deadline and detail.process.poll() is None:
                if not workers.running():
                    raise RuntimeError("workload exited during separate resource capture")
                args.resident.poll()
                time.sleep(.025)
        manifest = detail.close() if detail else None
        detail = None
        if manifest and (manifest.get("perf_lost_notifications")
                         or any(manifest.get("bpf_health", {}).values())):
            raise RuntimeError("detail reports lost/incomplete evidence")
        if manifest:
            journal.stage("targeted_resource_probes_detached")
        counts = function_counts(sampled["folded_path"])
        return {"profile": sampled, "counts": str(counts), "trigger": cause,
                "pressure_policy": workers.policy, "resource_manifest": manifest}
    finally:
        try:
            if detail:
                detail.close(check=False)
        finally:
            if workers:
                workers.close()


def native_settings(admin, names):
    data = json.loads(admin.execute(
        "SELECT json_object_agg(name, json_build_object('value',setting,'context',context,'type',vartype)) "
        "FROM pg_settings WHERE name IN (" + ",".join(sql_literal(name) for name in names) + ")",
        value=True))
    result = {}
    for name, row in data.items():
        result[name] = (int(row["value"]) if row["type"] == "integer" else
                        float(row["value"]) if row["type"] == "real" else row["value"])
    return result, data


def build_model_input(args, root, case, detection, baseline_tps, admin):
    from db_profile import profile_summary, resolve_profile
    from sysinsight_source_evidence import build_source_evidence
    matched = {row["knob_name"] for row in detection["source_match"]["matched_knob"]}
    profile = resolve_profile("postgresql", "12")
    allowed = sorted(matched & POLICY & set(profile.constraints()))
    if not allowed:
        raise RuntimeError("original differential matcher found no policy-allowed parameter")
    current, details = native_settings(admin, allowed)
    anomaly = root / "anomaly/profile/profile.folded"
    case_result = {
        "mode": "live_alert", "id": case["id"], "workload": case["title"],
        "collection": {"type": "ebpf", "semantics": "CPU stack samples"},
        "baseline_metrics": {"tps": baseline_tps}, "before_settings": current,
        "active_queries": [], "sysinsight_source_detection": detection,
        "anomaly": {"perf_postprocess": {"folded_path": str(anomaly)}},
    }
    path = root / "case_result.json"
    evidence = build_source_evidence(case_result, path, profile_summary(profile))
    evidence["semantics"]["runtime_observation"] = "ebpf_cpu_stack_samples"
    evidence["semantics"]["runtime_call_chain"] = "real eBPF user/kernel folded on-CPU stacks"
    for chain in evidence["runtime_call_chains"]:
        chain["source"] = "ebpf_cpu_folded_stack"
    if evidence["status"] != "completed" or not evidence["static_parameter_evidence"]:
        raise RuntimeError("verified original PostgreSQL source evidence unavailable")
    payload = {
        "schema": "sysinsight.postgresql.input.v1",
        "database": {"dbms": "postgresql", "version": "12", "name": "postgres",
                     "schema": "keeninsight_tpcc"},
        "collection": case_result["collection"], "workload": case["title"],
        "source_evidence": evidence,
        "function_anomalies": {"key_functions": detection["source_compare"]["key_functions"],
                               "matched_knobs": detection["source_match"]["matched_knob"]},
        "host_metrics": {}, "database_metrics": {},
        "ebpf_observations": {
            "normal_profile": detection["source_compare"]["normal_profile"],
            "anomaly_profile": str(anomaly), "full_differential_artifact": str(root / "source_detection.json"),
            "key_function_count": detection["source_compare"]["key_function_count"],
            "runtime_semantics": "real CPU stack sample rates; NOT function invocation counts",
            "differential_algorithm": detection["algorithm"],
            "anomaly_only_function_count_ignored_by_original": len(
                detection["source_compare"]["anomaly_only_functions_ignored_by_original"]),
        },
        "tuning_context": {"objective": {"metric": "normal_business_tps", "lower_is_better": False},
                           "baseline_metrics": {"tps": baseline_tps}, "allowed_knobs": allowed,
                           "knob_limits": {n: LIMITS[n] for n in allowed if n in LIMITS},
                           "current_configuration": current,
                           "safety": "isolated cluster; fixed pressure; preserve durability; restore"},
    }
    atomic_json(path, case_result)
    atomic_json(root / "sysinsight_input.json", payload)
    return path, root / "sysinsight_input.json", allowed, current


def invariant_state(admin):
    # Business values should roll back. WAL/tuple headers/sequence counters do
    # not roll back and are NOT claimed unchanged.
    queries = {
        "warehouse": "count(*)::text||':'||sum(w_ytd)::text",
        "district": "count(*)::text||':'||sum(d_ytd)::text||':'||sum(d_next_o_id)::text",
        "customer": "count(*)::text||':'||sum(c_balance)::text||':'||sum(c_payment_cnt)::text"
                    "||':'||sum(c_delivery_cnt)::text||':'||sum(c_ytd_payment)::text",
        "stock": "count(*)::text||':'||sum(s_quantity)::text||':'||sum(s_order_cnt)::text"
                 "||':'||sum(s_ytd)::text||':'||sum(s_remote_cnt)::text",
        "orders": "count(*)::text||':'||sum(coalesce(o_carrier_id,0))::text",
        "new_order": "count(*)::text||':'||sum(no_o_id)::text",
        "order_line": "count(*)::text||':'||sum(ol_quantity)::text||':'||sum(ol_amount)::text",
        "history": "count(*)::text||':'||sum(h_amount)::text",
    }
    return {table: admin.execute("SELECT " + expr + " FROM keeninsight_tpcc." + table, value=True)
            for table, expr in queries.items()}


def measure(args, case, root, server, connection, admin, name, settings=None, pressure=True):
    trials = []
    for index in range(args.measure_repeats):
        directory = root / (name + "-trial-" + str(index))
        directory.mkdir(mode=0o700)
        workers = None
        try:
            workers = Workers(case, directory, server, connection, admin, args.measure_seconds,
                              args.business_rate, args.pressure_rate, pressure, settings,
                              resident=args.resident)
            trials.append(workers.finish(timeout=args.measure_seconds + 15))
        finally:
            if workers:
                workers.close()
    result = {"status": "completed", "median_business_tps": statistics.median(
        trial["business"]["steady_tps"] for trial in trials), "trials": trials,
        "offered_pressure_tps": args.pressure_rate if pressure else 0,
        "offered_business_tps": args.business_rate, "detailed_profiling_enabled": False,
        "resident_enabled_identically_in_all_phases": bool(args.resident),
        "session_settings_delivery": "PGOPTIONS_once_per_connection",
        "scope": "original startup-excluded steady business TPS summary, not SQL latency"}
    atomic_json(root / (name + ".json"), result)
    return result


def gain_control(baseline, restored, tolerance=.05):
    """A conservative veto, NOT a statistical test or proof of causality."""
    before, after = baseline["median_business_tps"], restored["median_business_tps"]
    if not before or not math.isfinite(before) or not math.isfinite(after):
        return {"valid": False, "reason": "invalid_control_TPS"}
    drift = after / before - 1
    stable = abs(drift) <= tolerance + 1e-12
    return {"valid": stable, "restored_drift_ratio": drift,
            "max_abs_restored_drift_ratio": tolerance,
            "reason": "control_stable" if stable else "baseline_drift",
            "semantics": "necessary control, not confidence interval or perf/eBPF equivalence"}


def tune(args, case, root, server, connection, admin, api, allowed, baseline, journal):
    from sysinsight_live_chain import _candidate_and_validation
    from pg_temporary_config import TemporaryPostgresConfiguration
    from lab_controller import _tpcc_comparison
    validated = _candidate_and_validation(api, args, args)
    atomic_json(root / "candidate_validation.json", validated)
    before, _ = native_settings(admin, allowed)
    invariants = invariant_state(admin)
    ordered = list(validated["candidates"])
    selected = validated.get("selected_candidate")
    if selected:
        ordered.sort(key=lambda item: item["candidate_id"] != selected["candidate_id"])
    records, seen = [], set()
    for candidate in ordered:
        config = candidate["validation"].get("configuration", {})
        record = {"candidate_id": candidate["candidate_id"], "source": candidate["source"],
                  "configuration": config}
        if not candidate["validation"].get("valid") or candidate["live_validation"].get("status") != "completed":
            records.append({**record, "status": "invalid"})
            continue
        identity = json.dumps(config, sort_keys=True)
        if identity in seen:
            continue
        seen.add(identity)
        if not config or set(config) - set(allowed):
            records.append({**record, "status": "policy_rejected"})
            continue
        if any(name in LIMITS and not LIMITS[name][0] <= float(value) <= LIMITS[name][1]
               for name, value in config.items()):
            records.append({**record, "status": "policy_rejected"})
            continue
        applier = TemporaryPostgresConfiguration(args, config, "ebpf-equivalent", allow_restart=False)
        journal.stage("candidate_apply", candidate_id=candidate["candidate_id"])
        try:
            with applier as applied:
                if applied["skipped_configuration"] or applied["connection_configuration"]:
                    raise RuntimeError("candidate needs unsupported restart/connection change")
                tuned = measure(args, case, root, server, connection, admin,
                                candidate["candidate_id"] + "-tuned", applied["session_configuration"])
            restored, _ = native_settings(admin, allowed)
            if restored != before:
                raise RuntimeError("candidate settings restoration verification failed")
            restored_trial = measure(args, case, root, server, connection, admin,
                                     candidate["candidate_id"] + "-restored")
            equivalent = invariant_state(admin) == invariants
            if not equivalent:
                raise RuntimeError("business state invariant changed after candidate replay")
            gain = tuned["median_business_tps"] / baseline["median_business_tps"] - 1
            def comparison_phase(row):
                return {"business_tps_summary": {"steady_tps": row["median_business_tps"]},
                        "pressure_tps_summary": {"steady_tps": statistics.median(
                            trial["pressure"]["steady_tps"] for trial in row["trials"])},
                        "pressure_target_tps": row["offered_pressure_tps"]}
            comparison = _tpcc_comparison({"pressure_before": comparison_phase(baseline),
                                           "pressure_after": comparison_phase(tuned)})
            control = gain_control(baseline, restored_trial)
            accepted = equivalent and comparison["comparison_valid"] and control["valid"] and gain >= args.min_gain
            record.update(status="accepted" if accepted else
                          "inconclusive_measurement" if not control["valid"] else "no_validated_gain",
                          business_state_unchanged=equivalent, gain_ratio=gain, tuned=tuned,
                          restored=restored_trial, settings_restored=restored == before,
                          application=applier.state, original_TPS_comparison=comparison,
                          gain_control=control)
            journal.stage("candidate_retest_restore", candidate_id=candidate["candidate_id"],
                          result=record["status"], gain_ratio=gain, settings_restored=True,
                          business_state_unchanged=equivalent)
        except Exception as exc:
            record.update(status="candidate_failed", error=type(exc).__name__ + ": " + str(exc),
                          application=applier.state)
            if native_settings(admin, allowed)[0] != before:
                raise RuntimeError("restore failed after candidate error") from exc
        records.append(record)
        if sum("tuned" in item for item in records) >= args.max_candidates:
            break
    completed = [item for item in records if "tuned" in item]
    accepted = [item for item in completed if item["status"] == "accepted"]
    result = {"status": "tuned_and_restored" if accepted else
              "completed_without_validated_gain" if completed else "no_executable_candidate",
              "candidates": records, "pipeline_completed": bool(completed),
              "best": max(accepted, key=lambda item: item["gain_ratio"]) if accepted else None,
              "objective": "normal_business_tps_at_fixed_offered_external_pressure",
              "durability_unchanged": True, "persistent_deployment": False}
    atomic_json(root / "tuning_result.json", result)
    return result


def run_case(options, case, root, server, connection, admin):
    from db_profile import resolve_profile
    from sysinsight_live_chain import _api_result
    args = SimpleNamespace(**vars(options))
    args.host = connection[connection.index("-h") + 1]
    args.port = int(connection[connection.index("-p") + 1])
    args.db, args.db_user, args.run_as = "postgres", "postgres", "postgres"
    args.dbms, args.pg_version, args.pg_cluster, args.candidate_count = "postgresql", "12", "", 1
    directory = root / case["id"]
    directory.mkdir(mode=0o700)
    journal = Journal(directory)
    result = {"schema": "keeninsight.ebpf-functional-equivalence.v1", "id": case["id"],
              "status": "running", "uses_prometheus": False, "uses_perf_subprocess": False,
              "objective": "normal_business_tps", "historical_candidates_used": False,
              "scope": "original six-case TP tuning, not DREAM model/SQL rewriting",
              "original_SQL_hash": hashlib.sha256(
                  (ROOT / "app/tpcc_cases" / case["sql"]).read_bytes()).hexdigest()}
    args.resident = None
    try:
        args.resident = Resident(server, directory / "resident")
        checkpointer = int(admin.execute(
            "SELECT pid FROM pg_stat_activity WHERE backend_type='checkpointer'", value=True))
        args.resident.add([admin.pid, checkpointer])
        journal.stage("resident_monitoring", query_boundaries=True, CPU_sampling=False)
        normal = [collect_phase(args, case, directory, server, connection, admin,
                                "normal-" + str(index), False, journal)
                  for index in range(args.normal_windows)]
        normal_path = directory / "normal_profile.csv"
        normal_profile([row["counts"] for row in normal], normal_path)
        anomaly = collect_phase(args, case, directory, server, connection, admin,
                                "anomaly", True, journal)
        detection = compare(anomaly["counts"], normal_path, directory,
                            resolve_profile("postgresql", "12"), len(normal))
        journal.stage("original_differential_and_matching",
                      key_functions=detection["source_compare"]["key_function_count"],
                      matched_knobs=len(detection["source_match"]["matched_knob"]))
        initial = invariant_state(admin)
        baseline_normal = measure(args, case, directory, server, connection, admin,
                                  "business-baseline", pressure=False)
        baseline_pressure = measure(args, case, directory, server, connection, admin, "pressure-before")
        if invariant_state(admin) != initial:
            raise RuntimeError("original rollback-protected workload changed business state")
        case_path, input_path, allowed, current = build_model_input(
            args, directory, case, detection, baseline_pressure["median_business_tps"], admin)
        journal.stage("original_source_evidence", allowed_knobs=allowed)
        journal.stage("original_model", status="running", detail_probes_already_detached=True)
        api = _api_result(case_path, input_path, directory / "sysinsight_api", args, os.environ.copy())
        journal.stage("original_model", calls=len(api.get("llm_calls", [])), artifact=api["artifact"])
        tuning = tune(args, case, directory, server, connection, admin, api, allowed, baseline_pressure, journal)
        result.update(status=tuning["status"], pipeline_completed=tuning["pipeline_completed"],
                      tuning=tuning, baseline_normal=baseline_normal, baseline_pressure=baseline_pressure,
                      trigger=anomaly["trigger"], current_settings=current,
                      normal_sample_counts=[row["profile"]["sample_count"] for row in normal],
                      anomaly_sample_count=anomaly["profile"]["sample_count"])
        args.resident.snapshot()
        journal.stage("resident_resumed", detail_probes_stopped=True,
                      active_authorized_PIDs=list(args.resident.targets))
    except (Exception, KeyboardInterrupt) as exc:
        result.update(status="failed", pipeline_completed=False,
                      error=type(exc).__name__ + ": " + str(exc))
        journal.stage("failed", status="failed", error=result["error"])
    finally:
        if args.resident:
            result["resident_manifest"] = args.resident.close()
            journal.stage("resident_stopped", all_probes_detached=True)
        result["stages"] = journal.entries
        atomic_json(directory / "result.json", result)
    return result


def validate_options(args):
    for name in ("profile_seconds", "frequency", "normal_windows", "measure_seconds",
                 "measure_repeats", "max_candidates", "min_samples"):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            raise ValueError("positive finite " + name + " required")
    if not .1 <= args.profile_seconds <= 30 or not 1 <= args.frequency <= 1000:
        raise ValueError("capture budget exceeds equivalence lab limit")
    if not 1 <= args.normal_windows <= 5 or not 1 <= args.measure_repeats <= 5:
        raise ValueError("too many reference/trial repeats")
    if not 3 <= args.measure_seconds <= 60 or not 1 <= args.max_candidates <= 2:
        raise ValueError("trial duration or candidate count outside lab budget")
    if not 0 <= args.business_rate <= 2000 or not 1 <= args.pressure_rate <= 4000:
        raise ValueError("offered workload rate exceeds explicit lab budget")
    if not .1 <= args.trigger_timeout <= 30 or not 0 <= args.min_gain <= .5:
        raise ValueError("invalid trigger/gain budget")
    if not any(os.environ.get(name) for name in
               ("SYSINSIGHT_GPT_API_KEY", "SYSINSIGHT_API_KEY", "OPENAI_API_KEY")):
        raise ValueError("real original-model credentials required; no candidate fallback")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--pg-config", default="/usr/lib/postgresql/12/bin/pg_config")
    parser.add_argument("--scenario", choices=[case["id"] for case in original_cases()])
    parser.add_argument("--profile-seconds", type=float, default=15)
    parser.add_argument("--frequency", type=int, default=300)
    parser.add_argument("--normal-windows", type=int, default=2)
    parser.add_argument("--min-samples", type=int, default=100)
    parser.add_argument("--trigger-mode", choices=("activity", "selected"), default="activity")
    parser.add_argument("--trigger-timeout", type=float, default=10)
    parser.add_argument("--measure-seconds", type=int, default=12)
    parser.add_argument("--measure-repeats", type=int, default=2)
    parser.add_argument("--max-candidates", type=int, default=1)
    parser.add_argument("--min-gain", type=float, default=.05)
    parser.add_argument("--business-rate", type=float, default=1600)
    parser.add_argument("--pressure-rate", type=float, default=2500)
    parser.add_argument("--api-timeout", type=float, default=240)
    parser.add_argument("--api-base", default=os.environ.get("SYSINSIGHT_GPT_BASE_URL", ""))
    parser.add_argument("--model", default=os.environ.get("SYSINSIGHT_GPT_MODEL", ""))
    args = parser.parse_args(argv)
    validate_options(args)
    root = Path(args.output).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    with cluster(args.pg_config, max_connections=20) as (server, connection):
        with (root / "fixture.log").open("w") as log:
            subprocess.run(connection + ["--single-transaction", "-v", "warehouses=5", "-v", "seed=1",
                           "-f", str(ROOT / "scripts/tpcc_schema.sql")], stdout=log,
                           stderr=subprocess.STDOUT, check=True, timeout=180)
        admin = LibpqSession(connection)
        try:
            results = [run_case(args, case, root, server, connection, admin)
                       for case in original_cases() if not args.scenario or args.scenario == case["id"]]
        finally:
            admin.close()
    summary = {"status": "completed" if all(row.get("pipeline_completed") for row in results) else "incomplete",
               "cases": results, "pipeline_completed_count": sum(bool(row.get("pipeline_completed")) for row in results),
               "validated_gain_count": sum(row["status"] == "tuned_and_restored" for row in results),
               "no_production_database_or_service_changed": True,
               "functional_parity_scope": "fresh eBPF differential + original model + original TP TPS trials",
               "not_claimed": "equal gains, low overhead, restart-knob coverage or full-paper models"}
    atomic_json(root / "summary.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "cases"}, indent=2))
    raise SystemExit(0 if summary["status"] == "completed" else 1)


if __name__ == "__main__":
    main()
