"""Scoped reuse of accepted Literature extractions, separate from parse caches.

This is a single-authority local service store, not a multi-user access-control
system. Callers supply its namespace; neither credentials nor model text do.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import idea_generation
import idea_lab
from idea_generation import IdeaGenerationError

VERSION = 1
LEASE_SECONDS = 45
WAIT_SECONDS = 100
MAX_JSON = 256 * 1024
PROTOCOL = "literature-cache-v0.5"
RECIPE = "accepted-exact-literature-v1;one-frozen-packet;at-most-one-citation-repair"
_LOADED = {name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
           for name, module in (("provider", idea_generation), ("validation", idea_lab))}
_CACHE_CODE_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class EvidenceCacheError(IdeaGenerationError):
    pass


class LiteratureCacheHit(Exception):
    def __init__(self, generated):
        self.generated = generated


def _json(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_JSON:
        raise EvidenceCacheError("cache_bounds", "Literature reuse record exceeded its bounded storage size")
    return encoded


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def extraction_packet(brief, previous):
    """Project the *actual* extraction input; discovery receipts stay elsewhere.

    Only operational fetch/search metadata is removed. Source text, scientific
    coverage, parser/version, goal, feedback and review retain their exact values.
    This projection is both sent to the model and frozen for exact verification.
    """
    brief, previous = copy.deepcopy(brief), copy.deepcopy(previous)
    for source in brief.get("sources", []):
        provenance = source.get("provenance")
        if isinstance(provenance, dict):
            for key in ("retrieved_at", "cache_hit", "cache_freshness_seconds", "discovery"):
                provenance.pop(key, None)
    context = previous.get("context", {})
    projected = {key: copy.deepcopy(value) for key, value in context.items()
                 if key not in ("prior_searches", "search_summary", "limits", "round")}
    projected["discovery_scope"] = {
        "receipts_recorded": bool(context.get("prior_searches") or context.get("search_summary")),
        "instruction": "Group discovery receipts are saved separately. This task extracts only these source packets; do not make corpus-wide absence or completeness claims."}
    previous["context"] = projected
    return brief, previous


async def _event(callback, value):
    if callback:
        answer = callback(value)
        if inspect.isawaitable(answer):
            await answer


async def _owned(operation, *args):
    task = asyncio.create_task(asyncio.to_thread(operation, *args))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class LiteratureEvidenceCache:
    def __init__(self, root, idea_root, *, namespace, lease_seconds=LEASE_SECONDS, clock=time.time):
        if not isinstance(namespace, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", namespace):
            raise ValueError("A service-owned namespace is required")
        self.root = Path(root).absolute()
        self.idea_path = Path(idea_root).absolute() / "ideas.sqlite3"
        self.path = self.root / "evidence-cache.sqlite3"
        self.namespace, self.clock = namespace, clock
        self.lease_seconds = max(1, min(lease_seconds, 300))
        if self.root.is_symlink() or self.path.is_symlink():
            raise EvidenceCacheError("cache_storage", "Literature reuse storage cannot be symlinked")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS entries (
                    key TEXT PRIMARY KEY, namespace TEXT NOT NULL, descriptor TEXT NOT NULL,
                    epoch INTEGER NOT NULL, owner TEXT, expires REAL, accepted TEXT, invalidated TEXT);
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, key TEXT, namespace TEXT NOT NULL, session_id TEXT NOT NULL,
                    generation_id TEXT NOT NULL, round INTEGER NOT NULL, status TEXT NOT NULL, receipt TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS requests_key ON requests(key);
                CREATE TABLE IF NOT EXISTS metadata (version INTEGER NOT NULL);
            """)
            version = db.execute("SELECT version FROM metadata").fetchone()
            if version is None:
                db.execute("INSERT INTO metadata VALUES (?)", (VERSION,))
            elif version[0] != VERSION:
                raise EvidenceCacheError("cache_version", "Unsupported Literature reuse schema")
        self.path.chmod(0o600)

    @contextmanager
    def _db(self):
        if any(path.is_symlink() for path in (self.root, self.path, Path(str(self.path) + '-wal'), Path(str(self.path) + '-shm'))):
            raise EvidenceCacheError("cache_storage", "Literature reuse storage cannot be symlinked")
        db = sqlite3.connect(self.path, timeout=4)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        finally:
            db.close()

    def request(self, session_id, generation_id, round_number, brief, previous, on_event=None):
        return LiteratureReuseRequest(self, session_id, generation_id, round_number, brief, previous, on_event)

    def _origin(self, record):
        """Require a still-present service-accepted attempt and frozen packet."""
        origin = record["origin"]
        if self.idea_path.is_symlink() or not self.idea_path.is_file():
            raise EvidenceCacheError("cache_origin_missing", "Accepted Literature origin is unavailable")
        db = sqlite3.connect(self.idea_path.as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            row = db.execute("SELECT data FROM stage_attempts WHERE id=? AND session_id=? AND generation_id=? AND stage='literature'",
                             (origin["attempt_id"], origin["session_id"], origin["generation_id"])).fetchone()
            if not row or len(row[0].encode()) > MAX_JSON:
                raise ValueError()
            attempt = json.loads(row[0])
            packet = db.execute("SELECT sources,context FROM stage_packets WHERE generation_id=? AND round=? AND stage='literature'",
                                (origin["generation_id"], origin["round"])).fetchone()
            if (attempt["status"] != "accepted" or not packet or _hash(attempt["output"]) != record["artifact_hash"]
                    or attempt["output_hash"] != record["artifact_hash"]
                    or _hash({"sources": json.loads(packet[0]), "context": json.loads(packet[1])}) != origin["packet_digest"]):
                raise ValueError()
            return attempt
        except (KeyError, ValueError, TypeError, sqlite3.Error):
            raise EvidenceCacheError("cache_origin_invalid", "Literature reuse origin no longer matches its accepted immutable receipt") from None
        finally:
            db.close()

    def acquire(self, request, descriptor):
        key = _hash(descriptor)
        with self._db() as db:
            row = db.execute("SELECT * FROM entries WHERE key=? AND namespace=?", (key, self.namespace)).fetchone()
            stale = None
            if row and row["accepted"]:
                accepted = json.loads(row["accepted"])
                try:
                    self._origin(accepted)
                    if _hash(accepted["output"]) != accepted["artifact_hash"]:
                        raise EvidenceCacheError("cache_corrupt", "Literature reuse artifact hash does not match")
                    idea_lab.validate_output("literature", accepted["output"], request.brief, {})
                except (EvidenceCacheError, idea_lab.IdeaError):
                    stale = "accepted_origin_or_exact_validation_failed"
                    db.execute("UPDATE entries SET accepted=NULL, invalidated=? WHERE key=?", (stale, key))
                else:
                    self._record(db, request, key, "join" if request.waited else "hit", {"artifact_hash": accepted["artifact_hash"]})
                    return {"status": "join" if request.waited else "hit", "key": key, "accepted": accepted}
            if row and row["owner"] and row["expires"] > self.clock():
                self._record(db, request, key, "waiting", {})
                return {"status": "waiting", "key": key}
            epoch = row["epoch"] + 1 if row else 1
            db.execute("INSERT INTO entries VALUES (?,?,?,?,?,?,NULL,?) ON CONFLICT(key) DO UPDATE SET owner=excluded.owner,epoch=excluded.epoch,expires=excluded.expires,accepted=NULL",
                       (key, self.namespace, _json(descriptor), epoch, request.id, self.clock() + self.lease_seconds, stale))
            self._record(db, request, key, "miss", {"epoch": epoch, "stale_rejected": stale, "waited": request.waited})
            return {"status": "miss", "key": key, "epoch": epoch, "stale_rejected": stale}

    def _record(self, db, request, key, status, extra):
        data = {"version": VERSION, "at": self.clock(), "status": status, **extra}
        db.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET key=excluded.key,status=excluded.status,receipt=excluded.receipt",
                   (request.id, key, self.namespace, request.session_id, request.generation_id, request.round, status, _json(data)))

    def renew(self, request):
        with self._db() as db:
            changed = db.execute("UPDATE entries SET expires=? WHERE key=? AND namespace=? AND owner=? AND epoch=? AND expires>?",
                                 (self.clock() + self.lease_seconds, request.claim["key"], self.namespace, request.id,
                                  request.claim["epoch"], self.clock())).rowcount
            return bool(changed)

    def publish(self, request, accepted_item):
        summary = accepted_item["research"]["validation_attempts"][-1]
        if (accepted_item["id"] != request.session_id or accepted_item["generation_id"] != request.generation_id
                or summary["generation_id"] != request.generation_id or summary["stage"] != "literature"
                or summary["status"] != "accepted" or summary["round"] != request.round):
            raise EvidenceCacheError("cache_stale", "Only this generation's accepted Literature attempt may publish reuse")
        origin = {"session_id": request.session_id, "generation_id": request.generation_id,
                  "round": request.round, "attempt_id": summary["id"], "packet_digest": summary["input_ref"]["packet_digest"]}
        record = {"artifact_hash": summary["output_hash"], "origin": origin, "output": request.generated["output"],
                  "provider": request.generated["provider"], "usage": request.generated["usage"]}
        attempt = self._origin(record)
        if attempt["provider"].get("resolved_model") != request.model["resolved_model"]:
            raise EvidenceCacheError("cache_model_changed", "Accepted extraction does not match the cache's resolved model")
        with self._db() as db:
            published = db.execute("UPDATE entries SET accepted=?,owner=NULL,expires=NULL WHERE key=? AND namespace=? AND owner=? AND epoch=? AND expires>?",
                                   (_json(record), request.claim["key"], self.namespace, request.id,
                                    request.claim["epoch"], self.clock())).rowcount
            self._record(db, request, request.claim["key"], "published" if published else "stale_publish_rejected",
                         {"artifact_hash": record["artifact_hash"], "origin": origin})
        return bool(published)

    def release(self, request):
        with self._db() as db:
            # Cancellation may arrive after acquire committed but before its
            # return value was delivered to the coroutine. The unique request
            # owner still permits exact cleanup without an in-memory claim.
            db.execute("UPDATE entries SET owner=NULL,expires=NULL WHERE namespace=? AND owner=?",
                       (self.namespace, request.id))
            db.execute("UPDATE requests SET status='released' WHERE id=? AND status IN ('miss','waiting')", (request.id,))


class LiteratureReuseRequest:
    def __init__(self, cache, session_id, generation_id, round_number, brief, previous, on_event):
        self.cache, self.session_id, self.generation_id, self.round = cache, session_id, generation_id, round_number
        self.brief, self.previous = copy.deepcopy(brief), copy.deepcopy(previous)
        self.on_event, self.id = on_event, uuid.uuid4().hex
        self.claim = self.model = self.generated = self.heartbeat = None
        self.waited, self.lost = False, False

    async def emit(self, kind, summary):
        await _event(self.on_event, {"agent": "literature", "task": "literature", "type": kind, "summary": summary})

    async def resolve(self, model):
        try:
            return await self._resolve(model)
        except (EvidenceCacheError, sqlite3.Error, OSError, ValueError, TypeError) as exc:
            if isinstance(exc, EvidenceCacheError) and exc.code in {"cache_wait_timeout", "cache_lease_lost"}:
                # An observed live owner or lost fence must not become a
                # duplicate native extraction through the optional-cache path.
                raise
            self.claim = {"status": "bypass", "key": (self.claim or {}).get("key"), "reason": "cache_lookup_unavailable"}
            await self.emit("literature_cache_bypassed", "文献复用检查不可用；按原预算重新提取，不宣称缓存节省。")

    async def _resolve(self, model):
        if self.claim:
            if self.claim["status"] == "bypass":
                return
            if model["resolved_model"] != self.model["resolved_model"] or model["configuration_hash"] != self.model["configuration_hash"]:
                raise EvidenceCacheError("cache_model_changed", "Literature repair configuration changed; no cached result was published")
            if self.lost:
                raise EvidenceCacheError("cache_lease_lost", "Literature extraction lease was lost; another task owns this key")
            return
        if model.get("model_resolution") != "thread_start" or not model.get("resolved_model") or not model.get("configuration_hash"):
            raise EvidenceCacheError("cache_model_unknown", "Resolved native model configuration is required for scoped Literature reuse")
        for name, module in (("provider", idea_generation), ("validation", idea_lab)):
            if hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() != _LOADED[name]:
                raise EvidenceCacheError("cache_recipe_changed", "Loaded Literature recipe differs from disk; restart the isolated service before reuse")
        if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != _CACHE_CODE_HASH:
            raise EvidenceCacheError("cache_recipe_changed", "Loaded Literature reuse code differs from disk")
        self.model = copy.deepcopy(model)
        descriptor = {"version": VERSION, "namespace": self.cache.namespace, "protocol": PROTOCOL, "recipe": RECIPE,
                      "recipe_files": {**_LOADED, "cache": _CACHE_CODE_HASH}, "model": {key: model.get(key) for key in ("backend", "resolved_model", "configuration_hash", "cli")},
                      "schema_hash": _hash(idea_lab.GENERATION_SCHEMAS["literature"]),
                      "prompt_hash": hashlib.sha256(idea_generation.stage_prompt("literature", self.brief, self.previous).encode()).hexdigest(),
                      "brief_hash": _hash({key: value for key, value in self.brief.items() if key != "sources"}),
                      "context_hash": _hash(self.previous.get("context", {})),
                      "source_dependencies": [{"id": source["id"], "packet_hash": hashlib.sha256(source["text"].encode()).hexdigest(),
                          "coverage_hash": _hash(source.get("coverage")), "provenance_hash": _hash(source.get("provenance"))}
                          for source in self.brief.get("sources", [])]}
        started = time.monotonic()
        while True:
            claim = await _owned(self.cache.acquire, self, descriptor)
            if claim["status"] != "waiting":
                self.claim = claim
                break
            if not self.waited:
                self.waited = True
                await self.emit("literature_cache_join_wait", "正在等待同一作用域的文献提取；尚未调用模型。")
            if time.monotonic() - started > WAIT_SECONDS:
                raise EvidenceCacheError("cache_wait_timeout", "Another Literature task still owns this extraction; no duplicate model turn was started")
            await asyncio.sleep(.1)
        if claim["status"] in ("hit", "join"):
            cached = claim["accepted"]
            await self.emit("literature_cache_" + claim["status"], "复用已通过精确引用核验的文献卡；本次未调用提取模型，推理有效性仍未核验。")
            raise LiteratureCacheHit({"output": copy.deepcopy(cached["output"]),
                "usage": {"available": True, "complete": True, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                          "monetary_cost": None, "cost_note": "No model turn dispatched for this extraction; origin usage is retained separately, not counted twice."},
                "provider": {"backend": "literature_artifact_cache", "model": model["resolved_model"],
                             "literature_cache": self.receipt(), "origin_provider": copy.deepcopy(cached["provider"]),
                             "origin_usage": copy.deepcopy(cached["usage"])}})
        await self.emit("literature_cache_miss", "没有可复用的已核验文献卡；取得提取租约后才预留模型任务。")
        self.heartbeat = asyncio.create_task(self._renew())

    async def _renew(self):
        while True:
            await asyncio.sleep(max(.2, self.cache.lease_seconds / 3))
            try:
                renewed = await _owned(self.cache.renew, self)
            except Exception:
                renewed = False
            if not renewed:
                self.lost = True
                return

    async def before_dispatch(self):
        if self.claim and self.claim["status"] == "bypass":
            return
        if not self.claim or self.claim["status"] != "miss" or self.lost or not await _owned(self.cache.renew, self):
            raise EvidenceCacheError("cache_lease_lost", "The extraction no longer owns its scoped lease; no new model turn was dispatched")

    def receipt(self):
        claim = self.claim or {}
        accepted = claim.get("accepted") or {}
        return {"version": VERSION, "status": claim.get("status", "miss"), "key": claim.get("key"), "scope": self.cache.namespace,
                "request_id": self.id, "waited": self.waited, "stale_rejected": claim.get("stale_rejected"),
                "reason": claim.get("reason"),
                "artifact_hash": accepted.get("artifact_hash"), "origin": accepted.get("origin"),
                "avoided_model_jobs": 1 if claim.get("status") in ("hit", "join") else 0}

    def annotate(self, generated):
        self.generated = copy.deepcopy(generated)
        result = copy.deepcopy(generated)
        result["provider"]["literature_cache"] = self.receipt()
        return result

    async def commit(self, item):
        if self.claim and self.claim["status"] == "miss":
            if not self.generated["output"].get("evidence"):
                await self.emit("literature_cache_not_reusable", "本次没有已核验文献卡；证据缺口仍保留，不缓存为缺失证据的证明。")
                return
            try:
                published = await _owned(self.cache.publish, self, item)
            except (EvidenceCacheError, sqlite3.Error, OSError, ValueError, TypeError, KeyError):
                await self.emit("literature_cache_publish_failed", "文献卡已被当前研究接受，但缓存写入失败；结果保留，不宣称本次节省调用。")
            else:
                await self.emit("literature_cache_published" if published else "literature_cache_stale_rejected",
                                "本次已接受文献卡已写入作用域缓存。" if published else "旧租约的缓存发布已拒绝；本次研究记录仍保留。")

    async def close(self):
        if self.heartbeat:
            self.heartbeat.cancel()
            try:
                await self.heartbeat
            except asyncio.CancelledError:
                pass
        try:
            await _owned(self.cache.release, self)
        except (EvidenceCacheError, sqlite3.Error, OSError):
            await self.emit("literature_cache_release_failed", "文献缓存租约清理失败，将依到期规则恢复；已保存研究结果不变。")


class UnavailableLiteratureEvidenceCache:
    """Storage-free fallback; fixed diagnostic, no inferred access namespace."""
    namespace = None

    def request(self, session_id, generation_id, round_number, brief, previous, on_event):
        return _UnavailableRequest(self, session_id, generation_id, round_number, brief, previous, on_event)


class _UnavailableRequest(LiteratureReuseRequest):
    async def resolve(self, model):
        if self.claim is None:
            self.claim = {"status": "bypass", "key": None, "reason": "cache_initialization_unavailable"}
            await self.emit("literature_cache_bypassed", "文献复用存储不可用；本次按原预算提取，已有研究结果仍可读取。")

    async def close(self):
        pass
