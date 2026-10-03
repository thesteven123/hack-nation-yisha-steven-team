import hashlib
import io
import json
import socket
import sqlite3
from urllib.error import HTTPError

import pytest

from scientesis.adapters.firecrawl import FirecrawlClient, FirecrawlError, ScrapedPage, UnsafeSourceURL, validate_public_url
from scientesis.db.repository import Repository
from scientesis.services.evidence import load_evidence_content, save_evidence_candidate


def public_dns(host, port, type):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, limit):
        assert limit > 0
        return self.payload


def page(markdown="# Robust methods\n\nAn external finding."):
    return ScrapedPage(
        source_url="https://example.com/robust-rl",
        title="Robust methods",
        description="Documentation page",
        markdown=markdown,
        retrieved_at="2026-10-03T12:00:00+00:00",
        status_code=200,
    )


def details():
    return {
        "title": "Robust methods documentation",
        "evidence_level": "official_docs",
        "relevance": "medium",
        "claim": "Training under perturbation can improve robustness in studied tasks.",
        "scope": "The source discusses simulation benchmarks.",
        "limitations": "It does not establish results for our local Reacher configuration.",
        "implementation_hint": "Use this only to motivate a matched comparison.",
    }


def test_public_url_validator_blocks_private_addresses_credentials_and_tokens():
    for value in (
        "file:///etc/passwd",
        "http://127.0.0.1/admin",
        "http://[::1]/",
        "https://user:pass@example.com/",
        "https://example.com/page?session=secret",
        "https://device.local/",
    ):
        with pytest.raises(UnsafeSourceURL):
            validate_public_url(value)


def test_public_url_validator_blocks_domains_that_resolve_to_private_ips(monkeypatch):
    monkeypatch.setattr(
        "scientesis.adapters.firecrawl.socket.getaddrinfo",
        lambda host, port, type: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))],
    )
    with pytest.raises(UnsafeSourceURL, match="public IP addresses"):
        validate_public_url("https://private.example/")


def test_firecrawl_v2_request_parses_markdown_and_sends_optional_bearer_key(monkeypatch):
    monkeypatch.setattr("scientesis.adapters.firecrawl.socket.getaddrinfo", public_dns)
    response = {
        "success": True,
        "data": {
            "markdown": "# Example\n\nA public page.",
            "metadata": {"title": "Example title", "description": "Short summary", "statusCode": 200},
        },
    }
    captured = {}

    def opener(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse(json.dumps(response).encode())

    result = FirecrawlClient(api_key="test-token", opener=opener).scrape("https://example.com/research#section")
    request = captured["request"]
    assert request.full_url == "https://api.firecrawl.dev/v2/scrape"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer test-token"
    assert captured["timeout"] == 45
    assert json.loads(request.data) == {
        "url": "https://example.com/research",
        "formats": ["markdown"],
        "onlyMainContent": True,
        "maxAge": 0,
    }
    assert result.title == "Example title"
    assert result.markdown == "# Example\n\nA public page."
    assert result.status_code == 200


def test_firecrawl_allows_documented_unauthenticated_mode(monkeypatch):
    monkeypatch.setattr("scientesis.adapters.firecrawl.socket.getaddrinfo", public_dns)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    captured = {}

    def opener(request, timeout):
        captured["request"] = request
        return FakeResponse(b'{"success":true,"data":{"markdown":"content","metadata":{}}}')

    FirecrawlClient(opener=opener).scrape("https://example.com/")
    assert captured["request"].get_header("Authorization") is None


def test_firecrawl_reports_service_errors_without_returning_partial_text(monkeypatch):
    monkeypatch.setattr("scientesis.adapters.firecrawl.socket.getaddrinfo", public_dns)

    def opener(*args, **kwargs):
        raise HTTPError("https://api.firecrawl.dev/v2/scrape", 429, "rate limited", {}, io.BytesIO(b"try later"))

    with pytest.raises(FirecrawlError, match="HTTP 429"):
        FirecrawlClient(opener=opener).scrape("https://example.com/")


def test_evidence_card_persists_exact_snapshot_and_requires_human_review(tmp_path):
    repository = Repository(tmp_path / "scientesis.sqlite3")
    repository.initialize()
    project_id = repository.get_project_id()
    card = save_evidence_candidate(repository, project_id, details(), page(), tmp_path)
    source_file = tmp_path / card["content_path"]
    source_bytes = source_file.read_bytes()

    assert card["approval_status"] == "pending_review"
    assert card["quality"] == {
        "evidence_level": "official_docs",
        "relevance": "medium",
        "approved_for": "method_design_only",
        "claim_status": "external_prior_not_local_result",
    }
    assert source_bytes == page().markdown.encode()
    assert card["content_sha256"] == hashlib.sha256(source_bytes).hexdigest()
    assert load_evidence_content(card, tmp_path) == page().markdown
    assert repository.list_evidence_cards(project_id, "approved") == []

    repository.review_evidence_card(card["id"], "approved")
    reviewed = repository.get_evidence_card(card["id"])
    assert reviewed["approval_status"] == "approved"
    assert reviewed["approved_by"] == "scientist"
    assert repository.list_evidence_cards(project_id, "approved")[0]["id"] == card["id"]
    with pytest.raises(ValueError, match="awaiting review"):
        repository.review_evidence_card(card["id"], "rejected")


def test_evidence_content_integrity_failure_is_visible(tmp_path):
    repository = Repository(tmp_path / "scientesis.sqlite3")
    repository.initialize()
    card = save_evidence_candidate(repository, repository.get_project_id(), details(), page(), tmp_path)
    (tmp_path / card["content_path"]).write_text("modified outside review", encoding="utf-8")
    with pytest.raises(ValueError, match="integrity check"):
        load_evidence_content(repository.get_evidence_card(card["id"]), tmp_path)


def test_legacy_evidence_rows_migrate_without_losing_approval(tmp_path):
    database_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE evidence_cards (id TEXT PRIMARY KEY, project_id TEXT, title TEXT, source_url TEXT, source_type TEXT, claim_text TEXT, scope_text TEXT, limitations_text TEXT, implementation_hint TEXT, retrieved_at TEXT, approved_by TEXT, moss_document_id TEXT)"
        )
        connection.execute(
            "INSERT INTO evidence_cards VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("EV-OLD", "robust-robot-learning", "Old source", "https://example.com", "official_docs", "claim", "scope", "limits", "hint", "2026-01-01T00:00:00+00:00", "scientist", None),
        )
    repository = Repository(database_path)
    repository.initialize()
    with repository.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(evidence_cards)")}
        migrated = connection.execute("SELECT approval_status, quality_json FROM evidence_cards WHERE id = 'EV-OLD'").fetchone()
    assert {"approval_status", "quality_json", "content_path", "content_sha256", "approved_at"} <= columns
    assert migrated["approval_status"] == "approved"
    assert json.loads(migrated["quality_json"]) == {}


def test_invalid_human_claim_does_not_leave_an_artifact(tmp_path):
    repository = Repository(tmp_path / "scientesis.sqlite3")
    repository.initialize()
    incomplete = details()
    incomplete["claim"] = " "
    with pytest.raises(ValueError, match="External claim"):
        save_evidence_candidate(repository, repository.get_project_id(), incomplete, page(), tmp_path)
    assert list((tmp_path / "artifacts" / "evidence").glob("*.md")) == []
