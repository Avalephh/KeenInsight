#!/usr/bin/env python3
"""Generate a root-cause / tuning-action explanation from retained evidence.

The selected LLM acquisition candidate already contains the reasoning used to
choose its parameters.  This tool turns that reasoning plus the actual
before/after settings, source evidence and replay comparison into a concise
Chinese explanation, and stores it next to the incident for the portal.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parent
DEFAULT_STATE_DB = Path("/root/new/monitoring/data/sysinsight_dream_bridge.sqlite3")
SCHEMA = 1
MODEL = "gpt-6.1-sol"


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def load_json(path: Path, default=None):
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    return default


def env_file_values() -> Dict[str, str]:
    path = Path("/etc/keeninsight/keeninsight.env")
    values = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                name, value = line.split("=", 1)
                values[name.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def api_settings():
    values = env_file_values()
    base = (os.environ.get("SYSINSIGHT_GPT_BASE_URL")
            or os.environ.get("SYSINSIGHT_API_BASE")
            or os.environ.get("OPENAI_BASE_URL")
            or values.get("KI_RAG_LLM_URL") or "").strip()
    key = (os.environ.get("SYSINSIGHT_GPT_API_KEY")
           or os.environ.get("SYSINSIGHT_API_KEY")
           or os.environ.get("OPENAI_API_KEY")
           or values.get("KI_RAG_LLM_TOKEN") or "").strip()
    model = (os.environ.get("SYSINSIGHT_GPT_MODEL")
             or os.environ.get("SYSINSIGHT_API_MODEL")
             or os.environ.get("OPENAI_MODEL")
             or values.get("KI_RAG_LLM_MODEL") or MODEL).strip()
    if not base or not key:
        return None
    base = base.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base, key, model


def parse_json(text: str) -> Optional[Dict[str, Any]]:
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def display_value(name: str, value: Any, pre: Dict[str, Any]) -> str:
    setting = pre.get(name) if isinstance(pre, dict) else None
    unit = (setting or {}).get("unit") or ""
    if value is None:
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if unit == "8kB":
        gib = number * 8192.0 / (1024.0 ** 3)
        return "{} (约 {:.1f} GiB)".format(int(number), gib)
    if unit:
        return "{} {}".format(int(number) if number.is_integer() else number, unit)
    return str(int(number) if number.is_integer() else number)


def selected_payload(api: Dict[str, Any], validation: Dict[str, Any]):
    selected = (validation or {}).get("selected_candidate") or {}
    call_index = selected.get("call_index")
    choice_index = selected.get("choice_index")
    selected_reasoning = ""
    for item in api.get("api_generated_configurations") or []:
        if item.get("call_index") == call_index and item.get("choice_index") == choice_index:
            selected_reasoning = str(item.get("raw_content") or "")
            break
    if not selected_reasoning:
        rows = api.get("api_generated_configurations") or []
        if rows:
            selected_reasoning = str(rows[0].get("raw_content") or "")
    return selected, selected_reasoning


def parameter_reason_map(reasoning: str, parameters: Iterable[str]) -> Dict[str, str]:
    """Best-effort mapping of reasoning paragraphs to parameter names."""
    paragraphs = []
    current = []
    for line in str(reasoning or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r"^\d+[.)]\s", stripped) and current:
            paragraphs.append(" ".join(current))
            current = []
        current.append(stripped)
    if current:
        paragraphs.append(" ".join(current))
    result = {}
    for parameter in parameters:
        tokens = {parameter.lower(), parameter.lower().replace("_", " ")}
        matches = [p for p in paragraphs if any(token in p.lower() for token in tokens)]
        if matches:
            result[parameter] = " ".join(matches)[:1600]
    return result


def evidence_map(source_evidence: Dict[str, Any]) -> Dict[str, List[str]]:
    result: Dict[str, List[str]] = {}
    for item in source_evidence.get("static_parameter_evidence") or []:
        if not isinstance(item, dict):
            continue
        knob = item.get("knob_name") or item.get("guc_variable")
        if not knob:
            continue
        text = "{} ({})".format(item.get("function") or "unknown",
                                "{}:{}".format(item.get("file"), item.get("line")))
        result.setdefault(str(knob), [])
        if text not in result[str(knob)]:
            result[str(knob)].append(text)
    return result


def same_value(before: Any, after: Any) -> bool:
    if before is None or after is None:
        return False

    def normalize(value):
        text = str(value).strip().strip("'").strip('"')
        try:
            return ("number", float(text))
        except ValueError:
            return ("text", text.lower())

    return normalize(before) == normalize(after)


def build_inputs(incident_dir: Path):
    live = load_json(incident_dir / "live_chain_result.json", {}) or {}
    api = load_json(incident_dir / "sysinsight_api" / "result.json", {}) or {}
    input_payload = load_json(incident_dir / "sysinsight_input.json", {}) or {}
    case = load_json(incident_dir / "case_result.json", {}) or {}
    application = live.get("temporary_application") or {}
    validation = live.get("candidate_validation") or {}
    selected, selected_reasoning = selected_payload(api, validation)
    comparison = live.get("comparison") or {}
    pre = application.get("pre_settings") or {}
    skipped = application.get("skipped_configuration") or {}
    requested = application.get("requested_configuration") or selected.get("configuration") or {}
    changed = list((validation.get("selected_candidate") or {}).get("validation", {}).get("changed_parameters") or [])
    if not changed:
        changed = list(requested.keys())
    evidence = evidence_map(live.get("source_evidence") or input_payload.get("source_evidence") or {})
    anomalies = (input_payload.get("function_anomalies") or {})
    hot = anomalies.get("key_functions") or (case.get("sysinsight_source_detection") or {}).get("source_compare", {}).get("key_functions") or []
    hot = hot[:8]
    actions = []
    actual_changed = []
    for parameter in changed:
        after = requested.get(parameter)
        before = (pre.get(parameter) or {}).get("setting")
        if same_value(before, after):
            continue
        actual_changed.append(parameter)
        actions.append({
            "parameter": parameter,
            "before": display_value(parameter, before, pre),
            "after": display_value(parameter, after, pre),
            "applied": parameter not in skipped,
            "skipped_reason": (skipped.get(parameter) or {}).get("reason"),
            "evidence": evidence.get(parameter, [])[:4],
        })
    return {
        "live": live,
        "application": application,
        "comparison": comparison,
        "selected_reasoning": selected_reasoning,
        "actions": actions,
        "hot_functions": hot,
        "pre": pre,
        "skipped": {k: v for k, v in skipped.items() if k in actual_changed},
        "changed": actual_changed,
        "model": (api.get("api") or {}).get("resolved_model") or (api.get("api") or {}).get("requested_model"),
    }


def fallback(bundle: Dict[str, Any], model: str) -> Dict[str, Any]:
    reasons = parameter_reason_map(bundle.get("selected_reasoning") or "", bundle.get("changed") or [])
    actions = []
    for action in bundle.get("actions") or []:
        parameter = action["parameter"]
        reason = reasons.get(parameter)
        if not reason:
            reason = "所选 LLM 候选给出了该参数变更，但候选理由没有单独展开；本项仍需复测确认收益。"
        actions.append(dict(action, reason=reason))
    skipped = []
    for parameter, value in (bundle.get("skipped") or {}).items():
        skipped.append({"parameter": parameter,
                        "reason": "该参数需要重启 PostgreSQL，在线实验未应用。原始原因：{}".format(value.get("reason") or "restart required")})
    hot = [f.get("Function") for f in bundle.get("hot_functions") or [] if isinstance(f, dict) and f.get("Function")]
    comparison = bundle.get("comparison") or {}
    validation = ""
    if comparison.get("baseline_ms") is not None:
        validation = "实测复测：基线 {} ms → 调优后 {} ms → 恢复后 {} ms。".format(
            round(comparison.get("baseline_ms"), 2),
            round(comparison.get("tuned_ms"), 2) if comparison.get("tuned_ms") is not None else "—",
            round(comparison.get("restored_ms"), 2) if comparison.get("restored_ms") is not None else "—",
        )
    return {
        "root_cause": "异常阶段的差分采样中，{} 等函数明显升高。以下调优动作来自本次 LLM 候选理由和源码关联证据；关联不代表因果，收益以复测结果为准。".format(
            "、".join(hot[:4]) or "热点函数"),
        "actions": actions,
        "not_applied": skipped,
        "validation": validation,
        "model": model,
    }


def request_explanation(bundle: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    settings = api_settings()
    if settings is None:
        return None
    base, key, model = settings
    prompt = (
        "You are writing a Chinese root-cause and tuning-action explanation for one PostgreSQL incident. "
        "Use only the supplied data. Distinguish measured evidence from LLM hypotheses; do not claim causality. "
        "For every changed parameter, explain in 1-2 Chinese sentences why the selected LLM candidate chose it, "
        "referring to the selected reasoning and the parameter/source evidence. If a parameter was skipped, put it "
        "under not_applied with the operational reason. Summarize the measured anomaly in root_cause using the "
        "top differential functions and the observed environment. Summarize baseline/tuned/restored in validation. "
        "Return JSON only with keys root_cause, actions, not_applied, validation. Each action must contain "
        "parameter, reason, evidence (list of strings).\n\n"
        "DATA:\n{}"
    ).format(json.dumps({
        "selected_llm_reasoning": str(bundle.get("selected_reasoning") or "")[:14000],
        "changed_actions": bundle.get("actions"),
        "skipped": bundle.get("skipped"),
        "top_differential_functions": bundle.get("hot_functions"),
        "comparison": bundle.get("comparison"),
        "candidate_model": bundle.get("model"),
    }, ensure_ascii=False, indent=2))
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Return valid JSON only. Be factual, concise and explicitly non-causal."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 2200,
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json",
                 "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    choices = body.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return None
    parsed = parse_json((choices[0].get("message") or {}).get("content") or "")
    if not parsed:
        return None
    parsed["model"] = model
    return parsed


def format_section(value: Any) -> str:
    labels = {
        "metric": "验证指标", "baseline_ms": "基线耗时", "tuned_ms": "调优后耗时",
        "restored_ms": "恢复后耗时", "tuned_relative_decrease_pct": "相对降低",
        "tuned_observed_reduction_pct": "观测相对降低",
        "restored_within_20pct_of_baseline": "恢复在基线 ±20% 内",
        "configuration_status": "配置状态", "interpretation": "解释",
        "limitations": "限制", "limits": "限制", "conclusion": "结论", "follow_up": "后续验证",
        "summary": "摘要", "scope_notes": "范围说明",
        "measured_evidence": "实测证据", "llm_hypotheses": "LLM 假设", "结论": "结论",
    }
    metric_labels = {"median_read_only_sql_duration_ms": "只读 SQL 耗时中位数（ms）"}
    status_labels = {"applied_and_restored": "已应用并恢复", "applied": "已应用",
                     "candidate_only": "仅候选", "failed": "失败"}
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            label = labels.get(str(key), str(key))
            if str(key) == "metric" and isinstance(item, str):
                item = metric_labels.get(item, item)
            if str(key) == "configuration_status" and isinstance(item, str):
                item = status_labels.get(item, item)
            if isinstance(item, list):
                lines.append("{}：".format(label))
                for entry in item:
                    if isinstance(entry, (dict, list)):
                        lines.append("- " + format_section(entry).replace("\n", "；"))
                    else:
                        lines.append("- {}".format(entry))
            elif isinstance(item, dict):
                lines.append("{}：{}".format(label, format_section(item)))
            elif isinstance(item, bool):
                lines.append("{}：{}".format(label, "是" if item else "否"))
            elif isinstance(item, float) and (str(key).endswith("_ms") or str(key).endswith("_pct")):
                lines.append("{}：{:.2f}".format(label, item))
            else:
                lines.append("{}：{}".format(label, item))
        return "\n".join(lines)
    if isinstance(value, list):
        return "\n".join("- {}".format(format_section(item) if isinstance(item, (dict, list)) else item)
                         for item in value)
    return str(value or "")


def normalize_terms(text: str) -> str:
    return (str(text or "")
            .replace("Sample Rate", "采样率")
            .replace("Diff From Mean", "相对均值差")
            .replace("TPS", "TPS"))


def skipped_reason_text(action: Dict[str, Any], generated_skipped: Dict[str, Dict[str, Any]]) -> str:
    generated = generated_skipped.get(action["parameter"]) or {}
    reason = str(generated.get("reason") or "").strip()
    if reason:
        return reason
    original = str(action.get("skipped_reason") or "").strip()
    if original == "restart is disabled for a live experiment":
        return "该参数需要重启 PostgreSQL 才能生效，在线实验未应用。"
    if original:
        return "未应用：{}".format(original)
    return "该参数在在线实验中未应用。"


def merge_explanation(bundle: Dict[str, Any], generated: Dict[str, Any], model: str) -> Dict[str, Any]:
    fallback_value = fallback(bundle, model)
    if not generated:
        return fallback_value
    reason_map = {}
    for item in generated.get("actions") or []:
        if isinstance(item, dict) and item.get("parameter"):
            reason_map[str(item["parameter"])] = item
    generated_skipped = {str(x.get("parameter")): x for x in generated.get("not_applied") or [] if isinstance(x, dict)}
    actions = []
    for action in bundle.get("actions") or []:
        parameter = action["parameter"]
        item = reason_map.get(parameter) or {}
        if action.get("applied") is False:
            reason = skipped_reason_text(action, generated_skipped)
        else:
            reason = str(item.get("reason") or "").strip() or next(
                (a["reason"] for a in fallback_value["actions"] if a["parameter"] == parameter),
                "本项原因未单独生成；以复测结果为准。")
        evidence = action.get("evidence") or [str(x) for x in item.get("evidence") or []][:4]
        actions.append(dict(action, reason=reason, evidence=evidence))
    skipped = []
    for item in fallback_value.get("not_applied") or []:
        action = next((a for a in bundle.get("actions") or [] if a["parameter"] == item["parameter"]), None)
        reason = skipped_reason_text(action, generated_skipped) if action else item["reason"]
        skipped.append(dict(item, reason=reason))
    root_text = normalize_terms(format_section(generated.get("root_cause")))
    hot_names = [str(item.get("Function")) for item in bundle.get("hot_functions") or []
                 if isinstance(item, dict) and item.get("Function")][:4]
    if hot_names and not root_text.startswith("结论"):
        root_text = "本次异常阶段的差分采样中，{} 等函数相对均值明显升高。\n{}".format(
            "、".join(hot_names), root_text)
    return {
        "root_cause": root_text or fallback_value["root_cause"],
        "actions": actions,
        "not_applied": skipped,
        "validation": normalize_terms(format_section(generated.get("validation"))) or fallback_value["validation"],
        "model": model,
    }


def generate(incident_dir: Path, force: bool = False) -> Dict[str, Any]:
    incident_dir = incident_dir.resolve()
    target = incident_dir / "tuning_rationale.json"
    if target.is_file() and not force:
        existing = load_json(target, {}) or {}
        if existing.get("schemaVersion") == SCHEMA:
            return existing
    bundle = build_inputs(incident_dir)
    if not bundle.get("changed"):
        raise ValueError("no changed tuning parameters found in {}".format(incident_dir))
    model = (api_settings() or (None, None, MODEL))[2]
    merged = merge_explanation(bundle, request_explanation(bundle), model)
    payload = {
        "schemaVersion": SCHEMA,
        "generatedAt": now(),
        "generatedBy": "tuning_rationale.py",
        "incidentDirectory": str(incident_dir),
        "model": model,
        **merged,
        "provenance": {
            "selectedLLMReasoning": bundle.get("selected_reasoning"),
            "candidateModel": bundle.get("model"),
            "comparison": bundle.get("comparison"),
        },
    }
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(temporary), str(target))
    return payload


def scan(state_db: Path, force: bool = False) -> List[Dict[str, Any]]:
    results = []
    if not state_db.is_file():
        raise FileNotFoundError("state database not found: {}".format(state_db))
    connection = sqlite3.connect(str(state_db))
    rows = connection.execute(
        "select incident_id,directory from incidents where status='completed' and directory is not null order by started_at"
    ).fetchall()
    connection.close()
    for incident_id, directory in rows:
        path = Path(directory)
        if not (path / "live_chain_result.json").is_file():
            continue
        try:
            value = generate(path, force=force)
            results.append({"incident_id": incident_id, "directory": str(path),
                            "actions": len(value.get("actions") or []), "status": "generated"})
        except Exception as exc:
            results.append({"incident_id": incident_id, "directory": str(path),
                            "status": "failed", "error": "{}: {}".format(type(exc).__name__, exc)})
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("incident_dir", nargs="?")
    parser.add_argument("--scan", action="store_true", help="process all completed incidents")
    parser.add_argument("--state-db", default=str(DEFAULT_STATE_DB))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.scan:
        print(json.dumps(scan(Path(args.state_db), force=args.force), ensure_ascii=False, indent=2))
        return 0
    if not args.incident_dir:
        parser.error("incident_dir or --scan is required")
    value = generate(Path(args.incident_dir), force=args.force)
    print(json.dumps({key: value.get(key) for key in
                      ("model", "root_cause", "validation", "actions", "not_applied")},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
