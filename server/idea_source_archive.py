"""Original public response bytes, separate from parsed text and scientific claims.

Only a service-derived retained reference authorizes lookup. This helper is not
a global hash HTTP endpoint. Legacy references are never upgraded by URL/hash
similarity. SourceLibrary integration owns download and parse lifecycle calls.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
import sqlite3
import uuid

MAX_RAW_BYTES = 10 * 1024 * 1024
DEFAULT_ARCHIVE_BYTES = 512 * 1024 * 1024
HEX = re.compile(r"[a-f0-9]{64}")
REF_KEYS = {"status", "sha256", "bytes", "fetch_id", "mime"}


class RawArchiveError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _text(value, maximum, *, empty=False):
    if not isinstance(value, str) or (not value and not empty) or len(value.encode("utf-8")) > maximum or "\x00" in value:
        raise RawArchiveError("invalid_raw_metadata")
    return value


class RawSourceArchive:
    """Append-only bytes, per-fetch parse receipts, no implicit refetch or eviction."""
    def __init__(self, library_root, *, max_archive_bytes=DEFAULT_ARCHIVE_BYTES):
        if type(max_archive_bytes) is not int or not 1 <= max_archive_bytes <= 4 * 1024**3:
            raise RawArchiveError("invalid_archive_limit")
        self.root = Path(library_root).absolute()
        self.path = self.root / "sources.sqlite3"
        self.max_archive_bytes = max_archive_bytes
        self._paths()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS raw_sources (
                    digest TEXT PRIMARY KEY, body BLOB NOT NULL, bytes INTEGER NOT NULL,
                    retained_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS raw_fetches (
                    id TEXT PRIMARY KEY, digest TEXT NOT NULL REFERENCES raw_sources(digest),
                    requested_url TEXT NOT NULL, final_url TEXT NOT NULL, mime TEXT NOT NULL,
                    downloaded_at TEXT NOT NULL, parse_version TEXT NOT NULL,
                    parse_key TEXT, parse_status TEXT NOT NULL, parse_error TEXT);
            """)
        self.path.chmod(0o600)

    @classmethod
    def open_existing(cls, library_root):
        """Read facade: no mkdir, schema setup, recovery or file-mode changes."""
        archive = cls.__new__(cls)
        archive.root = Path(library_root).absolute()
        archive.path = archive.root / "sources.sqlite3"
        archive.max_archive_bytes = DEFAULT_ARCHIVE_BYTES
        archive._paths()
        return archive

    def _paths(self):
        # No renderer-controlled paths are accepted. Still reject local symlink
        # substitution so an archive cannot escape its service-owned directory.
        if any(path.is_symlink() for path in (self.root, *self.root.parents)):
            raise RawArchiveError("unsafe_archive_path")
        if any(path.is_symlink() for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm"))):
            raise RawArchiveError("unsafe_archive_path")

    @contextmanager
    def _db(self, *, readonly=False):
        self._paths()
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=3) if readonly else sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            if readonly:
                db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA foreign_keys=ON")
            with db:
                yield db
        finally:
            db.close()

    def retain(self, body, *, requested_url, final_url, mime, parse_version):
        if not isinstance(body, bytes) or not 0 < len(body) <= MAX_RAW_BYTES:
            raise RawArchiveError("invalid_raw_size")
        requested_url, final_url = _text(requested_url, 4096), _text(final_url, 4096)
        mime, parse_version = _text(mime, 1024, empty=True), _text(parse_version, 128)
        digest = hashlib.sha256(body).hexdigest()
        fetch_id, now = uuid.uuid4().hex, datetime.now(timezone.utc).isoformat()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT bytes,length(body) AS actual_size FROM raw_sources WHERE digest=?", (digest,)).fetchone()
            if old:
                if old["bytes"] != len(body) or old["actual_size"] != len(body):
                    raise RawArchiveError("corrupt_raw_artifact")
                previous = db.execute("SELECT body FROM raw_sources WHERE digest=?", (digest,)).fetchone()[0]
                if not isinstance(previous, bytes) or hashlib.sha256(previous).hexdigest() != digest:
                    raise RawArchiveError("corrupt_raw_artifact")
            else:
                total = db.execute("SELECT COALESCE(SUM(bytes),0) FROM raw_sources").fetchone()[0]
                if total + len(body) > self.max_archive_bytes:
                    raise RawArchiveError("raw_archive_full")
                db.execute("INSERT INTO raw_sources VALUES(?,?,?,?)", (digest, body, len(body), now))
            db.execute("INSERT INTO raw_fetches VALUES(?,?,?,?,?,?,?,NULL,'downloaded',NULL)",
                       (fetch_id, digest, requested_url, final_url, mime, now, parse_version))
        return {"status": "retained", "sha256": digest, "bytes": len(body), "fetch_id": fetch_id, "mime": mime}

    def finish_parse(self, fetch_id, *, status, parse_key=None, error=None):
        if not isinstance(fetch_id, str) or re.fullmatch(r"[a-f0-9]{32}", fetch_id) is None:
            raise RawArchiveError("invalid_raw_reference")
        if status not in {"parsed", "failed", "cancelled"}:
            raise RawArchiveError("invalid_parse_status")
        if status == "parsed":
            _text(parse_key, 128)
            if error is not None:
                raise RawArchiveError("invalid_parse_status")
        elif parse_key is not None or not isinstance(error, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error) is None:
            raise RawArchiveError("invalid_parse_status")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT parse_status,parse_key,parse_error FROM raw_fetches WHERE id=?", (fetch_id,)).fetchone()
            if not row:
                raise RawArchiveError("raw_fetch_missing")
            if row["parse_status"] != "downloaded":
                if tuple(row) == (status, parse_key, error):
                    return
                raise RawArchiveError("raw_fetch_already_finished")
            db.execute("UPDATE raw_fetches SET parse_status=?,parse_key=?,parse_error=? WHERE id=?",
                       (status, parse_key, error, fetch_id))

    def read(self, reference, *, include_body=False):
        """Verify exact retained service reference; missing legacy ref stays absent.

        Never use a URL or a bare content hash to synthesize the missing receipt.
        Body is base64 for explicit native Save As, never executable HTML.
        """
        if reference is None or isinstance(reference, dict) and reference.get("status") == "not_retained":
            return {"version": 1, "status": "not_retained", "reason": "original_fetch_has_no_retained_raw_reference"}
        if (not isinstance(reference, dict) or set(reference) != REF_KEYS or reference["status"] != "retained"
                or not isinstance(reference["sha256"], str) or HEX.fullmatch(reference["sha256"]) is None
                or type(reference["bytes"]) is not int or not 0 < reference["bytes"] <= MAX_RAW_BYTES
                or not isinstance(reference["fetch_id"], str) or re.fullmatch(r"[a-f0-9]{32}", reference["fetch_id"]) is None):
            raise RawArchiveError("invalid_raw_reference")
        _text(reference["mime"], 1024, empty=True)
        result = {"version": 1, **reference}
        if not self.path.is_file():
            return {**result, "status": "missing", "reason": "retained_raw_artifact_unavailable"}
        with self._db(readonly=True) as db:
            db.execute("BEGIN")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"raw_sources", "raw_fetches"} <= tables:
                return {**result, "status": "missing", "reason": "retained_raw_artifact_unavailable"}
            fetch = db.execute("SELECT * FROM raw_fetches WHERE id=?", (reference["fetch_id"],)).fetchone()
            meta = db.execute("SELECT bytes,length(body) AS actual_size FROM raw_sources WHERE digest=?", (reference["sha256"],)).fetchone()
            if not fetch or not meta:
                return {**result, "status": "missing", "reason": "retained_raw_artifact_unavailable"}
            if (fetch["digest"] != reference["sha256"] or fetch["mime"] != reference["mime"]
                    or meta["bytes"] != reference["bytes"] or meta["actual_size"] != reference["bytes"]):
                return {**result, "status": "corrupt", "reason": "raw_reference_or_size_mismatch"}
            body = db.execute("SELECT body FROM raw_sources WHERE digest=?", (reference["sha256"],)).fetchone()[0]
            if not isinstance(body, bytes) or hashlib.sha256(body).hexdigest() != reference["sha256"]:
                return {**result, "status": "corrupt", "reason": "raw_hash_mismatch"}
        result.update(verified=True, receipt=dict(fetch), transfer="base64_no_render")
        if include_body:
            result["body_base64"] = base64.b64encode(body).decode("ascii")
        return result
