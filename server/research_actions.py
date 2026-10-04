"""Bounded, local source-span actions; never a scientific claim validator.

All durable state, including content-addressed artifacts, lives in one SQLite
database. Execution does no external work: a transaction can safely roll back
and retry this deterministic calculation. This is not an external job runner.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1
METHOD_VERSION = "exact-source-span/1"
PARSER_VERSION = "utf8-supplied-text/1"
MAX_SOURCE_BYTES = 1024 * 1024
MAX_QUOTE_BYTES = 16 * 1024
MAX_SPANS = 100
REQUIRED_PROTOCOLS = (
    "literature-cache-v0.5", "records-v0.5", "execution-v0.5",
)


class ResearchError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fields(value: object, expected: set[str], name: str) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise ResearchError("invalid_request", f"{name} must contain exactly: {', '.join(sorted(expected))}")
    return value


def _string(value: object, name: str, limit: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ResearchError("invalid_request", f"{name} must be a nonempty string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ResearchError("invalid_request", f"{name} must be valid UTF-8") from exc
    if size > limit:
        raise ResearchError("invalid_request", f"{name} exceeds {limit} UTF-8 bytes")
    return value


def _validate(request: object) -> dict:
    request = _fields(request, {"idempotency_key", "goal", "source", "quote"}, "request")
    key = _string(request["idempotency_key"], "idempotency_key", 128)
    goal = _fields(request["goal"], {"id", "revision", "question", "completion_criterion"}, "goal")
    if type(goal["revision"]) is not int or not 1 <= goal["revision"] <= 2147483647:
        raise ResearchError("invalid_request", "goal.revision must be a positive 32-bit integer")
    goal = {
        "id": _string(goal["id"], "goal.id", 256),
        "revision": goal["revision"],
        "question": _string(goal["question"], "goal.question", 8192),
        "completion_criterion": _string(goal["completion_criterion"], "goal.completion_criterion", 8192),
    }
    source = _fields(request["source"], {"uri", "text", "coverage", "missing_sections"}, "source")
    if source["coverage"] not in ("full_text", "excerpt"):
        raise ResearchError("invalid_request", "source.coverage must be full_text or excerpt")
    missing = source["missing_sections"]
    if not isinstance(missing, list) or len(missing) > 100:
        raise ResearchError("invalid_request", "source.missing_sections must be a list of at most 100 strings")
    missing = [_string(item, "missing section", 256) for item in missing]
    if source["coverage"] == "full_text" and missing:
        raise ResearchError("invalid_request", "full_text coverage cannot declare missing sections")
    return {
        "idempotency_key": key,
        "goal": goal,
        "source": {
            "uri": _string(source["uri"], "source.uri", 4096),
            "text": _string(source["text"], "source.text", MAX_SOURCE_BYTES, empty=True),
            "coverage": source["coverage"],
            "missing_sections": missing,
        },
        "quote": _string(request["quote"], "quote", MAX_QUOTE_BYTES),
    }


class ResearchActionStore:
    """Private service-owned store. Callers authorize access before invoking it."""

    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        self.path = self.root / "research.sqlite3"
        try:
            if self.root.is_symlink() or self.path.is_symlink():
                raise ResearchError("storage_error", "Research state paths must not be symlinks")
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self._connection(write=True, initialize=True) as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, SCHEMA_VERSION):
                    raise ResearchError("unsupported_schema", "Unsupported research database schema")
                if version == 0:
                    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                    if tables:
                        raise ResearchError("unsupported_schema", "Unversioned nonempty research database")
                    db.execute("CREATE TABLE artifacts (digest TEXT PRIMARY KEY, data BLOB NOT NULL)")
                    db.execute("""CREATE TABLE actions (
                        id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
                        request_hash TEXT NOT NULL, spec_hash TEXT NOT NULL REFERENCES artifacts(digest),
                        status TEXT NOT NULL CHECK(status IN ('planned', 'completed')),
                        created_at TEXT NOT NULL, result_digest TEXT REFERENCES artifacts(digest))""")
                    db.execute("""CREATE TABLE events (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT, action_id TEXT NOT NULL REFERENCES actions(id),
                        type TEXT NOT NULL, at TEXT NOT NULL, data_json TEXT NOT NULL)""")
                    db.execute("""CREATE TABLE parse_cache (
                        key TEXT PRIMARY KEY, artifact_digest TEXT NOT NULL REFERENCES artifacts(digest))""")
                    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.path.chmod(0o600)
        except OSError as exc:
            raise ResearchError("storage_error", "Could not initialize research state") from exc

    @contextmanager
    def _connection(self, *, write: bool = False, initialize: bool = False):
        db = None
        try:
            if self.root.is_symlink() or self.path.is_symlink():
                raise ResearchError("storage_error", "Research state paths must not be symlinks")
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if not initialize and db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise ResearchError("unsupported_schema", "Unsupported research database schema")
            yield db
            db.commit()
        except (sqlite3.Error, OSError) as exc:
            raise ResearchError("storage_error", "Research storage failed; the transaction was rolled back") from exc
        finally:
            if db is not None:
                db.close()  # SQLite rolls back uncommitted work, including non-SQL exceptions.

    @staticmethod
    def _read_artifact(db: sqlite3.Connection, digest: str) -> bytes:
        row = db.execute("SELECT data FROM artifacts WHERE digest=?", (digest,)).fetchone()
        if row is None:
            raise ResearchError("not_found", "Artifact was not found")
        data = bytes(row["data"])
        if _digest(data) != digest:
            raise ResearchError("integrity_error", "Artifact content does not match its SHA-256 digest")
        return data

    @classmethod
    def _store_artifact(cls, db: sqlite3.Connection, data: bytes) -> str:
        digest = _digest(data)
        db.execute("INSERT OR IGNORE INTO artifacts(digest, data) VALUES (?, ?)", (digest, data))
        cls._read_artifact(db, digest)
        return digest

    @staticmethod
    def _event(db: sqlite3.Connection, action_id: str, kind: str, data: dict) -> None:
        db.execute("INSERT INTO events(action_id, type, at, data_json) VALUES (?, ?, ?, ?)",
                   (action_id, kind, _now(), _json(data).decode("utf-8")))

    @staticmethod
    def _action(db: sqlite3.Connection, action_id: str) -> sqlite3.Row:
        if not isinstance(action_id, str) or not re.fullmatch(r"[0-9a-f]{32}", action_id):
            raise ResearchError("not_found", "Research action was not found")
        row = db.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        if row is None:
            raise ResearchError("not_found", "Research action was not found")
        return row

    @classmethod
    def _view(cls, db: sqlite3.Connection, action_id: str) -> dict:
        row = cls._action(db, action_id)
        return {
            "action_id": row["id"], "status": row["status"], "created_at": row["created_at"],
            "spec_hash": row["spec_hash"],
            "spec": json.loads(cls._read_artifact(db, row["spec_hash"])),
            "result": json.loads(cls._read_artifact(db, row["result_digest"])) if row["result_digest"] else None,
            "result_digest": row["result_digest"],
            "events": [
                {"seq": event["seq"], "type": event["type"], "at": event["at"],
                 "data": json.loads(event["data_json"])}
                for event in db.execute("SELECT * FROM events WHERE action_id=? ORDER BY seq", (action_id,))
            ],
        }

    def plan(self, request: dict) -> dict:
        request = _validate(request)
        key = request.pop("idempotency_key")
        request_hash = _digest(_json(request))
        source = request["source"]
        source_data = source["text"].encode("utf-8")
        spec = {
            "schema_version": SCHEMA_VERSION,
            "goal": request["goal"],
            "method": {"id": METHOD_VERSION, "parser_version": PARSER_VERSION,
                       "proposition": "The exact supplied quote occurs in the supplied text.",
                       "scientific_claim_validation": False},
            "source": {"uri": source["uri"], "digest": _digest(source_data),
                       "coverage": source["coverage"], "missing_sections": source["missing_sections"],
                       "access_provenance": "caller_supplied_text; URI is a label and is never fetched"},
            "quote": request["quote"],
            "authorization": {"operation": "local_exact_source_span", "external_execution": False},
            "budget": {"max_source_bytes": MAX_SOURCE_BYTES, "max_quote_bytes": MAX_QUOTE_BYTES,
                       "max_returned_spans": MAX_SPANS, "external_requests": 0,
                       "model_tokens": 0, "monetary_cost": 0},
            "qc_criteria": ["source_sha256_matches", "parser_complete_for_supplied_text",
                            "exact_match_present (observation, not an infrastructure success criterion)"],
            "required_protocols": list(REQUIRED_PROTOCOLS),
        }
        with self._connection(write=True) as db:
            existing = db.execute("SELECT id, request_hash FROM actions WHERE idempotency_key=?", (key,)).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise ResearchError("idempotency_conflict", "Idempotency key already identifies a different request")
                return self._view(db, existing["id"])
            self._store_artifact(db, source_data)
            spec_hash = self._store_artifact(db, _json(spec))
            action_id = uuid.uuid4().hex
            db.execute("""INSERT INTO actions(id,idempotency_key,request_hash,spec_hash,status,created_at)
                          VALUES (?, ?, ?, ?, 'planned', ?)""", (action_id, key, request_hash, spec_hash, _now()))
            self._event(db, action_id, "planned", {"spec_hash": spec_hash})
            return self._view(db, action_id)

    @staticmethod
    def _observe(text: str, quote: str) -> dict:
        # KMP keeps repetitive maximum-size inputs linear while counting every
        # overlapping occurrence. Repeated str.find(offset + 1) rescans the
        # whole quote at each occurrence and can monopolize the write lock.
        prefix = [0] * len(quote)
        matched = 0
        for index in range(1, len(quote)):
            while matched and quote[index] != quote[matched]:
                matched = prefix[matched - 1]
            if quote[index] == quote[matched]:
                matched += 1
            prefix[index] = matched

        spans = []
        count = 0
        matched = 0
        char_cursor, byte_cursor, line_cursor = 0, 0, 1
        quote_bytes = len(quote.encode("utf-8"))
        quote_line_delta = quote.count("\n", 0, len(quote) - 1)
        for end, character in enumerate(text, 1):
            while matched and character != quote[matched]:
                matched = prefix[matched - 1]
            if character == quote[matched]:
                matched += 1
            if matched == len(quote):
                count += 1
                if len(spans) < MAX_SPANS:
                    offset = end - len(quote)
                    # Match starts increase monotonically. Encode/count each
                    # intervening segment once, including for Unicode spans.
                    segment = text[char_cursor:offset]
                    byte_cursor += len(segment.encode("utf-8"))
                    line_cursor += segment.count("\n")
                    char_cursor = offset
                    spans.append({
                        "char_start": offset, "char_end": end,
                        "byte_start": byte_cursor, "byte_end": byte_cursor + quote_bytes,
                        "line_start": line_cursor, "line_end": line_cursor + quote_line_delta,
                    })
                matched = prefix[matched - 1]
        return {"exact_match_found": count > 0, "match_count": count, "spans": spans,
                "spans_truncated": count > len(spans),
                "offset_convention": "zero-based, end-exclusive characters/UTF-8 bytes; one-based LF-delimited lines"}

    def run(self, action_id: str) -> dict:
        # Holding the write transaction is intentional: this bounded, entirely
        # local action has no external side effect and cannot be dispatched twice.
        with self._connection(write=True) as db:
            row = self._action(db, action_id)
            if row["status"] == "completed":
                return self._view(db, action_id)
            started = time.perf_counter()
            spec = json.loads(self._read_artifact(db, row["spec_hash"]))
            if spec["method"]["id"] != METHOD_VERSION or spec["method"]["parser_version"] != PARSER_VERSION:
                raise ResearchError("unsupported_schema", "Frozen action method is not supported by this runtime")
            source = spec["source"]
            data = self._read_artifact(db, source["digest"])
            self._event(db, action_id, "execution_started", {"spec_hash": row["spec_hash"]})
            cache_key = _digest(_json({"source": source, "parser_version": PARSER_VERSION}))
            cached = db.execute("SELECT artifact_digest FROM parse_cache WHERE key=?", (cache_key,)).fetchone()
            if cached:
                parsed_digest = cached["artifact_digest"]
                parsed = json.loads(self._read_artifact(db, parsed_digest))
            else:
                parsed = {"text": data.decode("utf-8"), "source": source,
                          "parser_version": PARSER_VERSION, "complete_for_supplied_text": True}
                parsed_digest = self._store_artifact(db, _json(parsed))
                db.execute("INSERT INTO parse_cache(key,artifact_digest) VALUES (?, ?)", (cache_key, parsed_digest))
            if (parsed["source"] != source or parsed["parser_version"] != PARSER_VERSION
                    or parsed["complete_for_supplied_text"] is not True
                    or _digest(parsed["text"].encode("utf-8")) != source["digest"]):
                raise ResearchError("integrity_error", "Parsed source provenance does not match the frozen specification")
            observation = self._observe(parsed["text"], spec["quote"])
            observation.update({"action_id": action_id, "source_digest": source["digest"],
                                "quote": spec["quote"], "coverage": source["coverage"],
                                "missing_sections": source["missing_sections"],
                                "coverage_note": "Only supplied text was inspected; original completeness is unverified.",
                                "is_fresh_replicate": False})
            qc = {"passed": True, "source_sha256_matches": True,
                  "parser_complete_for_supplied_text": True,
                  "exact_match_present": observation["exact_match_found"],
                  "scope": "Mechanical source-span verification only; no scientific or inference assessment."}
            observation_digest = self._store_artifact(db, _json(observation))
            qc_digest = self._store_artifact(db, _json(qc))
            result = {
                "observation": observation, "qc": qc,
                "source_support": "supported" if observation["exact_match_found"] else "cannot_determine",
                "supported_proposition": spec["method"]["proposition"],
                "inference_validity": "cannot_determine",
                "cache": {"key": cache_key, "parsed_source_reused": bool(cached),
                          "observation_reused": False, "verification_executed": True,
                          "reuse_scope": "same source hash, parser version, URI, coverage and missing sections"},
                "cost": {"external_requests": 0, "model_tokens": 0, "monetary_cost": 0,
                         "wall_ms": round((time.perf_counter() - started) * 1000, 3)},
                "artifact_digests": {"source": source["digest"], "parsed_source": parsed_digest,
                                     "observation": observation_digest, "qc": qc_digest},
            }
            result_digest = self._store_artifact(db, _json(result))
            db.execute("UPDATE actions SET status='completed',result_digest=? WHERE id=?", (result_digest, action_id))
            self._event(db, action_id, "completed", {"result_digest": result_digest})
            return self._view(db, action_id)

    def get(self, action_id: str) -> dict:
        with self._connection() as db:
            return self._view(db, action_id)

    def artifact(self, digest: str) -> bytes:
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ResearchError("not_found", "Artifact was not found")
        with self._connection() as db:
            return self._read_artifact(db, digest)
