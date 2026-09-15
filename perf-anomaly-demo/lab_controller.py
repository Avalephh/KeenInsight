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
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parent
TPCC_CASE_ROOT = ROOT / "tpcc_cases"
TPCDS_QUERY_ROOT = ROOT.parent / "dream" / "data" / "slow_queries" / "TPC-DS"

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
    result: List[Dict[str, Any]] = []
    for value in getattr(tpcc_transaction_cases, "CASE_DEFINITIONS", []):
        if not isinstance(value, dict) or value.get("mode") != "pgbench":
            continue
        sql_name = str(value.get("sql", ""))
        normal_name = str(value.get("normal_sql", "tp_normal.sql"))
        if not (TPCC_CASE_ROOT / sql_name).is_file() or not (TPCC_CASE_ROOT / normal_name).is_file():
            continue
        result.append(
            {
                "scenario_id": str(value.get("id")),
                "title": str(value.get("title", value.get("id", "TPCC"))),
                "event": str(value.get("event", "")),
                "clients": int(value.get("clients", 1)),
                "sql": sql_name,
                "normal_sql": normal_name,
                "transactions": list(value.get("tpcc_transactions", [])),
                "pressure_evidence": list(value.get("pressure_evidence", [])),
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
                # Q4 is the bounded, real-data scenario validated by this
                # console.  Keep every catalog query selectable, but let a
                # fresh operator land on a known-good complex SQL instead of
                # an arbitrary benchmark query that may exceed the lab
                # timeout on a local-scale dataset.
                "recommended": query_id == 4,
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
        # old run. Mark those rows explicitly so the dashboard never presents
        # stale work as active.
        for row in self.store.list_lab_runs(200):
            if row.get("status") in {"queued", "running"}:
                self.store.update_lab_run(
                    str(row.get("run_id")),
                    status="failed",
                    phase="bridge_restart",
                    error="bridge restarted before the experiment could be reattached",
                )

    def shutdown(self) -> None:
        with self._lock:
            active = list(self._active.values())
            for item in active:
                event = item.get("stop_event")
                if event is not None:
                    event.set()
                self._terminate_processes(item.get("processes", []))

    def _run_id(self, kind: str) -> str:
        digest = hashlib.sha1(str(time.time_ns()).encode()).hexdigest()[:10]
        return "lab-{}-{}-{}".format(kind, dt.datetime.now().strftime("%Y%m%d%H%M%S"), digest)

    def _active_kind(self, kind: str) -> None:
        for row in self.store.list_lab_runs(100, kind=kind):
            if row.get("status") in {"queued", "running"}:
                raise RuntimeError("a {} lab run is already queued or running".format(kind))

    def catalog(self) -> Dict[str, Any]:
        return {
            "status": "ok",
            "tpcc_scenarios": self.tpcc_cases,
            "sql_catalog": self.sql_catalog,
            "database": {
                "name": self.bridge.args.db,
                "schema": self.bridge.args.db_schema,
                "host": self.bridge.args.host,
                "port": self.bridge.args.port,
                "reset_parameters": list(LAB_RESET_PARAMETERS),
            },
            "lab": self.status(),
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
        runs = self.store.list_lab_runs(30)
        current = [row for row in runs if row.get("status") in {"queued", "running"}]
        current_details: List[Dict[str, Any]] = []
        for row in current:
            detail = dict(row)
            detail["samples"] = self.store.list_lab_samples(str(row["run_id"]), 180)
            detail["executions"] = self.store.list_lab_executions(str(row["run_id"]), 20)
            current_details.append(detail)

        latest_dream = next((row for row in runs if row.get("kind") == "dream"), None)
        dream_detail: Optional[Dict[str, Any]] = None
        if latest_dream:
            dream_detail = dict(latest_dream)
            dream_detail["executions"] = self.store.list_lab_executions(str(latest_dream["run_id"]), 20)
            result = latest_dream.get("result") if isinstance(latest_dream.get("result"), dict) else {}
            job_id = result.get("job_id")
            if job_id:
                dream_detail["job"] = self.store.get_job(str(job_id))
                if result.get("sql_key"):
                    dream_detail["improvement"] = self.store.latest_improvement(str(result["sql_key"]))

        latest_sysinsight = next((row for row in runs if row.get("kind") == "sysinsight"), None)
        sysinsight_detail: Optional[Dict[str, Any]] = None
        if latest_sysinsight:
            sysinsight_detail = dict(latest_sysinsight)
            sysinsight_detail["samples"] = self.store.list_lab_samples(str(latest_sysinsight["run_id"]), 180)
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
        self._active_kind("sysinsight")
        scenario_id = str(body.get("scenario_id") or "tp_order_status_burst")
        case = next((value for value in self.tpcc_cases if value["scenario_id"] == scenario_id), None)
        if case is None:
            raise KeyError("unknown TPCC scenario: {}".format(scenario_id))
        normal_clients = _bounded_int(body.get("normal_clients"), "normal_clients", 2, 1, 16)
        pressure_clients = _bounded_int(body.get("pressure_clients"), "pressure_clients", case["clients"], 1, 32)
        baseline_seconds = _bounded_int(body.get("baseline_seconds"), "baseline_seconds", 10, 5, 180)
        pressure_seconds = _bounded_int(body.get("pressure_seconds"), "pressure_seconds", 20, 5, 180)
        recovery_seconds = _bounded_int(body.get("recovery_seconds"), "recovery_seconds", 15, 5, 180)
        free_bytes = shutil.disk_usage("/").free
        if free_bytes < 512 * 1024 * 1024:
            raise RuntimeError("insufficient disk space for TPCC lab: {} bytes free".format(free_bytes))
        config = {
            "normal_clients": normal_clients,
            "pressure_clients": pressure_clients,
            "baseline_seconds": baseline_seconds,
            "pressure_seconds": pressure_seconds,
            "recovery_seconds": recovery_seconds,
            "actual_prometheus_alert": True,
        }
        run_id = self._run_id("sysinsight")
        self.store.create_lab_run(run_id, "sysinsight", scenario_id, case["title"], config)
        stop_event = threading.Event()
        with self._lock:
            self._active[run_id] = {"stop_event": stop_event, "processes": []}
        future = self.bridge.lab_executor.submit(self._run_tpcc, run_id, case, config, stop_event)
        future.add_done_callback(lambda _future: self._clear_active(run_id))
        return {"status": "queued", "run": self.store.get_lab_run(run_id)}

    def stop(self, run_id: str) -> Dict[str, Any]:
        row = self.store.get_lab_run(run_id)
        if row is None:
            raise KeyError("lab run not found: {}".format(run_id))
        if row.get("status") not in {"queued", "running"}:
            return {"status": "unchanged", "run": row}
        with self._lock:
            item = self._active.get(run_id)
            if item:
                item["stop_event"].set()
                self._terminate_processes(item.get("processes", []))
        return {"status": "stop_requested", "run": self.store.get_lab_run(run_id)}

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

    def _stage_sql(self, source: Path, stage_dir: Path) -> Path:
        destination = stage_dir / source.name
        shutil.copyfile(str(source), str(destination))
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
    ) -> Dict[str, Any]:
        log_path = run_dir / "{}.log".format(role)
        log_handle = log_path.open("w", encoding="utf-8")
        args = self.bridge.args
        command = [
            "runuser",
            "-u",
            args.run_as,
            "--",
            "env",
            "PGAPPNAME={}".format(app_name),
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
            "-f",
            str(sql_path),
            "-P",
            "1",
            args.db,
        ]
        process = subprocess.Popen(
            command,
            cwd="/",
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
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
    ) -> None:
        metrics: Dict[str, Any] = {
            "phase": phase,
            "elapsed_seconds": round(elapsed, 3),
            "control": {},
            "external": {},
        }
        try:
            metrics["database"] = self.db.database_snapshot()
            rows = self.db._rows(
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
            elif item.get("role") == "external":
                metrics["external"] = parsed
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
    ) -> Dict[str, Any]:
        self.store.update_lab_run(run_id, status="running", phase=phase)
        # PostgreSQL truncates application_name at 63 bytes. Keep the
        # experiment prefix short enough that the sampler can identify every
        # control/external backend reliably.
        prefix = "lab-{}-{}-".format(run_id.rsplit("-", 1)[-1][:8], phase)
        processes: List[Dict[str, Any]] = []
        (run_dir / phase).mkdir(parents=True, exist_ok=True)
        control = self._start_pgbench(
            normal_sql,
            run_dir / phase,
            prefix + "control",
            normal_clients,
            duration,
            "control",
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
            )
            processes.append(external)
        self._set_processes(run_id, processes)
        started = time.monotonic()
        try:
            while time.monotonic() - started < duration:
                if stop_event.is_set():
                    raise LabStopped("operator requested stop")
                self._sample_tpcc(run_id, phase, time.monotonic() - started, prefix, processes)
                time.sleep(min(1.0, max(0.05, duration - (time.monotonic() - started))))
            if stop_event.is_set():
                raise LabStopped("operator requested stop")
        except Exception:
            self._wait_processes(processes, terminate=True)
            raise
        self._wait_processes(processes)
        failed = [
            "{} returned {}".format(item.get("role", "worker"), item.get("returncode"))
            for item in processes
            if item.get("returncode") not in (0, None)
        ]
        if failed:
            self._set_processes(run_id, [])
            raise RuntimeError("TPCC phase {} failed: {}".format(phase, "; ".join(failed)))
        result = {
            "phase": phase,
            "duration_seconds": duration,
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

    def _run_tpcc(
        self,
        run_id: str,
        case: Mapping[str, Any],
        config: Mapping[str, Any],
        stop_event: threading.Event,
    ) -> None:
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        stage_dir = Path(tempfile.mkdtemp(prefix="sysinsight-lab-tpcc-", dir="/tmp"))
        stage_dir.chmod(0o755)
        phases: Dict[str, Any] = {}
        run_started_at = utc_now()
        try:
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
            )
            phases["pressure"] = self._run_phase(
                run_id,
                "pressure",
                int(config["pressure_seconds"]),
                normal_sql,
                pressure_sql,
                int(config["normal_clients"]),
                int(config["pressure_clients"]),
                stop_event,
                run_dir,
            )
            phases["recovery"] = self._run_phase(
                run_id,
                "recovery",
                int(config["recovery_seconds"]),
                normal_sql,
                None,
                int(config["normal_clients"]),
                int(config["pressure_clients"]),
                stop_event,
                run_dir,
            )
            samples = self.store.list_lab_samples(run_id, 2000)
            alert_observed = any(
                bool((sample.get("metrics") or {}).get("alert_firing"))
                for sample in samples
                if isinstance(sample, dict)
            )
            current_incidents = [
                incident
                for incident in self.store.list_incidents(50)
                if str(incident.get("started_at") or "") >= run_started_at
            ]
            result = {
                "scenario": dict(case),
                "config": dict(config),
                "phases": phases,
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
                "phases": phases,
                "stopped_at": utc_now(),
                "artifact_directory": str(run_dir),
            }
            self.store.update_lab_run(run_id, status="stopped", phase="stopped", result_patch=result, error=str(exc))
        except Exception as exc:
            result = {
                "scenario": dict(case),
                "config": dict(config),
                "phases": phases,
                "artifact_directory": str(run_dir),
            }
            (run_dir / "error.txt").write_text("{}: {}\n".format(type(exc).__name__, exc), encoding="utf-8")
            self.store.update_lab_run(run_id, status="failed", phase="failed", result_patch=result, error="{}: {}".format(type(exc).__name__, exc))
        finally:
            with self._lock:
                active = self._active.get(run_id)
                if active:
                    self._terminate_processes(active.get("processes", []))
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
        self._active_kind("dream")
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
        self._active_kind("dream")
        sql_id = str(body.get("sql_id") or "")
        item = self.get_sql(sql_id)
        run_id = self._run_id("dream")
        config = {
            "sql_id": sql_id,
            "title": item["title"],
            "schema": item["schema"],
            "query_number": item["query_number"],
            "execution": "baseline_then_real_dream_analysis",
        }
        self.store.create_lab_run(run_id, "dream", sql_id, item["title"], config)
        stop_event = threading.Event()
        with self._lock:
            self._active[run_id] = {"stop_event": stop_event, "processes": []}
        future = self.bridge.lab_executor.submit(self._run_dream, run_id, item, stop_event)
        future.add_done_callback(lambda _future: self._clear_active(run_id))
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
        run_marker = str(run_id).rsplit("-", 1)[-1][:12]
        result = self.db.execute_timed(
            query,
            application_name="lab-dream-{}-{}".format(run_marker, phase),
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
        row = self.store.get_lab_run(run_id)
        if row is None or row.get("kind") != "dream":
            raise KeyError("DREAM lab run not found: {}".format(run_id))
        if row.get("status") not in {"completed", "failed"}:
            raise RuntimeError("DREAM analysis is not finished")
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        if result.get("optimized_execution_id"):
            return {"status": "already_completed", "run": row}
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
        with self._lock:
            self._active[run_id] = {"stop_event": stop_event, "processes": []}
        future = self.bridge.lab_executor.submit(
            self._run_dream_replay,
            run_id,
            str(result.get("sql_id")),
            query,
            method,
            settings,
            result,
            stop_event,
        )
        future.add_done_callback(lambda _future: self._clear_active(run_id))
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
