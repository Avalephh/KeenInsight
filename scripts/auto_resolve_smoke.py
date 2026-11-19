#!/usr/bin/env python3
"""Verify the automatic generate -> deploy -> measure -> feedback -> rollback loop."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


BASE_URL = os.getenv("KI_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
OUTPUT = Path(os.getenv("KI_AUTO_RESOLVE_ARTIFACT", "artifacts/auto-resolve-strict.json"))


def request(path: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if payload is not None else {}
    req = urllib.request.Request(BASE_URL + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=300) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"{path} did not return an object")
    return result


def main() -> None:
    baseline = request(
        "/api/workloads/hash_join_spill/runs",
        method="POST",
        payload={
            "phase": "baseline",
            "repetitions": 3,
            "warmup_repetitions": 1,
            "parameters": {"baseline_work_mem": "64kB"},
        },
    )
    result = request(
        f"/api/workload-runs/{int(baseline['run_id'])}/auto-resolve",
        method="POST",
        payload={"rollback_after_success": True},
    )
    feedback = ((result.get("feedback") or {}).get("feedback") or {}).get("outcome") or {}
    action = result.get("action") or {}
    after = result.get("after") or {}
    resolution = result.get("resolution") or {}
    analysis = result.get("analysis") or {}
    source_evidence = analysis.get("source_evidence") or []
    code_knowledge = analysis.get("code_knowledge") or {}
    validated_code_knowledge = analysis.get("validated_code_knowledge") or []
    targeted_experiment = analysis.get("targeted_experiment") or {}
    plan_change = feedback.get("plan_change") or {}
    checks = {
        "automatic_mode": result.get("mode") == "automatic_closed_loop",
        "typed_action_generated": action.get("action_type") == "set_database_parameter",
        "automatic_execution_recorded": (action.get("validation") or {}).get("execution_mode") == "automatic_closed_loop",
        "rule_analysis_recorded": bool(analysis.get("rule_causes")) and bool(analysis.get("rule_evidence")),
        "grounded_knowledge_used": bool(analysis.get("retrieved_knowledge")),
        "code_knowledge_recorded": bool(code_knowledge.get("records"))
        and bool(code_knowledge.get("hypotheses")),
        "llvm_dataflow_evidence_used": any(
            row.get("relation_type") == "llvm_data_flow" for row in source_evidence
        ),
        "targeted_code_experiment_recorded": bool(targeted_experiment)
        and targeted_experiment.get("method")
        == "same recorded SQL and parameters before/after typed deployment"
        and targeted_experiment.get("baseline_run_id") == baseline.get("run_id")
        and targeted_experiment.get("after_run_id") == after.get("run_id")
        and targeted_experiment.get("result_preserved") is True,
        "code_knowledge_validated": bool(validated_code_knowledge)
        and any(
            (row.get("validation") or {}).get("validation_count", 0) >= 1
            for row in validated_code_knowledge
        ),
        "after_is_same_scenario": after.get("scenario") == "hash_join_spill" and after.get("phase") == "after",
        "feedback_effective": bool(feedback.get("effective")),
        "same_result": bool((feedback.get("measurement") or {}).get("result_comparable")),
        "hash_spill_reduced": float(plan_change.get("max_hash_batches_after") or 0) < float(plan_change.get("max_hash_batches_before") or 0),
        "automatic_rollback_verified": resolution.get("rollback_performed") is True and action.get("status") == "rolled_back",
    }
    artifact = {
        "format": "keeninsight-auto-resolve-strict-v1",
        "base_url": BASE_URL,
        "baseline": baseline,
        "result": result,
        "checks": checks,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(artifact, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"baseline_run_id": baseline.get("run_id"), "after_run_id": after.get("run_id"), "action_id": action.get("id"), "feedback": feedback, "checks": checks}, indent=2, ensure_ascii=False, default=str))
    print("Wrote automatic resolution artifact to", OUTPUT)
    if not all(checks.values()):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
