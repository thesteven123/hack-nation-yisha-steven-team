from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

MAX_PROMPT_CHARS = 60_000
MAX_RESPONSE_BYTES = 64_000
MAX_OUTPUT_CHARS = 32_000


@dataclass(frozen=True)
class LLMSettings:
    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: int = 60

    @classmethod
    def from_environment(cls) -> "LLMSettings":
        api_key = os.environ.get("SCIENTESIS_LLM_API_KEY", "").strip()
        model = os.environ.get("SCIENTESIS_LLM_MODEL", "").strip()
        base_url = os.environ.get("SCIENTESIS_LLM_BASE_URL", "https://api.openai.com/v1").strip()
        if not api_key:
            raise ValueError("Set SCIENTESIS_LLM_API_KEY before using LLM-assisted synthesis.")
        if not model or len(model) > 120:
            raise ValueError("Set SCIENTESIS_LLM_MODEL to the model name for your OpenAI-compatible endpoint.")
        if len(api_key) > 4096:
            raise ValueError("SCIENTESIS_LLM_API_KEY is unexpectedly long.")
        _chat_completions_url(base_url)
        return cls(api_key=api_key, model=model, base_url=base_url)

    @property
    def model_name(self) -> str:
        return self.model

    @property
    def endpoint(self) -> str:
        return _chat_completions_url(self.base_url)


class OpenAICompatibleClient:
    def __init__(self, settings: LLMSettings):
        self.settings = settings

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict:
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("System prompt must be non-empty text.")
        if not isinstance(user_prompt, str) or not user_prompt.strip() or len(user_prompt) > MAX_PROMPT_CHARS:
            raise ValueError(f"Prompt must be 1–{MAX_PROMPT_CHARS} characters.")
        payload = {
            "model": self.settings.model,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        request = urllib.request.Request(
            self.settings.endpoint,
            data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"LLM provider returned HTTP {error.code}; check the configured model and credentials.") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError("Could not reach the configured LLM endpoint; check its URL and network availability.") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("LLM provider response exceeded the 64 KB safety limit.")
        try:
            envelope = json.loads(raw)
            content = envelope["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
            raise RuntimeError("LLM provider returned an invalid Chat Completions response.") from error
        if not isinstance(content, str) or not content.strip() or len(content) > MAX_OUTPUT_CHARS:
            raise RuntimeError("LLM provider returned empty or oversized synthesis output.")
        try:
            result = json.loads(content)
        except json.JSONDecodeError as error:
            raise RuntimeError("LLM provider did not return valid JSON.") from error
        if not isinstance(result, dict):
            raise RuntimeError("LLM provider JSON output must be an object.")
        return result


def _chat_completions_url(base_url: str) -> str:
    if not isinstance(base_url, str) or not base_url.strip() or len(base_url) > 1000:
        raise ValueError("SCIENTESIS_LLM_BASE_URL must be a valid endpoint base URL.")
    parsed = urlsplit(base_url.strip())
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"https", "http"} or not hostname or parsed.username or parsed.password:
        raise ValueError("LLM endpoint must be an HTTP(S) URL without embedded credentials.")
    if parsed.scheme == "http" and hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Non-local LLM endpoints must use HTTPS.")
    if parsed.query or parsed.fragment:
        raise ValueError("LLM endpoint base URL cannot contain a query string or fragment.")
    path = parsed.path.rstrip("/") + "/chat/completions"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
