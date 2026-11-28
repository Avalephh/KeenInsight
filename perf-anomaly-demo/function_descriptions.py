#!/usr/bin/env python3
"""Generate and cache explanations for functions without source comments.

Source comments are preferred.  This module is the fallback requested for
functions whose pinned source has no leading comment (or whose symbol is not
in the PostgreSQL tree at all).  It calls the configured chat-completions
endpoint and stores both Chinese and English explanations in a small cache.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "results" / "function_descriptions.json"
SCHEMA = 1
MODEL = "gpt-6.1-sol"
_LOCK = threading.Lock()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _api_settings():
    base = (os.environ.get("SYSINSIGHT_GPT_BASE_URL")
            or os.environ.get("SYSINSIGHT_API_BASE")
            or os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("KI_RAG_LLM_URL") or "").strip()
    key = (os.environ.get("SYSINSIGHT_GPT_API_KEY")
           or os.environ.get("SYSINSIGHT_API_KEY")
           or os.environ.get("OPENAI_API_KEY")
           or os.environ.get("KI_RAG_LLM_TOKEN") or "").strip()
    model = (os.environ.get("SYSINSIGHT_GPT_MODEL")
             or os.environ.get("SYSINSIGHT_API_MODEL")
             or os.environ.get("OPENAI_MODEL")
             or os.environ.get("KI_RAG_LLM_MODEL") or MODEL).strip()
    if not base or not key:
        return None
    base = base.rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base, key, model


def _source_hash(name: str, detail: Dict[str, Any]) -> str:
    value = "|".join([
        str(name),
        str(detail.get("file") or ""),
        str(detail.get("line") or ""),
        str(detail.get("description") or ""),
        str(detail.get("signature") or ""),
        str(detail.get("excerpt") or "")[:2000],
    ])
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _load_cache() -> Dict[str, Any]:
    if not CACHE.is_file():
        return {"schemaVersion": SCHEMA, "model": MODEL, "functions": {}}
    try:
        payload = json.loads(CACHE.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("functions"), dict):
            return payload
    except (OSError, ValueError):
        pass
    return {"schemaVersion": SCHEMA, "model": MODEL, "functions": {}}


def _save_cache(payload: Dict[str, Any]) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CACHE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(temporary), str(CACHE))


def _parse_json(text: str) -> Optional[Dict[str, Any]]:
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


def _clip(value: Any, limit: int = 900) -> Optional[str]:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:limit] or None


def _request_description(name: str, detail: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    settings = _api_settings()
    if settings is None:
        return None
    base, key, model = settings
    excerpt = str(detail.get("excerpt") or "")[:5000]
    prompt = (
        "You are a factual PostgreSQL source-code explainer. Explain what this "
        "function does in 1-3 sentences. Use only the supplied signature, source "
        "comment and excerpt; do not invent performance impact or call relationships. "
        "If the excerpt is empty (for example a kernel or extension symbol), explain "
        "from the symbol name and label uncertainty in the text.\n\n"
        "Function: {name}\nFile: {file}\nLine: {line}\nSignature: {signature}\n"
        "Source comment: {description}\nSource excerpt:\n{excerpt}\n\n"
        "Return JSON only, with exactly these keys: "
        '{{\"zh\":\"中文说明\",\"en\":\"English explanation\"}}.'
    ).format(
        name=name,
        file=detail.get("file") or "(not in pinned PostgreSQL source)",
        line=detail.get("line") or "-",
        signature=detail.get("signature") or "(unknown)",
        description=detail.get("description") or "(none)",
        excerpt=excerpt or "(none)",
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Return valid JSON only. Be factual and concise."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 500,
    }
    request = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json",
                 "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None
    choices = body.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return None
    content = ((choices[0].get("message") or {}).get("content") or "")
    parsed = _parse_json(content)
    if not parsed:
        return None
    zh = _clip(parsed.get("zh"))
    en = _clip(parsed.get("en"))
    if not zh and not en:
        return None
    return {"zh": zh, "en": en, "model": model}


def describe_if_missing(name: str, detail: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return a cached or newly generated explanation for a function."""
    if detail.get("description"):
        return None
    source_hash = _source_hash(name, detail)
    with _LOCK:
        cache = _load_cache()
        entry = (cache.get("functions") or {}).get(name)
        if isinstance(entry, dict) and entry.get("sourceHash") == source_hash:
            if entry.get("zh") or entry.get("en"):
                return {"zh": entry.get("zh"), "en": entry.get("en"),
                        "model": entry.get("model") or cache.get("model") or MODEL,
                        "cached": True}
        generated = _request_description(name, detail)
        if not generated:
            return None
        cache.setdefault("functions", {})[name] = {
            "zh": generated.get("zh"),
            "en": generated.get("en"),
            "model": generated.get("model") or MODEL,
            "sourceHash": source_hash,
            "generatedAt": _now(),
        }
        cache["schemaVersion"] = SCHEMA
        cache["model"] = generated.get("model") or MODEL
        _save_cache(cache)
        return dict(generated, cached=False)
