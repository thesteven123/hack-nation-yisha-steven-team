"""Bounded, read-only inspection and explicit offline campaign recovery.

Bundles contain one campaign, never credentials or other campaigns. Checksums
detect corruption, not a forged file whose author recomputes every checksum.
Restore is an offline administrative operation into a new empty directory.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path

from research_lab import DEFAULT_ADAPTERS, SCHEMA_VERSION, LabError, ResearchLabStore

MAX_BUNDLE_BYTES = 16 * 1024 * 1024
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_ROWS = 10000
FORMAT = "agentsdock-research-campaign/1"
HEX = re.compile(r"[0-9a-f]{64}\Z")


def _encoded(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise LabError("invalid_request", "Inspection requires finite bounded JSON") from exc


def _sha(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _require(condition, message="Campaign bundle failed integrity validation"):
    if not condition:
        raise LabError("integrity_error", message)


def _safe(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise LabError("storage_error", "Inspection paths cannot contain symlinks")
    return path


def _limit(value):
    if type(value) is not int or not 1 <= value <= 50:
        raise LabError("invalid_request", "Page limit must be between 1 and 50")
    return value


def _cursor(kind, scope, key):
    return base64.urlsafe_b64encode(_encoded({"v": 1, "kind": kind, "scope": scope, "key": key})).decode().rstrip("=")


def _uncursor(value, kind, scope):
    if value is None:
        return None
    try:
        if not isinstance(value, str) or len(value) > 1024 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ValueError()
        decoded = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
        if set(decoded) != {"v", "kind", "scope", "key"} or decoded["v"] != 1 or decoded["kind"] != kind or decoded["scope"] != scope:
            raise ValueError()
        key = decoded["key"]
        if kind == "history":
            if type(key) is not int or not 1 <= key <= 2147483647:
                raise ValueError()
        elif (not isinstance(key, list) or len(key) != 2
              or any(not isinstance(x, str) or not x or len(x) > 128 for x in key)):
            raise ValueError()
        return key
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise LabError("invalid_request", "Invalid or differently scoped continuation cursor") from None


def _page(rows, limit, transform, cursor_key, kind, scope):
    items, keys, size = [], [], 0
    for row in rows[:limit]:
        item = transform(row)
        cost = len(_encoded(item))
        if size + cost > MAX_PAGE_BYTES:
            if not items:
                raise LabError("too_large", "One inspection entry exceeds the page byte limit")
            break
        items.append(item)
        keys.append(cursor_key(row))
        size += cost
    more = len(rows) > len(items)
    return {"items": items, "has_more": more,
            "next_cursor": _cursor(kind, scope, keys[-1]) if more else None,
            "consistency": "live_keyset; concurrent updates may move list entries" if kind == "list" else "immutable_revision_keyset"}


class ResearchLabInspector:
    def __init__(self, root):
        self.root = Path(root).absolute()
        self.path = self.root / "lab.sqlite3"

    @contextmanager
    def _db(self):
        _safe(self.path)
        if not self.path.is_file():
            raise LabError("not_found", "Research Lab store does not exist")
        db = None
        try:
            db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise LabError("unsupported_schema", "Unsupported Research Lab schema")
            yield db
        except sqlite3.Error as exc:
            raise LabError("storage_error", "Research inspection could not read a consistent snapshot") from exc
        finally:
            if db is not None:
                db.close()

    def list(self, before=None, limit=50):
        limit, key = _limit(limit), _uncursor(before, "list", None)
        sql = "SELECT id,json_extract(payload,'$.updated_at') AS updated_at,json_set(payload,'$.rounds',json('[]'),'$.claims',json('[]'),'$.decisions',json('[]'),'$.comparisons',json('[]'),'$.events',json('[]'),'$.current_plan',NULL) AS compact FROM campaigns"
        args = []
        if key is not None:
            sql += " WHERE (json_extract(payload,'$.updated_at'),id) < (?,?)"
            args.extend(key)
        sql += " ORDER BY updated_at DESC,id DESC LIMIT ?"
        args.append(limit + 1)
        with self._db() as db:
            rows = db.execute(sql, args).fetchall()
            return _page(rows, limit, lambda r: json.loads(r["compact"]),
                         lambda r: [r["updated_at"], r["id"]], "list", None)

    def history(self, campaign_id, before=None, limit=50):
        limit, key = _limit(limit), _uncursor(before, "history", campaign_id)
        with self._db() as db:
            if not isinstance(campaign_id, str) or not 1 <= len(campaign_id) <= 128:
                raise LabError("invalid_request", "A bounded campaign identity is required")
            if not db.execute("SELECT 1 FROM campaigns WHERE id=?", (campaign_id,)).fetchone():
                raise LabError("not_found", "Research campaign was not found")
            sql = "SELECT revision,at,event,json_extract(payload,'$.brief') AS brief,json_extract(payload,'$.status') AS status,json_extract(payload,'$.events[#-1].artifact') AS event_artifact FROM versions WHERE campaign_id=?"
            args = [campaign_id]
            if key is not None:
                sql += " AND revision < ?"
                args.append(key)
            rows = db.execute(sql + " ORDER BY revision DESC LIMIT ?", [*args, limit + 1]).fetchall()
            return _page(rows, limit, lambda r: {"revision": r["revision"], "at": r["at"], "event": r["event"],
                         "brief": json.loads(r["brief"]), "status": r["status"],
                         **({"event_artifact": r["event_artifact"]} if r["event_artifact"] else {})}, lambda r: r["revision"], "history", campaign_id)

    @staticmethod
    def _campaign(db, campaign_id):
        if not isinstance(campaign_id, str) or not 1 <= len(campaign_id) <= 128:
            raise LabError("invalid_request", "A bounded campaign identity is required")
        row = db.execute("SELECT payload FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if row is None:
            raise LabError("not_found", "Research campaign was not found")
        return json.loads(row[0])

    def export_campaign(self, campaign_id):
        with self._db() as db:
            data = {"campaign": self._campaign(db, campaign_id)}
            size = len(_encoded(data))

            def collect(name, sql, transform):
                nonlocal size
                result = []
                for row in db.execute(sql, (campaign_id,)):
                    record = transform(row)
                    size += len(_encoded(record))
                    if len(result) >= MAX_ROWS or size > MAX_BUNDLE_BYTES:
                        raise LabError("too_large", "Campaign export exceeds its complete-bundle limit; no truncated backup was produced")
                    result.append(record)
                data[name] = result

            collect("versions", "SELECT * FROM versions WHERE campaign_id=? ORDER BY revision", lambda r: {
                "revision": r["revision"], "at": r["at"], "event": r["event"], "payload": json.loads(r["payload"])})
            collect("events", "SELECT * FROM events WHERE campaign_id=? ORDER BY seq", lambda r: {
                **{k: r[k] for k in ("seq", "id", "revision", "type", "at")}, "data": json.loads(r["data"])})
            collect("mutations", "SELECT * FROM mutations WHERE json_extract(response,'$.id')=? ORDER BY scope,key", lambda r: {
                "scope": r["scope"], "key": r["key"], "request_hash": r["request_hash"], "response": json.loads(r["response"])})
            collect("artifacts", "SELECT a.digest,a.data FROM artifacts a JOIN artifact_links l ON a.digest=l.digest WHERE l.campaign_id=? ORDER BY a.digest", lambda r: {
                "sha256": r["digest"], "encoding": "base64", "data": base64.b64encode(bytes(r["data"])).decode()})
        bundle = {"format": FORMAT, "schema_version": SCHEMA_VERSION, "campaign_id": campaign_id,
                  "snapshot_kind": "sqlite_read_transaction", "data": data,
                  "manifest": {"table_hashes": {k: _sha(v) for k, v in data.items()},
                               "authentication": "content checksums only; not a signature or scientific verification"}}
        bundle["bundle_sha256"] = _sha(bundle)
        verify_bundle(bundle)
        return bundle

    @staticmethod
    def protocol(identifier):
        if not isinstance(identifier, str) or identifier not in PROTOCOL_SUMMARIES:
            raise LabError("not_found", "No local implementation summary exists for that protocol")
        result = copy.deepcopy(PROTOCOL_SUMMARIES[identifier])
        result.update(id=identifier, summary_version="implementation/1", is_full_source=False,
                      scope="Implementation guidance for bounded local adapters; not the complete design protocol")
        result["summary_sha256"] = _sha(result)
        return result


def verify_bundle(bundle):
    """Verify one campaign without creating a store, executing work or fetching URLs."""
    if len(_encoded(bundle)) > MAX_BUNDLE_BYTES:
        raise LabError("too_large", "Campaign bundle exceeds its verification byte limit")
    try:
        _require(set(bundle) == {"format", "schema_version", "campaign_id", "snapshot_kind", "data", "manifest", "bundle_sha256"})
        _require(bundle["format"] == FORMAT and bundle["schema_version"] == SCHEMA_VERSION)
        _require(bundle["snapshot_kind"] == "sqlite_read_transaction")
        _require(bundle["bundle_sha256"] == _sha({k: v for k, v in bundle.items() if k != "bundle_sha256"}))
        data, cid = bundle["data"], bundle["campaign_id"]
        _require(set(data) == {"campaign", "versions", "events", "mutations", "artifacts"})
        _require(bundle["manifest"]["table_hashes"] == {k: _sha(v) for k, v in data.items()})
        for key in ("versions", "events", "mutations", "artifacts"):
            _require(isinstance(data[key], list) and len(data[key]) <= MAX_ROWS)
        campaign = data["campaign"]
        _require(campaign["id"] == cid and isinstance(cid, str) and re.fullmatch(r"campaign_[0-9a-f]{32}", cid) is not None)
        versions = {v["revision"]: v for v in data["versions"]}
        revision = campaign["revision"]
        _require(type(revision) is int and 1 <= revision <= MAX_ROWS)
        _require(len(versions) == len(data["versions"]) == revision and set(versions) == set(range(1, revision + 1)))
        _require(versions[revision]["payload"] == campaign, "Current campaign does not equal its final immutable revision")
        artifacts = {}
        for entry in data["artifacts"]:
            _require(set(entry) == {"sha256", "encoding", "data"} and entry["encoding"] == "base64")
            raw = base64.b64decode(entry["data"], validate=True)
            digest = entry["sha256"]
            _require(isinstance(digest, str) and HEX.fullmatch(digest) and digest not in artifacts)
            _require(hashlib.sha256(raw).hexdigest() == digest, "Artifact bytes do not match their immutable digest")
            value = json.loads(raw)
            _require(_encoded(value) == raw, "Artifact is not canonical finite JSON")
            artifacts[digest] = value

        def references(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in ("provenance", "original_provenance"):
                        continue  # User/source metadata is not the lab artifact-reference schema.
                    if key in ("decision_artifact", "hypothesis_set_artifact", "previous_hypothesis_set_artifact", "previous_artifact") and child is None:
                        continue  # Policy selections and optional hypothesis records may have no prior artifact.
                    if key in ("input_artifact", "derived_from_input_artifact", "round_artifact", "spec_artifact", "observation_artifact", "dispatch_artifact", "decision_artifact", "artifact",
                               "hypothesis_set_artifact", "previous_hypothesis_set_artifact", "previous_artifact"):
                        _require(child in artifacts, "A required artifact is missing from the campaign bundle")
                    elif key == "input_refs":
                        _require(isinstance(child, list) and all(x in artifacts for x in child), "An analysis input reference is missing")
                    references(child)
            elif isinstance(value, list):
                for child in value:
                    references(child)

        fixed_budget = {k: campaign["budget"][k] for k in ("max_actions", "max_rounds")}
        for number, version in versions.items():
            item = version["payload"]
            _require(item["id"] == cid and item["revision"] == number and item["schema_version"] == SCHEMA_VERSION)
            adapter = DEFAULT_ADAPTERS.get(item["adapter"]["id"])
            _require(adapter is not None and item["adapter"]["execution"] == "local_deterministic", "Restore supports local deterministic adapters only")
            _require(item["brief"]["authorized_actions"] == adapter.manifest["methods"], "Bundle cannot expand local execution permissions")
            budget = item["budget"]
            _require({k: budget[k] for k in fixed_budget} == fixed_budget)
            _require(type(budget["max_actions"]) is int and 1 <= budget["max_actions"] <= 12)
            _require(type(budget["max_rounds"]) is int and 1 <= budget["max_rounds"] <= 6)
            _require(budget["reserved_actions"] == 0 and budget["used_actions"] == len(item["rounds"]) <= budget["max_rounds"])
            _require(budget["remaining_actions"] == budget["max_actions"] - budget["used_actions"] >= 0)
            _require(len({r["run"]["id"] for r in item["rounds"]}) == len(item["rounds"]))
            for r in item["rounds"]:
                _require(r["run"]["usage"]["actions"] == 1 and r["run"]["is_independent_replicate"] is False)
                _require(all(r["run"]["usage"][k] == 0 for k in ("external_requests", "model_tokens", "monetary_cost")))
            references(item)
            if item.get("branch_set"):
                from research_branches import verify_persisted, BranchError
                try:
                    verify_persisted(item, lambda digest: artifacts[digest])
                except BranchError as exc:
                    raise LabError("integrity_error", exc.message) from None
        for value in artifacts.values():
            references(value)
        events = data["events"]
        _require(len(events) == revision and len({e["id"] for e in events}) == revision)
        _require([e["revision"] for e in events] == list(range(1, revision + 1)))
        _require(all(type(e["seq"]) is int and e["seq"] > 0 for e in events))
        _require([e["seq"] for e in events] == sorted({e["seq"] for e in events}))
        for e in events:
            v = versions[e["revision"]]
            _require(e["type"] == v["event"] and e["at"] == v["at"])
            saved = v["payload"]["events"][-1]
            _require(e == {k: value for k, value in saved.items() if k != "artifact"})
            if "artifact" in saved:
                _require(artifacts.get(saved["artifact"]) == e, "Versioned event artifact differs from its immutable ledger record")
        mutations = data["mutations"]
        _require(len(mutations) == revision and len({(m["scope"], m["key"]) for m in mutations}) == revision)
        _require(sum(m["scope"] == "create" for m in mutations) == 1)
        _require({m["response"]["revision"] for m in mutations} == set(versions))
        for m in mutations:
            _require(m["scope"] in ("create", cid) and isinstance(m["key"], str) and 1 <= len(m["key"].encode()) <= 128)
            _require(isinstance(m["request_hash"], str) and HEX.fullmatch(m["request_hash"]))
            response = m["response"]
            _require((m["scope"] == "create") == (response["revision"] == 1))
            _require(response == versions[response["revision"]]["payload"], "Replay response differs from its committed version")
        return {"verified": True, "campaign_id": cid, "revision": revision, "artifacts": len(artifacts),
                "runs": len(campaign["rounds"]), "bundle_sha256": bundle["bundle_sha256"],
                "authentication": "checksums only", "execution_performed": False}
    except LabError:
        raise
    except (KeyError, TypeError, ValueError, IndexError, AttributeError, RecursionError):
        raise LabError("integrity_error", "Campaign bundle has an invalid structure or reference") from None


def restore_campaign(bundle, destination):
    """Restore into an explicit new empty root. No HTTP route should expose this."""
    report = verify_bundle(bundle)
    destination = _safe(destination)
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise LabError("invalid_request", "Restore requires a new empty isolated directory")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    _safe(destination)
    data, cid = bundle["data"], bundle["campaign_id"]
    with tempfile.TemporaryDirectory(prefix=".campaign-restore-", dir=destination) as staging:
        # The temporary directory resolves inside the verified isolated target.
        stage = _safe(staging)
        _require(stage.parent == destination, "Restore staging escaped the isolated destination")
        store = ResearchLabStore(stage)
        with closing(sqlite3.connect(store.path)) as db, db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO campaigns VALUES (?,?)", (cid, _encoded(data["campaign"]).decode()))
            for a in data["artifacts"]:
                db.execute("INSERT INTO artifacts VALUES (?,?)", (a["sha256"], base64.b64decode(a["data"])))
                db.execute("INSERT INTO artifact_links VALUES (?,?)", (cid, a["sha256"]))
            for v in data["versions"]:
                db.execute("INSERT INTO versions VALUES (?,?,?,?,?)", (cid, v["revision"], v["at"], v["event"], _encoded(v["payload"]).decode()))
            for e in data["events"]:
                db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?)", (e["seq"], e["id"], cid, e["revision"], e["type"], e["at"], _encoded(e["data"]).decode()))
            for m in data["mutations"]:
                db.execute("INSERT INTO mutations VALUES (?,?,?,?)", (m["scope"], m["key"], m["request_hash"], _encoded(m["response"]).decode()))
        restored = ResearchLabInspector(stage).export_campaign(cid)
        _require(restored["bundle_sha256"] == bundle["bundle_sha256"], "Restored snapshot differs from the verified export")
        # A hard-link publication cannot overwrite a file created concurrently.
        os.link(store.path, destination / "lab.sqlite3")
        os.chmod(destination / "lab.sqlite3", 0o600)
    return {**report, "restored": True, "replay_not_executed": True}


# Implementation summaries below are deliberately authored summaries, not copies
# of full source protocols. Block IDs and hashes were read from the v0.5 Pages.
PROTOCOL_SUMMARIES = {
    "literature-cache-v0.5": {
        "title": "Literature coverage and scoped reuse",
        "content": "Use the current question, requested coverage and resource ceiling to inspect the shared library before obtaining missing sources. Preserve source identity, content version, parser configuration and exact locations. Abstract-only access, omitted figures or appendices, parser gaps and bounded subsets remain visible; inaccessible evidence is not evidence of absence. A source parse may be reused only for matching content, parser and coverage. Reusing a research analysis also requires matching access scope, input/dependency versions, recipe, model configuration, schema and question-dependent brief. Similarity retrieves candidates but does not establish equivalent answers. Corrections trigger dependency review while preserving old artifacts. Cache retrieval, retry and same-data analysis are not independent replication. These local source adapters consume supplied text; they do not claim live retrieval or cross-campaign revalidation merely by attaching this summary.",
        "source": {"page_id": "page_1775310b16d08191906760157d513776", "url": "https://chatgpt.com/space/page_1775310b16d08191906760157d513776", "sequence": 1, "updated_at": "2026-10-03T19:49:12.737250Z", "blocks": [
            {"id": "aef9775f-abae-414e-b871-bafb72b0606b", "hash": "20d20699214560bd934d39086cbdd749071fe6564aae7e7769dfdbd4f5291fd9"},
            {"id": "56149a61-18ff-4a16-8e3b-5df955e6c6a0", "hash": "5e517bbc61eefc2a4b2dab4ebe2e0112100894f2574e47b9cc587360aab5cbe7"},
            {"id": "295c7103-68a6-4753-a081-92715da804b4", "hash": "a929110e4cd8b34648304ea121be9624445c69b7f2dbb19abac64437a6bd4fe8"}]}},
    "planning-v0.5": {
        "title": "Planning and frozen local actions",
        "content": "Start with the current question, known evidence, applicable adapter and remaining budget. Keep candidate alternatives and record whether policy or the person selected one. Before execution freeze the exact input digest, question revision, method version, procedure, QC, interpretation rules and stopping/resource limits. A plan or preferred idea grants no extra execution authority. Later changes to criteria are new exploratory revisions; they do not rewrite prior results. The supplied-source and paired-numeric adapters provide bounded local observations only; a working wrapper does not establish scientific validity.",
        "source": {"page_id": "page_d28993a86bbc8191ab71c7b2d4ea397a", "url": "https://chatgpt.com/space/page_d28993a86bbc8191ab71c7b2d4ea397a", "sequence": 2, "updated_at": "2026-10-03T19:49:16.893661Z", "blocks": [
            {"id": "154cc3a6-f215-47ec-beab-331df222f23c", "hash": "3470d5b7f061585327cf5bb8c97a1ae6f40ee8f78ae285fee8c0327b45bc59bc"},
            {"id": "47b9264b-1f85-444e-885c-0011a3e24ca5", "hash": "70981418f9ea6436961810eed455eaa116387b90883138aecc4c42103a2b6639"}]}},
    "records-v0.5": {
        "title": "Records, provenance and bounded claims",
        "content": "Preserve stable identities, immutable revisions, timestamps, producing actions and fixed input references. Distinguish source reports from this service's observations. Keep raw artifacts and failed attempts so an output can be reconstructed. Record source coverage, conditions and missing material. Link each claim to the exact input, observation and analysis. Source occurrence/support and inference validity are separate assessments; neither execution completion nor a checksum is a universal verified flag. Input corrections retain history and trigger explicit revalidation rather than silently replacing evidence.",
        "source": {"page_id": "page_0481c0d21b8c8191a1d00519ce96d9b0", "url": "https://chatgpt.com/space/page_0481c0d21b8c8191a1d00519ce96d9b0", "sequence": 2, "updated_at": "2026-10-03T19:49:04.847643Z", "blocks": [
            {"id": "6061e346-080f-4eef-82c2-d216f574a11a", "hash": "7f68bd938c8983cf2c11688c81d51ee4027b100633a4e70b4244fc3deaaffe2d"},
            {"id": "e078a290-5374-4a0a-b326-c5ccdc43df31", "hash": "cc8f404396bfd499809ce4c5763b1e3d344172f74fa72c539db35f7647c49db5"}]}},
    "execution-v0.5": {
        "title": "Local execution and recovery boundaries",
        "content": "Execute only an authorized frozen action after checking and reserving its resource ceiling. Preserve action, run, attempt and idempotency identities with artifacts and measured usage. Only the authoritative attempt may publish. This implementation computes bounded local functions within one SQLite transaction: a precommit failure rolls back and a committed request replays its saved response. This is not an external exactly-once guarantee. External submission, unknown outcomes, reconciliation and cancellation acknowledgement require a different executor and are unsupported here. Do not treat timeout as scientific failure or independently rerun external work.",
        "source": {"page_id": "page_7110b10c78948191a844b4548fdce56c", "url": "https://chatgpt.com/space/page_7110b10c78948191a844b4548fdce56c", "sequence": 1, "updated_at": "2026-10-03T19:49:20.106012Z", "blocks": [
            {"id": "77c08e1c-8f5f-4b55-978c-57212ba780f4", "hash": "de0dff89d35e14af6eb469efad4e03ad20c5dcb07385318bc957a47729f5f59c"},
            {"id": "b55e16f2-b98c-4c7b-829c-81a11f157532", "hash": "77c6fa13fa851c6cdf5f679227887b8021df1bb389c70943c3f8464ccff53da3"}]}},
    "analysis-review-v0.5": {
        "title": "QC, analysis and review limits",
        "content": "Check the frozen action and actual observation before interpreting it. Execution failure is diagnostic, not negative scientific evidence. Completed work failing QC remains available for inspection but cannot support its intended comparison. Apply the adapter's predeclared criterion and retain inconclusive outcomes. The analyst records findings, limits and complete input references; review assesses the precise tested proposition and decides a bounded next action. Source support is separate from inference validity. Same-data sensitivity is not independent confirmation. Post-observation revisions preserve old criteria, and corrections mark affected claims as needing revalidation. Separate deterministic functions do not establish independent reviewer errors.",
        "source": {"page_id": "page_479ca901a2c08191bafdb79ee0a6f706", "url": "https://chatgpt.com/space/page_479ca901a2c08191bafdb79ee0a6f706", "sequence": 1, "updated_at": "2026-10-03T19:49:23.901311Z", "blocks": [
            {"id": "7bdc886f-e953-4a38-b07e-7487ce437819", "hash": "2fcfe405ff41e88d8b318f0d0d77af7ac5c0d0c794b12aeb701f6d062a531800"},
            {"id": "bf86eeef-23d3-4adc-80fd-a7296ac82686", "hash": "a05b54cd01317eba892b7a45356207eff9896c337fce194696505cda4b84b52e"}]}}
}
