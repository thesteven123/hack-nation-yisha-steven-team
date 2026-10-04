import io
import json
import urllib.error

import pytest

from scientesis.adapters.elevenlabs import ElevenLabsClient, ElevenLabsSettings, MAX_AUDIO_BYTES, validate_mp3


class FakeResponse:
    headers = {"Content-Type": "audio/mpeg"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size):
        assert size == MAX_AUDIO_BYTES + 1
        return b"ID3mocked-audio"


def test_settings_require_key_and_voice_without_exposing_key(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_VOICE_ID", raising=False)
    with pytest.raises(ValueError, match="ELEVENLABS_API_KEY"):
        ElevenLabsSettings.from_environment()
    monkeypatch.setenv("ELEVENLABS_API_KEY", "secret-test-key")
    with pytest.raises(ValueError, match="ELEVENLABS_VOICE_ID"):
        ElevenLabsSettings.from_environment()
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "testvoice")
    settings = ElevenLabsSettings.from_environment()
    assert "secret-test-key" not in repr(settings)
    assert settings.model_id == "eleven_multilingual_v2"
    with pytest.raises(ValueError, match="ELEVENLABS_VOICE_ID"):
        ElevenLabsSettings("key", "../../unsafe")


def test_only_reviewed_text_is_sent_to_fixed_endpoint_with_redirects_blocked(monkeypatch):
    captured = {}
    class FakeOpener:
        def open(self, request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return FakeResponse()
    def build_opener(handler):
        assert handler.redirect_request(None, None, 302, "", {}, "https://other.example") is None
        return FakeOpener()
    monkeypatch.setattr("scientesis.adapters.elevenlabs.urllib.request.build_opener", build_opener)
    client = ElevenLabsClient(ElevenLabsSettings("test-secret", "voice123"))
    assert client.synthesize("Verified summary") == b"ID3mocked-audio"
    request = captured["request"]
    assert request.full_url == "https://api.elevenlabs.io/v1/text-to-speech/voice123?output_format=mp3_44100_128"
    assert dict(request.header_items())["Xi-api-key"] == "test-secret"
    assert json.loads(request.data) == {"text": "Verified summary", "model_id": "eleven_multilingual_v2"}


def test_provider_errors_do_not_echo_key_or_response_body(monkeypatch):
    class FakeOpener:
        def open(self, *_args, **_kwargs):
            raise urllib.error.HTTPError("https://api.elevenlabs.io", 401, "private-key", {}, io.BytesIO(b"provider-secret"))
    monkeypatch.setattr("scientesis.adapters.elevenlabs.urllib.request.build_opener", lambda *_: FakeOpener())
    with pytest.raises(RuntimeError) as error:
        ElevenLabsClient(ElevenLabsSettings("private-key", "voice123")).synthesize("Verified text")
    assert "401" in str(error.value)
    assert "private-key" not in str(error.value) and "provider-secret" not in str(error.value)


@pytest.mark.parametrize("audio", [b"", b"<html>error</html>", b"ID3" + b"x" * MAX_AUDIO_BYTES])
def test_invalid_or_oversized_audio_is_rejected(audio):
    with pytest.raises(ValueError):
        validate_mp3(audio)
