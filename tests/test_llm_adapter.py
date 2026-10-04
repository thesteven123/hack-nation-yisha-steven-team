import io
import json
import urllib.error

import pytest

from scientesis.adapters.llm import LLMSettings, OpenAICompatibleClient


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]


def test_settings_require_credentials_and_a_model(monkeypatch):
    monkeypatch.delenv("SCIENTESIS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("SCIENTESIS_LLM_MODEL", raising=False)
    with pytest.raises(ValueError, match="SCIENTESIS_LLM_API_KEY"):
        LLMSettings.from_environment()

    monkeypatch.setenv("SCIENTESIS_LLM_API_KEY", "test-secret")
    with pytest.raises(ValueError, match="SCIENTESIS_LLM_MODEL"):
        LLMSettings.from_environment()


def test_client_sends_json_only_without_tools_or_echoing_the_key(monkeypatch):
    settings = LLMSettings("never-log-this", "test-model")
    expected = {"hypothesis": {}, "proposal": {}}
    body = json.dumps({"choices": [{"message": {"content": json.dumps(expected)}}]}).encode()
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return FakeResponse(body)

    monkeypatch.setattr("scientesis.adapters.llm.urllib.request.urlopen", fake_urlopen)
    result = OpenAICompatibleClient(settings).complete_json("system", "selected evidence")

    assert result == expected
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer never-log-this"
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert "tools" not in captured["payload"]
    assert captured["timeout"] == 60


def test_endpoint_rejects_remote_plain_http_and_embedded_credentials():
    with pytest.raises(ValueError, match="HTTPS"):
        LLMSettings("test", "model", "http://provider.example/v1").endpoint
    with pytest.raises(ValueError, match="embedded credentials"):
        LLMSettings("test", "model", "https://user:pass@provider.example/v1").endpoint
    assert LLMSettings("test", "model", "http://127.0.0.1:8000/v1").endpoint.endswith("/v1/chat/completions")


def test_provider_errors_do_not_include_secret_or_response_body(monkeypatch):
    secret = "never-log-this"

    def fail_urlopen(_request, timeout):
        raise urllib.error.HTTPError("https://provider.example", 401, "Unauthorized", {}, io.BytesIO(b"leaked-provider-body"))

    monkeypatch.setattr("scientesis.adapters.llm.urllib.request.urlopen", fail_urlopen)
    with pytest.raises(RuntimeError) as error:
        OpenAICompatibleClient(LLMSettings(secret, "model")).complete_json("system", "user")
    assert secret not in str(error.value)
    assert "leaked-provider-body" not in str(error.value)
