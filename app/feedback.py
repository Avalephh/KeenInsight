from __future__ import annotations

import json
import math
import re
from typing import Any

from .audit import record_audit
from .db import Database
from .knowledge import record_feedback_knowledge


class FeedbackError(ValueError):
    """The requested before/after comparison is not a valid measurement."""


MIN_FEEDBACK_REPETITIONS = 3
MIN_EFFECTIVE_IMPROVEMENT_PCT = 5.0


def record_action_feedback(
    db: Database,
    action_id: int,
    baseline_run_id: int,
    after_run_id: int,
) -> dict[str, Any]:
    """Compare two completed real workload runs for one applied action.

    The endpoint only accepts a workload run that was the source of the
    proposal and an explicit after-phase run from the same scenario.  The
    outcome is calculated from measured client timings and JSON plans; no
    scenario-specific expected number is stored or substituted.
    """
    action = db.fetch_one(
        """
        SELECT id, incident_id, action_type, target, description, status,
               before_state, after_state, rollback_state, validation,
               error, proposed_at, applied_at, rolled_back_at
        FROM keeninsight.tuning_actions WHERE id = %s
        """,
        (action_id,),
    )
    if not action:
        raise KeyError(f"action {action_id} was not found")
    if action["status"] not in {"applied", "completed", "rolled_back"}:
        raise FeedbackError(
            f"action {action_id} must have been applied before feedback; current status is {action['status']!r}"
        )

    source = (action.get("validation") or {}).get("proposal_source")
    if not isinstance(source, dict):
        raise FeedbackError("the action does not retain a measured workload source")
    source_type = source.get("type")
    # A system-level recommendation is derived from a differential probe and
    # LLVM evidence, but it still carries the exact overlapping workload run.
    # Accept that provenance as the baseline anchor so the full
    # differential->action->measurement->knowledge loop works for both the
    # original workload agent and the lower-level tuning path.
    if source_type == "workload_run":
        source_run_value = source.get("run_id")
    elif source_type == "function_differential_plus_llvm":
        source_run_value = source.get("workload_run_id")
    else:
        raise FeedbackError(
            "the action does not retain a supported measured workload source"
        )
    try:
        source_run_id = int(source_run_value)
    except (KeyError, TypeError, ValueError) as exc:
        raise FeedbackError("the action's measured workload source is invalid") from exc
    # Workload-origin actions are exact proposals: their baseline is the run
    # that generated the action.  A differential/LLVM recommendation carries
    # a measured source run as causal evidence, while the operator may collect
    # a fresh baseline immediately before applying it; the same-scenario and
    # ordering checks below still make that comparison reproducible.
    if source_type == "workload_run" and source_run_id != baseline_run_id:
        raise FeedbackError(
            f"baseline run {baseline_run_id} is not the run that produced action {action_id}"
        )

    runs = db.fetch_all(
        """
        SELECT id, scenario, phase, status, parameters, observations,
               started_at, finished_at, error
        FROM keeninsight.workload_runs
        WHERE id IN (%s, %s)
        """,
        (baseline_run_id, after_run_id),
    )
    by_id = {int(row["id"]): row for row in runs}
    baseline = by_id.get(baseline_run_id)
    after = by_id.get(after_run_id)
    if not baseline or not after:
        missing = [str(run_id) for run_id, row in ((baseline_run_id, baseline), (after_run_id, after)) if not row]
        raise FeedbackError(f"workload run(s) not found: {', '.join(missing)}")
    if baseline["status"] != "completed" or after["status"] != "completed":
        raise FeedbackError("both workload runs must be completed")
    if baseline["scenario"] != after["scenario"]:
        raise FeedbackError("baseline and after runs must use the same scenario")
    if (baseline.get("parameters") or {}) != (after.get("parameters") or {}):
        raise FeedbackError(
            "baseline and after runs must use identical workload and measurement parameters"
        )
    baseline_query = (baseline.get("observations") or {}).get("query")
    after_query = (after.get("observations") or {}).get("query")
    if baseline_query != after_query:
        raise FeedbackError("baseline and after runs must execute the identical SQL")
    if baseline["phase"] != "baseline" or after["phase"] != "after":
        raise FeedbackError("feedback requires a baseline run and an after run")
    source_scenario = source.get("scenario") or source.get("workload_scenario")
    if source_scenario and source_scenario != baseline["scenario"]:
        raise FeedbackError("the workload source scenario does not match the compared runs")
    if action.get("applied_at") and baseline.get("finished_at"):
        if baseline["finished_at"] > action["applied_at"]:
            raise FeedbackError("the baseline run must finish before the action is applied")
    if action.get("applied_at") and after.get("started_at"):
        if after["started_at"] < action["applied_at"]:
            raise FeedbackError("the after run must start after the action is applied")
    if action.get("rolled_back_at") and after.get("started_at"):
        if after["started_at"] >= action["rolled_back_at"]:
            raise FeedbackError("the after run must start before the action is rolled back")
    if action.get("rolled_back_at") and after.get("finished_at"):
        if after["finished_at"] > action["rolled_back_at"]:
            raise FeedbackError("the after run must finish before the action is rolled back")

    outcome = evaluate_runs(action, baseline, after)
    root_cause = _root_cause(action, baseline, outcome)

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, action_id, baseline_run_id, after_run_id, outcome, created_at
                FROM keeninsight.tuning_feedback
                WHERE action_id = %s AND baseline_run_id = %s AND after_run_id = %s
                """,
                (action_id, baseline_run_id, after_run_id),
            )
            existing = cur.fetchone()
            if existing:
                return {
                    "feedback": existing,
                    "action": action,
                    "knowledge": _feedback_knowledge_for_action(
                        db, action, baseline["scenario"]
                    ),
                    "idempotent": True,
                }

            cur.execute(
                """
                INSERT INTO keeninsight.tuning_feedback
                    (action_id, baseline_run_id, after_run_id, outcome)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (action_id, baseline_run_id, after_run_id) DO NOTHING
                RETURNING id, action_id, baseline_run_id, after_run_id, outcome, created_at
                """,
                (action_id, baseline_run_id, after_run_id, json.dumps(outcome, default=str)),
            )
            feedback = cur.fetchone()
            if not feedback:
                cur.execute(
                    """
                    SELECT id, action_id, baseline_run_id, after_run_id, outcome, created_at
                    FROM keeninsight.tuning_feedback
                    WHERE action_id = %s AND baseline_run_id = %s AND after_run_id = %s
                    """,
                    (action_id, baseline_run_id, after_run_id),
                )
                feedback = cur.fetchone()
                if not feedback:
                    raise RuntimeError("could not persist tuning feedback")
                return {
                    "feedback": feedback,
                    "action": action,
                    "knowledge": _feedback_knowledge_for_action(
                        db, action, baseline["scenario"]
                    ),
                    "idempotent": True,
                }

            validation = dict(action.get("validation") or {})
            validation["latest_feedback"] = {
                "feedback_id": feedback["id"],
                "baseline_run_id": baseline_run_id,
                "after_run_id": after_run_id,
                "effective": bool(outcome.get("effective")),
                "observed_effect": outcome.get("observed_effect"),
                "median_improvement_pct": outcome.get("median_improvement_pct"),
                "p95_improvement_pct": outcome.get("p95_improvement_pct"),
            }
            cur.execute(
                """
                UPDATE keeninsight.tuning_actions
                SET validation = %s::jsonb
                WHERE id = %s
                """,
                (json.dumps(validation, default=str), action_id),
            )

    knowledge = record_feedback_knowledge(
        db,
        root_cause=root_cause,
        scenario=baseline["scenario"],
        action=action,
        outcome={**outcome, "root_cause": root_cause},
        baseline_run_id=baseline_run_id,
        after_run_id=after_run_id,
    )
    record_audit(
        db,
        "action_feedback_recorded",
        "tuning_action",
        action_id,
        {
            "feedback_id": feedback["id"],
            "baseline_run_id": baseline_run_id,
            "after_run_id": after_run_id,
            "root_cause": root_cause,
            "effective": outcome.get("effective"),
            "observed_effect": outcome.get("observed_effect"),
        },
    )
    return {"feedback": feedback, "action": get_action_for_feedback(db, action_id), "knowledge": knowledge}


def list_action_feedback(
    db: Database, action_id: int, *, limit: int = 50
) -> list[dict[str, Any]]:
    return db.fetch_all(
        """
        SELECT id, action_id, baseline_run_id, after_run_id, outcome, created_at
        FROM keeninsight.tuning_feedback
        WHERE action_id = %s
        ORDER BY created_at DESC
        LIMIT %s
        """,
        (action_id, max(1, min(limit, 200))),
    )


def evaluate_runs(
    action: dict[str, Any], baseline: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    before_observations = baseline.get("observations") or {}
    after_observations = after.get("observations") or {}
    baseline_median = _required_number(before_observations, "median_query_time_ms")
    after_median = _required_number(after_observations, "median_query_time_ms")
    baseline_p95 = _required_number(before_observations, "p95_query_time_ms")
    after_p95 = _required_number(after_observations, "p95_query_time_ms")
    before_plan = _plan_features(before_observations)
    after_plan = _plan_features(after_observations)
    plan_change = _plan_change(before_plan, after_plan)

    median_improvement_pct = _improvement_pct(baseline_median, after_median)
    p95_improvement_pct = _improvement_pct(baseline_p95, after_p95)
    timing_improved = after_median < baseline_median
    p95_improved = after_p95 < baseline_p95
    baseline_repetitions = len(before_observations.get("timings_ms") or [])
    after_repetitions = len(after_observations.get("timings_ms") or [])
    enough_repetitions = min(baseline_repetitions, after_repetitions) >= MIN_FEEDBACK_REPETITIONS
    result_before = before_observations.get("result")
    result_after = after_observations.get("result")
    result_present = result_before is not None and result_after is not None
    result_comparable = result_present and result_before == result_after
    action_type = action.get("action_type")
    target = action.get("target") or {}
    effectiveness_criterion = "latency_and_verified_plan_signal"
    intervention_verified = True
    intervention_reason = "the action target was observed in the after-phase execution context"

    if action_type == "create_index":
        target_index = target.get("index")
        index_observed_before = target_index in before_plan["index_names"]
        index_observed_after = target_index in after_plan["index_names"]
        verified_plan_change = index_observed_after and not index_observed_before
        plan_reason = "target index appears in the after-phase PostgreSQL plan"
    elif action_type == "set_database_parameter" and target.get("parameter") == "work_mem":
        observed_value = after_observations.get("phase_work_mem")
        intervention_verified = _memory_values_equal(observed_value, target.get("value"))
        intervention_reason = (
            "after-phase SHOW work_mem matches the action target"
            if intervention_verified
            else "after-phase SHOW work_mem does not match the action target"
        )
        hash_batches_decreased = (
            before_plan["max_hash_batches"] > after_plan["max_hash_batches"]
        )
        temporary_blocks_resolved = (
            before_plan["has_temp_blocks"] and not after_plan["has_temp_blocks"]
        )
        verified_plan_change = intervention_verified and (
            hash_batches_decreased or temporary_blocks_resolved
        )
        if not intervention_verified:
            plan_reason = intervention_reason
        elif hash_batches_decreased:
            plan_reason = "observed multi-batch hash join changed to fewer batches"
        elif baseline.get("scenario") == "aggregate_spill" and temporary_blocks_resolved:
            plan_reason = "aggregate changed from sorted/temp-spilling to hashed/no-temp"
        elif baseline.get("scenario") == "sort_spill" and temporary_blocks_resolved:
            plan_reason = "window sort changed from external disk sort to in-memory sort"
        else:
            plan_reason = "observed temporary-block usage decreased"
    elif (
        action_type == "set_database_parameter"
        and target.get("parameter") == "max_parallel_workers_per_gather"
    ):
        observed_workers = _integer_value(after_observations.get("phase_parallel_workers"))
        target_workers = _integer_value(target.get("value"))
        intervention_verified = (
            observed_workers is not None
            and target_workers is not None
            and observed_workers == target_workers
        )
        intervention_reason = (
            "after-phase SHOW max_parallel_workers_per_gather matches the action target"
            if intervention_verified
            else "after-phase worker setting does not match the action target"
        )
        verified_plan_change = intervention_verified and (
            after_plan["max_workers_launched"] > 0
            and after_plan["max_workers_launched"] > before_plan["max_workers_launched"]
        )
        plan_reason = (
            "EXPLAIN ANALYZE reported actual parallel workers in the after phase"
            if intervention_verified
            else intervention_reason
        )
    elif action_type == "refresh_statistics":
        effectiveness_criterion = "estimate_accuracy_and_result_preservation"
        verified_plan_change = (
            before_plan["estimate_error_ratio"] >= 2.0
            and after_plan["estimate_error_ratio"] < before_plan["estimate_error_ratio"]
            and after_plan["estimate_error_ratio"] <= 2.0
        )
        plan_reason = (
            "ANALYZE reduced the observed estimated/actual row-count error on "
            "the same query"
        )
    else:
        verified_plan_change = False
        plan_reason = "no evaluator is registered for this typed action"

    if not result_present:
        observed_effect = "missing_result"
    elif not result_comparable:
        observed_effect = "result_changed"
    elif verified_plan_change and not enough_repetitions:
        observed_effect = "insufficient_repetitions"
    elif action_type == "refresh_statistics" and verified_plan_change:
        observed_effect = "effective"
    elif verified_plan_change and (
        median_improvement_pct >= MIN_EFFECTIVE_IMPROVEMENT_PCT
        and p95_improvement_pct >= MIN_EFFECTIVE_IMPROVEMENT_PCT
    ):
        observed_effect = "effective"
    elif verified_plan_change:
        observed_effect = "plan_changed_timing_inconclusive"
    elif not timing_improved and not p95_improved:
        observed_effect = "no_verified_effect_or_regression"
    else:
        observed_effect = "timing_improved_without_expected_plan_change"

    return {
        "scenario": baseline["scenario"],
        "action_type": action_type,
        "target": target,
        "baseline_parameters": baseline.get("parameters") or {},
        "after_parameters": after.get("parameters") or {},
        "effective": observed_effect == "effective",
        "observed_effect": observed_effect,
        "measurement": {
            "baseline_repetitions": baseline_repetitions,
            "after_repetitions": after_repetitions,
            "minimum_repetitions_for_effective": MIN_FEEDBACK_REPETITIONS,
            "minimum_improvement_pct_for_effective": MIN_EFFECTIVE_IMPROVEMENT_PCT,
            "effectiveness_criterion": effectiveness_criterion,
            "latency_threshold_applied": action_type != "refresh_statistics",
            "enough_repetitions": enough_repetitions,
            "result_present": result_present,
            "result_comparable": result_comparable,
            "intervention_verified": intervention_verified,
            "intervention_reason": intervention_reason,
            "baseline_median_query_time_ms": baseline_median,
            "after_median_query_time_ms": after_median,
            "baseline_p95_query_time_ms": baseline_p95,
            "after_p95_query_time_ms": after_p95,
            "timing_improved": timing_improved,
            "p95_improved": p95_improved,
            "median_improvement_pct": median_improvement_pct,
            "p95_improvement_pct": p95_improvement_pct,
            "baseline_timing_summary": before_observations.get("timing_summary"),
            "after_timing_summary": after_observations.get("timing_summary"),
            "baseline_server_execution_time_ms": before_observations.get("server_execution_time_ms"),
            "after_server_execution_time_ms": after_observations.get("server_execution_time_ms"),
            "baseline_query_measurement": before_observations.get("query_measurement"),
            "after_query_measurement": after_observations.get("query_measurement"),
        },
        "median_improvement_pct": median_improvement_pct,
        "p95_improvement_pct": p95_improvement_pct,
        "plan_change": {
            **plan_change,
            "verified_plan_change": verified_plan_change,
            "reason": plan_reason,
        },
        "plan_before": before_plan,
        "plan_after": after_plan,
    }


def get_action_for_feedback(db: Database, action_id: int) -> dict[str, Any] | None:
    return db.fetch_one(
        """
        SELECT id, incident_id, action_type, target, description, status,
               before_state, after_state, rollback_state, validation,
               error, proposed_at, applied_at, rolled_back_at
        FROM keeninsight.tuning_actions WHERE id = %s
        """,
        (action_id,),
    )


def _feedback_knowledge_for_action(
    db: Database, action: dict[str, Any], scenario: str
) -> dict[str, Any] | None:
    row = db.fetch_one(
        """
        SELECT k.id, k.source_type, k.title, k.root_cause, k.context,
               k.action_type, k.action_template, k.provenance, k.verified,
               k.confidence, k.feedback_count, k.success_count, k.failure_count,
               k.last_feedback_at, k.updated_at, k.created_at
        FROM keeninsight.knowledge_entries k
        WHERE k.source_type = 'tuning-feedback'
          AND k.title = %s
        ORDER BY k.updated_at DESC LIMIT 1
        """,
        (f"Measured {action.get('action_type')} outcome for {scenario}",),
    )
    return row


def _root_cause(
    action: dict[str, Any], baseline: dict[str, Any], outcome: dict[str, Any]
) -> str:
    action_type = action.get("action_type")
    before = outcome.get("plan_before") or {}
    if action_type == "create_index" and "Seq Scan" in (before.get("scan_nodes") or []):
        return "filtered_sequential_scan"
    if action_type == "refresh_statistics":
        return "stale_statistics"
    if action_type == "set_database_parameter" and baseline["scenario"] in {
        "aggregate_spill",
        "sort_spill",
    }:
        return baseline["scenario"]
    if action_type == "set_database_parameter" and (
        float(before.get("max_hash_batches") or 0) > 1 or before.get("has_temp_blocks")
    ):
        return "hash_join_spill"
    if action_type == "set_database_parameter" and (
        action.get("target") or {}
    ).get("parameter") == "max_parallel_workers_per_gather":
        return "parallel_query_underprovisioned"
    return f"workload:{baseline['scenario']}"


def _plan_features(observations: dict[str, Any]) -> dict[str, Any]:
    summary = observations.get("plan_summary") or {}
    plan = observations.get("plan") or {}
    nodes = plan.get("nodes") or []
    index_names = sorted(
        {
            str(node.get("Index Name"))
            for node in nodes
            if node.get("Index Name")
        }
    )
    hash_values = [
        _number(node.get("Hash Batches", node.get("Batches"))) or 0.0
        for node in nodes
        if node.get("Node Type") == "Hash"
    ]
    temp = any(
        (_number(node.get("Temp Read Blocks")) or 0.0) > 0
        or (_number(node.get("Temp Written Blocks")) or 0.0) > 0
        for node in nodes
    )
    return {
        "root_node": summary.get("root_node") or (plan.get("root") or {}).get("Node Type"),
        "scan_nodes": list(summary.get("scan_nodes") or []),
        "join_nodes": list(summary.get("join_nodes") or []),
        "has_temp_blocks": bool(summary.get("has_temp_blocks")) or temp,
        "max_hash_batches": max(hash_values, default=0.0),
        "sort_methods": list(
            dict.fromkeys(
                str(node.get("Sort Method"))
                for node in nodes
                if node.get("Sort Method")
            )
        ),
        "sort_space_types": list(
            dict.fromkeys(
                str(node.get("Sort Space Type"))
                for node in nodes
                if node.get("Sort Space Type")
            )
        ),
        "aggregate_strategies": list(
            dict.fromkeys(
                str(node.get("Strategy"))
                for node in nodes
                if node.get("Node Type") in {"Aggregate", "HashAggregate"}
                and node.get("Strategy")
            )
        ),
        "index_names": index_names,
        "max_workers_planned": max(
            (_number(node.get("Workers Planned")) or 0.0 for node in nodes),
            default=0.0,
        ),
        "max_workers_launched": max(
            (_number(node.get("Workers Launched")) or 0.0 for node in nodes),
            default=0.0,
        ),
        "estimate_error_ratio": _estimate_error_ratio(nodes),
    }


def _estimate_error_ratio(nodes: list[dict[str, Any]]) -> float:
    ratios: list[float] = []
    for node in nodes:
        estimated = _number(node.get("Plan Rows"))
        actual_rows = _number(node.get("Actual Rows"))
        if estimated is None or actual_rows is None or estimated < 0 or actual_rows < 0:
            continue
        loops = max(1.0, _number(node.get("Actual Loops")) or 1.0)
        actual = actual_rows * loops
        ratios.append(max((actual + 1.0) / (estimated + 1.0), (estimated + 1.0) / (actual + 1.0)))
    return max(ratios, default=0.0)


def _plan_change(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    added_indexes = sorted(set(after["index_names"]) - set(before["index_names"]))
    removed_indexes = sorted(set(before["index_names"]) - set(after["index_names"]))
    return {
        "changed": any(
            (
                before["root_node"] != after["root_node"],
                before["scan_nodes"] != after["scan_nodes"],
                before["join_nodes"] != after["join_nodes"],
                before["has_temp_blocks"] != after["has_temp_blocks"],
                before["max_hash_batches"] != after["max_hash_batches"],
                before["sort_methods"] != after["sort_methods"],
                before["sort_space_types"] != after["sort_space_types"],
                before["aggregate_strategies"] != after["aggregate_strategies"],
                before["max_workers_planned"] != after["max_workers_planned"],
                before["max_workers_launched"] != after["max_workers_launched"],
                before["estimate_error_ratio"] != after["estimate_error_ratio"],
                added_indexes,
                removed_indexes,
            )
        ),
        "root_node_before": before["root_node"],
        "root_node_after": after["root_node"],
        "scan_nodes_before": before["scan_nodes"],
        "scan_nodes_after": after["scan_nodes"],
        "join_nodes_before": before["join_nodes"],
        "join_nodes_after": after["join_nodes"],
        "has_temp_blocks_before": before["has_temp_blocks"],
        "has_temp_blocks_after": after["has_temp_blocks"],
        "max_hash_batches_before": before["max_hash_batches"],
        "max_hash_batches_after": after["max_hash_batches"],
        "sort_methods_before": before["sort_methods"],
        "sort_methods_after": after["sort_methods"],
        "sort_space_types_before": before["sort_space_types"],
        "sort_space_types_after": after["sort_space_types"],
        "aggregate_strategies_before": before["aggregate_strategies"],
        "aggregate_strategies_after": after["aggregate_strategies"],
        "max_workers_planned_before": before["max_workers_planned"],
        "max_workers_planned_after": after["max_workers_planned"],
        "max_workers_launched_before": before["max_workers_launched"],
        "max_workers_launched_after": after["max_workers_launched"],
        "estimate_error_ratio_before": before["estimate_error_ratio"],
        "estimate_error_ratio_after": after["estimate_error_ratio"],
        "indexes_added": added_indexes,
        "indexes_removed": removed_indexes,
    }


def _required_number(observations: dict[str, Any], name: str) -> float:
    value = _number(observations.get(name))
    if value is None or value < 0:
        raise FeedbackError(f"workload observations do not contain a valid {name}")
    return value


def _improvement_pct(before: float, after: float) -> float | None:
    if before <= 0:
        return None
    return (before - after) / before * 100.0


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer_value(value: Any) -> int | None:
    try:
        text = str(value).strip()
        if not text or any(character in text for character in ".eE"):
            return None
        return int(text, 10)
    except (TypeError, ValueError):
        return None


def _memory_values_equal(first: Any, second: Any) -> bool:
    """Compare PostgreSQL SHOW memory values after normalizing their units."""
    first_bytes = _memory_bytes(first)
    second_bytes = _memory_bytes(second)
    return first_bytes is not None and first_bytes == second_bytes


def _memory_bytes(value: Any) -> int | None:
    if value is None:
        return None
    match = re.fullmatch(
        r"\s*([0-9]+(?:\.[0-9]+)?)\s*(b|kb|mb|gb|tb)?\s*",
        str(value),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    number = float(match.group(1))
    multiplier = {
        "b": 1,
        "kb": 1024,
        "mb": 1024**2,
        "gb": 1024**3,
        "tb": 1024**4,
    }.get((match.group(2) or "b").lower())
    if multiplier is None or not math.isfinite(number):
        return None
    return int(number * multiplier)
