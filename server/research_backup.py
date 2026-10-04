"""Offline, whole-research backups. Never opens an operational store or a provider.

All service instances that use a research root must hold research_service_guard
for their complete lifetime, including owned-child cleanup. The offline tool
fails immediately while any such service is alive. This is a cooperative local
POSIX lock, not protection against uncooperative writers or a remote filesystem.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import time
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

FORMAT = "agentsdock-research-backup/1"
LOADED_SOURCE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
LOCK = ".maintenance.lock"
MAX_BYTES = 2 * 1024**3
MAX_DATABASES = 64
MAX_RAW_BYTES = 10 * 1024**2
HEX = re.compile(r"[0-9a-f]{64}\Z")
FIXED = {
    "research.sqlite3": "actions", "lab/lab.sqlite3": "lab",
    "ideas/ideas.sqlite3": "ideas", "model-jobs/model-jobs.sqlite3": "models",
    "library/sources.sqlite3": "library", "evidence-cache/evidence-cache.sqlite3": "cache",
}
# Unknown versions/tables are refused, never silently left out of an archive.
SCHEMAS = {
    "actions": ({1}, {"actions", "artifacts", "events", "parse_cache"}),
    "lab": ({1}, {"campaigns", "artifacts", "artifact_links", "versions", "mutations", "events"}),
    "ideas": ({1, 2}, {"sessions", "versions", "generations", "followups", "activities", "stage_packets", "stage_attempts"}),
    "models": ({1}, {"jobs", "artifacts", "quota"}),
    "library": ({0}, {"versions", "urls", "leases", "raw_sources", "raw_fetches"}),
    "cache": ({0}, {"metadata", "entries", "requests"}),
    "corrections": ({1}, {"registrations", "scope_registrations", "edges", "corrections", "intents", "outbox"}),
    "marks": ({1}, {"marks"}),
}
OPTIONAL_IDEA_TABLES = {"followups", "activities", "stage_packets", "stage_attempts"}
PARSED_TABLES = {"versions", "urls", "leases"}
RAW_TABLES = {"raw_sources", "raw_fetches"}
_PROCESS_GUARDS = {}
_PROCESS_GUARDS_MUTEX = threading.Lock()


class BackupError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _require(condition, message, code="integrity_error"):
    if not condition:
        raise BackupError(code, message)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _decode(value):
    return json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _public_errors(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except BackupError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise BackupError("storage_error", "Research backup storage operation failed; no existing destination was overwritten") from exc
    return wrapped


def _safe(path):
    path = Path(os.path.abspath(path))
    for ancestor in [path, *path.parents]:
        _require(not ancestor.is_symlink(), "Backup paths cannot traverse symbolic links", "unsafe_path")
    return path


def _acquire_guard(root, *, service):
    try:
        import fcntl
    except ImportError:
        raise BackupError("unsupported_platform", "Maintenance requires a local POSIX flock filesystem") from None
    root = _safe(root)
    if service:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require(root.is_dir(), "Research root does not exist", "not_found")
    lock = _safe(root / LOCK)
    if not service:
        _require(lock.is_file(), "Start the maintenance-aware service once before offline backup", "maintenance_not_initialized")
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_CREAT if service else 0)
    descriptor = os.open(lock, flags, 0o600)
    try:
        os.set_inheritable(descriptor, False)
        info = os.fstat(descriptor)
        _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, "Maintenance lock must be a private regular file", "unsafe_path")
        try:
            fcntl.flock(descriptor, (fcntl.LOCK_SH if service else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("maintenance_busy", "The research service or another maintenance operation still owns this root") from None
        _require(os.stat(lock, follow_symlinks=False).st_ino == info.st_ino, "Maintenance lock changed during admission", "unsafe_path")
        return root, descriptor
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _guard(root, *, service):
    root, descriptor = _acquire_guard(root, service=service)
    try:
        yield root
    finally:
        os.close(descriptor)


def research_service_guard(root):
    """Context lifetime lock for bounded tools/tests that join all owned work."""
    return _guard(root, service=True)


def retain_research_service_guard(root):
    """Production fence retained until OS process exit, including finalizers.

    No atexit release or Python-owned descriptor object is registered: bounded
    server lifespan cleanup may finish while storage finalizers remain alive.
    The numeric descriptor stays open, CLOEXEC, and cannot be released through
    this API. Re-entry validates the actual inode instead of trusting a key.
    """
    root = _safe(root)
    key = str(root)
    with _PROCESS_GUARDS_MUTEX:
        existing = _PROCESS_GUARDS.get(key)
        if existing is not None:
            _require(root.is_dir(), "Retained research root disappeared", "unsafe_path")
            lock = _safe(root / LOCK)
            _require(lock.is_file(), "Retained maintenance lock disappeared", "unsafe_path")
            root_info, lock_info = root.stat(), lock.stat(follow_symlinks=False)
            try:
                descriptor_info = os.fstat(existing["descriptor"])
            except OSError:
                raise BackupError("unsafe_path", "Retained maintenance descriptor is no longer valid") from None
            _require((root_info.st_dev, root_info.st_ino) == existing["root_identity"]
                     and (lock_info.st_dev, lock_info.st_ino) == existing["lock_identity"]
                     and (descriptor_info.st_dev, descriptor_info.st_ino) == existing["lock_identity"]
                     and stat.S_ISREG(lock_info.st_mode) and lock_info.st_nlink == 1
                     and not os.get_inheritable(existing["descriptor"]),
                     "Retained research root or maintenance lock was replaced", "unsafe_path")
            return root
        root, descriptor = _acquire_guard(root, service=True)
        root_info, lock_info = root.stat(), os.fstat(descriptor)
        _PROCESS_GUARDS[key] = {"descriptor": descriptor,
                                "root_identity": (root_info.st_dev, root_info.st_ino),
                                "lock_identity": (lock_info.st_dev, lock_info.st_ino)}
        return root


def _kind(relative):
    if relative in FIXED:
        return FIXED[relative]
    match = re.fullmatch(r"dependencies/[0-9a-f]{64}/(corrections|marks)\.sqlite3", relative)
    return match[1] if match else None


def _inventory(root, *, bundle=False):
    databases, total = {}, 0
    files = []
    for path in root.rglob("*"):
        _safe(path)
        _require(path.is_dir() or path.is_file(), "Research root contains a special file", "unsafe_path")
        if path.is_file():
            files.append(path.relative_to(root).as_posix())
    for relative in sorted(files):
        if relative == LOCK and not bundle:
            continue
        kind = _kind(relative)
        if kind:
            databases[relative] = kind
            total += (root / relative).stat().st_size
            continue
        if not bundle and any(relative == name + ending for name in files if _kind(name)
                              for ending in ("-wal", "-shm", "-journal")):
            total += (root / relative).stat().st_size
            continue
        raise BackupError("unexpected_file", "An unrecognized research file is present; no partial backup was made")
    _require(databases, "No research databases are present", "not_found")
    _require(len(databases) <= MAX_DATABASES and total <= MAX_BYTES, "Complete research backup exceeds its configured size bound", "too_large")
    for relative, kind in databases.items():
        if kind in {"marks", "corrections"}:
            sibling = str(Path(relative).with_name("marks.sqlite3" if kind == "corrections" else "corrections.sqlite3")).replace("\\", "/")
            _require(sibling in databases, "The correction producer and receipt database must be backed up as a pair")
    return databases


@contextmanager
def _read(path):
    with closing(sqlite3.connect(_safe(path).as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        yield db


def _backup_database(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    started = time.monotonic()
    def progress(status, remaining, total):
        _require(time.monotonic() - started < 120, "SQLite snapshot timed out; nothing was published", "backup_timeout")
        _require(total * 65536 <= MAX_BYTES * 64, "SQLite snapshot exceeded its page bound", "too_large")
    with _read(source) as incoming, closing(sqlite3.connect(destination)) as outgoing:
        _require(incoming.execute("PRAGMA page_count").fetchone()[0] * incoming.execute("PRAGMA page_size").fetchone()[0] <= MAX_BYTES,
                 "Database snapshot exceeds the byte limit", "too_large")
        incoming.backup(outgoing, pages=128, progress=progress, sleep=.01)
        outgoing.execute("PRAGMA journal_mode=DELETE")
    destination.chmod(0o600)


def _cell(value):
    return {"blob_base64": base64.b64encode(value).decode("ascii")} if isinstance(value, bytes) else value


def _raw_integrity(db):
    # Check SQLite lengths before fetching any BLOB into Python/base64 memory.
    for row in db.execute("SELECT digest,bytes,length(body),typeof(body),retained_at FROM raw_sources"):
        _require(isinstance(row[0], str) and HEX.fullmatch(row[0]) and type(row[1]) is int
                 and 0 < row[1] == row[2] <= MAX_RAW_BYTES and row[3] == "blob"
                 and isinstance(row[4], str) and 0 < len(row[4]) <= 128, "Retained source has invalid size or identity")
    for row in db.execute("SELECT digest,body FROM raw_sources"):
        _require(_sha(row[1]) == row[0], "Retained source bytes do not match their content hash")
    for row in db.execute("SELECT * FROM raw_fetches"):
        _exists(db, "SELECT 1 FROM raw_sources WHERE digest=?", (row["digest"],), "Raw fetch has no retained source bytes")
        _require(isinstance(row["id"], str) and re.fullmatch(r"[0-9a-f]{32}", row["id"]), "Raw fetch identity is malformed")
        for name, bound, empty in (("requested_url", 4096, False), ("final_url", 4096, False), ("mime", 1024, True),
                                   ("downloaded_at", 128, False), ("parse_version", 128, False)):
            value = row[name]
            _require(isinstance(value, str) and (empty or value) and len(value.encode()) <= bound and "\0" not in value, "Raw fetch metadata is malformed")
        status, key, error = row["parse_status"], row["parse_key"], row["parse_error"]
        _require(status in {"downloaded", "parsed", "failed", "cancelled"}, "Unknown raw fetch parse status")
        if status == "parsed":
            _require(isinstance(key, str) and 0 < len(key.encode()) <= 128 and error is None, "Completed parse receipt is malformed")
        elif status == "downloaded":
            _require(key is None and error is None, "Unparsed raw fetch has a spurious parse result")
        else:
            _require(key is None and isinstance(error, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error), "Failed/cancelled parse receipt is malformed")


def _raw_reference(db, reference, content_hash):
    if reference is None:
        return False
    _require(isinstance(reference, dict), "Source retention reference is malformed")
    _require(reference.get("status") in {"retained", "not_retained"}, "Unknown source retention state")
    if reference["status"] == "not_retained":
        return False  # Later same-hash fetches cannot upgrade a legacy receipt.
    _require(set(reference) == {"status", "sha256", "bytes", "fetch_id", "mime"}, "Retained source reference is malformed")
    _require(isinstance(reference["sha256"], str) and HEX.fullmatch(reference["sha256"])
             and type(reference["bytes"]) is int and 0 < reference["bytes"] <= MAX_RAW_BYTES
             and isinstance(reference["fetch_id"], str) and re.fullmatch(r"[0-9a-f]{32}", reference["fetch_id"])
             and isinstance(reference["mime"], str), "Retained source reference fields are malformed")
    _require(db is not None, "Retained source archive is missing")
    _require(db.execute("SELECT 1 FROM sqlite_master WHERE name='raw_fetches'").fetchone(), "Retained source archive is missing")
    row = db.execute("SELECT f.digest,f.mime,r.bytes FROM raw_fetches f JOIN raw_sources r ON r.digest=f.digest WHERE f.id=?", (reference["fetch_id"],)).fetchone()
    _require(row is not None and row[0] == reference["sha256"] == content_hash and row[1] == reference["mime"] and row[2] == reference["bytes"],
             "Published source reference differs from its original fetch/bytes")
    return True


def _raw_holder(library, value):
    """Only reader-owned fields, never recursive client_metadata inspection."""
    _require(isinstance(value, dict), "Reader provenance is malformed")
    _raw_reference(library, value.get("raw_document"), value.get("content_hash"))
    alternatives = value.get("raw_fetches", [])
    _require(isinstance(alternatives, list), "Alternate raw fetch receipts are malformed")
    for receipt in alternatives:
        _require(isinstance(receipt, dict), "Alternate raw fetch receipt is malformed")
        _raw_reference(library, receipt.get("raw_document"), receipt.get("content_hash"))


def _idea_raw_inputs(ideas, library):
    # Every immutable revision matters, even after a new generation replaces the
    # current reading. Supplied brief sources cannot contain provenance fields.
    for row in ideas.execute("SELECT data FROM versions"):
        research = _decode(row[0]).get("research", {})
        for source in research.get("sources", []):
            _raw_holder(library, source.get("provenance", {}))
        for paper in research.get("papers", []):
            _raw_holder(library, paper)
    if ideas.execute("SELECT 1 FROM sqlite_master WHERE name='stage_packets'").fetchone():
        for row in ideas.execute("SELECT sources FROM stage_packets"):
            for source in _decode(row[0]):
                _raw_holder(library, source.get("provenance", {}))


def _imported_raw_input(ideas, library, origin, source):
    provenance = source.get("provenance", {})
    if provenance.get("kind") != "idea_frozen_packet":
        return  # user_supplied / corrected metadata never authenticates a fetch.
    original = provenance.get("original_provenance", {})
    if not (original.get("raw_document") or original.get("raw_fetches")):
        return  # Preserve legacy imports without imposing a new lineage schema.
    _require(ideas is not None and isinstance(origin, dict) and origin.get("kind") == "idea"
             and all(provenance.get(key) == origin.get(key) for key in ("session_id", "generation_id", "decision_revision")),
             "Imported raw source does not match its authoritative Idea origin")
    row = ideas.execute("SELECT data FROM versions WHERE session_id=? AND revision=?",
                        (origin["session_id"], origin.get("decision_revision"))).fetchone()
    _require(row is not None, "Imported raw source decision revision is missing")
    item = _decode(row[0]); decision = item.get("decision") or {}
    selected = [decision.get("selected_id")] if decision.get("kind") == "select" else decision.get("selected_ids")
    _require(item.get("generation_id") == origin["generation_id"] and decision.get("generation_id") == origin["generation_id"]
             and decision.get("kind") in {"select", "combine"} and selected == origin.get("selected_ids"),
             "Imported raw source is not bound to the saved human selection")
    result = item.get("result") or {}
    evidence = next((e for e in (result.get("literature") or {}).get("evidence", []) if e["id"] == provenance.get("evidence_id")), None)
    selected_evidence = {identity for direction in (result.get("ideas") or {}).get("directions", [])
                         if direction["id"] in selected for identity in direction.get("evidence_ids", [])}
    digest = _sha(source["text"].encode("utf-8"))
    _require(evidence is not None and evidence["id"] in selected_evidence
             and evidence.get("source_id") == provenance.get("source_id")
             and evidence.get("source_hash") == provenance.get("source_hash") == digest
             and evidence["quote"] in source["text"], "Imported raw source differs from selected frozen evidence")
    def matches(encoded):
        packet = _decode(encoded)
        return (packet.get("id") == provenance["source_id"] and packet.get("text") == source["text"]
                and packet.get("provenance", {}) == original)
    found = False
    if ideas.execute("SELECT 1 FROM sqlite_master WHERE name='stage_packets'").fetchone():
        rows = ideas.execute("SELECT s.value FROM stage_packets p JOIN generations g ON g.id=p.generation_id, json_each(p.sources) s "
                             "WHERE g.session_id=? AND p.generation_id=? AND json_extract(s.value,'$.id')=?",
                             (origin["session_id"], origin["generation_id"], provenance["source_id"]))
        found = any(matches(row[0]) for row in rows)
    if not found:
        for field in ("research.sources", "brief.sources"):
            rows = ideas.execute("SELECT s.value FROM versions v,json_each(v.data,?) s WHERE v.session_id=? "
                                 "AND json_extract(v.data,'$.generation_id')=? AND json_extract(s.value,'$.id')=?",
                                 ("$." + field, origin["session_id"], origin["generation_id"], provenance["source_id"]))
            if any(matches(row[0]) for row in rows):
                found = True
                break
    _require(found, "Imported raw provenance differs from its authoritative frozen source packet")
    _raw_holder(library, original)


def _database_report(path, kind):
    with _read(path) as db:
        _require([r[0] for r in db.execute("PRAGMA integrity_check")] == ["ok"], "SQLite integrity check failed")
        _require(not db.execute("PRAGMA foreign_key_check").fetchone(), "SQLite foreign key check failed")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        versions, allowed = SCHEMAS[kind]
        _require(version in versions, "Unsupported research database schema", "unsupported_schema")
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        required = allowed - OPTIONAL_IDEA_TABLES if kind == "ideas" else (set() if kind == "library" else allowed)
        if kind == "corrections":
            required = required - {"scope_registrations"}
        _require(required <= tables <= allowed, "Missing or unrecognized research tables", "unsupported_schema")
        if kind == "library":
            _require((PARSED_TABLES <= tables or RAW_TABLES <= tables) and not (tables & PARSED_TABLES and not PARSED_TABLES <= tables)
                     and not (tables & RAW_TABLES and not RAW_TABLES <= tables), "Incomplete source-library table family", "unsupported_schema")
            if RAW_TABLES <= tables:
                _raw_integrity(db)
        _require(not db.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger','view')").fetchone(), "Unrecognized active database schema", "unsupported_schema")
        if kind == "cache":
            _require([r[0] for r in db.execute("SELECT version FROM metadata")] == [1], "Unsupported evidence cache schema", "unsupported_schema")
        report = {}
        for table in sorted(tables | ({"sqlite_sequence"} if db.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone() else set())):
            rows, digest = 0, hashlib.sha256()
            columns = [r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
            digest.update(_json(columns) + b"\n")
            for row in db.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
                digest.update(_json([_cell(value) for value in row]) + b"\n")
                rows += 1
            report[table] = {"rows": rows, "logical_sha256": digest.hexdigest()}
        if "artifacts" in tables:
            for row in db.execute("SELECT digest,data FROM artifacts"):
                _require(row[0] == _sha(bytes(row[1])), "A content-addressed artifact is corrupt")
        schema = [list(r) for r in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")]
        return {"kind": kind, "schema_version": version, "schema_sha256": _sha(_json(schema)), "tables": report,
                "bytes": path.stat().st_size, "sha256": _file_hash(path)}


def _exists(db, sql, values, message):
    _require(db is not None and db.execute(sql, values).fetchone() is not None, message)


def _artifact(db, digest):
    _require(isinstance(digest, str) and HEX.fullmatch(digest), "Malformed artifact reference")
    row = db.execute("SELECT data FROM artifacts WHERE digest=?", (digest,)).fetchone() if db else None
    _require(row is not None, "A required artifact is missing")
    return _decode(row[0])


def _packet_refs(value):
    """Only the Lab reference fields in a native TaskPacket, not source hashes."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"input_artifact", "derived_from_input_artifact", "spec_artifact", "observation_artifact", "round_artifact", "dispatch_artifact", "hypothesis_set_artifact",
                       "previous_hypothesis_set_artifact", "previous_artifact", "decision_artifact", "artifact"} and child:
                yield child
            elif key == "input_refs":
                yield from child
            elif key not in {"provenance", "original_provenance", "prior_interpretations"}:
                yield from _packet_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _packet_refs(child)


def _lab_integrity(db, cid, campaign):
    """Stream immutable history: the separate 16 MiB JSON export cap is irrelevant."""
    from research_lab import DEFAULT_ADAPTERS
    _require(campaign["id"] == cid and re.fullmatch(r"campaign_[0-9a-f]{32}", cid), "Campaign identity differs")
    revision = campaign["revision"]
    _require(type(revision) is int and revision > 0, "Invalid campaign revision")
    links = {r[0] for r in db.execute("SELECT digest FROM artifact_links WHERE campaign_id=?", (cid,))}
    for row in db.execute("SELECT a.data FROM artifacts a JOIN artifact_links l ON a.digest=l.digest WHERE l.campaign_id=?", (cid,)):
        value = _decode(row[0])
        _require(_json(value) == bytes(row[0]), "Lab artifact is not canonical JSON")
        _require(all(ref in links for ref in _packet_refs(value)), "Lab artifact has a missing immutable reference")
    fixed_budget = {key: campaign["budget"][key] for key in ("max_actions", "max_rounds")}
    count = 0
    for row in db.execute("SELECT revision,at,event,payload FROM versions WHERE campaign_id=? ORDER BY revision", (cid,)):
        count += 1
        value = _decode(row[3])
        _require(row[0] == count and value["revision"] == count and value["id"] == cid and value["schema_version"] == 1,
                 "Campaign version history is incomplete or inconsistent")
        adapter = DEFAULT_ADAPTERS.get(value["adapter"]["id"])
        _require(adapter is not None and value["adapter"]["execution"] == "local_deterministic"
                 and value["brief"]["authorized_actions"] == adapter.manifest["methods"], "Backup cannot expand adapter execution permissions")
        budget = value["budget"]
        _require({key: budget[key] for key in fixed_budget} == fixed_budget and 1 <= budget["max_actions"] <= 12 and 1 <= budget["max_rounds"] <= 6,
                 "Campaign resource ceiling changed")
        _require(budget["reserved_actions"] == 0 and budget["used_actions"] == len(value["rounds"]) <= budget["max_rounds"]
                 and budget["remaining_actions"] == budget["max_actions"] - budget["used_actions"] >= 0, "Local action budget is inconsistent")
        _require(len({r["run"]["id"] for r in value["rounds"]}) == len(value["rounds"]), "Duplicate local run identity")
        for run in (r["run"] for r in value["rounds"]):
            _require(run["usage"]["actions"] == 1 and run["is_independent_replicate"] is False
                     and all(run["usage"][key] == 0 for key in ("external_requests", "model_tokens", "monetary_cost")), "Local run ledger differs from its adapter scope")
        _require(all(ref in links for ref in _packet_refs(value)), "Campaign history has a missing immutable artifact")
        if value.get("branch_set"):
            from research_branches import verify_persisted
            verify_persisted(value, lambda digest: _artifact(db, digest))
        event = db.execute("SELECT * FROM events WHERE campaign_id=? AND revision=?", (cid, count)).fetchall()
        _require(len(event) == 1, "Campaign event history is incomplete or duplicated")
        event = dict(event[0]); event.pop("campaign_id")
        event["data"] = _decode(event["data"])
        saved = value["events"][-1]
        _require(event["at"] == row[1] and event["type"] == row[2]
                 and event == {key: child for key, child in saved.items() if key != "artifact"}, "Immutable event differs from its version")
        if saved.get("artifact"):
            _require(_artifact(db, saved["artifact"]) == event, "Frozen event artifact differs from the ledger")
        if count == revision:
            _require(value == campaign, "Current campaign differs from its final immutable version")
    _require(count == revision and db.execute("SELECT count(*) FROM events WHERE campaign_id=?", (cid,)).fetchone()[0] == revision,
             "Campaign history has missing or excess versions/events")
    seen = set()
    for row in db.execute("SELECT scope,key,request_hash,response FROM mutations WHERE json_extract(response,'$.id')=?", (cid,)):
        value = _decode(row[3]); number = value["revision"]
        _require(row[0] in ("create", cid) and (row[0] == "create") == (number == 1) and HEX.fullmatch(row[2])
                 and isinstance(row[1], str) and 1 <= len(row[1].encode()) <= 128 and number not in seen, "Campaign replay identity is invalid")
        saved = db.execute("SELECT payload FROM versions WHERE campaign_id=? AND revision=?", (cid, number)).fetchone()
        _require(saved is not None and _decode(saved[0]) == value, "Replay receipt differs from its immutable version")
        seen.add(number)
    _require(len(seen) == revision, "Campaign replay receipt is missing")


def _idea_integrity(db):
    """Verify service-owned immutable references; never infer refs in source text."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def generation(sid, gid):
        if gid is not None:
            _exists(db, "SELECT 1 FROM generations WHERE session_id=? AND id=?", (sid, gid),
                    "Idea generation is missing or belongs to another session")

    def version(sid, revision):
        _require(type(revision) is int and revision > 0, "Invalid Idea version reference")
        row = db.execute("SELECT data FROM versions WHERE session_id=? AND revision=?", (sid, revision)).fetchone()
        _require(row is not None, "An immutable Idea version reference is missing")
        return _decode(row[0])

    def attempt(sid, identity, gid=None):
        row = db.execute("SELECT generation_id,data FROM stage_attempts WHERE session_id=? AND id=?",
                         (sid, identity)).fetchone() if "stage_attempts" in tables else None
        _require(row is not None and (gid is None or row[0] == gid), "Idea attempt reference is missing or has another owner")
        return _decode(row[1])

    def packet(gid, number, stage, digest):
        row = db.execute("SELECT sources,context FROM stage_packets WHERE generation_id=? AND round=? AND stage=?",
                         (gid, number, stage)).fetchone() if "stage_packets" in tables else None
        _require(row is not None and _sha(_json({"sources": _decode(row[0]), "context": _decode(row[1])})) == digest,
                 "Idea frozen input reference is missing or differs")

    def snapshot(sid, value):
        generation(sid, value.get("generation_id"))
        for decision in (value.get("decision"), value.get("decision_card")):
            if not decision:
                continue
            generation(sid, decision.get("generation_id"))
            for key in ("options_revision", "options_version"):
                if key in decision:
                    _require(decision[key] <= value["revision"], "Idea choice refers to a future version")
                    version(sid, decision[key])
        for event in value.get("events", []):
            generation(sid, event.get("generation_id"))
            if event.get("attempt_id"):
                attempt(sid, event["attempt_id"], event.get("generation_id"))
        research = value.get("research") or {}
        for metadata in research.get("input_packets", []):
            packet(value.get("generation_id"), metadata["round"], metadata["stage"], metadata["digest"])
        for summary in research.get("validation_attempts", []):
            stored = attempt(sid, summary["id"], value.get("generation_id"))
            _require(all(stored.get(key) == child for key, child in summary.items()), "Idea attempt summary differs from its immutable record")
        pending = research.get("pending_repair")
        if pending:
            stored = attempt(sid, pending["attempt_id"], value.get("generation_id"))
            _require(stored["status"] == "rejected" and stored.get("repair_scheduled")
                     and stored["input_ref"] == pending["input_ref"], "Idea repair refers to another rejected input")
        for activity in research.get("activities", []):
            row = db.execute("SELECT generation_id,data FROM activities WHERE session_id=? AND seq=?",
                             (sid, activity.get("seq"))).fetchone() if "activities" in tables else None
            _require(row is not None and row[0] == value.get("generation_id")
                     and _decode(row[1]) == {k: v for k, v in activity.items() if k != "seq"},
                     "Idea activity reference is missing or differs")

    count = 0
    for sid, encoded in db.execute("SELECT id,data FROM sessions"):
        current = _decode(encoded)
        _require(current["id"] == sid and type(current["revision"]) is int and current["revision"] > 0,
                 "Idea session identity or revision differs")
        number, saved = 0, None
        for revision, data in db.execute("SELECT revision,data FROM versions WHERE session_id=? ORDER BY revision", (sid,)):
            number += 1
            saved = _decode(data)
            _require(revision == number and saved["id"] == sid and saved["revision"] == number,
                     "Idea version history is incomplete or inconsistent")
            snapshot(sid, saved)
        _require(number == current["revision"], "Idea current revision is missing or history has extra versions")
        # Activity receipts can be appended without changing the human revision.
        # They have their own immutable table and are checked by snapshot above.
        def stable(value):
            value = dict(value)
            if "research" in value:
                value["research"] = {k: v for k, v in value["research"].items()
                                     if k not in {"activities", "activities_omitted", "agents"}}
            return value
        _require(stable(saved) == stable(current), "Current Idea snapshot differs from its immutable version")
        snapshot(sid, current)
        count += 1
    for table in ("versions", "generations", "followups", "activities", "stage_attempts"):
        if table in tables:
            for row in db.execute(f'SELECT session_id FROM "{table}"'):
                _exists(db, "SELECT 1 FROM sessions WHERE id=?", (row[0],), "Idea history refers to a missing session")
    for gid, sid, brief in db.execute("SELECT id,session_id,brief FROM generations"):
        start = db.execute("SELECT data FROM versions WHERE session_id=? AND json_extract(data,'$.generation_id')=? ORDER BY revision LIMIT 1",
                           (sid, gid)).fetchone()
        _require(start is not None, "Idea generation has no immutable start version")
        value = _decode(start[0])
        _require(value["brief"] == _decode(brief) and value["events"][-1].get("type") == "generation_started"
                 and value["events"][-1].get("generation_id") == gid, "Idea generation start identity differs")
    if "followups" in tables:
        for sid, revision in db.execute("SELECT session_id,revision FROM followups"):
            value = version(sid, revision)
            _require((value.get("decision") or {}).get("kind") == "followup"
                     and value["events"][-1].get("type") == "human_followup", "Idea follow-up replay points to another operation")
    if "activities" in tables:
        for sid, gid, data in db.execute("SELECT session_id,generation_id,data FROM activities"):
            generation(sid, gid)
            _require(_decode(data).get("generation_id") == gid, "Idea activity generation differs from its row")
    if "stage_packets" in tables:
        for gid, number, stage in db.execute("SELECT generation_id,round,stage FROM stage_packets"):
            _exists(db, "SELECT 1 FROM generations WHERE id=?", (gid,), "Frozen Idea packet generation is missing")
            _require(type(number) is int and number >= 0 and stage in {"literature", "ideas", "review"}, "Invalid frozen Idea packet identity")
    if "stage_attempts" in tables:
        for aid, sid, gid, number, stage, ordinal, data in db.execute("SELECT * FROM stage_attempts"):
            generation(sid, gid)
            value = _decode(data)
            _require(all(value.get(key) == expected for key, expected in
                         (("id", aid), ("generation_id", gid), ("round", number), ("stage", stage), ("ordinal", ordinal))),
                     "Idea attempt identity differs from its row")
            ref = value.get("input_ref") or {}
            _require(all(ref.get(key) == expected for key, expected in (("generation_id", gid), ("round", number), ("stage", stage))),
                     "Idea attempt input identity differs")
            if ref.get("packet_digest"):
                packet(gid, number, stage, ref["packet_digest"])
            if value.get("repair_of"):
                prior = attempt(sid, value["repair_of"], gid)
                _require(prior["round"] == number and prior["stage"] == stage and prior["ordinal"] < ordinal
                         and prior["status"] == "rejected" and prior.get("repair_scheduled"), "Idea repair lineage differs")
    return count


def _crosscheck(root, inventory):
    """Validate durable references without current-state reconciliation/writes."""
    counts = {"lab_campaigns": 0, "model_jobs": 0, "idea_sessions": 0, "idea_unretained_attempts": 0,
              "dependency_namespaces": 0, "cache_entries": 0,
              "raw_sources": {"retained_blobs": 0, "retained_bytes": 0, "fetch_receipts": 0, "versions_retained": 0, "versions_not_retained": 0}}
    with ExitStack() as stack:
        stores = {rel: stack.enter_context(_read(root / rel)) for rel in inventory}
        lab, ideas, models = (stores.get(p) for p in ("lab/lab.sqlite3", "ideas/ideas.sqlite3", "model-jobs/model-jobs.sqlite3"))
        library = stores.get("library/sources.sqlite3")
        actions = stores.get("research.sqlite3")
        if actions:
            for row in actions.execute("SELECT spec_hash,result_digest FROM actions"):
                spec = _artifact(actions, row[0])
                _exists(actions, "SELECT 1 FROM artifacts WHERE digest=?", (spec["source"]["digest"],), "Action source artifact is missing")
                if row[1]:
                    result = _artifact(actions, row[1])
                    for digest in result["artifact_digests"].values():
                        _exists(actions, "SELECT 1 FROM artifacts WHERE digest=?", (digest,), "Action result artifact is missing")
        if lab:
            for row in lab.execute("SELECT id,payload FROM campaigns"):
                _lab_integrity(lab, row[0], _decode(row[1]))
                counts["lab_campaigns"] += 1
                origin = _decode(row[1]).get("origin")
                if origin and origin.get("kind") == "idea":
                    _exists(ideas, "SELECT 1 FROM sessions WHERE id=?", (origin["session_id"],), "Campaign Idea origin is missing")
                    _exists(ideas, "SELECT 1 FROM generations WHERE id=? AND session_id=?", (origin["generation_id"], origin["session_id"]), "Campaign Idea generation is missing")
                    if origin.get("decision_revision"):
                        _exists(ideas, "SELECT 1 FROM versions WHERE session_id=? AND revision=?", (origin["session_id"], origin["decision_revision"]), "Campaign Idea decision revision is missing")
                for digest in lab.execute("SELECT DISTINCT json_extract(payload,'$.input_artifact') FROM versions WHERE campaign_id=?", (row[0],)):
                    for source in _artifact(lab, digest[0]).get("sources", []):
                        _imported_raw_input(ideas, library, origin, source)
        if ideas:
            counts["idea_sessions"] = _idea_integrity(ideas)
            _idea_raw_inputs(ideas, library)
            for table in ("versions", "generations", "followups", "activities", "stage_attempts"):
                if not ideas.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                    continue
                for row in ideas.execute(f'SELECT session_id FROM "{table}"'):
                    _exists(ideas, "SELECT 1 FROM sessions WHERE id=?", (row[0],), "Idea history refers to a missing session")
            if ideas.execute("SELECT 1 FROM sqlite_master WHERE name='stage_packets'").fetchone():
                for row in ideas.execute("SELECT generation_id,sources,context FROM stage_packets"):
                    _exists(ideas, "SELECT 1 FROM generations WHERE id=?", (row[0],), "Frozen Idea packet generation is missing")
                    _decode(row[1]); _decode(row[2])
            if ideas.execute("SELECT 1 FROM sqlite_master WHERE name='stage_attempts'").fetchone():
                for row in ideas.execute("SELECT generation_id,round,stage,data FROM stage_attempts"):
                    value = _decode(row[3])
                    if value.get("output_retained", value.get("output") is not None):
                        _require(_sha(_json(value["output"])) == value["output_hash"], "Idea attempt output hash differs")
                    else:
                        _require(value["status"] == "rejected" and value["output"] is None, "An accepted Idea output cannot be missing")
                        _require((value.get("output_hash") is None and value.get("output_bytes") is None) or
                                 (isinstance(value.get("output_hash"), str) and HEX.fullmatch(value["output_hash"])
                                  and type(value.get("output_bytes")) is int and value["output_bytes"] >= 0), "Unretained output diagnostic is malformed")
                        counts["idea_unretained_attempts"] += 1
                    ref = value.get("input_ref") or {}
                    if ref.get("packet_digest"):
                        packet = ideas.execute("SELECT sources,context FROM stage_packets WHERE generation_id=? AND round=? AND stage=?", tuple(row[:3])).fetchone()
                        _require(packet is not None and _sha(_json({"sources": _decode(packet[0]), "context": _decode(packet[1])})) == ref["packet_digest"], "Idea attempt packet differs")
        if models:
            actual = {}
            for row in models.execute("SELECT id,campaign_id,payload FROM jobs"):
                value = _decode(row[2])
                _require(value["id"] == row[0] and value["campaign_id"] == row[1], "Native job identity differs")
                _exists(lab, "SELECT 1 FROM versions WHERE campaign_id=? AND revision=?", (row[1], value["campaign_revision"]), "Native task campaign revision is missing")
                for key in ("packet_ref", "output_ref", "raw_output_ref"):
                    if value.get(key):
                        _artifact(models, value[key])
                packet = _artifact(models, value["packet_ref"])
                _require(packet["campaign_id"] == row[1] and packet["campaign_revision"] == value["campaign_revision"], "Native frozen packet identity differs")
                if value.get("branch_id"):
                    _require(packet.get("branch_id") == value["branch_id"] and packet.get("branch_scope") == value.get("branch_scope"),
                             "Native branch packet differs from its reserved scope")
                    historical = _decode(lab.execute("SELECT payload FROM versions WHERE campaign_id=? AND revision=?", (row[1], value["campaign_revision"])).fetchone()[0])
                    branch = next((b for b in historical.get("branch_set", {}).get("branches", []) if b["id"] == value["branch_id"]), None)
                    _require(branch is not None and branch["revision"] == value["branch_scope"]["branch_revision"]
                             and historical["branch_set"]["authority_epoch"] == value["branch_scope"]["authority_epoch"],
                             "Native branch reservation has no matching historical context")
                for digest in _packet_refs(packet):
                    _exists(lab, "SELECT 1 FROM artifact_links WHERE campaign_id=? AND digest=?", (row[1], digest), "Native frozen input reference is missing from this campaign")
                for prior in packet.get("prior_interpretations", []):
                    _exists(models, "SELECT 1 FROM jobs WHERE id=? AND campaign_id=? AND json_extract(payload,'$.output_ref')=?",
                            (prior["job_id"], row[1], prior["output_ref"]), "Native review interpretation origin is missing")
                if value.get("output_ref"):
                    _require(_artifact(models, value["output_ref"]) == value["output"], "Native accepted output differs from its artifact")
                usage = actual.setdefault(row[1], [0, 0])
                usage[0] += value.get("attempt_count", 0)
                usage[1] += value["status"] == "planned"
                counts["model_jobs"] += 1
            for row in models.execute("SELECT campaign_id,limit_jobs,used,reserved FROM quota"):
                _exists(lab, "SELECT 1 FROM campaigns WHERE id=?", (row[0],), "Native quota campaign is missing")
                _require(0 <= row[2] + row[3] <= row[1] and min(row[2:]) >= 0, "Native quota is inconsistent")
                _require([row[2], row[3]] == actual.pop(row[0], [0, 0]), "Native reservation/attempt ledger differs from quota")
            _require(not actual, "Native job quota is missing")
        if library:
            raw = counts["raw_sources"]
            if library.execute("SELECT 1 FROM sqlite_master WHERE name='raw_sources'").fetchone():
                raw["retained_blobs"], raw["retained_bytes"] = library.execute("SELECT count(*),COALESCE(sum(bytes),0) FROM raw_sources").fetchone()
                raw["fetch_receipts"] = library.execute("SELECT count(*) FROM raw_fetches").fetchone()[0]
            if library.execute("SELECT 1 FROM sqlite_master WHERE name='versions'").fetchone():
                for row in library.execute("SELECT key,content_hash,parser,data FROM versions"):
                    value = _decode(row[3])
                    _require(value["id"] == row[0] and value["content_hash"] == row[1] and value["parser_version"] == row[2], "Parsed source identity differs")
                    retained = _raw_reference(library, value.get("raw_document"), value["content_hash"])
                    raw["versions_retained" if retained else "versions_not_retained"] += 1
                for row in library.execute("SELECT key,parser,receipt FROM urls"):
                    _exists(library, "SELECT 1 FROM versions WHERE key=? AND parser=?", tuple(row[:2]), "Source fetch receipt lacks its parsed version")
                    if row[2]:
                        value = _decode(row[2])
                        _require(value["id"] == row[0], "Source fetch receipt version differs")
                        _raw_reference(library, value.get("raw_document"), value["content_hash"])
        for relative, kind in inventory.items():
            if kind != "corrections":
                continue
            counts["dependency_namespaces"] += 1
            producer = stores[relative]
            consumer = stores[relative.replace("corrections.sqlite3", "marks.sqlite3")]
            for row in producer.execute("SELECT campaign_id,input_hash FROM registrations UNION SELECT campaign_id,input_hash FROM edges"):
                _exists(lab, "SELECT 1 FROM artifact_links WHERE campaign_id=? AND digest=?", tuple(row), "Dependency input reference is missing")
            if producer.execute("SELECT 1 FROM sqlite_master WHERE name='scope_registrations'").fetchone():
                for row in producer.execute("SELECT campaign_id,scope_id,input_hash FROM scope_registrations"):
                    _exists(lab, "SELECT 1 FROM artifact_links WHERE campaign_id=? AND digest=?", (row[0], row[2]), "Branch input registration is missing its frozen artifact")
                    _exists(lab, "SELECT 1 FROM versions v,json_each(v.payload,'$.branch_set.branches') b WHERE v.campaign_id=? "
                            "AND json_extract(b.value,'$.id')=? AND json_extract(b.value,'$.input_artifact')=?",
                            tuple(row), "Branch dependency registration lacks a real historical branch")
            for row in producer.execute("SELECT event_id FROM outbox UNION SELECT event_id FROM intents WHERE event_id IS NOT NULL"):
                _exists(producer, "SELECT 1 FROM corrections WHERE event_id=?", (row[0],), "Dependency event or durable intent is incomplete")
            for row in producer.execute("SELECT data FROM intents"):
                value = _decode(row[0])
                _exists(lab, "SELECT 1 FROM campaigns WHERE id=?", (value["campaign_id"],), "Correction intent campaign is missing")
            for row in producer.execute("SELECT data FROM corrections"):
                value = _decode(row[0])
                action = value["source_action"]
                if action["store"] == "lab":
                    for revision in (action["old_revision"], action["new_revision"]):
                        _exists(lab, "SELECT 1 FROM versions WHERE campaign_id=? AND revision=?", (action["campaign_id"], revision), "Correction origin Lab revision is missing")
                elif action["store"] == "ideas":
                    for name in ("old", "new"):
                        origin = value[name]
                        _exists(ideas, "SELECT 1 FROM versions WHERE session_id=? AND revision=?", (origin["session_id"], origin["revision"]), "Correction origin Idea revision is missing")
                else:
                    raise BackupError("integrity_error", "Unrecognized correction origin store")
            for row in consumer.execute("SELECT event_id,campaign_id,input_hash FROM marks"):
                _exists(producer, "SELECT 1 FROM corrections WHERE event_id=?", (row[0],), "Dependency receipt event is missing")
                _exists(lab, "SELECT 1 FROM artifact_links WHERE campaign_id=? AND digest=?", tuple(row[1:]), "Dependency receipt input is missing")
        cache = stores.get("evidence-cache/evidence-cache.sqlite3")
        if cache:
            for row in cache.execute("SELECT key,descriptor,accepted FROM entries"):
                _require(_sha(_json(_decode(row[1]))) == row[0], "Literature reuse descriptor is corrupt")
                counts["cache_entries"] += 1
                if row[2]:
                    accepted = _decode(row[2]); origin = accepted["origin"]
                    _require(_sha(_json(accepted["output"])) == accepted["artifact_hash"], "Literature reuse output is corrupt")
                    _exists(ideas, "SELECT 1 FROM stage_attempts WHERE id=? AND session_id=? AND generation_id=?", (origin["attempt_id"], origin["session_id"], origin["generation_id"]), "Literature reuse accepted origin is missing")
                    attempt = _decode(ideas.execute("SELECT data FROM stage_attempts WHERE id=?", (origin["attempt_id"],)).fetchone()[0])
                    _require(attempt["status"] == "accepted" and attempt["output_hash"] == accepted["artifact_hash"] and attempt["input_ref"]["packet_digest"] == origin["packet_digest"], "Literature reuse origin does not match its accepted receipt")
            for row in cache.execute("SELECT session_id,generation_id FROM requests"):
                _exists(ideas, "SELECT 1 FROM generations WHERE session_id=? AND id=?", tuple(row), "Literature reuse request origin is missing")
    return counts


def _verify_data(root, inventory):
    try:
        reports = {relative: _database_report(root / relative, kind) for relative, kind in sorted(inventory.items())}
        _require(sum(r["bytes"] for r in reports.values()) <= MAX_BYTES, "Complete backup exceeds its byte bound", "too_large")
        return reports, _crosscheck(root, inventory)
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError("integrity_error", "Research schema or durable reference validation failed") from exc


def _new_target(target, excluded_root=None):
    target = _safe(target)
    _require(not target.exists(), "Publication requires a new, nonexistent destination", "destination_exists")
    _require(target.parent.is_dir(), "Destination parent must already exist", "not_found")
    if excluded_root:
        _require(not target.is_relative_to(excluded_root) and not excluded_root.is_relative_to(target), "Backup and restore destinations must be isolated from their sources", "unsafe_path")
    return target


def _publish(stage, target):
    # Linux renameat2 provides atomic no-replace directory publication. A plain
    # rename can replace a concurrently created empty directory on POSIX.
    rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    _require(rename is not None, "Atomic no-replace publication requires Linux renameat2", "unsupported_platform")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(stage), -100, os.fsencode(target), 1):
        code = ctypes.get_errno()
        if code in (errno.EEXIST, errno.ENOTEMPTY):
            raise BackupError("destination_exists", "Destination appeared before publication; it was not overwritten")
        raise BackupError("publication_failed", "Atomic backup publication failed")
    descriptor = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sync_tree(root):
    for path in root.rglob("*"):
        if path.is_file():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
    directories = [root, *(p for p in root.rglob("*") if p.is_dir())]
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _retention(checks):
    return "per_fetch;see_checks.raw_sources" if checks["raw_sources"]["retained_blobs"] else "not_retained"


@_public_errors
def backup_research(source_root, bundle_dir):
    """Copy all recognized research stores while holding the offline fence."""
    source = _safe(source_root)
    target = _new_target(bundle_dir, source)
    with _guard(source, service=False), tempfile.TemporaryDirectory(prefix=".research-backup-", dir=target.parent) as temporary:
        stage = Path(temporary)
        inventory = _inventory(source)
        data = stage / "databases"
        data.mkdir(mode=0o700)
        for relative in inventory:
            _backup_database(source / relative, data / relative)
        _require(_inventory(source) == inventory, "Research inventory changed during maintenance")
        reports, checks = _verify_data(data, inventory)
        manifest = {"format": FORMAT, "created_at": datetime.now(timezone.utc).isoformat(),
                    "backup_tool": {"loaded_source_sha256": LOADED_SOURCE_SHA256,
                                    "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version},
                    "consistency": "cooperative_service_offline_flock;sqlite_backup_api",
                    "databases": reports, "checks": checks,
                    "absent_stores": sorted(set(FIXED) - set(inventory)),
                    "scope": {"records_events_artifacts": "all_rows_in_recognized_research_stores",
                              "raw_document_bytes": _retention(checks), "retained_sources": "parsed_text_exact_frozen_packets_and_explicitly_retained_raw_fetches",
                              "credentials_configuration": "excluded;not_read", "provider_resubmission": "never",
                              "authentication": "checksums_only_not_a_signature",
                              "limitations": ["Requires every writer to honor the maintenance lock on a local filesystem.",
                                              "Source versions without a retained fetch reference remain not_retained; later downloads cannot reconstruct an old original."]}}
        manifest["manifest_sha256"] = _sha(_json(manifest))
        (stage / "manifest.json").write_bytes(_json(manifest))
        (stage / "manifest.json").chmod(0o600)
        verify_research_backup(stage)
        _sync_tree(stage)
        _publish(stage, target)
    return manifest


@_public_errors
def verify_research_backup(bundle_dir):
    root = _safe(bundle_dir)
    _require(root.is_dir(), "Backup directory does not exist", "not_found")
    _require({p.name for p in root.iterdir()} == {"manifest.json", "databases"}, "Backup directory has missing or unexpected entries")
    path = _safe(root / "manifest.json")
    _require(path.is_file() and path.stat().st_size <= 2 * 1024**2, "Manifest is missing or oversized")
    try:
        manifest = _decode(path.read_bytes())
        digest = manifest.pop("manifest_sha256")
        _require(_sha(_json(manifest)) == digest and manifest["format"] == FORMAT, "Backup manifest checksum or version differs")
        inventory = _inventory(_safe(root / "databases"), bundle=True)
        _require(set(inventory) == set(manifest["databases"]), "Backup database inventory differs")
        reports, checks = _verify_data(root / "databases", inventory)
        _require(reports == manifest["databases"] and checks == manifest["checks"]
                 and manifest["scope"]["raw_document_bytes"] == _retention(checks), "Backup content differs from the manifest")
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError("integrity_error", "Backup manifest could not be validated") from exc
    return {"verified": True, "manifest_sha256": digest, "databases": len(inventory), "checks": checks,
            "raw_document_bytes": _retention(checks), "provider_calls": 0, "store_recovery_executed": False}


@_public_errors
def restore_research(bundle_dir, destination):
    """Restore to a new isolated root; preserve raw states without recovery."""
    source = _safe(bundle_dir)
    target = _new_target(destination, source)
    report = verify_research_backup(source)
    with tempfile.TemporaryDirectory(prefix=".research-restore-", dir=target.parent) as temporary:
        stage = Path(temporary)
        shutil.copytree(source / "databases", stage, dirs_exist_ok=True)
        inventory = _inventory(stage, bundle=True)
        reports, checks = _verify_data(stage, inventory)
        manifest = _decode((source / "manifest.json").read_bytes())
        _require(reports == manifest["databases"] and checks == manifest["checks"], "Backup changed during restore")
        # A new, empty lock belongs only to this restored root. No running-state
        # records, model reservations, cache leases or unknown usage are reset.
        (stage / LOCK).touch(mode=0o600)
        for path in stage.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)
        _sync_tree(stage)
        _publish(stage, target)
    return {**report, "restored": True, "replay_executed": False,
            "next_start": "explicit_only;existing_service_recovery_marks_owned_running_work_interrupted_without_resubmission"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    create = sub.add_parser("backup")
    create.add_argument("source_root"); create.add_argument("bundle_dir")
    verify = sub.add_parser("verify"); verify.add_argument("bundle_dir")
    restore = sub.add_parser("restore"); restore.add_argument("bundle_dir"); restore.add_argument("new_root")
    args = parser.parse_args(argv)
    try:
        if args.operation == "backup":
            result = backup_research(args.source_root, args.bundle_dir)
        elif args.operation == "verify":
            result = verify_research_backup(args.bundle_dir)
        else:
            result = restore_research(args.bundle_dir, args.new_root)
        print(_json(result).decode())
        return 0
    except BackupError as exc:
        print(_json({"error": exc.code, "message": exc.message}).decode(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
