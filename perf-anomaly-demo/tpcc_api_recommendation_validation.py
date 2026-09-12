#!/usr/bin/env python3
"""Run TPCC external-pressure validation with an exact SysInsight API result.

The legacy ``tpcc_external_cases.py`` runner remains useful for controlled
experiments, but its ``repair`` fields are presets.  This entry point never
reads those fields as a repair.  It runs the anomaly, invokes
``sysinsight_original_llm.py`` against the supplied API (unless an existing
API artifact is explicitly supplied), selects the raw configuration returned
by the original source selector, applies every returned field, measures the
same protected TPCC control load, and restores the database afterwards.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib
import json
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import tpcc_external_cases as legacy_runner  # noqa: E402
from pg_temporary_config import TemporaryPostgresConfiguration  # noqa: E402
from pg_temporary_config import connection_args_for_configuration  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="d01_work_mem_sort")
    parser.add_argument("--db", default="keeninsight")
    parser.add_argument("--db-user", default="postgres")
    parser.add_argument("--run-as", default="postgres")
    parser.add_argument("--host", default="/var/run/postgresql")
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--pg-version", default="12")
    parser.add_argument("--pg-cluster", default="main")
    parser.add_argument("--prometheus-url", default="http://127.0.0.1:9090")
    parser.add_argument("--alert-name", default="SysInsightDemoAnomaly")
    parser.add_argument("--baseline-duration", type=int, default=20)
    parser.add_argument("--case-duration", type=int, default=25)
    parser.add_argument("--tuned-duration", type=int, default=20)
    parser.add_argument("--normal-clients", type=int, default=2)
    parser.add_argument("--perf-frequency", type=int, default=300)
    parser.add_argument("--normal-sql", default="normal.sql")
    parser.add_argument(
        "--workload-module",
        default="",
        help="optional Python module exporting CASE_DEFINITIONS and lifecycle hooks",
    )
    parser.add_argument("--api-base", default="http://35.212.195.134:28317/v1")
    parser.add_argument("--model", default="GPT5.6-SOL")
    parser.add_argument("--n-candidates", type=int, default=1)
    parser.add_argument("--n-templates", type=int, default=1)
    parser.add_argument("--selector-n-gens", type=int, default=1)
    parser.add_argument("--api-result", default="", help="已有 sysinsight_original_llm result.json；不再重复调用 API")
    parser.add_argument("--api-config-index", type=int, default=-1)
    parser.add_argument("--output", default="")
    return parser.parse_args()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def runner_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        db=args.db,
        db_user=args.db_user,
        run_as=args.run_as,
        host=args.host,
        port=args.port,
        prometheus_url=args.prometheus_url,
        alert_name=args.alert_name,
        baseline_duration=args.baseline_duration,
        case_duration=args.case_duration,
        tuned_duration=args.tuned_duration,
        normal_clients=args.normal_clients,
        perf_frequency=args.perf_frequency,
        normal_sql=args.normal_sql,
    )


def process_args(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        perf_frequency=args.perf_frequency,
        stackcollapse=str(ROOT / "vendor" / "FlameGraph" / "stackcollapse-perf.pl"),
        prometheus_url=args.prometheus_url,
        alert_name=args.alert_name,
    )


def workload_hooks(module: Any, name: str, args: argparse.Namespace, case: Dict[str, Any], case_dir: Path) -> Dict[str, Any]:
    hook = getattr(module, name, None)
    if not callable(hook):
        return {"status": "not_available"}
    value = hook(args, case, case_dir)
    return value if isinstance(value, dict) else {"status": "completed", "value": value}


def stage_api_sql(case: Dict[str, Any], stage_dir: Path, session_config: Dict[str, Any]) -> Path:
    destination = stage_dir / "api_tuned_{}".format(case["sql"])
    source_text = (legacy_runner.CASE_ROOT / case["sql"]).read_text(encoding="utf-8")
    statements: List[str] = []
    for name, value in session_config.items():
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
            raise ValueError("unsafe session GUC name: {}".format(name))
        text = str(value)
        if not re.fullmatch(r"[A-Za-z0-9_./:+%\- ]+", text):
            raise ValueError("unsafe session GUC value for {}".format(name))
        statements.append('SET "{}" = \'{}\';'.format(name, text.replace("'", "''")))
    destination.write_text("\n".join(statements + [source_text]), encoding="utf-8")
    destination.chmod(0o644)
    return destination


def control_tps(all_process_metrics: Dict[str, Any]) -> Optional[float]:
    for worker in all_process_metrics.get("workers", []):
        if "control" in str(worker.get("app_name", "")):
            value = worker.get("metrics", {}).get("tps")
            if isinstance(value, (int, float)):
                return float(value)
    return None


def probe_effective_configuration(
    db_args: argparse.Namespace,
    session_configuration: Dict[str, Any],
    connection_configuration: Dict[str, Any],
    names: List[str],
) -> Dict[str, Any]:
    """Verify session and connection-start GUCs on a fresh backend."""

    statements: List[str] = []
    for name, value in session_configuration.items():
        statements.append(
            'SET "{}" = \'{}\';'.format(
                name, str(value).replace("'", "''")
            )
        )
    values = ", ".join(
        "({})".format(legacy_runner.sql_literal(name)) for name in sorted(names)
    )
    sql = "{} SELECT json_object_agg(name, current_setting(name, true)) FROM (VALUES {}) AS v(name);".format(
        " ".join(statements), values
    )
    raw = legacy_runner.psql_text(
        db_args,
        sql,
        "perf-anomaly-demo-api-effective-config",
        timeout=30.0,
        connection_config=connection_configuration,
    )
    return {
        "requested_session_configuration": session_configuration,
        "requested_connection_configuration": connection_configuration,
        "effective_current_setting_on_probe_backend": json.loads(raw),
    }


def source_api_configurations(api_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    values: List[Dict[str, Any]] = []
    for item in api_result.get("api_generated_configurations", []):
        if item.get("phase") != "acquisition" or item.get("parse_status") != "parsed":
            continue
        value = item.get("parsed_by_source_LLM_ACQ__convert_to_json")
        if isinstance(value, dict) and value:
            values.append({
                "call_index": item.get("call_index"),
                "choice_index": item.get("choice_index"),
                "raw_content": item.get("raw_content"),
                "configuration_section": item.get("configuration_section"),
                "configuration": value,
            })
    return values


def choose_api_configuration(
    api_result: Dict[str, Any], requested_index: int
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    configurations = source_api_configurations(api_result)
    if not configurations:
        raise RuntimeError("API artifact has no parsed acquisition configuration; no preset fallback is allowed")
    selector = api_result.get("source_selector") or {}
    if requested_index >= 0:
        index = requested_index
        selection_reason = "explicit_api_config_index"
    elif isinstance(selector.get("selected_row_index"), int):
        index = int(selector["selected_row_index"])
        selection_reason = "original_LLM_DIS_SM_selected_row_index"
    else:
        index = 0
        selection_reason = "first_acquisition_response_without_source_selector"
    if index < 0 or index >= len(configurations):
        raise IndexError("selected API configuration index {} outside {} parsed responses".format(index, len(configurations)))
    selected = configurations[index]
    return selected["configuration"], {
        "selection_reason": selection_reason,
        "selected_index": index,
        "parsed_acquisition_count": len(configurations),
        "selected_api_response": selected,
        "source_selector": selector,
    }


def run_anomaly(
    args: argparse.Namespace,
    case: Dict[str, Any],
    case_dir: Path,
    stage_dir: Path,
    normal_profile: Path,
) -> Dict[str, Any]:
    rargs = runner_args(args)
    prefix = "perf-anomaly-demo-tpcc-{}-".format(case["id"])
    normal_sql = legacy_runner.stage_sql(
        legacy_runner.CASE_ROOT / case.get("normal_sql", args.normal_sql), stage_dir
    )
    anomaly_sql = legacy_runner.stage_sql(legacy_runner.CASE_ROOT / case["sql"], stage_dir)
    anomaly_dir = case_dir / "anomaly"
    anomaly_dir.mkdir(parents=True, exist_ok=True)
    legacy_runner.wait_alert_clear(rargs)
    control = legacy_runner.start_pgbench(
        rargs, normal_sql, anomaly_dir, prefix + "control", args.normal_clients, args.case_duration, role="control"
    )
    processes: List[Dict[str, Any]] = [control]
    time.sleep(0.5)
    if case["mode"] == "pgbench":
        processes.append(
            legacy_runner.start_pgbench(
                rargs, anomaly_sql, anomaly_dir, prefix + "external", case["clients"], args.case_duration, role="external"
            )
        )
    else:
        processes.extend(legacy_runner.start_psql_batch(rargs, anomaly_sql, anomaly_dir, prefix, case["clients"]))
    samples = legacy_runner.sample_phase(rargs, prefix, anomaly_dir, "anomaly", args.case_duration, True)
    legacy_runner.wait_processes(processes)
    perf = samples.get("perf")
    postprocess: Dict[str, Any] = {}
    if perf is not None:
        postprocess = legacy_runner.strict_demo.postprocess_perf(process_args(args), anomaly_dir, perf)
    detection = legacy_runner.source_detection(anomaly_dir, normal_profile)
    return {
        "samples": samples,
        "workers": legacy_runner.serializable_processes(processes),
        "all_process_metrics": legacy_runner.phase_external_metrics(processes),
        "metrics": legacy_runner.external_only_metrics(processes),
        "perf_postprocess": postprocess,
        "sysinsight_source_detection": detection,
    }


def invoke_api(
    args: argparse.Namespace, case_result_path: Path, output: Path
) -> Tuple[Path, Dict[str, Any]]:
    command = [
        sys.executable,
        str(ROOT / "sysinsight_original_llm.py"),
        "--case-result", str(case_result_path),
        "--dbms", "postgresql",
        "--db-version", args.pg_version,
        "--api-base", args.api_base,
        "--model", args.model,
        "--n-candidates", str(args.n_candidates),
        "--n-templates", str(args.n_templates),
        "--selector-n-gens", str(args.selector_n_gens),
        "--run-source-selector",
        "--sync-transport",
        "--output", str(output),
    ]
    completed = subprocess.run(
        command,
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output.mkdir(parents=True, exist_ok=True)
    (output / "wrapper_stdout.log").write_text(completed.stdout, encoding="utf-8")
    result_path = output / "result.json"
    if completed.returncode != 0 or not result_path.exists():
        partial = output / "partial_result.json"
        detail = partial if partial.exists() else output / "wrapper_stdout.log"
        raise RuntimeError("original API wrapper failed ({}), inspect {}".format(completed.returncode, detail))
    return result_path, json.loads(result_path.read_text(encoding="utf-8"))


def validate_case(
    args: argparse.Namespace,
    case: Dict[str, Any],
    run_dir: Path,
    stage_dir: Path,
    normal_profile: Path,
    baseline: Dict[str, Any],
    workload_module: Any,
) -> Dict[str, Any]:
    case_dir = run_dir / case["id"]
    case_dir.mkdir(parents=True, exist_ok=True)
    prepared = workload_hooks(workload_module, "prepare_case", runner_args(args), case, case_dir)
    result: Optional[Dict[str, Any]] = None
    try:
        result = _validate_case(
            args, case, run_dir, stage_dir, normal_profile, baseline,
            workload_module, prepared,
        )
        return result
    finally:
        cleanup = workload_hooks(workload_module, "cleanup_case", runner_args(args), case, case_dir)
        if result is not None:
            result.setdefault("workload_lifecycle", {})["cleanup"] = cleanup
            (case_dir / "case_result.json").write_text(
                json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
            )


def _validate_case(
    args: argparse.Namespace,
    case: Dict[str, Any],
    run_dir: Path,
    stage_dir: Path,
    normal_profile: Path,
    baseline: Dict[str, Any],
    workload_module: Any,
    prepared: Dict[str, Any],
) -> Dict[str, Any]:
    case_dir = run_dir / case["id"]
    case_dir.mkdir(parents=True, exist_ok=True)
    anomaly = run_anomaly(args, case, case_dir, stage_dir, normal_profile)
    preliminary = {
        "id": case["id"],
        "title": case["title"],
        "external_event": case["event"],
        "mode": case["mode"],
        "baseline_metrics": baseline["metrics"],
        "anomaly": anomaly,
        # The original wrapper reads this field at case-result top level.
        "sysinsight_source_detection": anomaly["sysinsight_source_detection"],
        "provenance": {
            "repair_candidate_from_case_definition": case.get("repair"),
            "repair_candidate_is_not_used": True,
        },
    }
    preliminary_path = case_dir / "pre_api_case_result.json"
    preliminary_path.write_text(json.dumps(preliminary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    if args.api_result:
        api_result_path = Path(args.api_result).resolve()
        api_result = json.loads(api_result_path.read_text(encoding="utf-8"))
    else:
        api_result_path, api_result = invoke_api(args, preliminary_path, case_dir / "sysinsight_api")
    api_config, selection = choose_api_configuration(api_result, args.api_config_index)
    (case_dir / "selected_api_configuration.json").write_text(
        json.dumps(api_config, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
    )

    reset_state = workload_hooks(workload_module, "reset_case", runner_args(args), case, case_dir)
    rargs = runner_args(args)
    prefix = "perf-anomaly-demo-tpcc-{}-".format(case["id"])
    tuned_dir = case_dir / "tuned"
    tuned_dir.mkdir(parents=True, exist_ok=True)
    before_api_settings = legacy_runner.settings_snapshot(
        rargs, sorted(api_config), case["id"] + "-api-before"
    )
    tuned_processes: List[Dict[str, Any]] = []
    apply_state: Dict[str, Any] = {}
    plans: Dict[str, str] = {}
    config_applier = TemporaryPostgresConfiguration(rargs, api_config, case["id"])
    with config_applier as applied:
        tuned_rargs = connection_args_for_configuration(
            rargs, applied["normalized_configuration"]
        )
        apply_state = applied
        tuned_sql = stage_api_sql(case, stage_dir, applied["session_configuration"])
        apply_state["effective_configuration_probe"] = probe_effective_configuration(
            tuned_rargs,
            applied["session_configuration"],
            applied["connection_configuration"],
            sorted(api_config),
        )
        normal_sql = legacy_runner.stage_sql(
            legacy_runner.CASE_ROOT / case.get("normal_sql", args.normal_sql), stage_dir
        )
        tuned_control = legacy_runner.start_pgbench(
            tuned_rargs, normal_sql, tuned_dir, prefix + "tuned-control", args.normal_clients,
            args.tuned_duration, role="control",
            connection_config=applied["connection_configuration"],
        )
        tuned_processes = [tuned_control]
        time.sleep(0.5)
        if case["mode"] == "pgbench":
            tuned_processes.append(
                legacy_runner.start_pgbench(
                    tuned_rargs, tuned_sql, tuned_dir, prefix + "tuned-external", case["clients"],
                    args.tuned_duration, role="external",
                    connection_config=applied["connection_configuration"],
                )
            )
        else:
            tuned_processes.extend(legacy_runner.start_psql_batch(
                tuned_rargs, tuned_sql, tuned_dir, prefix + "tuned-", case["clients"],
                connection_config=applied["connection_configuration"],
            ))
        tuned_samples = legacy_runner.sample_phase(tuned_rargs, prefix + "tuned-", tuned_dir, "tuned", args.tuned_duration, False)
        legacy_runner.wait_processes(tuned_processes)
        if case.get("plan_sql"):
            plans["base"] = legacy_runner.explain(rargs, case["plan_sql"], [], case["id"] + "-base-api")
            session_sets = [
                '{}=\'{}\''.format(name, str(value).replace("'", "''"))
                for name, value in applied["session_configuration"].items()
            ]
            plans["tuned"] = legacy_runner.explain(
                tuned_rargs, case["plan_sql"], session_sets, case["id"] + "-tuned-api",
                connection_config=applied["connection_configuration"],
            )
        tuned_settings_live = legacy_runner.settings_snapshot(tuned_rargs, sorted(api_config), case["id"] + "-api-applied")

    tuned_all_metrics = legacy_runner.phase_external_metrics(tuned_processes)
    tuned_metrics = legacy_runner.external_only_metrics(tuned_processes)
    restored_settings = legacy_runner.settings_snapshot(rargs, sorted(api_config), case["id"] + "-api-restored")
    anomaly_control = control_tps(anomaly["all_process_metrics"])
    tuned_control_tps = control_tps(tuned_all_metrics)
    baseline_tps = baseline["metrics"].get("tps")
    pressure_ratio = anomaly_control / baseline_tps if baseline_tps and anomaly_control else None
    repair_ratio = tuned_control_tps / anomaly_control if anomaly_control and tuned_control_tps else None
    restored_ratio = tuned_control_tps / baseline_tps if baseline_tps and tuned_control_tps else None
    api_pre_values = {name: str(value) for name, value in before_api_settings.items()}
    restored_exact = all(str(restored_settings.get(name)) == value for name, value in api_pre_values.items())
    application_restored = apply_state.get("status") in {
        # Global GUCs are explicitly restored through ALTER SYSTEM and reload/
        # restart.  Session-only GUCs disappear when the API test backends
        # exit; the applier records that state separately.
        "applied_and_restored",
        "session_only_applied_and_ended",
    }
    decision = {
        "baseline_control_tps": baseline_tps,
        "pressure_control_tps": anomaly_control,
        "repaired_control_tps": tuned_control_tps,
        "pressure_ratio_to_baseline": pressure_ratio,
        "repair_ratio_to_pressure": repair_ratio,
        "repaired_ratio_to_baseline": restored_ratio,
        "pressure_drop_over_20pct": bool(pressure_ratio is not None and pressure_ratio <= 0.80),
        "repair_rise_over_20pct": bool(repair_ratio is not None and repair_ratio >= 1.20),
        "restored_within_20pct_of_baseline": bool(restored_ratio is not None and restored_ratio >= 0.80),
        "api_configuration_restored": bool(application_restored and restored_exact),
        "complete_case": bool(
            pressure_ratio is not None and repair_ratio is not None
            and restored_ratio is not None
            and pressure_ratio <= 0.80 and repair_ratio >= 1.20
            and restored_ratio >= 0.80
            and application_restored
            and restored_exact
        ),
        "metric": "normal_control_tps",
    }
    result = {
        "id": case["id"],
        "title": case["title"],
        "external_event": case["event"],
        "mode": case["mode"],
        "baseline_metrics": baseline["metrics"],
        "anomaly": anomaly,
        "api": {
            "artifact": str(api_result_path),
            "profile": "postgresql/{}".format(args.pg_version),
            "selected_configuration": api_config,
            "selection": selection,
            "raw_api_response_trace_in_artifact": "llm_calls",
        },
        "temporary_application": apply_state,
        "tuned": {
            "samples": tuned_samples,
            "workers": legacy_runner.serializable_processes(tuned_processes),
            "metrics": tuned_metrics,
            "all_process_metrics": tuned_all_metrics,
            "settings_while_api_config_applied": tuned_settings_live,
            "effective_configuration_probe": apply_state.get("effective_configuration_probe"),
        },
        "settings_after_restore": restored_settings,
        "plans": plans,
        "decision": decision,
        "workload_lifecycle": {
            "prepared": prepared,
            "reset_before_tuned": reset_state,
        },
        "preset_case_definition_not_used_as_repair": True,
    }
    (case_dir / "case_result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return result


def main() -> int:
    args = parse_args()
    if args.normal_clients < 1 or args.baseline_duration < 5 or args.case_duration < 5 or args.tuned_duration < 5:
        raise SystemExit("durations must be at least 5 seconds and clients positive")
    workload_module: Any = legacy_runner
    if args.workload_module:
        workload_module = importlib.import_module(args.workload_module)
    definitions = getattr(workload_module, "CASE_DEFINITIONS", legacy_runner.CASE_DEFINITIONS)
    selected = [item.strip() for item in args.only.split(",") if item.strip()]
    cases = [case for case in definitions if case["id"] in selected]
    if len(cases) != len(selected):
        known = {case["id"] for case in definitions}
        raise SystemExit("unknown cases: {}".format(sorted(set(selected) - known)))
    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = Path(args.output).resolve() if args.output else ROOT / "results" / "tpcc_api_validation" / run_id
    output_root.mkdir(parents=True, exist_ok=False)
    stage_dir = Path(tempfile.mkdtemp(prefix="tpcc-api-sql-"))
    stage_dir.chmod(0o755)
    rargs = runner_args(args)
    summary: Dict[str, Any] = {
        "run_id": run_id,
        "started_at": utc_now(),
        "cases_requested": selected,
        "workload_module": args.workload_module or "tpcc_external_cases",
        "criteria": {
            "pressure_drop": "normal TPCC control TPS during external pressure <= 0.80 of baseline",
            "repair_rise": "normal TPCC control TPS after applying the exact API configuration >= 1.20 of pressure-stage TPS",
            "restored_level": "normal TPCC control TPS after applying the exact API configuration >= 0.80 of no-pressure baseline",
            "api_requirement": "selected configuration is parsed from the original API response; no preset fallback",
            "apply_requirement": "every selected API field is applied according to pg_settings.context and restored afterwards",
        },
        "preset_repair_fields_are_excluded": True,
    }
    try:
        baseline_dir = output_root / "baseline"
        baseline_dir.mkdir(parents=True, exist_ok=True)
        baseline = legacy_runner.baseline_run(
            rargs, output_root, stage_dir, normal_sql_name=args.normal_sql
        )
        summary["baseline"] = baseline
        profile_rel = baseline.get("normal_profile", {}).get("path")
        if not profile_rel:
            raise RuntimeError("baseline did not produce normal profile; cannot run original SysInsight detection")
        normal_profile = ROOT / profile_rel
        results = []
        for case in cases:
            print("开始 API 完整链路：{}".format(case["id"]), flush=True)
            result = validate_case(
                args, case, output_root, stage_dir, normal_profile, baseline, workload_module
            )
            results.append({
                "id": result["id"],
                "api_configuration": result["api"]["selected_configuration"],
                "decision": result["decision"],
                "apply_status": result["temporary_application"].get("status"),
                "perf_status": result["anomaly"]["perf_postprocess"].get("status"),
                "prometheus_triggered": bool(result["anomaly"]["samples"].get("trigger")),
            })
        summary["cases"] = results
        summary["complete_case_count"] = sum(1 for item in results if item["decision"].get("complete_case"))
        summary["completed_at"] = utc_now()
        (output_root / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print("结果目录：{}".format(output_root), flush=True)
        print("满足完整标准的案例数：{}".format(summary["complete_case_count"]), flush=True)
        return 0 if summary["complete_case_count"] == len(cases) else 1
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
