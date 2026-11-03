"""eBPF-to-existing-SysInsight contract. Never manufacture perf samples."""
from collections import Counter
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

from .catalog import BY_NAME
from .collector import atomic_json

APP = Path(__file__).resolve().parents[2] / "app"


def canonical(sql):
    return str(sql).strip().rstrip(";").strip()


def diagnosis(events, target, replay_sql):
    def identity(event):
        return (event.get("pid"), event.get("process_start_ticks"), event.get("execution_id"))

    begins = {identity(e): e for e in events if e.get("type") == "query_begin"}
    completed = []
    seen = set()
    for end in events:
        if end.get("type") != "query_end":
            continue
        if end.get("pid") != target["pid"] or end.get("process_start_ticks") != target["start_ticks"]:
            continue
        key = identity(end)
        if not end.get("execution_id") or key in seen:
            continue
        begin = begins.get(key, {})
        prefix = canonical(begin.get("sql_prefix", ""))
        if not prefix or not canonical(replay_sql).startswith(prefix):
            continue
        seen.add(key)
        completed.append(end)
    counts = Counter()
    for event in completed:
        for name, metric in event.get("metrics", {}).items():
            if name in BY_NAME and event.get("probe_coverage", {}).get(name) == "attached":
                counts[BY_NAME[name].symbol] += int(metric.get("calls", 0))
    functions = [{"function": name, "calls": calls}
                 for name, calls in counts.most_common() if calls > 0]
    spill = counts["BufFileRead"] > 0 or counts["ExecHashJoinSaveTuple"] > 0
    return {"schema": "keeninsight.ebpf-diagnosis.v1",
            "status": "recognized" if spill else "insufficient_evidence",
            "anomaly_type": "temporary_file_materialization_or_spill" if spill else None,
            "cause_status": "memory_pressure_hypothesis_requires_source_matching_and_retest",
            "target": target, "completed_execution_ids": [e["execution_id"] for e in completed],
            "observed_functions": functions,
            "replay_sql_sha256": hashlib.sha256(canonical(replay_sql).encode()).hexdigest(),
            "replay_identity": "operator_supplied_SQL_plus_PID_birth_and_observed_sourceText_prefix",
            "identity_limit": "sourceText is truncated; prefix is not a proof of full SQL equality",
            "not_claimed": "perf stack, on-CPU percentages, physical IO, exact operator ID or causality"}


def build_input(directory, observed, trigger, sql, database, schema, allowed, current, baseline,
                knob_limits=None):
    directory = Path(directory)
    if observed["status"] != "recognized":
        raise ValueError("cannot tune without recognized eBPF evidence")
    sys.path.insert(0, str(APP))
    from db_profile import profile_summary, resolve_profile
    from sysinsight_source_evidence import build_source_evidence
    profile = resolve_profile("postgresql", "12")
    source_root = Path(__import__("os").environ["SYSINSIGHT_SOURCE_ROOT"])
    matcher_path = source_root / "DBTuner/utils/matchFunctions.py"
    spec = importlib.util.spec_from_file_location("keen_ebpf_original_matcher", matcher_path)
    matcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(matcher)
    functions_path = directory / "observed_functions.tsv"
    # The original reader consumes four numeric fields positionally. Names and
    # provenance explicitly state calls, not its legacy perf sample semantics.
    functions_path.write_text("Function\tEvent Calls\tObserved Call Score\tDirection\n" + "".join(
        "{}\t{}\t{}\t1\n".format(row["function"], row["calls"], row["calls"])
        for row in observed["observed_functions"]))
    names, knobs, mapping = matcher.find_top_and_matched_functions(
        str(functions_path), str(profile.path("association")))
    if not set(allowed) & {row["knob_name"] for row in knobs}:
        raise ValueError("original function-to-knob matcher found no allowed tuning knob")
    detection = {"status": "completed",
                 "collection": {"type": "ebpf", "semantics": "function invocation counts"},
                 "source_compare": {"status": "not_run_not_perf_samples",
                                    "key_function_file": str(functions_path),
                                    "key_function_count": len(observed["observed_functions"]),
                                    "key_functions": observed["observed_functions"]},
                 "source_match": {"bkFunctions_list": names, "matched_knob": knobs,
                                  "csv_func_to_knob": mapping, "source": str(matcher_path)}}
    case = {"mode": "live_alert", "workload": "operator_selected_read_only_SQL",
            "collection": {"type": "ebpf"}, "baseline_metrics": {"duration_ms": baseline},
            "before_settings": current, "sysinsight_source_detection": detection,
            "slow_queries": [{"replay_sql": sql}], "anomaly": {"trigger": trigger}}
    case_path = directory / "case_result.json"
    source = build_source_evidence(case, case_path, profile_summary(profile))
    source.setdefault("semantics", {})["runtime_observation"] = "ebpf_function_invocations"
    source["semantics"]["runtime_call_chain"] = "not_collected"
    if source.get("status") != "completed" or not source.get("static_parameter_evidence"):
        raise ValueError("verified PostgreSQL source evidence unavailable; do not tune blindly")
    result = {"schema": "sysinsight.postgresql.input.v1",
              "database": {"dbms": "postgresql", "version": "12", "name": database, "schema": schema},
              "workload": case["workload"], "collection": {"type": "ebpf",
                  "path": "resident_event_to_bounded_uprobes", "source_semantics": "invocation_counts"},
              "alert": {"triggered": True, "source": "ebpf", "alert_name": trigger["reason"]},
              "host_metrics": {}, "database_metrics": {},
              "ebpf_observations": observed, "source_evidence": source,
              "function_anomalies": {"function_count": len(observed["observed_functions"]),
                                    "key_functions": observed["observed_functions"],
                                    "matched_knobs": knobs},
              "tuning_context": {"allowed_knobs": list(allowed), "current_configuration": current,
                                 "knob_limits": knob_limits or {},
                                 "objective": {"metric": "read_only_sql_duration_ms", "lower_is_better": True},
                                 "baseline_metrics": case["baseline_metrics"],
                                 "safety": "read-only replay; session-local candidate; always restore; no DDL"}}
    for name, data in (("diagnosis.json", observed), ("source_detection.json", detection),
                       ("case_result.json", case), ("sysinsight_input.json", result)):
        atomic_json(directory / name, data)
    return case_path, directory / "sysinsight_input.json", detection
