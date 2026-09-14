#!/usr/bin/env python3
"""Run the PostgreSQL SysInsight workflow around the original source code.

This is the missing orchestration layer for the demo.  It connects the
monitoring output, the original SysInsight detector/matcher and the original
LLAMBO acquisition wrapper, then optionally benchmarks every generated
candidate against the real TPCC workload.  Configuration lifecycle operations
are delegated to :mod:`pg_temporary_config` and are intentionally not
reimplemented here.

The command is useful in two modes:

* replay a completed case result and generate the canonical SysInsight input;
* use ``--benchmark-candidates N`` to run the generated candidates through a
  real PostgreSQL/TPCC measurement loop.

Without an API key or an API artifact the input and detection stages still
run, but the result explicitly records that candidate generation was skipped.
No skipped stage is reported as successful.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from db_profile import profile_summary, resolve_profile  # noqa: E402
from sysinsight_candidate_benchmark import (  # noqa: E402
    benchmark_candidate,
    candidate_configurations,
    choose_best,
    jsonable,
    validate_candidate,
    validate_candidate_live,
)
from sysinsight_prometheus import (  # noqa: E402
    PrometheusClient,
    PrometheusError,
    _case_window,
    build_sysinsight_input,
    collect_window,
)


DEFAULT_API_BASE = "http://35.212.195.134:28317/v1"
DEFAULT_MODEL = "GPT5.6-SOL"


def api_key_from_environment() -> str:
    """Read a supplied key without persisting it; keep legacy names compatible."""

    for name in ("SYSINSIGHT_GPT_API_KEY", "SYSINSIGHT_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(name, "")
        if value:
            return value
    return ""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-result", default="", help="completed case_result.json")
    parser.add_argument("--run-dir", default="", help="find the only case_result.json below this directory")
    parser.add_argument("--db", default=os.environ.get("SYSINSIGHT_DB", "keeninsight"))
    parser.add_argument("--db-user", default="postgres")
    parser.add_argument("--run-as", default="postgres")
    parser.add_argument("--host", default="/var/run/postgresql")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--pg-version", default=os.environ.get("SYSINSIGHT_DB_VERSION", "12"))
    parser.add_argument("--pg-cluster", default="main")
    parser.add_argument("--dbms", default=os.environ.get("SYSINSIGHT_DBMS", "postgresql"))
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:9090")
    parser.add_argument("--prometheus-capture", default="", help="reuse a previously captured adapter JSON")
    parser.add_argument("--prometheus-step", type=float, default=15.0)
    parser.add_argument("--prometheus-timeout", type=float, default=5.0)
    parser.add_argument("--alert-name", default="SysInsightDemoAnomaly")
    parser.add_argument("--skip-prometheus", action="store_true")
    parser.add_argument("--skip-detection", action="store_true")
    parser.add_argument("--skip-live-validation", action="store_true", help="do not query live pg_settings")
    parser.add_argument("--api-result", default="", help="reuse sysinsight_original_llm/result.json")
    parser.add_argument("--api-base", default=os.environ.get("SYSINSIGHT_GPT_BASE_URL", DEFAULT_API_BASE))
    parser.add_argument("--model", default=os.environ.get("SYSINSIGHT_GPT_MODEL", DEFAULT_MODEL))
    parser.add_argument("--n-candidates", type=int, default=5)
    parser.add_argument("--n-templates", type=int, default=1)
    parser.add_argument("--selector-n-gens", type=int, default=1)
    parser.add_argument("--no-api", action="store_true", help="do not call the API even when a key is available")
    parser.add_argument("--max-candidates", type=int, default=5)
    parser.add_argument(
        "--benchmark-candidates",
        type=int,
        default=0,
        help="benchmark up to N parsed candidates; 0 only prepares the evaluation stage",
    )
    parser.add_argument("--workload-module", default="tpcc_external_cases")
    parser.add_argument("--baseline-duration", type=int, default=20)
    parser.add_argument("--case-duration", type=int, default=25)
    parser.add_argument("--tuned-duration", type=int, default=20)
    parser.add_argument("--normal-clients", type=int, default=2)
    parser.add_argument("--normal-sql", default="normal.sql")
    parser.add_argument("--perf-frequency", type=int, default=300)
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    if args.n_candidates < 1 or args.n_templates < 1 or args.max_candidates < 0:
        parser.error("candidate counts must be positive, and max-candidates cannot be negative")
    if args.benchmark_candidates < 0:
        parser.error("benchmark-candidates cannot be negative")
    if args.prometheus_step <= 0 or args.prometheus_timeout <= 0:
        parser.error("Prometheus step and timeout must be positive")
    return args


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(jsonable(value), indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )


def _find_case_result(args: argparse.Namespace) -> Path:
    if args.case_result:
        path = Path(args.case_result).resolve()
        if not path.is_file():
            raise FileNotFoundError("case result not found: {}".format(path))
        return path
    if not args.run_dir:
        raise SystemExit("--case-result or --run-dir is required")
    root = Path(args.run_dir).resolve()
    paths = sorted(root.rglob("case_result.json"))
    if len(paths) != 1:
        raise SystemExit("--run-dir must contain exactly one case_result.json (found {})".format(len(paths)))
    return paths[0]


def _load_json(path: Path) -> Dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON object required: {}".format(path))
    return value


def _stage(steps: List[Dict[str, Any]], name: str, status: str, detail: Any = None) -> None:
    item: Dict[str, Any] = {"name": name, "status": status}
    if detail is not None:
        item["detail"] = detail
    steps.append(item)


def _profile_for(args: argparse.Namespace) -> Dict[str, Any]:
    profile = resolve_profile(args.dbms, args.pg_version)
    return profile_summary(profile)


def _load_prometheus_capture(args: argparse.Namespace, case_result: Mapping[str, Any]) -> Dict[str, Any]:
    if args.skip_prometheus:
        return {
            "status": "skipped",
            "source": {"type": "prometheus", "url": args.prometheus_url},
            "queries": {},
            "alerts": {"status": "skipped", "selected": None, "all": []},
            "window": {"start": None, "end": None, "step": args.prometheus_step},
        }
    if args.prometheus_capture:
        path = Path(args.prometheus_capture).resolve()
        payload = _load_json(path)
        payload.setdefault("source", {})["reused_capture"] = str(path)
        return payload
    start, end = _case_window(case_result)
    client = PrometheusClient(args.prometheus_url, timeout=args.prometheus_timeout)
    return collect_window(
        client,
        args.db,
        start,
        end,
        args.prometheus_step,
        args.alert_name,
    )


def _detection_present(case_result: Mapping[str, Any]) -> bool:
    value = case_result.get("sysinsight_source_detection")
    if not isinstance(value, dict) or not value:
        anomaly = case_result.get("anomaly", {})
        value = anomaly.get("sysinsight_source_detection") if isinstance(anomaly, dict) else None
    if not isinstance(value, dict):
        return False
    return bool(value.get("source_compare") or value.get("source_match"))


def _run_source_detection(args: argparse.Namespace, case_path: Path) -> Dict[str, Any]:
    """Run the existing source detector only when the case lacks its result."""

    case_dir = case_path.parent
    anomaly_dir = case_dir / "anomaly"
    detection_dir = anomaly_dir if anomaly_dir.is_dir() else case_dir
    command = [
        sys.executable,
        str(ROOT / "sysinsight_detection.py"),
        "--run-dir",
        str(detection_dir),
        "--dbms",
        args.dbms,
        "--db-version",
        args.pg_version,
    ]
    completed = subprocess.run(
        command,
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log_path = case_dir / "sysinsight_pipeline_detection.log"
    log_path.write_text(completed.stdout, encoding="utf-8")
    result_path = detection_dir / "sysinsight_source_detection_result.json"
    if completed.returncode != 0 or not result_path.exists():
        return {
            "status": "failed",
            "returncode": completed.returncode,
            "log": str(log_path),
            "output": completed.stdout[-4000:],
        }
    result = _load_json(result_path)
    result["runner_returncode"] = completed.returncode
    result["result_path"] = str(result_path)
    return result


def _invoke_api(args: argparse.Namespace, case_path: Path, output_dir: Path) -> Tuple[Optional[Path], Dict[str, Any]]:
    api_key = api_key_from_environment()
    if args.no_api:
        return None, {"status": "skipped", "reason": "--no-api"}
    if not api_key:
        return None, {"status": "skipped", "reason": "SYSINSIGHT_GPT_API_KEY is not set"}
    # ``sysinsight_original_llm.py`` creates the artifact directory itself
    # with ``exist_ok=False``.  Only create its parent here.
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(ROOT / "sysinsight_original_llm.py"),
        "--case-result",
        str(case_path),
        "--sysinsight-input",
        str(output_dir.parent / "sysinsight_input.json"),
        "--dbms",
        args.dbms,
        "--db-version",
        args.pg_version,
        "--api-base",
        args.api_base,
        "--model",
        args.model,
        "--n-candidates",
        str(args.n_candidates),
        "--n-templates",
        str(args.n_templates),
        "--selector-n-gens",
        str(args.selector_n_gens),
        "--run-source-selector",
        "--sync-transport",
        "--output",
        str(output_dir),
    ]
    completed = subprocess.run(
        command,
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "wrapper_stdout.log").write_text(completed.stdout, encoding="utf-8")
    result_path = output_dir / "result.json"
    if completed.returncode != 0 or not result_path.exists():
        partial = output_dir / "partial_result.json"
        return None, {
            "status": "failed",
            "returncode": completed.returncode,
            "artifact": str(partial if partial.exists() else output_dir / "wrapper_stdout.log"),
            "output": completed.stdout[-4000:],
        }
    result = _load_json(result_path)
    result["wrapper_returncode"] = completed.returncode
    return result_path, result


def _load_workload_case(args: argparse.Namespace, case_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        module = importlib.import_module(args.workload_module)
    except Exception as exc:
        return None, "cannot import workload module {}: {}".format(args.workload_module, exc)
    definitions = getattr(module, "CASE_DEFINITIONS", [])
    for case in definitions:
        if isinstance(case, dict) and case.get("id") == case_id:
            return case, None
    return None, "case {} is not defined by {}".format(case_id, args.workload_module)


def _benchmark_args(args: argparse.Namespace) -> Any:
    return args


def _write_markdown_report(path: Path, summary: Mapping[str, Any]) -> None:
    stages = summary.get("workflow_steps", [])
    candidates = summary.get("candidates", {})
    lines = [
        "# SysInsight pipeline report",
        "",
        "- Run: `{}`".format(summary.get("run_id")),
        "- Case: `{}`".format(summary.get("case_result")),
        "- Database: `{}`".format(summary.get("database", {}).get("name")),
        "",
        "## Workflow status",
        "",
        "| Stage | Status |",
        "|---|---|",
    ]
    for item in stages:
        lines.append("| {} | {} |".format(item.get("name"), item.get("status")))
    lines.extend(
        [
            "",
            "## Candidates",
            "",
            "- Parsed: `{}`".format(candidates.get("parsed_count", 0)),
            "- Validated: `{}`".format(candidates.get("valid_count", 0)),
            "- Benchmarked: `{}`".format(candidates.get("benchmarked_count", 0)),
            "- Best measured candidate: `{}`".format(
                (summary.get("best_candidate") or {}).get("candidate_id", "none")
            ),
            "",
            "The benchmark stage is real only when its status is `completed`; a skipped or failed stage is retained as such.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    case_path = _find_case_result(args)
    case_result = _load_json(case_path)
    case_dir = case_path.parent
    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output = Path(args.output).resolve() if args.output else case_dir / "sysinsight_pipeline_{}".format(run_id)
    if output.exists():
        raise SystemExit("output already exists: {}".format(output))
    output.mkdir(parents=True, exist_ok=False)

    workflow_steps: List[Dict[str, Any]] = []
    summary: Dict[str, Any] = {
        "schema": "sysinsight.postgresql.pipeline.v1",
        "run_id": run_id,
        "started_at": utc_now(),
        "case_result": str(case_path),
        "output": str(output),
        "database": {
            "dbms": args.dbms,
            "version": args.pg_version,
            "name": args.db,
            "host": args.host,
            "port": args.port,
        },
        "workflow_steps": workflow_steps,
    }
    _write_json(output / "preflight.json", {
        "started_at": summary["started_at"],
        "case_result": str(case_path),
        "executables": {
            name: shutil.which(name) for name in ("psql", "pgbench", "perf", "perl")
        },
        "prometheus_url": args.prometheus_url,
        "api_base": args.api_base,
        "api_key_saved": False,
        "api_key_available": bool(api_key_from_environment()),
    })

    try:
        profile = _profile_for(args)
        summary["profile"] = profile
        _stage(workflow_steps, "environment", "completed", profile.get("name"))
    except Exception as exc:
        _stage(workflow_steps, "environment", "failed", "{}: {}".format(type(exc).__name__, exc))
        summary["status"] = "failed"
        summary["finished_at"] = utc_now()
        _write_json(output / "summary.json", summary)
        _write_markdown_report(output / "summary.md", summary)
        return 2

    try:
        prometheus = _load_prometheus_capture(args, case_result)
        _write_json(output / "prometheus_capture.json", prometheus)
        status = str(prometheus.get("status", "unknown"))
        _stage(workflow_steps, "prometheus_capture", status, {
            "failed_query_count": prometheus.get("failed_query_count", 0),
            "window": prometheus.get("window"),
        })
    except (PrometheusError, OSError, ValueError) as exc:
        prometheus = {
            "status": "failed",
            "error": "{}: {}".format(type(exc).__name__, exc),
            "queries": {},
            "alerts": {"status": "failed", "selected": None, "all": []},
        }
        _write_json(output / "prometheus_capture.json", prometheus)
        _stage(workflow_steps, "prometheus_capture", "failed", prometheus["error"])

    try:
        input_payload = build_sysinsight_input(
            case_result,
            prometheus,
            summary["profile"],
            case_path,
            database_name=args.db,
        )
        _write_json(output / "sysinsight_input.json", input_payload)
        _stage(workflow_steps, "input_conversion", "completed", {
            "schema": input_payload.get("schema"),
            "section_count": len(input_payload.get("steps", [])),
        })
    except Exception as exc:
        _stage(workflow_steps, "input_conversion", "failed", "{}: {}".format(type(exc).__name__, exc))
        input_payload = {}

    case_path_for_api = case_path
    if _detection_present(case_result):
        detection = case_result.get("sysinsight_source_detection", {})
        if not isinstance(detection, dict) or not detection:
            anomaly = case_result.get("anomaly", {})
            detection = anomaly.get("sysinsight_source_detection", {}) if isinstance(anomaly, dict) else {}
        _stage(workflow_steps, "source_detection_and_matching", "already_present")
    elif args.skip_detection:
        detection = {}
        _stage(workflow_steps, "source_detection_and_matching", "skipped", "--skip-detection")
    else:
        detection = _run_source_detection(args, case_path)
        if detection.get("status") == "failed":
            _stage(workflow_steps, "source_detection_and_matching", "failed", detection)
        else:
            case_result["sysinsight_source_detection"] = detection
            _stage(workflow_steps, "source_detection_and_matching", "completed", detection.get("result_path"))
            # Keep a copy of the actual input used by later API stages.  The
            # original case_result file is never overwritten.
            updated_case_path = output / "case_result_with_detection.json"
            _write_json(updated_case_path, case_result)
            case_path_for_api = updated_case_path
    # The final API-validation artifact nests detection under ``anomaly``,
    # while the original LLAMBO wrapper consumes the case-root field.  Promote
    # it in a pipeline-local copy so both layouts are accepted.
    if detection and not case_result.get("sysinsight_source_detection"):
        case_result["sysinsight_source_detection"] = detection
    if detection and case_path_for_api == case_path:
        updated_case_path = output / "case_result_for_sysinsight.json"
        _write_json(updated_case_path, case_result)
        case_path_for_api = updated_case_path
    if detection:
        # Detection is allowed to be generated by this invocation.  Refresh
        # the canonical input after that stage so its function/knob section is
        # not a stale pre-detection copy.
        input_payload = build_sysinsight_input(
            case_result,
            prometheus,
            summary["profile"],
            case_path,
            database_name=args.db,
        )
        _write_json(output / "sysinsight_input.json", input_payload)

    api_result: Dict[str, Any] = {}
    api_result_path: Optional[Path] = None
    if args.api_result:
        source = Path(args.api_result).resolve()
        try:
            api_result = _load_json(source)
            api_result_path = source
            _stage(workflow_steps, "candidate_generation", "completed", {"source": str(source)})
        except (OSError, ValueError) as exc:
            api_result = {"status": "failed", "error": "{}: {}".format(type(exc).__name__, exc)}
            _stage(workflow_steps, "candidate_generation", "failed", api_result["error"])
    elif not detection or detection.get("status") in {"failed", "source_detection_failed", "normal_profile_missing"}:
        api_result = {
            "status": "skipped",
            "reason": "source detection did not produce a usable function comparison",
        }
        _stage(workflow_steps, "candidate_generation", "skipped", api_result["reason"])
    else:
        api_result_path, api_result = _invoke_api(args, case_path_for_api, output / "sysinsight_api")
        _stage(workflow_steps, "candidate_generation", api_result.get("status", "completed"), {
            "artifact": str(api_result_path) if api_result_path else api_result.get("artifact"),
            "reason": api_result.get("reason"),
            "returncode": api_result.get("wrapper_returncode", api_result.get("returncode")),
        })

    constraints: Mapping[str, Any] = {}
    try:
        profile_obj = resolve_profile(args.dbms, args.pg_version)
        constraints = profile_obj.constraints()
    except Exception:
        # Environment/profile failure was already recorded above.  Candidate
        # validation still emits a useful structural result below.
        constraints = {}
    candidates = candidate_configurations(api_result, args.max_candidates)
    candidate_records: List[Dict[str, Any]] = []
    for item in candidates:
        validation = validate_candidate(item.get("configuration", {}), constraints)
        if args.skip_live_validation:
            live_validation = {"status": "skipped", "reason": "--skip-live-validation"}
        else:
            live_validation = validate_candidate_live(args, validation.get("configuration", {}))
        candidate_records.append({
            **item,
            "validation": validation,
            "live_validation": live_validation,
        })
    _write_json(output / "candidates.json", {
        "source_artifact": str(api_result_path) if api_result_path else None,
        "candidates": candidate_records,
    })
    valid_candidates = [item for item in candidate_records if item["validation"].get("valid")]
    _stage(workflow_steps, "candidate_validation", "completed" if candidates else "skipped", {
        "parsed_count": len(candidates),
        "valid_count": len(valid_candidates),
        "live_validation": {
            "completed": sum(1 for item in candidate_records if item["live_validation"].get("status") == "completed"),
            "invalid": sum(1 for item in candidate_records if item["live_validation"].get("status") == "invalid"),
            "unavailable": sum(1 for item in candidate_records if item["live_validation"].get("status") == "unavailable"),
        },
    })

    benchmark_results: List[Dict[str, Any]] = []
    benchmark_status = "skipped"
    benchmark_detail: Any = "--benchmark-candidates=0"
    if args.benchmark_candidates > 0:
        case_id = str(case_result.get("id", ""))
        case, case_error = _load_workload_case(args, case_id)
        if case is None:
            benchmark_status = "failed"
            benchmark_detail = case_error
        elif not valid_candidates:
            benchmark_status = "skipped"
            benchmark_detail = "no valid candidates"
        else:
            benchmark_status = "completed"
            selected = valid_candidates[:args.benchmark_candidates]
            for item in selected:
                candidate_dir = output / "benchmarks" / str(item["candidate_id"])
                result = benchmark_candidate(
                    _benchmark_args(args),
                    case,
                    case_result,
                    item,
                    candidate_dir,
                    constraints,
                )
                benchmark_results.append(result)
            if any(item.get("status") == "failed" for item in benchmark_results):
                benchmark_status = "failed"
            elif any(item.get("status") != "completed" for item in benchmark_results):
                benchmark_status = "partial"
            _write_json(output / "candidate_benchmark_summary.json", {
                "case": case_id,
                "results": benchmark_results,
                "best_candidate": choose_best(benchmark_results),
            })
            benchmark_detail = {
                "requested": args.benchmark_candidates,
                "attempted": len(selected),
                "completed": sum(1 for item in benchmark_results if item.get("status") == "completed"),
            }
    _stage(workflow_steps, "candidate_benchmark", benchmark_status, benchmark_detail)

    best_candidate = choose_best(benchmark_results)
    summary["prometheus"] = {
        "status": prometheus.get("status"),
        "capture": str(output / "prometheus_capture.json"),
    }
    summary["sysinsight_input"] = str(output / "sysinsight_input.json")
    summary["detection"] = detection
    summary["api"] = {
        "status": api_result.get("status", "completed" if api_result else "skipped"),
        "artifact": str(api_result_path) if api_result_path else None,
        "model": args.model,
        "api_key_saved": False,
    }
    summary["candidates"] = {
        "parsed_count": len(candidate_records),
        "valid_count": len(valid_candidates),
        "benchmark_attempted_count": len(benchmark_results),
        "benchmarked_count": sum(1 for item in benchmark_results if item.get("status") == "completed"),
        "benchmark_failed_count": sum(1 for item in benchmark_results if item.get("status") == "failed"),
        "validation_errors": [
            {"candidate_id": item["candidate_id"], "errors": item["validation"].get("errors", [])}
            for item in candidate_records
            if not item["validation"].get("valid")
        ],
    }
    summary["best_candidate"] = best_candidate
    summary["benchmark_results"] = benchmark_results
    stage_statuses = [str(item.get("status")) for item in workflow_steps]
    if "failed" in stage_statuses:
        summary["status"] = "failed"
    elif any(status in {"skipped", "partial", "unavailable", "invalid"} for status in stage_statuses):
        summary["status"] = "completed_with_gaps"
    else:
        summary["status"] = "completed"
    summary["finished_at"] = utc_now()
    _stage(workflow_steps, "result_persistence", "completed", {
        "summary": str(output / "summary.json"),
        "input": str(output / "sysinsight_input.json"),
    })
    _write_json(output / "pipeline_manifest.json", {
        "schema": "sysinsight.postgresql.pipeline.manifest.v1",
        "run_id": run_id,
        "artifacts": {
            "preflight": "preflight.json",
            "prometheus_capture": "prometheus_capture.json",
            "sysinsight_input": "sysinsight_input.json",
            "candidates": "candidates.json",
            "summary": "summary.json",
            "report": "summary.md",
        },
    })
    _write_json(output / "summary.json", summary)
    _write_markdown_report(output / "summary.md", summary)
    print("SysInsight 主流程结果：{}".format(output))
    print("输入：{}".format(output / "sysinsight_input.json"))
    print("候选：{} 个，合法：{} 个，实测：{} 个".format(
        len(candidate_records), len(valid_candidates), len(benchmark_results)
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
