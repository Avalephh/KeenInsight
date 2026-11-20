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
import contextlib
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
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from pg_temporary_config import (  # noqa: E402
    TemporaryPostgresConfiguration,
    connection_args_for_configuration,
)


ROOT = Path(__file__).resolve().parent
TPCC_CASE_ROOT = ROOT / "tpcc_cases"
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
# The console's baseline/pressure modes are operator-controlled, so
# the pgbench phase duration is effectively a safety ceiling rather
# than a planned stop time.
TPCC_CONTINUOUS_SECONDS = 24 * 60 * 60

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
    """Own bounded TPCC system-tuning lab runs."""

    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge
        self.store = bridge.store
        self.db = bridge.db
        self.root = Path(bridge.output_root).resolve() / "lab"
        self.root.mkdir(parents=True, exist_ok=True)
        self.tpcc_cases = _case_catalog()
        self._lock = threading.RLock()
        self._active: Dict[str, Dict[str, Any]] = {}

        # A bridge restart cannot reattach a Python ThreadPoolExecutor to an
        # old run. Recover those rows and terminate only the lab connections
        # belonging to the old run, so the next click starts from a clean
        # state instead of seeing a permanent queued/running lock.
        self._recover_stale_runs("sysinsight")

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
            "database": {
                "name": self.bridge.args.db,
                "schema": self.bridge.args.db_schema,
                "host": self.bridge.args.host,
                "port": self.bridge.args.port,
                "reset_parameters": list(LAB_RESET_PARAMETERS),
            },
            "retention_hours": round(float(self.bridge.args.sample_retention_days) * 24.0, 3),
        }

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
            current_details.append(detail)

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
            "runs": runs,
        }

    def metrics_snapshot(self) -> Dict[str, Any]:
        value = self.status()
        result: Dict[str, Any] = {"current": value.get("current", [])}
        current = next((row for row in value.get("current", []) if row.get("kind") == "sysinsight"), None)
        latest = value.get("latest_sysinsight")
        row = current or latest or {}
        result["sysinsight"] = {
            "run_id": row.get("run_id", ""),
            "status": row.get("status", "idle"),
            "phase": row.get("phase", "idle"),
            "result": row.get("result", {}),
        }
        if current and current.get("samples"):
            result["sysinsight"]["sample"] = current["samples"][-1]
        return result

    def reset_database(self) -> Dict[str, Any]:
        """Reset only the known demo system-tuning GUCs."""

        before = self.db.settings_snapshot(LAB_RESET_PARAMETERS)
        database = '"{}"'.format(str(self.bridge.args.db).replace('"', '""'))
        commands: List[str] = []
        errors: List[Dict[str, str]] = []

        # Remove database-local overrides only when one is actually present.
        # session_preload_libraries is intentionally outside this list, so
        # this button cannot disconnect the monitoring runtime.
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
            "note": "仅清理实验白名单中的数据库级/ALTER SYSTEM 覆盖；不自动重启 PostgreSQL。",
            "completed_at": utc_now(),
        }
        path = self.root / "last_database_reset.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self.store.set_meta("last_lab_database_reset", result)
        return result

    def _tpcc_config(
        self,
        case: Mapping[str, Any],
        body: Mapping[str, Any],
        continuous_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        scenario_id = str(case["scenario_id"])
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
        if continuous_mode is not None:
            mode = str(continuous_mode).strip().lower()
            if mode not in {"baseline", "pressure"}:
                raise ValueError("continuous mode must be baseline or pressure")
            auto_tune_seconds = _bounded_int(
                body.get("auto_tune_seconds"), "auto_tune_seconds", 120, 10, 300
            )
            continuous_seconds = _bounded_int(
                body.get("continuous_seconds"),
                "continuous_seconds",
                TPCC_CONTINUOUS_SECONDS,
                30,
                86400,
            )
            pressure = mode == "pressure"
            config.update(
                {
                    "continuous": True,
                    "continuous_mode": mode,
                    "continuous_seconds": continuous_seconds,
                    "auto_tune_seconds": auto_tune_seconds,
                    "skip_baseline": pressure,
                    "baseline_seconds": 0 if pressure else continuous_seconds,
                    "pressure_seconds": auto_tune_seconds + continuous_seconds if pressure else 0,
                    "pressure_before_seconds": auto_tune_seconds if pressure else 0,
                    "pressure_after_seconds": continuous_seconds if pressure else 0,
                    "tuning_observation": (
                        "after {} s of pressure, apply the verified SysInsight candidate "
                        "and keep it active until the operator restores the baseline".format(auto_tune_seconds)
                    ),
                }
            )
        return config

    def start_tpcc(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        scenario_id = str(body.get("scenario_id") or "tp_payment_hot")
        case = next((value for value in self.tpcc_cases if value["scenario_id"] == scenario_id), None)
        if case is None:
            raise KeyError("unknown TPCC scenario: {}".format(scenario_id))
        config = self._tpcc_config(case, body)
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
        return self._queue_tpcc_run(case, config)

    def _queue_tpcc_run(self, case: Mapping[str, Any], config: Mapping[str, Any]) -> Dict[str, Any]:
        with self._lock:
            existing = self._active_kind("sysinsight")
            if existing:
                return {
                    "status": "already_running",
                    "message": "a sysinsight lab run is already queued or running",
                    "run": existing,
                }
            free_bytes = shutil.disk_usage("/").free
            if free_bytes < 512 * 1024 * 1024:
                raise RuntimeError("insufficient disk space for TPCC lab: {} bytes free".format(free_bytes))
            run_id = self._run_id("sysinsight")
            self.store.create_lab_run(run_id, "sysinsight", str(case["scenario_id"]), str(case["title"]), config)
            stop_event = threading.Event()
            self._submit_active(run_id, stop_event, self._run_tpcc, run_id, case, config, stop_event)
            return {"status": "queued", "run": self.store.get_lab_run(run_id)}

    def start_continuous(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        """Start or switch the operator-controlled baseline/pressure session.

        A baseline session runs the normal control workload indefinitely.
        A pressure session adds the selected external workload immediately,
        applies the verified SysInsight candidate after ``auto_tune_seconds``
        and keeps the tuned configuration running.  Switching modes stops the
        old worker, restores its configuration, and starts the requested mode.
        """

        scenario_id = str(body.get("scenario_id") or "tp_payment_hot")
        case = next((value for value in self.tpcc_cases if value["scenario_id"] == scenario_id), None)
        if case is None:
            raise KeyError("unknown TPCC scenario: {}".format(scenario_id))
        mode = str(body.get("mode") or "baseline").strip().lower()
        if mode not in {"baseline", "pressure"}:
            raise ValueError("mode must be baseline or pressure")
        active_run: Optional[Dict[str, Any]] = None
        with self._lock:
            active_rows = self._recover_stale_runs_locked("sysinsight")
            if active_rows:
                active_run = self.store.get_lab_run(str(active_rows[0]["run_id"]))
                active_config = (active_run or {}).get("config") or {}
                if not active_config.get("continuous"):
                    raise RuntimeError(
                        "a one-shot TPCC experiment is running; stop it before using the continuous console"
                    )
                if str(active_run.get("scenario_id")) != scenario_id:
                    raise RuntimeError(
                        "stop the current continuous experiment before changing scenario"
                    )
                if active_config.get("continuous_mode") == mode:
                    return {"status": "already_running", "run": active_run}
        if active_run:
            previous_id = str(active_run["run_id"])
            self.stop(previous_id)
            deadline = time.monotonic() + 60.0
            while True:
                row = self.store.get_lab_run(previous_id)
                with self._lock:
                    still_active = previous_id in self._active
                if (row is None or row.get("status") not in {"queued", "running"}) and not still_active:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "timed out stopping the previous continuous experiment; press Stop and retry"
                    )
                time.sleep(0.2)
        config = self._tpcc_config(case, body, continuous_mode=mode)
        queued = self._queue_tpcc_run(case, config)
        if queued.get("status") != "already_running":
            queued["status"] = "switch_queued" if active_run else "queued"
        return queued

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
        progress: Optional[Callable[[str, float], None]] = None,
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
            stopped = False
            while time.monotonic() - started < duration:
                if stop_event.is_set():
                    stopped = True
                    break
                self._sample_tpcc(
                    run_id,
                    phase,
                    time.monotonic() - started,
                    prefix,
                    processes,
                    database_client=self.db,
                )
                if progress is not None:
                    try:
                        progress(phase, time.monotonic() - started)
                    except Exception:
                        LOGGER.exception("could not publish live TPCC progress for %s", phase)
                time.sleep(min(1.0, max(0.05, duration - (time.monotonic() - started))))
            if stop_event.is_set():
                stopped = True
            if stopped:
                self._wait_processes(processes, terminate=True)
            else:
                self._wait_processes(processes)
            failed = [] if stopped else [
                "{} returned {}".format(item.get("role", "worker"), item.get("returncode"))
                for item in processes
                if item.get("returncode") not in (0, None)
            ]
            if failed:
                raise RuntimeError("TPCC phase {} failed: {}".format(phase, "; ".join(failed)))
            result = {
                "phase": phase,
                "duration_seconds": round(time.monotonic() - started, 3) if stopped else duration,
                "stopped": stopped,
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

    @staticmethod
    def _phase_summaries(samples: Sequence[Mapping[str, Any]], phase: str, duration: float) -> Dict[str, Any]:
        return {
            "business_tps_summary": _tpcc_tps_summary(samples, phase, "business", duration),
            "pressure_tps_summary": _tpcc_tps_summary(samples, phase, "pressure", duration),
        }

    def _attach_phase_summaries(self, run_id: str, phases: Dict[str, Any]) -> List[Dict[str, Any]]:
        samples = self.store.list_lab_samples(run_id, 5000)
        for name, item in phases.items():
            if not isinstance(item, dict) or "business_tps_summary" in item:
                continue
            item.update(self._phase_summaries(samples, name, item.get("duration_seconds") or 0))
        return samples

    def _publish_tpcc_progress(
        self,
        run_id: str,
        case: Mapping[str, Any],
        config: Mapping[str, Any],
        phases: Dict[str, Any],
        tuning: Mapping[str, Any],
        observation_model: str,
        phase_name: str,
        elapsed: float,
    ) -> None:
        samples = self.store.list_lab_samples(run_id, 5000)
        current = phases.get(phase_name)
        if isinstance(current, dict):
            current["duration_seconds"] = int(round(elapsed))
            current.update(self._phase_summaries(samples, phase_name, elapsed))
        comparison = _tpcc_comparison(phases)
        comparison["tuning_status"] = tuning.get("status")
        comparison["tuning_source"] = tuning.get("source")
        alert_observed = any(
            bool((sample.get("metrics") or {}).get("alert_firing"))
            for sample in samples
            if isinstance(sample, dict)
        )
        self.store.update_lab_run(
            run_id,
            result_patch={
                "scenario": dict(case),
                "config": dict(config),
                "observation_model": observation_model,
                "phases": phases,
                "tuning": dict(tuning),
                "comparison": comparison,
                "sample_count": len(samples),
                "updated_at": utc_now(),
                "live": True,
                "sysinsight_state": {
                    "alert_firing": alert_observed,
                    "alert_observed": alert_observed,
                    "current_incidents": [],
                },
            },
        )

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
        config_applier: Optional[TemporaryPostgresConfiguration] = None
        continuous_mode = str(config.get("continuous_mode") or "")
        continuous = bool(config.get("continuous"))
        observation_model = (
            "continuous_baseline"
            if continuous and continuous_mode == "baseline"
            else "continuous_pressure_auto_tune"
            if continuous
            else "baseline_then_pressure_before_and_after_tuning"
        )
        progress_state = {"at": 0.0}

        def progress(phase_name: str, elapsed: float) -> None:
            now = time.monotonic()
            if now - progress_state["at"] < 2.0:
                return
            progress_state["at"] = now
            try:
                self._publish_tpcc_progress(
                    run_id, case, config, phases, tuning, observation_model, phase_name, elapsed
                )
            except Exception:
                LOGGER.exception("could not publish live TPCC progress for %s", phase_name)

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
            initial_phase = "pressure_before" if config.get("skip_baseline") else "baseline"
            self.store.update_lab_run(run_id, status="running", phase=initial_phase)
            if not config.get("skip_baseline"):
                phases["baseline"] = {"phase": "baseline", "duration_seconds": 0, "status": "running"}
                baseline_result = self._run_phase(
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
                    progress=progress,
                )
                baseline_result.update(
                    self._phase_summaries(
                        self.store.list_lab_samples(run_id, 5000),
                        "baseline",
                        baseline_result.get("duration_seconds") or 0,
                    )
                )
                phases["baseline"] = baseline_result
                if stop_event.is_set():
                    raise LabStopped("operator requested stop")

            if continuous_mode != "baseline":
                phases["pressure_before"] = {"phase": "pressure_before", "duration_seconds": 0, "status": "running", "pressure_target_tps": pressure_rate}
                before_result = self._run_phase(
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
                    progress=progress,
                )
                before_result.update(
                    self._phase_summaries(
                        self.store.list_lab_samples(run_id, 5000),
                        "pressure_before",
                        before_result.get("duration_seconds") or 0,
                    )
                )
                phases["pressure_before"] = before_result
                if stop_event.is_set():
                    raise LabStopped("operator requested stop")

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
                                phases["pressure_after"] = {"phase": "pressure_after", "duration_seconds": 0, "status": "running", "pressure_target_tps": pressure_rate}
                                after_result = self._run_phase(
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
                                    progress=progress,
                                )
                                after_result.update(
                                    self._phase_summaries(
                                        self.store.list_lab_samples(run_id, 5000),
                                        "pressure_after",
                                        after_result.get("duration_seconds") or 0,
                                    )
                                )
                                phases["pressure_after"] = after_result
                            finally:
                                self.db.host, self.db.port = old_host, old_port
                            if stop_event.is_set():
                                raise LabStopped("operator requested stop")
                        tuning["application"] = applied
                        tuning["status"] = applied.get("status", "applied_and_restored")
                        tuning["reason"] = "safe SysInsight candidate applied and restored; blocked settings were not activated"
                    except BaseException as exc:
                        tuning["application"] = getattr(config_applier, "state", {})
                        tuning["status"] = "apply_failed"
                        tuning["reason"] = "safe candidate application or tuned phase failed: {}".format(exc)
                        raise
                else:
                    phases["pressure_after"] = {"phase": "pressure_after", "duration_seconds": 0, "status": "running", "pressure_target_tps": pressure_rate}
                    after_result = self._run_phase(
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
                        progress=progress,
                    )
                    after_result.update(
                        self._phase_summaries(
                            self.store.list_lab_samples(run_id, 5000),
                            "pressure_after",
                            after_result.get("duration_seconds") or 0,
                        )
                    )
                    phases["pressure_after"] = after_result
                    if stop_event.is_set():
                        raise LabStopped("operator requested stop")

            samples = self._attach_phase_summaries(run_id, phases)
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
                "observation_model": observation_model,
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
            if config_applier is not None:
                tuning["application"] = getattr(config_applier, "state", {})
                tuning["status"] = str(tuning["application"].get("status") or tuning.get("status") or "")
            self._attach_phase_summaries(run_id, phases)
            result = {
                "scenario": dict(case),
                "config": dict(config),
                "observation_model": observation_model,
                "phases": phases,
                "tuning": tuning,
                "comparison": _tpcc_comparison(phases),
                "stopped_at": utc_now(),
                "artifact_directory": str(run_dir),
            }
            self._safe_update_run(run_id, "stopped", "stopped", str(exc), result)
        except BaseException as exc:
            if config_applier is not None:
                tuning["application"] = getattr(config_applier, "state", {})
                tuning["status"] = str(tuning["application"].get("status") or tuning.get("status") or "")
            self._attach_phase_summaries(run_id, phases)
            result = {
                "scenario": dict(case),
                "config": dict(config),
                "observation_model": observation_model,
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
