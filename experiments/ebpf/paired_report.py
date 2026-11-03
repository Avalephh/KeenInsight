"""Read-only audit of completed/partial paired TPS artifacts and input gaps."""
import argparse
from collections import Counter
import json
from pathlib import Path

from .collector import atomic_json
from .paired_tps import read
from .six_scenarios import original_cases


def metric_available(record):
    return (record.get("status") == "completed" and
            any(value.get("value") is not None for series in record.get("series", [])
                for value in series.get("values", [])))


def weighted_rate_gap(rows):
    """Compare two weightings of the SAME BPF stacks, not two workloads."""
    rates = {}
    for weight in ("sample_count", "sample_period_weight"):
        total = sum(row[weight] for row in rows)
        counts = Counter()
        for row in rows:
            for name in list(reversed(row["user_frames"])) + list(reversed(row["kernel_frames"])):
                if name not in ("[unknown]", "connection", "[mysqld]"):
                    counts[name] += row[weight]
        rates[weight] = {name: 100 * count / total for name, count in counts.items()} if total else {}
    names = set().union(*[set(value) for value in rates.values()])
    differences = sorted((
        {"function": name, "unweighted_percent": rates["sample_count"].get(name, 0),
         "period_weighted_percent": rates["sample_period_weight"].get(name, 0),
         "difference_percentage_points": rates["sample_period_weight"].get(name, 0) -
                                         rates["sample_count"].get(name, 0)}
        for name in names), key=lambda row: abs(row["difference_percentage_points"]), reverse=True)
    return {"same_stacks_weighting_only": True, "top_differences": differences[:20]}


def coverage(root):
    payload = read(root / "sysinsight_input.json")
    case = read(root / "case_result.json")
    from sysinsight_original_llm import compact_sysinsight_observation
    compact = compact_sysinsight_observation(payload)
    database = payload["database_metrics"]
    if "prometheus" in database:
        database = database["prometheus"]
    resource = case.get("resource_metrics", {})
    slow = case.get("slow_queries", [])
    phase = read(root / "anomaly/phase.json")
    collected = phase.get("after") or {}
    background = sorted(key for key in collected if key.startswith(("checkpoint", "buffers_backend")))
    result = {
        "database_metric_coverage": {key: metric_available(value) for key, value in database.items()},
        "database_metric_names_in_actual_model_context": sorted(compact["database_metrics"]),
        "resource_metrics_consumed": {key: resource.get(key) for key in
                                      ("cpu", "readIO", "writeIO", "virtualMem", "physical", "hit")},
        "resource_scope": resource.get("source"),
        "SQL_snapshot_rows": len(case.get("active_queries", [])),
        "SQL_wait_snapshots_forwarded_to_model": False,
        "SQL_statement_statistics_rows": len(slow),
        "SQL_statistic_fields": sorted(set().union(*(set(row) for row in slow))) if slow else [],
        "full_explain_plans_collected": False,
        "reference_bgwriter_counters_collected": background,
        "bgwriter_fields_in_compact_database_metrics": sorted(set(background) &
                                                             set(compact["database_metrics"])),
        "collected_but_not_forwarded_note": "the canonical compactor does not include full "
            "pg_stat_activity wait snapshots or the raw bgwriter snapshot; do not attribute a "
            "reference advantage to those fields without verifying an actual model input",
        "source_evidence_completed": payload["source_evidence"].get("status") == "completed",
        "source_runtime_chains": len(payload["source_evidence"].get("runtime_call_chains", [])),
        "source_static_records": len(payload["source_evidence"].get("static_parameter_evidence", [])),
        "SHAP_status": payload.get("shap_analysis", {}).get("status"),
        "matched_knobs": sorted(row["knob_name"] for row in
                                payload["function_anomalies"].get("matched_knobs", [])),
        "top_differential_functions": [row["Function"] for row in
                                       payload["function_anomalies"].get("key_functions", [])[:20]],
    }
    profile = root / "anomaly/profile"
    if (profile / "samples.json").exists():
        rows = read(profile / "samples.json")["rows"]
        result["BPF_weighting_audit"] = weighted_rate_gap(rows)
        result["BPF_capture"] = read(profile / "manifest.json")
    return result


def audit(root):
    cases = []
    for case in original_cases():
        directory = root / case["id"]
        sides = {}
        for arm in ("perf", "ebpf"):
            path = directory / arm
            if (path / "sysinsight_input.json").exists():
                sides[arm] = coverage(path)
        if len(sides) != 2:
            continue
        reference, bpf = sides["perf"], sides["ebpf"]
        missing = [key for key, present in reference["database_metric_coverage"].items()
                   if present and not bpf["database_metric_coverage"].get(key)]
        cases.append({
            "case": case["id"], "coverage": sides,
            "reference_database_global_records_missing_in_BPF_model": missing,
            "resource_prompt_unknowns_remaining": [key for key, value in
                bpf["resource_metrics_consumed"].items() if value is None],
            "SQL_statement_statistics_missing": bool(reference["SQL_statement_statistics_rows"]
                                                      and not bpf["SQL_statement_statistics_rows"]),
            "top20_function_overlap_count": len(set(reference["top_differential_functions"]) &
                                                set(bpf["top_differential_functions"])),
            "matched_knob_overlap_count": len(set(reference["matched_knobs"]) & set(bpf["matched_knobs"])),
            "causal_limit": "missing/different evidence identified; no claim that one missing field "
                            "alone caused a TPS gap without an ablation",
        })
    return {"cases": cases, "paired_input_count": len(cases),
            "scope": "actual input and model-compactor coverage; not an invented diagnosis or gain"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    args = parser.parse_args(argv)
    root = Path(args.input)
    result = audit(root)
    atomic_json(root / "input_gap_audit.json", result)
    print(json.dumps({"paired_input_count": result["paired_input_count"]}))


if __name__ == "__main__":
    main()
