from __future__ import annotations

import ipaddress
import json
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from urllib.request import Request

from scientesis.adapters.http import open_without_redirects
from scientesis.services.integration_settings import get_settings, validate_endpoint

API_URL = "https://api.firecrawl.dev/v2/scrape"
MAX_RESPONSE_BYTES = 8_000_000
MAX_MARKDOWN_CHARS = 500_000
SENSITIVE_QUERY_KEYS = {"api_key", "apikey", "auth", "authorization", "key", "password", "session", "token"}


class FirecrawlError(RuntimeError):
    pass


class UnsafeSourceURL(ValueError):
    pass


@dataclass(frozen=True)
class ScrapedPage:
    source_url: str
    title: str
    description: str
    markdown: str
    retrieved_at: str
    status_code: int | None


def validate_public_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise UnsafeSourceURL("Enter a public HTTP or HTTPS URL under 2,048 characters.")
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise UnsafeSourceURL("Only public HTTP and HTTPS source URLs are allowed.")
    if parsed.username or parsed.password:
        raise UnsafeSourceURL("URLs containing usernames or passwords are not allowed.")
    try:
        port = parsed.port
    except ValueError as error:
        raise UnsafeSourceURL("The URL has an invalid port.") from error
    if port not in {None, 80, 443}:
        raise UnsafeSourceURL("Only standard public web ports (80 and 443) are allowed.")

    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "metadata.google.internal"} or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid")):
        raise UnsafeSourceURL("Local, internal, and reserved hostnames cannot be scraped.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            resolved = socket.getaddrinfo(host, port or (443 if parsed.scheme.lower() == "https" else 80), type=socket.SOCK_STREAM)
        except OSError as error:
            raise UnsafeSourceURL("The source hostname could not be resolved.") from error
        addresses = {item[4][0].split("%", 1)[0] for item in resolved}
        if not addresses or any(not ipaddress.ip_address(item).is_global for item in addresses):
            raise UnsafeSourceURL("The source must resolve only to public IP addresses.")
    else:
        if not address.is_global:
            raise UnsafeSourceURL("Private, loopback, link-local, and reserved IP addresses cannot be scraped.")

    if any(key.lower() in SENSITIVE_QUERY_KEYS for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        raise UnsafeSourceURL("Remove authentication or session parameters from the source URL.")
    netloc = host
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]"
    if port is not None:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


class FirecrawlClient:
    def __init__(self, api_key: str | None = None, timeout: int = 45, opener=None, endpoint: str | None = None):
        if not isinstance(timeout, int) or timeout < 1 or timeout > 120:
            raise ValueError("Timeout must be between 1 and 120 seconds.")
        values = get_settings(("FIRECRAWL_API_KEY", "FIRECRAWL_API_URL"))
        self.api_key = api_key if api_key is not None else values["FIRECRAWL_API_KEY"]
        self.endpoint = validate_endpoint(endpoint if endpoint is not None else values["FIRECRAWL_API_URL"])
        self.timeout = timeout
        self.opener = opener or open_without_redirects

    def scrape(self, source_url: str) -> ScrapedPage:
        source_url = validate_public_url(source_url)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            self.endpoint,
            data=json.dumps({"url": source_url, "formats": ["markdown"], "onlyMainContent": True, "maxAge": 0}).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                status = getattr(response, "status", 200)
        except HTTPError as error:
            raise FirecrawlError(f"Firecrawl returned HTTP {error.code}; check credentials and provider limits.") from None
        except (URLError, TimeoutError, OSError):
            raise FirecrawlError("Could not reach Firecrawl; check the endpoint and network availability.") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise FirecrawlError("Firecrawl response exceeded the 8 MB safety limit.")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise FirecrawlError("Firecrawl returned an invalid JSON response.") from error
        if status < 200 or status >= 300 or not isinstance(payload, dict) or payload.get("success") is False:
            raise FirecrawlError(f"Firecrawl returned an unsuccessful response (HTTP {status}); check the source and provider status.")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise FirecrawlError("Firecrawl response did not contain page data.")
        markdown = data.get("markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            raise FirecrawlError("Firecrawl found no readable Markdown on that page.")
        if len(markdown) > MAX_MARKDOWN_CHARS:
            raise FirecrawlError("The extracted page exceeded the 500,000-character safety limit.")
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        status_code = metadata.get("statusCode")
        return ScrapedPage(
            source_url=source_url,
            title=str(metadata.get("title") or source_url)[:500],
            description=str(metadata.get("description") or "")[:4000],
            markdown=markdown,
            retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            status_code=status_code if isinstance(status_code, int) else None,
        )
