"""Read-only audit of real hybrid stages, original inputs, and TPS outcomes."""
import argparse
import json
from pathlib import Path
import statistics

from .collector import atomic_json


def read(path):
    return json.loads(Path(path).read_text())


def audit_case(directory):
    directory = Path(directory)
    result = read(directory / "result.json")
    stages = read(directory / "workflow.json")["stages"]
    names = [row["stage"] for row in stages]
    row = {"id": result["id"], "status": result["status"],
           "collection_chain_completed": result.get("collection_chain_completed", False),
           "pipeline_completed": result.get("pipeline_completed", False),
           "error": result.get("error")}
    if not row["collection_chain_completed"]:
        return row
    payload = read(directory / "sysinsight_input.json")
    monitors = [read(path) for path in directory.rglob("supervisor.json")]
    sentinel = read(directory / "sentinel/manifest.json")
    transition = next((stage for stage in stages if stage["stage"] == "controlled_workload_transition"), None)
    alert = next((stage for stage in stages if stage["stage"] == "ebpf_alert"), None)
    metrics = payload.get("database_metrics", {})
    chains = payload["source_evidence"].get("runtime_call_chains", [])
    windows = [json.loads(line) for line in (directory / "sentinel/windows.jsonl").read_text().splitlines()]
    elapsed = windows[-1]["mono"] - windows[0]["mono"]
    healthy_count = len(read(directory / "sentinel-baseline.json")["windows"])
    healthy_first, healthy_last = windows[0], windows[healthy_count]
    healthy_seconds = healthy_last["mono"] - healthy_first["mono"]
    row.update(
        automatic_ebpf_alert=result["alert"]["source"] == "ebpf_native_PG12_received_statistics",
        canonical_alert_source=(payload.get("alert") or {}).get("source"),
        canonical_alert_matches_real_alarm=(
            (payload.get("alert") or {}).get("source") == result["alert"]["source"]),
        alert_rules=[signal["rule"] for signal in result["alert"]["signals"]],
        alert_after_workload_transition_seconds=(
            (alert["monotonic_ns"] - transition["monotonic_ns"]) / 1e9 if alert and transition else None),
        healthy_guard_alerts=result.get("monitor", {}).get("healthy_guard_alerts"),
        returns_to_resident=("resident_resumed" in names and "post_incident_resident_verified" in names),
        original_database_metric_records=len(metrics),
        database_metrics_collected=bool(metrics) and all(
            item.get("status") == "completed" and bool(item.get("series")) for item in metrics.values()),
        original_model_slow_SQL_rows=len(payload.get("tuning_context", {}).get("slow_queries", [])),
        original_perf_evidence=bool(chains) and all(chain.get("source") == "perf_folded_stack"
                                                   for chain in chains),
        perf_windows=len(monitors),
        all_perf_supervisors_completed=bool(monitors) and all(
            item.get("status") == "completed" and item.get("all_owned_processes_stopped") for item in monitors),
        all_sentinel_probes_detached=sentinel.get("all_probes_detached", False),
        resident_decode_errors=sentinel["final_counters"]["errors"],
        resident_recv_calls_per_second=(
            (windows[-1]["recv_calls"] - windows[0]["recv_calls"]) / elapsed if elapsed else None),
        resident_rate_note="includes time waiting for model/scoring; not an overhead measurement",
        healthy_recv_calls_per_second=(
            (healthy_last["recv_calls"] - healthy_first["recv_calls"]) / healthy_seconds),
    )
    if row["pipeline_completed"]:
        score = result["retest"]
        blocks = score["blocks"]
        row.update(
            actual_original_API_calls=result["API_call_count"],
            original_model_after_perf_stopped=(
                names.index("perf_burst_stopped") < names.index("original_model_and_TPS_retest_started")),
            gate_released_after_tuning=(
                names.index("original_model_and_TPS_retest_completed") < names.index("resident_resumed")),
            before_business_TPS=statistics.median(block["before"]["business"]["steady_tps"] for block in blocks),
            tuned_business_TPS=statistics.median(block["tuned"]["business"]["steady_tps"] for block in blocks),
            restored_business_TPS=statistics.median(block["restored"]["business"]["steady_tps"] for block in blocks),
            median_bracketed_gain_ratio=score["median_gain_ratio"],
            measurement_valid=score["measurement_valid"], validated_gain=score["validated_gain"],
            settings_restored=all(block["settings_restored"] for block in blocks),
            unchanged_business_aggregate=score["business_aggregate_unchanged"],
        )
    return row


def report(root):
    root = Path(root)
    cases = [audit_case(path.parent) for path in sorted(root.glob("*/result.json"))]
    return {
        "schema": "keeninsight.hybrid-audit.v1", "cases": cases,
        "collection_chain_completed_count": sum(row["collection_chain_completed"] for row in cases),
        "pipeline_completed_count": sum(row["pipeline_completed"] for row in cases),
        "validated_gain_count": sum(bool(row.get("validated_gain")) for row in cases),
        "same_TPS_benefit_proven": False, "sentinel_low_overhead_proven": False,
        "limits": "functional hybrid test, not a fresh randomized paired equivalence trial; "
                  "short TPS blocks on a shared host do not establish detector accuracy or overhead",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    args = parser.parse_args(argv)
    result = report(args.input)
    atomic_json(Path(args.input) / "hybrid_audit.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
