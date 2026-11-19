import json

from app.llm import LLMClient, parse_json_object, normalize_chat_endpoint


def test_openai_compatible_endpoint_normalization():
    assert normalize_chat_endpoint("https://example.test") == (
        "https://example.test/v1/chat/completions"
    )
    assert normalize_chat_endpoint("https://example.test/v1/") == (
        "https://example.test/v1/chat/completions"
    )
    assert normalize_chat_endpoint("https://example.test/v1/chat/completions") == (
        "https://example.test/v1/chat/completions"
    )


def test_json_parser_accepts_fenced_or_wrapped_model_output():
    assert parse_json_object('```json\n{"candidates": []}\n```') == {"candidates": []}
    assert parse_json_object('Here is the result: {"summary":"ok"}') == {
        "summary": "ok"
    }
    assert parse_json_object("not json") is None


def test_client_extracts_chat_content_and_redacts_token(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return json.dumps(
                {
                    "id": "chatcmpl-test",
                    "model": "gpt-4o-mini",
                    "choices": [{"message": {"content": "[D1] grounded"}}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15},
                }
            ).encode("utf-8")

    captured = {}

    def fake_urlopen(request, timeout):
        captured["authorization"] = request.headers.get("Authorization")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("app.llm.urlopen", fake_urlopen)
    client = LLMClient(
        url="https://example.test/v1",
        model="gpt-4o-mini",
        token="test-token",
    )
    result = client.complete([{"role": "user", "content": "hello"}])
    assert result["content"] == "[D1] grounded"
    assert result["usage"]["total_tokens"] == 15
    assert captured["authorization"] == "Bearer test-token"
    assert captured["timeout"] == 20.0
    status = client.status()
    assert status["token_configured"] is True
    assert "test-token" not in json.dumps(status)
