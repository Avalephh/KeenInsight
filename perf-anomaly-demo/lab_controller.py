#!/usr/bin/env python3
"""Operator-facing experiments for the local SysInsight/DREAM bridge.

This module deliberately keeps the experiment controls beside the bridge API.
The TPCC phase uses the repository's rollback-protected transaction scripts;
the DREAM phase executes read-only TPC-DS statements from the checked-out
catalog, submits the real statement to the existing DREAM worker, and records
the second execution separately.
"""

from __future__ import annotations

import datetime as dt
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from pg_temporary_config import (  # noqa: E402
    TemporaryPostgresConfiguration,
    connection_args_for_configuration,
)


ROOT = Path(__file__).resolve().parent
TPCC_CASE_ROOT = ROOT / "tpcc_cases"
TPCDS_QUERY_ROOT = ROOT.parent / "dream" / "data" / "slow_queries" / "TPC-DS"
LOGGER = logging.getLogger(__name__)


def _positive_env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return float(default)
    return value if value > 0 else float(default)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SQL_STRING = re.compile(r"'(?:''|[^'])*'")
_SQL_NUMBER = re.compile(r"(?<![A-Za-z0-9_])[-+]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?![A-Za-z0-9_])")
_SQL_COMMENT = re.compile(r"--[^\n]*|/\*(?!\+)[\s\S]*?\*/")
_HINT_COMMENT = re.compile(r"/\*\+([\s\S]*?)\*/")
_LEADING_SET = re.compile(
    r"^\s*SET\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([^;]+);\s*",
    re.IGNORECASE,
)

LAB_RESET_PARAMETERS = (
    "work_mem",
    "maintenance_work_mem",
    "temp_buffers",
    "max_parallel_workers",
    "max_parallel_workers_per_gather",
    "parallel_setup_cost",
    "parallel_tuple_cost",
    "random_page_cost",
    "seq_page_cost",
    "cpu_tuple_cost",
    "effective_cache_size",
    "default_statistics_target",
    "join_collapse_limit",
    "from_collapse_limit",
    "geqo",
    "geqo_threshold",
    "enable_bitmapscan",
    "enable_hashagg",
    "enable_hashjoin",
    "enable_indexonlyscan",
    "enable_indexscan",
    "enable_material",
    "enable_mergejoin",
    "enable_nestloop",
    "enable_seqscan",
    "enable_sort",
    "jit",
    "fsync",
    "synchronous_commit",
    "max_wal_size",
    "shared_buffers",
    "max_connections",
)

# These settings either need a postmaster restart or are too disruptive to
# change as part of a one-click browser experiment.  The reset action still
# reports them and removes an explicit ALTER SYSTEM/database override, but it
# never restarts PostgreSQL implicitly.
LAB_RESTART_RESET_PARAMETERS = frozenset({
    "max_wal_size",
    "shared_buffers",
    "max_connections",
})

SESSION_SETTING_NAMES = {
    "work_mem",
    "maintenance_work_mem",
    "temp_buffers",
    "max_parallel_workers",
    "max_parallel_workers_per_gather",
    "parallel_setup_cost",
    "parallel_tuple_cost",
    "random_page_cost",
    "seq_page_cost",
    "cpu_tuple_cost",
    "effective_cache_size",
    "default_statistics_target",
    "join_collapse_limit",
    "from_collapse_limit",
    "geqo",
    "geqo_threshold",
    "enable_bitmapscan",
    "enable_hashagg",
    "enable_hashjoin",
    "enable_indexonlyscan",
    "enable_indexscan",
    "enable_material",
    "enable_mergejoin",
    "enable_nestloop",
    "enable_seqscan",
    "enable_sort",
    "jit",
}

# The six focus scenarios are backed by the real GPT5.6-SOL selections kept in
# the repository's structured validation evidence.  The lab uses these exact
# API-produced candidates as its repeatable demonstration input; it does not
# invent a repair value when a candidate is unavailable.
VERIFIED_SYSINSIGHT_CANDIDATE_FILES = {
    "tp_order_status_burst": ROOT / "results/tpcc_api_validation/20260912_002346/tp_order_status_burst/selected_api_configuration.json",
    "tp_stock_level_burst": ROOT / "results/tpcc_api_validation/20260912_003154/tp_stock_level_burst/selected_api_configuration.json",
    "tp_payment_moderate": ROOT / "results/tpcc_api_validation/20260912_023000_tp_payment_moderate_retry/tp_payment_moderate/selected_api_configuration.json",
    "tp_delivery_burst": ROOT / "results/tpcc_api_validation/20260912_030000_tp_delivery_burst_retry/tp_delivery_burst/selected_api_configuration.json",
    "tp_order_status_hot": ROOT / "results/tpcc_api_validation/20260912_053000_tp_order_status_hot/tp_order_status_hot/selected_api_configuration.json",
    "tp_new_order_burst": ROOT / "results/tpcc_api_validation/20260912_031000_tp_new_order_burst_retry/tp_new_order_burst/selected_api_configuration.json",
    "tp_remote_payment_surge": ROOT / "results/tpcc_api_validation/20260912_055000_tp_remote_payment_surge/tp_remote_payment_surge/selected_api_configuration.json",
    "tp_read_mix_burst": ROOT / "results/tpcc_api_validation/20260912_014110/tp_read_mix_burst/selected_api_configuration.json",
    "tp_payment_hot_surge": ROOT / "results/tpcc_api_validation/20260912_044000_tp_payment_hot_surge/tp_payment_hot_surge/selected_api_configuration.json",
    "tp_full_mix_surge_high": ROOT / "results/tpcc_api_validation/20260912_051000_tp_full_mix_surge_high_replay/tp_full_mix_surge_high/selected_api_configuration.json",
    "tp_wal_checkpoint": ROOT / "results/tpcc_api_validation/20260912_023500_tp_wal_checkpoint_strict/tp_wal_checkpoint/selected_api_configuration.json",
    # Current replacements for the two weaker original focus scenarios.  Both
    # were rechecked by this same lab runner under fixed-rate pressure.
    "tp_order_status_io_burst": ROOT / "results/tpcc_api_validation/20260912_032000_tp_order_status_io_burst/tp_order_status_io_burst/selected_api_configuration.json",
    "tp_payment_surge": ROOT / "results/tpcc_api_validation/20260912_040000_tp_payment_surge/tp_payment_surge/selected_api_configuration.json",
    "tp_stock_level_hot": ROOT / "results/tpcc_api_validation/20260912_052000_tp_stock_level_hot/tp_stock_level_hot/selected_api_configuration.json",
    "tp_payment_hot": ROOT / "results/tpcc_api_validation/20260912_054000_tp_payment_hot/tp_payment_hot/selected_api_configuration.json",
}

# The focus and previously exposed console scenarios use fixed-rate pgbench
# pressure.  Closed-loop pressure changes its observed TPS with database
# latency, so an apparent business-TPS gain can simply be a different amount
# of pressure.  These rates are the rounded pre-tuning pressure levels
# measured on this demo workload.  They are offered through pgbench's
# open-loop -R mode and are scaled when the operator changes the number of
# external clients.
TPCC_PRESSURE_TARGET_TPS = {
    "tp_order_status_burst": 6600,
    "tp_stock_level_burst": 3800,
    "tp_payment_moderate": 2100,
    "tp_delivery_burst": 2000,
    "tp_order_status_io_burst": 5600,
    "tp_payment_surge": 3350,
    "tp_stock_level_hot": 3800,
    "tp_payment_hot": 1950,
    "tp_order_status_hot": 5600,
    "tp_new_order_burst": 3200,
    "tp_remote_payment_surge": 3200,
    "tp_read_mix_burst": 3200,
    "tp_payment_hot_surge": 3000,
    "tp_full_mix_surge_high": 2800,
    "tp_wal_checkpoint": 2500,
}

# Resource guardrails for the local four-core/no-swap demo host.  The values
# can be raised explicitly for a larger machine, but the default keeps the
# monitoring/bridge processes responsive while retaining a visible pressure
# gap for the SysInsight experiment.
TPCC_DEFAULT_PRESSURE_TPS = _positive_env_float("SYSINSIGHT_TPCC_DEFAULT_PRESSURE_TPS", 2500.0)
TPCC_MAX_PRESSURE_TPS = _positive_env_float("SYSINSIGHT_TPCC_MAX_PRESSURE_TPS", 4000.0)
TPCC_NORMAL_TARGET_TPS = _positive_env_float("SYSINSIGHT_TPCC_NORMAL_TARGET_TPS", 1600.0)
TPCC_MAX_NORMAL_TPS = _positive_env_float("SYSINSIGHT_TPCC_MAX_NORMAL_TPS", 2000.0)
TPCC_POSTGRES_CPU_QUOTA = os.environ.get("SYSINSIGHT_PG_CPU_QUOTA", "250%").strip() or "250%"
TPCC_POSTGRES_MEMORY_LIMIT = os.environ.get("SYSINSIGHT_PG_MEMORY_LIMIT", "24G").strip() or "24G"
TPCC_POSTGRES_TASKS_MAX = os.environ.get("SYSINSIGHT_PG_TASKS_MAX", "512").strip() or "512"

# These API-selected values either restart the whole cluster, allocate a very
# large shared memory segment, or weaken durability for the live demo.  A lab
# must remain online and recoverable, so they are audited but not applied by
# the one-click experiment.  Session-level planner/JIT candidates are still
# applied to the tuned control worker.
LAB_UNSAFE_TUNING_PARAMETERS = frozenset({
    "autovacuum_max_workers",
    "fsync",
    "full_page_writes",
    "maintenance_work_mem",
    "max_connections",
    "max_parallel_workers",
    "max_parallel_workers_per_gather",
    "max_worker_processes",
    "max_wal_size",
    "shared_buffers",
    "temp_buffers",
    "work_mem",
})

class LabStopped(RuntimeError):
    """Internal signal used to stop a bounded workload cleanly."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _sql_literal(value: Any) -> str:
    return "'{}'".format(str(value).replace("'", "''"))


def _canonical_sql(sql: str) -> str:
    text = _HINT_COMMENT.sub(" ", str(sql or ""))
    text = _SQL_COMMENT.sub(" ", text)
    text = _SQL_STRING.sub("?", text)
    text = _SQL_NUMBER.sub("?", text)
    return " ".join(text.lower().split()).strip().rstrip(";").strip()


def _hint_pattern(sql: str) -> str:
    text = _HINT_COMMENT.sub("", str(sql or "")).strip()
    text = _SQL_STRING.sub("?", text)
    text = _SQL_NUMBER.sub("?", text)
    if text and not text.endswith(";"):
        text += ";"
    return text


def _is_read_only(sql: str) -> bool:
    """Allow exactly one SELECT/WITH statement for the DREAM lab."""

    text = str(sql or "").strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    if not text or ";" in text:
        return False
    without_strings = _SQL_STRING.sub(" ", text).lower()
    if not without_strings.startswith(("select", "with", "explain")):
        return False
    forbidden = r"\b(insert|update|delete|merge|create|drop|alter|truncate|grant|revoke|copy|call|do|vacuum|refresh|set|reset|begin|commit|rollback|prepare|execute|lock)\b"
    return re.search(forbidden, without_strings) is None


def _bounded_int(value: Any, name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if result < minimum or result > maximum:
        raise ValueError("{} must be between {} and {}".format(name, minimum, maximum))
    return result


def _case_catalog() -> List[Dict[str, Any]]:
    sys.path.insert(0, str(ROOT))
    try:
        import tpcc_transaction_cases  # type: ignore
    except Exception:
        return []
    focus_ids = tuple(str(value) for value in getattr(tpcc_transaction_cases, "FOCUS_SCENARIO_IDS", ()))
    focus_order = {value: index for index, value in enumerate(focus_ids, 1)}
    result: List[Dict[str, Any]] = []
    for value in getattr(tpcc_transaction_cases, "CASE_DEFINITIONS", []):
        if not isinstance(value, dict) or value.get("mode") != "pgbench":
            continue
        scenario_id = str(value.get("id"))
        sql_name = str(value.get("sql", ""))
        normal_name = str(value.get("normal_sql", "tp_normal.sql"))
        if not (TPCC_CASE_ROOT / sql_name).is_file() or not (TPCC_CASE_ROOT / normal_name).is_file():
            continue
        result.append(
            {
                "scenario_id": scenario_id,
                "title": str(value.get("title", scenario_id or "TPCC")),
                "event": str(value.get("event", "")),
                "clients": int(value.get("clients", 1)),
                "sql": sql_name,
                "normal_sql": normal_name,
                "transactions": list(value.get("tpcc_transactions", [])),
                "pressure_evidence": list(value.get("pressure_evidence", [])),
                "pressure_target_tps": TPCC_PRESSURE_TARGET_TPS.get(
                    scenario_id, TPCC_DEFAULT_PRESSURE_TPS
                ),
                "pressure_mode": "open_loop_fixed_rate",
                "focus": scenario_id in focus_order,
                "focus_order": focus_order.get(scenario_id),
                "source": "tpcc_transaction_cases.CASE_DEFINITIONS",
                "rollback_protected": True,
            }
        )
    return result


def _query_catalog() -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for path in sorted(
        TPCDS_QUERY_ROOT.glob("*.sql"),
        key=lambda item: int(item.stem) if item.stem.isdigit() else item.stem,
    ):
        if not path.stem.isdigit():
            continue
        query_id = int(path.stem)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        complexity = 1
        lower = text.lower()
        complexity += lower.count(" join ")
        complexity += lower.count(" union ")
        complexity += lower.count(" intersect ")
        complexity += lower.count(" except ")
        complexity += lower.count(" over (")
        complexity += lower.count(" with ")
        result.append(
            {
                "sql_id": "tpcds_q{}".format(query_id),
                "query_number": query_id,
                "title": "TPC-DS Query {}".format(query_id),
                "source": str(path.relative_to(ROOT.parent)),
                "line_count": len(text.splitlines()),
                "character_count": len(text),
                "complexity_score": complexity,
                # These are the SQL rewrites that passed three fresh
                # baseline -> DREAM -> replay runs on the local SF10 data.
                # Q4 used to be the default entry, but its original SQL
                # exceeded the lab timeout during the stability audit, so it
                # must remain selectable without being presented as a demo.
                "recommended": query_id in {31, 44, 88},
                "schema": "tpcds",
                "read_only": _is_read_only(text),
            }
        )
    return result


def _pgbench_metrics(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8", errors="replace")
    result: Dict[str, Any] = {}
    progress_tps = re.findall(r"progress:\s+[^\n]*?([0-9]+(?:\.[0-9]+)?)\s+tps", text, re.IGNORECASE)
    progress_latency = re.findall(r"progress:\s+[^\n]*?lat(?:ency)?\s+([0-9]+(?:\.[0-9]+)?)\s+ms", text, re.IGNORECASE)
    final_tps = re.findall(r"tps\s*=\s*([0-9]+(?:\.[0-9]+)?)", text, re.IGNORECASE)
    final_latency = re.findall(r"latency average\s*=\s*([0-9]+(?:\.[0-9]+)?)\s+ms", text, re.IGNORECASE)
    if progress_tps:
        result["tps_live"] = float(progress_tps[-1])
    if progress_latency:
        result["latency_live_ms"] = float(progress_latency[-1])
    if final_tps:
        result["tps"] = float(final_tps[-1])
    elif progress_tps:
        result["tps"] = float(progress_tps[-1])
    if final_latency:
        result["latency_ms"] = float(final_latency[-1])
    elif progress_latency:
        result["latency_ms"] = float(progress_latency[-1])
    return result


def _tpcc_tps_summary(
    samples: Sequence[Mapping[str, Any]],
    phase: str,
    metric: str,
    duration_seconds: int,
) -> Dict[str, Any]:
    """Summarize a TPCC TPS series after its short startup/warm-up window."""

    rows: List[Tuple[float, float]] = []
    for sample in samples:
        if str(sample.get("phase") or "") != phase:
            continue
        metrics = sample.get("metrics") or {}
        if not isinstance(metrics, Mapping):
            continue
        if metric == "business":
            values = metrics.get("business") or metrics.get("control") or {}
        else:
            values = metrics.get("pressure") or metrics.get("external") or {}
        if not isinstance(values, Mapping):
            continue
        value = values.get("tps_live", values.get("tps"))
        try:
            tps = float(value)
            elapsed = float(sample.get("elapsed_seconds") or 0.0)
        except (TypeError, ValueError):
            continue
        if tps >= 0:
            rows.append((elapsed, tps))

    warmup_seconds = min(15.0, max(3.0, float(duration_seconds) * 0.10))
    steady_rows = [item for item in rows if item[0] >= warmup_seconds]
    # Very short developer smoke tests should still produce a useful result.
    if len(steady_rows) < 3:
        steady_rows = rows
    values = [item[1] for item in steady_rows]
    result: Dict[str, Any] = {
        "metric": "normal_business_tps" if metric == "business" else "pressure_injection_tps",
        "phase": phase,
        "warmup_seconds": round(warmup_seconds, 1),
        "sample_count": len(rows),
        "steady_sample_count": len(values),
    }
    if not values:
        result["status"] = "insufficient_data"
        return result
    mean = statistics.mean(values)
    stdev = statistics.pstdev(values) if len(values) > 1 else 0.0
    result.update(
        {
            "status": "ok",
            # Median is the displayed stable TPS: it is less sensitive to a
            # single overloaded or startup sample than the final value.
            "steady_tps": round(statistics.median(values), 2),
            "mean_tps": round(mean, 2),
            "min_tps": round(min(values), 2),
            "max_tps": round(max(values), 2),
            "stdev_tps": round(stdev, 2),
            "cv_percent": round((stdev / mean) * 100.0, 2) if mean else None,
        }
    )
    return result


def _tpcc_comparison(phases: Mapping[str, Any]) -> Dict[str, Any]:
    """Build the operator-facing before/after comparison from steady TPS.

    The raw before/after values remain visible, but the reported gain is
    adjusted to the same observed external pressure when the pressure
    generator is closed-loop.  This prevents a faster external generator from
    being mistaken for a tuning improvement.  New console runs use a fixed
    open-loop pressure target as well, so this adjustment should normally be
    close to one-to-one.
    """

    def value(phase: str, metric: str = "business_tps_summary") -> Optional[float]:
        item = phases.get(phase) if isinstance(phases, Mapping) else None
        summary = item.get(metric, {}) if isinstance(item, Mapping) else {}
        raw = summary.get("steady_tps") if isinstance(summary, Mapping) else None
        try:
            return float(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    baseline = value("baseline")
    before = value("pressure_before")
    after = value("pressure_after")
    pressure_before = value("pressure_before", "pressure_tps_summary")
    pressure_after = value("pressure_after", "pressure_tps_summary")
    external_delta = pressure_after - pressure_before if pressure_after is not None and pressure_before is not None else None
    external_delta_ratio = external_delta / pressure_before if external_delta is not None and pressure_before else None
    same_pressure = abs(external_delta_ratio) <= 0.20 if external_delta_ratio is not None else None
    pressure_adjusted_after = None
    raw_gain = after - before if after is not None and before is not None else None
    raw_gain_ratio = raw_gain / before if raw_gain is not None and before else None
    after_phase = phases.get("pressure_after") if isinstance(phases, Mapping) else None
    before_phase = phases.get("pressure_before") if isinstance(phases, Mapping) else None
    configured_before = None
    configured_after = None
    for phase, target_name in (
        (before_phase, "before"),
        (after_phase, "after"),
    ):
        if isinstance(phase, Mapping) and phase.get("pressure_target_tps") is not None:
            try:
                if target_name == "before":
                    configured_before = float(phase["pressure_target_tps"])
                else:
                    configured_after = float(phase["pressure_target_tps"])
            except (TypeError, ValueError):
                pass
    open_loop_pressure = (
        configured_before is not None
        and configured_after is not None
        and abs(configured_before - configured_after) <= 0.01
    )
    configured_pressure = configured_after if configured_after is not None else configured_before
    if open_loop_pressure:
        # The offered load is already identical by construction.  Do not
        # scale the business result by completed external TPS: under an
        # overloaded server, a tuned run can complete more or fewer pressure
        # transactions while still receiving the same offered request rate.
        pressure_adjusted_after = after
        pressure_adjustment_method = "open_loop_fixed_rate_no_scaling"
    else:
        pressure_adjustment_method = "closed_loop_observed_rate_scaling"
        if after is not None and pressure_before and pressure_after:
            pressure_adjusted_after = after * pressure_before / pressure_after
    pressure_adjusted_gain = (
        pressure_adjusted_after - before
        if pressure_adjusted_after is not None and before is not None
        else None
    )
    pressure_adjusted_ratio = (
        pressure_adjusted_gain / before
        if pressure_adjusted_gain is not None and before
        else None
    )
    result: Dict[str, Any] = {
        "metric": "normal_business_tps",
        "metric_label": "目标业务 TPS",
        "baseline_tps": baseline,
        "pressure_before_tps": before,
        "pressure_after_tps": after,
        # Keep the old raw names for compatibility with existing artifacts;
        # operator-facing gain fields below use the pressure-adjusted value.
        "raw_tuning_gain_tps": raw_gain,
        "raw_tuning_gain_ratio": raw_gain_ratio,
        "pressure_adjusted_after_tps": pressure_adjusted_after,
        "pressure_adjusted_gain_tps": pressure_adjusted_gain,
        "pressure_adjusted_gain_ratio": pressure_adjusted_ratio,
        "tuning_gain_tps": pressure_adjusted_gain if pressure_adjusted_gain is not None else raw_gain,
        "tuning_gain_ratio": pressure_adjusted_ratio if pressure_adjusted_ratio is not None else raw_gain_ratio,
        "recovery_ratio": after / baseline if after is not None and baseline else None,
        "improved_under_pressure": bool(
            pressure_adjusted_after is not None
            and before is not None
            and pressure_adjusted_after > before
            and same_pressure is not False
        ),
        "comparison_valid": bool(after is not None and before is not None and same_pressure is not False),
        "external_pressure_before_tps": pressure_before,
        "external_pressure_after_tps": pressure_after,
        "external_pressure_delta_tps": external_delta,
        "external_pressure_delta_ratio": external_delta_ratio,
        "same_pressure_observed": same_pressure,
        "pressure_target_tps": configured_pressure,
        "pressure_mode": "open_loop_fixed_rate" if open_loop_pressure else "closed_loop_observed_rate",
        "pressure_adjustment_method": pressure_adjustment_method,
    }
    if before is not None and baseline is not None:
        result["pressure_drop_ratio"] = (baseline - before) / baseline if baseline else None
    return result


class LabController:
    """Own bounded TPCC processes and single-query DREAM lab runs."""

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge
        self.store = bridge.store
        self.db = bridge.db
        self.root = Path(bridge.output_root).resolve() / "lab"
        self.root.mkdir(parents=True, exist_ok=True)
        self.tpcc_cases = _case_catalog()
        self.sql_catalog = _query_catalog()
        self._lock = threading.RLock()
        self._active: Dict[str, Dict[str, Any]] = {}

        # A bridge restart cannot reattach a Python ThreadPoolExecutor to an
        # old run. Recover those rows and terminate only the lab connections
        # belonging to the old run, so the next click starts from a clean
        # state instead of seeing a permanent queued/running lock.
        self._recover_stale_runs("sysinsight")
        self._recover_stale_runs("dream")

    def shutdown(self) -> None:
        with self._lock:
            active_ids = list(self._active)
            active = list(self._active.values())
            for item in active:
                event = item.get("stop_event")
                if event is not None:
                    event.set()
                self._terminate_processes(item.get("processes", []))
        for run_id in active_ids:
            self._cleanup_active_processes(run_id)
            row = self.store.get_lab_run(run_id)
            if row and row.get("status") in {"queued", "running"}:
                self._safe_update_run(
                    run_id,
                    status="stopped",
                    phase="bridge_shutdown",
                    error="bridge shutdown requested before the experiment completed",
                )

    def _run_id(self, kind: str) -> str:
        digest = hashlib.sha1(str(time.time_ns()).encode()).hexdigest()[:10]
        return "lab-{}-{}-{}".format(kind, dt.datetime.now().strftime("%Y%m%d%H%M%S"), digest)

    @staticmethod
    def _run_marker(run_id: str, length: int = 8) -> str:
        return str(run_id).rsplit("-", 1)[-1][:length]

    def _application_prefix(self, run_id: str, kind: str) -> str:
        marker = self._run_marker(run_id)
        if kind == "dream":
            return "lab-dream-{}-".format(marker)
        return "lab-{}-".format(marker)

    def _terminate_orphaned_lab(self, run_id: str, kind: str) -> Dict[str, Any]:
        """Terminate exact lab backends/process groups left by a lost worker."""

        prefix = self._application_prefix(run_id, kind)
        cleanup: Dict[str, Any] = {
            "application_prefix": prefix,
            "backend_pids": [],
            "process_groups": [],
            "force_killed_process_groups": [],
            "errors": [],
        }
        # First close matching PostgreSQL sessions. This is intentionally an
        # exact generated application_name prefix, never a broad pg_terminate
        # operation against unrelated user sessions.
        try:
            rows = self.db._rows(
                """
                SELECT pid::int AS pid, application_name
                FROM pg_stat_activity
                WHERE datname=current_database()
                  AND backend_type='client backend'
                  AND application_name LIKE {}
                  AND pid <> pg_backend_pid()
                """.format(_sql_literal(prefix + "%")),
                "sysinsight-lab-recovery",
            )
            cleanup["backend_pids"] = [int(row["pid"]) for row in rows if row.get("pid") is not None]
            if cleanup["backend_pids"]:
                self.db._rows(
                    """
                    SELECT pid::int AS pid, pg_terminate_backend(pid) AS terminated
                    FROM pg_stat_activity
                    WHERE datname=current_database()
                      AND backend_type='client backend'
                      AND application_name LIKE {}
                      AND pid <> pg_backend_pid()
                    """.format(_sql_literal(prefix + "%")),
                    "sysinsight-lab-recovery-terminate",
                )
        except Exception as exc:
            cleanup["errors"].append("database sessions: {}".format(exc))

        # pgbench is launched in its own session. Inspect only processes that
        # inherited this run's PGAPPNAME and terminate their process groups;
        # this also handles a bridge killed before it could persist a result.
        process_groups = set()
        try:
            expected = ("PGAPPNAME=" + prefix).encode("utf-8")
            for process_dir in Path("/proc").iterdir():
                if not process_dir.name.isdigit():
                    continue
                try:
                    environ = (process_dir / "environ").read_bytes().split(b"\0")
                    if not any(value.startswith(expected) for value in environ):
                        continue
                    pid = int(process_dir.name)
                    pgid = os.getpgid(pid)
                    if pgid != os.getpgrp():
                        process_groups.add(pgid)
                except (OSError, ValueError, ProcessLookupError):
                    continue
            for pgid in sorted(process_groups):
                try:
                    os.killpg(pgid, signal.SIGTERM)
                    cleanup["process_groups"].append(pgid)
                except (OSError, ProcessLookupError) as exc:
                    cleanup["errors"].append("process group {}: {}".format(pgid, exc))
            # SIGTERM is normally enough for pgbench/runuser, but a bridge
            # crash must not leave a stubborn worker blocking the next lab.
            # Escalate only for the exact process groups discovered above.
            for pgid in sorted(process_groups):
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    try:
                        os.killpg(pgid, 0)
                    except ProcessLookupError:
                        break
                    except OSError:
                        break
                    time.sleep(0.05)
                else:
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                        cleanup["force_killed_process_groups"].append(pgid)
                    except (OSError, ProcessLookupError) as exc:
                        cleanup["errors"].append("force kill process group {}: {}".format(pgid, exc))
        except OSError as exc:
            cleanup["errors"].append("process scan: {}".format(exc))
        return cleanup

    def _safe_update_run(
        self,
        run_id: str,
        status: str,
        phase: str,
        error: str,
        result_patch: Optional[Mapping[str, Any]] = None,
    ) -> None:
        try:
            self.store.update_lab_run(
                run_id,
                status=status,
                phase=phase,
                result_patch=result_patch,
                error=error,
            )
        except Exception:
            # The next start will retry stale-row recovery. Do not let a
            # secondary SQLite/disk error hide the original worker failure.
            LOGGER.exception("could not persist lab run %s state", run_id)

    def _recover_stale_runs_locked(self, kind: str) -> List[Dict[str, Any]]:
        active_rows: List[Dict[str, Any]] = []
        for row in self.store.list_lab_runs(100, kind=kind):
            if row.get("status") not in {"queued", "running"}:
                continue
            run_id = str(row.get("run_id") or "")
            item = self._active.get(run_id)
            future = item.get("future") if item else None
            # The short interval before a future is attached is protected by
            # this lock in all start paths, so a missing entry here is stale.
            if item is not None and (future is None or not future.done()):
                active_rows.append(row)
                continue
            self._active.pop(run_id, None)
            cleanup = self._terminate_orphaned_lab(run_id, kind)
            error = "recovered stale {} lab run before a new action".format(kind)
            self._safe_update_run(
                run_id,
                status="failed",
                phase="startup_recovery",
                error=error,
                result_patch={
                    "recovery": {
                        "status": "completed",
                        "kind": kind,
                        "recovered_at": utc_now(),
                        "cleanup": cleanup,
                    }
                },
            )
        return active_rows

    def _active_kind(self, kind: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            active = self._recover_stale_runs_locked(kind)
            return active[0] if active else None

    def _recover_stale_runs(self, kind: str) -> List[Dict[str, Any]]:
        with self._lock:
            return self._recover_stale_runs_locked(kind)

    def _mark_unhandled_future(self, run_id: str, future: Any) -> None:
        error = ""
        try:
            if future.cancelled():
                error = "lab worker future was cancelled"
            else:
                try:
                    future.result()
                except BaseException as exc:
                    error = "{}: {}".format(type(exc).__name__, exc)
        except BaseException as exc:
            # A broken Future implementation or executor callback must not
            # leave the in-memory active marker behind forever.
            error = "future callback failed: {}: {}".format(type(exc).__name__, exc)
        if error:
            try:
                self._cleanup_active_processes(run_id)
                row = self.store.get_lab_run(run_id)
                if row and row.get("status") in {"queued", "running"}:
                    self._safe_update_run(run_id, "failed", "worker_exception", error)
            except BaseException:
                # State persistence is retried by the next startup/action
                # recovery pass; always release the local active marker now.
                LOGGER.exception("could not record failed lab worker %s", run_id)
        self._clear_active(run_id)

    def _submit_active(
        self,
        run_id: str,
        stop_event: threading.Event,
        target: Any,
        *args: Any,
    ) -> Any:
        with self._lock:
            self._active[run_id] = {"stop_event": stop_event, "processes": []}
            try:
                future = self.bridge.lab_executor.submit(target, *args)
            except BaseException as exc:
                self._active.pop(run_id, None)
                self._safe_update_run(
                    run_id,
                    "failed",
                    "submit_failed",
                    "{}: {}".format(type(exc).__name__, exc),
                )
                raise
            self._active[run_id]["future"] = future
            future.add_done_callback(lambda completed: self._mark_unhandled_future(run_id, completed))
            return future

    def _cleanup_active_processes(self, run_id: str) -> None:
        with self._lock:
            item = self._active.get(run_id)
            if not item:
                return
            processes = list(item.get("processes", []))
            item["processes"] = []
        if not processes:
            return
        try:
            self._wait_processes(processes, terminate=True)
        except BaseException:
            LOGGER.exception("could not clean TPCC processes for lab run %s", run_id)

    def catalog(self) -> Dict[str, Any]:
        focus_scenarios = [value for value in self.tpcc_cases if value.get("focus")]
        focus_scenarios.sort(key=lambda value: int(value.get("focus_order") or 999))
        return {
            "status": "ok",
            "tpcc_scenarios": self.tpcc_cases,
            "tpcc_focus_scenarios": focus_scenarios,
            "tpcc_metric": {
                "primary": "normal_business_tps",
                "primary_label": "目标业务 TPS",
                "primary_source": "control worker running tp_normal.sql",
                "secondary": "pressure_injection_tps",
                "secondary_label": "压力注入 TPS",
                "secondary_source": "external worker running the selected scenario SQL",
                "scenario_transaction": "sysinsight_dream_lab_tpcc_scenario_transaction_tps",
                "scenario_transaction_label": "场景事务 TPS",
                "scenario_transaction_source": "selected scenario target business control worker; zero when the scenario is not running",
                "resource_guard": {
                    "postgres_cpu_quota": TPCC_POSTGRES_CPU_QUOTA,
                    "postgres_memory_limit": TPCC_POSTGRES_MEMORY_LIMIT,
                    "postgres_tasks_max": TPCC_POSTGRES_TASKS_MAX,
                    "normal_target_tps_default": TPCC_NORMAL_TARGET_TPS,
                    "normal_target_tps_max": TPCC_MAX_NORMAL_TPS,
                    "pressure_target_tps_max": TPCC_MAX_PRESSURE_TPS,
                },
            },
            "sql_catalog": self.sql_catalog,
            "database": {
                "name": self.bridge.args.db,
                "schema": self.bridge.args.db_schema,
                "host": self.bridge.args.host,
                "port": self.bridge.args.port,
                "reset_parameters": list(LAB_RESET_PARAMETERS),
            },
            "retention_hours": round(float(self.bridge.args.sample_retention_days) * 24.0, 3),
        }

    def get_sql(self, sql_id: str) -> Dict[str, Any]:
        item = next((value for value in self.sql_catalog if value["sql_id"] == sql_id), None)
        if item is None:
            raise KeyError("unknown SQL catalog id: {}".format(sql_id))
        path = TPCDS_QUERY_ROOT / "{}.sql".format(item["query_number"])
        query = path.read_text(encoding="utf-8")
        if not _is_read_only(query):
            raise ValueError("catalog SQL is not a single read-only statement")
        return {**item, "query": query}

    def status(self) -> Dict[str, Any]:
        # The history table only needs metadata. Returning every completed
        # TPCC/DREAM result on each two-second browser poll made this endpoint
        # multi-megabyte and caused overlapping refresh requests. Fetch the
        # full JSON only for the current and latest run that the console
        # actually renders. One latest sample is sufficient for the live
        # cards; phase history is already summarized in result.phases.
        runs = self.store.list_lab_runs(30, include_result=False)
        current = [row for row in runs if row.get("status") in {"queued", "running"}]
        current_details: List[Dict[str, Any]] = []
        for row in current:
            detail = self.store.get_lab_run(str(row["run_id"])) or dict(row)
            detail["samples"] = self.store.list_lab_samples(str(row["run_id"]), 1)
            detail["executions"] = self.store.list_lab_executions(str(row["run_id"]), 20)
            if row.get("kind") == "dream":
                result = detail.get("result") if isinstance(detail.get("result"), dict) else {}
                job_id = result.get("job_id")
                if job_id:
                    detail["job"] = self.store.get_job(str(job_id))
                    if result.get("sql_key"):
                        detail["improvement"] = self.store.latest_improvement(str(result["sql_key"]))
            current_details.append(detail)

        latest_dream_summary = next((row for row in runs if row.get("kind") == "dream"), None)
        dream_detail: Optional[Dict[str, Any]] = None
        if latest_dream_summary:
            latest_dream = self.store.get_lab_run(str(latest_dream_summary["run_id"])) or latest_dream_summary
            dream_detail = dict(latest_dream)
            dream_detail["executions"] = self.store.list_lab_executions(str(latest_dream["run_id"]), 20)
            result = latest_dream.get("result") if isinstance(latest_dream.get("result"), dict) else {}
            job_id = result.get("job_id")
            if job_id:
                dream_detail["job"] = self.store.get_job(str(job_id))
                if result.get("sql_key"):
                    dream_detail["improvement"] = self.store.latest_improvement(str(result["sql_key"]))

        latest_sysinsight_summary = next((row for row in runs if row.get("kind") == "sysinsight"), None)
        sysinsight_detail: Optional[Dict[str, Any]] = None
        if latest_sysinsight_summary:
            latest_sysinsight = self.store.get_lab_run(str(latest_sysinsight_summary["run_id"])) or latest_sysinsight_summary
            sysinsight_detail = dict(latest_sysinsight)
            sysinsight_detail["samples"] = self.store.list_lab_samples(str(latest_sysinsight["run_id"]), 1)
        return {
            "status": "ok",
            "current": current_details,
            "latest_sysinsight": sysinsight_detail,
            "latest_dream": dream_detail,
            "runs": runs,
        }

    def metrics_snapshot(self) -> Dict[str, Any]:
        value = self.status()
        result: Dict[str, Any] = {"current": value.get("current", [])}
        for kind, key in (("sysinsight", "sysinsight"), ("dream", "dream")):
            current = next((row for row in value.get("current", []) if row.get("kind") == kind), None)
            latest = value.get("latest_{}".format(kind))
            row = current or latest or {}
            result[key] = {
                "run_id": row.get("run_id", ""),
                "status": row.get("status", "idle"),
                "phase": row.get("phase", "idle"),
                "result": row.get("result", {}),
            }
            if current and current.get("samples"):
                result[key]["sample"] = current["samples"][-1]
            if latest and latest.get("executions"):
                result[key]["executions"] = latest["executions"]
        return result

    def reset_database(self) -> Dict[str, Any]:
        """Reset only the known demo GUCs and retain pg_hint_plan settings."""

        before = self.db.settings_snapshot(LAB_RESET_PARAMETERS)
        database = '"{}"'.format(str(self.bridge.args.db).replace('"', '""'))
        commands: List[str] = []
        errors: List[Dict[str, str]] = []

        # Remove database-local overrides only when one is actually present.
        # session_preload_libraries and pg_hint_plan.enable_hint_table are
        # intentionally outside this list, so this button cannot disconnect
        # the automatic hint-table runtime.
        for name in LAB_RESET_PARAMETERS:
            command = "ALTER DATABASE {} RESET {}".format(database, '"{}"'.format(name))
            try:
                if self.db.database_setting(name):
                    self.db.execute(command, "sysinsight-lab-reset-database")
                    commands.append(command)
            except Exception as exc:
                errors.append({"command": command, "error": str(exc)})

        # Restore temporary ALTER SYSTEM values. PostgreSQL will report
        # postmaster settings as pending_restart; the lab does not restart the
        # server implicitly because that would interrupt the monitoring path.
        for name, setting in before.items():
            sourcefile = str(setting.get("sourcefile") or "")
            # Only remove values that this console could have written.  This
            # avoids touching the packaged PostgreSQL configuration and keeps
            # the reset button safe when it is clicked repeatedly.
            if not sourcefile.endswith("postgresql.auto.conf"):
                continue
            command = "ALTER SYSTEM RESET {}".format('"{}"'.format(name))
            try:
                self.db.execute(command, "sysinsight-lab-reset-system")
                commands.append(command)
            except Exception as exc:
                errors.append({"command": command, "error": str(exc)})
        try:
            reload_output = self.db.execute(
                "SELECT pg_reload_conf()",
                "sysinsight-lab-reset-reload",
            )
        except Exception as exc:
            reload_output = ""
            errors.append({"command": "SELECT pg_reload_conf()", "error": str(exc)})
        after = self.db.settings_snapshot(LAB_RESET_PARAMETERS)
        restart_required = any(bool(value.get("pending_restart")) for value in after.values())
        result = {
            "status": "completed" if not errors else "completed_with_errors",
            "before": before,
            "after": after,
            "commands": commands,
            "reload_output": reload_output,
            "errors": errors,
            "restart_required": restart_required,
            "note": "仅清理实验白名单中的数据库级/ALTER SYSTEM 覆盖；不自动重启 PostgreSQL，pg_hint_plan 的 session_preload_libraries 保持不变。",
            "completed_at": utc_now(),
        }
        path = self.root / "last_database_reset.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self.store.set_meta("last_lab_database_reset", result)
        return result

    def start_tpcc(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        scenario_id = str(body.get("scenario_id") or "tp_payment_hot")
        case = next((value for value in self.tpcc_cases if value["scenario_id"] == scenario_id), None)
        if case is None:
            raise KeyError("unknown TPCC scenario: {}".format(scenario_id))
        normal_clients = _bounded_int(body.get("normal_clients"), "normal_clients", 2, 1, 16)
        pressure_clients = _bounded_int(body.get("pressure_clients"), "pressure_clients", case["clients"], 1, 32)
        baseline_seconds = _bounded_int(body.get("baseline_seconds"), "baseline_seconds", 60, 5, 300)
        # The single UI value is the total pressure observation window.  Split
        # it into two equal windows so the same external pressure can be
        # compared before and after the SysInsight candidate is applied.
        pressure_seconds = _bounded_int(body.get("pressure_seconds"), "pressure_seconds", 180, 10, 300)
        pressure_before_seconds = max(5, pressure_seconds // 2)
        pressure_after_seconds = pressure_seconds - pressure_before_seconds
        base_pressure_target = TPCC_PRESSURE_TARGET_TPS.get(
            scenario_id, TPCC_DEFAULT_PRESSURE_TPS
        )
        scaled_pressure_target = float(base_pressure_target) * pressure_clients / float(case["clients"])
        pressure_target_tps = round(min(TPCC_MAX_PRESSURE_TPS, scaled_pressure_target), 1)
        normal_target_tps = round(
            min(TPCC_MAX_NORMAL_TPS, TPCC_NORMAL_TARGET_TPS * normal_clients / 2.0),
            1,
        )
        config = {
            "normal_clients": normal_clients,
            "pressure_clients": pressure_clients,
            "baseline_seconds": baseline_seconds,
            "pressure_seconds": pressure_seconds,
            "pressure_before_seconds": pressure_before_seconds,
            "pressure_after_seconds": pressure_after_seconds,
            "normal_target_tps": normal_target_tps,
            "pressure_target_tps": pressure_target_tps,
            "pressure_mode": "open_loop_fixed_rate",
            "resource_guard": {
                "postgres_cpu_quota": TPCC_POSTGRES_CPU_QUOTA,
                "postgres_memory_limit": TPCC_POSTGRES_MEMORY_LIMIT,
                "postgres_tasks_max": TPCC_POSTGRES_TASKS_MAX,
                "normal_target_tps": normal_target_tps,
                "pressure_target_tps": pressure_target_tps,
            },
            "pressure_hold_until_completion": True,
            "tuning_observation": "same external pressure is replayed before and after SysInsight tuning",
            "primary_metric": "normal_business_tps",
            "primary_metric_source": "control worker running tp_normal.sql",
            "secondary_metric": "pressure_injection_tps",
            "actual_prometheus_alert": True,
        }
        with self._lock:
            existing = self._active_kind("sysinsight")
            if existing:
                # A duplicate click or a polling race should not surface as a
                # 409 to the operator. Keep the one real workload and let the
                # console continue following it.
                return {
                    "status": "already_running",
                    "message": "a sysinsight lab run is already queued or running",
                    "run": existing,
                }
            free_bytes = shutil.disk_usage("/").free
            if free_bytes < 512 * 1024 * 1024:
                raise RuntimeError("insufficient disk space for TPCC lab: {} bytes free".format(free_bytes))
            run_id = self._run_id("sysinsight")
            self.store.create_lab_run(run_id, "sysinsight", scenario_id, case["title"], config)
            stop_event = threading.Event()
            self._submit_active(run_id, stop_event, self._run_tpcc, run_id, case, config, stop_event)
            return {"status": "queued", "run": self.store.get_lab_run(run_id)}

    def stop(self, run_id: str) -> Dict[str, Any]:
        row = self.store.get_lab_run(run_id)
        if row is None:
            raise KeyError("lab run not found: {}".format(run_id))
        if row.get("status") not in {"queued", "running"}:
            return {"status": "unchanged", "run": row}
        recovered = False
        with self._lock:
            item = self._active.get(run_id)
            if item:
                item["stop_event"].set()
                self._terminate_processes(item.get("processes", []))
            else:
                # A worker lost during a bridge/API failure is recoverable
                # even when the operator presses Stop first.
                self._recover_stale_runs_locked(str(row.get("kind") or "sysinsight"))
                recovered = True
        return {
            "status": "recovered" if recovered else "stop_requested",
            "run": self.store.get_lab_run(run_id),
        }

    def _clear_active(self, run_id: str) -> None:
        with self._lock:
            self._active.pop(run_id, None)

    def _set_processes(self, run_id: str, processes: List[Dict[str, Any]]) -> None:
        with self._lock:
            if run_id in self._active:
                self._active[run_id]["processes"] = processes

    @staticmethod
    def _terminate_processes(processes: Sequence[Mapping[str, Any]]) -> None:
        for item in processes:
            process = item.get("process")
            if process is None or process.poll() is not None:
                continue
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    @staticmethod
    def _wait_processes(processes: List[Dict[str, Any]], terminate: bool = False) -> None:
        if terminate:
            LabController._terminate_processes(processes)
        for item in processes:
            process = item.get("process")
            if process is not None:
                try:
                    process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
                item["returncode"] = process.returncode
                item["finished_at"] = utc_now()
            handle = item.pop("log_handle", None)
            if handle is not None:
                handle.close()

    @staticmethod
    def _setting_assignment(name: str, value: Any) -> str:
        # TemporaryPostgresConfiguration validates the candidate against the
        # live pg_settings context.  At this point this helper only renders
        # the already-approved session subset into the staged pgbench script.
        if not _IDENTIFIER.fullmatch(name):
            raise ValueError("unsupported lab tuning setting: {}".format(name))
        return 'SET "{}" = {}'.format(name.replace('"', '""'), _sql_literal(value))

    @staticmethod
    def _connection_options(settings: Optional[Mapping[str, Any]]) -> Optional[str]:
        if not settings:
            return None
        options: List[str] = []
        for name, value in settings.items():
            if not _IDENTIFIER.fullmatch(str(name)):
                raise ValueError("unsafe connection setting name: {}".format(name))
            text = str(value)
            if not re.fullmatch(r"[A-Za-z0-9_./:+%\-* ]+", text):
                raise ValueError("unsafe connection setting value for {}".format(name))
            options.append("-c {}={}".format(name, text))
        return " ".join(options)

    def _load_lab_tuning(self, scenario_id: str) -> Dict[str, Any]:
        """Load the exact API candidate for TemporaryPostgresConfiguration.

        The applier, rather than this controller, decides whether each field
        is session, connection, reload, or restart scoped.  It snapshots and
        restores every global field, which keeps this lab consistent with the
        standalone TPCC API validation workflow.
        """

        source = VERIFIED_SYSINSIGHT_CANDIDATE_FILES.get(str(scenario_id))
        result: Dict[str, Any] = {
            "status": "candidate_loaded",
            "scenario_id": str(scenario_id),
            "source": str(source) if source else None,
            "source_type": "verified_sysinsight_gpt5.6-sol_selection",
            "scope": "safe candidate through TemporaryPostgresConfiguration with snapshot/apply/restore",
            "requested_configuration": {},
            "applied_configuration": {},
            "blocked_by_safety": {},
            "application": {},
            "safety_policy": {
                "blocked_parameters": sorted(LAB_UNSAFE_TUNING_PARAMETERS),
                "restart_parameters": "skipped by the lab applier",
                "reason": "keep PostgreSQL online and preserve durability while the host is under pressure",
            },
        }
        if source is None or not source.is_file():
            result["status"] = "unavailable"
            result["reason"] = "no checked-in verified SysInsight candidate for this scenario"
            return result
        try:
            candidate = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            result["status"] = "unavailable"
            result["reason"] = "could not read verified SysInsight candidate: {}".format(exc)
            return result
        if not isinstance(candidate, dict) or not candidate:
            result["status"] = "unavailable"
            result["reason"] = "verified SysInsight candidate is empty"
            return result
        blocked = {
            name: value
            for name, value in candidate.items()
            if name in LAB_UNSAFE_TUNING_PARAMETERS
        }
        applied = {
            name: value
            for name, value in candidate.items()
            if name not in LAB_UNSAFE_TUNING_PARAMETERS
        }
        result["requested_configuration"] = candidate
        result["applied_configuration"] = applied
        result["blocked_by_safety"] = blocked
        if not applied:
            result["status"] = "no_safe_candidate"
            result["reason"] = "all API-selected settings were blocked by the lab safety policy"
            return result
        result["validated_at"] = utc_now()
        result["reason"] = "safe subset loaded; blocked global settings remain in the audit record"
        return result

    def _stage_sql(
        self,
        source: Path,
        stage_dir: Path,
        session_settings: Optional[Mapping[str, Any]] = None,
        destination_name: Optional[str] = None,
    ) -> Path:
        destination = stage_dir / (destination_name or source.name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = source.read_text(encoding="utf-8")
        prefix = ""
        for name, value in (session_settings or {}).items():
            prefix += self._setting_assignment(str(name), value) + ";\n"
        destination.write_text(prefix + content, encoding="utf-8")
        destination.chmod(0o644)
        return destination

    def _start_pgbench(
        self,
        sql_path: Path,
        run_dir: Path,
        app_name: str,
        clients: int,
        duration: int,
        role: str,
        connection_args: Optional[Any] = None,
        connection_config: Optional[Mapping[str, Any]] = None,
        rate: Optional[float] = None,
    ) -> Dict[str, Any]:
        log_path = run_dir / "{}.log".format(role)
        log_handle = log_path.open("w", encoding="utf-8")
        args = connection_args or self.bridge.args
        options = self._connection_options(connection_config)
        command = [
            "runuser",
            "-u",
            args.run_as,
            "--",
            "env",
            "PGAPPNAME={}".format(app_name),
            *(["PGOPTIONS={}".format(options)] if options else []),
            "pgbench",
            "-h",
            args.host,
            "-p",
            str(args.port),
            "-U",
            args.db_user,
            "-n",
            "-M",
            "simple",
            "-c",
            str(clients),
            "-T",
            str(duration),
            *( ["-R", str(rate)] if rate is not None and rate > 0 else [] ),
            "-f",
            str(sql_path),
            "-P",
            "1",
            args.db,
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd="/",
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except BaseException:
            log_handle.close()
            raise
        return {
            "process": process,
            "log_handle": log_handle,
            "log_path": log_path,
            "app_name": app_name,
            "role": role,
            "command": command,
            "started_at": utc_now(),
        }

    def _sample_tpcc(
        self,
        run_id: str,
        phase: str,
        elapsed: float,
        app_prefix: str,
        processes: Sequence[Mapping[str, Any]],
        database_client: Optional[Any] = None,
    ) -> None:
        metrics: Dict[str, Any] = {
            "phase": phase,
            "elapsed_seconds": round(elapsed, 3),
            "control": {},
            "external": {},
        }
        db = database_client or self.db
        try:
            metrics["database"] = db.database_snapshot()
            rows = db._rows(
                """
                SELECT
                  COUNT(*) FILTER (WHERE state='active')::int AS active_total,
                  COUNT(*) FILTER (WHERE application_name LIKE {})::int AS lab_sessions,
                  COUNT(*) FILTER (WHERE state='active' AND application_name LIKE {})::int AS active_lab,
                  COUNT(*) FILTER (WHERE state='active' AND wait_event_type='Lock')::int AS lock_waits
                FROM pg_stat_activity
                WHERE datname=current_database() AND backend_type='client backend'
                """.format(_sql_literal(app_prefix + "%"), _sql_literal(app_prefix + "%")),
                "sysinsight-lab-sample",
            )
            if rows:
                metrics.update(rows[0])
        except Exception as exc:
            metrics["database_error"] = str(exc)
        for item in processes:
            parsed = _pgbench_metrics(Path(str(item["log_path"])))
            if item.get("role") == "control":
                metrics["control"] = parsed
                metrics["business"] = parsed
            elif item.get("role") == "external":
                metrics["external"] = parsed
                metrics["pressure"] = parsed
        try:
            alert = self.bridge._current_alert()
            metrics["alert_firing"] = bool(alert)
            metrics["alert"] = alert or {}
        except Exception as exc:
            metrics["alert_firing"] = False
            metrics["alert_error"] = str(exc)
        self.store.add_lab_sample(run_id, phase, elapsed, metrics)

    def _run_phase(
        self,
        run_id: str,
        phase: str,
        duration: int,
        normal_sql: Path,
        pressure_sql: Optional[Path],
        normal_clients: int,
        pressure_clients: int,
        stop_event: threading.Event,
        run_dir: Path,
        connection_args: Optional[Any] = None,
        connection_config: Optional[Mapping[str, Any]] = None,
        pressure_connection_args: Optional[Any] = None,
        pressure_connection_config: Optional[Mapping[str, Any]] = None,
        normal_rate: Optional[float] = None,
        pressure_rate: Optional[float] = None,
    ) -> Dict[str, Any]:
        self.store.update_lab_run(run_id, status="running", phase=phase)
        # PostgreSQL truncates application_name at 63 bytes. Keep the
        # experiment prefix short enough that the sampler can identify every
        # control/external backend reliably.
        prefix = self._application_prefix(run_id, "sysinsight") + phase + "-"
        processes: List[Dict[str, Any]] = []
        (run_dir / phase).mkdir(parents=True, exist_ok=True)
        try:
            control = self._start_pgbench(
                normal_sql,
                run_dir / phase,
                prefix + "control",
                normal_clients,
                duration,
                "control",
                connection_args=connection_args,
                connection_config=connection_config,
                rate=normal_rate,
            )
            processes.append(control)
            if pressure_sql is not None:
                external = self._start_pgbench(
                    pressure_sql,
                    run_dir / phase,
                    prefix + "external",
                    pressure_clients,
                    duration,
                    "external",
                    connection_args=pressure_connection_args or connection_args,
                    connection_config=(
                        pressure_connection_config
                        if pressure_connection_config is not None
                        else connection_config
                    ),
                    rate=pressure_rate,
                )
                processes.append(external)
            self._set_processes(run_id, processes)
            started = time.monotonic()
            while time.monotonic() - started < duration:
                if stop_event.is_set():
                    raise LabStopped("operator requested stop")
                self._sample_tpcc(
                    run_id,
                    phase,
                    time.monotonic() - started,
                    prefix,
                    processes,
                    database_client=self.db,
                )
                time.sleep(min(1.0, max(0.05, duration - (time.monotonic() - started))))
            if stop_event.is_set():
                raise LabStopped("operator requested stop")
            self._wait_processes(processes)
            failed = [
                "{} returned {}".format(item.get("role", "worker"), item.get("returncode"))
                for item in processes
                if item.get("returncode") not in (0, None)
            ]
            if failed:
                raise RuntimeError("TPCC phase {} failed: {}".format(phase, "; ".join(failed)))
            result = {
                "phase": phase,
                "duration_seconds": duration,
                "normal_target_tps": normal_rate,
                "pressure_target_tps": pressure_rate,
                "pressure_mode": "open_loop_fixed_rate" if pressure_rate else "closed_loop_observed_rate",
                "workers": [
                    {
                        "role": item.get("role"),
                        "app_name": item.get("app_name"),
                        "returncode": item.get("returncode"),
                        "log_path": str(item.get("log_path")),
                        "metrics": _pgbench_metrics(Path(str(item["log_path"]))),
                    }
                    for item in processes
                ],
            }
            self._set_processes(run_id, [])
            return result
        except BaseException:
            try:
                self._wait_processes(processes, terminate=True)
            except BaseException:
                LOGGER.exception("could not clean processes after TPCC phase %s failed", phase)
            finally:
                self._set_processes(run_id, [])
            raise

    def _run_tpcc(
        self,
        run_id: str,
        case: Mapping[str, Any],
        config: Mapping[str, Any],
        stop_event: threading.Event,
    ) -> None:
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        stage_dir: Optional[Path] = None
        phases: Dict[str, Any] = {}
        tuning: Dict[str, Any] = {}
        normal_rate = None
        pressure_rate = None
        try:
            if config.get("normal_target_tps") is not None:
                normal_rate = float(config["normal_target_tps"])
        except (TypeError, ValueError):
            normal_rate = None
        try:
            if config.get("pressure_target_tps") is not None:
                pressure_rate = float(config["pressure_target_tps"])
        except (TypeError, ValueError):
            pressure_rate = None
        run_started_at = utc_now()
        try:
            stage_dir = Path(tempfile.mkdtemp(prefix="sysinsight-lab-tpcc-", dir="/tmp"))
            stage_dir.chmod(0o755)
            normal_sql = self._stage_sql(TPCC_CASE_ROOT / str(case["normal_sql"]), stage_dir)
            pressure_sql = self._stage_sql(TPCC_CASE_ROOT / str(case["sql"]), stage_dir)
            self.store.update_lab_run(run_id, status="running", phase="baseline")
            phases["baseline"] = self._run_phase(
                run_id,
                "baseline",
                int(config["baseline_seconds"]),
                normal_sql,
                None,
                int(config["normal_clients"]),
                int(config["pressure_clients"]),
                stop_event,
                run_dir,
                normal_rate=normal_rate,
            )
            phases["pressure_before"] = self._run_phase(
                run_id,
                "pressure_before",
                int(config["pressure_before_seconds"]),
                normal_sql,
                pressure_sql,
                int(config["normal_clients"]),
                int(config["pressure_clients"]),
                stop_event,
                run_dir,
                normal_rate=normal_rate,
                pressure_rate=pressure_rate,
            )
            tuning = self._load_lab_tuning(str(case["scenario_id"]))
            tuned_normal_sql = normal_sql
            tuned_pressure_sql = pressure_sql
            tuned_connection_args: Any = self.bridge.args
            tuned_normal_connection_config: Mapping[str, Any] = {}
            tuned_pressure_connection_config: Mapping[str, Any] = {}
            if tuning.get("status") == "candidate_loaded" and tuning.get("applied_configuration"):
                config_applier = TemporaryPostgresConfiguration(
                    self.bridge.args,
                    dict(tuning.get("applied_configuration") or {}),
                    "sysinsight-lab-{}".format(self._run_marker(run_id)),
                    allow_restart=False,
                )
                tuning["status"] = "applying"
                try:
                    with config_applier as applied:
                        tuning["application"] = applied
                        tuning["status"] = "applied"
                        # TemporaryPostgresConfiguration exposes the endpoint
                        # that was actually activated.  Its raw normalized
                        # candidate also contains postmaster values such as
                        # port/unix_socket_directories that are intentionally
                        # skipped when allow_restart=False; using that raw
                        # mapping here would send the tuned workers to an
                        # inactive endpoint (for example port 5543).
                        tuned_connection_args = copy.copy(self.bridge.args)
                        endpoint = applied.get("connection_endpoint") or {}
                        if endpoint.get("host"):
                            tuned_connection_args.host = str(endpoint["host"])
                        if endpoint.get("port") is not None:
                            tuned_connection_args.port = int(endpoint["port"])
                        # Connection-startup options are applied once when
                        # pgbench opens a backend.  Do not prepend SET
                        # statements to the script: pgbench would execute
                        # those statements on every transaction and distort
                        # the business TPS being measured.
                        tuned_normal_connection_config = dict(
                            applied.get("connection_configuration", {})
                        )
                        tuned_normal_connection_config.update(
                            applied.get("session_configuration", {})
                        )
                        # Keep the external pressure recipe unchanged.  The
                        # candidate's connection-scoped settings are harmless
                        # bookkeeping for this lab (currently log settings),
                        # but session planner/commit settings must not alter
                        # the load generator's work.
                        tuned_pressure_connection_config = dict(
                            applied.get("connection_configuration", {})
                        )
                        tuned_stage_dir = stage_dir / "tuned"
                        # Keep a separate staged path for auditability, while
                        # passing the session candidate through PGOPTIONS at
                        # connection startup instead of executing SET per
                        # transaction.
                        tuned_normal_sql = self._stage_sql(
                            TPCC_CASE_ROOT / str(case["normal_sql"]),
                            tuned_stage_dir,
                            destination_name="tp_normal_tuned.sql",
                        )
                        old_host, old_port = self.db.host, self.db.port
                        self.db.host = str(getattr(tuned_connection_args, "host", old_host))
                        self.db.port = int(getattr(tuned_connection_args, "port", old_port))
                        try:
                            phases["pressure_after"] = self._run_phase(
                                run_id,
                                "pressure_after",
                                int(config["pressure_after_seconds"]),
                                tuned_normal_sql,
                                tuned_pressure_sql,
                                int(config["normal_clients"]),
                                int(config["pressure_clients"]),
                                stop_event,
                                run_dir,
                                connection_args=tuned_connection_args,
                                connection_config=tuned_normal_connection_config,
                                pressure_connection_args=tuned_connection_args,
                                pressure_connection_config=tuned_pressure_connection_config,
                                normal_rate=normal_rate,
                                pressure_rate=pressure_rate,
                            )
                        finally:
                            self.db.host, self.db.port = old_host, old_port
                    tuning["application"] = applied
                    tuning["status"] = applied.get("status", "applied_and_restored")
                    tuning["reason"] = "safe SysInsight candidate applied and restored; blocked settings were not activated"
                except BaseException as exc:
                    tuning["application"] = getattr(config_applier, "state", {})
                    tuning["status"] = "apply_failed"
                    tuning["reason"] = "safe candidate application or tuned phase failed: {}".format(exc)
                    raise
            else:
                phases["pressure_after"] = self._run_phase(
                    run_id,
                    "pressure_after",
                    int(config["pressure_after_seconds"]),
                    tuned_normal_sql,
                    tuned_pressure_sql,
                    int(config["normal_clients"]),
                    int(config["pressure_clients"]),
                    stop_event,
                    run_dir,
                    normal_rate=normal_rate,
                    pressure_rate=pressure_rate,
                )
            samples = self.store.list_lab_samples(run_id, 2000)
            for phase_name, phase_result in phases.items():
                duration = int(phase_result.get("duration_seconds") or config.get("pressure_seconds", 0))
                phase_result["business_tps_summary"] = _tpcc_tps_summary(
                    samples, phase_name, "business", duration
                )
                phase_result["pressure_tps_summary"] = _tpcc_tps_summary(
                    samples, phase_name, "pressure", duration
                )
            alert_observed = any(
                bool((sample.get("metrics") or {}).get("alert_firing"))
                for sample in samples
                if isinstance(sample, dict)
            )
            comparison = _tpcc_comparison(phases)
            comparison["tuning_status"] = tuning.get("status")
            comparison["tuning_source"] = tuning.get("source")
            current_incidents = [
                incident
                for incident in self.store.list_incidents(50)
                if str(incident.get("started_at") or "") >= run_started_at
            ]
            result = {
                "scenario": dict(case),
                "config": dict(config),
                "observation_model": "baseline_then_pressure_before_and_after_tuning",
                "phases": phases,
                "tuning": tuning,
                "comparison": comparison,
                "sample_count": len(samples),
                "completed_at": utc_now(),
                "sysinsight_state": {
                    "alert_firing": alert_observed,
                    "alert_observed": alert_observed,
                    "current_incidents": current_incidents,
                },
                "artifact_directory": str(run_dir),
            }
            (run_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            self.store.update_lab_run(run_id, status="completed", phase="completed", result_patch=result)
        except LabStopped as exc:
            result = {
                "scenario": dict(case),
                "config": dict(config),
                "observation_model": "baseline_then_pressure_before_and_after_tuning",
                "phases": phases,
                "tuning": tuning,
                "comparison": _tpcc_comparison(phases),
                "stopped_at": utc_now(),
                "artifact_directory": str(run_dir),
            }
            self._safe_update_run(run_id, "stopped", "stopped", str(exc), result)
        except BaseException as exc:
            result = {
                "scenario": dict(case),
                "config": dict(config),
                "observation_model": "baseline_then_pressure_before_and_after_tuning",
                "phases": phases,
                "tuning": tuning,
                "comparison": _tpcc_comparison(phases),
                "artifact_directory": str(run_dir),
            }
            try:
                (run_dir / "error.txt").write_text("{}: {}\n".format(type(exc).__name__, exc), encoding="utf-8")
            except OSError:
                LOGGER.exception("could not write TPCC error artifact for %s", run_id)
            self._safe_update_run(
                run_id,
                "failed",
                "failed",
                "{}: {}".format(type(exc).__name__, exc),
                result,
            )
        finally:
            self._cleanup_active_processes(run_id)
            if stage_dir is not None:
                shutil.rmtree(str(stage_dir), ignore_errors=True)

    def _memory_paths(self) -> List[Path]:
        paths: List[Path] = []
        try:
            config = json.loads(Path(self.bridge.args.dream_config).read_text(encoding="utf-8"))
            memory = config.get("MEMORY_MANAGER_CONFIG", {})
            for key in ("db_path", "samples_save_path"):
                value = memory.get(key)
                if value:
                    paths.append(Path(str(value)).expanduser().resolve())
        except (OSError, ValueError, TypeError):
            pass
        return paths

    def clear_dream(self, reset_pg_stat_statements: bool = True) -> Dict[str, Any]:
        if self._active_kind("dream"):
            raise RuntimeError("a dream lab run is already queued or running")
        stamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
        archive_dir = self.root / "archives"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / "dream-workspace-{}.sqlite3".format(stamp)
        # Stop the bridge collector from repopulating the rows between the
        # archive and the optional pg_stat_statements reset.
        with self.bridge._cycle_lock:
            result = self.store.clear_dream_records(archive_path)
            if reset_pg_stat_statements:
                try:
                    result["pg_stat_statements_reset"] = self.db.reset_statement_stats()
                except Exception as exc:
                    result["pg_stat_statements_reset"] = {"status": "failed", "error": str(exc)}
            else:
                result["pg_stat_statements_reset"] = {"status": "skipped"}
        memory_archives: List[str] = []
        memory_deleted: List[str] = []
        for path in self._memory_paths():
            if not path.is_file():
                continue
            target = archive_dir / "{}-{}".format(stamp, path.name)
            try:
                shutil.copy2(str(path), str(target))
                path.unlink()
                memory_archives.append(str(target))
                memory_deleted.append(str(path))
            except OSError as exc:
                result.setdefault("memory_errors", []).append(str(exc))
        result["memory_archives"] = memory_archives
        result["memory_deleted"] = memory_deleted
        result["completed_at"] = utc_now()
        (self.root / "last_dream_clear.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self.store.set_meta("last_lab_dream_clear", result)
        return result

    def start_dream(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        sql_id = str(body.get("sql_id") or "")
        item = self.get_sql(sql_id)
        # A manual lab action should fail before creating a run/job when the
        # worker cannot call the shared DREAM/SysInsight endpoint.  Previously
        # the request was accepted, the baseline ran, and the asynchronous
        # worker immediately produced a misleading ``completed /
        # analysis_completed / blocked`` record.
        dream_offline = bool(getattr(self.bridge.args, "dream_offline", False))
        api_configured = any(
            bool(os.environ.get(name))
            for name in ("SYSINSIGHT_GPT_API_KEY", "SYSINSIGHT_API_KEY", "OPENAI_API_KEY")
        )
        if not dream_offline and not api_configured:
            raise RuntimeError(
                "DREAM is not ready: shared API key is not configured; "
                "set SYSINSIGHT_GPT_API_KEY and restart the bridge"
            )
        run_id = self._run_id("dream")
        request_id = str(body.get("request_id") or "").strip()
        if len(request_id) > 160:
            raise ValueError("request_id is too long")
        config = {
            "sql_id": sql_id,
            "title": item["title"],
            "schema": item["schema"],
            "query_number": item["query_number"],
            "execution": "baseline_then_real_dream_analysis",
        }
        if request_id:
            config["request_id"] = request_id
        with self._lock:
            # Make browser retries idempotent even when the first worker has
            # already finished its very short baseline before the second HTTP
            # request arrives.  The request id is persisted in config_json,
            # so this also works across a page refresh within the same bridge
            # lifetime.
            if request_id:
                for previous in self.store.list_lab_runs(200, kind="dream"):
                    previous_config = previous.get("config") if isinstance(previous.get("config"), dict) else {}
                    if previous_config.get("request_id") == request_id:
                        return {
                            "status": "already_submitted",
                            "message": "this DREAM request was already submitted",
                            "run": previous,
                        }
            existing = self._active_kind("dream")
            if existing:
                if existing.get("scenario_id") == sql_id:
                    return {
                        "status": "already_running",
                        "message": "this SQL already has a DREAM run queued or running",
                        "run": existing,
                    }
                raise RuntimeError("a dream lab run is already queued or running")
            # A successful analysis already has a separate replay button. Do
            # not let a second click on the original-SQL button create a new
            # baseline/job for the same SQL; clear DREAM records explicitly
            # when a fresh run is intended.  Blocked/failed runs remain
            # retryable after the configuration problem is fixed.
            previous = next(
                (
                    value
                    for value in self.store.list_lab_runs(200, kind="dream")
                    if value.get("scenario_id") == sql_id
                    and value.get("status") == "completed"
                    and isinstance(value.get("result"), dict)
                    and isinstance(value["result"].get("job"), dict)
                    and value["result"]["job"].get("status") in {"completed", "candidate"}
                ),
                None,
            )
            if previous is not None:
                return {
                    "status": "already_completed",
                    "message": "this SQL already has a completed DREAM analysis; use replay or clear records first",
                    "run": previous,
                }
            self.store.create_lab_run(run_id, "dream", sql_id, item["title"], config)
            stop_event = threading.Event()
            self._submit_active(run_id, stop_event, self._run_dream, run_id, item, stop_event)
            return {"status": "queued", "run": self.store.get_lab_run(run_id)}

    def _execute_lab_sql(
        self,
        run_id: str,
        sql_id: str,
        phase: str,
        query: str,
        method: str,
        session_settings: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        started_at = utc_now()
        # PostgreSQL truncates application_name at NAMEDATALEN-1 (63 bytes).
        # Keep the run/phase marker short so the sampler can always distinguish
        # the lab connections, including DREAM replay sessions.
        result = self.db.execute_timed(
            query,
            application_name=self._application_prefix(run_id, "dream") + phase,
            search_path=[self.bridge.args.db_schema, "public"] if self.bridge.args.db_schema != "public" else ["public"],
            session_settings=session_settings,
            timeout=max(30.0, min(180.0, float(self.bridge.args.db_timeout) * 6.0)),
        )
        execution_id = self.store.add_lab_execution(
            {
                "run_id": run_id,
                "sql_id": sql_id,
                "phase": phase,
                "started_at": started_at,
                "finished_at": utc_now(),
                "duration_ms": result.get("duration_ms"),
                "status": result.get("status", "failed"),
                "method": method,
                "query_text": query,
                "result": result,
                "error": result.get("error", ""),
            }
        )
        return execution_id, result

    def _observation_for_lab_sql(self, item: Mapping[str, Any], duration_ms: float) -> str:
        query = str(item["query"])
        query_id = "lab-{}".format(item["sql_id"])
        row = {
            "database": self.bridge.args.db,
            "username": self.bridge.args.db_user,
            "queryid": query_id,
            "query": query,
            "canonical_sql": _canonical_sql(query),
            "hint_pattern": _hint_pattern(query),
            "replay_sql": query,
            "calls": 1,
            "total_time_ms": duration_ms,
            "min_time_ms": duration_ms,
            "max_time_ms": duration_ms,
            "mean_time_ms": duration_ms,
            "rows": 0,
            "shared_blks_hit": 0,
            "shared_blks_read": 0,
        }
        keys = self.store.upsert_observations([row], [])
        if not keys:
            raise RuntimeError("could not persist the selected SQL observation")
        return str(keys[0])

    @staticmethod
    def _job_summary(job: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        if not job:
            return {}
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        publication = result.get("bridge_publication") if isinstance(result.get("bridge_publication"), dict) else {}
        evaluation = result.get("evaluation") if isinstance(result.get("evaluation"), dict) else {}
        return {
            "job_id": job.get("job_id"),
            "status": job.get("status"),
            "attempts": job.get("attempts"),
            "error": job.get("error"),
            "reason": result.get("reason") or result.get("error") or publication.get("reason") or evaluation.get("msg", ""),
            "root_causes": result.get("root_causes", []),
            "fix_action": result.get("fix_action", ""),
            "rewrite_sql": result.get("rewrite_sql", ""),
            "old_time": result.get("old_time"),
            "new_time": result.get("new_time"),
            "evaluation_status": result.get("evaluation_status"),
            "bridge_publication": publication,
        }

    def _run_dream(self, run_id: str, item: Mapping[str, Any], stop_event: threading.Event) -> None:
        (self.root / run_id).mkdir(parents=True, exist_ok=True)
        try:
            self.store.update_lab_run(run_id, status="running", phase="baseline_execution")
            execution_id, baseline = self._execute_lab_sql(
                run_id,
                str(item["sql_id"]),
                "baseline",
                str(item["query"]),
                "original SQL baseline",
            )
            if baseline.get("status") != "completed":
                raise RuntimeError("baseline SQL failed: {}".format(baseline.get("error", "unknown error")))
            if stop_event.is_set():
                raise LabStopped("operator requested stop")
            sql_key = self._observation_for_lab_sql(item, float(baseline.get("duration_ms") or 0.0))
            self.store.update_lab_run(
                run_id,
                status="running",
                phase="dream_analysis",
                result_patch={
                    "baseline_execution_id": execution_id,
                    "baseline": baseline,
                    "sql_key": sql_key,
                },
            )
            job_id = self.store.enqueue_job(sql_key, None, cooldown=0.0, force=True)
            if not job_id:
                raise RuntimeError("selected SQL already has an active DREAM improvement; clear the lab records first")
            dream_future = self.bridge.executor.submit(self.bridge._run_dream_job, job_id)
            self.bridge.futures.append(dream_future)
            job_result = dream_future.result()
            job = self.store.get_job(job_id)
            improvement = self.store.latest_improvement(sql_key)
            result = {
                "sql_id": item["sql_id"],
                "title": item["title"],
                "query": item["query"],
                "baseline_execution_id": execution_id,
                "baseline": baseline,
                "sql_key": sql_key,
                "job_id": job_id,
                "job_result": job_result,
                "job": self._job_summary(job),
                "improvement": improvement,
                "analysis_completed_at": utc_now(),
            }
            (self.root / run_id / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            self.store.update_lab_run(run_id, status="completed", phase="analysis_completed", result_patch=result)
        except LabStopped as exc:
            self.store.update_lab_run(run_id, status="stopped", phase="stopped", error=str(exc))
        except Exception as exc:
            (self.root / run_id).mkdir(parents=True, exist_ok=True)
            (self.root / run_id / "error.txt").write_text("{}: {}\n".format(type(exc).__name__, exc), encoding="utf-8")
            self.store.update_lab_run(run_id, status="failed", phase="failed", error="{}: {}".format(type(exc).__name__, exc))

    def _parse_rewrite(self, value: str) -> Tuple[Dict[str, str], str]:
        settings: Dict[str, str] = {}
        remaining = str(value or "").strip()
        while remaining:
            match = _LEADING_SET.match(remaining)
            if not match:
                break
            name = match.group(1).lower()
            raw_value = match.group(2).strip().strip("'").strip('"')
            if name not in SESSION_SETTING_NAMES or not raw_value or not re.fullmatch(r"[A-Za-z0-9_.+/%-]+", raw_value):
                raise ValueError("DREAM rewrite contains an unsupported SET: {}".format(name))
            settings[name] = raw_value
            remaining = remaining[match.end():].strip()
        if remaining and not _is_read_only(remaining):
            raise ValueError("DREAM rewrite is not a single read-only statement")
        return settings, remaining

    def replay_dream(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        run_id = str(body.get("run_id") or "")
        with self._lock:
            row = self.store.get_lab_run(run_id)
            if row is None or row.get("kind") != "dream":
                raise KeyError("DREAM lab run not found: {}".format(run_id))
            result = row.get("result") if isinstance(row.get("result"), dict) else {}
            if result.get("optimized_execution_id"):
                return {"status": "already_completed", "run": row}
            # This is the critical idempotency guard.  It is held while the
            # row changes to optimized_execution and the future is attached,
            # so a rapid double-click cannot submit two replay workers.
            if row.get("status") == "running" and row.get("phase") == "optimized_execution":
                return {
                    "status": "already_queued",
                    "message": "optimized SQL replay is already queued or running",
                    "run": row,
                }
            if row.get("status") not in {"completed", "failed"}:
                raise RuntimeError("DREAM analysis is not finished")
            job = self.store.get_job(str(result.get("job_id", ""))) if result.get("job_id") else None
            improvement = self.store.latest_improvement(str(result.get("sql_key", ""))) if result.get("sql_key") else None
            if not job or str(job.get("status")) not in {"completed", "candidate"}:
                raise RuntimeError("DREAM job has not completed successfully")
            if not improvement:
                raise RuntimeError("DREAM did not produce an improvement candidate")
            apply_hint = bool(body.get("apply_hint", False))
            query = str(result.get("query") or "")
            settings: Dict[str, str] = {}
            method = ""
            if str(improvement.get("status")) == "active" and improvement.get("hints"):
                method = "pg_hint_plan active hint; original SQL"
            else:
                rewrite = str(improvement.get("rewrite_sql") or "").strip()
                action = str(improvement.get("fix_action") or "").strip()
                if rewrite:
                    settings, rewritten = self._parse_rewrite(rewrite)
                    if rewritten:
                        query = rewritten
                        method = "DREAM rewrite_sql"
                    elif not settings:
                        raise RuntimeError("DREAM rewrite is empty")
                if not method and action:
                    action_settings, action_query = self._parse_rewrite(action)
                    if action_query:
                        settings.update(action_settings)
                        query = action_query
                    else:
                        settings.update(action_settings)
                    if settings:
                        method = "DREAM session setting"
                if not method and improvement.get("hints") and apply_hint:
                    self.bridge.activate_improvement(str(improvement["improvement_id"]))
                    query = str(result.get("query") or "")
                    method = "pg_hint_plan explicit activation; original SQL"
                if not method:
                    raise RuntimeError("DREAM has no executable rewrite; for a Hint candidate enable apply_hint")
            if not _is_read_only(query):
                raise ValueError("optimized SQL is not read-only")
            self.store.update_lab_run(run_id, status="running", phase="optimized_execution")
            stop_event = threading.Event()
            self._submit_active(
                run_id,
                stop_event,
                self._run_dream_replay,
                run_id,
                str(result.get("sql_id")),
                query,
                method,
                settings,
                result,
                stop_event,
            )
            return {"status": "queued", "run": self.store.get_lab_run(run_id)}

    def _run_dream_replay(
        self,
        run_id: str,
        sql_id: str,
        query: str,
        method: str,
        settings: Mapping[str, str],
        previous: Mapping[str, Any],
        stop_event: threading.Event,
    ) -> None:
        try:
            if stop_event.is_set():
                raise LabStopped("operator requested stop")
            execution_id, optimized = self._execute_lab_sql(
                run_id,
                sql_id,
                "optimized",
                query,
                method,
                settings,
            )
            baseline_ms = float((previous.get("baseline") or {}).get("duration_ms") or 0.0)
            optimized_ms = float(optimized.get("duration_ms") or 0.0)
            ratio = (baseline_ms - optimized_ms) / baseline_ms if baseline_ms > 0 else 0.0
            patch = {
                "optimized_execution_id": execution_id,
                "optimized": optimized,
                "optimized_query": query,
                "optimization_method": method,
                "session_settings": dict(settings),
                "comparison": {
                    "baseline_ms": baseline_ms,
                    "optimized_ms": optimized_ms,
                    "improvement_ratio": ratio,
                    "improved": optimized.get("status") == "completed" and ratio > 0,
                },
                "replayed_at": utc_now(),
            }
            self.store.update_lab_run(run_id, status="completed", phase="completed", result_patch=patch)
            (self.root / run_id / "result.json").write_text(
                json.dumps({**dict(previous), **patch}, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except LabStopped as exc:
            self.store.update_lab_run(run_id, status="stopped", phase="replay_stopped", error=str(exc))
        except Exception as exc:
            self.store.update_lab_run(run_id, status="failed", phase="replay_failed", error="{}: {}".format(type(exc).__name__, exc))
