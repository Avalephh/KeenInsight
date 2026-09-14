#!/usr/bin/env python3
"""Candidate handling and real PostgreSQL benchmark helpers for SysInsight.

The original SysInsight source produces a full candidate configuration, while
the existing demo previously only tested one selected API configuration.  This
module turns every parsed acquisition response into an auditable candidate and
can evaluate it with the same TPCC control/external workload.  It deliberately
delegates configuration lifecycle handling to ``pg_temporary_config``; this
file does not implement another snapshot, apply, or restore mechanism.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tpcc_external_cases as legacy_runner  # noqa: E402
from pg_temporary_config import (  # noqa: E402
    TemporaryPostgresConfiguration,
    connection_args_for_configuration,
    settings_details,
)


_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return value


def _stable_config(config: Mapping[str, Any]) -> str:
    return json.dumps(jsonable(dict(config)), sort_keys=True, ensure_ascii=False, default=str)


def source_api_configurations(api_result: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Extract only configurations parsed by the original LLM_ACQ parser."""

    result: List[Dict[str, Any]] = []
    for item in api_result.get("api_generated_configurations", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("phase") != "acquisition" or item.get("parse_status") != "parsed":
            continue
        configuration = item.get("parsed_by_source_LLM_ACQ__convert_to_json")
        if isinstance(configuration, dict) and configuration:
            result.append(
                {
                    "source": "original_LLM_ACQ_parser",
                    "call_index": item.get("call_index"),
                    "choice_index": item.get("choice_index"),
                    "raw_content": item.get("raw_content"),
                    "configuration_section": item.get("configuration_section"),
                    "configuration": configuration,
                }
            )
    return result


def _materialized_configurations(api_result: Mapping[str, Any]) -> List[Dict[str, Any]]:
    values = api_result.get("source_materialized_candidate_points", [])
    if not isinstance(values, list):
        return []
    result: List[Dict[str, Any]] = []
    for index, value in enumerate(values):
        if isinstance(value, dict) and value:
            result.append(
                {
                    "source": "original_candidate_dataframe",
                    "row_index": index,
                    "configuration": value,
                }
            )
    return result


def candidate_configurations(
    api_result: Mapping[str, Any], max_candidates: int = 0
) -> List[Dict[str, Any]]:
    """Return de-duplicated source candidates in acquisition order.

    The API-parsed responses are preferred because they contain only values
    explicitly returned by the model.  The materialized DataFrame is used only
    as a fallback for old artifacts that predate the raw-response trace.
    """

    values = source_api_configurations(api_result)
    if not values:
        values = _materialized_configurations(api_result)
    result: List[Dict[str, Any]] = []
    seen = set()
    for index, item in enumerate(values):
        configuration = item.get("configuration")
        if not isinstance(configuration, dict) or not configuration:
            continue
        key = _stable_config(configuration)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            {
                "candidate_id": "candidate-{:03d}".format(index),
                "source": item.get("source"),
                "call_index": item.get("call_index"),
                "choice_index": item.get("choice_index"),
                "row_index": item.get("row_index"),
                "configuration": configuration,
            }
        )
        if max_candidates > 0 and len(result) >= max_candidates:
            break
    return result


def selected_candidate_index(api_result: Mapping[str, Any], count: int) -> Optional[int]:
    """Resolve the source selector's row when it maps to an acquisition row."""

    selector = api_result.get("source_selector") or {}
    value = selector.get("selected_row_index") if isinstance(selector, dict) else None
    if isinstance(value, int) and 0 <= value < count:
        return value
    return None


def _finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _constraint_error(name: str, value: Any, constraint: Any) -> Optional[str]:
    if not isinstance(constraint, list) or len(constraint) < 3:
        return None
    kind = str(constraint[0])
    domain = constraint[2]
    if kind in {"int", "integer"}:
        if isinstance(value, bool) or not _finite_number(value) or float(value) != int(float(value)):
            return "{} requires an integer".format(name)
        if isinstance(domain, list) and len(domain) == 2:
            if float(value) < float(domain[0]) or float(value) > float(domain[1]):
                return "{}={} is outside [{}, {}]".format(name, value, domain[0], domain[1])
    elif kind in {"float", "real", "double"}:
        if not _finite_number(value):
            return "{} requires a finite number".format(name)
        if isinstance(domain, list) and len(domain) == 2:
            if float(value) < float(domain[0]) or float(value) > float(domain[1]):
                return "{}={} is outside [{}, {}]".format(name, value, domain[0], domain[1])
    elif kind in {"bool", "boolean"}:
        if str(value).lower() not in {"on", "off", "true", "false", "1", "0"}:
            return "{} requires a boolean".format(name)
    elif kind == "enum" and isinstance(domain, list):
        if str(value) not in {str(item) for item in domain}:
            return "{}={} is not in the enum domain".format(name, value)
    return None


def validate_candidate(
    configuration: Mapping[str, Any], constraints: Optional[Mapping[str, Any]] = None
) -> Dict[str, Any]:
    """Validate the same names/domains used by the PostgreSQL profile.

    Live ``pg_settings`` validation is still performed by the existing
    temporary configuration context.  This preflight catches malformed model
    output before a database operation is attempted.
    """

    errors: List[Dict[str, Any]] = []
    normalized: Dict[str, Any] = {}
    constraints = constraints or {}
    for raw_name, raw_value in configuration.items():
        name = str(raw_name)
        if not _NAME.fullmatch(name):
            errors.append({"name": name, "error": "unsafe or unsupported PostgreSQL GUC name"})
            continue
        value: Any = raw_value
        constraint = constraints.get(name)
        error = _constraint_error(name, value, constraint)
        if error:
            errors.append({"name": name, "value": value, "error": error})
            continue
        if isinstance(constraint, list) and constraint:
            kind = str(constraint[0])
            if kind in {"int", "integer"}:
                value = int(float(value))
            elif kind in {"float", "real", "double"}:
                value = float(value)
            elif kind in {"bool", "boolean"}:
                value = "on" if str(value).lower() in {"on", "true", "1"} else "off"
        normalized[name] = value
    return {
        "valid": not errors and bool(normalized),
        "configuration": normalized,
        "errors": errors,
        "changed_parameters": sorted(normalized),
    }


def validate_candidate_live(db_args: Any, configuration: Mapping[str, Any]) -> Dict[str, Any]:
    """Check candidate names against the live PostgreSQL ``pg_settings`` view.

    This is a read-only PostgreSQL operation.  The result records contexts so
    the later evaluator can use the existing session/connection/global
    routing in ``TemporaryPostgresConfiguration``.
    """

    try:
        details = settings_details(db_args, configuration.keys(), "pipeline-live-validation")
    except Exception as exc:
        return {
            "status": "unavailable",
            "error": "{}: {}".format(type(exc).__name__, exc),
            "settings": {},
            "missing": sorted(str(name) for name in configuration),
        }
    missing = sorted(set(str(name) for name in configuration) - set(details))
    supported_contexts = {"user", "superuser", "superuser-backend", "backend", "sighup", "postmaster"}
    unsupported_contexts = {
        name: detail.get("context")
        for name, detail in details.items()
        if str(detail.get("context") or "") not in supported_contexts
    }
    return {
        "status": "completed" if not missing and not unsupported_contexts else "invalid",
        "settings": details,
        "missing": missing,
        "unsupported_contexts": unsupported_contexts,
        "live_setting_count": len(details),
    }


def _runner_args(args: Any) -> Any:
    """Build the argument object expected by the existing TPCC runner."""

    return type("RunnerArgs", (), {
        "db": getattr(args, "db", "keeninsight"),
        "db_user": getattr(args, "db_user", "postgres"),
        "run_as": getattr(args, "run_as", "postgres"),
        "host": getattr(args, "host", "/var/run/postgresql"),
        "port": int(getattr(args, "port", 5432)),
        "prometheus_url": getattr(args, "prometheus_url", "http://127.0.0.1:9090"),
        "alert_name": getattr(args, "alert_name", "SysInsightDemoAnomaly"),
        "baseline_duration": int(getattr(args, "baseline_duration", 20)),
        "case_duration": int(getattr(args, "case_duration", 25)),
        "tuned_duration": int(getattr(args, "tuned_duration", 20)),
        "normal_clients": int(getattr(args, "normal_clients", 2)),
        "perf_frequency": int(getattr(args, "perf_frequency", 300)),
        "normal_sql": getattr(args, "normal_sql", "normal.sql"),
        "pg_version": getattr(args, "pg_version", "12"),
        "pg_cluster": getattr(args, "pg_cluster", "main"),
    })()


def _session_sql(case: Mapping[str, Any], session_configuration: Mapping[str, Any], stage_dir: Path) -> Path:
    source = legacy_runner.CASE_ROOT / str(case["sql"])
    destination = stage_dir / "candidate_{}.sql".format(source.name)
    statements: List[str] = []
    for name, value in session_configuration.items():
        if not _NAME.fullmatch(str(name)):
            raise ValueError("unsafe session GUC name: {}".format(name))
        text = str(value)
        if not re.fullmatch(r"[A-Za-z0-9_./:+%\-* ]+", text):
            raise ValueError("unsafe session GUC value for {}".format(name))
        statements.append('SET "{}" = \'{}\';'.format(name, text.replace("'", "''")))
    destination.write_text("\n".join(statements + [source.read_text(encoding="utf-8")]), encoding="utf-8")
    destination.chmod(0o644)
    return destination


def _control_tps(metrics: Mapping[str, Any]) -> Optional[float]:
    for worker in metrics.get("workers", []) or []:
        if "control" not in str(worker.get("app_name", "")):
            continue
        value = worker.get("metrics", {}).get("tps")
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _case_control_tps(case_result: Mapping[str, Any]) -> Optional[float]:
    anomaly = case_result.get("anomaly", {})
    if isinstance(anomaly, dict):
        metrics = anomaly.get("all_process_metrics", {})
        if isinstance(metrics, dict):
            return _control_tps(metrics)
    return None


def _safe_write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(jsonable(value), indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def benchmark_candidate(
    args: Any,
    case: Mapping[str, Any],
    case_result: Mapping[str, Any],
    candidate: Mapping[str, Any],
    output_dir: Path,
    constraints: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Run one candidate against the real TPCC control and external load."""

    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_id = str(candidate.get("candidate_id", "candidate"))
    raw_configuration = candidate.get("configuration", {})
    validation = validate_candidate(raw_configuration, constraints)
    result: Dict[str, Any] = {
        "candidate_id": candidate_id,
        "source": candidate.get("source"),
        "configuration": validation.get("configuration", {}),
        "validation": validation,
        "started_at": utc_now(),
        "status": "not_started",
        "apply": None,
        "benchmark": None,
        "comparison": None,
    }
    _safe_write(output_dir / "candidate.json", result)
    if not validation["valid"]:
        result["status"] = "invalid_candidate"
        _safe_write(output_dir / "candidate_result.json", result)
        return result

    runner_args = _runner_args(args)
    stage_dir = Path(tempfile.mkdtemp(prefix="sysinsight-candidate-sql-"))
    stage_dir.chmod(0o755)
    processes: List[Dict[str, Any]] = []
    apply_state: Dict[str, Any] = {}
    tuned_samples: Dict[str, Any] = {}
    tuned_all_metrics: Dict[str, Any] = {}
    tuned_metrics: Dict[str, Any] = {}
    plans: Dict[str, Any] = {}
    error: Optional[str] = None
    try:
        legacy_runner.wait_alert_clear(runner_args)
        applier = TemporaryPostgresConfiguration(
            runner_args, validation["configuration"], "sysinsight-{}".format(candidate_id)
        )
        with applier as applied:
            apply_state = applied
            tuned_args = connection_args_for_configuration(
                runner_args, applied.get("normalized_configuration", validation["configuration"])
            )
            normal_sql = legacy_runner.stage_sql(
                legacy_runner.CASE_ROOT / str(case.get("normal_sql", runner_args.normal_sql)), stage_dir
            )
            candidate_sql = _session_sql(case, applied.get("session_configuration", {}), stage_dir)
            prefix = "perf-anomaly-demo-sysinsight-{}-".format(candidate_id)
            control = legacy_runner.start_pgbench(
                tuned_args,
                normal_sql,
                output_dir,
                prefix + "control",
                int(getattr(args, "normal_clients", 2)),
                int(getattr(args, "tuned_duration", 20)),
                role="control",
                connection_config=applied.get("connection_configuration"),
            )
            processes = [control]
            time.sleep(0.5)
            if str(case.get("mode")) == "pgbench":
                processes.append(
                    legacy_runner.start_pgbench(
                        tuned_args,
                        candidate_sql,
                        output_dir,
                        prefix + "external",
                        int(case.get("clients", 1)),
                        int(getattr(args, "tuned_duration", 20)),
                        role="external",
                        connection_config=applied.get("connection_configuration"),
                    )
                )
            else:
                processes.extend(
                    legacy_runner.start_psql_batch(
                        tuned_args,
                        candidate_sql,
                        output_dir,
                        prefix,
                        int(case.get("clients", 1)),
                        connection_config=applied.get("connection_configuration"),
                    )
                )
            tuned_samples = legacy_runner.sample_phase(
                tuned_args,
                prefix,
                output_dir,
                "candidate",
                int(getattr(args, "tuned_duration", 20)),
                False,
            )
            legacy_runner.wait_processes(processes)
            if case.get("plan_sql"):
                plans["base"] = legacy_runner.explain(
                    runner_args, str(case["plan_sql"]), [], candidate_id + "-base"
                )
                session_sets = [
                    '{}=\'{}\''.format(name, str(value).replace("'", "''"))
                    for name, value in applied.get("session_configuration", {}).items()
                ]
                plans["candidate"] = legacy_runner.explain(
                    tuned_args,
                    str(case["plan_sql"]),
                    session_sets,
                    candidate_id + "-candidate",
                    connection_config=applied.get("connection_configuration"),
                )
        tuned_all_metrics = legacy_runner.phase_external_metrics(processes)
        tuned_metrics = legacy_runner.external_only_metrics(processes)
        result["status"] = "completed"
    except Exception as exc:
        error = "{}: {}".format(type(exc).__name__, exc)
        result["status"] = "failed"
    finally:
        if processes:
            try:
                legacy_runner.wait_processes(processes, terminate=True)
            except Exception as exc:
                error = error or "cleanup {}: {}".format(type(exc).__name__, exc)
        shutil.rmtree(str(stage_dir), ignore_errors=True)

    result["apply"] = apply_state
    result["benchmark"] = {
        "samples": tuned_samples,
        "all_process_metrics": tuned_all_metrics,
        "metrics": tuned_metrics,
        "plans": plans,
    }
    baseline_tps = case_result.get("baseline_metrics", {}).get("tps")
    anomaly_control_tps = _case_control_tps(case_result)
    candidate_control_tps = _control_tps(tuned_all_metrics)
    comparison: Dict[str, Any] = {
        "baseline_control_tps": baseline_tps,
        "anomaly_control_tps": anomaly_control_tps,
        "candidate_control_tps": candidate_control_tps,
        "candidate_score": candidate_control_tps,
        "candidate_over_anomaly_ratio": (
            candidate_control_tps / anomaly_control_tps
            if isinstance(candidate_control_tps, (int, float)) and anomaly_control_tps
            else None
        ),
        "candidate_over_baseline_ratio": (
            candidate_control_tps / float(baseline_tps)
            if isinstance(candidate_control_tps, (int, float)) and isinstance(baseline_tps, (int, float)) and baseline_tps
            else None
        ),
        "all_workers_succeeded": all(
            item.get("returncode") == 0 for item in tuned_all_metrics.get("workers", [])
        ) if tuned_all_metrics else False,
    }
    if error:
        result["error"] = error
    result["comparison"] = comparison
    result["finished_at"] = utc_now()
    _safe_write(output_dir / "candidate_result.json", result)
    return result


def choose_best(results: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    eligible = [
        item for item in results
        if item.get("status") == "completed"
        and isinstance(item.get("comparison", {}).get("candidate_score"), (int, float))
    ]
    if not eligible:
        return None
    best = max(eligible, key=lambda item: float(item["comparison"]["candidate_score"]))
    return {
        "candidate_id": best.get("candidate_id"),
        "candidate_score": best.get("comparison", {}).get("candidate_score"),
        "configuration": best.get("configuration", {}),
        "selection": "highest measured normal-control TPS under candidate workload",
    }
