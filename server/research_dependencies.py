"""Explicit source corrections and recoverable local campaign invalidation.

This is a single native-administrator namespace, not multi-user authorization.
Only service code may register corrections, using persisted record identities.
No URL, renderer-supplied hash, model, or new source version implicitly declares
a correction. Original Idea/Lab databases are read-only to this module.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from research_lab import LabError, ResearchLabStore

SCHEMA = 1
MAX_JSON = 4 * 1024 * 1024
MAX_EXPORT = 1024 * 1024
MAX_HISTORY_INPUTS = 16
MAX_SOURCE_ROW_BYTES = 64 * 1024 * 1024  # Read old large snapshots through bounded SQL projections.
HEX = re.compile(r"[0-9a-f]{64}\Z")


def _json(value):
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(text.encode()) > MAX_JSON:
            raise LabError("dependency_limit", "Dependency record exceeds its byte bound")
        return text
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise LabError("invalid_request", "Dependency record must be finite bounded JSON") from None


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _text(value, size=128):
    try:
        valid = isinstance(value, str) and bool(value) and len(value.encode()) <= size
    except UnicodeError:
        valid = False
    if not valid:
        raise LabError("invalid_request", "A bounded nonempty dependency identifier is required")
    return value


def _revision(value):
    if type(value) is not int or not 1 <= value <= 2147483647:
        raise LabError("invalid_request", "A positive persisted revision is required")
    return value


def _check(condition, message="Dependency source records failed verification"):
    if not condition:
        raise LabError("integrity_error", message)


def _path(value):
    value = Path(value).absolute()
    if any(p.is_symlink() for p in (value, *value.parents)):
        raise LabError("storage_error", "Dependency paths cannot contain symlinks")
    return value


def _decode(value):
    if not isinstance(value, (str, bytes)) or len(value if isinstance(value, bytes) else value.encode()) > MAX_JSON:
        raise LabError("dependency_limit", "Persisted dependency input exceeds its read bound")
    try:
        result = json.loads(value)
        _json(result)
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise LabError("integrity_error", "Dependency input is invalid JSON") from None


def _projected_row(db, table, column, where, args, projection):
    # SQL computes only the required service fields before Python materializes
    # them. Old legal snapshots can contain large repeated plans/observations;
    # their existence must not disable dependency checks or require wire export.
    row = db.execute(f"SELECT CASE WHEN length(CAST({column} AS BLOB))<=? THEN {projection} ELSE NULL END FROM {table} WHERE {where}",
                     (MAX_SOURCE_ROW_BYTES, *args)).fetchone()
    if row is None:
        return None
    if row[0] is None:
        raise LabError("dependency_limit", "Historical source row exceeds the explicit service projection limit")
    return _decode(row[0])


@contextmanager
def _read(path):
    path = _path(path)
    if not path.is_file():
        raise LabError("not_found", "Dependency source store was not found")
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error as exc:
        raise LabError("storage_error", "Dependency source could not be opened read-only") from exc
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        yield connection
    except sqlite3.Error as exc:
        raise LabError("storage_error", "Dependency source could not be inspected without mutation") from exc
    finally:
        connection.close()


class ResearchDependencies:
    def __init__(self, root, idea_root, lab_root, namespace="native-local"):
        if not isinstance(namespace, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", namespace) is None:
            raise LabError("invalid_request", "Invalid internal dependency namespace")
        self.namespace = namespace
        self.root = _path(root) / hashlib.sha256(namespace.encode()).hexdigest()
        self.idea_path = _path(idea_root) / "ideas.sqlite3"
        self.lab_path = _path(lab_root) / "lab.sqlite3"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.producer = self.root / "corrections.sqlite3"
        self.consumer = self.root / "marks.sqlite3"
        if self.producer.exists() != self.consumer.exists():
            raise LabError("storage_error", "One dependency database is missing; restore the matching pair instead of recreating empty state")
        for path, statements in ((self.producer, (
                "CREATE TABLE registrations(campaign_id TEXT PRIMARY KEY,input_hash TEXT NOT NULL,revision INTEGER NOT NULL)",
                "CREATE TABLE edges(campaign_id TEXT NOT NULL,input_hash TEXT NOT NULL,source_key TEXT NOT NULL,evidence_key TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(campaign_id,input_hash,source_key,evidence_key))",
                "CREATE INDEX edges_source ON edges(source_key,campaign_id,input_hash)",
                "CREATE INDEX edges_evidence ON edges(evidence_key,campaign_id,input_hash)",
                "CREATE TABLE corrections(event_id TEXT PRIMARY KEY,request_key TEXT UNIQUE NOT NULL,request_hash TEXT NOT NULL,data TEXT NOT NULL)",
                "CREATE INDEX corrections_source ON corrections(json_extract(data,'$.old.source_key'))",
                "CREATE INDEX corrections_evidence ON corrections(json_extract(data,'$.old.evidence_key'))",
                "CREATE INDEX corrections_campaign ON corrections(json_extract(data,'$.campaign_id'))",
                "CREATE TABLE intents(intent_id TEXT PRIMARY KEY,request_hash TEXT NOT NULL,data TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN('pending','completed','rejected')),event_id TEXT,checked_at TEXT NOT NULL DEFAULT '')",
                "CREATE INDEX intents_campaign ON intents(state,json_extract(data,'$.campaign_id'))",
                "CREATE INDEX intents_source ON intents(state,json_extract(data,'$.source_key'))",
                "CREATE INDEX intents_pending ON intents(state,checked_at)",
                "CREATE TABLE outbox(event_id TEXT PRIMARY KEY REFERENCES corrections(event_id),cursor TEXT NOT NULL,complete INTEGER NOT NULL CHECK(complete IN(0,1)))")),
                (self.consumer, ("CREATE TABLE marks(event_id TEXT NOT NULL,campaign_id TEXT NOT NULL,input_hash TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(event_id,campaign_id,input_hash))",))):
            with self._db(path, write=True, initialize=True) as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    _check(not db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone(), "Unversioned dependency database is not empty")
                    for statement in statements:
                        db.execute(statement)
                    db.execute("PRAGMA user_version=1")
                elif version != SCHEMA:
                    raise LabError("unsupported_schema", "Unsupported dependency schema")
                if path == self.producer:
                    db.execute("CREATE TABLE IF NOT EXISTS scope_registrations(campaign_id TEXT NOT NULL,scope_id TEXT NOT NULL,input_hash TEXT NOT NULL,PRIMARY KEY(campaign_id,scope_id,input_hash))")
            path.chmod(0o600)

    @contextmanager
    def _db(self, path=None, *, write=False, initialize=False):
        path = _path(path or self.producer)
        try:
            db = sqlite3.connect(path if initialize else path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                                 uri=not initialize, timeout=10)
        except sqlite3.Error as exc:
            raise LabError("storage_error", "Dependency state could not be opened; no empty replacement was created") from exc
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if not write:
                db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if not initialize and db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA:
                raise LabError("unsupported_schema", "Unsupported dependency schema")
            yield db
            db.commit()
        except sqlite3.Error as exc:
            raise LabError("storage_error", "Dependency state was not committed; retry the same identity") from exc
        finally:
            db.close()

    def _campaign(self, campaign_id, revision=None):
        _text(campaign_id)
        projection = """json_object('id',json_extract(payload,'$.id'),
            'revision',json_extract(payload,'$.revision'),'input_artifact',json_extract(payload,'$.input_artifact'),
            'origin',json_extract(payload,'$.origin'),
            'claims',json((SELECT json_group_array(json_object('id',json_extract(value,'$.id'),
                 'input_artifact',json_extract(value,'$.input_artifact'))) FROM json_each(payload,'$.claims'))),
            'events',json_array(json_extract(payload,'$.events[#-1]')))"""
        with _read(self.lab_path) as db:
            if revision is None:
                item = _projected_row(db, "campaigns", "payload", "id=?", (campaign_id,), projection)
            else:
                _revision(revision)
                item = _projected_row(db, "versions", "payload", "campaign_id=? AND revision=?", (campaign_id, revision), projection)
            if item is None:
                raise LabError("not_found", "Persisted campaign revision was not found")
            _check(item["id"] == campaign_id)
            return item

    @staticmethod
    def _idea_version(db, session_id, revision):
        return _projected_row(db, "versions", "data", "session_id=? AND revision=?", (session_id, revision),
            """json_object('generation_id',json_extract(data,'$.generation_id'),
                 'result',json_object('literature',json_extract(data,'$.result.literature'),
                                      'ideas',json_extract(data,'$.result.ideas')),
                 'decision',json_extract(data,'$.decision'),
                 'brief',json_object('sources',json_extract(data,'$.brief.sources')),
                 'research',json_object('sources',json_extract(data,'$.research.sources')))""")

    def _artifact(self, campaign_id, digest):
        _check(isinstance(digest, str) and HEX.fullmatch(digest))
        with _read(self.lab_path) as db:
            row = db.execute("SELECT a.data FROM artifacts a JOIN artifact_links l ON a.digest=l.digest WHERE l.campaign_id=? AND a.digest=?", (campaign_id, digest)).fetchone()
            if row is None:
                raise LabError("not_found", "Dependency artifact does not belong to this campaign")
            raw = bytes(row[0])
            _check(hashlib.sha256(raw).hexdigest() == digest, "Dependency artifact hash changed")
            return _decode(raw)

    def _idea_evidence(self, session_id, revision, evidence_id):
        _text(session_id); _revision(revision); _text(evidence_id)
        with _read(self.idea_path) as db:
            item = self._idea_version(db, session_id, revision)
            if item is None:
                raise LabError("not_found", "Persisted Idea evidence revision was not found")
            evidence = ((item.get("result") or {}).get("literature") or {}).get("evidence", [])
            matches = [entry for entry in evidence if entry.get("id") == evidence_id]
            _check(len(matches) == 1, "The named Idea EvidenceCard is unavailable or ambiguous")
            entry = matches[0]
            _check(entry.get("source_support") == "exact_quote_verified", "Idea evidence was not verified against its source")
            digest = entry.get("source_hash")
            _check(isinstance(digest, str) and HEX.fullmatch(digest), "Unversioned Idea evidence cannot authenticate a dependency")
            candidates = list(item.get("brief", {}).get("sources") or []) + list(item.get("research", {}).get("sources") or [])
            rows = db.execute("SELECT sources FROM stage_packets WHERE generation_id=? ORDER BY round LIMIT 10", (item["generation_id"],)).fetchall()
            for packet in rows:
                candidates.extend(_decode(packet[0]))
            source = next((s for s in candidates if s.get("id") == entry["source_id"] and
                           hashlib.sha256(s["text"].encode()).hexdigest() == digest), None)
            _check(source is not None, "Exact frozen Idea source packet is missing")
            span = entry.get("span") or {}
            _check(type(span.get("start")) is int and type(span.get("end")) is int and
                   0 <= span["start"] < span["end"] <= len(source["text"]) and
                   source["text"][span["start"]:span["end"]] == entry["quote"], "Evidence span does not match the frozen source")
            result = {"session_id": session_id, "revision": revision, "generation_id": item["generation_id"],
                      "evidence_id": evidence_id, "evidence_hash": _hash(entry), "source_id": source["id"], "source_hash": digest}
            result["source_key"] = _hash({"namespace": self.namespace, "session_id": session_id, "source_id": source["id"], "source_hash": digest})
            result["evidence_key"] = _hash({"namespace": self.namespace, **{k: result[k] for k in
                                                    ("session_id", "generation_id", "evidence_id", "evidence_hash", "source_hash")}})
            return result

    def _trusted_source(self, item, source):
        provenance = source.get("provenance") or {}
        origin = item.get("origin") or {}
        if provenance.get("kind") != "idea_frozen_packet" or origin.get("kind") != "idea":
            return None
        # Pre-revision imports remain readable and locally executable. Missing
        # authenticated identity cannot be reconstructed from a URL or current
        # Idea choice, and must never acquire shared correction authority.
        if (type(provenance.get("decision_revision")) is not int or provenance["decision_revision"] < 1 or
                type(origin.get("decision_revision")) is not int or origin["decision_revision"] < 1):
            return None
        _check(provenance.get("session_id") == origin.get("session_id"), "Idea dependency belongs to another origin")
        _check(provenance.get("decision_revision") == origin.get("decision_revision") and
               provenance.get("generation_id") == origin.get("generation_id"), "Idea dependency belongs to another decision")
        with _read(self.idea_path) as db:
            decision_item = self._idea_version(db, origin["session_id"], origin["decision_revision"])
            _check(decision_item is not None, "Imported Idea decision was not found")
        decision = decision_item.get("decision") or {}
        selected = ([decision.get("selected_id")] if decision.get("kind") == "select" else
                    decision.get("selected_ids", []) if decision.get("kind") == "combine" else [])
        _check(bool(selected) and selected == origin.get("selected_ids") and
               decision.get("generation_id") == origin["generation_id"], "Imported Idea decision is not authoritative")
        relevant_ids = {identity for direction in decision_item["result"]["ideas"]["directions"]
                        if direction["id"] in selected for identity in direction["evidence_ids"]}
        _check(provenance.get("evidence_id") in relevant_ids, "Imported evidence did not belong to the chosen direction")
        reference = self._idea_evidence(provenance["session_id"], provenance["decision_revision"], provenance["evidence_id"])
        _check(reference["source_id"] == provenance.get("source_id") and reference["source_hash"] == provenance.get("source_hash")
               and reference["generation_id"] == provenance.get("generation_id")
               and reference["source_hash"] == hashlib.sha256(source["text"].encode()).hexdigest(), "Imported Idea lineage changed")
        return reference

    def _source_references(self, item, source):
        reference = self._trusted_source(item, source)
        if reference is None:
            return []
        # A source packet can support several selected EvidenceCards. The import
        # UI stores one representative; corrections to the others must propagate.
        with _read(self.idea_path) as db:
            decision_item = self._idea_version(db, reference["session_id"], reference["revision"])
        selected = set(item["origin"]["selected_ids"])
        relevant = {identity for direction in decision_item["result"]["ideas"]["directions"]
                    if direction["id"] in selected for identity in direction["evidence_ids"]}
        identities = [e["id"] for e in decision_item["result"]["literature"]["evidence"]
                      if e["id"] in relevant and e["source_id"] == reference["source_id"] and e.get("source_hash") == reference["source_hash"]]
        if len(identities) > 12:
            raise LabError("dependency_limit", "Too many EvidenceCards for one dependency packet")
        return [self._idea_evidence(reference["session_id"], reference["revision"], identity) for identity in identities]

    def _register(self, db, item):
        # Reconcile pre-index edits too: a metadata-only correction could have
        # removed its current provenance before this bridge was first installed.
        # Original scoped immutable inputs retain the genuine lineage.
        with _read(self.lab_path) as lab:
            historical = lab.execute("SELECT DISTINCT json_extract(payload,'$.input_artifact') FROM versions WHERE campaign_id=? LIMIT ?",
                                     (item["id"], MAX_HISTORY_INPUTS + 1)).fetchall()
        inputs = list(dict.fromkeys([item["input_artifact"], *[c["input_artifact"] for c in item.get("claims", [])], *[r[0] for r in historical]]))
        if len(inputs) > MAX_HISTORY_INPUTS:
            raise LabError("dependency_limit", "Too many historical inputs for one dependency reconciliation")
        added = 0
        for digest in inputs:
            artifact = self._artifact(item["id"], digest)
            for source in artifact.get("sources", []):
                for reference in self._source_references(item, source):
                    edge = {"namespace": self.namespace, "campaign_id": item["id"], "input_hash": digest, **reference}
                    added += db.execute("INSERT OR IGNORE INTO edges VALUES (?,?,?,?,?)", (item["id"], digest, reference["source_key"], reference["evidence_key"], _json(edge))).rowcount
        # A metadata-only edit or renamed source must not bypass an already known
        # corrected packet. Changed text remains user-corrected, not authenticated.
        current = self._artifact(item["id"], item["input_artifact"])
        current_hashes = {hashlib.sha256(s["text"].encode()).hexdigest() for s in current.get("sources", [])}
        existing = db.execute("SELECT data FROM edges WHERE campaign_id=? LIMIT 101", (item["id"],)).fetchall()
        if len(existing) > 100:
            raise LabError("dependency_limit", "Campaign dependency traversal exceeds its bound")
        for row in existing:
            edge = _decode(row[0])
            if edge["source_hash"] in current_hashes:
                edge["input_hash"] = item["input_artifact"]
                added += db.execute("INSERT OR IGNORE INTO edges VALUES (?,?,?,?,?)", (item["id"], item["input_artifact"], edge["source_key"], edge["evidence_key"], _json(edge))).rowcount
        db.execute("INSERT INTO registrations VALUES (?,?,?) ON CONFLICT(campaign_id) DO UPDATE SET input_hash=excluded.input_hash,revision=excluded.revision", (item["id"], item["input_artifact"], item["revision"]))
        if added:
            # A campaign registered after fan-out still receives old corrections.
            # Re-scanning is safe because consumer receipts have stable keys.
            db.execute("UPDATE outbox SET cursor='',complete=0")
        return {"campaign_id": item["id"], "input_hash": item["input_artifact"], "new_edges": added}

    def register_campaign(self, campaign_id):
        with self._db(write=True) as db:
            return self._register(db, self._campaign(campaign_id))

    def _record(self, request, build):
        key = _text(request["idempotency_key"])
        fingerprint = _hash(request)
        with self._db(write=True) as db:
            prior = db.execute("SELECT request_hash,data FROM corrections WHERE request_key=?", (key,)).fetchone()
            if prior:
                if prior[0] != fingerprint:
                    raise LabError("idempotency_conflict", "Correction identity belongs to a different declaration")
                return _decode(prior[1])
            data = build()
            event = {"id": _hash({"namespace": self.namespace, "correction_key": key}), "namespace": self.namespace,
                     "schema_version": 1, "at": datetime.now(timezone.utc).isoformat(), "actor": "native_service",
                     "reason": _text(request["reason"], 8000), "scientific_validity": "not_assessed",
                     "effect": "review_dependencies_only; no execution authority or new budget", **data}
            db.execute("INSERT INTO corrections VALUES (?,?,?,?)", (event["id"], key, fingerprint, _json(event)))
            db.execute("INSERT INTO outbox VALUES (?,'',0)", (event["id"],))
            return event

    def record_idea_correction(self, session_id, old_revision, old_evidence_id, new_revision, new_evidence_id, *, idempotency_key, reason):
        request = dict(kind="idea_correction", session_id=session_id, old_revision=old_revision, old_evidence_id=old_evidence_id,
                       new_revision=new_revision, new_evidence_id=new_evidence_id, idempotency_key=idempotency_key, reason=reason)
        def build():
            _check(_revision(new_revision) > _revision(old_revision), "A correction must identify a later persisted Idea revision")
            old = self._idea_evidence(session_id, old_revision, old_evidence_id)
            new = self._idea_evidence(session_id, new_revision, new_evidence_id)
            _check(old["evidence_hash"] != new["evidence_hash"] or old["source_hash"] != new["source_hash"], "The declared evidence did not change")
            return {"kind": "idea_correction", "scope": "source_version" if old["source_hash"] != new["source_hash"] else "evidence_record",
                    "old": old, "new": new, "source_action": {"store": "ideas", "old_revision": old_revision, "new_revision": new_revision}}
        return self._record(request, build)

    def record_lab_source_correction(self, campaign_id, old_revision, new_revision, source_id, *, idempotency_key, reason):
        request = dict(kind="lab_source_correction", campaign_id=campaign_id, old_revision=old_revision,
                       new_revision=new_revision, source_id=source_id, idempotency_key=idempotency_key, reason=reason)
        def build():
            _check(_revision(new_revision) == _revision(old_revision) + 1, "Source correction must reference adjacent committed revisions")
            before, after = self._campaign(campaign_id, old_revision), self._campaign(campaign_id, new_revision)
            event = after["events"][-1]
            _check(event["type"] == "inputs_corrected" and event["data"]["old_input_artifact"] == before["input_artifact"] and
                   event["data"]["new_input_artifact"] == after["input_artifact"], "No matching committed source correction action exists")
            old_sources = self._artifact(campaign_id, before["input_artifact"]).get("sources", [])
            new_sources = self._artifact(campaign_id, after["input_artifact"]).get("sources", [])
            old = next((s for s in old_sources if s["id"] == source_id), None)
            new = next((s for s in new_sources if s["id"] == source_id), None)
            _check(old is not None and new is not None, "Correction source identity is unavailable")
            trusted = self._trusted_source(before, old)
            lineage = (new.get("provenance") or {}).get("derived_from", {})
            new_hash = hashlib.sha256(new["text"].encode()).hexdigest()
            old_hash = hashlib.sha256(old["text"].encode()).hexdigest()
            cross = trusted is not None and old_hash != new_hash
            if cross:
                _check(new["provenance"].get("kind") == "user_corrected" and lineage.get("input_artifact") == before["input_artifact"] and
                       lineage.get("source_id") == source_id and lineage.get("source_hash") == old_hash,
                       "Changed input lacks a committed correction lineage")
            return {"kind": "lab_source_correction", "scope": "source_version" if cross else "local_input",
                    "campaign_id": campaign_id, "input_hash": before["input_artifact"],
                    "old": trusted or {"source_hash": old_hash, "source_id": source_id},
                    "new": {"source_hash": new_hash, "source_id": source_id, "input_hash": after["input_artifact"], "verification": "user_corrected_not_source_verified"},
                    "source_action": {"store": "lab", "campaign_id": campaign_id, "old_revision": old_revision,
                                      "new_revision": new_revision, "event_id": event["id"]}}
        return self._record(request, build)

    def prepare_lab_correction(self, campaign_id, request, source_id):
        """Persist a service intent before calling core.correct_inputs.

        The request is not executed or stored here; only its fingerprint and
        immutable old-record references are retained. An unfinished intent is
        conservative admission state, not evidence that the source is false.
        """
        if not isinstance(request, dict) or set(request) != {"expected_revision", "idempotency_key", "inputs", "reason"}:
            raise LabError("invalid_request", "Use the exact core source-correction request")
        _text(campaign_id); _text(source_id); _revision(request["expected_revision"])
        key, reason = _text(request["idempotency_key"]), _text(request["reason"], 8000)
        fingerprint = _hash({"operation": "inputs_corrected", **request})
        identity = _hash({"namespace": self.namespace, "campaign_id": campaign_id, "correction_key": key})
        with self._db(write=True) as db:
            prior = db.execute("SELECT request_hash,data,state FROM intents WHERE intent_id=?", (identity,)).fetchone()
            if prior:
                data = _decode(prior[1])
                if prior[0] != fingerprint or data["source_id"] != source_id:
                    raise LabError("idempotency_conflict", "This correction identity belongs to another source request")
                if prior[2] == "rejected":
                    raise LabError("invalid_request", "This correction request was rejected; use a new identity after fixing its input")
                return data
            with _read(self.lab_path) as lab:
                existing = lab.execute("SELECT request_hash FROM mutations WHERE scope=? AND key=?", (campaign_id, key)).fetchone()
            if existing:
                raise LabError("idempotency_conflict", "The request key already identifies a core action without this correction intent; use the explicit historical declaration API")
            item = self._campaign(campaign_id)
            if item["revision"] != request["expected_revision"]:
                raise LabError("revision_conflict", "Reload the campaign before declaring a source correction")
            self._register(db, item)
            sources = self._artifact(campaign_id, item["input_artifact"]).get("sources", [])
            source = next((s for s in sources if s["id"] == source_id), None)
            if source is None:
                raise LabError("invalid_request", "The named source is not in the current input")
            # The declared source must itself be present in the submitted input.
            # Core remains responsible for all content validation and permissions.
            replacements = (request["inputs"].get("sources", []) if isinstance(request["inputs"], dict) else [])
            if not isinstance(replacements, list) or not any(isinstance(s, dict) and s.get("id") == source_id for s in replacements):
                raise LabError("invalid_request", "A shared source correction must retain its source identity")
            old_by_id = {s["id"]: s for s in sources}
            if (not all(isinstance(s, dict) and isinstance(s.get("id"), str) for s in replacements) or
                    len(replacements) != len(old_by_id) or {s["id"] for s in replacements} != set(old_by_id) or
                    [s["id"] for s in replacements if s.get("text") != old_by_id[s["id"]]["text"]] != [source_id]):
                raise LabError("invalid_request", "A shared correction must change exactly the named source text and preserve the source set")
            trusted = self._trusted_source(item, source)
            data = {"id": identity, "namespace": self.namespace, "campaign_id": campaign_id,
                    "old_revision": item["revision"], "input_hash": item["input_artifact"], "source_id": source_id,
                    "request_key": key, "reason": reason, "source_key": trusted["source_key"] if trusted else None,
                    "at": datetime.now(timezone.utc).isoformat(), "scientific_validity": "not_assessed"}
            db.execute("INSERT INTO intents(intent_id,request_hash,data,state,event_id) VALUES (?,?,?,'pending',NULL)", (identity, fingerprint, _json(data)))
            return data

    def _recover_intent(self, identity, *, reject_uncommitted=False):
        with self._db() as db:
            row = db.execute("SELECT request_hash,data,state,event_id FROM intents WHERE intent_id=?", (identity,)).fetchone()
            if row is None:
                raise LabError("not_found", "Correction intent was not found")
            data = _decode(row[1])
            if row[2] != "pending":
                return {"id": identity, "state": row[2], "event_id": row[3]}
        with _read(self.lab_path) as lab:
            receipt = lab.execute("SELECT request_hash,response FROM mutations WHERE scope=? AND key=?",
                                  (data["campaign_id"], data["request_key"])).fetchone()
        if receipt is None:
            if reject_uncommitted:
                # Only the synchronous apply wrapper may do this, after its core
                # call has exited with a known validation failure. Recovery alone
                # cannot infer that a request never ran and must retain pending.
                with self._db(write=True) as db:
                    db.execute("UPDATE intents SET state='rejected' WHERE intent_id=? AND state='pending'", (identity,))
                return {"id": identity, "state": "rejected", "event_id": None}
            return {"id": identity, "state": "pending", "event_id": None}
        _check(receipt[0] == row[0], "A correction request key has a conflicting core receipt")
        committed = _decode(receipt[1])
        event = self.record_lab_source_correction(data["campaign_id"], data["old_revision"], committed["revision"], data["source_id"],
            idempotency_key="intent-" + identity, reason=data["reason"])
        with self._db(write=True) as db:
            db.execute("UPDATE intents SET state='completed',event_id=? WHERE intent_id=?", (event["id"], identity))
        return {"id": identity, "state": "completed", "event_id": event["id"]}

    def apply_lab_source_correction(self, campaign_id, request, source_id, *, correct_inputs):
        """Synchronous service bridge. A retry uses the original core identity.

        correct_inputs must be the trusted bounded core method, not external or
        asynchronous execution. No user callback is accepted at the transport.
        """
        intent = self.prepare_lab_correction(campaign_id, request, source_id)
        try:
            result = correct_inputs(campaign_id, request)
        except LabError as exc:
            # Storage/unknown failures may be after commit. They remain pending
            # unless the actual core receipt can prove what happened.
            known_rejection = exc.code in {"invalid_request", "revision_conflict", "idempotency_conflict", "invalid_state", "unsupported_adapter"}
            self._recover_intent(intent["id"], reject_uncommitted=known_rejection)
            raise
        final = self._recover_intent(intent["id"])
        if final["state"] != "completed":
            raise LabError("storage_error", "No committed correction receipt is available; retain and retry the same request")
        return result

    def recover_lab_corrections(self, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise LabError("invalid_request", "Correction recovery limit must be between 1 and 100")
        with self._db() as db:
            pending = db.execute("SELECT intent_id FROM intents WHERE state='pending' ORDER BY checked_at,rowid LIMIT ?", (limit,)).fetchall()
        items = []
        for row in pending:
            try:
                items.append(self._recover_intent(row[0]))
            except LabError as exc:
                items.append({"id": row[0], "state": "attention_required", "error": {"code": exc.code, "message": exc.message}})
            with self._db(write=True) as db:
                db.execute("UPDATE intents SET checked_at=? WHERE intent_id=?", (datetime.now(timezone.utc).isoformat(), row[0]))
        with self._db() as db:
            remaining = db.execute("SELECT count(*) FROM intents WHERE state='pending'").fetchone()[0]
        return {"items": items, "pending_intents": remaining, "has_more": remaining > 0,
                "execution_performed": False}

    def replay_lab_mutation(self, campaign_id, event, request):
        """Return a saved core mutation or None without admission or new work."""
        shapes = {"action_executed": ({"expected_revision", "idempotency_key"}, set()),
                  "next_plan_frozen": ({"expected_revision", "idempotency_key"}, set()),
                  "inputs_corrected": ({"expected_revision", "idempotency_key", "inputs", "reason"}, set()),
                  "human_decision": ({"expected_revision", "idempotency_key", "kind", "feedback"},
                                     {"selected_action_id", "goal", "hypothesis", "success_criteria", "constraints"})}
        if event not in shapes or not isinstance(request, dict):
            raise LabError("invalid_request", "Use a known core mutation and its exact request")
        required, optional = shapes[event]
        if not required <= set(request) or set(request) - required - optional:
            raise LabError("invalid_request", "Unexpected or missing mutation fields")
        _text(campaign_id); _revision(request["expected_revision"]); _text(request["idempotency_key"])
        _json(request)
        with _read(self.lab_path) as db:
            row = db.execute("SELECT json_extract(payload,'$.deleted_at') FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if row is None:
                raise LabError("not_found", "Campaign was not found")
            if row[0]:
                raise LabError("deleted", "This research is in Trash. Restore it before opening or continuing it.")
            result, _ = ResearchLabStore._replay(db, campaign_id, request["idempotency_key"], {"operation": event, **request})
            # Match the core's bounded public replay projection while preserving
            # the original immutable response in its database.
            return ResearchLabStore.project(result) if result is not None else None

    def reconcile_campaign(self, campaign_id):
        """Explicit metadata maintenance, with no calculation or model work."""
        self.register_campaign(campaign_id)
        recovery = self.recover_lab_corrections(limit=50)
        delivery = self.drain(limit=50)
        return {"campaign_id": campaign_id, "dependency_status": self.snapshot(campaign_id),
                "recovery": {key: recovery[key] for key in ("pending_intents", "has_more")},
                "delivery": delivery, "execution_performed": False}

    @staticmethod
    def _targets(db, event, cursor, limit):
        if event["scope"] == "local_input":
            targets = [(event["campaign_id"], event["input_hash"])]
            return [target for target in targets if not cursor or list(target) > cursor][:limit]
        column = "source_key" if event["scope"] == "source_version" else "evidence_key"
        args = [event["old"][column]]
        sql = f"SELECT DISTINCT campaign_id,input_hash FROM edges WHERE {column}=?"
        if cursor:
            sql += " AND (campaign_id,input_hash) > (?,?)"
            args.extend(cursor)
        return [tuple(row) for row in db.execute(sql + " ORDER BY campaign_id,input_hash LIMIT ?", [*args, limit])]

    def drain(self, limit=50, *, after_publish=None):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise LabError("invalid_request", "Dependency drain limit must be between 1 and 100")
        processed = 0
        with self._db(write=True) as producer:
            jobs = producer.execute("SELECT c.data,o.cursor FROM outbox o JOIN corrections c ON c.event_id=o.event_id WHERE complete=0 ORDER BY c.rowid LIMIT ?", (limit,)).fetchall()
            for job in jobs:
                event = _decode(job[0])
                cursor = _decode(job[1]) if job[1] else None
                targets = self._targets(producer, event, cursor, limit - processed + 1)
                selected = targets[:limit - processed]
                with self._db(self.consumer, write=True) as consumer:
                    for cid, input_hash in selected:
                        receipt = {"namespace": self.namespace, "event_id": event["id"], "campaign_id": cid,
                                   "input_hash": input_hash, "state": "needs_revalidation", "event_hash": _hash(event)}
                        prior = consumer.execute("SELECT data FROM marks WHERE event_id=? AND campaign_id=? AND input_hash=?", (event["id"], cid, input_hash)).fetchone()
                        if prior:
                            _check(_decode(prior[0]) == receipt, "Consumer receipt conflicts with its immutable correction")
                        else:
                            consumer.execute("INSERT INTO marks VALUES (?,?,?,?)", (event["id"], cid, input_hash, _json(receipt)))
                if after_publish:
                    after_publish(event["id"])  # Test/service fault boundary after consumer commit, before producer ack.
                processed += len(selected)
                complete = len(targets) == len(selected)
                producer.execute("UPDATE outbox SET cursor=?,complete=? WHERE event_id=?", (_json(list(selected[-1])) if selected else job[1], int(complete), event["id"]))
                if processed == limit:
                    break
            pending = producer.execute("SELECT count(*) FROM outbox WHERE complete=0").fetchone()[0]
        return {"processed_targets": processed, "pending_events": pending, "has_more": pending > 0}

    def _effects(self, db, item):
        edges = db.execute("SELECT input_hash,source_key,evidence_key FROM edges WHERE campaign_id=? LIMIT 101", (item["id"],)).fetchall()
        if len(edges) > 100:
            raise LabError("dependency_limit", "Campaign dependency traversal exceeds its bound")
        sources = {r["source_key"] for r in edges}
        evidence = {r["evidence_key"] for r in edges}
        # SQL limits scope before materializing events; a busy unrelated campaign
        # cannot silently hide a relevant correction behind a global row limit.
        sql = "SELECT data FROM corrections WHERE (json_extract(data,'$.scope')='local_input' AND json_extract(data,'$.campaign_id')=?)"
        args = [item["id"]]
        for column, values in (("source_key", sources), ("evidence_key", evidence)):
            if values:
                sql += f" OR json_extract(data,'$.old.{column}') IN ({','.join('?' for _ in values)})"
                args.extend(sorted(values))
        rows = db.execute(sql + " ORDER BY rowid LIMIT 1001", args).fetchall()
        if len(rows) > 1000:
            raise LabError("dependency_limit", "Campaign correction history requires a bounded maintenance action")
        result = []
        for row in rows:
            event = _decode(row[0])
            if event["scope"] == "local_input":
                affected = {event["input_hash"]} if event["campaign_id"] == item["id"] else set()
            else:
                key = "source_key" if event["scope"] == "source_version" else "evidence_key"
                affected = {edge["input_hash"] for edge in edges if edge[key] == event["old"][key]}
            if affected:
                result.append((event, affected))
        return result

    def _status(self, db, item, scope_id=None):
        registered = (db.execute("SELECT input_hash FROM scope_registrations WHERE campaign_id=? AND scope_id=? AND input_hash=?",
                     (item["id"], scope_id, item["input_artifact"])).fetchone() if scope_id else
                     db.execute("SELECT input_hash FROM registrations WHERE campaign_id=?", (item["id"],)).fetchone())
        reconciled = registered is not None and registered[0] == item["input_artifact"]
        effects = self._effects(db, item)
        affected_inputs = set().union(*(inputs for _, inputs in effects)) if effects else set()
        edges = db.execute("SELECT input_hash,source_key FROM edges WHERE campaign_id=? LIMIT 101", (item["id"],)).fetchall()
        keys = {row["source_key"] for row in edges}
        sql, args = "SELECT data FROM intents WHERE state='pending' AND (json_extract(data,'$.campaign_id')=?", [item["id"]]
        if keys:
            sql += f" OR json_extract(data,'$.source_key') IN ({','.join('?' for _ in keys)})"
            args.extend(sorted(keys))
        intents = db.execute(sql + ") ORDER BY rowid LIMIT 101", args).fetchall()
        if len(intents) > 100:
            raise LabError("dependency_limit", "Too many pending corrections for one campaign")
        pending_intents, intent_inputs = [], set()
        for row in intents:
            intent = _decode(row[0])
            hashes = {r["input_hash"] for r in edges if r["source_key"] == intent["source_key"]} if intent["source_key"] else set()
            if intent["campaign_id"] == item["id"]:
                hashes.add(intent["input_hash"])
            if hashes:
                pending_intents.append({"id": intent["id"], "reason": intent["reason"], "affected_input_hashes": sorted(hashes)})
                intent_inputs.update(hashes)
        affected_inputs.update(intent_inputs)
        blocked = not reconciled or item["input_artifact"] in affected_inputs
        with self._db(self.consumer) as consumer:
            receipts = consumer.execute("SELECT event_id,input_hash FROM marks WHERE campaign_id=? LIMIT 10001", (item["id"],)).fetchall()
            if len(receipts) > 10000:
                raise LabError("dependency_limit", "Campaign receipt history exceeds its bound")
            received = {(row[0], row[1]) for row in receipts}
        pending = sum((event["id"], digest) not in received for event, digests in effects for digest in digests)
        return {"namespace": self.namespace, "scope": "single_native_administrator", "registered": reconciled,
                "state": "unregistered" if not reconciled else "correction_pending" if pending_intents else "needs_revalidation" if affected_inputs else "current",
                "current_input_blocked": blocked, "run_allowed": not blocked,
                "affected_claim_ids": [c["id"] for c in item.get("claims", []) if c["input_artifact"] in affected_inputs],
                "pending_deliveries": pending, "corrections": [{"id": e["id"], "scope": e["scope"], "reason": e["reason"],
                    "affected_input_hashes": sorted(inputs)} for e, inputs in effects[:50]],
                "total_corrections": len(effects), "has_more": len(effects) > 50,
                "pending_intents": pending_intents,
                "scientific_validity": "not_assessed", "execution_performed": False}

    def snapshot(self, campaign_id):
        with self._db() as db:
            return self._status(db, self._campaign(campaign_id))

    def admission(self, campaign_id):
        """Read-only fast check, not an atomic reservation; use run_guard for that."""
        status = self.snapshot(campaign_id)
        if status["current_input_blocked"]:
            raise LabError("dependency_stale", "A source correction is pending or affects these inputs; reconcile explicitly before new work")
        return status

    def decorate(self, item):
        """Read-only projection. Original persisted snapshots/claims stay intact."""
        with self._db() as db:
            current = self._campaign(item["id"])
            _check(current["revision"] == item["revision"] and current["input_artifact"] == item["input_artifact"], "Reload the campaign before projecting dependencies")
            status = self._status(db, current)
        result = copy.deepcopy(item)
        result["dependency_status"] = status
        for claim in result.get("claims", []):
            if claim["id"] in status["affected_claim_ids"]:
                claim["status"] = "needs_revalidation"
        return result

    @contextmanager
    def run_guard(self, campaign_id):
        """Hold the correction producer lock across the caller's local run.

        Order must always be dependency producer -> Lab transaction. Never call
        this from inside an already open Lab write transaction. No external work
        may be performed while this guard is held.
        """
        with self._db(write=True) as db:
            item = self._campaign(campaign_id)
            self._register(db, item)
            status = self._status(db, item)
            if status["current_input_blocked"]:
                raise LabError("dependency_stale", "A declared source correction affects the frozen plan; explicitly correct its inputs before running")
            yield status

    def _branch_state(self, db, item, projections, *, register):
        """Called with a service Lab snapshot while the producer lock is held."""
        state = item.get("branch_set") or {}
        registered, blocked = [], []
        for branch in state.get("branches", []):
            digest = branch["input_artifact"]
            inputs = projections[branch["id"]]
            _check(_hash(inputs) == digest, "Branch projection differs from its frozen artifact")
            if register:
                added = 0
                for source in inputs.get("sources", []):
                    for ref in self._source_references(item, source):
                        edge = {"namespace": self.namespace, "campaign_id": item["id"], "input_hash": digest, **ref}
                        added += db.execute("INSERT OR IGNORE INTO edges VALUES (?,?,?,?,?)", (item["id"], digest, ref["source_key"], ref["evidence_key"], _json(edge))).rowcount
                hashes = {hashlib.sha256(source["text"].encode()).hexdigest() for source in inputs.get("sources", [])}
                prior = db.execute("SELECT data FROM edges WHERE campaign_id=? LIMIT 301", (item["id"],)).fetchall()
                if len(prior) > 300:
                    raise LabError("dependency_limit", "Branch dependency history exceeds its bound")
                for row in prior:
                    edge = _decode(row[0])
                    if edge["source_hash"] in hashes:
                        edge["input_hash"] = digest
                        added += db.execute("INSERT OR IGNORE INTO edges VALUES (?,?,?,?,?)", (item["id"], digest, edge["source_key"], edge["evidence_key"], _json(edge))).rowcount
                db.execute("INSERT OR IGNORE INTO scope_registrations VALUES (?,?,?)", (item["id"], branch["id"], digest))
                if added:
                    db.execute("UPDATE outbox SET cursor='',complete=0")
            local = {**item, "input_artifact": digest}
            status = self._status(db, local, branch["id"])
            pending_local = db.execute("SELECT data FROM intents WHERE state='pending' AND json_extract(data,'$.campaign_id')=?", (item["id"],)).fetchall()
            source_ids = {source["id"] for source in inputs.get("sources", [])}
            pending_selected = any((intent := _decode(row[0]))["input_hash"] == item["input_artifact"]
                                   and intent.get("source_id") in source_ids for row in pending_local)
            if status["registered"]:
                registered.append(digest)
            if status["current_input_blocked"] or pending_selected:
                blocked.append(digest)
        return {"known": True, "registered_input_artifacts": list(set(registered)), "blocked_input_artifacts": list(set(blocked))}

    @contextmanager
    def branch_guard(self, campaign_id, *, write=True):
        """Producer -> Lab -> model. The callback consumes the locked Lab view.

        Admission uses write=True to reconcile exact projections; read-only UI
        uses write=False and reports unknown registration as blocked. Never hold
        either scope across native execution, downloads or other external work.
        """
        with self._db(write=write) as db:
            if write:
                self._register(db, self._campaign(campaign_id))
            def resolve(item, projections):
                _check(item["id"] == campaign_id, "Branch dependency callback crossed campaigns")
                return self._branch_state(db, item, projections, register=write)
            yield resolve

    def scoped_export(self, campaign_id):
        with self._db() as db:
            item = self._campaign(campaign_id)
            effects = self._effects(db, item)
            event_ids = {e["id"] for e, _ in effects}
            data = {"namespace": self.namespace, "campaign_id": campaign_id, "input_hash": item["input_artifact"],
                    "edges": [_decode(r[0]) for r in db.execute("SELECT data FROM edges WHERE campaign_id=? ORDER BY input_hash,source_key,evidence_key", (campaign_id,))],
                    "corrections": [e for e, _ in effects], "status": self._status(db, item)}
            scoped = db.execute("SELECT scope_id,input_hash FROM scope_registrations WHERE campaign_id=? ORDER BY scope_id,input_hash LIMIT 1001", (campaign_id,)).fetchall()
            if len(scoped) > 1000:
                raise LabError("dependency_limit", "Complete branch registration history exceeds its export bound")
            if scoped:
                data["scope_registrations"] = [dict(row) for row in scoped]
            keys = {edge["source_key"] for edge in data["edges"]}
            sql, args = "SELECT data,request_hash,state,event_id FROM intents WHERE json_extract(data,'$.campaign_id')=?", [campaign_id]
            if keys:
                sql += f" OR json_extract(data,'$.source_key') IN ({','.join('?' for _ in keys)})"
                args.extend(sorted(keys))
            intents = db.execute(sql + " ORDER BY rowid LIMIT 1001", args).fetchall()
            if len(intents) > 1000:
                raise LabError("dependency_limit", "Complete dependency intent history exceeds its export bound")
            data["intents"] = [{"record": _decode(r[0]), "request_hash": r[1], "state": r[2], "event_id": r[3]} for r in intents]
            with self._db(self.consumer) as consumer:
                receipts = consumer.execute("SELECT data FROM marks WHERE campaign_id=? ORDER BY event_id,input_hash LIMIT 10001", (campaign_id,)).fetchall()
                if len(receipts) > 10000:
                    raise LabError("dependency_limit", "Campaign receipt history exceeds its bound")
                data["receipts"] = [value for r in receipts if (value := _decode(r[0]))["event_id"] in event_ids]
        bundle = {"format": "agentsdock-dependencies/1", "data": data,
                  "scope": "Separate sidecar bundle; core campaign export does not include these dependency records",
                  "authentication": "content checksum, not a signature or multi-user authorization"}
        bundle["sha256"] = _hash(bundle)
        if len(_json(bundle).encode()) > MAX_EXPORT:
            raise LabError("dependency_limit", "Complete dependency export exceeds its byte limit; no partial bundle produced")
        return bundle
