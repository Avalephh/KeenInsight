#!/usr/bin/env python3
"""Run the source-aware SysInsight chain for one live alert incident.

This adapter is intentionally called by the long-running bridge after an
alert has fired.  It keeps the online path auditable and bounded:

    active backend PIDs -> perf -> original source detector -> original
    LLAMBO/API acquisition -> live GUC validation -> real SQL replay under a
    temporary candidate -> automatic restore.

The candidate is never left in PostgreSQL by this command.  A permanent
production policy still needs a separate approval/publish mechanism; the
online demonstration proves the complete detect/analyse/apply/retest/restore
chain against the same database.
"""

from __future__ import annotations

import argparse
import ast
import copy
import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parent
DEFAULT_API_BASE = "http://35.212.195.134:28317/v1"
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_POSTGRES_SOURCE = Path("/root/keeninsight-postgres/third_party/postgresql-12.22")
DEFAULT_SYSINSIGHT_SOURCE = ROOT.parent / "repositories/Avalephh-KeenInsight/branch-sources/WorkloadTune"
DEFAULT_VALIDATION_SQL = "SELECT count(*) FROM tpcds.store_sales"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def _load_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON object required: {}".format(path))
    return value


def _stage(stages: List[Dict[str, Any]], name: str, status: str, detail: Any = None) -> None:
    item: Dict[str, Any] = {"name": name, "status": status}
    if detail is not None:
        item["detail"] = detail
    stages.append(item)


def _counter_rate(prometheus: Mapping[str, Any], query_name: str, lookback_seconds: float = 90.0) -> Optional[float]:
    """Return a recent rate from one postgres_exporter counter series."""

    queries = prometheus.get("queries", {})
    record = queries.get(query_name, {}) if isinstance(queries, Mapping) else {}
    series = record.get("series", []) if isinstance(record, Mapping) else []
    if not isinstance(series, list) or not series:
        return None
    values = series[0].get("values", []) if isinstance(series[0], Mapping) else []
    if not isinstance(values, list) or len(values) < 2:
        return None
    try:
        end = values[-1]
        end_ts = float(end["timestamp"])
        end_value = float(end["value"])
        start = values[0]
        for candidate in reversed(values[:-1]):
            candidate_ts = float(candidate["timestamp"])
            if end_ts - candidate_ts >= lookback_seconds:
                start = candidate
                break
        elapsed = end_ts - float(start["timestamp"])
        delta = end_value - float(start["value"])
    except (KeyError, TypeError, ValueError):
        return None
    if elapsed <= 0 or delta < 0:
        return None
    return delta / elapsed


def _live_transaction_metrics(prometheus: Mapping[str, Any]) -> Dict[str, Any]:
    """Give the original LLM the required measured TPS field for live alerts.

    A generic online alert does not have a benchmark controller's phase score.
    Use the real PostgreSQL transaction counters collected by Prometheus and
    label the metric explicitly, rather than inventing a benchmark TPS value.
    """

    committed = _counter_rate(prometheus, "transactions_committed")
    rolled_back = _counter_rate(prometheus, "transactions_rolled_back")
    components = {
        "committed_tps": round(committed, 6) if committed is not None else None,
        "rolled_back_tps": round(rolled_back, 6) if rolled_back is not None else None,
    }
    values = [value for value in (committed, rolled_back) if value is not None]
    total = sum(values) if values else 0.0
    return {
        "tps": round(total, 6),
        "metric": "postgres_transactions_per_second",
        "source": "Prometheus pg_stat_database_xact_commit + pg_stat_database_xact_rollback counter rate",
        "components": components,
        "measured": bool(values),
    }


def _source_environment() -> Dict[str, str]:
    """Resolve the checked-out original source without exposing credentials."""

    env = os.environ.copy()
    if not env.get("SYSINSIGHT_SOURCE_ROOT"):
        candidates = [
            DEFAULT_SYSINSIGHT_SOURCE,
            ROOT.parent / "repositories/Avalephh-KeenInsight/branch-sources/WorkloadTune_new/sysinsight",
        ]
        for candidate in candidates:
            if (candidate / "DBTuner" / "utils" / "analyzeException.py").is_file():
                env["SYSINSIGHT_SOURCE_ROOT"] = str(candidate)
                break
    if not env.get("POSTGRES_SOURCE_ROOT") and (DEFAULT_POSTGRES_SOURCE / "src" / "backend").is_dir():
        env["POSTGRES_SOURCE_ROOT"] = str(DEFAULT_POSTGRES_SOURCE)
    return env


def _read_pids(path: Path) -> List[int]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("active PID list must be an array")
    result: List[int] = []
    for item in value:
        try:
            pid = int(item)
        except (TypeError, ValueError):
            continue
        if pid > 1 and pid not in result:
            result.append(pid)
    return result


def _capture_perf(
    result_dir: Path,
    pids: Sequence[int],
    seconds: float,
    frequency: int,
    stackcollapse: str,
    env: Mapping[str, str],
) -> Dict[str, Any]:
    """Capture and post-process a real PostgreSQL perf window."""

    result_dir.mkdir(parents=True, exist_ok=True)
    info: Dict[str, Any] = {
        "phase": "anomaly",
        "requested_at": utc_now(),
        "perf_path": shutil.which("perf"),
        "pids": list(pids),
        "frequency": int(frequency),
        "duration_seconds": float(seconds),
    }
    if not info["perf_path"]:
        info.update({"status": "unavailable", "reason": "perf executable was not found"})
        return {"perf": info, "postprocess": {"status": "unavailable", "reason": info["reason"]}}
    if not pids:
        info.update({"status": "no_active_backend", "reason": "the alert had no active PostgreSQL client backend to sample"})
        return {"perf": info, "postprocess": {"status": "no_active_backend", "reason": info["reason"]}}

    data_path = result_dir / "anomaly.perf.data"
    perf_log = result_dir / "anomaly_perf.log"
    command = [
        str(info["perf_path"]),
        "record",
        "-F",
        str(int(frequency)),
        "-g",
        "-p",
        ",".join(str(pid) for pid in pids),
        "-o",
        str(data_path),
        "--",
        "sleep",
        str(max(1, int(round(seconds)))),
    ]
    started = time.monotonic()
    with perf_log.open("w", encoding="utf-8") as handle:
        completed = subprocess.run(
            command,
            cwd="/",
            env=dict(env),
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=max(30.0, float(seconds) + 20.0),
            check=False,
        )
    has_data = data_path.is_file() and data_path.stat().st_size > 0
    info.update({
        "status": "completed" if has_data else "failed",
        "returncode": completed.returncode,
        "command": command,
        "data_path": str(data_path),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "finished_at": utc_now(),
    })
    if not has_data:
        info["reason"] = "perf did not produce a data file"
        return {"perf": info, "postprocess": {"status": "raw_data_missing", "data_path": str(data_path)}}

    script_path = result_dir / "anomaly.perf.script"
    with script_path.open("w", encoding="utf-8") as handle:
        script = subprocess.run(
            [str(info["perf_path"]), "script", "-i", str(data_path)],
            cwd="/",
            env=dict(env),
            stdout=handle,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    postprocess: Dict[str, Any] = {
        "status": "script_generated" if script.returncode == 0 else "script_failed",
        "script_path": str(script_path),
        "script_returncode": script.returncode,
    }
    if script.returncode != 0:
        postprocess["stderr"] = script.stderr[-2000:]
        return {"perf": info, "postprocess": postprocess}

    collapse = stackcollapse or str(ROOT / "vendor" / "FlameGraph" / "stackcollapse-perf.pl")
    if not Path(collapse).is_file():
        postprocess.update({
            "status": "script_generated_no_stackcollapse",
            "reason": "FlameGraph/stackcollapse-perf.pl is unavailable",
        })
        return {"perf": info, "postprocess": postprocess}
    folded_path = result_dir / "anomaly.folded"
    with script_path.open("r", encoding="utf-8", errors="replace") as source, folded_path.open("w", encoding="utf-8") as output:
        collapsed = subprocess.run(
            ["perl", collapse],
            cwd="/",
            env=dict(env),
            stdin=source,
            stdout=output,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    if collapsed.returncode != 0:
        postprocess.update({"status": "stackcollapse_failed", "stderr": collapsed.stderr[-2000:]})
        return {"perf": info, "postprocess": postprocess}

    source_root = Path(str(env.get("SYSINSIGHT_SOURCE_ROOT", "")))
    dbenv_path = source_root / "DBTuner" / "dbenv.py"
    counts_path: Optional[Path] = None
    if dbenv_path.is_file():
        try:
            tree = ast.parse(dbenv_path.read_text(encoding="utf-8"), filename=str(dbenv_path))
            db_env = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "DBEnv")
            method = next(
                node for node in db_env.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "get_perf_function_range"
            )
            module = ast.Module(body=[method], type_ignores=[])
            ast.fix_missing_locations(module)
            namespace = {"os": os}
            exec(compile(module, str(dbenv_path), "exec"), namespace)
            counts_value = namespace["get_perf_function_range"](None, None, 2, str(folded_path))
            if counts_value:
                counts_path = Path(str(counts_value))
        except Exception as exc:
            postprocess.update({"status": "sysinsight_function_range_failed", "error": str(exc)})
            return {"perf": info, "postprocess": postprocess}
    if counts_path is None or not counts_path.is_file():
        postprocess.update({
            "status": "sysinsight_function_range_failed",
            "source": str(dbenv_path),
        })
        return {"perf": info, "postprocess": postprocess}
    function_count = max(0, len(counts_path.read_text(encoding="utf-8", errors="replace").splitlines()) - 1)
    postprocess.update({
        "status": "sysinsight_counts_generated",
        "folded_path": str(folded_path),
        "counts_path": str(counts_path),
        "function_count": function_count,
    })
    return {"perf": info, "postprocess": postprocess}


def _run_source_detection(
    case_path: Path,
    anomaly_dir: Path,
    normal_profile: Path,
    dbms: str,
    pg_version: str,
    env: Mapping[str, str],
) -> Dict[str, Any]:
    log_path = anomaly_dir / "sysinsight_detection.log"
    command = [
        sys.executable,
        str(ROOT / "sysinsight_detection.py"),
        "--run-dir",
        str(anomaly_dir),
        "--dbms",
        dbms,
        "--db-version",
        pg_version,
        "--normal-profile",
        str(normal_profile),
    ]
    completed = subprocess.run(
        command,
        cwd=str(ROOT),
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    log_path.write_text(completed.stdout, encoding="utf-8")
    result_path = anomaly_dir / "sysinsight_source_detection_result.json"
    if completed.returncode != 0 or not result_path.is_file():
        raise RuntimeError("original SysInsight source detection failed ({}): {}".format(completed.returncode, completed.stdout[-3000:]))
    result = _load_json(result_path)
    result["runner_returncode"] = completed.returncode
    result["result_path"] = str(result_path)
    return result


def _api_result(
    case_path: Path,
    input_path: Path,
    output_dir: Path,
    args: argparse.Namespace,
    env: Mapping[str, str],
) -> Dict[str, Any]:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(ROOT / "sysinsight_original_llm.py"),
        "--case-result",
        str(case_path),
        "--sysinsight-input",
        str(input_path),
        "--dbms",
        args.dbms,
        "--db-version",
        args.pg_version,
        "--api-base",
        args.api_base,
        "--model",
        args.model,
        "--n-candidates",
        str(args.candidate_count),
        "--n-templates",
        "1",
        "--selector-n-gens",
        "1",
        "--run-source-selector",
        "--sync-transport",
        "--output",
        str(output_dir),
    ]
    completed = subprocess.run(
        command,
        cwd=str(ROOT),
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=float(args.api_timeout),
        check=False,
    )
    (output_dir / "wrapper_stdout.log").parent.mkdir(parents=True, exist_ok=True)
    (output_dir / "wrapper_stdout.log").write_text(completed.stdout, encoding="utf-8")
    result_path = output_dir / "result.json"
    if completed.returncode != 0 or not result_path.is_file():
        raise RuntimeError("SysInsight LLM acquisition failed ({}): {}".format(completed.returncode, completed.stdout[-4000:]))
    result = _load_json(result_path)
    result["wrapper_returncode"] = completed.returncode
    result["artifact"] = str(result_path)
    return result


def _is_read_only_sql(sql: str) -> bool:
    text = str(sql or "").strip().lower()
    if not text or "$" in text or "?" in text or ";" in text.rstrip(";"):
        return False
    return text.startswith(("select", "with", "values", "explain")) and not any(
        token in text[:300]
        for token in ("insert ", "update ", "delete ", "merge ", "create ", "drop ", "alter ", "truncate ")
    )


def _sql_from_case(case: Mapping[str, Any], explicit: str) -> Tuple[str, str]:
    candidates: List[Tuple[str, Any]] = []
    for row in case.get("slow_queries", []) if isinstance(case.get("slow_queries"), list) else []:
        if isinstance(row, Mapping):
            candidates.extend([("completed_slow_sql.replay_sql", row.get("replay_sql")), ("completed_slow_sql.query", row.get("query"))])
    for row in case.get("active_queries", []) if isinstance(case.get("active_queries"), list) else []:
        if isinstance(row, Mapping):
            candidates.append(("active_sql.query", row.get("query")))
    if explicit:
        candidates.append(("configured_validation_sql", explicit))
    candidates.append(("safe_tpcds_canary", DEFAULT_VALIDATION_SQL))
    candidates.append(("safe_catalog_canary", "SELECT count(*) FROM pg_catalog.pg_class"))
    for source, value in candidates:
        text = str(value or "").strip()
        if _is_read_only_sql(text):
            return text.rstrip(";"), source
    raise RuntimeError("no replayable read-only SQL was available for live SysInsight validation")


def _db_args(args: argparse.Namespace) -> SimpleNamespace:
    return SimpleNamespace(
        db=args.db,
        db_user=args.db_user,
        run_as=args.run_as,
        host=args.host,
        port=args.port,
        pg_version=args.pg_version,
        pg_cluster=args.pg_cluster,
    )


def _measure_sql(
    db: Any,
    sql: str,
    args: argparse.Namespace,
    phase: str,
    session_settings: Optional[Mapping[str, Any]] = None,
    startup_settings: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    results: List[Dict[str, Any]] = []
    search_path = [args.db_schema, "public"] if args.db_schema and args.db_schema != "public" else ["public"]
    for index in range(max(1, int(args.measure_repeats))):
        result = db.execute_timed(
            sql,
            application_name="sysinsight-live-{}-{}".format(phase, index),
            search_path=search_path,
            session_settings=session_settings,
            startup_settings=startup_settings,
            timeout=float(args.sql_timeout),
        )
        result["attempt"] = index + 1
        results.append(result)
    successful = [float(item["duration_ms"]) for item in results if item.get("status") == "completed" and isinstance(item.get("duration_ms"), (int, float))]
    median = sorted(successful)[len(successful) // 2] if successful else None
    if successful and len(successful) % 2 == 0:
        median = (sorted(successful)[len(successful) // 2 - 1] + sorted(successful)[len(successful) // 2]) / 2.0
    return {
        "status": "completed" if successful else "failed",
        "attempts": results,
        "successful_count": len(successful),
        "median_duration_ms": median,
        "mean_duration_ms": sum(successful) / len(successful) if successful else None,
    }


def _candidate_and_validation(api: Mapping[str, Any], args: argparse.Namespace, db_args: Any) -> Dict[str, Any]:
    sys.path.insert(0, str(ROOT))
    from db_profile import resolve_profile  # type: ignore
    from sysinsight_candidate_benchmark import (  # type: ignore
        candidate_configurations,
        selected_candidate_index,
        validate_candidate,
        validate_candidate_live,
    )

    profile = resolve_profile(args.dbms, args.pg_version)
    constraints = profile.constraints()
    candidates = candidate_configurations(api, max_candidates=max(1, int(args.candidate_count)))
    records: List[Dict[str, Any]] = []
    for candidate in candidates:
        structural = validate_candidate(candidate.get("configuration", {}), constraints)
        live = validate_candidate_live(db_args, structural.get("configuration", {})) if structural.get("valid") else {"status": "invalid", "reason": "structural validation failed"}
        records.append({**candidate, "validation": structural, "live_validation": live})
    selected_index = selected_candidate_index(api, len(candidates))
    selected: Optional[Dict[str, Any]] = None
    if selected_index is not None:
        for item in records:
            if item.get("candidate_id") == "candidate-{:03d}".format(selected_index) and item.get("validation", {}).get("valid") and item.get("live_validation", {}).get("status") == "completed":
                selected = item
                break
    if selected is None:
        selected = next(
            (item for item in records if item.get("validation", {}).get("valid") and item.get("live_validation", {}).get("status") == "completed"),
            None,
        )
    return {
        "candidate_count": len(records),
        "candidates": records,
        "selected_candidate": selected,
        "selector_index": selected_index,
    }


def _normal_profile(configured: str) -> Path:
    if configured:
        path = Path(configured).resolve()
        if path.is_file():
            return path
        raise FileNotFoundError("configured SysInsight normal profile not found: {}".format(path))
    candidates = [
        path for path in (ROOT / "results").rglob("normal_profile_postgresql_demo.csv")
        if "/baseline/" in str(path)
    ]
    if not candidates:
        candidates = list((ROOT / "results").rglob("normal_profile_postgresql_demo.csv"))
    if not candidates:
        raise FileNotFoundError("no normal_profile_postgresql_demo.csv is available")
    return max(candidates, key=lambda path: path.stat().st_mtime).resolve()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-result", required=True)
    parser.add_argument("--prometheus-capture", required=True)
    parser.add_argument("--active-pids", required=True)
    parser.add_argument("--incident-dir", required=True)
    parser.add_argument("--db", default="keeninsight")
    parser.add_argument("--db-user", default="postgres")
    parser.add_argument("--run-as", default="postgres")
    parser.add_argument("--host", default="/var/run/postgresql")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--db-schema", default="tpcds")
    parser.add_argument("--dbms", default="postgresql")
    parser.add_argument("--pg-version", default="12")
    parser.add_argument("--pg-cluster", default="main")
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:9090")
    parser.add_argument("--alert-name", default="SysInsightDemoAnomaly")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-timeout", type=float, default=1200.0)
    parser.add_argument("--perf-seconds", type=float, default=15.0)
    parser.add_argument("--perf-frequency", type=int, default=300)
    parser.add_argument("--stackcollapse", default="")
    parser.add_argument("--normal-profile", default="")
    parser.add_argument("--candidate-count", type=int, default=1)
    parser.add_argument("--validation-sql", default="")
    parser.add_argument("--measure-repeats", type=int, default=2)
    parser.add_argument("--sql-timeout", type=float, default=90.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    incident_dir = Path(args.incident_dir).resolve()
    case_path = Path(args.case_result).resolve()
    prometheus_path = Path(args.prometheus_capture).resolve()
    active_pids_path = Path(args.active_pids).resolve()
    anomaly_dir = incident_dir / "anomaly"
    anomaly_dir.mkdir(parents=True, exist_ok=True)
    stages: List[Dict[str, Any]] = []
    result: Dict[str, Any] = {
        "schema": "sysinsight.postgresql.live-chain.v1",
        "status": "running",
        "started_at": utc_now(),
        "incident_dir": str(incident_dir),
        "stages": stages,
    }
    result_path = incident_dir / "live_chain_result.json"
    env = _source_environment()
    try:
        case = _load_json(case_path)
        prometheus = _load_json(prometheus_path)
        pids = _read_pids(active_pids_path)
        result["active_pids"] = pids
        transaction_metrics = _live_transaction_metrics(prometheus)
        case["baseline_metrics"] = transaction_metrics
        case.setdefault("anomaly", {})["metrics"] = {
            **transaction_metrics,
            "active_backend_count": len(pids),
        }
        case["metric_deltas"] = {"tps": 0.0, "source": transaction_metrics.get("source")}
        _write_json(case_path, case)
        _stage(stages, "alert_handoff", "completed", {"case_result": str(case_path), "active_pid_count": len(pids)})

        perf_capture = _capture_perf(
            anomaly_dir,
            pids,
            args.perf_seconds,
            args.perf_frequency,
            args.stackcollapse,
            env,
        )
        case.setdefault("anomaly", {})["perf"] = perf_capture.get("perf")
        case["anomaly"]["perf_postprocess"] = perf_capture.get("postprocess")
        _write_json(case_path, case)
        result["perf"] = perf_capture
        if perf_capture.get("postprocess", {}).get("status") != "sysinsight_counts_generated":
            raise RuntimeError("perf/source input was not generated: {}".format(perf_capture.get("postprocess")))
        _stage(stages, "perf_capture_and_function_range", "completed", perf_capture.get("postprocess"))

        normal_profile = _normal_profile(args.normal_profile)
        detection = _run_source_detection(case_path, anomaly_dir, normal_profile, args.dbms, args.pg_version, env)
        case["sysinsight_source_detection"] = detection
        case.setdefault("anomaly", {})["sysinsight_source_detection"] = detection
        _write_json(case_path, case)
        result["source_detection"] = detection
        _stage(stages, "original_source_detection_and_matching", "completed", {
            "function_count": detection.get("source_compare", {}).get("key_function_count"),
            "matched_knob_count": len(detection.get("source_match", {}).get("matched_knob", []) or []),
        })

        sys.path.insert(0, str(ROOT))
        from db_profile import profile_summary, resolve_profile  # type: ignore
        from sysinsight_prometheus import build_sysinsight_input  # type: ignore

        profile = profile_summary(resolve_profile(args.dbms, args.pg_version))
        input_payload = build_sysinsight_input(
            case,
            prometheus,
            profile,
            case_path,
            database_name=args.db,
            database_schema=args.db_schema,
        )
        input_payload["live_chain"] = {
            "status": "source_evidence_ready",
            "active_pid_count": len(pids),
            "normal_profile": str(normal_profile),
        }
        input_path = incident_dir / "sysinsight_input.json"
        _write_json(input_path, input_payload)
        result["sysinsight_input"] = str(input_path)
        result["source_evidence"] = input_payload.get("source_evidence", {})
        if input_payload.get("source_evidence", {}).get("status") != "completed":
            raise RuntimeError("source evidence is unavailable: {}".format(input_payload.get("source_evidence")))
        _stage(stages, "canonical_input_and_source_evidence", "completed", {
            "runtime_call_chains": len(input_payload.get("source_evidence", {}).get("runtime_call_chains", [])),
            "static_parameter_evidence": len(input_payload.get("source_evidence", {}).get("static_parameter_evidence", [])),
        })

        api = _api_result(case_path, input_path, incident_dir / "sysinsight_api", args, env)
        result["api"] = {
            "status": "completed",
            "model": api.get("api", {}).get("resolved_model") or api.get("api", {}).get("requested_model"),
            "artifact": api.get("artifact"),
            "llm_call_count": len(api.get("llm_calls", []) if isinstance(api.get("llm_calls"), list) else []),
            "candidate_count": len(api.get("api_generated_configurations", []) if isinstance(api.get("api_generated_configurations"), list) else []),
            "source_selector": api.get("source_selector"),
        }
        _stage(stages, "llm_candidate_generation", "completed", result["api"])

        db_args = _db_args(args)
        selected = _candidate_and_validation(api, args, db_args)
        _write_json(incident_dir / "candidate_validation.json", selected)
        result["candidate_validation"] = selected
        candidate = selected.get("selected_candidate")
        if not isinstance(candidate, dict):
            raise RuntimeError("no API-generated candidate passed structural and live pg_settings validation")
        _stage(stages, "candidate_validation", "completed", {
            "candidate_id": candidate.get("candidate_id"),
            "valid_count": sum(1 for item in selected.get("candidates", []) if item.get("validation", {}).get("valid") and item.get("live_validation", {}).get("status") == "completed"),
        })

        from pg_temporary_config import TemporaryPostgresConfiguration  # type: ignore
        from sysinsight_dream_bridge import DatabaseClient  # type: ignore

        sql, sql_source = _sql_from_case(case, args.validation_sql)
        result["validation_sql"] = {"source": sql_source, "sql": sql}
        db = DatabaseClient(args.db, args.db_user, args.host, args.port, args.run_as, timeout=max(30.0, args.sql_timeout))
        baseline = _measure_sql(db, sql, args, "baseline")
        if baseline.get("status") != "completed":
            raise RuntimeError("baseline validation SQL failed: {}".format(baseline))
        _stage(stages, "baseline_replay", "completed", baseline)

        apply_state: Dict[str, Any] = {}
        tuned: Dict[str, Any] = {}
        configuration = candidate.get("validation", {}).get("configuration", {})
        applier = TemporaryPostgresConfiguration(db_args, dict(configuration), "sysinsight-live", allow_restart=False)
        try:
            with applier as applied:
                tuned = _measure_sql(
                    db,
                    sql,
                    args,
                    "tuned",
                    session_settings=dict(applied.get("session_configuration", {})),
                    startup_settings=dict(applied.get("connection_configuration", {})),
                )
            # __exit__ records the restore outcome in the same state object;
            # copy it only after leaving the context so the artifact proves
            # the candidate was actually removed.
            apply_state = copy.deepcopy(applier.state)
        except Exception:
            apply_state = copy.deepcopy(getattr(applier, "state", {}))
            raise
        if apply_state.get("status") not in {"applied_and_restored", "session_only_applied_and_ended"}:
            raise RuntimeError(
                "temporary candidate was not restored cleanly: {}".format(apply_state.get("status"))
            )
        result["temporary_application"] = apply_state
        result["tuned_replay"] = tuned
        _stage(stages, "temporary_candidate_apply_and_tuned_replay", "completed" if tuned.get("status") == "completed" else "failed", {
            "status": apply_state.get("status"),
            "tuned": tuned,
        })

        restored = _measure_sql(db, sql, args, "restored")
        result["restored_replay"] = restored
        if restored.get("status") != "completed":
            raise RuntimeError("post-restore validation SQL failed: {}".format(restored))
        _stage(stages, "restore_and_post_restore_replay", "completed", {
            "application_status": apply_state.get("status"),
            "restore": apply_state.get("restore"),
            "replay": restored,
        })

        baseline_ms = baseline.get("median_duration_ms")
        tuned_ms = tuned.get("median_duration_ms")
        restored_ms = restored.get("median_duration_ms")
        result["comparison"] = {
            "metric": "median_read_only_sql_duration_ms",
            "baseline_ms": baseline_ms,
            "tuned_ms": tuned_ms,
            "restored_ms": restored_ms,
            "tuned_improvement_ratio": ((baseline_ms - tuned_ms) / baseline_ms) if isinstance(baseline_ms, (int, float)) and isinstance(tuned_ms, (int, float)) and baseline_ms > 0 else None,
            "restored_within_20pct_of_baseline": bool(isinstance(baseline_ms, (int, float)) and isinstance(restored_ms, (int, float)) and restored_ms <= baseline_ms * 1.20),
            "configuration_status": apply_state.get("status"),
        }
        if tuned.get("status") != "completed":
            raise RuntimeError(
                "candidate application completed and was restored, but tuned replay failed: {}".format(
                    tuned.get("attempts")
                )
            )
        result["status"] = "completed"
        result["finished_at"] = utc_now()
        _stage(stages, "result_persistence", "completed", str(result_path))
        _write_json(result_path, result)
        return 0
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = "{}: {}".format(type(exc).__name__, exc)
        result["traceback"] = traceback.format_exc()
        result["finished_at"] = utc_now()
        _stage(stages, "result_persistence", "completed", str(result_path))
        _write_json(result_path, result)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
