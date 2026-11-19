from __future__ import annotations

import json
import hashlib
import re
import statistics
import threading
import time
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg import sql
from psycopg.errors import UndefinedColumn, UndefinedTable
from psycopg.rows import dict_row

from demo.provision import provision
from demo.scenarios import (
    SCENARIO_METADATA,
    STATS_PROBE_INITIAL_RARE_ROWS,
    STATS_PROBE_PRESSURE_RARE_ROWS,
    STATS_PROBE_TOTAL_ROWS,
    get_scenario,
)

from .db import Database
from .diagnose import flatten_plan, hash_batches, summarize_plan
from .pg import index_exists, relation_columns, relation_exists, safe_identifier
from .tuning import create_action


class WorkloadError(RuntimeError):
    pass


NORMAL_LOAD_METADATA = [
    {
        "name": "point_lookup",
        "title": "订单点查",
        "description": "按订单主键读取一条记录，模拟轻量在线请求。",
        "query": (
            "SELECT order_id, customer_id, amount, status "
            "FROM keeninsight_demo.orders WHERE order_id = %s"
        ),
        "parameters": (12345,),
        "default_rate": 2.0,
    },
    {
        "name": "customer_history",
        "title": "客户订单查询",
        "description": "按客户读取最近订单，模拟常见业务列表页。",
        "query": (
            "SELECT order_id, order_date, amount, status "
            "FROM keeninsight_demo.orders "
            "WHERE customer_id = %s ORDER BY order_date DESC LIMIT 30"
        ),
        "parameters": (1234,),
        "default_rate": 1.0,
    },
    {
        "name": "order_summary",
        "title": "订单状态汇总",
        "description": "按状态聚合订单，模拟低频报表查询。",
        "query": (
            "SELECT status, count(*)::bigint AS total "
            "FROM keeninsight_demo.orders GROUP BY status ORDER BY status"
        ),
        "parameters": (),
        "default_rate": 0.2,
    },
    {
        "name": "tpcc_order_lookup",
        "title": "TPC-C 最新订单查询",
        "description": "从真实 TPC-C 订单表读取某仓库/地区的最新订单，模拟订单详情页。",
        "query": (
            "SELECT o_id, o_c_id, o_entry_d, o_carrier_id, o_ol_cnt "
            "FROM keeninsight_tpcc.orders "
            "WHERE o_w_id = %s AND o_d_id = %s ORDER BY o_id DESC LIMIT 1"
        ),
        "parameters": (1, 1),
        "default_rate": 2.0,
        "requires_tpcc": True,
    },
    {
        "name": "tpcc_customer_history",
        "title": "TPC-C 客户订单历史",
        "description": "联结真实客户、订单和订单明细，模拟客户历史页查询。",
        "query": (
            "SELECT o.o_id, o.o_entry_d, ol.ol_i_id, ol.ol_amount "
            "FROM keeninsight_tpcc.orders AS o "
            "JOIN keeninsight_tpcc.order_line AS ol "
            "  ON ol.ol_w_id = o.o_w_id AND ol.ol_d_id = o.o_d_id AND ol.ol_o_id = o.o_id "
            "WHERE o.o_w_id = %s AND o.o_d_id = %s AND o.o_c_id = %s "
            "ORDER BY o.o_id DESC, ol.ol_number LIMIT 30"
        ),
        "parameters": (1, 1, 1),
        "default_rate": 1.0,
        "requires_tpcc": True,
    },
    {
        "name": "tpcc_stock_check",
        "title": "TPC-C 库存检查",
        "description": "联结真实库存和商品表，模拟下单前的库存可用性检查。",
        "query": (
            "SELECT i.i_id, i.i_name, i.i_price, s.s_quantity "
            "FROM keeninsight_tpcc.stock AS s "
            "JOIN keeninsight_tpcc.item AS i ON i.i_id = s.s_i_id "
            "WHERE s.s_w_id = %s AND s.s_i_id BETWEEN %s AND %s "
            "ORDER BY s.s_i_id LIMIT 30"
        ),
        "parameters": (1, 1, 31),
        "default_rate": 0.5,
        "requires_tpcc": True,
    },
]


class WorkloadRunner:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._lock_runs: dict[int, _LockRun] = {}
        self._batch_runs: dict[int, threading.Thread] = {}
        self._normal_load: _NormalLoadRun | None = None
        self._next_lock_id = 1
        self._lock = threading.Lock()
        self._progress_local = threading.local()

    def scenarios(self) -> list[dict[str, Any]]:
        return SCENARIO_METADATA

    def normal_loads(self) -> list[dict[str, Any]]:
        """Return selectable read-only loads without exposing SQL to the UI."""
        tpcc_available = self.tpcc_state().get("available", False)
        return [
            {
                key: item[key]
                for key in ("name", "title", "description", "default_rate")
            }
            for item in NORMAL_LOAD_METADATA
            if not item.get("requires_tpcc") or tpcc_available
        ]

    def normal_load_status(self) -> dict[str, Any]:
        with self._lock:
            state = self._normal_load
        return state.status() if state else {"running": False}

    def start_normal_load(self, name: str, rate_per_second: float | None = None) -> dict[str, Any]:
        definition = next(
            (item for item in NORMAL_LOAD_METADATA if item["name"] == name),
            None,
        )
        if definition is None:
            raise WorkloadError(f"unknown normal load: {name}")
        if definition.get("requires_tpcc") and not self.tpcc_state().get("available", False):
            raise WorkloadError("TPC-C 数据尚未加载，请先运行 scripts/bootstrap_tpcc.py")
        rate = definition["default_rate"] if rate_per_second is None else float(rate_per_second)
        if not 0.05 <= rate <= 20.0:
            raise WorkloadError("normal load rate must be between 0.05 and 20 per second")
        with self._lock:
            if self._normal_load and self._normal_load.running:
                raise WorkloadError("a normal load is already running; stop it first")
            state = _NormalLoadRun(self.db.dsn, definition, rate)
            self._normal_load = state
            state.thread = threading.Thread(
                target=state.execute,
                name=f"keeninsight-normal-load-{name}",
                daemon=True,
            )
            state.thread.start()
        return state.status()

    def stop_normal_load(self) -> dict[str, Any]:
        with self._lock:
            state = self._normal_load
        if not state:
            return {"running": False}
        state.stop.set()
        if state.thread:
            state.thread.join(timeout=5)
        return state.status()

    def bootstrap(self) -> dict[str, Any]:
        with self.db.connection() as conn:
            provision(conn)
        return self.demo_state()

    def recover_stale_runs(self) -> int:
        """Close runs left in ``running`` after an unclean service exit."""
        return self.db.execute(
            """
            UPDATE keeninsight.workload_runs
            SET status = 'failed',
                error = COALESCE(error, 'service restarted while the workload was running'),
                finished_at = now()
            WHERE status = 'running'
            """
        )

    def demo_state(self) -> dict[str, Any]:
        relations = {}
        table_stats: dict[str, dict[str, Any]] = {}
        for schema_name, table_name in (
            ("keeninsight_demo", "customers"),
            ("keeninsight_demo", "orders"),
            ("keeninsight_demo", "accounts"),
            ("keeninsight_demo", "stats_probe"),
        ):
            relation_name = f"{schema_name}.{table_name}"
            exists = relation_exists(self.db, schema_name, table_name)
            relations[relation_name] = exists
            if exists:
                row = self.db.fetch_one(
                    sql.SQL(
                        "SELECT count(*)::bigint AS row_count, "
                        "pg_total_relation_size(%s::regclass)::bigint AS total_bytes "
                        "FROM {}.{}"
                    ).format(sql.Identifier(schema_name), sql.Identifier(table_name)),
                    (relation_name,),
                )
                table_stats[relation_name] = row or {}
        # Database-level ALTER DATABASE changes the defaults for new sessions.
        # The application pool may contain older sessions, so the state card
        # deliberately uses one fresh connection.  This is outside the timed
        # workload path and prevents the UI from showing a stale setting after
        # an apply/rollback.
        with psycopg.connect(self.db.dsn, row_factory=dict_row, autocommit=True) as conn:
            setting_rows = conn.execute(
                "SELECT name, setting, unit, source FROM pg_settings "
                "WHERE name IN ('work_mem', 'max_parallel_workers_per_gather')"
            ).fetchall()
        setting_by_name = {row["name"]: dict(row) for row in setting_rows}
        setting = setting_by_name.get("work_mem")
        parallel_setting = setting_by_name.get("max_parallel_workers_per_gather")
        return {
            "relations": relations,
            "table_stats": table_stats,
            "work_mem": setting or {},
            "parallel_workers": parallel_setting or {},
            "missing_index_exists": index_exists(
                self.db, "keeninsight_demo", "orders_customer_id_idx"
            ),
            "stats_probe": self._stats_probe_state(),
            "tpcc": self.tpcc_state(),
        }

    def tpcc_state(self) -> dict[str, Any]:
        """Return the actual TPC-C seed state without fabricating counts."""
        rows = self.db.fetch_all(
            """
            SELECT c.relname, c.reltuples::bigint AS estimated_rows,
                   pg_total_relation_size(c.oid)::bigint AS total_bytes
            FROM pg_class AS c
            JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relkind = 'r'
            ORDER BY c.relname
            """,
            ("keeninsight_tpcc",),
        )
        table_names = {str(row["relname"]) for row in rows}
        required = {"warehouse", "district", "item", "stock", "customer", "orders", "order_line"}
        if not rows:
            return {"available": False, "schema": "keeninsight_tpcc", "row_counts": {}}
        info = self.db.fetch_one(
            """
            SELECT warehouses, generator_version, loaded_at, row_counts, total_bytes
            FROM keeninsight_tpcc.load_info
            WHERE id = 1
            """
        ) if "load_info" in table_names else None
        row_counts = dict((info or {}).get("row_counts") or {})
        if not row_counts:
            row_counts = {
                str(row["relname"]): int(row["estimated_rows"] or 0)
                for row in rows
            }
        total_bytes = sum(int(row["total_bytes"] or 0) for row in rows)
        return {
            "available": required.issubset(table_names),
            "schema": "keeninsight_tpcc",
            "warehouses": (info or {}).get("warehouses"),
            "generator_version": (info or {}).get("generator_version"),
            "loaded_at": (info or {}).get("loaded_at"),
            "row_counts": row_counts,
            "total_bytes": int((info or {}).get("total_bytes") or total_bytes),
        }

    def reset_demo_mutations(self) -> dict[str, Any]:
        """Restore only the distribution-shift fixture used by the stats case."""
        if not relation_exists(self.db, "keeninsight_demo", "stats_probe"):
            return {"stats_probe_reset": False, "reason": "stats_probe is missing"}
        with self.db.connection() as conn:
            with conn.transaction():
                self._reset_stats_probe_rows(conn)
        return {"stats_probe_reset": True, "state": self._stats_probe_state()}

    def run(
        self,
        name: str,
        phase: str,
        repetitions: int = 5,
        parameters: dict[str, Any] | None = None,
        warmup_repetitions: int = 2,
    ) -> dict[str, Any]:
        scenario, repetitions, warmup_repetitions, effective_parameters, run_parameters = (
            self._prepare_batch_run(name, phase, repetitions, parameters, warmup_repetitions)
        )
        run_id = self._start_run(name, phase, run_parameters)
        return self._execute_batch_run(
            run_id,
            name,
            phase,
            scenario,
            repetitions,
            warmup_repetitions,
            effective_parameters,
            run_parameters,
            False,
        )

    def start_async(
        self,
        name: str,
        phase: str,
        repetitions: int = 5,
        parameters: dict[str, Any] | None = None,
        warmup_repetitions: int = 2,
    ) -> dict[str, Any]:
        """Create a real workload run and execute it outside the HTTP request.

        The run row is created before the worker starts.  This gives the UI a
        durable run id that it can poll while PostgreSQL is doing the actual
        warm-up, timed samples, and EXPLAIN ANALYZE.
        """
        scenario, repetitions, warmup_repetitions, effective_parameters, run_parameters = (
            self._prepare_batch_run(name, phase, repetitions, parameters, warmup_repetitions)
        )
        run_id = self._start_run(name, phase, run_parameters)
        thread = threading.Thread(
            target=self._run_batch_thread,
            args=(
                run_id,
                name,
                phase,
                scenario,
                repetitions,
                warmup_repetitions,
                effective_parameters,
                run_parameters,
            ),
            name=f"keeninsight-workload-{run_id}",
            daemon=True,
        )
        with self._lock:
            self._batch_runs[run_id] = thread
        thread.start()
        row = self.db.fetch_one(
            "SELECT started_at FROM keeninsight.workload_runs WHERE id = %s",
            (run_id,),
        ) or {}
        return {
            "run_id": run_id,
            "scenario": name,
            "scenario_definition": scenario,
            "phase": phase,
            "status": "running",
            "repetitions": repetitions,
            "warmup_repetitions": warmup_repetitions,
            "parameters": run_parameters,
            "started_at": row.get("started_at"),
            "observations": {
                "progress": {
                    "stage": "queued",
                    "completed_samples": 0,
                    "total_samples": repetitions + warmup_repetitions,
                    "timed_samples": 0,
                    "warmup_samples": 0,
                }
            },
        }

    def _prepare_batch_run(
        self,
        name: str,
        phase: str,
        repetitions: int,
        parameters: dict[str, Any] | None,
        warmup_repetitions: int,
    ) -> tuple[dict[str, Any], int, int, dict[str, Any], dict[str, Any]]:
        scenario = get_scenario(name)
        if name == "lock_contention":
            raise WorkloadError("lock_contention is started and stopped, not run as a batch")
        if phase not in {"baseline", "after"}:
            raise WorkloadError("phase must be baseline or after")
        repetitions = max(1, min(int(repetitions), 50))
        warmup_repetitions = max(0, min(int(warmup_repetitions), 10))
        effective_parameters = self._validate_parameters(name, parameters or {})
        run_parameters = {
            **effective_parameters,
            "repetitions": repetitions,
            "warmup_repetitions": warmup_repetitions,
        }
        return scenario, repetitions, warmup_repetitions, effective_parameters, run_parameters

    def _run_batch_thread(
        self,
        run_id: int,
        name: str,
        phase: str,
        scenario: dict[str, Any],
        repetitions: int,
        warmup_repetitions: int,
        effective_parameters: dict[str, Any],
        run_parameters: dict[str, Any],
    ) -> None:
        try:
            self._execute_batch_run(
                run_id,
                name,
                phase,
                scenario,
                repetitions,
                warmup_repetitions,
                effective_parameters,
                run_parameters,
                True,
            )
        except Exception:
            # The worker has already persisted the failure and error text.
            # Avoid an unhandled background-thread traceback in the server log.
            pass
        finally:
            with self._lock:
                self._batch_runs.pop(run_id, None)

    def _execute_batch_run(
        self,
        run_id: int,
        name: str,
        phase: str,
        scenario: dict[str, Any],
        repetitions: int,
        warmup_repetitions: int,
        effective_parameters: dict[str, Any],
        run_parameters: dict[str, Any],
        publish_progress: bool = False,
    ) -> dict[str, Any]:
        self._progress_local.run_id = run_id
        self._progress_local.enabled = publish_progress
        self._publish_progress(
            {
                "stage": "starting",
                "completed_samples": 0,
                "total_samples": repetitions + warmup_repetitions,
                "timed_samples": 0,
                "warmup_samples": 0,
            }
        )
        try:
            if name == "missing_index":
                observations = self._run_missing_index(
                    phase, repetitions, warmup_repetitions, effective_parameters
                )
            elif name == "hash_join_spill":
                observations = self._run_hash_join(
                    phase, repetitions, warmup_repetitions, effective_parameters
                )
            elif name == "aggregate_spill":
                observations = self._run_aggregate_spill(
                    phase, repetitions, warmup_repetitions, effective_parameters
                )
            elif name == "sort_spill":
                observations = self._run_sort_spill(
                    phase, repetitions, warmup_repetitions, effective_parameters
                )
            elif name == "parallel_query_underprovisioned":
                observations = self._run_parallel_query(
                    phase, repetitions, warmup_repetitions, effective_parameters
                )
            elif name == "stale_statistics":
                observations = self._run_stale_statistics(
                    phase, repetitions, warmup_repetitions, effective_parameters
                )
            else:
                raise WorkloadError(f"scenario {name} is not executable")
            self._finish_run(run_id, "completed", observations)
            return {
                "run_id": run_id,
                "scenario": name,
                "scenario_definition": scenario,
                "phase": phase,
                "repetitions": repetitions,
                "warmup_repetitions": warmup_repetitions,
                "parameters": run_parameters,
                **observations,
            }
        except Exception as exc:
            self._finish_run(run_id, "failed", {"error": str(exc)}, error=str(exc))
            raise
        finally:
            self._progress_local.run_id = None
            self._progress_local.enabled = False

    def start_lock_contention(self) -> dict[str, Any]:
        run_id = self._start_run("lock_contention", "contention", {"account_id": 1})
        state = _LockRun(run_id=run_id, dsn=self.db.dsn)
        with self._lock:
            self._lock_runs[run_id] = state
        state.thread = threading.Thread(
            target=state.execute,
            args=(self._finish_lock_run,),
            name=f"keeninsight-lock-{run_id}",
            daemon=True,
        )
        state.thread.start()
        if not state.ready.wait(timeout=5):
            self.stop_lock_contention(run_id)
            raise WorkloadError("lock contention workload did not become ready")
        if state.ready_error:
            self.stop_lock_contention(run_id)
            raise WorkloadError(state.ready_error)
        return {
            "run_id": run_id,
            "scenario": "lock_contention",
            "scenario_definition": get_scenario("lock_contention"),
            "status": "running",
            "message": "The second session is waiting; inspect incidents and pg_locks, then stop the workload.",
        }

    def stop_lock_contention(self, run_id: int) -> dict[str, Any]:
        with self._lock:
            state = self._lock_runs.get(run_id)
        if not state:
            raise WorkloadError(f"lock workload {run_id} was not found")
        state.release.set()
        if state.thread:
            state.thread.join(timeout=10)
        with self._lock:
            self._lock_runs.pop(run_id, None)
        row = self.db.fetch_one(
            "SELECT id, status, observations, error, started_at, finished_at FROM keeninsight.workload_runs WHERE id = %s",
            (run_id,),
        )
        return row or {"run_id": run_id, "status": "stopped"}

    def stop_all(self) -> None:
        self.stop_normal_load()
        with self._lock:
            runs = list(self._lock_runs.values())
            batch_threads = list(self._batch_runs.values())
        for state in runs:
            state.release.set()
        for state in runs:
            if state.thread:
                state.thread.join(timeout=5)
        for thread in batch_threads:
            thread.join(timeout=5)
        with self._lock:
            self._lock_runs.clear()
            self._batch_runs.clear()

    def active_lock_run_ids(self) -> list[int]:
        with self._lock:
            return sorted(self._lock_runs)

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.db.fetch_all(
            """
            SELECT id, scenario, phase, status, parameters, observations,
                   started_at, finished_at, error
            FROM keeninsight.workload_runs
            ORDER BY started_at DESC LIMIT %s
            """,
            (max(1, min(limit, 200)),),
        )

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        return self.db.fetch_one(
            """
            SELECT id, scenario, phase, status, parameters, observations,
                   started_at, finished_at, error
            FROM keeninsight.workload_runs
            WHERE id = %s
            """,
            (run_id,),
        )

    def _run_missing_index(
        self,
        phase: str,
        repetitions: int,
        warmup_repetitions: int,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        query = get_scenario("missing_index")["query"]
        customer_id = int(parameters["customer_id"])
        index_present = index_exists(self.db, "keeninsight_demo", "orders_customer_id_idx")
        timings: list[float] = []
        result_row_count = 0
        plan: dict[str, Any] | None = None
        with psycopg.connect(
            self.db.dsn, row_factory=dict_row, autocommit=True
        ) as conn:
            for warmup_index in range(warmup_repetitions):
                conn.execute(query, (customer_id,)).fetchall()
                self._publish_sample_progress(
                    phase=phase,
                    query=query,
                    warmup_completed=warmup_index + 1,
                    timed_completed=0,
                    total_samples=warmup_repetitions + repetitions,
                    timings=timings,
                )
            counters_before = _database_counter_snapshot(self.db.dsn)
            query_counters_before = _query_counter_snapshot(
                self.db.dsn, query, parameter_count=1
            )
            timed_started = time.perf_counter()
            for timed_index in range(repetitions):
                started = time.perf_counter()
                rows = conn.execute(query, (customer_id,)).fetchall()
                result_row_count = len(rows)
                sample_ms = (time.perf_counter() - started) * 1000
                timings.append(sample_ms)
                self._publish_sample_progress(
                    phase=phase,
                    query=query,
                    warmup_completed=warmup_repetitions,
                    timed_completed=timed_index + 1,
                    total_samples=warmup_repetitions + repetitions,
                    timings=timings,
                    last_sample_ms=sample_ms,
                )
            timed_elapsed_ms = (time.perf_counter() - timed_started) * 1000
            counters_after = _database_counter_snapshot(self.db.dsn)
            query_counters_after = _query_counter_snapshot(
                self.db.dsn, query, parameter_count=1
            )
            plan = _explain_analyze(conn, query, (customer_id,))
        observations = {
            "query": query,
            "phase": phase,
            "parameters": {"customer_id": customer_id},
            "index_present": index_present,
            "timings_ms": timings,
            "median_query_time_ms": statistics.median(timings),
            "p95_query_time_ms": percentile(timings, 0.95),
            "timing_summary": summarize_timings(timings),
            "result": {"row_count": result_row_count},
            "server_execution_time_ms": plan["document"].get("Execution Time"),
            "database_measurement": database_measurement(
                counters_before, counters_after, timed_elapsed_ms
            ),
            "query_measurement": query_measurement(
                query_counters_before, query_counters_after, timed_elapsed_ms
            ),
            "plan": plan,
            "plan_summary": summarize_plan(plan["document"], plan["nodes"]),
            "measurement_note": (
                "Warm-up executions are excluded from timed samples. Client timing "
                "includes the PostgreSQL round trip; server execution time comes "
                "from the separately observed EXPLAIN ANALYZE plan. Database "
                "counter deltas include observer/concurrent activity; no expected "
                "value is substituted."
            ),
        }
        if not index_present:
            proposal = _index_proposal_from_plan(self.db, plan)
            if proposal:
                observations["action_proposal"] = proposal
        return observations

    def _run_hash_join(
        self,
        phase: str,
        repetitions: int,
        warmup_repetitions: int,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        return self._run_work_mem_query(
            "hash_join_spill", phase, repetitions, warmup_repetitions, parameters
        )

    def _run_aggregate_spill(
        self,
        phase: str,
        repetitions: int,
        warmup_repetitions: int,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        return self._run_work_mem_query(
            "aggregate_spill", phase, repetitions, warmup_repetitions, parameters
        )

    def _run_sort_spill(
        self,
        phase: str,
        repetitions: int,
        warmup_repetitions: int,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        return self._run_work_mem_query(
            "sort_spill", phase, repetitions, warmup_repetitions, parameters
        )

    def _run_work_mem_query(
        self,
        scenario_name: str,
        phase: str,
        repetitions: int,
        warmup_repetitions: int,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        query = get_scenario(scenario_name)["query"]
        baseline_work_mem = parameters["baseline_work_mem"]
        timings: list[float] = []
        with psycopg.connect(
            self.db.dsn, row_factory=dict_row, autocommit=True
        ) as conn:
            if phase == "baseline":
                _set_session(conn, "work_mem", baseline_work_mem)
            current_work_mem = conn.execute("SHOW work_mem").fetchone()["work_mem"]
            for warmup_index in range(warmup_repetitions):
                conn.execute(query).fetchone()
                self._publish_sample_progress(
                    phase=phase,
                    query=query,
                    warmup_completed=warmup_index + 1,
                    timed_completed=0,
                    total_samples=warmup_repetitions + repetitions,
                    timings=timings,
                )
            counters_before = _database_counter_snapshot(self.db.dsn)
            query_counters_before = _query_counter_snapshot(
                self.db.dsn, query, parameter_count=0
            )
            timed_started = time.perf_counter()
            for timed_index in range(repetitions):
                started = time.perf_counter()
                result = conn.execute(query).fetchone()
                sample_ms = (time.perf_counter() - started) * 1000
                timings.append(sample_ms)
                self._publish_sample_progress(
                    phase=phase,
                    query=query,
                    warmup_completed=warmup_repetitions,
                    timed_completed=timed_index + 1,
                    total_samples=warmup_repetitions + repetitions,
                    timings=timings,
                    last_sample_ms=sample_ms,
                )
            timed_elapsed_ms = (time.perf_counter() - timed_started) * 1000
            counters_after = _database_counter_snapshot(self.db.dsn)
            query_counters_after = _query_counter_snapshot(
                self.db.dsn, query, parameter_count=0
            )
            plan = _explain_analyze(conn, query, ())
        observations = {
            "query": query,
            "phase": phase,
            "parameters": {"baseline_work_mem": baseline_work_mem},
            "phase_work_mem": current_work_mem,
            "timings_ms": timings,
            "median_query_time_ms": statistics.median(timings),
            "p95_query_time_ms": percentile(timings, 0.95),
            "timing_summary": summarize_timings(timings),
            "server_execution_time_ms": plan["document"].get("Execution Time"),
            "result": dict(result) if result is not None else None,
            "database_measurement": database_measurement(
                counters_before, counters_after, timed_elapsed_ms
            ),
            "query_measurement": query_measurement(
                query_counters_before, query_counters_after, timed_elapsed_ms
            ),
            "plan": plan,
            "plan_summary": summarize_plan(plan["document"], plan["nodes"]),
            "measurement_note": (
                "Warm-up executions are excluded from timed samples. Baseline uses "
                "a session-only work_mem value; after-phase uses the actual new "
                "session default after the typed database action. PostgreSQL plan "
                "nodes and temporary-block counters are inspected for operator-specific "
                "spill evidence. Client timing "
                "includes the PostgreSQL round trip; no expected value is substituted."
            ),
        }
        if phase == "baseline":
            action = _work_mem_action_from_plan(
                self.db, plan, prefer_sort=scenario_name == "sort_spill"
            )
            if action:
                observations["action_proposal"] = action
        return observations

    def _run_parallel_query(
        self,
        phase: str,
        repetitions: int,
        warmup_repetitions: int,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        query = get_scenario("parallel_query_underprovisioned")["query"]
        baseline_workers = parameters["baseline_parallel_workers"]
        timings: list[float] = []
        with psycopg.connect(
            self.db.dsn, row_factory=dict_row, autocommit=True
        ) as conn:
            if phase == "baseline":
                _set_session(
                    conn,
                    "max_parallel_workers_per_gather",
                    str(baseline_workers),
                )
            current_workers = conn.execute(
                "SHOW max_parallel_workers_per_gather"
            ).fetchone()["max_parallel_workers_per_gather"]
            for warmup_index in range(warmup_repetitions):
                conn.execute(query).fetchone()
                self._publish_sample_progress(
                    phase=phase,
                    query=query,
                    warmup_completed=warmup_index + 1,
                    timed_completed=0,
                    total_samples=warmup_repetitions + repetitions,
                    timings=timings,
                )
            counters_before = _database_counter_snapshot(self.db.dsn)
            query_counters_before = _query_counter_snapshot(
                self.db.dsn, query, parameter_count=0
            )
            timed_started = time.perf_counter()
            for timed_index in range(repetitions):
                started = time.perf_counter()
                result = conn.execute(query).fetchone()
                sample_ms = (time.perf_counter() - started) * 1000
                timings.append(sample_ms)
                self._publish_sample_progress(
                    phase=phase,
                    query=query,
                    warmup_completed=warmup_repetitions,
                    timed_completed=timed_index + 1,
                    total_samples=warmup_repetitions + repetitions,
                    timings=timings,
                    last_sample_ms=sample_ms,
                )
            timed_elapsed_ms = (time.perf_counter() - timed_started) * 1000
            counters_after = _database_counter_snapshot(self.db.dsn)
            query_counters_after = _query_counter_snapshot(
                self.db.dsn, query, parameter_count=0
            )
            plan = _explain_analyze(conn, query, ())
        observations = {
            "query": query,
            "phase": phase,
            "parameters": {"baseline_parallel_workers": baseline_workers},
            "phase_parallel_workers": current_workers,
            "timings_ms": timings,
            "median_query_time_ms": statistics.median(timings),
            "p95_query_time_ms": percentile(timings, 0.95),
            "timing_summary": summarize_timings(timings),
            "result": dict(result) if result is not None else None,
            "server_execution_time_ms": plan["document"].get("Execution Time"),
            "database_measurement": database_measurement(
                counters_before, counters_after, timed_elapsed_ms
            ),
            "query_measurement": query_measurement(
                query_counters_before, query_counters_after, timed_elapsed_ms
            ),
            "plan": plan,
            "plan_summary": summarize_plan(plan["document"], plan["nodes"]),
            "measurement_note": (
                "Warm-up executions are excluded from timed samples. Baseline "
                "uses a session-only max_parallel_workers_per_gather value; "
                "after-phase uses the actual new-session database setting. "
                "Client timing includes the PostgreSQL round trip; actual "
                "workers must be verified in EXPLAIN ANALYZE."
            ),
        }
        parallel_nodes = [
            node
            for node in plan["nodes"]
            if node.get("Node Type") in {
                "Gather",
                "Gather Merge",
                "Parallel Seq Scan",
                "Parallel Hash",
            }
            or node.get("Workers Planned")
        ]
        if phase == "baseline" and not parallel_nodes:
            from .diagnose import parallel_workers_action

            action = parallel_workers_action(self.db, plan=plan)
            if action:
                observations["action_proposal"] = action
        return observations

    def _validate_parameters(
        self, scenario: str, parameters: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate user knobs against the connected PostgreSQL catalog/data."""
        if not isinstance(parameters, dict):
            raise WorkloadError("workload parameters must be a JSON object")
        if scenario == "missing_index":
            unknown = sorted(set(parameters) - {"customer_id"})
            if unknown:
                raise WorkloadError(f"unsupported missing_index parameter(s): {unknown}")
            default = int(get_scenario(scenario)["parameters"]["customer_id"])
            raw_customer_id = parameters.get("customer_id", default)
            if isinstance(raw_customer_id, bool):
                raise WorkloadError("customer_id must be an integer")
            try:
                customer_id = int(raw_customer_id)
            except (TypeError, ValueError) as exc:
                raise WorkloadError("customer_id must be an integer") from exc
            try:
                row = self.db.fetch_one(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM keeninsight_demo.orders WHERE customer_id = %s
                    ) AS has_rows
                    """,
                    (customer_id,),
                )
            except Exception as exc:
                raise WorkloadError(
                    f"could not validate customer_id against demo data: {exc}"
                ) from exc
            if not row or not row["has_rows"]:
                raise WorkloadError(
                    f"customer_id {customer_id} has no rows in keeninsight_demo.orders"
                )
            return {"customer_id": customer_id}
        if scenario in {"hash_join_spill", "aggregate_spill", "sort_spill"}:
            unknown = sorted(set(parameters) - {"baseline_work_mem"})
            if unknown:
                raise WorkloadError(f"unsupported {scenario} parameter(s): {unknown}")
            default = str(get_scenario(scenario)["parameters"]["baseline_work_mem"])
            value = str(parameters.get("baseline_work_mem", default)).strip()
            if not value or ";" in value or "\x00" in value:
                raise WorkloadError("baseline_work_mem is not a valid PostgreSQL memory value")
            try:
                row = self.db.fetch_one(
                    "SELECT pg_size_bytes(%s) AS bytes",
                    (value,),
                )
            except Exception as exc:
                raise WorkloadError(
                    f"PostgreSQL rejected baseline_work_mem {value!r}: {exc}"
                ) from exc
            try:
                memory_bytes = int(row["bytes"])
            except (TypeError, ValueError, KeyError):
                memory_bytes = 0
            if memory_bytes <= 0 or memory_bytes > 256 * 1024 * 1024:
                raise WorkloadError(
                    "baseline_work_mem must be a positive value no larger than 256MB"
                )
            return {"baseline_work_mem": value}
        if scenario == "parallel_query_underprovisioned":
            unknown = sorted(set(parameters) - {"baseline_parallel_workers"})
            if unknown:
                raise WorkloadError(
                    "unsupported parallel_query_underprovisioned parameter(s): "
                    f"{unknown}"
                )
            default = int(
                get_scenario(scenario)["parameters"]["baseline_parallel_workers"]
            )
            raw_workers = parameters.get("baseline_parallel_workers", default)
            if isinstance(raw_workers, bool):
                raise WorkloadError("baseline_parallel_workers must be an integer")
            try:
                workers = int(raw_workers)
            except (TypeError, ValueError) as exc:
                raise WorkloadError(
                    "baseline_parallel_workers must be an integer"
                ) from exc
            if workers < 0:
                raise WorkloadError("baseline_parallel_workers cannot be negative")
            setting = self.db.fetch_one(
                """
                SELECT min_val, max_val
                FROM pg_settings
                WHERE name = 'max_parallel_workers_per_gather'
                """
            )
            if not setting:
                raise WorkloadError(
                    "max_parallel_workers_per_gather is unavailable on this server"
                )
            try:
                minimum = int(setting["min_val"])
                maximum = int(setting["max_val"])
            except (TypeError, ValueError) as exc:
                raise WorkloadError(
                    "PostgreSQL returned invalid parallel-worker bounds"
                ) from exc
            if workers < minimum or workers > maximum:
                raise WorkloadError(
                    "baseline_parallel_workers must be within PostgreSQL's "
                    f"range [{minimum}, {maximum}]"
                )
            return {"baseline_parallel_workers": workers}
        if scenario == "stale_statistics":
            if parameters:
                unknown = sorted(parameters)
                raise WorkloadError(f"unsupported stale_statistics parameter(s): {unknown}")
            if not relation_exists(self.db, "keeninsight_demo", "stats_probe"):
                raise WorkloadError(
                    "keeninsight_demo.stats_probe is missing; bootstrap the demo data first"
                )
            row = self.db.fetch_one(
                "SELECT count(*)::bigint AS row_count FROM keeninsight_demo.stats_probe"
            )
            if not row or int(row.get("row_count") or 0) < STATS_PROBE_TOTAL_ROWS:
                raise WorkloadError(
                    "stats_probe does not contain the expected demonstration cardinality"
                )
            return {}
        raise WorkloadError(f"scenario {scenario} does not accept batch parameters")

    def propose_action(self, run_id: int) -> dict[str, Any]:
        row = self.db.fetch_one(
            "SELECT id, scenario, observations FROM keeninsight.workload_runs WHERE id = %s",
            (run_id,),
        )
        if not row:
            raise KeyError(f"workload run {run_id} was not found")
        proposal = (row.get("observations") or {}).get("action_proposal")
        if not proposal:
            raise WorkloadError("this run produced no evidence-backed tuning proposal")
        proposal = dict(proposal)
        proposal["source"] = {
            "type": "workload_run",
            "run_id": row["id"],
            "scenario": row["scenario"],
        }
        return create_action(self.db, proposal)

    def _start_run(self, scenario: str, phase: str, parameters: dict[str, Any]) -> int:
        row = self.db.fetch_one(
            """
            INSERT INTO keeninsight.workload_runs (scenario, phase, status, parameters)
            VALUES (%s, %s, 'running', %s::jsonb)
            RETURNING id
            """,
            (scenario, phase, json.dumps(parameters)),
        )
        if not row:
            raise RuntimeError("could not create workload run")
        return int(row["id"])

    def _finish_run(
        self,
        run_id: int,
        status: str,
        observations: dict[str, Any],
        error: str | None = None,
    ) -> None:
        self._finish_lock_run(run_id, status, observations, error)

    def _publish_progress(self, progress: dict[str, Any]) -> None:
        """Persist only real in-flight sample progress for the runner page.

        Progress writes happen outside the measured query connection and are
        deliberately best-effort: telemetry persistence must never turn a
        successful PostgreSQL measurement into a failed workload run.
        """
        run_id = getattr(self._progress_local, "run_id", None)
        if not run_id or not getattr(self._progress_local, "enabled", False):
            return
        try:
            self.db.execute(
                """
                UPDATE keeninsight.workload_runs
                SET observations = %s::jsonb
                WHERE id = %s AND status = 'running'
                """,
                (json.dumps({"progress": progress}), run_id),
            )
        except Exception:
            return

    def _publish_sample_progress(
        self,
        *,
        phase: str,
        query: str,
        warmup_completed: int,
        timed_completed: int,
        total_samples: int,
        timings: list[float],
        last_sample_ms: float | None = None,
    ) -> None:
        self._publish_progress(
            {
                "stage": "measuring" if timed_completed else "warming_up",
                "phase": phase,
                "query": query,
                "completed_samples": warmup_completed + timed_completed,
                "total_samples": total_samples,
                "warmup_samples": warmup_completed,
                "timed_samples": timed_completed,
                "timings_ms": list(timings),
                "last_sample_ms": last_sample_ms,
            }
        )

    def _finish_lock_run(
        self,
        run_id: int,
        status: str,
        observations: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self.db.execute(
            """
            UPDATE keeninsight.workload_runs
            SET status = %s, observations = %s::jsonb,
                error = %s, finished_at = CASE WHEN %s = 'running' THEN NULL ELSE now() END
            WHERE id = %s
            """,
            (status, json.dumps(observations or {}), error, status, run_id),
        )

    def _stats_probe_state(self) -> dict[str, Any]:
        if not relation_exists(self.db, "keeninsight_demo", "stats_probe"):
            return {"exists": False}
        row = self.db.fetch_one(
            """
            SELECT count(*)::bigint AS row_count,
                   count(*) FILTER (WHERE category = 'common')::bigint AS common_rows,
                   count(*) FILTER (WHERE category = 'rare')::bigint AS rare_rows,
                   pg_total_relation_size('keeninsight_demo.stats_probe'::regclass)::bigint
                       AS total_bytes
            FROM keeninsight_demo.stats_probe
            """
        ) or {}
        stats = self.db.fetch_one(
            """
            SELECT s.last_analyze, s.last_autoanalyze, s.n_live_tup,
                   c.reloptions
            FROM pg_stat_user_tables s
            JOIN pg_class c ON c.oid = 'keeninsight_demo.stats_probe'::regclass
            WHERE s.schemaname = 'keeninsight_demo' AND s.relname = 'stats_probe'
            """
        ) or {}
        options = stats.get("reloptions") or []
        stats["autovacuum_disabled_for_probe"] = any(
            str(option).lower() == "autovacuum_enabled=false" for option in options
        )
        normalized_stats = {
            key: value.isoformat() if hasattr(value, "isoformat") else value
            for key, value in stats.items()
        }
        return {"exists": True, **row, **normalized_stats}

    def _run_stale_statistics(
        self,
        phase: str,
        repetitions: int,
        warmup_repetitions: int,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        """Measure a real catalog-estimate mismatch and its ANALYZE recovery."""
        query = get_scenario("stale_statistics")["query"]
        if phase == "baseline":
            # Establish the same initial histogram for every baseline, then
            # shift the data distribution without refreshing its statistics.
            with psycopg.connect(self.db.dsn, autocommit=True) as prepare:
                # Re-seed instead of repeatedly updating the same tuples.  It
                # keeps both the initial histogram and physical table size
                # stable across repeated demonstrations.
                self._reset_stats_probe_rows(prepare)
                prepare.execute(
                    f"""
                    UPDATE keeninsight_demo.stats_probe
                    SET category = CASE
                        WHEN probe_id <= {STATS_PROBE_PRESSURE_RARE_ROWS}
                        THEN 'rare' ELSE 'common' END
                    """
                )

        timings: list[float] = []
        with psycopg.connect(
            self.db.dsn, row_factory=dict_row, autocommit=True
        ) as conn:
            for warmup_index in range(warmup_repetitions):
                conn.execute(query).fetchone()
                self._publish_sample_progress(
                    phase=phase,
                    query=query,
                    warmup_completed=warmup_index + 1,
                    timed_completed=0,
                    total_samples=warmup_repetitions + repetitions,
                    timings=timings,
                )
            counters_before = _database_counter_snapshot(self.db.dsn)
            query_counters_before = _query_counter_snapshot(
                self.db.dsn, query, parameter_count=0
            )
            timed_started = time.perf_counter()
            result: dict[str, Any] | None = None
            for timed_index in range(repetitions):
                started = time.perf_counter()
                result = conn.execute(query).fetchone()
                sample_ms = (time.perf_counter() - started) * 1000
                timings.append(sample_ms)
                self._publish_sample_progress(
                    phase=phase,
                    query=query,
                    warmup_completed=warmup_repetitions,
                    timed_completed=timed_index + 1,
                    total_samples=warmup_repetitions + repetitions,
                    timings=timings,
                    last_sample_ms=sample_ms,
                )
            timed_elapsed_ms = (time.perf_counter() - timed_started) * 1000
            counters_after = _database_counter_snapshot(self.db.dsn)
            query_counters_after = _query_counter_snapshot(
                self.db.dsn, query, parameter_count=0
            )
            plan = _explain_analyze(conn, query, ())
        statistics_observation = _stats_estimate_observation(plan)
        observations: dict[str, Any] = {
            "query": query,
            "phase": phase,
            "parameters": {},
            "timings_ms": timings,
            "median_query_time_ms": statistics.median(timings),
            "p95_query_time_ms": percentile(timings, 0.95),
            "timing_summary": summarize_timings(timings),
            "server_execution_time_ms": plan["document"].get("Execution Time"),
            "result": dict(result or {}),
            "database_measurement": database_measurement(
                counters_before, counters_after, timed_elapsed_ms
            ),
            "query_measurement": query_measurement(
                query_counters_before, query_counters_after, timed_elapsed_ms
            ),
            "statistics_measurement": {
                **statistics_observation,
                "table_state": self._stats_probe_state(),
            },
            "plan": plan,
            "plan_summary": summarize_plan(plan["document"], plan["nodes"]),
            "measurement_note": (
                "The baseline re-seeds the compact probe and refreshes its initial histogram, then "
                "shifts 92% of rows to the rare value without ANALYZE. The after "
                "phase uses the same SQL after a typed ANALYZE action. Effectiveness "
                "is judged by estimate/actual agreement and result preservation, "
                "not by an invented latency improvement."
            ),
        }
        if phase == "baseline" and statistics_observation["estimate_error_ratio"] >= 2.0:
            observations["action_proposal"] = {
                "action_type": "refresh_statistics",
                "target": {
                    "schema": "keeninsight_demo",
                    "table": "stats_probe",
                },
                "description": (
                    "Refresh PostgreSQL statistics for keeninsight_demo.stats_probe "
                    "after the observed distribution shift; verify estimate/actual "
                    "agreement on the same query."
                ),
                "validation_required": True,
                "source": "EXPLAIN ANALYZE estimated/actual rows and pg_stat_user_tables",
                "proposal_evidence": {
                    "relation": statistics_observation.get("relation"),
                    "estimated_rows": statistics_observation.get("estimated_rows"),
                    "actual_rows": statistics_observation.get("actual_rows"),
                    "estimate_error_ratio": statistics_observation.get("estimate_error_ratio"),
                    "minimum_error_ratio_for_proposal": 2.0,
                    "calculation": "refresh_statistics_when_estimate_error_ratio >= 2",
                },
            }
        return observations

    def _reset_stats_probe_rows(self, connection) -> None:
        """Recreate the compact initial distribution outside timed work."""
        connection.execute("TRUNCATE TABLE keeninsight_demo.stats_probe")
        connection.execute(
            f"""
            INSERT INTO keeninsight_demo.stats_probe (probe_id, category, payload)
            SELECT g,
                   CASE WHEN g <= {STATS_PROBE_TOTAL_ROWS - STATS_PROBE_INITIAL_RARE_ROWS}
                        THEN 'common' ELSE 'rare' END,
                   md5(g::text || ':keeninsight-stats-probe')
            FROM generate_series(1, {STATS_PROBE_TOTAL_ROWS}) AS g
            """
        )
        connection.execute("ANALYZE keeninsight_demo.stats_probe")


class _NormalLoadRun:
    def __init__(self, dsn: str, definition: dict[str, Any], rate: float) -> None:
        self.dsn = dsn
        self.definition = definition
        self.rate = rate
        self.started_at = datetime.now(timezone.utc)
        self.finished_at: datetime | None = None
        self.running = True
        self.calls = 0
        self.last_latency_ms: float | None = None
        self.last_error: str | None = None
        self.backend_pid: int | None = None
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def execute(self) -> None:
        query = self.definition["query"]
        parameters = tuple(self.definition.get("parameters") or ())
        try:
            with psycopg.connect(
                self.dsn,
                row_factory=dict_row,
                autocommit=True,
                application_name=f"keeninsight-normal-{self.definition['name']}",
            ) as conn:
                self.backend_pid = conn.info.backend_pid
                while not self.stop.is_set():
                    started = time.perf_counter()
                    conn.execute(query, parameters).fetchall()
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    with self._lock:
                        self.calls += 1
                        self.last_latency_ms = elapsed_ms
                        self.last_error = None
                    period = 1.0 / self.rate
                    self.stop.wait(max(0.0, period - (elapsed_ms / 1000.0)))
        except Exception as exc:
            with self._lock:
                self.last_error = str(exc)
        finally:
            with self._lock:
                self.running = False
                self.finished_at = datetime.now(timezone.utc)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "running": self.running,
                "name": self.definition["name"],
                "title": self.definition["title"],
                "description": self.definition["description"],
                "rate_per_second": self.rate,
                "calls": self.calls,
                "last_latency_ms": self.last_latency_ms,
                "last_error": self.last_error,
                "backend_pid": self.backend_pid,
                "started_at": self.started_at.isoformat(),
                "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            }


class _LockRun:
    def __init__(self, run_id: int, dsn: str) -> None:
        self.run_id = run_id
        self.dsn = dsn
        self.ready = threading.Event()
        self.ready_error: str | None = None
        self.release = threading.Event()
        self.thread: threading.Thread | None = None

    def execute(self, finish) -> None:
        observations: dict[str, Any] = {}
        holder = None
        waiter = None
        try:
            holder = psycopg.connect(self.dsn, row_factory=dict_row)
            waiter = psycopg.connect(self.dsn, row_factory=dict_row)
            holder.execute(
                "SELECT balance FROM keeninsight_demo.accounts WHERE account_id = 1 FOR UPDATE"
            )
            holder.commit()
            # Re-open a transaction and retain the row lock until release.
            holder.execute("BEGIN")
            holder.execute(
                "SELECT balance FROM keeninsight_demo.accounts WHERE account_id = 1 FOR UPDATE"
            )
            observations["holder_pid"] = holder.info.backend_pid
            observations["waiter_pid"] = waiter.info.backend_pid
            waiter_result: dict[str, Any] = {}

            def wait_for_lock() -> None:
                try:
                    waiter.execute(
                        "SELECT balance FROM keeninsight_demo.accounts WHERE account_id = 1 FOR UPDATE"
                    )
                    waiter.commit()
                    waiter_result["wait_completed"] = True
                except Exception as exc:
                    waiter_result["error"] = str(exc)

            waiter_thread = threading.Thread(
                target=wait_for_lock,
                name=f"keeninsight-lock-waiter-{self.run_id}",
                daemon=True,
            )
            waiter_thread.start()
            if not self._wait_until_blocked(waiter.info.backend_pid):
                self.ready_error = "the waiter did not enter a PostgreSQL lock wait"
            self.ready.set()
            if self.ready_error:
                raise WorkloadError(self.ready_error)
            # Keep the holder alive until the user explicitly stops the
            # scenario.  The waiter is a separate session, so pg_stat_activity
            # and pg_locks can observe the real wait.
            while not self.release.wait(0.1):
                pass
            holder.rollback()
            waiter_thread.join(timeout=10)
            observations.update(waiter_result)
            finish(self.run_id, "completed", observations)
        except Exception as exc:
            finish(self.run_id, "failed", observations, str(exc))
        finally:
            if holder:
                try:
                    holder.rollback()
                except Exception:
                    pass
                holder.close()
            if waiter:
                try:
                    waiter.rollback()
                except Exception:
                    pass
                waiter.close()

    def stop(self) -> None:
        self.release.set()

    def _wait_until_blocked(self, waiter_pid: int, timeout: float = 4.0) -> bool:
        deadline = time.monotonic() + timeout
        try:
            with psycopg.connect(self.dsn, row_factory=dict_row) as monitor:
                while time.monotonic() < deadline:
                    row = monitor.execute(
                        """
                        SELECT wait_event_type, wait_event,
                               COALESCE(array_length(pg_blocking_pids(pid), 1), 0)
                                   AS blocker_count
                        FROM pg_stat_activity WHERE pid = %s
                        """,
                        (waiter_pid,),
                    ).fetchone()
                    if not row:
                        return False
                    if row["wait_event_type"] == "Lock" or row["blocker_count"] > 0:
                        return True
                    time.sleep(0.05)
        except Exception:
            return False
        return False


def _explain_analyze(conn, query: str, params: tuple[Any, ...]) -> dict[str, Any]:
    row = conn.execute(
        "EXPLAIN (ANALYZE TRUE, BUFFERS TRUE, FORMAT JSON, VERBOSE TRUE, SETTINGS TRUE) " + query,
        params,
    ).fetchone()
    raw = next(iter(row.values()))
    if isinstance(raw, str):
        raw = json.loads(raw)
    document = raw[0] if isinstance(raw, list) else raw
    root = document.get("Plan") or {}
    return {"document": document, "root": root, "nodes": flatten_plan(root)}


def _stats_estimate_observation(plan: dict[str, Any]) -> dict[str, Any]:
    """Extract the strongest estimate/actual row mismatch from a plan."""
    candidates = []
    for node in plan.get("nodes") or []:
        if node.get("Node Type") not in {
            "Seq Scan",
            "Index Scan",
            "Bitmap Heap Scan",
            "Bitmap Index Scan",
        }:
            continue
        try:
            estimated = float(node.get("Plan Rows"))
            actual = float(node.get("Actual Rows")) * max(
                1.0, float(node.get("Actual Loops") or 1.0)
            )
        except (TypeError, ValueError):
            continue
        if estimated < 0 or actual < 0:
            continue
        ratio = max((actual + 1.0) / (estimated + 1.0), (estimated + 1.0) / (actual + 1.0))
        candidates.append(
            {
                "node_type": node.get("Node Type"),
                "relation": node.get("Relation Name"),
                "estimated_rows": estimated,
                "actual_rows": actual,
                "estimate_error_ratio": ratio,
            }
        )
    if not candidates:
        return {
            "node_type": None,
            "relation": None,
            "estimated_rows": None,
            "actual_rows": None,
            "estimate_error_ratio": 0.0,
        }
    return max(candidates, key=lambda item: item["estimate_error_ratio"])


def _set_session(conn, parameter: str, value: str) -> None:
    """Set a validated session setting for one isolated workload connection."""
    conn.execute(
        sql.SQL("SET {} TO {}").format(sql.Identifier(parameter), sql.Literal(value))
    )


def _work_mem_action_from_plan(
    db: Database, plan: dict[str, Any], *, prefer_sort: bool = False
) -> dict[str, Any] | None:
    """Create a work_mem proposal only when the observed plan shows a spill."""
    candidates = [
        node
        for node in plan.get("nodes") or []
        if hash_batches(node) > 1 or _node_has_temp_blocks(node)
    ]
    if not candidates:
        return None
    # Prefer a multi-batch Hash node, then an operator that can explain an
    # aggregate/sort spill.  The setting validator still performs the final
    # catalog and range checks before any mutation is allowed.
    def priority(node: dict[str, Any]) -> tuple[int, int]:
        if hash_batches(node) > 1:
            return (0, 0)
        if prefer_sort and node.get("Node Type") == "Sort":
            return (0, 1)
        if node.get("Node Type") in {"HashAggregate", "Aggregate", "WindowAgg"}:
            return (1, 0)
        if node.get("Node Type") == "Sort":
            return (1, 1)
        return (2, 0)

    candidates.sort(key=priority)
    from .diagnose import work_mem_action

    return work_mem_action(db, candidates[0])


def _node_has_temp_blocks(node: dict[str, Any]) -> bool:
    for field in ("Temp Read Blocks", "Temp Written Blocks"):
        try:
            if float(node.get(field) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


DATABASE_COUNTER_FIELDS = (
    "xact_commit",
    "xact_rollback",
    "tup_returned",
    "tup_fetched",
    "tup_inserted",
    "tup_updated",
    "tup_deleted",
    "blks_read",
    "blks_hit",
    "temp_files",
    "temp_bytes",
)


def _database_counter_snapshot(dsn: str) -> dict[str, Any]:
    """Read the real database counters surrounding a timed workload window."""
    with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as conn:
        row = conn.execute(
            """
            SELECT xact_commit, xact_rollback, tup_returned, tup_fetched,
                   tup_inserted, tup_updated, tup_deleted, blks_read,
                   blks_hit, temp_files, temp_bytes
            FROM pg_stat_database
            WHERE datname = current_database()
            """
        ).fetchone()
    return dict(row or {})


def database_measurement(
    before: dict[str, Any], after: dict[str, Any], elapsed_ms: float
) -> dict[str, Any]:
    """Return auditable counter deltas and rates, preserving reset information."""
    deltas: dict[str, float] = {}
    reset_fields: list[str] = []
    for field in DATABASE_COUNTER_FIELDS:
        try:
            current = float(after.get(field) or 0)
            previous = float(before.get(field) or 0)
        except (TypeError, ValueError):
            continue
        if current < previous:
            deltas[field] = current
            reset_fields.append(field)
        else:
            deltas[field] = current - previous
    seconds = max(float(elapsed_ms) / 1000.0, 0.001)
    return {
        "elapsed_ms": float(elapsed_ms),
        "counters_before": before,
        "counters_after": after,
        "counter_deltas": deltas,
        "counters_per_second": {key: value / seconds for key, value in deltas.items()},
        "reset_fields": reset_fields,
        "note": "Rates include the measured workload plus any concurrent database activity and observer transactions.",
    }


def _query_counter_snapshot(
    dsn: str, query: str, *, parameter_count: int
) -> dict[str, Any]:
    """Read the exact pg_stat_statements row for the measured SQL template."""
    normalized = _pgss_normalized_query(query, parameter_count)
    modern_sql = """
        SELECT s.queryid::text AS query_id, s.query, s.calls,
               s.total_exec_time::double precision AS total_exec_time_ms,
               s.rows, s.shared_blks_hit, s.shared_blks_read,
               s.temp_blks_read, s.temp_blks_written
        FROM pg_stat_statements s
        WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
          AND s.query = %s
    """
    legacy_sql = """
        SELECT s.queryid::text AS query_id, s.query, s.calls,
               s.total_time::double precision AS total_exec_time_ms,
               s.rows, s.shared_blks_hit, s.shared_blks_read,
               s.temp_blks_read, s.temp_blks_written
        FROM pg_stat_statements s
        WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
          AND s.query = %s
    """
    try:
        with psycopg.connect(dsn, row_factory=dict_row, autocommit=True) as conn:
            try:
                row = conn.execute(modern_sql, (normalized,)).fetchone()
            except (UndefinedColumn, UndefinedTable):
                row = conn.execute(legacy_sql, (normalized,)).fetchone()
    except (UndefinedColumn, UndefinedTable, psycopg.errors.InsufficientPrivilege) as exc:
        return {
            "available": False,
            "query": normalized,
            "error": str(exc),
        }
    return {
        "available": bool(row),
        "query": normalized,
        "row": dict(row) if row else None,
    }


def _pgss_normalized_query(query: str, parameter_count: int) -> str:
    """Match psycopg's positional placeholders as stored by pg_stat_statements."""
    normalized = str(query)
    for number in range(1, parameter_count + 1):
        normalized = normalized.replace("%s", f"${number}", 1)
    return normalized


def query_measurement(
    before: dict[str, Any], after: dict[str, Any], elapsed_ms: float
) -> dict[str, Any]:
    """Compare the exact measured statement's cumulative counters."""
    if not before.get("available") or not after.get("available"):
        return {
            "available": False,
            "query": after.get("query") or before.get("query"),
            "before": before,
            "after": after,
            "note": "The exact statement was not available in pg_stat_statements; no query-counter effect is inferred.",
        }
    before_row = before.get("row") or {}
    after_row = after.get("row") or {}
    fields = (
        "calls",
        "total_exec_time_ms",
        "rows",
        "shared_blks_hit",
        "shared_blks_read",
        "temp_blks_read",
        "temp_blks_written",
    )
    deltas: dict[str, float] = {}
    reset_fields: list[str] = []
    for field in fields:
        try:
            current = float(after_row.get(field) or 0)
            previous = float(before_row.get(field) or 0)
        except (TypeError, ValueError):
            continue
        if current < previous:
            deltas[field] = current
            reset_fields.append(field)
        else:
            deltas[field] = current - previous
    seconds = max(float(elapsed_ms) / 1000.0, 0.001)
    return {
        "available": True,
        "query": after.get("query") or before.get("query"),
        "query_id": after_row.get("query_id") or before_row.get("query_id"),
        "before": before_row,
        "after": after_row,
        "counter_deltas": deltas,
        "counters_per_second": {key: value / seconds for key, value in deltas.items()},
        "reset_fields": reset_fields,
        "note": "The before snapshot is taken after warmups; the delta covers timed calls, plus any concurrent execution of the same statement template.",
    }


def summarize_timings(values: list[float]) -> dict[str, Any]:
    """Summarize exactly the timed samples; never pad or smooth the sample list."""
    if not values:
        raise ValueError("cannot summarize no measurements")
    mean = statistics.fmean(values)
    return {
        "count": len(values),
        "min_ms": min(values),
        "max_ms": max(values),
        "mean_ms": mean,
        "median_ms": statistics.median(values),
        "p95_ms": percentile(values, 0.95),
        "stdev_ms": statistics.stdev(values) if len(values) > 1 else 0.0,
        "relative_stdev": (statistics.stdev(values) / mean) if len(values) > 1 and mean else 0.0,
    }


def _index_proposal_from_plan(
    db: Database, plan: dict[str, Any]
) -> dict[str, Any] | None:
    """Derive a narrow index proposal from an observed simple equality filter.

    This intentionally declines joins, expressions, compound predicates, and
    unknown relations.  The server-side action validator performs the final
    catalog checks before anything can be applied.
    """
    for node in plan.get("nodes") or []:
        if node.get("Node Type") != "Seq Scan" or not node.get("Filter"):
            continue
        schema_name = str(node.get("Schema") or "public")
        table_name = str(node.get("Relation Name") or "")
        column_name = _simple_equality_column(
            str(node.get("Filter")),
            {str(node.get("Alias") or ""), table_name},
        )
        if not (
            safe_identifier(schema_name)
            and safe_identifier(table_name)
            and safe_identifier(column_name or "")
        ):
            continue
        if not relation_exists(db, schema_name, table_name):
            continue
        actual_columns = {
            row["column_name"] for row in relation_columns(db, schema_name, table_name)
        }
        if column_name not in actual_columns:
            continue
        index_name = _derived_index_name(table_name, column_name)
        if index_exists(db, schema_name, index_name):
            continue
        return {
            "action_type": "create_index",
            "target": {
                "schema": schema_name,
                "table": table_name,
                "index": index_name,
                "columns": [column_name],
            },
            "description": (
                "Create a btree index on the equality predicate observed in "
                "the sequential-scan plan, then validate the query again."
            ),
            "proposal_evidence": {
                "node_type": node.get("Node Type"),
                "relation": f"{schema_name}.{table_name}",
                "filter": node.get("Filter"),
                "plan_rows": node.get("Plan Rows"),
                "actual_rows": node.get("Actual Rows"),
                "predicate_column": column_name,
                "calculation": "derive_single_column_btree_from_observed_simple_equality_filter",
            },
        }
    return None


def _simple_equality_column(filter_text: str, qualifiers: set[str]) -> str | None:
    normalized = filter_text.strip()
    while _wrapped_by_parentheses(normalized):
        normalized = normalized[1:-1].strip()
    match = re.fullmatch(
        r"(?:(?P<qual>[A-Za-z_][A-Za-z0-9_]*)\.)?"
        r"(?P<column>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
        r"(?:'[^']*(?:''[^']*)*'(?:\:\:[A-Za-z_][A-Za-z0-9_ ]*)?|"
        r"[-+]?[0-9]+(?:\.[0-9]+)?(?:\:\:[A-Za-z_][A-Za-z0-9_ ]*)?)",
        normalized,
    )
    if not match:
        return None
    qualifier = match.group("qual")
    if qualifier and qualifier not in qualifiers:
        return None
    return match.group("column")


def _wrapped_by_parentheses(value: str) -> bool:
    if len(value) < 2 or value[0] != "(" or value[-1] != ")":
        return False
    depth = 0
    quoted = False
    index = 0
    while index < len(value):
        char = value[index]
        if char == "'":
            if quoted and index + 1 < len(value) and value[index + 1] == "'":
                index += 1
            else:
                quoted = not quoted
        elif not quoted:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(value) - 1:
                    return False
        index += 1
    return depth == 0 and not quoted


def _derived_index_name(table_name: str, column_name: str) -> str:
    base = f"{table_name}_{column_name}_idx"
    if len(base) <= 63:
        return base
    suffix = hashlib.sha256(base.encode("utf-8")).hexdigest()[:10]
    return f"{base[:52]}_{suffix}"


def percentile(values: list[float], quantile: float) -> float:
    """Return a linearly interpolated percentile without fabricated samples."""
    if not values:
        raise ValueError("cannot calculate a percentile from no measurements")
    if not 0.0 <= quantile <= 1.0:
        raise ValueError("quantile must be between 0 and 1")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
