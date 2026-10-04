from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

SETTINGS_PATH = Path(__file__).resolve().parents[3] / "data" / "integration-settings.json"
MAX_FILE_BYTES = 64_000
_LOCK = threading.RLock()


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class SettingField:
    name: str
    label: str
    kind: str = "text"
    default: str = ""
    limit: int = 120

    @property
    def secret(self) -> bool:
        return self.kind == "token"


@dataclass(frozen=True)
class Integration:
    name: str
    label: str
    fields: tuple[SettingField, ...]
    note: str


INTEGRATIONS = (
    Integration("llm", "LLM synthesis", (
        SettingField("SCIENTESIS_LLM_API_KEY", "API token", "token", limit=4096),
        SettingField("SCIENTESIS_LLM_MODEL", "Model"),
        SettingField("SCIENTESIS_LLM_BASE_URL", "Endpoint base URL", "endpoint", "https://api.openai.com/v1", 1000),
    ), "OpenAI-compatible Chat Completions with JSON mode. Use a base URL, not the /chat/completions path."),
    Integration("firecrawl", "Firecrawl", (
        SettingField("FIRECRAWL_API_KEY", "API token (optional)", "token", limit=4096),
        SettingField("FIRECRAWL_API_URL", "Scrape endpoint URL", "endpoint", "https://api.firecrawl.dev/v2/scrape", 1000),
    ), "Public-page capture. A custom endpoint must implement the Firecrawl v2 scrape API."),
    Integration("moss", "Moss retrieval", (
        SettingField("MOSS_PROJECT_KEY", "Project API token", "token", limit=4096),
        SettingField("MOSS_PROJECT_ID", "Moss project ID", limit=256),
        SettingField("MOSS_INDEX_NAME", "Index name", "index", "scientesis-research", 256),
    ), "Requires the optional Moss SDK. Its endpoint is managed by the SDK and is not configurable here."),
    Integration("elevenlabs", "ElevenLabs audio", (
        SettingField("ELEVENLABS_API_KEY", "API token", "token", limit=4096),
        SettingField("ELEVENLABS_VOICE_ID", "Voice ID", "identifier", limit=128),
        SettingField("ELEVENLABS_MODEL_ID", "Model ID", "identifier", "eleven_multilingual_v2", 128),
        SettingField("ELEVENLABS_BASE_URL", "Endpoint base URL", "endpoint", "https://api.elevenlabs.io/v1", 1000),
    ), "Text-to-speech only. Use a base URL, not a voice-specific /text-to-speech path."),
)
FIELDS = {field.name: field for integration in INTEGRATIONS for field in integration.fields}


def validate_endpoint(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1000:
        raise SettingsError("Endpoint must be a non-empty URL under 1,000 characters.")
    value = value.strip()
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise SettingsError("Endpoint cannot contain spaces or control characters.")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise SettingsError("Endpoint contains an invalid host or port.") from None
    if parsed.scheme not in {"https", "http"} or not hostname or parsed.username is not None or parsed.password is not None:
        raise SettingsError("Endpoint must be an HTTP(S) URL without embedded credentials.")
    if port is not None and not 1 <= port <= 65535:
        raise SettingsError("Endpoint contains an invalid port.")
    if parsed.scheme == "http" and hostname.lower() not in {"localhost", "127.0.0.1", "::1"}:
        raise SettingsError("Non-local endpoints must use HTTPS.")
    if parsed.query or parsed.fragment:
        raise SettingsError("Endpoint cannot contain a query string or fragment; put tokens in the token field.")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def validate_setting(name: str, value: str) -> str:
    field = FIELDS.get(name)
    if field is None:
        raise SettingsError("Unknown connection setting.")
    if not isinstance(value, str):
        raise SettingsError(f"{field.label} must be text.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise SettingsError(f"{field.label} cannot contain control characters.")
    value = value.strip()
    if len(value) > field.limit:
        raise SettingsError(f"{field.label} exceeds its {field.limit}-character limit.")
    if field.secret and value and (not value.isascii() or any(character.isspace() for character in value)):
        raise SettingsError("API tokens cannot contain whitespace or non-ASCII characters.")
    if field.kind == "endpoint":
        return validate_endpoint(value)
    if field.kind == "identifier" and value and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise SettingsError(f"{field.label} must contain letters, digits, underscores, or hyphens only.")
    if field.kind == "index" and (not value or value in {".", ".."} or "/" in value or len(value.encode("utf-8")) > 256):
        raise SettingsError("Moss index name must be 1–256 bytes, without slashes, and cannot be '.' or '..'.")
    return value


class SettingsStore:
    def __init__(self, path: str | Path | None = None, environment=None):
        self.path = Path(path) if path is not None else SETTINGS_PATH
        self.environment = os.environ if environment is None else environment

    def load(self) -> dict[str, str]:
        with _LOCK:
            try:
                if self.path.is_symlink():
                    raise SettingsError("Local connection settings cannot be a symbolic link.")
                if not self.path.exists():
                    return {}
                with self.path.open("rb") as handle:
                    raw = handle.read(MAX_FILE_BYTES + 1)
                if len(raw) > MAX_FILE_BYTES:
                    raise SettingsError("Local connection settings exceed the file-size limit.")
                payload = json.loads(raw)
                if not isinstance(payload, dict) or payload.get("version") != 1 or set(payload) != {"version", "values"}:
                    raise SettingsError("Local connection settings have an unsupported format.")
                values = payload["values"]
                if not isinstance(values, dict):
                    raise SettingsError("Local connection settings must contain a settings object.")
                return {name: validate_setting(name, value) for name, value in values.items()}
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                raise SettingsError("Local connection settings could not be read. Use Settings to reset the local file.") from None

    def get_many(self, names) -> dict[str, str]:
        names = tuple(names)
        if any(name not in FIELDS for name in names):
            raise SettingsError("Unknown connection setting.")
        values = self.load()
        environment = dict(self.environment)
        return {
            name: validate_setting(name, values.get(name, environment.get(name, FIELDS[name].default)))
            for name in names
        }

    def get(self, name: str) -> str:
        return self.get_many((name,))[name]

    def status(self, name: str) -> dict:
        values = self.load()
        value = self.get(name)
        source = "saved locally" if name in values else "server environment" if name in self.environment else "default"
        return {"configured": bool(value), "source": source, "disabled": FIELDS[name].secret and name in values and not value}

    def save(self, changes: dict[str, str], reset=()) -> None:
        normalized = {name: validate_setting(name, value) for name, value in changes.items()}
        reset = set(reset)
        if reset - FIELDS.keys() or reset & normalized.keys():
            raise SettingsError("Invalid connection-settings update.")
        with _LOCK:
            values = self.load()
            for name in reset:
                values.pop(name, None)
            values.update(normalized)
            temporary_path = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, prefix=".integration-settings-", suffix=".tmp", delete=False) as handle:
                    temporary_path = Path(handle.name)
                    os.chmod(temporary_path, stat.S_IRUSR | stat.S_IWUSR)
                    json.dump({"version": 1, "values": values}, handle, ensure_ascii=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, self.path)
            except OSError:
                raise SettingsError("Could not save local connection settings; check the app's data-folder permissions.") from None
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)

    def reset_all(self) -> None:
        with _LOCK:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                raise SettingsError("Could not reset local connection settings.") from None


def get_setting(name: str) -> str:
    return SettingsStore().get(name)


def get_settings(names) -> dict[str, str]:
    return SettingsStore().get_many(names)
