import io
import json
import os
import stat
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from scientesis.adapters.elevenlabs import ElevenLabsClient, ElevenLabsSettings
from scientesis.adapters.firecrawl import FirecrawlClient, FirecrawlError
from scientesis.adapters.http import NoRedirect, open_without_redirects
from scientesis.adapters.llm import LLMSettings
from scientesis.adapters.moss import MossConfigurationError, MossSettings
from scientesis.services.integration_settings import FIELDS, SettingsError, SettingsStore, validate_endpoint


@pytest.fixture
def store(tmp_path):
    return SettingsStore(tmp_path / "private" / "integration-settings.json", environment={})


def test_local_values_override_environment_without_mutating_it(tmp_path):
    environment = {"SCIENTESIS_LLM_API_KEY": "environment-secret", "SCIENTESIS_LLM_MODEL": "old-model"}
    store = SettingsStore(tmp_path / "settings.json", environment=environment)
    assert store.get("SCIENTESIS_LLM_API_KEY") == "environment-secret"
    store.save({"SCIENTESIS_LLM_API_KEY": "new-secret", "SCIENTESIS_LLM_MODEL": "new-model"})
    assert store.get("SCIENTESIS_LLM_API_KEY") == "new-secret"
    assert SettingsStore(store.path, environment=environment).get("SCIENTESIS_LLM_MODEL") == "new-model"
    assert environment["SCIENTESIS_LLM_API_KEY"] == "environment-secret"
    assert store.status("SCIENTESIS_LLM_API_KEY") == {"configured": True, "source": "saved locally", "disabled": False}
    assert "secret" not in repr(store.status("SCIENTESIS_LLM_API_KEY"))


def test_related_settings_are_read_from_one_local_snapshot(store, monkeypatch):
    calls = []
    def load():
        calls.append(True)
        return {"SCIENTESIS_LLM_API_KEY": "snapshot-token", "SCIENTESIS_LLM_MODEL": "snapshot-model"}
    monkeypatch.setattr(store, "load", load)
    values = store.get_many(("SCIENTESIS_LLM_API_KEY", "SCIENTESIS_LLM_MODEL"))
    assert len(calls) == 1
    assert values == {"SCIENTESIS_LLM_API_KEY": "snapshot-token", "SCIENTESIS_LLM_MODEL": "snapshot-model"}


def test_disable_blocks_environment_fallback_and_restore_is_explicit(tmp_path):
    store = SettingsStore(tmp_path / "settings.json", environment={"FIRECRAWL_API_KEY": "environment-secret"})
    store.save({"FIRECRAWL_API_KEY": ""})
    assert store.get("FIRECRAWL_API_KEY") == ""
    assert store.status("FIRECRAWL_API_KEY")["disabled"] is True
    store.save({}, reset={"FIRECRAWL_API_KEY"})
    assert store.get("FIRECRAWL_API_KEY") == "environment-secret"
    assert store.status("FIRECRAWL_API_KEY")["source"] == "server environment"


def test_atomic_owner_only_save_preserves_other_services(store):
    store.save({"SCIENTESIS_LLM_API_KEY": "llm-secret"})
    store.save({"FIRECRAWL_API_KEY": "firecrawl-secret"})
    assert store.get("SCIENTESIS_LLM_API_KEY") == "llm-secret"
    if os.name == "posix":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert list(store.path.parent.glob(".integration-settings-*.tmp")) == []
    before = store.path.read_bytes()
    with pytest.raises(SettingsError):
        store.save({"SCIENTESIS_LLM_API_KEY": "replacement", "ELEVENLABS_BASE_URL": "http://remote.example"})
    assert store.path.read_bytes() == before


def test_disk_failure_preserves_previous_settings_and_removes_temporary_file(store, monkeypatch):
    store.save({"FIRECRAWL_API_KEY": "original-secret"})
    before = store.path.read_bytes()
    def fail(*_args):
        raise OSError("sensitive-provider-info")
    monkeypatch.setattr("scientesis.services.integration_settings.os.replace", fail)
    with pytest.raises(SettingsError) as error:
        store.save({"FIRECRAWL_API_KEY": "new-secret"})
    assert "secret" not in str(error.value)
    assert store.path.read_bytes() == before
    assert list(store.path.parent.glob(".integration-settings-*.tmp")) == []


@pytest.mark.parametrize("endpoint", [
    "http://remote.example/v1", "https://user:secret@example.com/v1", "https://example.com?token=secret",
    "https://example.com/#secret", "https://example.com:99999/v1", "https://exa mple.com/v1",
    "file:///etc/passwd", "https://example.com/\nsecret", "https://example.com:0/v1",
])
def test_unsafe_endpoints_are_rejected_without_echoing_tokens(endpoint):
    with pytest.raises(SettingsError) as error:
        validate_endpoint(endpoint)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("endpoint", ["https://provider.example/v1/", "http://localhost:8080/v1", "http://127.0.0.1:8080", "http://[::1]:8080/v1"])
def test_https_and_loopback_endpoints_are_supported(endpoint):
    assert validate_endpoint(endpoint) == endpoint.rstrip("/")


@pytest.mark.parametrize("changes", [
    {"UNKNOWN_TOKEN": "secret"}, {"FIRECRAWL_API_KEY": "secret\r\nheader"},
    {"FIRECRAWL_API_KEY": "secret token"}, {"SCIENTESIS_LLM_MODEL": "secret\nmodel"},
    {"ELEVENLABS_VOICE_ID": "../../unsafe"}, {"MOSS_INDEX_NAME": "../index"},
])
def test_invalid_fields_never_write_credentials(store, changes):
    with pytest.raises(SettingsError):
        store.save(changes)
    assert not store.path.exists()


def test_invalid_settings_file_can_be_reset_without_exposing_contents(store):
    store.path.parent.mkdir()
    store.path.write_text('{"token":"secret-and-invalid', encoding="utf-8")
    with pytest.raises(SettingsError) as error:
        store.load()
    assert "secret-and-invalid" not in str(error.value)
    store.reset_all()
    assert store.load() == {}


def test_symlinks_are_not_followed(tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"version": 1, "values": {"FIRECRAWL_API_KEY": "unrelated-secret"}}))
    link = tmp_path / "settings.json"
    link.symlink_to(target)
    store = SettingsStore(link, environment={})
    with pytest.raises(SettingsError, match="symbolic link"):
        store.load()
    with pytest.raises(SettingsError):
        store.save({"FIRECRAWL_API_KEY": "replacement"})
    store.reset_all()
    assert "unrelated-secret" in target.read_text()


def test_all_adapters_use_saved_settings_on_the_next_request(monkeypatch):
    store = SettingsStore()
    store.save({
        "SCIENTESIS_LLM_API_KEY": "llm-local-secret", "SCIENTESIS_LLM_MODEL": "test-model",
        "SCIENTESIS_LLM_BASE_URL": "https://llm.example/v1",
        "FIRECRAWL_API_KEY": "firecrawl-local-secret", "FIRECRAWL_API_URL": "https://capture.example/v2/scrape",
        "MOSS_PROJECT_KEY": "moss-local-secret", "MOSS_PROJECT_ID": "test-project", "MOSS_INDEX_NAME": "test-index",
        "ELEVENLABS_API_KEY": "voice-local-secret", "ELEVENLABS_VOICE_ID": "voice123",
        "ELEVENLABS_BASE_URL": "https://voice.example/v1",
    })
    llm = LLMSettings.from_environment()
    assert llm.api_key == "llm-local-secret" and llm.endpoint == "https://llm.example/v1/chat/completions"
    firecrawl = FirecrawlClient()
    assert firecrawl.api_key == "firecrawl-local-secret" and firecrawl.endpoint == "https://capture.example/v2/scrape"
    moss = MossSettings.from_environment()
    assert moss.project_key == "moss-local-secret" and moss.index_name == "test-index"
    voice = ElevenLabsSettings.from_environment()
    assert voice.api_key == "voice-local-secret" and voice.base_url == "https://voice.example/v1"
    assert "local-secret" not in repr(llm) + repr(moss) + repr(voice)
    store.save({"SCIENTESIS_LLM_MODEL": "changed-model"})
    assert LLMSettings.from_environment().model == "changed-model"
    store.save({"MOSS_PROJECT_KEY": ""})
    monkeypatch.setenv("MOSS_PROJECT_KEY", "inherited-secret")
    with pytest.raises(MossConfigurationError):
        MossSettings.from_environment()


def test_elevenlabs_custom_endpoint_uses_selected_voice_without_redirects(monkeypatch):
    requests = []
    class Response:
        headers = {"Content-Type": "audio/mpeg"}
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return None
        def read(self, _limit):
            return b"ID3mock-audio"
    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return Response()
    monkeypatch.setattr("scientesis.adapters.elevenlabs.urllib.request.build_opener", lambda _handler: Opener())
    settings = ElevenLabsSettings("test-secret", "voice123", base_url="https://voice.example/v1/")
    assert ElevenLabsClient(settings).synthesize("Reviewed text") == b"ID3mock-audio"
    assert requests[0].full_url == "https://voice.example/v1/text-to-speech/voice123?output_format=mp3_44100_128"


def test_firecrawl_custom_endpoint_and_errors_do_not_expose_tokens(monkeypatch):
    monkeypatch.setattr("scientesis.adapters.firecrawl.validate_public_url", lambda url: url)
    requests = []
    def opener(request, timeout):
        requests.append(request)
        raise urllib.error.HTTPError(request.full_url, 401, "private-secret", {}, io.BytesIO(b"private-secret"))
    with pytest.raises(FirecrawlError) as error:
        FirecrawlClient(api_key="private-secret", opener=opener, endpoint="https://capture.example/v2/scrape").scrape("https://source.example")
    assert requests[0].full_url == "https://capture.example/v2/scrape"
    assert "private-secret" not in str(error.value)


def test_default_http_transport_blocks_redirects(monkeypatch):
    captured = []
    def build(handler):
        captured.append(handler)
        return SimpleNamespace(open=lambda request, timeout: "mock-response")
    monkeypatch.setattr("scientesis.adapters.http.urllib.request.build_opener", build)
    assert open_without_redirects("mock-request", 45) == "mock-response"
    assert isinstance(captured[0], NoRedirect)
    assert captured[0].redirect_request(None, None, 302, "", {}, "https://other.example") is None
