"""Compatibility surface for the upstream WorkloadTune UI.

The upstream Vue application is still useful as a presentation layer, but its
original API launched Docker processes and read result files from the
container.  This module keeps that HTTP contract while routing every result
through the live PostgreSQL workload runner.  It is deliberately a thin
compatibility layer: the canonical APIs remain ``/api/workloads`` and
``/api/workload-runs``.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from demo.scenarios import SCENARIO_METADATA, get_scenario

from .db import Database
from .feedback import FeedbackError, record_action_feedback
from .pg import jsonable
from .tuning import (
    ActionValidationError,
    apply_action,
    create_action,
    get_action,
    rollback_action,
)
from .workload import WorkloadError, WorkloadRunner


# The old selector exposes benchmark names, while this artifact ships a
# reproducible PostgreSQL workload suite.  The mapping is explicit and is
# returned to callers so a benchmark name is never silently presented as a
# benchmark measurement.
LEGACY_LOAD_TO_SCENARIO = {
    "tpcc": "missing_index",
    "tpch": "aggregate_spill",
    "sysbench": "sort_spill",
    "custom": "hash_join_spill",
}

LEGACY_DATABASE_NAMES = {
    "sysbench": "sbtest",
    "tpcc": "benchbase",
    "tpch": "tpch",
    "custom": "keeninsight_demo",
}

VALID_SCENARIOS = {item["name"] for item in SCENARIO_METADATA}
JSON_FIELDS = {
    "load_config",
    "tune_config",
    "configuration",
    "first_evaluate_result",
    "one_step_target",
    "one_step_result",
    "multi_result",
}
TASK_FIELDS = {
    "first_evaluate_status",
    "first_evaluate_run_id",
    "first_evaluate_result",
    "one_step_status",
    "one_step_action_id",
    "one_step_baseline_run_id",
    "one_step_after_run_id",
    "one_step_action_type",
    "one_step_target",
    "one_step_result",
    "multi_status",
    "multi_result",
    "last_error",
    "configuration",
}


class LegacySetConfigRequest(BaseModel):
    load: Optional[Dict[str, Any]] = None
    tune: Optional[Dict[str, Any]] = None


class LegacyTaskRequest(BaseModel):
    taskName: str = Field(min_length=1, max_length=200)


class LegacyApplyParamsRequest(BaseModel):
    taskName: str = Field(min_length=1, max_length=200)
    params: Dict[str, Any] = Field(default_factory=dict)


class LegacyTuneCompatibility:
    """Implement the old ``/api/tune`` workflow on real local measurements."""

    def __init__(self, db: Database, workloads: WorkloadRunner) -> None:
        self.db = db
        self.workloads = workloads
        self.router = APIRouter(tags=["legacy-workload-tune"])
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._register_routes()

    def register(self, app: FastAPI) -> None:
        app.include_router(self.router)

    def recover_stale_tasks(self) -> int:
        """Make persisted task state agree with a process that was restarted."""
        return self.db.execute(
            """
            UPDATE keeninsight.legacy_tune_tasks
            SET first_evaluate_status = CASE
                    WHEN first_evaluate_status IN ('running', 'pending') THEN 'failed'
                    ELSE first_evaluate_status END,
                one_step_status = CASE
                    WHEN one_step_status IN ('running', 'pending') THEN 'failed'
                    ELSE one_step_status END,
                multi_status = CASE
                    WHEN multi_status IN ('running', 'pending') THEN 'failed'
                    ELSE multi_status END,
                last_error = CASE
                    WHEN first_evaluate_status IN ('running', 'pending')
                      OR one_step_status IN ('running', 'pending')
                      OR multi_status IN ('running', 'pending')
                    THEN COALESCE(last_error, 'service restarted while a compatibility task was running')
                    ELSE last_error END,
                updated_at = now()
            WHERE first_evaluate_status IN ('running', 'pending')
               OR one_step_status IN ('running', 'pending')
               OR multi_status IN ('running', 'pending')
            """
        )

    # ------------------------------------------------------------------
    # HTTP registration
    # ------------------------------------------------------------------
    def _register_routes(self) -> None:
        self.router.add_api_route(
            "/api/tune/setConfig", self.set_config, methods=["POST"]
        )
        self.router.add_api_route(
            "/api/tune/firstEvaluate", self.start_first_evaluate, methods=["POST"]
        )
        self.router.add_api_route(
            "/api/tune/firstEvaluateResults",
            self.first_evaluate_results,
            methods=["GET"],
        )
        self.router.add_api_route(
            "/api/tune/oneStepStatus", self.one_step_status, methods=["GET"]
        )
        self.router.add_api_route(
            "/api/tune/exception-functions",
            self.exception_functions,
            methods=["GET"],
        )
        self.router.add_api_route(
            "/api/tune/oneStep", self.start_one_step, methods=["POST"]
        )
        self.router.add_api_route(
            "/api/tune/oneStepResult",
            self.one_step_result,
            methods=["GET", "POST"],
        )
        self.router.add_api_route(
            "/api/tune/applyParams", self.apply_params, methods=["POST"]
        )
        self.router.add_api_route(
            "/api/tune/startTune", self.start_multi_tune, methods=["POST"]
        )
        self.router.add_api_route(
            "/api/tune/multiTuneStatus", self.multi_tune_status, methods=["GET"]
        )
        self.router.add_api_route(
            "/api/tune/getMultiTuneResult",
            self.multi_tune_result,
            methods=["GET", "POST"],
        )
        # A short-lived branch of the old UI used this spelling.  Keeping the
        # alias costs nothing and makes the compatibility boundary explicit.
        self.router.add_api_route(
            "/api/tune/getMultiStepResult",
            self.multi_tune_result,
            methods=["GET", "POST"],
            include_in_schema=False,
        )

    # ------------------------------------------------------------------
    # Configuration and status endpoints
    # ------------------------------------------------------------------
    def set_config(self, request: LegacySetConfigRequest) -> Dict[str, Any]:
        try:
            parsed = self._parse_config(request)
            self._upsert_task(parsed)
            # The supplied Vue flow moves directly to the evaluation page and
            # does not call firstEvaluate.  Start the same real baseline here;
            # the explicit endpoint remains idempotent for older clients.
            status = self._start_first_evaluation(parsed["task_name"])
            return {
                "code": 200,
                "msg": "配置创建成功，真实 PostgreSQL 基线评估已启动",
                "task_id": parsed["task_name"],
                "ini_file": f"{parsed['task_name']}.ini",
                "details": (
                    "WorkloadTune-compatible task backed by the live PostgreSQL "
                    f"scenario {parsed['scenario']}; requested load type "
                    f"{parsed['load_type']!r} is retained as metadata."
                ),
                "output": json.dumps(
                    {
                        "scenario": parsed["scenario"],
                        "first_evaluate_status": status,
                        "measurement": "real PostgreSQL timed samples + EXPLAIN ANALYZE",
                    },
                    ensure_ascii=False,
                ),
                "evaluation_status": status,
            }
        except HTTPException:
            raise
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"配置创建失败: {exc}") from exc

    def start_first_evaluate(
        self,
        taskName: Optional[str] = Query(default=None),
        task_name: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        name = self._choose_task_name(taskName, task_name)
        self._require_task(name)
        status = self._start_first_evaluation(name)
        return {
            "code": 200,
            "msg": "真实 PostgreSQL 基线评估已启动" if status == "running" else "基线评估已完成",
            "task_id": name,
            "status": status,
        }

    def first_evaluate_results(
        self,
        taskName: Optional[str] = Query(default=None),
        task_name: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        name = self._choose_task_name(taskName, task_name)
        task = self._require_task(name)
        task = self._wait_for_first_result(task)
        status = task.get("first_evaluate_status")
        if status != "success":
            detail = task.get("last_error") or "基线评估尚未完成"
            raise HTTPException(status_code=409, detail=detail)
        result = task.get("first_evaluate_result") or {}
        return {"code": 200, "data": jsonable(result)}

    def one_step_status(
        self,
        taskName: Optional[str] = Query(default=None),
        task_name: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        name = self._choose_task_name(taskName, task_name)
        task = self._get_task(name)
        status = (task or {}).get("one_step_status", "idle")
        return {"code": 200, "msg": "success", "status": status, "task_id": name}

    def multi_tune_status(
        self,
        taskName: Optional[str] = Query(default=None),
        task_name: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        name = self._choose_task_name(taskName, task_name)
        task = self._get_task(name)
        status = (task or {}).get("multi_status", "idle")
        return {"code": 200, "msg": "success", "status": status, "task_name": name}

    # ------------------------------------------------------------------
    # One-step and multi-round workflow endpoints
    # ------------------------------------------------------------------
    def start_one_step(self, request: LegacyTaskRequest) -> Dict[str, Any]:
        name = request.taskName
        task = self._require_task(name)
        if task.get("first_evaluate_status") != "success":
            raise HTTPException(status_code=409, detail="请先完成基线评估")
        status = task.get("one_step_status", "idle")
        if status in {"running", "pending"}:
            return {"code": 400, "msg": "任务正在运行中", "task_id": name, "status": "running"}
        claimed = self.db.fetch_one(
            """
            UPDATE keeninsight.legacy_tune_tasks
            SET one_step_status = 'running', one_step_result = '{}'::jsonb,
                one_step_after_run_id = NULL, one_step_action_id = NULL,
                one_step_action_type = NULL, one_step_target = '{}'::jsonb,
                last_error = NULL, updated_at = now()
            WHERE task_name = %s AND one_step_status NOT IN ('running', 'pending')
            RETURNING task_name
            """,
            (name,),
        )
        if not claimed:
            current = self._require_task(name)
            return {
                "code": 400,
                "msg": "任务正在运行中",
                "task_id": name,
                "status": current.get("one_step_status", "running"),
            }
        self._launch(name, self._run_one_step, "one-step")
        return {"code": 200, "msg": "一步调优任务已启动", "task_id": name, "status": "running"}

    def one_step_result(
        self,
        taskName: Optional[str] = Query(default=None),
        task_name: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        name = self._choose_task_name(taskName, task_name)
        task = self._require_task(name)
        if task.get("one_step_status") != "success":
            detail = task.get("last_error") or "调优任务尚未完成或已失败"
            raise HTTPException(status_code=400, detail=detail)
        result = task.get("one_step_result") or {}
        return {"code": 200, "msg": "一步调优完成", "data": {"records": jsonable(result)}}

    def apply_params(self, request: LegacyApplyParamsRequest) -> Dict[str, Any]:
        task = self._require_task(request.taskName)
        if task.get("one_step_status") != "success":
            raise HTTPException(status_code=400, detail="请先完成一步调优推荐")
        action_type = task.get("one_step_action_type")
        target = dict(task.get("one_step_target") or {})
        if not action_type or not target:
            raise HTTPException(status_code=400, detail="一步调优没有可应用的 typed action")

        parameter = target.get("parameter")
        if action_type == "set_database_parameter" and parameter in request.params:
            target["value"] = self._normalise_parameter_value(parameter, request.params[parameter])
        elif action_type == "set_database_parameter" and request.params:
            unknown = sorted(str(key) for key in request.params if str(key) != parameter)
            if unknown:
                raise HTTPException(status_code=400, detail=f"不支持的参数: {unknown}")

        baseline_run_id = task.get("first_evaluate_run_id")
        proposal = {
            "action_type": action_type,
            "target": target,
            "description": (
                "Apply the reviewed WorkloadTune-compatible recommendation and "
                "retain PostgreSQL's apply verification."
            ),
            "source": {
                "type": "workload_run",
                "run_id": baseline_run_id,
                "scenario": task.get("scenario"),
            },
            "proposal_evidence": {
                "compatibility_api": True,
                "derived_from_one_step_action": task.get("one_step_action_id"),
                "requested_parameters": request.params,
            },
        }
        try:
            action = create_action(self.db, proposal)
            if action.get("status") == "proposed":
                action = apply_action(self.db, int(action["id"]))
            self._update_task(
                request.taskName,
                one_step_action_id=action.get("id"),
                one_step_action_type=action_type,
                one_step_target=target,
            )
            return {
                "code": 200,
                "msg": "参数应用成功，PostgreSQL 已完成 apply verification",
                "task_id": request.taskName,
                "status": action.get("status"),
                "action": jsonable(action),
            }
        except (ActionValidationError, WorkloadError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"参数应用失败: {exc}") from exc

    def start_multi_tune(self, request: LegacyTaskRequest) -> Dict[str, Any]:
        name = request.taskName
        task = self._require_task(name)
        if task.get("first_evaluate_status") != "success":
            raise HTTPException(status_code=409, detail="请先完成基线评估")
        if task.get("multi_status") in {"running", "pending"}:
            return {"code": 400, "msg": "任务正在运行中", "task_name": name, "status": "running"}
        claimed = self.db.fetch_one(
            """
            UPDATE keeninsight.legacy_tune_tasks
            SET multi_status = 'running', multi_result = '{}'::jsonb,
                last_error = NULL, updated_at = now()
            WHERE task_name = %s AND multi_status NOT IN ('running', 'pending')
            RETURNING task_name
            """,
            (name,),
        )
        if not claimed:
            return {"code": 400, "msg": "任务正在运行中", "task_name": name, "status": "running"}
        self._launch(name, self._run_multi_tune, "multi-round")
        return {"code": 200, "msg": "多轮调优任务已启动", "task_name": name, "status": "running"}

    def multi_tune_result(
        self,
        taskName: Optional[str] = Query(default=None),
        task_name: Optional[str] = Query(default=None),
    ) -> Dict[str, Any]:
        name = self._choose_task_name(taskName, task_name)
        task = self._require_task(name)
        if task.get("multi_status") != "success":
            detail = task.get("last_error") or "多轮调优尚未完成或已失败"
            raise HTTPException(status_code=400, detail=detail)
        return {"code": 200, "msg": "success", "data": jsonable(task.get("multi_result") or {})}

    def exception_functions(
        self,
        taskName: Optional[str] = Query(default=None),
        task_name: Optional[str] = Query(default=None),
        topN: int = Query(default=10, ge=1, le=100),
    ) -> Dict[str, Any]:
        name = self._choose_task_name(taskName, task_name)
        task = self._require_task(name)
        run_id = task.get("first_evaluate_run_id")
        if not run_id:
            return {"code": 200, "msg": "success", "data": []}
        run = self._workload_run(int(run_id))
        if not run:
            return {"code": 200, "msg": "success", "data": []}
        items = self._function_rows(run, topN)
        return {
            "code": 200,
            "msg": "success",
            "data": jsonable(items),
            "evidence": "persisted eBPF events overlapping the real baseline window; empty means no probe events were observed",
        }

    # ------------------------------------------------------------------
    # Background workers
    # ------------------------------------------------------------------
    def _run_first_evaluation(self, task_name: str) -> None:
        try:
            task = self._require_task(task_name)
            configuration = task.get("configuration") or {}
            rounds = self._integer(configuration.get("rounds"), 5, minimum=1, maximum=100)
            repetitions = max(3, min(rounds, 10))
            warmups = 2
            run = self.workloads.run(
                task["scenario"],
                "baseline",
                repetitions=repetitions,
                parameters=self._scenario_parameters(task),
                warmup_repetitions=warmups,
            )
            result = self._evaluation_result(task, run)
            execution = {
                "repetitions": repetitions,
                "warmup_repetitions": warmups,
                "requested_duration_minutes": configuration.get("duration_minutes"),
                "requested_thread_num": configuration.get("thread_num"),
                "thread_num_semantics": (
                    "retained for the upstream contract; this PostgreSQL compatibility "
                    "path measures controlled repeated SQL samples rather than claiming "
                    "an external benchmark generator"
                ),
            }
            configuration["execution"] = execution
            self._update_task(
                task_name,
                first_evaluate_status="success",
                first_evaluate_run_id=run.get("run_id"),
                first_evaluate_result=result,
                configuration=configuration,
                last_error=None,
            )
        except Exception as exc:
            self._update_task(
                task_name,
                first_evaluate_status="failed",
                last_error=f"基线评估失败: {exc}",
            )

    def _run_one_step(self, task_name: str) -> None:
        action: dict[str, Any] | None = None
        after: dict[str, Any] | None = None
        preserve_action = False
        try:
            task = self._require_task(task_name)
            baseline_id = int(task["first_evaluate_run_id"])
            baseline = self._workload_run(baseline_id)
            if not baseline:
                raise WorkloadError("基线 workload run 不存在")
            action = self.workloads.propose_action(baseline_id)
            preserve_action = action.get("status") in {"applied", "completed"}
            self._update_task(
                task_name,
                one_step_action_id=action.get("id"),
                one_step_baseline_run_id=baseline_id,
                one_step_action_type=action.get("action_type"),
                one_step_target=action.get("target") or {},
            )
            action = apply_action(self.db, int(action["id"])) if action.get("status") == "proposed" else action
            after = self._run_after_for_baseline(task, baseline)
            feedback = record_action_feedback(
                self.db, int(action["id"]), baseline_id, int(after["run_id"])
            )
            result = self._one_step_result_payload(task, baseline, after, action, feedback)
            self._rollback_compat_action(action, preserve_action)
            current = get_action(self.db, int(action["id"])) or action
            result["evidence"]["final_action_status"] = current.get("status")
            result["evidence"]["rollback_performed"] = (
                current.get("status") == "rolled_back"
            )
            self._update_task(
                task_name,
                one_step_status="success",
                one_step_after_run_id=after.get("run_id"),
                one_step_result=result,
                last_error=None,
            )
        except Exception as exc:
            if action:
                try:
                    current = get_action(self.db, int(action["id"]))
                    if current and current.get("status") == "applied" and not preserve_action:
                        rollback_action(self.db, int(action["id"]))
                except Exception:
                    pass
            self._update_task(
                task_name,
                one_step_status="failed",
                last_error=f"一步调优失败: {exc}",
            )

    def _run_multi_tune(self, task_name: str) -> None:
        partial: dict[str, Any] = {"rounds": [], "roundParams": {}}
        try:
            task = self._require_task(task_name)
            baseline_id = int(task["first_evaluate_run_id"])
            baseline = self._workload_run(baseline_id)
            if not baseline:
                raise WorkloadError("基线 workload run 不存在")
            baseline_eval = self._evaluation_result(task, baseline)
            base_action = self.workloads.propose_action(baseline_id)
            self._update_task(
                task_name,
                one_step_action_id=base_action.get("id"),
                one_step_baseline_run_id=baseline_id,
                one_step_action_type=base_action.get("action_type"),
                one_step_target=base_action.get("target") or {},
            )
            base_config = self._config_for_action(base_action, baseline, baseline)
            partial["rounds"].append(self._round_summary(1, baseline_eval, baseline_eval))
            partial["roundParams"]["1"] = self._round_params(base_config, base_config, 1)

            configuration = task.get("configuration") or {}
            requested_rounds = self._integer(configuration.get("rounds"), 5, minimum=1, maximum=100)
            candidate_targets = self._candidate_targets(
                task, base_action, baseline, max(0, min(requested_rounds, 8) - 1)
            )
            partial["measurement_contract"] = {
                "round_1": "the recorded baseline run",
                "candidate_rounds": "each candidate is typed, applied, measured with the same SQL and repetitions, compared, then rolled back unless it was already operator-applied",
                "score": "1000 / measured median query latency in milliseconds; this is a compatibility display score, not a model prediction",
                "candidate_round_limit": 8,
            }

            for index, target in enumerate(candidate_targets, start=2):
                proposal = {
                    "action_type": base_action.get("action_type"),
                    "target": target,
                    "description": (
                        "Measured candidate generated from the observed baseline "
                        "proposal; validate on the identical PostgreSQL workload."
                    ),
                    "source": {
                        "type": "workload_run",
                        "run_id": baseline_id,
                        "scenario": task.get("scenario"),
                    },
                    "proposal_evidence": {
                        "compatibility_multi_round": True,
                        "base_action_id": base_action.get("id"),
                        "candidate_round": index,
                    },
                }
                if self._same_target(target, base_action.get("target") or {}):
                    action = base_action
                else:
                    action = create_action(self.db, proposal)
                candidate = self._measure_candidate(task, baseline, action)
                after = candidate["after"]
                after_eval = self._evaluation_result(task, after)
                config = self._config_for_action(
                    action, baseline, after, recommended=True
                )
                partial["rounds"].append(
                    self._round_summary(index, after_eval, baseline_eval, candidate)
                )
                partial["roundParams"][str(index)] = self._round_params(
                    base_config, config, index
                )

            partial["round_count"] = len(partial["rounds"])
            partial["scenario"] = task.get("scenario")
            partial["workload"] = task.get("load_type")
            self._update_task(
                task_name,
                multi_status="success",
                multi_result=partial,
                last_error=None,
            )
        except Exception as exc:
            partial["partial"] = True
            self._update_task(
                task_name,
                multi_status="failed",
                multi_result=partial,
                last_error=f"多轮调优失败: {exc}",
            )

    def _measure_candidate(
        self,
        task: dict[str, Any],
        baseline: dict[str, Any],
        action: dict[str, Any],
    ) -> Dict[str, Any]:
        initial_status = action.get("status")
        preserve_action = initial_status in {"applied", "completed"}
        action_id = int(action["id"])
        try:
            if initial_status == "proposed":
                action = apply_action(self.db, action_id)
            after = self._run_after_for_baseline(task, baseline)
            feedback = record_action_feedback(
                self.db, action_id, int(baseline["id"]), int(after["run_id"])
            )
            self._rollback_compat_action(action, preserve_action)
            current = get_action(self.db, action_id) or action
            return {
                "action": current,
                "after": after,
                "feedback": feedback,
                "rollback_performed": current.get("status") == "rolled_back",
            }
        except Exception:
            current = get_action(self.db, action_id)
            if current and current.get("status") == "applied" and not preserve_action:
                try:
                    rollback_action(self.db, action_id)
                except Exception:
                    pass
            raise

    def _rollback_compat_action(self, action: dict[str, Any], preserve: bool) -> None:
        if preserve or action.get("action_type") == "refresh_statistics":
            return
        current = get_action(self.db, int(action["id"])) or action
        if current.get("status") == "applied":
            rollback_action(self.db, int(action["id"]))

    # ------------------------------------------------------------------
    # Configuration, measurements, and candidate generation
    # ------------------------------------------------------------------
    def _parse_config(self, request: LegacySetConfigRequest) -> dict[str, Any]:
        load = dict(request.load or {})
        tune = dict(request.tune or {})
        task_name = str(tune.get("tuneName") or "").strip()
        if not task_name:
            raise ValueError("tuneName is required")
        if len(task_name) > 200 or any(ord(char) < 32 for char in task_name):
            raise ValueError("tuneName contains an invalid control character or is too long")

        load_type = str(load.get("loadType") or "custom").strip()
        load_key = self._normalise_load_key(load_type)
        scenario_requested = load.get("scenario") or tune.get("scenario")
        scenario = self._resolve_scenario(scenario_requested, load_key)

        duration = self._integer(load.get("duration"), 30, minimum=1, maximum=24 * 60)
        thread_num = self._integer(load.get("threadNum"), 32, minimum=1, maximum=1000)
        rounds = self._integer(tune.get("rounds"), 5, minimum=1, maximum=100)
        metrics = tune.get("metrics") or []
        if not isinstance(metrics, list):
            raise ValueError("metrics must be an array")
        metrics = [str(metric) for metric in metrics]
        time_range = load.get("timeRange")
        if time_range is not None and (
            not isinstance(time_range, (list, tuple)) or len(time_range) != 2
        ):
            raise ValueError("timeRange must contain a start and end value")
        requested_parameters = load.get("parameters") or tune.get("parameters") or {}
        if not isinstance(requested_parameters, dict):
            raise ValueError("scenario parameters must be an object")

        return {
            "task_name": task_name,
            "load_type": load_type,
            "scenario": scenario,
            "load_config": load,
            "tune_config": tune,
            "configuration": {
                "task_id": task_name,
                "workload": load_type,
                "dbname": LEGACY_DATABASE_NAMES.get(load_key, "keeninsight_demo"),
                "duration_minutes": duration,
                "thread_num": thread_num,
                "rounds": rounds,
                "metrics": metrics,
                "time_range": time_range,
                "scenario": scenario,
                "scenario_parameters": requested_parameters,
                "scenario_mapping": {
                    "requested_load_type": load_type,
                    "mapped_scenario": scenario,
                    "explicit_scenario": bool(scenario_requested),
                    "note": (
                        "The compatibility API preserves the upstream benchmark label, "
                        "but executes only the named reproducible PostgreSQL scenario."
                    ),
                },
                "selected_params": tune.get("selectedParams") or [],
                "param_ranges": tune.get("paramRanges") or {},
            },
        }

    def _upsert_task(self, parsed: dict[str, Any]) -> None:
        existing = self._get_task(parsed["task_name"])
        if existing and any(
            existing.get(field) in {"running", "pending"}
            for field in ("first_evaluate_status", "one_step_status", "multi_status")
        ):
            raise HTTPException(status_code=409, detail="该任务仍在运行，不能覆盖配置")
        self.db.execute(
            """
            INSERT INTO keeninsight.legacy_tune_tasks
                (task_name, load_type, scenario, load_config, tune_config,
                 configuration, first_evaluate_status, first_evaluate_run_id,
                 first_evaluate_result, one_step_status, one_step_action_id,
                 one_step_baseline_run_id, one_step_after_run_id,
                 one_step_action_type, one_step_target, one_step_result,
                 multi_status, multi_result, last_error)
            VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                    'idle', NULL, '{}'::jsonb, 'idle', NULL, NULL, NULL,
                    NULL, '{}'::jsonb, '{}'::jsonb, 'idle', '{}'::jsonb, NULL)
            ON CONFLICT (task_name) DO UPDATE SET
                load_type = EXCLUDED.load_type,
                scenario = EXCLUDED.scenario,
                load_config = EXCLUDED.load_config,
                tune_config = EXCLUDED.tune_config,
                configuration = EXCLUDED.configuration,
                first_evaluate_status = 'idle',
                first_evaluate_run_id = NULL,
                first_evaluate_result = '{}'::jsonb,
                one_step_status = 'idle',
                one_step_action_id = NULL,
                one_step_baseline_run_id = NULL,
                one_step_after_run_id = NULL,
                one_step_action_type = NULL,
                one_step_target = '{}'::jsonb,
                one_step_result = '{}'::jsonb,
                multi_status = 'idle',
                multi_result = '{}'::jsonb,
                last_error = NULL,
                updated_at = now()
            """,
            (
                parsed["task_name"],
                parsed["load_type"],
                parsed["scenario"],
                self._dump(parsed["load_config"]),
                self._dump(parsed["tune_config"]),
                self._dump(parsed["configuration"]),
            ),
        )

    def _start_first_evaluation(self, task_name: str) -> str:
        task = self._require_task(task_name)
        if task.get("first_evaluate_status") == "success":
            return "success"
        claimed = self.db.fetch_one(
            """
            UPDATE keeninsight.legacy_tune_tasks
            SET first_evaluate_status = 'running', first_evaluate_result = '{}'::jsonb,
                first_evaluate_run_id = NULL, one_step_status = 'idle',
                one_step_action_id = NULL, one_step_baseline_run_id = NULL,
                one_step_after_run_id = NULL, one_step_action_type = NULL,
                one_step_target = '{}'::jsonb, one_step_result = '{}'::jsonb,
                multi_status = 'idle', multi_result = '{}'::jsonb,
                last_error = NULL, updated_at = now()
            WHERE task_name = %s AND first_evaluate_status NOT IN ('running', 'pending')
            RETURNING task_name
            """,
            (task_name,),
        )
        if claimed:
            self._launch(task_name, self._run_first_evaluation, "baseline")
            return "running"
        return str((self._require_task(task_name)).get("first_evaluate_status") or "running")

    def _launch(self, task_name: str, target, label: str) -> None:
        with self._lock:
            current = self._threads.get(task_name)
            if current and current.is_alive():
                return
            thread = threading.Thread(
                target=target,
                args=(task_name,),
                name=f"keeninsight-legacy-{label}-{task_name}",
                daemon=True,
            )
            self._threads[task_name] = thread
            thread.start()

    def _wait_for_first_result(self, task: dict[str, Any]) -> dict[str, Any]:
        deadline = time.monotonic() + 25.0
        name = task["task_name"]
        while task.get("first_evaluate_status") in {"running", "pending"} and time.monotonic() < deadline:
            time.sleep(0.1)
            refreshed = self._get_task(name)
            if refreshed:
                task = refreshed
        return task

    def _run_after_for_baseline(
        self, task: dict[str, Any], baseline: dict[str, Any]
    ) -> dict[str, Any]:
        parameters = dict(baseline.get("parameters") or {})
        try:
            repetitions = int(parameters.pop("repetitions"))
            warmups = int(parameters.pop("warmup_repetitions"))
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkloadError("基线 run 没有完整的测量参数") from exc
        if repetitions < 3:
            raise WorkloadError("一步/多轮反馈至少需要三次真实 timed samples")
        return self.workloads.run(
            task["scenario"],
            "after",
            repetitions=repetitions,
            parameters=parameters,
            warmup_repetitions=warmups,
        )

    def _evaluation_result(self, task: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        observations = self._observations(run)
        median = self._number(observations.get("median_query_time_ms"), 0.0)
        p95 = self._number(observations.get("p95_query_time_ms"), 0.0)
        rate = 1000.0 / median if median > 0 else 0.0
        return {
            "workload": task.get("load_type"),
            "scenario": task.get("scenario"),
            "run_id": run.get("run_id") or run.get("id"),
            "tps": rate,
            "qps": rate,
            "latency": median,
            "p95_latency": p95,
            "top_functions": self._function_rows(run, 10),
            "timing_summary": observations.get("timing_summary") or {},
            "plan_summary": observations.get("plan_summary") or {},
            "metric_semantics": (
                "tps/qps are the measured repeated-query rate derived from the real "
                "timed sample window; they are not an external TPC benchmark claim"
            ),
            "measurement": {
                "repetitions": len(observations.get("timings_ms") or []),
                "query": observations.get("query"),
                "phase": observations.get("phase"),
                "database_measurement": observations.get("database_measurement"),
                "query_measurement": observations.get("query_measurement"),
                "plan": observations.get("plan"),
            },
        }

    def _one_step_result_payload(
        self,
        task: dict[str, Any],
        baseline: dict[str, Any],
        after: dict[str, Any],
        action: dict[str, Any],
        feedback: dict[str, Any],
    ) -> dict[str, Any]:
        base_score = self._score(baseline)
        measured_score = self._score(after)
        outcome = ((feedback.get("feedback") or {}).get("outcome") or {})
        base_config = self._config_for_action(action, baseline, baseline)
        recommended_config = self._config_for_action(action, baseline, after, recommended=True)
        return {
            "base_config": base_config,
            "recommended_config": recommended_config,
            "base_score": base_score,
            # The upstream field is named predicted_score.  We populate it
            # with the measured after-phase score and say so explicitly; no
            # prediction is fabricated for this compatibility response.
            "predicted_score": measured_score,
            "delta_score": measured_score - base_score,
            "reasoning": (
                f"Observed {action.get('action_type')} from the baseline PostgreSQL "
                f"plan, applied the typed target {action.get('target')}, and measured "
                f"the identical SQL again. Observed effect: {outcome.get('observed_effect')}. "
                "The displayed predicted_score is the measured after-phase query-rate "
                "compatibility score, not an unvalidated model prediction."
            ),
            "evidence": {
                "action_id": action.get("id"),
                "baseline_run_id": baseline.get("id"),
                "after_run_id": after.get("run_id"),
                "feedback_id": (feedback.get("feedback") or {}).get("id"),
                "observed_effect": outcome.get("observed_effect"),
                "effective": outcome.get("effective"),
                "median_improvement_pct": outcome.get("median_improvement_pct"),
                "p95_improvement_pct": outcome.get("p95_improvement_pct"),
                "same_sql": True,
            },
        }

    def _score(self, run: dict[str, Any]) -> float:
        observations = self._observations(run)
        median = self._number(observations.get("median_query_time_ms"), 0.0)
        return round(1000.0 / median, 6) if median > 0 else 0.0

    def _config_for_action(
        self,
        action: dict[str, Any],
        baseline: dict[str, Any],
        after: dict[str, Any],
        *,
        recommended: bool = False,
    ) -> dict[str, Any]:
        target = action.get("target") or {}
        action_type = action.get("action_type")
        before_observations = self._observations(baseline)
        after_observations = self._observations(after)
        if action_type == "set_database_parameter":
            parameter = str(target.get("parameter") or "parameter")
            if parameter == "work_mem":
                raw = target.get("value") if recommended else before_observations.get("phase_work_mem")
                return {parameter: self._setting_number(raw)}
            if parameter == "max_parallel_workers_per_gather":
                raw = target.get("value") if recommended else before_observations.get("phase_parallel_workers")
                return {parameter: self._integer(raw, 0, minimum=0, maximum=10_000)}
        if action_type == "refresh_statistics":
            key = "statistics_estimate_error_ratio"
            before = ((before_observations.get("statistics_measurement") or {}).get("estimate_error_ratio"))
            after_value = ((after_observations.get("statistics_measurement") or {}).get("estimate_error_ratio"))
            return {key: self._number(after_value if recommended else before, 0.0)}
        if action_type == "create_index":
            index_name = str(target.get("index") or "index")
            return {f"index:{index_name}": 1 if recommended else 0}
        if action_type == "drop_index":
            index_name = str(target.get("index") or "index")
            return {f"index:{index_name}": 0 if recommended else 1}
        return {"typed_action": 1 if recommended else 0}

    def _candidate_targets(
        self,
        task: dict[str, Any],
        action: dict[str, Any],
        baseline: dict[str, Any],
        count: int,
    ) -> list[dict[str, Any]]:
        if count <= 0:
            return []
        target = dict(action.get("target") or {})
        action_type = action.get("action_type")
        if action_type != "set_database_parameter":
            return [target]
        parameter = target.get("parameter")
        if parameter == "work_mem":
            base = self._setting_number(self._observations(baseline).get("phase_work_mem"))
            end = self._setting_number(target.get("value"))
            if base <= 0:
                base = 64 * 1024
            if end <= base:
                end = min(256 * 1024 * 1024, max(base * 2, end))
            values = []
            for index in range(1, count + 1):
                fraction = index / count
                value = int(round(base * ((end / base) ** fraction)))
                value = max(64 * 1024, min(256 * 1024 * 1024, value))
                # PostgreSQL's pg_size_bytes accepts the human-readable
                # units used by pg_settings reliably across the supported
                # versions.  Round candidate bytes upward to a whole MB so
                # the old UI's scalar parameter shape remains valid without
                # emitting a server-dependent bare ``B`` suffix.
                value_mb = max(1, (value + 1024 * 1024 - 1) // (1024 * 1024))
                values.append(f"{value_mb}MB")
            return self._unique_targets(target, values)
        if parameter == "max_parallel_workers_per_gather":
            base = self._integer(
                self._observations(baseline).get("phase_parallel_workers"),
                0,
                minimum=0,
                maximum=10_000,
            )
            end = self._integer(target.get("value"), base + 1, minimum=0, maximum=10_000)
            max_row = self.db.fetch_one(
                "SELECT max_val FROM pg_settings WHERE name = 'max_parallel_workers_per_gather'"
            )
            maximum = self._integer((max_row or {}).get("max_val"), end, minimum=0, maximum=10_000)
            values = [str(value) for value in range(base + 1, min(maximum, max(end, base + count)) + 1)]
            return self._unique_targets(target, values)
        return [target]

    @staticmethod
    def _unique_targets(base: dict[str, Any], values: list[str]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for value in values:
            candidate = dict(base)
            candidate["value"] = value
            marker = json.dumps(candidate, sort_keys=True)
            if marker not in seen:
                seen.add(marker)
                result.append(candidate)
        return result

    @staticmethod
    def _same_target(left: dict[str, Any], right: dict[str, Any]) -> bool:
        return json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True)

    @staticmethod
    def _round_summary(
        number: int,
        current: dict[str, Any],
        baseline: dict[str, Any],
        candidate: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        base_tps = float(baseline.get("tps") or 0.0)
        base_latency = float(baseline.get("latency") or 0.0)
        tps = float(current.get("tps") or 0.0)
        latency = float(current.get("latency") or 0.0)
        tps_improvement = ((tps - base_tps) / base_tps * 100.0) if base_tps else 0.0
        latency_improvement = ((base_latency - latency) / base_latency * 100.0) if base_latency else 0.0
        return {
            "round": number,
            "tps": tps,
            "latency": latency,
            "improvement": round(max(0.0, tps_improvement, latency_improvement), 2),
            "observed_tps_change_pct": round(tps_improvement, 4),
            "observed_latency_change_pct": round(latency_improvement, 4),
            "evidence": (
                {
                    "action_id": candidate["action"].get("id"),
                    "after_run_id": candidate["after"].get("run_id"),
                    "feedback_id": (candidate["feedback"].get("feedback") or {}).get("id"),
                    "observed_effect": ((candidate["feedback"].get("feedback") or {}).get("outcome") or {}).get("observed_effect"),
                    "rollback_performed": candidate.get("rollback_performed"),
                }
                if candidate
                else {
                    "baseline_run_id": baseline.get("run_id") or baseline.get("id")
                }
            ),
        }

    @staticmethod
    def _round_params(
        base_config: dict[str, Any], current_config: dict[str, Any], number: int
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for key, original in base_config.items():
            current = current_config.get(key, original)
            changed = current != original
            if number != 1 and not changed:
                continue
            numeric_original = LegacyTuneCompatibility._number_or_none(original)
            numeric_current = LegacyTuneCompatibility._number_or_none(current)
            if numeric_original is not None and numeric_current is not None:
                increase = numeric_current > numeric_original
                percent = (
                    abs(numeric_current - numeric_original) / abs(numeric_original) * 100.0
                    if numeric_original != 0
                    else 0.0
                )
                change_type = "提升" if increase else ("降低" if changed else "不变")
            else:
                percent = 0.0
                change_type = "提升" if changed else "不变"
            rows.append(
                {
                    "paramName": key,
                    "originalValue": original,
                    "currentValue": current,
                    "changeType": change_type,
                    "changePercent": round(percent, 2),
                    "desc": "Measured PostgreSQL candidate; see evidence in the round record.",
                    "riskLevel": "medium" if changed else "low",
                }
            )
        return rows

    # ------------------------------------------------------------------
    # Persistence and evidence helpers
    # ------------------------------------------------------------------
    def _get_task(self, task_name: str) -> dict[str, Any] | None:
        return self.db.fetch_one(
            """
            SELECT task_name, load_type, scenario, load_config, tune_config,
                   configuration, first_evaluate_status, first_evaluate_run_id,
                   first_evaluate_result, one_step_status, one_step_action_id,
                   one_step_baseline_run_id, one_step_after_run_id,
                   one_step_action_type, one_step_target, one_step_result,
                   multi_status, multi_result, last_error, created_at, updated_at
            FROM keeninsight.legacy_tune_tasks WHERE task_name = %s
            """,
            (task_name,),
        )

    def _require_task(self, task_name: str) -> dict[str, Any]:
        name = str(task_name or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="taskName is required")
        task = self._get_task(name)
        if not task:
            raise HTTPException(status_code=404, detail="task not found")
        return task

    def _workload_run(self, run_id: int) -> dict[str, Any] | None:
        return self.db.fetch_one(
            """
            SELECT id, scenario, phase, status, parameters, observations,
                   started_at, finished_at, error
            FROM keeninsight.workload_runs WHERE id = %s
            """,
            (run_id,),
        )

    def _update_task(self, task_name: str, **fields: Any) -> None:
        unknown = set(fields) - TASK_FIELDS
        if unknown:
            raise ValueError(f"unsupported compatibility task fields: {sorted(unknown)}")
        assignments: list[str] = []
        params: list[Any] = []
        for field, value in fields.items():
            if field in JSON_FIELDS:
                assignments.append(f"{field} = %s::jsonb")
                params.append(self._dump(value or {}))
            else:
                assignments.append(f"{field} = %s")
                params.append(value)
        if not assignments:
            return
        assignments.append("updated_at = now()")
        params.append(task_name)
        self.db.execute(
            f"UPDATE keeninsight.legacy_tune_tasks SET {', '.join(assignments)} WHERE task_name = %s",
            tuple(params),
        )

    def _function_rows(self, run: dict[str, Any], limit: int) -> list[dict[str, Any]]:
        if not run.get("started_at") and run.get("run_id"):
            stored = self._workload_run(int(run["run_id"]))
            if stored:
                run = stored
        started = run.get("started_at")
        finished = run.get("finished_at") or started
        if not started:
            return []
        rows = self.db.fetch_all(
            """
            SELECT function_name, count(*)::bigint AS sample_count,
                   COALESCE(sum(duration_ns), 0)::numeric AS duration_ns
            FROM keeninsight.ebpf_events
            WHERE observed_at >= %s - interval '1 second'
              AND observed_at <= %s + interval '1 second'
            GROUP BY function_name
            ORDER BY COALESCE(sum(duration_ns), 0) DESC, count(*) DESC, function_name
            LIMIT %s
            """,
            (started, finished, max(1, min(int(limit), 100))),
        )
        if not rows:
            return []
        duration_total = sum(self._number(row.get("duration_ns"), 0.0) for row in rows)
        sample_total = sum(self._number(row.get("sample_count"), 0.0) for row in rows)
        mean_rate = 100.0 / len(rows)
        result: list[dict[str, Any]] = []
        for row in rows:
            weight = self._number(row.get("duration_ns"), 0.0) if duration_total > 0 else self._number(row.get("sample_count"), 0.0)
            total = duration_total if duration_total > 0 else sample_total
            rate = (weight / total * 100.0) if total > 0 else 0.0
            knobs = self.db.fetch_all(
                """
                SELECT DISTINCT knob FROM keeninsight.source_analyses
                WHERE function_name = %s AND knob IS NOT NULL
                ORDER BY knob LIMIT 20
                """,
                (row.get("function_name"),),
            )
            result.append(
                {
                    "funcName": row.get("function_name"),
                    "sampleRate": rate,
                    "diffFromMean": rate - mean_rate,
                    "change": 1 if rate >= mean_rate else 0,
                    "relatedKnobs": [item["knob"] for item in knobs],
                    "sampleCount": int(self._number(row.get("sample_count"), 0.0)),
                    "durationNs": self._number(row.get("duration_ns"), 0.0),
                    "evidence": "persisted eBPF event aggregation",
                }
            )
        return result

    def _scenario_parameters(self, task: dict[str, Any]) -> dict[str, Any]:
        configuration = task.get("configuration") or {}
        requested = configuration.get("scenario_parameters")
        if isinstance(requested, dict) and requested:
            return dict(requested)
        return dict(get_scenario(task["scenario"]).get("parameters") or {})

    @staticmethod
    def _observations(run: dict[str, Any]) -> dict[str, Any]:
        """Normalize WorkloadRunner's response and its persisted row shape."""
        observations = run.get("observations")
        if isinstance(observations, dict):
            return observations
        # ``WorkloadRunner.run`` returns the observation fields at the top
        # level, whereas a row read from workload_runs nests them.  Keeping
        # this conversion local prevents the compatibility API from changing
        # the canonical runner contract.
        return {
            key: value
            for key, value in run.items()
            if key not in {"run_id", "scenario", "scenario_definition"}
        }

    @staticmethod
    def _choose_task_name(*values: Optional[str]) -> str:
        for value in values:
            if value:
                return str(value).strip()
        raise HTTPException(status_code=400, detail="taskName is required")

    @staticmethod
    def _normalise_load_key(value: str) -> str:
        return re.sub(r"[-\s]", "", str(value).strip().lower())

    @staticmethod
    def _resolve_scenario(requested: Any, load_key: str) -> str:
        if requested:
            candidate = str(requested).strip().lower()
            if candidate in VALID_SCENARIOS:
                return candidate
            raise ValueError(
                f"unknown PostgreSQL scenario {requested!r}; choose one of {sorted(VALID_SCENARIOS)}"
            )
        return LEGACY_LOAD_TO_SCENARIO.get(load_key, "hash_join_spill")

    def _normalise_parameter_value(self, parameter: str, value: Any) -> str:
        if isinstance(value, bool) or value is None:
            raise ValueError(f"{parameter} must be a scalar value")
        if parameter == "work_mem":
            if isinstance(value, (int, float)):
                if not math.isfinite(float(value)) or int(value) <= 0 or float(value) != int(value):
                    raise ValueError("work_mem byte value must be a positive integer")
                return f"{int(value)}B"
            return str(value).strip()
        if parameter == "max_parallel_workers_per_gather":
            return str(self._integer(value, 0, minimum=0, maximum=10_000))
        return str(value)

    def _setting_number(self, value: Any) -> int:
        if isinstance(value, bool) or value is None:
            return 0
        try:
            text = str(value).strip()
            if re.fullmatch(r"[0-9]+", text):
                return int(text)
            row = self.db.fetch_one("SELECT pg_size_bytes(%s) AS bytes", (text,))
            return int(row["bytes"]) if row else 0
        except Exception:
            return 0

    @staticmethod
    def _integer(value: Any, default: int, *, minimum: int, maximum: int) -> int:
        if value is None or value == "":
            number = default
        else:
            if isinstance(value, bool):
                raise ValueError("integer value cannot be boolean")
            try:
                number = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid integer value: {value!r}") from exc
        if number < minimum or number > maximum:
            raise ValueError(f"integer value must be in [{minimum}, {maximum}]")
        return number

    @staticmethod
    def _number(value: Any, default: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        return number if math.isfinite(number) else default

    @staticmethod
    def _number_or_none(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    @staticmethod
    def _dump(value: Any) -> str:
        return json.dumps(value, default=str, ensure_ascii=False, separators=(",", ":"))


__all__ = ["LegacyTuneCompatibility"]
