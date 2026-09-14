#!/usr/bin/env python3
"""Run one real DREAM diagnosis/measurement cycle for a live SQL observation.

The normal DREAM CLI is workload-file oriented and keeps its best action in
an in-memory loop.  The bridge needs one asynchronous job at a time, so this
adapter reuses the same ``DBAgent -> Planner -> ActionManager`` objects but
exposes a single-query JSON contract.  It deliberately does not install a
database-global change; the caller decides whether a validated plan hint is
safe to publish to ``hint_plan.hints``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parent
DREAM_ROOT = ROOT.parent / "dream"
sys.path.insert(0, str(DREAM_ROOT))

from dream.agent.db_agent import DBAgent  # noqa: E402
from dream.utils.types import QueryInfo  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--sql", required=True)
    parser.add_argument("--query-id", default="live")
    parser.add_argument("--queryid", default="")
    parser.add_argument("--mean-time-ms", type=float, default=0.0)
    parser.add_argument("--max-time-ms", type=float, default=0.0)
    parser.add_argument("--calls", type=int, default=1)
    parser.add_argument("--plan-json", default="")
    parser.add_argument("--root-cause", default="")
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def _read_json(path: str) -> Any:
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def _plan_or_fallback(value: Any) -> Any:
    if value:
        return value
    # This is only a last-resort shape for diagnostics.  A real plan is
    # normally obtained by PostgresDB.get_plan before the model is called.
    return [{"Plan": {"Node Type": "Result", "Startup Cost": 0, "Total Cost": 0, "Plan Rows": 1, "Plan Width": 0}}]


def _internal_metrics(row: Dict[str, Any], duration_s: float) -> List[float]:
    return [
        float(row.get("rows", 0) or 0),
        float(row.get("shared_blks_hit", 0) or 0),
        float(row.get("shared_blks_read", 0) or 0),
        float(row.get("rows", 0) or 0),
        0.0,
        0.0,
        0.0,
        0.0,
        float(row.get("shared_blks_hit", 0) or 0),
        float(row.get("shared_blks_read", 0) or 0),
        0.0,
        0.0,
        duration_s,
    ]


def _external_metrics() -> List[List[float]]:
    # The live collector has aggregate statement statistics rather than a
    # per-query host time series.  Keep the tensor contract real and explicit;
    # the SysInsight/Prometheus observation is persisted beside this job.
    return [[0.0] * 9 for _ in range(7)]


def _is_read_only(sql: str) -> bool:
    stripped = sql.lstrip().lower()
    return stripped.startswith(("select", "with", "explain")) and not any(
        token in stripped[:200]
        for token in ("insert ", "update ", "delete ", "merge ", "create ", "drop ", "alter ")
    )


async def run_one(args: argparse.Namespace) -> Dict[str, Any]:
    configs = json.loads(Path(args.config).read_text(encoding="utf-8"))
    database_config = configs.get("DATABASE_CONFIG", {})
    sql = args.sql.strip()
    if not sql:
        raise ValueError("empty SQL")

    duration_s = max(0.001, float(args.mean_time_ms) / 1000.0)
    plan_json: Any = _read_json(args.plan_json)
    row = {
        "rows": 0,
        "shared_blks_hit": 0,
        "shared_blks_read": 0,
    }

    async with DBAgent(configs=configs) as agent:
        if plan_json is None:
            plan_json = agent.db.get_plan(sql)
        plan_json = _plan_or_fallback(plan_json)
        query_info = QueryInfo(
            query_id=args.query_id,
            query=sql,
            plan_json=plan_json,
            internal_metrics=_internal_metrics(row, duration_s),
            external_metrics=_external_metrics(),
            execution_time=duration_s,
            is_rewrite=False,
        )

        state: Dict[str, Any] = {
            "root_tried": set(),
            "current_root": None,
            "mode": "exploit",
            "confidence": {},
            "attempts": {},
            "successes": {},
            "component_attempts": {},
        }
        if args.root_cause:
            root_causes: Any = [item.strip() for item in args.root_cause.split(",") if item.strip()]
            state["current_root"] = root_causes
            predicted_explanation = "root cause supplied by bridge configuration"
        else:
            predicted_root, state, predicted_explanation = await agent.planner.predict(
                query_info, state, agent.memory_manager
            )
            root_causes = predicted_root

        if isinstance(root_causes, str):
            root_causes = [root_causes]
        if not root_causes or root_causes == ["normal"]:
            return {
                "status": "no_action",
                "query_id": args.query_id,
                "queryid": args.queryid,
                "root_causes": root_causes,
                "explanation": predicted_explanation,
                "old_time": duration_s,
            }

        evaluation = await agent.action_manager.step(query_info, root_causes, mode="exploit")
        result: Dict[str, Any] = {
            "status": "completed",
            "query_id": args.query_id,
            "queryid": args.queryid,
            "query": sql,
            "root_causes": root_causes,
            "prediction_explanation": predicted_explanation,
            "evaluation": evaluation,
            "old_time": duration_s,
            "mean_time_ms": args.mean_time_ms,
            "max_time_ms": args.max_time_ms,
            "calls": args.calls,
            "read_only": _is_read_only(sql),
            "api_model": agent.action_manager.api_runtime.get("model"),
            "api_base": agent.action_manager.api_runtime.get("base_url"),
        }
        # Make the fields the bridge needs easy to consume without changing
        # DREAM's original evaluation result shape.
        result.update({
            "evaluation_status": evaluation.get("status"),
            "fix_action": evaluation.get("fix_action", ""),
            "rewrite_sql": evaluation.get("rewrite_sql", ""),
            "new_time": evaluation.get("new_time"),
            "approve_time": evaluation.get("approve_time", 0.0),
            "message": evaluation.get("msg", ""),
        })
        return result


def main() -> int:
    args = parse_args()
    output = Path(args.output).resolve()
    try:
        result = asyncio.run(run_one(args))
        _write(output, result)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0
    except Exception as exc:
        result = {
            "status": "failed",
            "error": "{}: {}".format(type(exc).__name__, exc),
            "query_id": args.query_id,
            "queryid": args.queryid,
        }
        _write(output, result)
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
