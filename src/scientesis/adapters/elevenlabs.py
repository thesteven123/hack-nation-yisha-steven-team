from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from scientesis.services.integration_settings import get_settings, validate_endpoint

MAX_TEXT_CHARS = 5000
MAX_AUDIO_BYTES = 20 * 1024 * 1024
OUTPUT_FORMAT = "mp3_44100_128"


@dataclass(frozen=True)
class ElevenLabsSettings:
    api_key: str = field(repr=False)
    voice_id: str
    model_id: str = "eleven_multilingual_v2"
    timeout_seconds: int = 60
    base_url: str = "https://api.elevenlabs.io/v1"

    def __post_init__(self):
        validate_endpoint(self.base_url)
        if not isinstance(self.api_key, str) or not self.api_key.strip() or len(self.api_key) > 4096 or any(character in self.api_key for character in "\r\n"):
            raise ValueError("Set a valid ELEVENLABS_API_KEY to enable narration audio.")
        for name, value in (("ELEVENLABS_VOICE_ID", self.voice_id), ("ELEVENLABS_MODEL_ID", self.model_id)):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
                raise ValueError(f"Set a valid {name} before generating audio.")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)) or not math.isfinite(self.timeout_seconds) or not 1 <= self.timeout_seconds <= 120:
            raise ValueError("Narration request timeout must be between 1 and 120 seconds.")

    @classmethod
    def from_environment(cls) -> "ElevenLabsSettings":
        values = get_settings(("ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL_ID", "ELEVENLABS_BASE_URL"))
        return cls(
            api_key=values["ELEVENLABS_API_KEY"],
            voice_id=values["ELEVENLABS_VOICE_ID"],
            model_id=values["ELEVENLABS_MODEL_ID"],
            base_url=values["ELEVENLABS_BASE_URL"],
        )


class ElevenLabsClient:
    def __init__(self, settings: ElevenLabsSettings):
        self.settings = settings

    def synthesize(self, text: str) -> bytes:
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
            raise ValueError(f"Narration must contain 1–{MAX_TEXT_CHARS} characters.")
        request = urllib.request.Request(
            f"{validate_endpoint(self.settings.base_url)}/text-to-speech/{self.settings.voice_id}?output_format={OUTPUT_FORMAT}",
            data=json.dumps({"text": text, "model_id": self.settings.model_id}, allow_nan=False).encode("utf-8"),
            headers={"xi-api-key": self.settings.api_key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
            method="POST",
        )
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=self.settings.timeout_seconds) as response:
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type not in {"audio/mpeg", "audio/mp3", "application/octet-stream"}:
                    raise RuntimeError("ElevenLabs returned a non-audio response; nothing was saved.")
                audio = response.read(MAX_AUDIO_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"ElevenLabs returned HTTP {error.code}; check credentials, voice access, and provider limits.") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError("Could not reach ElevenLabs; check network availability and try again.") from None
        validate_mp3(audio)
        return audio


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def validate_mp3(audio: bytes) -> None:
    if not isinstance(audio, bytes) or not 4 <= len(audio) <= MAX_AUDIO_BYTES:
        raise ValueError("Narration audio is empty or exceeds the 20 MB limit.")
    if not (audio.startswith(b"ID3") or audio[0] == 0xFF and audio[1] & 0xE0 == 0xE0):
        raise ValueError("The narration response is not MP3 audio.")
