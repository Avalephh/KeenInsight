"""Offline acceptance of genuine equivalence-lab artifacts; never calls API."""
import argparse
import json
from pathlib import Path

from .collector import atomic_json
from .six_scenarios import original_cases


def read(path):
    return json.loads(path.read_text())


def verify_case(root):
    result = read(root / "result.json")
    stages = result["stages"]
    normal = sorted(root.glob("normal-*/profile/manifest.json"))
    profiles = [read(path) for path in normal] + [read(root / "anomaly/profile/manifest.json")]
    detection = read(root / "source_detection.json")
    api = read(root / "sysinsight_api/result.json")
    records = result.get("tuning", {}).get("candidates", [])
    executed = [row for row in records if row.get("settings_restored")
                and row.get("business_state_unchanged") and "restored" in row and "tuned" in row]
    model_started = min(s["monotonic_ns"] for s in stages if s["stage"] == "original_model")
    detached = [s["monotonic_ns"] for s in stages if s["stage"].endswith("_detail_detached")
                or s["stage"] == "targeted_resource_probes_detached"]
    trial_policies = [trial["pressure_policy"] for row in executed
                      for phase in (row["tuned"], row["restored"], result["baseline_pressure"])
                      for trial in phase["trials"]]
    context = read(root / "sysinsight_input.json")["tuning_context"]
    checks = {
        "real_normal_and_anomaly_profiles": len(normal) >= 1 and all(
            p["status"] == "completed" and p["sample_count"] > 0 for p in profiles),
        "same_capture_event_frequency_window": len({
            (p["sample_event"], p["frequency_hz_per_task"], p["requested_seconds"],
             p["binary_sha256"]) for p in profiles}) == 1,
        "all_task_sampling_FDs_closed": all(p.get("all_probe_FDs_closed") for p in profiles),
        "all_diagnostic_probes_stopped_before_model": bool(detached) and max(detached) < model_started,
        "normal_and_anomaly_targets_not_reduced_to_one_PID": all(len(p["targets"]) >= 2 for p in profiles),
        "original_differential_and_function_matching": detection["algorithm"].startswith(
            "unmodified original") and detection["source_compare"]["key_function_count"] > 0,
        "original_model_called_with_fresh_API_evidence": bool(api.get("llm_calls")),
        "original_candidate_applied_retested_and_restored": bool(executed) and all(
            row["source"] == "original_LLM_ACQ_parser" for row in executed),
        "same_offered_pressure_and_client_policy": bool(trial_policies) and all(
            policy == trial_policies[0] for policy in trial_policies),
        "business_TPS_not_SQL_duration_or_pressure_TPS": context["objective"]["metric"] == "normal_business_tps"
            and all("steady_summary" in trial["business"] for row in executed
                    for phase in (row["tuned"], row["restored"]) for trial in phase["trials"]),
        "original_pressure_comparison_ran": all("original_TPS_comparison" in row for row in executed),
        "no_durability_or_endpoint_tuning": all(not set(row["configuration"]) & {
            "fsync", "synchronous_commit", "full_page_writes", "port", "unix_socket_directories"}
            for row in executed),
        "resident_outlives_model_and_resumes": any(s["stage"] == "resident_resumed" for s in stages)
            and result["resident_manifest"]["stopped_monotonic_ns"] > model_started,
        "resident_cleanup_completed": result["resident_manifest"]["status"] == "completed"
            and result["resident_manifest"].get("all_probes_detached") is True,
        "no_Prometheus_perf_subprocess_or_old_candidate": not result["uses_prometheus"]
            and not result["uses_perf_subprocess"] and not result["historical_candidates_used"],
        "pipeline_completion_not_confused_with_numerical_gain": result.get("pipeline_completed") is True
            and result["status"] in {"tuned_and_restored", "completed_without_validated_gain"},
    }
    return {"id": result["id"], "status": "passed" if all(checks.values()) else "failed",
            "checks": checks, "pipeline_result": result["status"]}


def verify(root, expected=None):
    root = Path(root)
    expected = expected or [case["id"] for case in original_cases()]
    rows = []
    for identity in expected:
        try:
            rows.append(verify_case(root / identity))
        except (OSError, KeyError, ValueError) as exc:
            rows.append({"id": identity, "status": "failed",
                         "error": type(exc).__name__ + ": " + str(exc)})
    result = {"status": "passed" if all(row["status"] == "passed" for row in rows) else "failed",
              "cases": rows, "passed_count": sum(row["status"] == "passed" for row in rows),
              "scope": "functional chain acceptance, NOT equal numerical gain or low overhead"}
    atomic_json(root / "acceptance.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--scenario", choices=[case["id"] for case in original_cases()])
    args = parser.parse_args()
    result = verify(args.input, [args.scenario] if args.scenario else None)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
