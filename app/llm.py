"""Small, dependency-free client for an OpenAI-compatible chat endpoint.

The demo deliberately keeps the model at the edge of the evidence pipeline:
the client can summarize retrieved evidence or propose typed candidates, but
it never executes model output.  The API token is read from the process
environment and is never included in status responses or exception text.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


LOG = logging.getLogger(__name__)
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.IGNORECASE | re.DOTALL)


class LLMError(RuntimeError):
    """An external model request failed or returned an unusable response."""


def normalize_chat_endpoint(url: str | None) -> str | None:
    """Normalize a base URL, /v1 URL, or full chat-completions URL.

    OpenAI-compatible gateways are commonly configured in any of these forms:
    ``https://host``, ``https://host/v1``, or
    ``https://host/v1/chat/completions``.  Supporting all three avoids putting
    provider-specific path assumptions in the frontend or in each adapter.
    """

    raw = str(url or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("KI_RAG_LLM_URL must be an http(s) URL")
    path = parsed.path.rstrip("/")
    lowered = path.lower()
    if lowered.endswith("/chat/completions"):
        normalized_path = path
    elif lowered.endswith("/v1"):
        normalized_path = path + "/chat/completions"
    else:
        normalized_path = path + "/v1/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, normalized_path, "", ""))


def _public_endpoint(endpoint: str | None) -> str | None:
    """Return a display-safe endpoint without query strings or credentials."""

    if not endpoint:
        return None
    parsed = urlsplit(endpoint)
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = "[%s]" % hostname
    netloc = hostname
    if parsed.port:
        netloc += ":%s" % parsed.port
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _content_from_message(message: Any) -> str:
    if isinstance(message, str):
        return message.strip()
    if isinstance(message, list):
        parts: list[str] = []
        for item in message:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in {None, "text", "output_text"}:
                text = item.get("text")
                if text is not None:
                    parts.append(str(text))
        return "".join(parts).strip()
    if isinstance(message, dict):
        return str(message.get("text") or message.get("content") or "").strip()
    return str(message or "").strip()


def _response_content(body: dict[str, Any]) -> str:
    choices = body.get("choices")
    if isinstance(choices, list) and choices:
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") or {}
        content = _content_from_message(message.get("content")) if isinstance(message, dict) else ""
        if content:
            return content
        content = _content_from_message(choice.get("text"))
        if content:
            return content
    return _content_from_message(body.get("output_text"))


def parse_json_object(content: str) -> dict[str, Any] | None:
    """Parse a JSON object from a model response, tolerating markdown fences."""

    text = str(content or "").strip()
    fenced = _JSON_FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Small models occasionally add one sentence before/after the JSON.
        # Only recover an object-shaped span; callers still validate its
        # fields and never execute arbitrary returned text.
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


class LLMClient:
    """Thread-safe, redacted, OpenAI-compatible chat client."""

    def __init__(
        self,
        *,
        url: str | None,
        model: str | None,
        token: str | None,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.endpoint = normalize_chat_endpoint(url)
        self.model = str(model or "").strip() or None
        self._token = str(token or "").strip() or None
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 120.0))
        self._lock = threading.Lock()
        self._calls = 0
        self._successes = 0
        self._failures = 0
        self._last_error: str | None = None
        self._last_call_at: str | None = None
        self._last_latency_ms: float | None = None
        self._last_usage: dict[str, int] = {}

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.model)

    @property
    def token_configured(self) -> bool:
        return bool(self._token)

    def status(self) -> dict[str, Any]:
        """Return operational state without exposing the bearer token."""

        with self._lock:
            return {
                "configured": self.configured,
                "endpoint": _public_endpoint(self.endpoint),
                "model": self.model,
                "token_configured": self.token_configured,
                "timeout_seconds": self.timeout_seconds,
                "calls": self._calls,
                "successes": self._successes,
                "failures": self._failures,
                "last_call_at": self._last_call_at,
                "last_latency_ms": self._last_latency_ms,
                "last_usage": dict(self._last_usage),
                "last_error": self._last_error,
            }

    def complete(
        self,
        messages: Iterable[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 800,
    ) -> dict[str, Any]:
        """Call chat completions and return content plus non-sensitive metadata."""

        if not self.configured:
            raise LLMError("external model is not configured")
        clean_messages = [
            {
                "role": str(item.get("role") or "user"),
                "content": str(item.get("content") or ""),
            }
            for item in messages
            if isinstance(item, dict)
        ]
        if not clean_messages:
            raise LLMError("external model request has no messages")
        payload = {
            "model": self.model,
            "temperature": max(0.0, min(float(temperature), 2.0)),
            "max_tokens": max(1, min(int(max_tokens), 4096)),
            "messages": clean_messages,
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "KeenInsight-PostgreSQL/0.1",
        }
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        request = Request(
            str(self.endpoint),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        started = time.monotonic()
        with self._lock:
            self._calls += 1
            self._last_call_at = datetime.now(timezone.utc).isoformat()
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(2_000_000)
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise LLMError("external model returned a non-object response")
            content = _response_content(body)
            if not content:
                raise LLMError("external model returned empty content")
            usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
            safe_usage = {
                key: int(value)
                for key, value in usage.items()
                if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                and isinstance(value, (int, float))
            }
            latency_ms = round((time.monotonic() - started) * 1000.0, 2)
            with self._lock:
                self._successes += 1
                self._last_latency_ms = latency_ms
                self._last_usage = safe_usage
                self._last_error = None
            return {
                "content": content,
                "model": str(body.get("model") or self.model),
                "usage": safe_usage,
                "latency_ms": latency_ms,
                "id": str(body.get("id") or "")[:200] or None,
            }
        except Exception as exc:
            message = self._safe_error_message(exc)
            latency_ms = round((time.monotonic() - started) * 1000.0, 2)
            with self._lock:
                self._failures += 1
                self._last_latency_ms = latency_ms
                self._last_error = message
            LOG.warning("external model request failed: %s", message)
            if isinstance(exc, LLMError):
                raise
            raise LLMError(message) from None

    @staticmethod
    def _safe_error_message(exc: Exception) -> str:
        if isinstance(exc, HTTPError):
            return "external model HTTP error %s" % exc.code
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return "external model request timed out"
        if isinstance(exc, URLError):
            return "external model network error"
        if isinstance(exc, json.JSONDecodeError):
            return "external model returned invalid JSON"
        if isinstance(exc, LLMError):
            return str(exc)
        return "external model request failed"
