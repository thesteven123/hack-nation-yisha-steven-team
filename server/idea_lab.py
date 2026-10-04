"""Versioned research briefs and bounded, explicitly requested idea generation.

Supplied literature is untrusted data. Exact quote validation establishes source
occurrence only; neither generated ideas nor human choices establish scientific
truth or authorize experiments. No provider is invoked by this storage module.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 2
STAGES = ("literature", "ideas", "review")
LIMITS = {"max_rounds": 3, "max_model_calls": 12, "max_sources": 6, "max_search_queries": 9}
MAX_STAGE_ARTIFACT_BYTES = 128 * 1024
MAX_VALIDATION_DIAGNOSTIC_BYTES = 16 * 1024
QUESTION_SCHEMA = {"type": "object", "properties": {
    "id": {"type": "string", "minLength": 1, "maxLength": 128},
    "question": {"type": "string", "minLength": 1, "maxLength": 2000},
    "why": {"type": "string", "minLength": 1, "maxLength": 2000},
    "required": {"type": "boolean"},
    "options": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 1000}, "minItems": 0, "maxItems": 3},
}, "required": ["id", "question", "why", "required", "options"], "additionalProperties": False}


def _string_schema(limit=4000, minimum=1):
    return {"type": "string", "minLength": minimum, "maxLength": limit}


def _object(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def _array(items, maximum=20, minimum=0):
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": maximum}


_ID = _string_schema(128)
GENERATION_SCHEMAS = {
    "literature": _object({
        "summary": _string_schema(),
        "evidence": _array(_object({
            "id": _ID, "source_id": _ID, "quote": _string_schema(12000),
            "finding": _string_schema(), "limitation": _string_schema(),
            "conditions": _string_schema(), "assumptions": _array(_string_schema(1000), 8),
            "interpretation": _string_schema(),
        }), 20),
        "gaps": _array(_string_schema(), 20),
    }),
    "ideas": _object({
        "directions": _array(_object({
            "id": _ID, "title": _string_schema(256), "question": _string_schema(2000),
            "nearest_work": _string_schema(), "evidence_ids": _array(_ID, 20),
            "counterevidence": _array(_string_schema(), 20), "value": _string_schema(),
            "uncertainty": _string_schema(), "minimal_action": _string_schema(),
            "expected_learning": _string_schema(), "feasibility": _string_schema(),
            "cost_risk": _string_schema(),
        }), 3, 2),
        "open_alternative": _string_schema(),
    }),
    "review": _object({
        "recommendation_id": _ID, "reason": _string_schema(),
        "critiques": _array(_object({
            "direction_id": _ID, "concerns": _array(_string_schema(), 20),
            "test_before_commit": _string_schema(),
        }), 3, 2),
        "missing_evidence": _array(_string_schema(), 20),
        "comparison_summary": _string_schema(),
        "disposition": {"type": "string", "enum": ["ready", "retrieve_more", "revise_ideas", "needs_input"]},
        "followup_queries": _array(_string_schema(1000), 3),
        "questions": _array(QUESTION_SCHEMA, 3),
    }),
}


class IdeaError(Exception):
    def __init__(self, code, message, *, diagnostics=None):
        super().__init__(message)
        self.code, self.message = code, message
        self.diagnostics = diagnostics


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _keys(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise IdeaError("invalid_request", "Unexpected or missing request fields")


def _text(value, limit, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise IdeaError("invalid_request", "Expected a nonempty string")
    try:
        if len(value.encode("utf-8")) > limit:
            raise IdeaError("invalid_request", "Text exceeds its UTF-8 input limit")
    except UnicodeEncodeError:
        raise IdeaError("invalid_request", "Text must be valid UTF-8") from None
    return value


def validate_brief(brief):
    required = {"goal", "hypothesis", "constraints", "sources"}
    if not isinstance(brief, dict) or not required <= set(brief) or set(brief) - required - {"success_criteria", "preferences"}:
        raise IdeaError("invalid_request", "Unexpected or missing brief fields")
    goal = _text(brief["goal"], 8000)
    hypothesis = _text(brief["hypothesis"], 8000, empty=True)
    constraints = _text(brief["constraints"], 8000, empty=True)
    if not isinstance(brief["sources"], list) or len(brief["sources"]) > 5:
        raise IdeaError("invalid_request", "Provide at most five supplied sources")
    sources = []
    for source in brief["sources"]:
        _keys(source, ("id", "title", "uri", "text"))
        sources.append({"id": _text(source["id"], 128), "title": _text(source["title"], 1000),
                        "uri": _text(source["uri"], 4096), "text": _text(source["text"], 30000)})
    if len({source["id"] for source in sources}) != len(sources):
        raise IdeaError("invalid_request", "Source IDs must be unique")
    result = {"goal": goal, "hypothesis": hypothesis, "constraints": constraints, "sources": sources}
    for key in ("success_criteria", "preferences"):
        if key in brief:
            result[key] = _text(brief[key], 8000, empty=True)
    return result


def _schema_check(value, schema):
    kind = schema["type"]
    valid = True
    if kind == "object":
        valid = isinstance(value, dict) and set(value) == set(schema["properties"])
        if valid:
            for key, child in schema["properties"].items():
                _schema_check(value[key], child)
    elif kind == "array":
        valid = isinstance(value, list) and schema["minItems"] <= len(value) <= schema["maxItems"]
        if valid:
            for child in value:
                _schema_check(child, schema["items"])
    elif kind == "string":
        valid = isinstance(value, str) and schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 4000)
        if "enum" in schema:
            valid = valid and value in schema["enum"]
        if valid:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                valid = False
    elif kind == "boolean":
        valid = type(value) is bool
    if not valid:
        raise IdeaError("invalid_output", "Generated output did not satisfy the required bounded schema")


def validate_output(stage, output, brief, previous):
    """Validate generated structure/references and attach mechanical provenance."""
    if stage not in GENERATION_SCHEMAS:
        raise IdeaError("invalid_output", "Unknown generation stage")
    _schema_check(output, GENERATION_SCHEMAS[stage])
    output = copy.deepcopy(output)
    if stage == "literature":
        sources = {source["id"]: source for source in brief["sources"]}
        seen = set()
        failures = []
        for index, evidence in enumerate(output["evidence"]):
            source = sources.get(evidence["source_id"])
            gap_marker = (source or {}).get("provenance", {}).get("gap_marker")
            reasons = []
            if evidence["id"] in seen:
                reasons.append("duplicate_evidence_id")
            if source is None:
                reasons.append("unknown_source_id")
            elif evidence["quote"] not in source["text"]:
                reasons.append("quote_not_exact")
            if gap_marker and gap_marker in evidence["quote"]:
                reasons.append("quote_crosses_omission")
            seen.add(evidence["id"])
            if reasons:
                failures.append({"evidence_index": index, "evidence_id": evidence["id"].encode("utf-8")[:128].decode("utf-8", errors="ignore"),
                                 "source_id": evidence["source_id"].encode("utf-8")[:128].decode("utf-8", errors="ignore"), "reasons": reasons,
                                 "quote_characters": len(evidence["quote"]),
                                 "quote_hash": hashlib.sha256(evidence["quote"].encode("utf-8")).hexdigest(),
                                 "source_packet_hash": hashlib.sha256(source["text"].encode("utf-8")).hexdigest() if source else None})
                continue
            evidence["source_hash"] = hashlib.sha256(source["text"].encode("utf-8")).hexdigest()
            evidence["source_support"] = "exact_quote_verified"
            evidence["inference_validity"] = "not_assessed"
            start = source["text"].index(evidence["quote"])
            evidence["span"] = {"start": start, "end": start + len(evidence["quote"]), "coordinate": "source_packet_characters"}
            evidence["coverage"] = source.get("coverage", {"kind": "supplied_excerpt", "limitations": ["Original completeness unverified"]})
        if failures:
            diagnostics = {"version": 1, "kind": "evidence_reference_validation", "repairable": True,
                           "checked_evidence": len(output["evidence"]), "failures": failures,
                           "allowed_source_ids": list(sources),
                           "failure_codes": sorted({reason for failure in failures for reason in failure["reasons"]})}
            while len(_json(diagnostics).encode("utf-8")) > MAX_VALIDATION_DIAGNOSTIC_BYTES:
                if diagnostics["failures"]:
                    diagnostics["failures"].pop()
                    diagnostics["failures_omitted"] = diagnostics.get("failures_omitted", 0) + 1
                else:
                    diagnostics["allowed_source_ids"].pop()
                    diagnostics["source_ids_omitted"] = diagnostics.get("source_ids_omitted", 0) + 1
            raise IdeaError("invalid_output", "Literature evidence failed exact quote or source-reference validation; rejected evidence was not published",
                            diagnostics=diagnostics)
        if not output["evidence"] and not output["gaps"]:
            raise IdeaError("invalid_output", "Literature without evidence must declare its evidence gap")
    elif stage == "ideas":
        evidence_ids = {entry["id"] for entry in previous["literature"]["evidence"]}
        seen = set()
        for direction in output["directions"]:
            if direction["id"] in seen or not set(direction["evidence_ids"]) <= evidence_ids:
                raise IdeaError("invalid_output", "Ideas contain an invalid evidence reference or duplicate direction")
            seen.add(direction["id"])
            direction["grounding"] = "source_linked" if direction["evidence_ids"] else "provisional"
    else:
        ids = {entry["id"] for entry in previous["ideas"]["directions"]}
        critiques = [entry["direction_id"] for entry in output["critiques"]]
        if output["recommendation_id"] not in ids or set(critiques) != ids or len(critiques) != len(ids):
            raise IdeaError("invalid_output", "Review must reference and critique each generated direction once")
    return output


def research_state(*, legacy=False):
    return {"version": 2, "round": 0, "max_rounds": LIMITS["max_rounds"],
            "readiness": "legacy_unsearched" if legacy else "not_started", "stop_reason": None,
            "limits": dict(LIMITS), "used": {"model_calls": 0, "searches": 0, "reads": 0, "cache_hits": 0},
            "papers": [], "sources": [], "searches": [], "activities": [], "rounds": [], "questions": [],
            "validation_attempts": [], "pending_repair": None,
            "coverage_gaps": [], "agents": [
                {"id": "literature", "name": "Literature Agent", "role": "Discover, read and maintain evidence", "status": "idle", "task": None},
                {"id": "idea", "name": "Idea Agent", "role": "Develop and compare research directions", "status": "idle", "task": None}]}


def _hydrate(item):
    if "research" not in item:
        item["research"] = research_state(legacy=True)
        item["research"]["sources"] = copy.deepcopy(item.get("brief", {}).get("sources", []))
    item.setdefault("brief_revision", 1)
    item.setdefault("decision_card", None)
    return item


def _page_items(rows, *, listing=False):
    items, size = [], 0
    for row in rows:
        item = _hydrate(json.loads(row["data"]))
        item["summary_only"] = True
        item["brief"]["sources"] = []
        item["research"].update(sources=[], activities=[], rounds=[])
        if listing:
            item.update(result=None, events=[], usage=[], decision_card=None)
            item["research"].update(papers=[], searches=[])
        encoded_size = len(_json(item).encode("utf-8"))
        if len(items) >= 50 or (items and size + encoded_size > 1024 * 1024):
            break
        items.append(item)
        size += encoded_size
    return {"items": items, "has_more": len(items) < len(rows)}


class IdeaStore:
    def __init__(self, root):
        self.root = Path(root).absolute()
        self.path = self.root / "ideas.sqlite3"
        try:
            if self.root.is_symlink() or self.path.is_symlink():
                raise IdeaError("storage_error", "Idea state cannot use symlinks")
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with self._db(initialize=True) as db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, SCHEMA_VERSION):
                    raise IdeaError("unsupported_schema", "Unsupported idea database schema")
                if version == 0:
                    if db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                        raise IdeaError("unsupported_schema", "Unversioned nonempty idea database")
                    db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, create_key TEXT UNIQUE, request_hash TEXT, updated_at TEXT, data TEXT)")
                    db.execute("CREATE TABLE versions(session_id TEXT, revision INTEGER, data TEXT, PRIMARY KEY(session_id,revision))")
                    db.execute("CREATE TABLE generations(id TEXT PRIMARY KEY, session_id TEXT, request_key TEXT, request_hash TEXT, status TEXT, brief TEXT, UNIQUE(session_id,request_key))")
                if version < 2:
                    db.execute("CREATE TABLE followups(session_id TEXT, request_key TEXT, request_hash TEXT, revision INTEGER, PRIMARY KEY(session_id,request_key))")
                    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                db.execute("CREATE TABLE IF NOT EXISTS activities(seq INTEGER PRIMARY KEY AUTOINCREMENT,session_id TEXT,generation_id TEXT,data TEXT)")
                db.execute("CREATE TABLE IF NOT EXISTS stage_packets(generation_id TEXT,round INTEGER,stage TEXT,sources TEXT,context TEXT,PRIMARY KEY(generation_id,round,stage))")
                db.execute("CREATE TABLE IF NOT EXISTS stage_attempts(id TEXT PRIMARY KEY,session_id TEXT,generation_id TEXT,round INTEGER,stage TEXT,ordinal INTEGER,data TEXT,UNIQUE(generation_id,round,stage,ordinal))")
            self.path.chmod(0o600)
        except OSError:
            raise IdeaError("storage_error", "Idea storage is unavailable") from None

    @contextmanager
    def _db(self, *, initialize=False):
        db = None
        try:
            if self.root.is_symlink() or self.path.is_symlink():
                raise IdeaError("storage_error", "Idea state cannot use symlinks")
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            if not initialize and db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise IdeaError("unsupported_schema", "Unsupported idea database schema")
            yield db
            db.commit()
        except (sqlite3.Error, OSError):
            raise IdeaError("storage_error", "Idea storage failed; the transaction was rolled back") from None
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _get(db, session_id, *, include_deleted=False):
        if not isinstance(session_id, str) or not re.fullmatch(r"[0-9a-f]{32}", session_id):
            raise IdeaError("not_found", "Idea session was not found")
        row = db.execute("SELECT data FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise IdeaError("not_found", "Idea session was not found")
        item = _hydrate(json.loads(row["data"]))
        if item.get("deleted_at") and not include_deleted:
            raise IdeaError("deleted", "This research is in Trash. Restore it before opening or continuing it.")
        return item

    @staticmethod
    def _save(db, item, event, **details):
        item["revision"] += 1
        item["updated_at"] = _now()
        item["events"].append({"type": event, "at": item["updated_at"], **details})
        encoded = _json(item)
        db.execute("UPDATE sessions SET data=?,updated_at=? WHERE id=?", (encoded, item["updated_at"], item["id"]))
        db.execute("INSERT INTO versions(session_id,revision,data) VALUES(?,?,?)", (item["id"], item["revision"], encoded))
        return item

    @staticmethod
    def _revision(item, revision):
        if type(revision) is not int or revision < 1:
            raise IdeaError("invalid_request", "expected_revision must be a positive integer")
        if item["revision"] != revision:
            raise IdeaError("stale_revision", "The idea session changed; inspect its current revision")

    def create(self, request):
        _keys(request, ("idempotency_key", "brief"))
        key = _text(request["idempotency_key"], 128)
        brief = validate_brief(request["brief"])
        digest = _hash(brief)
        with self._db() as db:
            prior = db.execute("SELECT id,request_hash FROM sessions WHERE create_key=?", (key,)).fetchone()
            if prior:
                if prior["request_hash"] != digest:
                    raise IdeaError("idempotency_conflict", "Creation key already identifies another brief")
                return self._get(db, prior["id"])
            now, session_id = _now(), uuid.uuid4().hex
            item = {"id": session_id, "revision": 0, "created_at": now, "updated_at": now,
                    "brief": brief, "status": "draft", "phase": None, "generation_id": None,
                    "result": None, "decision": None, "error": None, "usage": [], "events": [],
                    "research": research_state(), "brief_revision": 1, "decision_card": None}
            db.execute("INSERT INTO sessions(id,create_key,request_hash,updated_at,data) VALUES(?,?,?,?,?)", (session_id, key, digest, now, "{}"))
            return self._save(db, item, "created")

    def get(self, session_id):
        with self._db() as db:
            return self._get(db, session_id)

    def list(self, trashed=False):
        with self._db() as db:
            condition = "IS NOT NULL" if trashed else "IS NULL"
            rows = db.execute(f"SELECT data FROM sessions WHERE json_extract(data,'$.deleted_at') {condition} ORDER BY updated_at DESC,id LIMIT 51").fetchall()
            return _page_items(rows, listing=True)

    def set_deleted(self, session_id, request, deleted):
        """Remove from active work without erasing shared evidence or history."""
        _keys(request, ("expected_revision",))
        with self._db() as db:
            item = self._get(db, session_id, include_deleted=True)
            self._revision(item, request["expected_revision"])
            if bool(item.get("deleted_at")) == deleted:
                return item
            if item["status"] == "running":
                raise IdeaError("busy", "Stop this generation before deleting the research.")
            item["deleted_at"] = _now() if deleted else None
            return self._save(db, item, "deleted" if deleted else "restored")

    def history(self, session_id):
        with self._db() as db:
            self._get(db, session_id)
            rows = db.execute("SELECT data FROM versions WHERE session_id=? ORDER BY revision DESC LIMIT 51", (session_id,)).fetchall()
            return _page_items(rows)

    def begin(self, session_id, request, capacity_available=True, full_research=False):
        _keys(request, ("idempotency_key", "expected_revision"))
        key = _text(request["idempotency_key"], 128)
        digest = _hash(request)
        with self._db() as db:
            item = self._get(db, session_id)
            prior = db.execute("SELECT request_hash FROM generations WHERE session_id=? AND request_key=?", (session_id, key)).fetchone()
            if prior:
                if prior["request_hash"] != digest:
                    raise IdeaError("idempotency_conflict", "Generation key already identifies another request")
                return item, False
            self._revision(item, request["expected_revision"])
            if item["status"] == "running":
                raise IdeaError("busy", "This idea session is already generating")
            if not capacity_available:
                raise IdeaError("busy", "Four idea sessions are already generating")
            generation_id = uuid.uuid4().hex
            db.execute("INSERT INTO generations(id,session_id,request_key,request_hash,status,brief) VALUES(?,?,?,?,?,?)",
                       (generation_id, session_id, key, digest, "running", _json(item["brief"])))
            prior_context = {"review": (item["result"] or {}).get("review"),
                             "coverage_gaps": item["research"].get("coverage_gaps", []),
                             "searches": item["research"].get("searches", [])[-6:]}
            last_feedback = (item["brief"].get("feedback") or [{}])[-1]
            reuse = (full_research and last_feedback.get("mode") in ("refine", "selected", "select", "combine")
                     and bool(item["research"].get("papers")) and bool((item["result"] or {}).get("literature")))
            prior_literature = copy.deepcopy((item["result"] or {}).get("literature")) if reuse else None
            research = research_state(legacy=not full_research)
            research.update(active=full_research, prior_context=prior_context,
                            readiness="researching" if full_research else "legacy_unsearched")
            research["sources"] = copy.deepcopy(item["brief"]["sources"])
            if reuse:
                research["sources"] = copy.deepcopy(item["research"]["sources"])
                research["papers"] = [{**copy.deepcopy(paper), "reused_in_generation": True} for paper in item["research"]["papers"]]
                research["searches"] = [{**copy.deepcopy(search), "reused_in_generation": True} for search in item["research"]["searches"]]
                research["coverage_gaps"] = list(item["research"]["coverage_gaps"])
                research["reused_source_packets"] = len(research["sources"])
            item.update(status="running", phase="searching" if full_research else "literature", generation_id=generation_id,
                        result={"literature": prior_literature, "ideas": None, "review": None}, decision=None, error=None, usage=[],
                        research=research, decision_card=None)
            return self._save(db, item, "generation_started", generation_id=generation_id), True

    def save_stage(self, session_id, generation_id, stage, generated, allow_repair=False):
        if not isinstance(generated, dict) or set(generated) != {"output", "usage", "provider"}:
            raise IdeaError("invalid_output", "Provider returned an invalid generation envelope")
        for field in ("usage", "provider"):
            try:
                valid = isinstance(generated[field], dict) and len(_json(generated[field]).encode("utf-8")) <= 16000
            except (ValueError, TypeError, RecursionError, UnicodeError):
                valid = False
            if not valid:
                raise IdeaError("invalid_output", "Provider returned invalid bounded usage metadata")
        artifact, output_hash, output_bytes, artifact_problem = None, None, None, None
        try:
            encoded = _json(generated["output"]).encode("utf-8")
            output_hash, output_bytes = hashlib.sha256(encoded).hexdigest(), len(encoded)
            if output_bytes > MAX_STAGE_ARTIFACT_BYTES:
                artifact_problem = "output_too_large"
            else:
                artifact = json.loads(encoded)
        except (ValueError, TypeError, RecursionError, UnicodeError):
            artifact_problem = "invalid_json"
        rejected = None
        with self._db() as db:
            item = self._get(db, session_id)
            if item["generation_id"] != generation_id or item["status"] != "running" or item["phase"] != stage:
                raise IdeaError("stale_generation", "Generation is no longer current")
            item["usage"].append({"stage": stage, "generation_id": generation_id,
                                   "usage": generated["usage"], "provider": generated["provider"]})
            validation_brief = {**item["brief"], "sources": item["research"]["sources"] or item["brief"]["sources"]}
            packet = None
            try:
                if artifact_problem:
                    raise IdeaError("invalid_output", "Generated stage artifact exceeded its storage or JSON bounds",
                                    diagnostics={"version": 1, "kind": artifact_problem, "repairable": False})
                if stage == "literature" and item["research"].get("active"):
                    packet = db.execute("SELECT sources,context FROM stage_packets WHERE generation_id=? AND round=? AND stage=?",
                                        (generation_id, item["research"]["round"], stage)).fetchone()
                    if packet is None:
                        raise IdeaError("invalid_output", "Literature input packet was not frozen before generation")
                    validation_brief["sources"] = json.loads(packet["sources"])
                output = validate_output(stage, generated["output"], validation_brief, item["result"])
            except IdeaError as exc:
                # A rejected answer still consumed its reported resources.
                rejected = exc
            research = item["research"]
            ordinal = db.execute("SELECT COALESCE(MAX(ordinal),0)+1 FROM stage_attempts WHERE generation_id=? AND round=? AND stage=?",
                                 (generation_id, research["round"], stage)).fetchone()[0]
            diagnostics = (rejected.diagnostics or {"version": 1, "kind": "stage_validation", "repairable": False}) if rejected else None
            can_repair = bool(rejected and allow_repair and stage == "literature" and research.get("active")
                              and diagnostics.get("repairable") and ordinal == 1 and packet is not None
                              and generated["provider"].get("stop_reason") is None)
            pending = research.get("pending_repair") or {}
            input_ref = {"generation_id": generation_id, "round": research["round"], "stage": stage,
                         "brief_revision": item["brief_revision"], "previous_result_hash": _hash(item["result"]),
                         "packet_digest": _hash({"sources": json.loads(packet["sources"]), "context": json.loads(packet["context"])}) if packet else None}
            summary = {"id": uuid.uuid4().hex, "at": _now(), "generation_id": generation_id, "round": research["round"],
                       "stage": stage, "ordinal": ordinal, "status": "rejected" if rejected else "accepted",
                       "error_code": rejected.code if rejected else None, "diagnostics": diagnostics,
                       "input_ref": input_ref, "output_hash": output_hash, "output_bytes": output_bytes,
                       "output_retained": artifact is not None, "repair_of": pending.get("attempt_id"),
                       "repair_scheduled": can_repair}
            record = {**summary, "output": artifact, "usage": copy.deepcopy(generated["usage"]), "provider": copy.deepcopy(generated["provider"])}
            db.execute("INSERT INTO stage_attempts VALUES(?,?,?,?,?,?,?)",
                       (summary["id"], session_id, generation_id, research["round"], stage, ordinal, _json(record)))
            research.setdefault("validation_attempts", []).append(summary)
            if rejected:
                if can_repair:
                    # Only a completed, structurally valid extraction can be
                    # repaired. The controller must reserve another native job
                    # before dispatch; no provider call occurs in this store.
                    research["pending_repair"] = {"attempt_id": summary["id"], "number": 1, "input_ref": input_ref,
                                                  "diagnostics": diagnostics,
                                                  "provider": {key: generated["provider"].get(key) for key in
                                                               ("backend", "model", "requested_model", "resolved_model", "model_resolution")}}
                    item["error"] = None
                    for agent in research["agents"]:
                        agent.update(status="idle", task=None)
                else:
                    item.update(status="failed", phase=None, error=rejected.message)
                    research.update(readiness="failed", stop_reason="evidence_validation_failed" if diagnostics.get("repairable") else rejected.code,
                                    pending_repair=None)
                    for agent in research["agents"]:
                        agent.update(status="failed", task=None)
                    db.execute("UPDATE generations SET status='failed' WHERE id=?", (generation_id,))
                self._save(db, item, "stage_rejected", stage=stage, generation_id=generation_id,
                           attempt_id=summary["id"], repair_scheduled=can_repair, failure_codes=diagnostics.get("failure_codes", []))
            else:
                research["pending_repair"] = None
                item["result"][stage] = output
                # Publishing new upstream output invalidates dependent results.
                # Earlier rounds remain in immutable snapshots, but an exhausted
                # repair budget must not pair new ideas with an old review.
                index = STAGES.index(stage)
                for downstream in STAGES[index + 1:]:
                    item["result"][downstream] = None
                role = "literature" if stage == "literature" else "idea"
                for agent in item["research"]["agents"]:
                    if agent["id"] == role:
                        agent.update(status="idle", task=None)
                item["phase"] = STAGES[index + 1] if index < 2 else None
                if index == 2 and not item["research"].get("active"):
                    item["status"] = "completed"
                    db.execute("UPDATE generations SET status='completed' WHERE id=?", (generation_id,))
                self._save(db, item, "stage_completed", stage=stage, generation_id=generation_id, attempt_id=summary["id"])
        if rejected is not None and not can_repair:
            raise rejected
        return item

    def stage_attempt(self, session_id, attempt_id):
        """Explicit authenticated inspection; raw output is never in default UI state."""
        if not isinstance(attempt_id, str) or re.fullmatch(r"[0-9a-f]{32}", attempt_id) is None:
            raise IdeaError("invalid_request", "Invalid stage attempt identity")
        with self._db() as db:
            self._get(db, session_id)
            row = db.execute("SELECT data FROM stage_attempts WHERE id=? AND session_id=?", (attempt_id, session_id)).fetchone()
            if row is None:
                raise IdeaError("not_found", "Stage attempt was not found in this session")
            return json.loads(row["data"])

    def stop(self, session_id, generation_id, status, message=None):
        if status not in ("failed", "cancelled", "interrupted"):
            raise ValueError("Invalid terminal generation status")
        with self._db() as db:
            item = self._get(db, session_id)
            if item["generation_id"] != generation_id or item["status"] != "running":
                return item
            item.update(status=status, phase=None, error=message)
            item["research"]["readiness"] = "failed" if status == "failed" else "needs_input"
            item["research"]["stop_reason"] = status
            item["research"]["pending_repair"] = None
            for agent in item["research"]["agents"]:
                agent.update(status=status, task=None)
            db.execute("UPDATE generations SET status=? WHERE id=?", (status, generation_id))
            return self._save(db, item, "generation_" + status, generation_id=generation_id)

    def interrupt_running(self):
        with self._db() as db:
            rows = db.execute("SELECT data FROM sessions").fetchall()
            for row in rows:
                item = _hydrate(json.loads(row["data"]))
                if item["status"] == "running":
                    item.update(status="interrupted", phase=None, error="Service restarted; generation was not automatically retried")
                    item["research"]["readiness"] = "needs_input"
                    item["research"]["stop_reason"] = "service_interrupted"
                    item["research"]["pending_repair"] = None
                    for agent in item["research"]["agents"]:
                        agent.update(status="interrupted", task=None)
                    db.execute("UPDATE generations SET status='interrupted' WHERE id=?", (item["generation_id"],))
                    self._save(db, item, "generation_interrupted", generation_id=item["generation_id"])

    @staticmethod
    def _current(db, session_id, generation_id):
        item = IdeaStore._get(db, session_id)
        if item["generation_id"] != generation_id or item["status"] != "running":
            raise IdeaError("stale_generation", "Generation is no longer current")
        return item

    def activity(self, session_id, generation_id, event):
        if not isinstance(event, dict):
            return
        allowed = {"agent", "type", "summary", "query", "url", "source_id", "thread_id", "turn_id", "task"}
        usage = event.get("usage")
        event = {key: value[:(2000 if key == "url" else 1000 if key == "query" else 500)]
                 for key, value in event.items() if key in allowed and isinstance(value, str)}
        if event.get("agent") not in ("literature", "idea"):
            return
        with self._db() as db:
            item = self._current(db, session_id, generation_id)
            research = item["research"]
            record = {**event, "id": uuid.uuid4().hex, "at": _now(), "generation_id": generation_id, "round": research["round"]}
            if isinstance(usage, dict) and len(_json(usage).encode("utf-8")) <= 8000:
                record["usage"] = copy.deepcopy(usage)
            cursor = db.execute("INSERT INTO activities(session_id,generation_id,data) VALUES(?,?,?)", (session_id, generation_id, _json(record)))
            record["seq"] = cursor.lastrowid
            research["activities"].append(record)
            if len(research["activities"]) > 80:
                research["activities"].pop(0)
                research["activities_omitted"] = research.get("activities_omitted", 0) + 1
            for agent in research["agents"]:
                if agent["id"] == event["agent"]:
                    kind = event.get("type")
                    if kind in ("task_started", "provider_task_started"):
                        agent.update(status="working", task=event.get("task", item["phase"]))
                        agent.update({key: event[key] for key in ("thread_id", "turn_id") if key in event})
                    elif kind in ("provider_task_completed", "provider_task_stopped", "handoff"):
                        if not event.get("task") or agent["task"] in (None, event["task"]):
                            agent.update(status="idle", task=None)
                    # Receipts and follow-up requests describe completed work;
                    # they cannot start a new owned task or revive a finished one.
            # Activity has identity/time but does not invalidate a human answer
            # or duplicate an entire research snapshot on every native receipt.
            db.execute("UPDATE sessions SET data=? WHERE id=?", (_json(item), session_id))

    def work(self, session_id, generation_id, phase, *, model=False, new_round=False):
        with self._db() as db:
            item = self._current(db, session_id, generation_id)
            research = item["research"]
            if new_round:
                if research["round"] >= research["limits"]["max_rounds"]:
                    raise IdeaError("budget_exhausted", "Research round limit reached")
                research["round"] += 1
            if model:
                if research["used"]["model_calls"] >= research["limits"]["max_model_calls"]:
                    raise IdeaError("budget_exhausted", "Native job limit reached")
                research["used"]["model_calls"] += 1
            item["phase"] = phase
            role = "literature" if phase in ("searching", "reading", "literature") else "idea"
            for agent in research["agents"]:
                agent.update(status="working" if agent["id"] == role else "idle",
                             task=phase if agent["id"] == role else None)
            return self._save(db, item, "work_started", phase=phase, round=research["round"], model_job=model)

    def freeze_packet(self, session_id, generation_id, stage, sources, context):
        """Freeze the actual role projection, not the larger inspectable library."""
        with self._db() as db:
            item = self._current(db, session_id, generation_id)
            research = item["research"]
            encoded_sources, encoded_context = _json(sources), _json(context)
            prior = db.execute("SELECT sources,context FROM stage_packets WHERE generation_id=? AND round=? AND stage=?",
                               (generation_id, research["round"], stage)).fetchone()
            if prior:
                if prior["sources"] != encoded_sources or prior["context"] != encoded_context:
                    raise IdeaError("stale_generation", "A frozen role packet cannot change during execution")
                return item
            db.execute("INSERT INTO stage_packets VALUES(?,?,?,?,?)", (generation_id, research["round"], stage, encoded_sources, encoded_context))
            metadata = {"round": research["round"], "stage": stage, "digest": _hash({"sources": sources, "context": context}),
                        "sources": [{"id": source["id"], "packet_hash": hashlib.sha256(source["text"].encode("utf-8")).hexdigest(),
                                     "characters": len(source["text"]), "coverage": source.get("coverage", {"kind": "supplied_excerpt"})} for source in sources]}
            research.setdefault("input_packets", []).append(metadata)
            return self._save(db, item, "role_packet_frozen", stage=stage, round=research["round"], digest=metadata["digest"])

    def discovery(self, session_id, generation_id, result):
        if not isinstance(result, dict) or not isinstance(result.get("papers"), list) or not isinstance(result.get("searches"), list):
            raise IdeaError("invalid_output", "Discovery did not return inspectable search receipts")
        if len(_json(result).encode("utf-8")) > 300000:
            raise IdeaError("invalid_output", "Discovery receipt exceeds its bound")
        with self._db() as db:
            item = self._current(db, session_id, generation_id)
            research = item["research"]
            searches = copy.deepcopy(result["searches"][:20])
            research["searches"].extend(searches)
            research["used"]["searches"] += sum(max(0, search.get("query_count", 1))
                                                  for search in searches if isinstance(search, dict) and type(search.get("query_count", 1)) is int)
            research["coverage_gaps"] = list(result.get("gaps", []))[:20]
            research["search_summary"] = str(result.get("summary", ""))[:8000]
            item["usage"].append({"stage": "discovery", "generation_id": generation_id,
                                  "round": research["round"], "usage": result.get("usage", {}), "provider": result.get("provider", {})})
            return self._save(db, item, "discovery_completed", round=research["round"], queries=len(searches))

    def retrieval(self, session_id, generation_id, result):
        if not isinstance(result, dict) or not all(isinstance(result.get(key), list) for key in ("sources", "papers", "coverage_gaps")):
            raise IdeaError("invalid_output", "Reader returned invalid source receipts")
        if len(_json(result).encode("utf-8")) > 700000:
            raise IdeaError("invalid_output", "Reader packet exceeds its bound")
        for source in result["sources"]:
            if not isinstance(source, dict) or not {"id", "title", "uri", "text"} <= set(source):
                raise IdeaError("invalid_output", "Reader source is missing required provenance fields")
            for key, limit in (("id", 256), ("title", 2000), ("uri", 8000), ("text", 100000)):
                _text(source[key], limit)
        with self._db() as db:
            item = self._current(db, session_id, generation_id)
            research = item["research"]
            by_id = {source["id"]: source for source in research["sources"]}
            for source in result["sources"]:
                existing = by_id.get(source["id"])
                if existing and existing["text"] != source["text"]:
                    raise IdeaError("invalid_output", "Changed source text must have a distinct source version ID")
                by_id[source["id"]] = copy.deepcopy(source)
            research["sources"] = list(by_id.values())
            research["papers"].extend(copy.deepcopy(result["papers"]))
            research["coverage_gaps"] = list(dict.fromkeys(research["coverage_gaps"] + result["coverage_gaps"]))[:30]
            research["used"]["reads"] += sum(paper.get("status") == "read" for paper in result["papers"])
            research["used"]["cache_hits"] += sum(bool(paper.get("cache_hit")) for paper in result["papers"])
            item["usage"].append({"stage": "reading", "generation_id": generation_id,
                                  "round": research["round"], "usage": result.get("usage", {}), "provider": {"backend": "public_source_reader"}})
            return self._save(db, item, "reading_completed", round=research["round"], source_count=len(result["sources"]))

    def round_result(self, session_id, generation_id, disposition):
        with self._db() as db:
            item = self._current(db, session_id, generation_id)
            research = item["research"]
            research["rounds"].append({"round": research["round"], **copy.deepcopy(item["result"]),
                                       "coverage_gaps": list(research["coverage_gaps"]), "disposition": disposition})
            return self._save(db, item, "round_completed", round=research["round"], disposition=disposition)

    def finish_research(self, session_id, generation_id, readiness, reason, questions=None):
        with self._db() as db:
            item = self._current(db, session_id, generation_id)
            research = item["research"]
            research.update(readiness=readiness, stop_reason=reason, questions=(questions or [])[:3], pending_repair=None)
            for agent in research["agents"]:
                agent.update(status="waiting_for_user" if readiness in ("ready_for_choice", "needs_input") else "idle", task=None)
            item.update(status="needs_input", phase=None)
            ideas = (item["result"] or {}).get("ideas")
            review = (item["result"] or {}).get("review") or {}
            evidence = ((item["result"] or {}).get("literature") or {}).get("evidence", [])
            if ideas:
                item["decision_card"] = {
                    "id": uuid.uuid4().hex, "brief_revision": item["brief_revision"], "generation_id": generation_id,
                    "options_version": item["revision"] + 1, "evidence_versions": sorted({entry["source_hash"] for entry in evidence}),
                    "choice_needed": "Choose, combine, reject, refine, or defer these research directions.",
                    "recommendation_id": review.get("recommendation_id"), "reason": review.get("reason", ""),
                    "options": copy.deepcopy(ideas["directions"]),
                    "next_actions": [{"direction_id": direction["id"], "action": direction["minimal_action"]} for direction in ideas["directions"]],
                    "questions": research["questions"], "execution_authorized": False,
                }
            db.execute("UPDATE generations SET status='needs_input' WHERE id=?", (generation_id,))
            return self._save(db, item, "research_stopped", readiness=readiness, reason=reason)

    def paper(self, session_id, source_id, source_hash=None):
        if source_hash is not None:
            if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", source_hash):
                raise IdeaError("invalid_request", "source_hash must be a SHA-256 content hash")
            source_hash = source_hash.lower()

        def digest(source):
            return hashlib.sha256(source["text"].encode("utf-8")).hexdigest()

        def result(source, snapshot, **metadata):
            paper = next((paper for paper in snapshot["research"]["papers"] if paper.get("id") == source_id), None)
            return {"source": source, "paper": paper, "packet_hash": digest(source), **metadata}

        with self._db() as db:
            item = self._get(db, session_id)
            if source_hash is not None:
                # Evidence hashes refer to the exact role projection. Scope
                # every lookup to this session, including previous generations.
                packets = db.execute("SELECT p.generation_id,p.round,s.value AS source FROM stage_packets p "
                                     "JOIN generations g ON g.id=p.generation_id, json_each(p.sources) s "
                                     "WHERE g.session_id=? AND json_extract(s.value,'$.id')=? "
                                     "ORDER BY g.rowid DESC,p.round DESC", (session_id, source_id))
                for packet in packets:
                    source = json.loads(packet["source"])
                    if digest(source) != source_hash:
                        continue
                    row = db.execute("SELECT data FROM versions WHERE session_id=? AND json_extract(data,'$.generation_id')=? "
                                     "ORDER BY revision DESC LIMIT 1", (session_id, packet["generation_id"])).fetchone()
                    snapshot = _hydrate(json.loads(row["data"]))
                    return result(source, snapshot, revision=snapshot["revision"], generation_id=packet["generation_id"],
                                  round=packet["round"], archived=packet["generation_id"] != item["generation_id"])
            source = next((source for source in item["research"]["sources"] if source["id"] == source_id), None)
            if source is not None and (source_hash is None or digest(source) == source_hash):
                return result(source, item)
            # Keep original packets inspectable for legacy evidence and library
            # cards as well as projected, frozen role packets above.
            for field in ("research.sources", "brief.sources"):
                rows = db.execute("SELECT v.revision,v.data,s.value AS source FROM versions v, json_each(v.data, ?) s "
                                  "WHERE v.session_id=? AND json_extract(s.value,'$.id')=? ORDER BY v.revision DESC",
                                  ("$." + field, session_id, source_id))
                for row in rows:
                    source = json.loads(row["source"])
                    if source_hash is None or digest(source) == source_hash:
                        return result(source, _hydrate(json.loads(row["data"])), revision=row["revision"], archived=True)
        raise IdeaError("not_found", "Source packet was not found in this session or its history")

    def activities(self, session_id, before=None):
        with self._db() as db:
            self._get(db, session_id)
            if before is not None and (type(before) is not int or before < 1):
                raise IdeaError("invalid_request", "Activity cursor must be a positive integer")
            rows = db.execute("SELECT seq,data FROM activities WHERE session_id=? AND seq<? ORDER BY seq DESC LIMIT 101",
                              (session_id, before or 9223372036854775807)).fetchall()
            items = [{**json.loads(row["data"]), "seq": row["seq"]} for row in rows[:100]]
            return {"items": items, "has_more": len(rows) > 100, "next_before": items[-1]["seq"] if len(rows) > 100 else None}

    def decision(self, session_id, request):
        required = {"expected_revision", "kind", "selected_id", "feedback"}
        if not isinstance(request, dict) or not required <= set(request) or set(request) - required - {"selected_ids", "goal", "answers"}:
            raise IdeaError("invalid_request", "Unexpected or missing decision fields")
        if request["kind"] not in ("select", "revise", "defer", "reject", "combine"):
            raise IdeaError("invalid_request", "Unknown human decision")
        feedback = _text(request["feedback"], 8000, empty=True)
        with self._db() as db:
            item = self._get(db, session_id)
            self._revision(item, request["expected_revision"])
            if item["status"] == "running":
                raise IdeaError("busy", "Wait for or cancel generation before choosing")
            kind, selected = request["kind"], request["selected_id"]
            answers = request.get("answers", [])
            if not isinstance(answers, list) or len(answers) > 3:
                raise IdeaError("invalid_request", "Answer at most three current questions")
            question_ids = {question["id"] for question in item["research"]["questions"]}
            for answer in answers:
                _keys(answer, ("question_id", "answer"))
                _text(answer["question_id"], 128)
                _text(answer["answer"], 8000)
                if answer["question_id"] not in question_ids:
                    raise IdeaError("stale_revision", "The answered question is no longer current")
            if kind in ("select", "combine") and not {q["id"] for q in item["research"]["questions"] if q.get("required")} <= {answer["question_id"] for answer in answers}:
                raise IdeaError("invalid_request", "Answer required current questions before choosing")
            if kind in ("select", "combine") and (item["status"] not in ("completed", "needs_input") or
                    (item["decision_card"] and item["decision_card"]["brief_revision"] != item["brief_revision"])):
                raise IdeaError("stale_revision", "These options belong to an earlier research brief")
            directions = ((item["result"] or {}).get("ideas") or {}).get("directions", [])
            decision_versions = {"brief_revision": item["brief_revision"], "options_revision": item["revision"],
                                 "evidence_versions": (item["decision_card"] or {}).get("evidence_versions", []),
                                 "generation_id": item["generation_id"]}
            if kind == "select":
                choice = next((direction for direction in directions if direction["id"] == selected), None)
                if choice is None:
                    raise IdeaError("invalid_request", "Select a direction from the completed current result")
                item["brief"]["goal"] = choice["question"]
                item["brief_revision"] += 1
                item["status"] = "completed"
            else:
                if selected is not None:
                    raise IdeaError("invalid_request", "Only a selection may include selected_id")
                if kind == "combine":
                    ids = request.get("selected_ids")
                    if not isinstance(ids, list) or not 2 <= len(ids) <= 3 or not all(isinstance(value, str) for value in ids) or len(set(ids)) != len(ids) or not set(ids) <= {d["id"] for d in directions}:
                        raise IdeaError("invalid_request", "Combine two or three valid current direction IDs")
                    item["brief"]["goal"] = _text(request.get("goal"), 8000)
                    item["brief_revision"] += 1
                    item["status"] = "completed"
                elif kind in ("revise", "reject"):
                    _text(feedback, 8000)
                    if kind == "revise" or request.get("goal"):
                        item["brief"]["goal"] = _text(request.get("goal", feedback), 8000)
                    item["brief_revision"] += 1
                    item.update(status="draft", error=None, phase=None)
            item["decision"] = {"kind": kind, "selected_id": selected, "selected_ids": request.get("selected_ids", []),
                                "feedback": feedback, "at": _now(), "owner": "authenticated_user", **decision_versions,
                                "answers": copy.deepcopy(answers),
                                "answered_questions": [copy.deepcopy(q) for q in item["research"]["questions"] if q["id"] in {a["question_id"] for a in answers}],
                                "scope": "research priorities only", "resulting_action": "await explicit generation or next action",
                                "reversal_conditions": "User may revise or reject this decision", "execution_authorized": False}
            item["brief"].setdefault("feedback", []).append({"text": feedback, "mode": kind, "answers": answers, "at": item["decision"]["at"]})
            item["brief"]["feedback"] = item["brief"]["feedback"][-10:]
            item["research"]["questions"] = [q for q in item["research"]["questions"] if q["id"] not in {a["question_id"] for a in answers}]
            return self._save(db, item, "human_decision", decision=item["decision"], execution_authorized=False)

    def followup(self, session_id, request):
        required = {"expected_revision", "idempotency_key", "feedback", "mode"}
        optional = {"goal", "constraints", "selected_ids", "answers"}
        if not isinstance(request, dict) or not required <= set(request) or set(request) - required - optional:
            raise IdeaError("invalid_request", "Unexpected or missing follow-up fields")
        key = _text(request["idempotency_key"], 128)
        feedback = _text(request["feedback"], 8000, empty=True)
        if request["mode"] not in ("research", "refine", "selected"):
            raise IdeaError("invalid_request", "Follow-up mode must be research, refine or selected")
        digest = _hash(request)
        with self._db() as db:
            item = self._get(db, session_id)
            prior = db.execute("SELECT request_hash,revision FROM followups WHERE session_id=? AND request_key=?", (session_id, key)).fetchone()
            if prior:
                if prior["request_hash"] != digest:
                    raise IdeaError("idempotency_conflict", "Follow-up key already identifies another response")
                return _hydrate(json.loads(db.execute("SELECT data FROM versions WHERE session_id=? AND revision=?", (session_id, prior["revision"])).fetchone()[0]))
            self._revision(item, request["expected_revision"])
            if item["status"] == "running":
                raise IdeaError("busy", "Cancel or finish the current research before changing priorities")
            answers = request.get("answers", [])
            if not isinstance(answers, list) or len(answers) > 3:
                raise IdeaError("invalid_request", "Answer at most three current questions")
            question_ids = {q["id"] for q in item["research"]["questions"]}
            for answer in answers:
                _keys(answer, ("question_id", "answer"))
                _text(answer["question_id"], 128)
                if answer["question_id"] not in question_ids:
                    raise IdeaError("stale_revision", "The answered question is not current")
                _text(answer["answer"], 8000)
            required_questions = {q["id"] for q in item["research"]["questions"] if q.get("required")}
            if not required_questions <= {answer["question_id"] for answer in answers}:
                raise IdeaError("invalid_request", "Answer the required current questions before continuing this branch")
            ids = request.get("selected_ids", [])
            directions = ((item["result"] or {}).get("ideas") or {}).get("directions", [])
            if not isinstance(ids, list) or len(ids) > 3 or not all(isinstance(value, str) for value in ids) or not set(ids) <= {d["id"] for d in directions}:
                raise IdeaError("invalid_request", "Follow-up references an unavailable direction")
            if not feedback.strip() and not answers and not ids and not request.get("goal") and not item["result"] and not item["research"]["coverage_gaps"]:
                raise IdeaError("invalid_request", "Provide feedback, an answer, a goal or a selected direction")
            record = {"kind": "followup", "mode": request["mode"], "feedback": feedback,
                      "answers": copy.deepcopy(answers), "selected_ids": ids, "at": _now(), "owner": "authenticated_user",
                      "brief_revision": item["brief_revision"], "options_revision": item["revision"],
                      "evidence_versions": (item["decision_card"] or {}).get("evidence_versions", []),
                      "answered_questions": [copy.deepcopy(q) for q in item["research"]["questions"] if q["id"] in {a["question_id"] for a in answers}],
                      "scope": "research priorities only", "resulting_action": "draft awaiting explicit generation", "execution_authorized": False}
            if "goal" in request:
                item["brief"]["goal"] = _text(request["goal"], 8000)
            elif request["mode"] == "selected" and len(ids) == 1:
                item["brief"]["goal"] = next(d["question"] for d in directions if d["id"] == ids[0])
            if "constraints" in request:
                item["brief"]["constraints"] = _text(request["constraints"], 8000, empty=True)
            item["brief"].setdefault("feedback", []).append({"text": feedback, "mode": request["mode"], "answers": answers, "selected_ids": ids, "at": record["at"]})
            item["brief"]["feedback"] = item["brief"]["feedback"][-10:]
            item["brief_revision"] += 1
            item["research"]["questions"] = [q for q in item["research"]["questions"] if q["id"] not in {a["question_id"] for a in answers}]
            item.update(status="draft", phase=None, decision=record, error=None)
            # Keep the preceding result available until an explicit generation
            # begins; the immutable history also retains its original brief.
            result = self._save(db, item, "human_followup", decision=record)
            db.execute("INSERT INTO followups VALUES(?,?,?,?)", (session_id, key, digest, result["revision"]))
            return result
