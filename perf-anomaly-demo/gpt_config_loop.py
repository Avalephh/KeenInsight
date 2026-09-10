#!/usr/bin/env python3
"""Call the supplied OpenAI-compatible API and retest its configuration.

The input is an already completed real TPCC/SysInsight result.  The model sees
the measured anomaly and original SysInsight function output, returns one
session-level PostgreSQL setting, and that setting is passed to the TPCC
runner via --tuned-override.  The API key is read from the environment and is
never written to the result artifacts.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parent
DEFAULT_OBSERVED = ROOT / "results" / "tpcc_external" / "20260910_001807" / "c10_window_spill" / "case_result.json"
DEFAULT_API_BASE = "http://35.212.195.134:28317/v1"
REQUESTED_MODEL = "GPT5.6-SOL"
ALLOWED_PARAMETERS = {
    "work_mem",
    "maintenance_work_mem",
    "temp_buffers",
    "max_parallel_workers_per_gather",
    "parallel_setup_cost",
    "parallel_tuple_cost",
    "random_page_cost",
    "jit",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--model", default=REQUESTED_MODEL)
    parser.add_argument("--api-key", default="", help="prefer SYSINSIGHT_GPT_API_KEY")
    parser.add_argument("--observed-result", default=str(DEFAULT_OBSERVED))
    parser.add_argument("--case", default="c10_window_spill")
    parser.add_argument("--baseline-duration", type=int, default=8)
    parser.add_argument("--case-duration", type=int, default=35)
    parser.add_argument("--tuned-duration", type=int, default=15)
    parser.add_argument("--normal-clients", type=int, default=2)
    parser.add_argument("--perf-frequency", type=int, default=300)
    return parser.parse_args()


def api_request(
    api_base: str, api_key: str, path: str, method: str = "GET", payload: Optional[Dict[str, Any]] = None
) -> Tuple[int, str]:
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        api_base.rstrip("/") + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def model_id(api_base: str, api_key: str, requested: str) -> Tuple[str, Dict[str, Any]]:
    status, body = api_request(api_base, api_key, "/models")
    if status != 200:
        raise RuntimeError("/models HTTP {}: {}".format(status, body[:1000]))
    response = json.loads(body)
    models = response.get("data", [])
    ids = [item.get("id") for item in models if isinstance(item, dict) and item.get("id")]
    wanted = re.sub(r"[^a-z0-9]", "", requested.lower())
    for item in ids:
        if re.sub(r"[^a-z0-9]", "", item.lower()) == wanted:
            return item, {"http_status": status, "model_ids": ids}
    raise RuntimeError("model {} is not listed; available models: {}".format(requested, ids))


def prompt_for(observed: Dict[str, Any]) -> str:
    anomaly = observed.get("anomaly", {})
    metrics = anomaly.get("metrics", {})
    samples = anomaly.get("samples", {})
    improvement = observed.get("improvement", {})
    source = observed.get("sysinsight_source_detection", {}).get("source_compare", {})
    top_functions: List[Dict[str, Any]] = []
    for item in source.get("key_functions", [])[:12]:
        if isinstance(item, dict):
            top_functions.append(
                {
                    "Function": item.get("Function"),
                    "Sample Rate": item.get("Sample Rate"),
                    "Diff From Mean": item.get("Diff From Mean"),
                    "Change": item.get("Change"),
                }
            )
    before = observed.get("before_settings", {})
    return """你是 PostgreSQL 性能调优助手。请基于一次已经真实完成的 TPCC、Prometheus、perf 和原始 SysInsight 检测结果，生成下一次测试使用的一个会话级 PostgreSQL 配置。

数据库：keeninsight；TPCC schema：keeninsight_tpcc。
当前全局参数（异常阶段保持不变）：{before}
Prometheus 告警：{trigger}
原始 SysInsight 异常函数数量：{function_count}
原始 SysInsight top 函数：{top_functions}
异常阶段外部 TPCC 指标：{metrics}
异常阶段资源增量：{deltas}

约束：
1. 只能推荐一个 PostgreSQL 会话级 SET 参数，不得 ALTER SYSTEM、重启、改表结构或改数据。
2. 只能从 work_mem、maintenance_work_mem、temp_buffers、max_parallel_workers_per_gather、parallel_setup_cost、parallel_tuple_cost、random_page_cost、jit 中选择。
3. 值必须适合 PostgreSQL 12，并且必须针对本次 TPCC 异常的观测证据。
4. 只返回 JSON，不要 Markdown，不要额外解释，格式必须是：
{{"parameter":"...","value":"...","sql_set":"SET ...;","reason":"...","confidence":0.0}}
""".format(
        before=json.dumps(before, ensure_ascii=False),
        trigger=bool(samples.get("trigger")),
        function_count=source.get("key_function_count"),
        top_functions=json.dumps(top_functions, ensure_ascii=False),
        metrics=json.dumps(metrics, ensure_ascii=False),
        deltas=json.dumps(observed.get("metric_deltas", {}), ensure_ascii=False),
    )


def parse_json_content(content: str) -> Dict[str, Any]:
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    elif not (text.startswith("{") and text.endswith("}")):
        match = re.search(r"(\{.*\})", text, re.DOTALL)
        if not match:
            raise ValueError("model did not return a JSON object")
        text = match.group(1)
    result = json.loads(text)
    if not isinstance(result, dict):
        raise ValueError("model JSON is not an object")
    return result


def validate_config(config: Dict[str, Any]) -> Dict[str, str]:
    parameter = str(config.get("parameter", "")).strip()
    value = str(config.get("value", "")).strip().strip("'").strip('"')
    if parameter not in ALLOWED_PARAMETERS:
        raise ValueError("model returned unsupported parameter: {}".format(parameter))
    if not re.fullmatch(r"[A-Za-z0-9_.+-]+", value):
        raise ValueError("model returned unsafe or empty value")
    return {
        "parameter": parameter,
        "value": value,
        "sql_set": "SET {} = '{}';".format(parameter, value),
    }


def main() -> int:
    args = parse_args()
    api_key = args.api_key or os.environ.get("SYSINSIGHT_GPT_API_KEY", "")
    if not api_key:
        raise SystemExit("set SYSINSIGHT_GPT_API_KEY or pass --api-key")
    observed_path = Path(args.observed_result).resolve()
    observed = json.loads(observed_path.read_text(encoding="utf-8"))
    resolved_model, model_info = model_id(args.api_base, api_key, args.model)
    prompt = prompt_for(observed)
    status, body = api_request(
        args.api_base,
        api_key,
        "/chat/completions",
        method="POST",
        payload={
            "model": resolved_model,
            "messages": [
                {"role": "system", "content": "Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 500,
        },
    )
    if status != 200:
        raise RuntimeError("/chat/completions HTTP {}: {}".format(status, body[:2000]))
    response = json.loads(body)
    choices = response.get("choices") or []
    if not choices:
        raise RuntimeError("chat response has no choices")
    content = ((choices[0].get("message") or {}).get("content") or "").strip()
    raw_config = parse_json_content(content)
    config = validate_config(raw_config)

    artifact_dir = ROOT / "results" / "gpt_config_loop" / dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    artifact_dir.mkdir(parents=True, exist_ok=False)
    (artifact_dir / "gpt_prompt.txt").write_text(prompt, encoding="utf-8")
    (artifact_dir / "gpt_response.json").write_text(
        json.dumps(
            {
                "api_base": args.api_base,
                "requested_model": args.model,
                "resolved_model": resolved_model,
                "models_lookup": model_info,
                "http_status": status,
                "usage": response.get("usage"),
                "raw_content": content,
                "raw_config": raw_config,
                "validated_config": config,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    command = [
        sys.executable,
        str(ROOT / "tpcc_external_cases.py"),
        "--only",
        args.case,
        "--tuned-override",
        "{}={}".format(config["parameter"], config["value"]),
        "--baseline-duration",
        str(args.baseline_duration),
        "--case-duration",
        str(args.case_duration),
        "--tuned-duration",
        str(args.tuned_duration),
        "--normal-clients",
        str(args.normal_clients),
        "--perf-frequency",
        str(args.perf_frequency),
    ]
    completed = subprocess.run(
        command,
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    (artifact_dir / "retest.log").write_text(completed.stdout, encoding="utf-8")
    matches = re.findall(r"结果目录：([^\r\n]+)", completed.stdout)
    retest_dir = Path(matches[-1].strip()) if matches else None
    retest_case: Optional[Dict[str, Any]] = None
    if retest_dir is not None:
        result_path = retest_dir / args.case / "case_result.json"
        if result_path.exists():
            retest_case = json.loads(result_path.read_text(encoding="utf-8"))
    final = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "observed_result": str(observed_path),
        "case": args.case,
        "gpt_config": config,
        "retest_command": command,
        "retest_returncode": completed.returncode,
        "retest_run_dir": str(retest_dir) if retest_dir else None,
        "retest_case_result": retest_case,
    }
    (artifact_dir / "gpt_loop_result.json").write_text(
        json.dumps(final, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    print("GPT API 调用成功，模型：{}".format(resolved_model))
    print("GPT 返回配置：{}".format(config["sql_set"]))
    print("复测返回码：{}".format(completed.returncode))
    print("证据目录：{}".format(artifact_dir))
    if retest_dir:
        print("复测结果目录：{}".format(retest_dir))
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
