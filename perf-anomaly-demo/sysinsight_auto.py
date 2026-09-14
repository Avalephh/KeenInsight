#!/usr/bin/env python3
"""Run one configured SysInsight tuning cycle from workload to measured choice.

This is the single-command entry point for the demo.  It first asks the real
TPCC runner to create a controlled baseline and pressure window, including the
Prometheus-triggered perf/source detection.  It then hands that case to
``sysinsight_pipeline.py`` which builds the canonical input, calls the
GPT5.6-SOL endpoint, validates the returned GUCs, and measures several
candidates against the same PostgreSQL workload.

The candidate measurements use the existing temporary configuration lifecycle:
each candidate is applied for its test session and is removed/restored before
the next candidate.  The best measured configuration is reported, but this
entry point does not leave a persistent configuration behind.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parent
DEFAULT_API_BASE = "http://35.212.195.134:28317/v1"
DEFAULT_MODEL = "GPT5.6-SOL"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="d01_work_mem_sort", help="one configured workload case")
    parser.add_argument("--workload-module", default="tpcc_external_cases")
    parser.add_argument("--db", default="keeninsight")
    parser.add_argument("--db-user", default="postgres")
    parser.add_argument("--run-as", default="postgres")
    parser.add_argument("--host", default="/var/run/postgresql")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--pg-version", default=os.environ.get("SYSINSIGHT_DB_VERSION", "12"))
    parser.add_argument("--pg-cluster", default="main")
    parser.add_argument("--dbms", default=os.environ.get("SYSINSIGHT_DBMS", "postgresql"))
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:9090")
    parser.add_argument("--alert-name", default="SysInsightDemoAnomaly")
    parser.add_argument("--api-base", default=os.environ.get("SYSINSIGHT_GPT_BASE_URL", DEFAULT_API_BASE))
    parser.add_argument("--model", default=os.environ.get("SYSINSIGHT_GPT_MODEL", DEFAULT_MODEL))
    parser.add_argument("--n-candidates", type=int, default=5)
    parser.add_argument("--n-templates", type=int, default=1)
    parser.add_argument("--selector-n-gens", type=int, default=1)
    parser.add_argument("--max-candidates", type=int, default=5)
    parser.add_argument(
        "--benchmark-candidates",
        type=int,
        default=3,
        help="number of valid API candidates to measure; at least one is required for automatic tuning",
    )
    parser.add_argument("--baseline-duration", type=int, default=20)
    parser.add_argument("--case-duration", type=int, default=25)
    parser.add_argument("--tuned-duration", type=int, default=20)
    parser.add_argument("--normal-clients", type=int, default=2)
    parser.add_argument("--perf-frequency", type=int, default=300)
    parser.add_argument("--normal-sql", default="normal.sql")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    if not args.only or "," in args.only:
        parser.error("--only must name exactly one workload case")
    if args.n_candidates < 1 or args.n_templates < 1 or args.selector_n_gens < 1:
        parser.error("LLM candidate counts must be positive")
    if args.max_candidates < 1 or args.benchmark_candidates < 1:
        parser.error("automatic tuning requires positive candidate counts")
    if args.normal_clients < 1 or args.baseline_duration < 5 or args.case_duration < 5 or args.tuned_duration < 5:
        parser.error("durations must be at least 5 seconds and clients positive")
    return args


def _api_key_available() -> bool:
    return any(os.environ.get(name) for name in (
        "SYSINSIGHT_GPT_API_KEY",
        "SYSINSIGHT_API_KEY",
        "OPENAI_API_KEY",
    ))


def _normalize_api_environment() -> None:
    """Accept the legacy SysInsight key name without writing it anywhere."""

    if os.environ.get("SYSINSIGHT_GPT_API_KEY"):
        return
    for name in ("SYSINSIGHT_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(name)
        if value:
            os.environ["SYSINSIGHT_GPT_API_KEY"] = value
            return


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def _run(command: List[str], log_path: Path) -> int:
    completed = subprocess.run(
        command,
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    log_path.write_text(completed.stdout, encoding="utf-8")
    return completed.returncode


def _prepare_command(args: argparse.Namespace, output: Path) -> List[str]:
    return [
        sys.executable,
        str(ROOT / "tpcc_api_recommendation_validation.py"),
        "--only", args.only,
        "--workload-module", args.workload_module,
        "--db", args.db,
        "--db-user", args.db_user,
        "--run-as", args.run_as,
        "--host", args.host,
        "--port", str(args.port),
        "--pg-version", args.pg_version,
        "--pg-cluster", args.pg_cluster,
        "--prometheus-url", args.prometheus_url,
        "--alert-name", args.alert_name,
        "--baseline-duration", str(args.baseline_duration),
        "--case-duration", str(args.case_duration),
        "--normal-clients", str(args.normal_clients),
        "--perf-frequency", str(args.perf_frequency),
        "--normal-sql", args.normal_sql,
        "--prepare-only",
        "--output", str(output),
    ]


def _pipeline_command(args: argparse.Namespace, case_result: Path, output: Path) -> List[str]:
    return [
        sys.executable,
        str(ROOT / "sysinsight_pipeline.py"),
        "--case-result", str(case_result),
        "--db", args.db,
        "--db-user", args.db_user,
        "--run-as", args.run_as,
        "--host", args.host,
        "--port", str(args.port),
        "--pg-version", args.pg_version,
        "--pg-cluster", args.pg_cluster,
        "--dbms", args.dbms,
        "--prometheus-url", args.prometheus_url,
        "--alert-name", args.alert_name,
        "--api-base", args.api_base,
        "--model", args.model,
        "--n-candidates", str(args.n_candidates),
        "--n-templates", str(args.n_templates),
        "--selector-n-gens", str(args.selector_n_gens),
        "--max-candidates", str(args.max_candidates),
        "--benchmark-candidates", str(args.benchmark_candidates),
        "--workload-module", args.workload_module,
        "--baseline-duration", str(args.baseline_duration),
        "--case-duration", str(args.case_duration),
        "--tuned-duration", str(args.tuned_duration),
        "--normal-clients", str(args.normal_clients),
        "--normal-sql", args.normal_sql,
        "--perf-frequency", str(args.perf_frequency),
        "--output", str(output),
    ]


def _find_prepared_case(preparation: Path, case_id: str) -> Path:
    direct = preparation / case_id / "case_result.json"
    if direct.is_file():
        return direct
    matches = sorted(preparation.rglob("case_result.json"))
    if len(matches) == 1:
        return matches[0]
    raise FileNotFoundError(
        "prepare-only did not produce exactly one case_result.json for {} (found {})".format(
            case_id, len(matches)
        )
    )


def _load_summary(path: Path) -> Optional[Dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def main() -> int:
    args = parse_args()
    _normalize_api_environment()
    if not _api_key_available():
        raise SystemExit(
            "automatic SysInsight tuning requires SYSINSIGHT_GPT_API_KEY "
            "(SYSINSIGHT_API_KEY and OPENAI_API_KEY are also accepted)"
        )

    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output = Path(args.output).resolve() if args.output else ROOT / "results" / "sysinsight_auto" / run_id
    if output.exists():
        raise SystemExit("output already exists: {}".format(output))
    output.mkdir(parents=True, exist_ok=False)
    preparation = output / "prepared_case"
    pipeline = output / "pipeline"

    manifest: Dict[str, Any] = {
        "schema": "sysinsight.postgresql.auto.manifest.v1",
        "run_id": run_id,
        "started_at": utc_now(),
        "case": args.only,
        "workload_module": args.workload_module,
        "api": {
            "base_url": args.api_base,
            "model": args.model,
            "key_available": True,
            "key_saved": False,
        },
        "stages": [],
    }
    _write_json(output / "preflight.json", manifest)

    prepare_command = _prepare_command(args, preparation)
    prepare_returncode = _run(prepare_command, output / "prepare_stdout.log")
    manifest["stages"].append({
        "name": "workload_detection",
        "status": "completed" if prepare_returncode == 0 else "failed",
        "returncode": prepare_returncode,
        "output": str(preparation),
    })
    if prepare_returncode != 0:
        manifest["status"] = "failed"
        manifest["finished_at"] = utc_now()
        _write_json(output / "auto_manifest.json", manifest)
        return prepare_returncode or 2

    try:
        case_result = _find_prepared_case(preparation, args.only)
    except (FileNotFoundError, OSError) as exc:
        manifest["stages"].append({"name": "case_handoff", "status": "failed", "error": str(exc)})
        manifest["status"] = "failed"
        manifest["finished_at"] = utc_now()
        _write_json(output / "auto_manifest.json", manifest)
        return 2

    manifest["case_result"] = str(case_result)
    manifest["stages"].append({
        "name": "case_handoff",
        "status": "completed",
        "case_result": str(case_result),
    })
    pipeline_command = _pipeline_command(args, case_result, pipeline)
    pipeline_returncode = _run(pipeline_command, output / "pipeline_stdout.log")
    pipeline_summary = _load_summary(pipeline / "summary.json")
    manifest["stages"].append({
        "name": "sysinsight_analysis_and_tuning",
        "status": (pipeline_summary or {}).get("status", "failed" if pipeline_returncode else "completed"),
        "returncode": pipeline_returncode,
        "output": str(pipeline),
        "best_candidate": (pipeline_summary or {}).get("best_candidate"),
    })
    manifest["pipeline_summary"] = str(pipeline / "summary.json")
    manifest["finished_at"] = utc_now()
    pipeline_status = (pipeline_summary or {}).get("status")
    manifest["status"] = "failed" if pipeline_returncode or pipeline_status == "failed" else (
        "completed_with_gaps" if pipeline_status == "completed_with_gaps" else "completed"
    )
    _write_json(output / "auto_manifest.json", manifest)
    print("SysInsight 自动流程结果：{}".format(output))
    print("检测现场：{}".format(case_result))
    print("统一报告：{}".format(pipeline / "summary.json"))
    print("最佳候选：{}".format((pipeline_summary or {}).get("best_candidate")))
    return 0 if manifest["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
