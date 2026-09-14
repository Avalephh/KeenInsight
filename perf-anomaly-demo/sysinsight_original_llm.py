#!/usr/bin/env python3
"""Run SysInsight's original LLAMBO acquisition prompt against a real case.

This wrapper deliberately imports the source snapshot instead of re-creating
the configuration prompt or its parser.  It only supplies the recorded TPCC
observation and captures the source call boundary for auditability.

The source snapshot contains an additional legacy SimpleParameterAnalyzer call
which uses a different, hard-coded endpoint.  That call is disabled here so
that the supplied API is the only external model endpoint used.  The original
ParameterLibrary/update(), LLM_ACQ prompt builder, ChatCompletion request,
response parser, range filter and duplicate filter remain the source methods.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = Path(
    os.environ.get(
        "SYSINSIGHT_SOURCE_ROOT",
        str(ROOT.parent / "repositories/Avalephh-KeenInsight/branch-sources/WorkloadTune"),
    )
)
PYDEPS = Path(os.environ.get("SYSINSIGHT_PYDEPS", str(ROOT / ".pydeps")))
DEFAULT_MODEL = "GPT5.6-SOL"
DEFAULT_API_BASE = "http://35.212.195.134:28317/v1"

sys.path.insert(0, str(ROOT))
from db_profile import DatabaseProfile, profile_summary, resolve_profile, available_profiles  # type: ignore


def api_key_from_environment() -> str:
    """Read the key supplied by the caller without storing it in artifacts."""

    for name in ("SYSINSIGHT_GPT_API_KEY", "SYSINSIGHT_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(name, "")
        if value:
            return value
    return ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-result", default="")
    parser.add_argument("--dbms", default=os.environ.get("SYSINSIGHT_DBMS", "postgresql"))
    parser.add_argument("--db-version", default=os.environ.get("SYSINSIGHT_DB_VERSION", "12"))
    parser.add_argument("--list-profiles", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="只生成 profile prompt，不调用模型接口")
    parser.add_argument(
        "--sysinsight-input",
        default="",
        help="canonical sysinsight_input.json produced by sysinsight_pipeline.py",
    )
    parser.add_argument("--api-base", default=os.environ.get("SYSINSIGHT_GPT_BASE_URL", DEFAULT_API_BASE))
    parser.add_argument("--model", default=os.environ.get("SYSINSIGHT_GPT_MODEL", DEFAULT_MODEL))
    parser.add_argument("--n-candidates", type=int, default=10)
    parser.add_argument("--n-templates", type=int, default=2)
    parser.add_argument("--alpha", type=float, default=0.1)
    parser.add_argument(
        "--sync-transport",
        action="store_true",
        help="用同步 OpenAI 客户端承载原始 LLM_ACQ 调用；不改变源 prompt/解析/过滤逻辑",
    )
    parser.add_argument(
        "--run-source-selector",
        action="store_true",
        help="在原始候选生成之后，继续调用原始 LLM_DIS_SM 选择下一点并记录选择 API",
    )
    parser.add_argument("--selector-n-gens", type=int, default=1)
    parser.add_argument("--output", default="")
    return parser.parse_args()


def json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return str(value)


def api_json(api_base: str, api_key: str, path: str) -> Dict[str, Any]:
    request = urllib.request.Request(
        api_base.rstrip("/") + path,
        headers={"Authorization": "Bearer " + api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", errors="replace")
        raise RuntimeError("{} HTTP {}: {}".format(path, exc.code, detail))


def resolve_model(api_base: str, api_key: str, requested: str) -> Tuple[str, Dict[str, Any]]:
    payload = api_json(api_base, api_key, "/models")
    model_ids = [
        item.get("id")
        for item in payload.get("data", [])
        if isinstance(item, dict) and item.get("id")
    ]
    wanted = re.sub(r"[^a-z0-9]", "", requested.lower())
    for model_id in model_ids:
        if re.sub(r"[^a-z0-9]", "", model_id.lower()) == wanted:
            return model_id, {"requested": requested, "resolved": model_id, "model_ids": model_ids}
    raise RuntimeError("model {} is not listed; available models: {}".format(requested, model_ids))


def read_meminfo() -> Dict[str, float]:
    result: Dict[str, float] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                result[parts[0].rstrip(":")] = float(parts[1]) * 1024.0
    except (OSError, ValueError):
        return result
    return result


def read_cpu_stat() -> Tuple[int, int]:
    line = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0]
    values = [int(item) for item in line.split()[1:]]
    return sum(values), values[3] if len(values) > 3 else 0


def host_cpu_percent() -> float:
    try:
        total_a, idle_a = read_cpu_stat()
        time.sleep(0.2)
        total_b, idle_b = read_cpu_stat()
        total_delta = total_b - total_a
        idle_delta = idle_b - idle_a
        if total_delta > 0:
            return round((total_delta - idle_delta) * 100.0 / total_delta, 2)
    except (OSError, ValueError, IndexError):
        pass
    return 0.0


def resource_snapshot(case: Dict[str, Any]) -> Dict[str, Any]:
    """Produce real resource values for the source prompt input."""

    samples = case.get("anomaly", {}).get("samples", {})
    first = samples.get("first") or {}
    last = samples.get("last") or {}
    elapsed = float(last.get("elapsed_seconds", 0.0) or 0.0)
    if elapsed <= 0:
        elapsed = 1.0
    read_delta = float(last.get("blks_read", 0) or 0) - float(first.get("blks_read", 0) or 0)
    temp_delta = float(last.get("temp_bytes", 0) or 0) - float(first.get("temp_bytes", 0) or 0)
    hits = float(last.get("blks_hit", 0) or 0) - float(first.get("blks_hit", 0) or 0)
    total_buffers = hits + max(0.0, read_delta)
    mem = read_meminfo()
    total_mem = mem.get("MemTotal", 0.0)
    available_mem = mem.get("MemAvailable", 0.0)
    hit_rate = (hits * 100.0 / total_buffers) if total_buffers > 0 else 0.0
    return {
        "cpu": host_cpu_percent(),
        "readIO": round(max(0.0, read_delta) / elapsed, 4),
        "writeIO": round(max(0.0, temp_delta) / elapsed, 4),
        "virtualMem": round(total_mem / (1024.0 ** 3), 4) if total_mem else 0.0,
        "physical": round((total_mem - available_mem) / (1024.0 ** 3), 4) if total_mem else 0.0,
        "hit": round(hit_rate, 4),
        "source": "actual /proc and the recorded PostgreSQL anomaly samples",
    }


def _compact_prometheus_record(record: Any) -> Dict[str, Any]:
    """Keep live metrics useful to the model without copying raw series."""

    if not isinstance(record, dict):
        return {"status": "missing"}
    compact: Dict[str, Any] = {
        "status": record.get("status"),
        "result_type": record.get("result_type"),
        "series": [],
    }
    for series in record.get("series", [])[:8]:
        if not isinstance(series, dict):
            continue
        values = series.get("values", []) or []
        compact["series"].append({
            "labels": series.get("labels", {}),
            "first": values[0] if values else None,
            "last": values[-1] if values else None,
        })
    if record.get("error"):
        compact["error"] = record.get("error")
    return compact


def compact_sysinsight_observation(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the canonical eight-part input relevant to a tuning decision."""

    database_metrics = payload.get("database_metrics", {})
    if not isinstance(database_metrics, dict):
        database_metrics = {}
    prometheus_database = database_metrics.get("prometheus", {})
    if not isinstance(prometheus_database, dict):
        prometheus_database = {}
    host_metrics = payload.get("host_metrics", {})
    if not isinstance(host_metrics, dict):
        host_metrics = {}
    functions = payload.get("function_anomalies", {})
    if not isinstance(functions, dict):
        functions = {}
    alert = payload.get("alert", {})
    if not isinstance(alert, dict):
        alert = {}
    if "selected" in alert or "alert_status" in alert:
        alert_status = alert.get("alert_status")
        selected_alert = alert.get("selected")
    else:
        # The canonical top-level input stores the selected alert directly;
        # the step representation wraps it in {selected, alert_status}.
        alert_status = "completed" if alert else "not_collected"
        selected_alert = alert
    if isinstance(selected_alert, dict):
        selected_alert = {
            key: selected_alert.get(key)
            for key in ("triggered", "at", "elapsed_seconds", "source", "alert_name", "alert")
            if key in selected_alert
        }
    return {
        "schema": payload.get("schema"),
        "database": payload.get("database"),
        "workload": payload.get("workload"),
        "time_window": payload.get("time_window"),
        "alert": {
            "status": alert_status,
            "selected": selected_alert,
        },
        "host_metrics": {
            name: _compact_prometheus_record(record)
            for name, record in host_metrics.items()
        },
        "database_metrics": {
            name: _compact_prometheus_record(record)
            for name, record in prometheus_database.items()
        },
        "function_anomalies": {
            "function_count": functions.get("function_count"),
            "key_functions": functions.get("key_functions", [])[:20],
            "matched_knobs": functions.get("matched_knobs", [])[:50],
        },
        "tuning_context": payload.get("tuning_context", {}),
    }


def case_path(path_text: str) -> Path:
    path = Path(path_text).resolve()
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def original_context(
    case_result: Dict[str, Any], case_file: Path, profile: DatabaseProfile,
    sysinsight_input: Optional[Dict[str, Any]] = None,
) -> Tuple[Any, Any, Any, Dict[str, Any], Dict[str, Any]]:
    """Build the same task/config objects used by the original SysInsight entry."""

    sys.path.insert(0, str(PYDEPS))
    sys.path.insert(0, str(SOURCE_ROOT))
    from llambo.extract_knob import ParameterLibrary  # type: ignore

    constraints = profile.constraints()
    defaults = profile.defaults()
    initial_config = profile.initial_config()
    with (SOURCE_ROOT / "db_configurations/task/dbtune.json").open(encoding="utf-8") as handle:
        task_context = json.load(handle)

    # This is the original main.py task-context mutation for TPCC.
    task_context["workload"] = "tpcc"
    task_context["task"] = ""
    task_context["lower_is_better"] = False
    profile_context = profile.task_fragment()
    task_context["sysinsight_profile"] = profile_context
    task_context.update(profile_context)
    task_context["dbms"] = profile.dbms
    task_context["model"] = profile.dbms
    task_context["prompt_metric"] = "transaction per second"
    task_context["hyperparameter_constraints"] = constraints
    task_context["hyperparameter_default"] = defaults
    compact_observation = compact_sysinsight_observation(sysinsight_input) if sysinsight_input else None
    if compact_observation:
        task_context["sysinsight_observation"] = compact_observation

    source_compare = case_result.get("sysinsight_source_detection", {}).get("source_compare", {})
    key_file = Path(source_compare.get("key_function_file", ""))
    if not key_file.is_absolute():
        key_file = (ROOT / key_file).resolve()
    if not key_file.exists():
        # The source detector stores a relative path under the case directory.
        key_file = (case_file.parent / key_file.name).resolve()
    if not key_file.exists():
        raise FileNotFoundError("original SysInsight key-function file: {}".format(key_file))

    promptlib = ParameterLibrary(task_context)
    promptlib.config = initial_config
    promptlib.keyFunction_file = str(key_file)
    promptlib.resource = resource_snapshot(case_result)
    if compact_observation:
        promptlib.question_template += (
            "\n\n10. Canonical live SysInsight observation captured for this tuning decision "
            "(use it together with the perf/source evidence above):\n"
            + json.dumps(compact_observation, ensure_ascii=False, indent=2, default=str)
        )
    return promptlib, task_context, initial_config, constraints, defaults


def object_to_dict(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): object_to_dict(v) for k, v in value.items()}
    if isinstance(value, list):
        return [object_to_dict(v) for v in value]
    if hasattr(value, "to_dict_recursive"):
        return object_to_dict(value.to_dict_recursive())
    if hasattr(value, "to_dict"):
        try:
            return object_to_dict(value.to_dict())
        except Exception:
            pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def extract_api_generated_configurations(calls: List[Dict[str, Any]], converter: Any) -> List[Dict[str, Any]]:
    """Parse every returned configuration with SysInsight's original parser.

    ``candidate_points`` is the source implementation's post-filter
    DataFrame.  That DataFrame may contain profile defaults for fields omitted
    by the model.  This separate trace is intentionally based only on the
    text returned by the supplied API, so a default-filled source candidate
    can never be mistaken for an API-generated value.
    """

    generated: List[Dict[str, Any]] = []
    for call_index, call in enumerate(calls):
        response = call.get("response", {}) or {}
        choices = response.get("choices", []) if isinstance(response, dict) else []
        for choice_index, choice in enumerate(choices):
            message = choice.get("message", {}) if isinstance(choice, dict) else {}
            content = message.get("content", "") if isinstance(message, dict) else ""
            if not isinstance(content, str):
                content = str(content)
            marker = "## configuration ##"
            if marker not in content:
                generated.append({
                    "call_index": call_index,
                    "phase": call.get("phase", "unknown"),
                    "choice_index": choice_index,
                    "raw_content": content,
                    "configuration_section": None,
                    "parsed_by_source_LLM_ACQ__convert_to_json": None,
                    "parse_status": "missing_configuration_marker",
                })
                continue
            section = content.split(marker, 1)[1].strip()
            try:
                parsed = converter(section)
                status = "parsed"
            except Exception as exc:
                parsed = None
                status = "source_parser_error: {}: {}".format(type(exc).__name__, exc)
            generated.append({
                "call_index": call_index,
                "phase": call.get("phase", "unknown"),
                "choice_index": choice_index,
                "raw_content": content,
                "configuration_section": section,
                "parsed_by_source_LLM_ACQ__convert_to_json": object_to_dict(parsed),
                "parse_status": status,
            })
    return generated


def main() -> int:
    args = parse_args()
    if args.list_profiles:
        print("\n".join(available_profiles()))
        return 0
    if not args.case_result:
        raise SystemExit("--case-result is required unless --list-profiles is used")
    api_key = api_key_from_environment()
    if not api_key and not args.dry_run:
        raise SystemExit("SYSINSIGHT_GPT_API_KEY is required")
    if args.n_candidates <= 0 or args.n_templates <= 0:
        raise SystemExit("n-candidates and n-templates must be positive")

    profile = resolve_profile(args.dbms, args.db_version)
    case_file = case_path(args.case_result)
    case_result = json.loads(case_file.read_text(encoding="utf-8"))
    sysinsight_input: Optional[Dict[str, Any]] = None
    sysinsight_input_path: Optional[Path] = None
    if args.sysinsight_input:
        sysinsight_input_path = case_path(args.sysinsight_input)
        loaded_input = json.loads(sysinsight_input_path.read_text(encoding="utf-8"))
        if not isinstance(loaded_input, dict):
            raise ValueError("sysinsight input must be a JSON object")
        sysinsight_input = loaded_input
    requested_output_dir = Path(args.output).resolve() if args.output else None
    if args.dry_run:
        resolved_model = args.model
        model_info: Dict[str, Any] = {"dry_run": True}
    else:
        resolved_model, model_info = resolve_model(args.api_base, api_key, args.model)

    # The source modules read these variables at import time.
    os.environ["OPENAI_API_TYPE"] = "open_ai"
    os.environ["OPENAI_API_VERSION"] = ""
    os.environ["OPENAI_API_BASE"] = args.api_base.rstrip("/")
    os.environ["OPENAI_API_KEY"] = api_key
    sys.path.insert(0, str(PYDEPS))
    sys.path.insert(0, str(SOURCE_ROOT))

    import pandas as pd  # type: ignore
    import openai  # type: ignore
    from llambo.acquisition_function import LLM_ACQ  # type: ignore
    import llambo.extract_knob as extract_knob  # type: ignore

    promptlib, task_context, initial_config, constraints, defaults = original_context(
        case_result, case_file, profile, sysinsight_input
    )

    # Run the original parameter-to-function/rule matching method.  The source
    # prompt's auxiliary analyzer otherwise calls its own legacy hard-coded API
    # when a cache entry is absent.  Keep the source prompt and all fields, but
    # stop that unrelated endpoint from being contacted.
    original_analyzer = extract_knob.SimpleParameterAnalyzer

    class NoLegacyEndpointAnalyzer:
        def __init__(self) -> None:
            pass

        def extract_instructions_by_param(self, *unused_args: Any, **unused_kwargs: Any) -> str:
            return ""

    link = Path("/home/sysinsight")
    created_link = False
    if not link.exists():
        link.symlink_to(SOURCE_ROOT, target_is_directory=True)
        created_link = True
    source_cwd = Path.cwd()
    artifact_dir: Optional[Path] = None
    calls: List[Dict[str, Any]] = []
    acq: Any = None
    selector: Any = None
    selector_result: Optional[Dict[str, Any]] = None
    try:
        os.chdir(SOURCE_ROOT)
        # The source methods are intentionally noisy (they print every rule
        # candidate and every missing configuration key).  Capture that
        # diagnostic stream in the artifact while keeping the terminal output
        # useful for a long-running API call.
        source_stdout = io.StringIO()
        with contextlib.redirect_stdout(source_stdout):
            promptlib.update()
            extract_knob.SimpleParameterAnalyzer = NoLegacyEndpointAnalyzer
            prompt_text_escaped = promptlib.get_prompt()
        prompt_text = prompt_text_escaped.replace("<hzt<", "{").replace(">hzt>", "}")

        baseline_score = case_result.get("baseline_metrics", {}).get("tps")
        if not isinstance(baseline_score, (int, float)):
            raise RuntimeError("case result has no measured baseline TPS")
        observed_configs = pd.DataFrame([initial_config])
        observed_fvals = pd.DataFrame([{"score": float(baseline_score)}])

        run_stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        if requested_output_dir is not None:
            artifact_dir = requested_output_dir
        else:
            artifact_dir = ROOT / "results" / "sysinsight_original_llm" / run_stamp
        artifact_dir.mkdir(parents=True, exist_ok=False)
        (artifact_dir / "source_prompt.txt").write_text(prompt_text, encoding="utf-8")
        (artifact_dir / "source_prompt_escaped.txt").write_text(prompt_text_escaped, encoding="utf-8")

        if args.dry_run:
            candidates = pd.DataFrame()
            total_cost = 0.0
            elapsed = 0.0
        else:
            original_acreate = openai.ChatCompletion.acreate
            original_create = openai.ChatCompletion.create

            async def sync_transport(*call_args: Any, **call_kwargs: Any) -> Any:
                """Run the same API request through the sync client in a worker thread.

                The source LLM_ACQ still owns prompt construction, retry behavior,
                response parsing, range filtering, and candidate selection.  This
                is only an opt-in transport compatibility path for the supplied
                OpenAI-compatible endpoint.
                """

                loop = asyncio.get_event_loop()
                return await loop.run_in_executor(
                    None,
                    lambda: original_create(*call_args, **call_kwargs),
                )

            async def logged_acreate(*call_args: Any, **call_kwargs: Any) -> Any:
                request_record = {
                    "model": call_kwargs.get("model"),
                    "temperature": call_kwargs.get("temperature"),
                    "top_p": call_kwargs.get("top_p"),
                    "n": call_kwargs.get("n"),
                    "messages": call_kwargs.get("messages"),
                }
                started = time.time()
                try:
                    if args.sync_transport:
                        response = await sync_transport(*call_args, **call_kwargs)
                    else:
                        response = await original_acreate(*call_args, **call_kwargs)
                except Exception as exc:
                    calls.append(
                        {
                            "phase": current_api_phase,
                            "request": request_record,
                            "error": "{}: {}".format(type(exc).__name__, str(exc)),
                            "elapsed_seconds": round(time.time() - started, 3),
                        }
                    )
                    raise
                calls.append(
                    {
                        "phase": current_api_phase,
                        "request": request_record,
                        "response": object_to_dict(response),
                        "elapsed_seconds": round(time.time() - started, 3),
                    }
                )
                return response

            openai.ChatCompletion.acreate = logged_acreate
            os.chdir(artifact_dir)
            current_api_phase = "acquisition"
            acq = LLM_ACQ(
                task_context,
                n_candidates=args.n_candidates,
                n_templates=args.n_templates,
                lower_is_better=False,
                jitter=False,
                chat_engine=resolved_model,
                prompt_setting="full_context",
                shuffle_features=False,
            )
            try:
                with contextlib.redirect_stdout(source_stdout):
                    candidates, total_cost, elapsed = acq.get_candidate_points(
                        observed_configs,
                        observed_fvals,
                        alpha=args.alpha,
                        config=promptlib,
                    )
                if args.run_source_selector:
                    from llambo.discriminative_sm import LLM_DIS_SM  # type: ignore

                    current_api_phase = "source_selector"
                    selector = LLM_DIS_SM(
                        task_context,
                        n_gens=args.selector_n_gens,
                        lower_is_better=False,
                        n_templates=1,
                        chat_engine=resolved_model,
                        prompt_setting="full_context",
                        shuffle_features=False,
                    )
                    selected_point, selector_cost, selector_elapsed = selector.select_query_point(
                        observed_configs,
                        observed_fvals,
                        candidates,
                        config=promptlib,
                    )
                    selector_result = {
                        "source_method": "llambo.discriminative_sm.LLM_DIS_SM.select_query_point",
                        "candidate_count": int(len(candidates)),
                        "selected_row_index": int(selected_point.index[0]),
                        "selected_source_materialized_configuration": object_to_dict(
                            selected_point.to_dict(orient="records")
                        ),
                        "cost_as_reported_by_source": selector_cost,
                        "elapsed_seconds": selector_elapsed,
                    }
            except Exception as exc:
                # Preserve the real request/response evidence even when the
                # original source loop cannot produce five accepted points.
                partial = {
                    "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "profile": profile_summary(profile),
                    "api": {
                        "base_url": args.api_base,
                        "requested_model": args.model,
                        "resolved_model": resolved_model,
                        "model_lookup": model_info,
                        "api_key_saved": False,
                    },
                    "transport": "sync_compatibility" if args.sync_transport else "source_async",
                    "source_prompt": prompt_text,
                    "sysinsight_input": str(sysinsight_input_path) if sysinsight_input_path else None,
                    "canonical_observation": compact_sysinsight_observation(sysinsight_input)
                    if sysinsight_input else None,
                    "source_stdout": source_stdout.getvalue(),
                    "llm_calls": calls,
                    "api_generated_configurations": extract_api_generated_configurations(
                        calls, acq._convert_to_json
                    ) if acq is not None else [],
                    "error": "{}: {}".format(type(exc).__name__, str(exc)),
                }
                (artifact_dir / "partial_result.json").write_text(
                    json.dumps(partial, ensure_ascii=False, indent=2, default=json_default),
                    encoding="utf-8",
                )
                raise
        result = {
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "profile": profile_summary(profile),
            "source": {
                "repo": str(SOURCE_ROOT),
                "acquisition_module": str(SOURCE_ROOT / "llambo/acquisition_function.py"),
                "parameter_library_module": str(SOURCE_ROOT / "llambo/extract_knob.py"),
                "original_parameter_library_update": True,
                "profile_switch": True,
                "legacy_analyzer_endpoint_suppressed": True,
                "legacy_analyzer_reason": "source module contains a separate hard-coded endpoint; it was not the supplied API",
            },
            "input": {
                "case_result": str(case_file),
                "sysinsight_input": str(sysinsight_input_path) if sysinsight_input_path else None,
                "profile": profile_summary(profile),
                "measured_baseline_tps": float(baseline_score),
                "initial_config": initial_config,
                "hyperparameters_from_original_update": promptlib.hyperparameters,
                "matched_knob_entries": promptlib.store_updateKnobs,
                "resource_for_prompt": promptlib.resource,
                "canonical_observation": compact_sysinsight_observation(sysinsight_input)
                if sysinsight_input else None,
                "task_context": task_context,
            },
            "api": {
                "base_url": args.api_base,
                "requested_model": args.model,
                "resolved_model": resolved_model,
                "model_lookup": model_info,
                "api_key_saved": False,
            },
            "original_acquisition_settings": {
                "n_candidates": args.n_candidates,
                "n_templates": args.n_templates,
                "n_gens": int(args.n_candidates / args.n_templates),
                "alpha": args.alpha,
                "temperature": 0.8,
                "top_p": 0.95,
                "system_message": "You are an AI assistant that helps people find information.",
                "dry_run": args.dry_run,
            },
            "transport": "sync_compatibility" if args.sync_transport else "source_async",
            "source_prompt": prompt_text,
            "source_stdout": source_stdout.getvalue(),
            "llm_calls": calls,
            "api_generated_configurations": extract_api_generated_configurations(
                calls, acq._convert_to_json
            ) if acq is not None else [],
            "candidate_points": object_to_dict(candidates),
            "source_materialized_candidate_points": object_to_dict(candidates.to_dict(orient="records"))
            if hasattr(candidates, "to_dict") else object_to_dict(candidates),
            "source_selector": selector_result,
            "total_cost_as_reported_by_source": total_cost,
            "acquisition_elapsed_seconds": elapsed,
        }
        (artifact_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=json_default), encoding="utf-8"
        )
        print(json.dumps({
            "artifact_dir": str(artifact_dir),
            "profile": profile.name,
            "resolved_model": resolved_model,
            "hyperparameters": promptlib.hyperparameters,
            "llm_call_count": len(calls),
            "candidate_count": len(candidates),
            "candidates": object_to_dict(candidates),
        }, ensure_ascii=False, indent=2, default=json_default))
        return 0
    finally:
        extract_knob.SimpleParameterAnalyzer = original_analyzer
        os.chdir(source_cwd)
        if created_link and link.is_symlink() and link.resolve() == SOURCE_ROOT.resolve():
            link.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
