#!/usr/bin/env python3
"""MySQL version of the TPCC system-tuning lab.

The bridge and portal contract is shared with the PostgreSQL lab: the same
``lab_runs``/``lab_samples`` tables, phase names and comparison fields are
used.  The workload, tuning applier and state checks are MySQL-specific.
"""
from __future__ import annotations

import json
import os
import shutil
import statistics
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from mysql_temporary_config import MySQLTemporaryConfiguration
from mysql_workload import MySQLLabWorkload, WorkloadSpec

try:
    from lab_controller import _tpcc_comparison, _tpcc_tps_summary
except Exception:  # pragma: no cover - fallback keeps the MySQL module standalone
    _tpcc_comparison = None
    _tpcc_tps_summary = None


MYSQL_SCENARIOS = [
    {"scenario_id": "tp_payment_hot", "title": "TPCC 热点仓库 Payment 外部并发", "clients": 3,
     "pressure_target_tps": 900, "scenario": "payment_hot", "focus": True,
     "transactions": ["Payment (hot warehouse/district)"]},
    {"scenario_id": "tp_stock_level_hot", "title": "TPCC 热点仓库 Stock Level 外部并发", "clients": 5,
     "pressure_target_tps": 1000, "scenario": "stock_level_hot", "focus": True,
     "transactions": ["Stock Level (hot warehouse/district)"]},
    {"scenario_id": "tp_stock_level_burst", "title": "TPCC Stock Level 外部读并发", "clients": 5,
     "pressure_target_tps": 1000, "scenario": "stock_level_burst", "focus": True,
     "transactions": ["Stock Level"]},
    {"scenario_id": "tp_full_mix_surge_high", "title": "TPCC 五类交易高强度混合突发", "clients": 8,
     "pressure_target_tps": 1200, "scenario": "full_mix", "focus": True,
     "transactions": ["New Order", "Payment", "Order Status", "Delivery", "Stock Level"]},
    {"scenario_id": "tp_wal_checkpoint", "title": "TPCC 写入突增造成 InnoDB redo/checkpoint 压力", "clients": 10,
     "pressure_target_tps": 1200, "scenario": "wal_checkpoint", "focus": True,
     "transactions": ["New Order", "Payment", "Order Status", "Delivery", "Stock Level"]},
    {"scenario_id": "tp_order_status_hot", "title": "TPCC 热点仓库 Order Status 外部并发", "clients": 6,
     "pressure_target_tps": 1000, "scenario": "order_status_hot", "focus": True,
     "transactions": ["Order Status (hot warehouse/district)"]},
]
CONTINUOUS_SECONDS = 24 * 60 * 60


def utc_now() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def _bounded_int(value: Any, name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if number < minimum or number > maximum:
        raise ValueError("{} must be between {} and {}".format(name, minimum, maximum))
    return number


class MySQLLabController:
    def __init__(self, bridge: Any) -> None:
        self.bridge = bridge
        self.store = bridge.store
        self.db = bridge.db
        self.root = Path(bridge.output_root).resolve() / "lab"
        self.root.mkdir(parents=True, exist_ok=True)
        self.tpcc_cases = [dict(value) for value in MYSQL_SCENARIOS]
        self._lock = threading.RLock()
        self._active: Dict[str, Dict[str, Any]] = {}
        self._workload: Optional[MySQLLabWorkload] = None
        self._password = os.environ.get("SYSINSIGHT_MYSQL_PASSWORD", "")
        if not self._password:
            try:
                for line in Path("/etc/keeninsight/mysql.env").read_text(encoding="utf-8").splitlines():
                    if line.startswith("SYSINSIGHT_MYSQL_PASSWORD="):
                        self._password = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
            except OSError:
                pass

    def _new_workload(self) -> MySQLLabWorkload:
        return MySQLLabWorkload(
            self.bridge.args.host, self.bridge.args.port, self.bridge.args.db_user,
            self._password, database=self.bridge.args.db, timeout=float(self.bridge.args.db_timeout),
        )

    def catalog(self) -> Dict[str, Any]:
        return {
            "status": "ok",
            "tpcc_scenarios": self.tpcc_cases,
            "tpcc_focus_scenarios": self.tpcc_cases,
            "tpcc_metric": {
                "primary": "normal_business_tps",
                "primary_label": "Target business TPS",
                "primary_source": "MySQL normal TPCC control workload",
                "secondary": "pressure_injection_tps",
                "secondary_label": "Pressure injection TPS",
                "secondary_source": "MySQL external scenario workload",
                "scenario_transaction": "sysinsight_mysql_lab_tpcc_scenario_transaction_tps",
            },
            "database": {
                "name": self.bridge.args.db,
                "schema": self.bridge.args.db_schema,
                "host": self.bridge.args.host,
                "port": self.bridge.args.port,
                "dbms": "mysql",
            },
            "retention_hours": round(float(self.bridge.args.sample_retention_days) * 24.0, 3),
        }

    def status(self) -> Dict[str, Any]:
        runs = self.store.list_lab_runs(30, include_result=False)
        current = [row for row in runs if row.get("status") in {"queued", "running"}]
        current_details = []
        for row in current:
            detail = self.store.get_lab_run(str(row["run_id"])) or dict(row)
            detail["samples"] = self.store.list_lab_samples(str(row["run_id"]), 1)
            current_details.append(detail)
        latest = next((row for row in runs if row.get("kind") == "sysinsight"), None)
        latest_detail = None
        if latest:
            latest_detail = self.store.get_lab_run(str(latest["run_id"])) or latest
            latest_detail = dict(latest_detail)
            latest_detail["samples"] = self.store.list_lab_samples(str(latest["run_id"]), 1)
        return {"status": "ok", "current": current_details, "latest_sysinsight": latest_detail, "runs": runs}

    def metrics_snapshot(self) -> Dict[str, Any]:
        current = next((row for row in self.status().get("current", []) if row.get("kind") == "sysinsight"), None)
        latest = self.status().get("latest_sysinsight")
        row = current or latest or {}
        result = {"sysinsight": {"run_id": row.get("run_id", ""), "status": row.get("status", "idle"),
                                  "phase": row.get("phase", "idle"), "result": row.get("result", {})}}
        if current and current.get("samples"):
            result["sysinsight"]["sample"] = current["samples"][-1]
        return result

    def reset_database(self) -> Dict[str, Any]:
        return {"status": "completed", "note": "MySQL lab uses SET GLOBAL variables and restores them per run; no restart is performed."}

    def _config(self, case: Mapping[str, Any], body: Mapping[str, Any], continuous_mode: Optional[str] = None) -> Dict[str, Any]:
        normal_clients = _bounded_int(body.get("normal_clients"), "normal_clients", 2, 1, 16)
        pressure_clients = _bounded_int(body.get("pressure_clients"), "pressure_clients", case["clients"], 1, 32)
        baseline_seconds = _bounded_int(body.get("baseline_seconds"), "baseline_seconds", 30, 5, 300)
        pressure_seconds = _bounded_int(body.get("pressure_seconds"), "pressure_seconds", 120, 10, 300)
        before = max(5, pressure_seconds // 2)
        after = pressure_seconds - before
        normal_target = round(500.0 * normal_clients / 2.0, 1)
        pressure_target = round(float(case["pressure_target_tps"]) * pressure_clients / float(case["clients"]), 1)
        config = {
            "normal_clients": normal_clients, "pressure_clients": pressure_clients,
            "baseline_seconds": baseline_seconds, "pressure_seconds": pressure_seconds,
            "pressure_before_seconds": before, "pressure_after_seconds": after,
            "normal_target_tps": normal_target, "pressure_target_tps": pressure_target,
            "pressure_mode": "open_loop_fixed_rate",
            "primary_metric": "normal_business_tps",
            "primary_metric_source": "MySQL normal TPCC control workload",
            "secondary_metric": "pressure_injection_tps",
            "dbms": "mysql",
        }
        if continuous_mode:
            mode = str(continuous_mode).lower()
            if mode not in {"baseline", "pressure"}:
                raise ValueError("continuous mode must be baseline or pressure")
            auto = _bounded_int(body.get("auto_tune_seconds"), "auto_tune_seconds", 120, 10, 600)
            seconds = _bounded_int(body.get("continuous_seconds"), "continuous_seconds", CONTINUOUS_SECONDS, 30, 86400)
            pressure = mode == "pressure"
            config.update({
                "continuous": True, "continuous_mode": mode, "auto_tune_seconds": auto,
                "continuous_seconds": seconds, "skip_baseline": pressure,
                "baseline_seconds": 0 if pressure else seconds,
                "pressure_before_seconds": auto if pressure else 0,
                "pressure_after_seconds": seconds if pressure else 0,
                "pressure_seconds": auto + seconds if pressure else 0,
            })
        return config

    def _queue(self, case: Mapping[str, Any], config: Mapping[str, Any]) -> Dict[str, Any]:
        with self._lock:
            existing = self._active.get("sysinsight")
            if existing:
                return {"status": "already_running", "run": self.store.get_lab_run(existing["run_id"])}
            run_id = "mysql-lab-" + uuid.uuid4().hex[:16]
            self.store.create_lab_run(run_id, "sysinsight", str(case["scenario_id"]), str(case["title"]), config)
            stop_event = threading.Event()
            self._active["sysinsight"] = {"run_id": run_id, "stop_event": stop_event, "processes": []}
            thread = threading.Thread(target=self._run_tpcc, args=(run_id, case, config, stop_event), daemon=True)
            thread.start()
            self._active["sysinsight"]["thread"] = thread
            return {"status": "queued", "run": self.store.get_lab_run(run_id)}

    def start_tpcc(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        scenario_id = str(body.get("scenario_id") or "tp_payment_hot")
        case = next((value for value in self.tpcc_cases if value["scenario_id"] == scenario_id), None)
        if case is None:
            raise KeyError("unknown MySQL scenario: {}".format(scenario_id))
        return self._queue(case, self._config(case, body))

    def start_continuous(self, body: Mapping[str, Any]) -> Dict[str, Any]:
        scenario_id = str(body.get("scenario_id") or "tp_payment_hot")
        case = next((value for value in self.tpcc_cases if value["scenario_id"] == scenario_id), None)
        if case is None:
            raise KeyError("unknown MySQL scenario: {}".format(scenario_id))
        mode = str(body.get("mode") or "baseline").lower()
        if mode not in {"baseline", "pressure"}:
            raise ValueError("mode must be baseline or pressure")
        active = self._active.get("sysinsight")
        if active:
            row = self.store.get_lab_run(active["run_id"]) or {}
            current_config = row.get("config") or {}
            if current_config.get("continuous_mode") == mode and row.get("scenario_id") == scenario_id:
                return {"status": "already_running", "run": row}
            self.stop(active["run_id"])
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and self._active.get("sysinsight"):
                time.sleep(0.2)
        result = self._queue(case, self._config(case, body, continuous_mode=mode))
        if result.get("status") == "queued":
            result["status"] = "switch_queued" if active else "queued"
        return result

    def stop(self, run_id: str) -> Dict[str, Any]:
        with self._lock:
            item = self._active.get("sysinsight")
            if not item or item.get("run_id") != run_id:
                return {"status": "unchanged", "run": self.store.get_lab_run(run_id)}
            item["stop_event"].set()
            if self._workload is not None:
                self._workload.stop()
            self.store.update_lab_run(run_id, status="stopped", phase="stopped", error="operator requested stop")
            return {"status": "stop_requested", "run": self.store.get_lab_run(run_id)}

    def shutdown(self) -> None:
        item = self._active.get("sysinsight")
        if item:
            item["stop_event"].set()
        if self._workload is not None:
            self._workload.stop()

    def _summary(self, samples: Sequence[Mapping[str, Any]], phase: str, key: str, duration: float) -> Dict[str, Any]:
        if _tpcc_tps_summary is not None:
            return _tpcc_tps_summary(samples, phase, key, duration)
        return {}

    def _sample(self, run_id: str, phase: str, elapsed: float, workload: MySQLLabWorkload) -> Dict[str, Any]:
        raw = workload.sample()
        metrics = {"phase": phase, "elapsed_seconds": round(elapsed, 3),
                   "control": raw.get("business", {}), "business": raw.get("business", {}),
                   "external": raw.get("pressure", {}), "pressure": raw.get("pressure", {})}
        self.store.add_lab_sample(run_id, phase, elapsed, metrics)
        return metrics

    def _run_phase(self, run_id: str, phase: str, duration: float, scenario: str,
                   normal: int, normal_rate: float, pressure: int = 0, pressure_rate: float = 0,
                   stop_event: Optional[threading.Event] = None, session_settings: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        workload = self._new_workload()
        self._workload = workload
        specs = [WorkloadSpec("control", "normal", normal, normal_rate, session_settings or {})]
        if pressure:
            specs.append(WorkloadSpec("external", scenario, pressure, pressure_rate, session_settings or {}))
        workload.start(specs)
        started = time.monotonic()
        samples = []
        try:
            while time.monotonic() - started < duration:
                if stop_event is not None and stop_event.is_set():
                    break
                elapsed = time.monotonic() - started
                self._sample(run_id, phase, elapsed, workload)
                time.sleep(1.0)
        finally:
            workload.stop()
            self._workload = None
        elapsed = max(0.0, time.monotonic() - started)
        samples = self.store.list_lab_samples(run_id, 5000)
        result = {"phase": phase, "duration_seconds": round(elapsed if stop_event and stop_event.is_set() else duration, 3),
                  "stopped": bool(stop_event and stop_event.is_set()),
                  "business_tps_summary": self._summary(samples, phase, "business", elapsed),
                  "pressure_tps_summary": self._summary(samples, phase, "pressure", elapsed),
                  "pressure_target_tps": pressure_rate or None,
                  "workers": []}
        return result

    def _load_tuning(self, scenario_id: str) -> Dict[str, Any]:
        path = Path(__file__).resolve().parent / "mysql_candidates" / (scenario_id + ".json")
        if not path.is_file():
            return {"status": "unavailable", "reason": "no MySQL candidate file for {}".format(scenario_id)}
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {"status": "unavailable", "reason": str(exc)}
        return {"status": "candidate_loaded", "source": str(path),
                "requested_configuration": config,
                "applied_configuration": config}

    def _run_tpcc(self, run_id: str, case: Mapping[str, Any], config: Mapping[str, Any], stop_event: threading.Event) -> None:
        phases: Dict[str, Any] = {}
        tuning: Dict[str, Any] = {}
        try:
            self.store.update_lab_run(run_id, status="running", phase="baseline")
            if not config.get("skip_baseline"):
                phases["baseline"] = self._run_phase(
                    run_id, "baseline", int(config["baseline_seconds"]), "normal",
                    int(config["normal_clients"]), float(config["normal_target_tps"]), stop_event=stop_event)
                if stop_event.is_set():
                    raise RuntimeError("operator requested stop")
            phases["pressure_before"] = self._run_phase(
                run_id, "pressure_before", int(config["pressure_before_seconds"]), case["scenario"],
                int(config["normal_clients"]), float(config["normal_target_tps"]),
                int(config["pressure_clients"]), float(config["pressure_target_tps"]), stop_event=stop_event)
            if stop_event.is_set():
                raise RuntimeError("operator requested stop")
            tuning = self._load_tuning(str(case["scenario_id"]))
            if tuning.get("status") == "candidate_loaded" and tuning.get("applied_configuration"):
                applier = MySQLTemporaryConfiguration(
                    self.bridge.args.host, self.bridge.args.port, self.bridge.args.db_user,
                    self._password, self.bridge.args.db, tuning["applied_configuration"],
                    label="mysql-lab-{}".format(run_id))
                tuning["status"] = "applying"
                try:
                    with applier as applied:
                        tuning["application"] = applied
                        tuning["status"] = "applied"
                        session_settings = dict(applied.get("session_configuration") or {})
                        phases["pressure_after"] = self._run_phase(
                            run_id, "pressure_after", int(config["pressure_after_seconds"]), case["scenario"],
                            int(config["normal_clients"]), float(config["normal_target_tps"]),
                            int(config["pressure_clients"]), float(config["pressure_target_tps"]),
                            stop_event=stop_event, session_settings=session_settings)
                    tuning["application"] = applier.state
                    tuning["status"] = applier.state.get("status", "applied_and_restored")
                except Exception as exc:
                    tuning["application"] = getattr(applier, "state", {})
                    tuning["status"] = "apply_failed"
                    tuning["reason"] = str(exc)
                    raise
            else:
                phases["pressure_after"] = self._run_phase(
                    run_id, "pressure_after", int(config["pressure_after_seconds"]), case["scenario"],
                    int(config["normal_clients"]), float(config["normal_target_tps"]),
                    int(config["pressure_clients"]), float(config["pressure_target_tps"]), stop_event=stop_event)
            if stop_event.is_set():
                raise RuntimeError("operator requested stop")
            comparison = _tpcc_comparison(phases) if _tpcc_comparison else {}
            comparison["tuning_status"] = tuning.get("status")
            comparison["tuning_source"] = tuning.get("source")
            result = {"scenario": dict(case), "config": dict(config), "phases": phases,
                      "tuning": tuning, "comparison": comparison, "completed_at": utc_now(),
                      "artifact_directory": str(self.root / run_id)}
            _write_json(self.root / run_id / "result.json", result)
            self.store.update_lab_run(run_id, status="completed", phase="completed", result_patch=result)
        except Exception as exc:
            result = {"scenario": dict(case), "config": dict(config), "phases": phases,
                      "tuning": tuning,
                      "comparison": _tpcc_comparison(phases) if _tpcc_comparison else {},
                      "stopped_at": utc_now(), "error": "{}: {}".format(type(exc).__name__, exc),
                      "traceback": traceback.format_exc() if not isinstance(exc, RuntimeError) else None}
            self.store.update_lab_run(run_id, status="stopped" if str(exc) == "operator requested stop" else "failed",
                                      phase="stopped", error=str(exc), result_patch=result)
        finally:
            with self._lock:
                self._active.pop("sysinsight", None)
