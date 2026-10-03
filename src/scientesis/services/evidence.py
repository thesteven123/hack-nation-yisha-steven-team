from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from uuid import uuid4

from scientesis.adapters.firecrawl import validate_public_url

EVIDENCE_LEVELS = {"official_docs", "peer_reviewed", "preprint", "technical_blog"}
RELEVANCE_LEVELS = {"high", "medium", "low"}
MAX_SOURCE_CHARS = 500_000


def _required_text(value, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"{label} must be 1–{limit} characters.")
    return value.strip()


def validate_evidence_details(details: dict, source_url: str) -> dict:
    if not isinstance(details, dict):
        raise ValueError("Evidence details must be a mapping of fields.")
    level = details.get("evidence_level")
    relevance = details.get("relevance")
    if not isinstance(level, str) or level not in EVIDENCE_LEVELS:
        raise ValueError(f"Evidence level must be one of: {', '.join(sorted(EVIDENCE_LEVELS))}.")
    if not isinstance(relevance, str) or relevance not in RELEVANCE_LEVELS:
        raise ValueError(f"Relevance must be one of: {', '.join(sorted(RELEVANCE_LEVELS))}.")
    return {
        "title": _required_text(details.get("title"), "Title", 500),
        "source_url": validate_public_url(source_url),
        "source_type": level,
        "claim_text": _required_text(details.get("claim"), "External claim", 4000),
        "scope_text": _required_text(details.get("scope"), "Source scope", 4000),
        "limitations_text": _required_text(details.get("limitations"), "Limitations", 4000),
        "implementation_hint": _required_text(details.get("implementation_hint"), "Potential lab use", 2000),
        "quality": {
            "evidence_level": level,
            "relevance": relevance,
            "approved_for": "method_design_only",
            "claim_status": "external_prior_not_local_result",
        },
    }


def save_evidence_candidate(repository, project_id: str, details: dict, page, project_root: str | Path) -> dict:
    if not isinstance(page.markdown, str) or not page.markdown.strip() or len(page.markdown) > MAX_SOURCE_CHARS:
        raise ValueError("The page must contain 1–500,000 characters of extracted Markdown.")
    normalized = validate_evidence_details(details, page.source_url)
    evidence_id = f"EV-{uuid4().hex[:10].upper()}"
    root = Path(project_root).expanduser().resolve()
    evidence_dir = root / "artifacts" / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    destination = evidence_dir / f"{evidence_id}.md"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{evidence_id}-", suffix=".tmp", dir=evidence_dir)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(page.markdown)
        os.replace(temporary, destination)
        content_path = destination.relative_to(root).as_posix()
        record = {
            "id": evidence_id,
            "project_id": project_id,
            "title": normalized["title"],
            "source_url": normalized["source_url"],
            "source_type": normalized["source_type"],
            "claim_text": normalized["claim_text"],
            "scope_text": normalized["scope_text"],
            "limitations_text": normalized["limitations_text"],
            "implementation_hint": normalized["implementation_hint"],
            "retrieved_at": page.retrieved_at,
            "quality": normalized["quality"],
            "content_path": content_path,
            "content_sha256": hashlib.sha256(page.markdown.encode("utf-8")).hexdigest(),
        }
        repository.add_evidence_card(record)
    except Exception:
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise
    return repository.get_evidence_card(evidence_id)


def load_evidence_content(card: dict, project_root: str | Path) -> str:
    relative_path = Path(card.get("content_path", ""))
    if relative_path.is_absolute() or ".." in relative_path.parts or relative_path.parts[:2] != ("artifacts", "evidence"):
        raise ValueError("Evidence content path is invalid.")
    root = Path(project_root).expanduser().resolve()
    path = (root / relative_path).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Evidence content path escapes the project directory.")
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError("Saved evidence content is missing or unreadable.") from error
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if digest != card.get("content_sha256"):
        raise ValueError("Saved evidence content failed its SHA-256 integrity check.")
    return content
