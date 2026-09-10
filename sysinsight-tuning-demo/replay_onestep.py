#!/usr/bin/env python3
"""Replay the observable, no-database side of SysInsight's one-step flow.

The anomaly comparison and function-to-knob matching are imported from the
vendored SysInsight source.  The recorded candidate configuration is reused
from the repository artifact, so this script never changes a database or
invokes the original DBTune benchmark loop.

Use --live-api only when a fresh model prediction is wanted.  The API key is
read from SYSINSIGHT_API_KEY and is never written to disk.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "vendor"
INPUTS = ROOT / "inputs"
RESULTS = ROOT / "results"

DEFAULT_BASE_URL = "http://35.212.195.134:28317/v1"
DEFAULT_MODEL = "gpt-5.6-sol"

sys.path.insert(0, str(VENDOR))
from DBTuner.utils.analyzeException import analyze_exception  # noqa: E402
from DBTuner.utils.matchFunctions import (  # noqa: E402
    find_top_and_matched_functions,
)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def replay_source_analysis() -> dict[str, Any]:
    """Run the original snapshot's file-based analysis in the demo folder."""

    current_perf = INPUTS / "current_perf.tsv"
    normal_profile = INPUTS / "normal_profile_sysbench.csv"
    resource_file = INPUTS / "resource.json"
    static_library = VENDOR / "DBTuner/utils/paramater_association_library.json"

    # analyze_exception() uses relative paths for its task/knob metadata.
    previous_cwd = Path.cwd()
    os.chdir(VENDOR)
    try:
        metrics, _task_context, key_file, source_resource = analyze_exception(
            "oltp",
            "mysql",
            677,
            str(current_perf),
            str(normal_profile),
            str(resource_file),
        )
    finally:
        os.chdir(previous_cwd)

    key_functions, matched_knobs, function_to_knob = find_top_and_matched_functions(
        key_file, str(static_library)
    )

    return {
        "metrics": metrics,
        "source_compatible_resource": source_resource,
        "key_function_file": str(Path(key_file).relative_to(ROOT)),
        "key_function_count": len(key_functions),
        "key_functions_top10": key_functions[:10],
        "matched_knob_count": len(matched_knobs),
        "matched_knobs": [item.get("knob_name") for item in matched_knobs],
        "function_to_knob_sample": dict(list(function_to_knob.items())[:20]),
    }


def build_prediction_prompt(base_config: dict[str, Any], new_config: dict[str, Any], base_score: float) -> str:
    """Keep the final prediction prompt equivalent to predictMetric.py."""

    return f"""You are an expert in database performance tuning.

We have a baseline database configuration and its measured performance score.
The new configuration has NOT been executed yet.

Baseline configuration:
{json.dumps(base_config, indent=2)}

Baseline performance score:
{base_score}

New configuration:
{json.dumps(new_config, indent=2)}

Respond ONLY with a JSON object:
{{
  "delta_score": <float>,
  "reasoning": "<short technical explanation>"
}}
"""


def parse_model_json(content: str) -> dict[str, Any]:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, flags=re.DOTALL)
        if not match:
            raise
        data = json.loads(match.group(0))
    if "delta_score" not in data:
        raise ValueError(f"Model response has no delta_score: {data}")
    return data


def predict_with_api(base_config: dict[str, Any], new_config: dict[str, Any], base_score: float) -> dict[str, Any]:
    api_key = os.environ.get("SYSINSIGHT_API_KEY")
    if not api_key:
        raise RuntimeError("--live-api requires SYSINSIGHT_API_KEY; it is not read from a file")

    base_url = os.environ.get("SYSINSIGHT_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    model = os.environ.get("SYSINSIGHT_MODEL", DEFAULT_MODEL)
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": build_prediction_prompt(base_config, new_config, base_score)}],
            "temperature": 0.2,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read(500).decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM API returned HTTP {exc.code}: {detail}") from exc

    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError(f"LLM API returned no choices: {sorted(payload)}")
    content = (choices[0].get("message") or {}).get("content", "")
    parsed = parse_model_json(content)
    delta = float(parsed["delta_score"])
    return {
        "mode": "live_api",
        "model": payload.get("model", model),
        "delta_score": delta,
        "predicted_score": float(base_score) + delta,
        "reasoning": str(parsed.get("reasoning", "")),
        "usage": payload.get("usage"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live-api",
        action="store_true",
        help="send the recorded candidate pair to the configured OpenAI-compatible API",
    )
    args = parser.parse_args()

    snapshot = load_json(INPUTS / "onestep_snapshot.json")
    analysis = replay_source_analysis()
    base_config = snapshot["base_config"]
    recommended_config = snapshot["recommended_config"]
    base_score = float(snapshot["base_score"])

    if args.live_api:
        prediction = predict_with_api(base_config, recommended_config, base_score)
    else:
        prediction = {
            "mode": "recorded_artifact",
            "model": "unknown (recorded repository output)",
            "delta_score": float(snapshot["delta_score"]),
            "predicted_score": float(snapshot["predicted_score"]),
            "reasoning": snapshot.get("reasoning", ""),
            "usage": None,
        }

    changed = {
        key: {"base": base_config.get(key), "recommended": value}
        for key, value in recommended_config.items()
        if base_config.get(key) != value
    }
    result = {
        "replay_scope": {
            "database_touched": False,
            "monitoring_connected": False,
            "candidate_generation": "recorded repository artifact",
            "prediction": prediction["mode"],
        },
        "source_analysis": analysis,
        "candidate": {
            "base_score": base_score,
            "changed_parameters": changed,
            "base_config": base_config,
            "recommended_config": recommended_config,
        },
        "prediction": prediction,
    }

    RESULTS.mkdir(parents=True, exist_ok=True)
    output = RESULTS / ("live_replay_result.json" if args.live_api else "replay_result.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "output": str(output),
        "key_function_count": analysis["key_function_count"],
        "matched_knob_count": analysis["matched_knob_count"],
        "changed_parameters": list(changed),
        "prediction_mode": prediction["mode"],
        "delta_score": prediction["delta_score"],
        "predicted_score": prediction["predicted_score"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
