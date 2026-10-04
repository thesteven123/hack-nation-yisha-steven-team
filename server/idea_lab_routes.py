"""Native-admin Idea Lab endpoints and an in-process, bounded job controller."""
from __future__ import annotations

import asyncio
import copy
import json
import inspect
import math
import re
import sqlite3
from contextlib import asynccontextmanager

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from idea_lab import IdeaError, IdeaStore, STAGES
from idea_generation import IdeaGenerationError
from idea_evidence_cache import extraction_packet, UnavailableLiteratureEvidenceCache
from idea_source_archive import RawArchiveError, RawSourceArchive
from idea_source_view import source_packet


HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
MAX_BODY_BYTES = 2 * 1024 * 1024  # Includes JSON escaping of all bounded brief fields.


async def _await_owned(task):
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
            # A second cancellation must not cancel the wrapper around an
            # unfinished SQLite thread or durable job admission.
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _storage(operation, *args):
    return await _await_owned(asyncio.create_task(asyncio.to_thread(operation, *args)))


class IdeaController:
    """One controller per server process; no retries or model calls on reads."""

    def __init__(self, root, generate, discover=None, retrieve=None, evidence_cache=None):
        self.store = IdeaStore(root)
        self.store.interrupt_running()
        self.generate = generate
        self.discover, self.retrieve = discover, retrieve
        self.evidence_cache = evidence_cache
        self.jobs = {}
        self.cancel_requested = set()
        self.lock = asyncio.Lock()
        self.closed = False

    async def start(self, session_id, request):
        # Once accepted durably, a disconnected HTTP caller cannot leave an
        # orphaned running row between the database write and task ownership.
        admission = asyncio.create_task(self._start(session_id, request))
        return await _await_owned(admission)

    async def _start(self, session_id, request):
        async with self.lock:
            if self.closed:
                raise IdeaError("invalid_state", "Idea controller is shutting down")
            item, fresh = await _storage(self.store.begin, session_id, request, len(self.jobs) < 4,
                                         self.discover is not None and self.retrieve is not None)
            if fresh:
                task = asyncio.create_task(self._run(session_id, item["generation_id"], copy.deepcopy(item["brief"])))
                task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
                self.jobs[session_id] = task
            return item

    async def _run(self, session_id, generation_id, brief):
        previous = {stage: None for stage in STAGES}
        try:
            if self.discover is not None and self.retrieve is not None:
                await self._research(session_id, generation_id, brief)
                return
            for stage in STAGES:
                # Provider implementation owns closing its request/subprocess
                # on cancellation. No database transaction spans this await.
                generated = await self.generate(stage, copy.deepcopy(brief), copy.deepcopy(previous))
                async with self.lock:
                    if generation_id in self.cancel_requested:
                        raise asyncio.CancelledError
                    item = await _storage(self.store.save_stage, session_id, generation_id, stage, generated)
                    previous = item["result"]
        except asyncio.CancelledError:
            status = "cancelled" if generation_id in self.cancel_requested else "interrupted"
            await _storage(self.store.stop, session_id, generation_id, status)
        except Exception as exc:
            # Provider exceptions can contain prompts, output or credentials.
            # Only fixed server validation messages may reach a saved record.
            if isinstance(exc, IdeaError) and exc.code == "budget_exhausted":
                await _storage(self.store.finish_research, session_id, generation_id, "evidence_limited", exc.code)
                return
            if isinstance(exc, (IdeaError, IdeaGenerationError)):
                message = f"[{exc.code}] {exc.message}"
            else:
                message = "Generation failed; inspect provider availability and retry explicitly"
            await _storage(self.store.stop, session_id, generation_id, "failed", message)
        finally:
            self.cancel_requested.discard(generation_id)
            if self.jobs.get(session_id) is asyncio.current_task():
                self.jobs.pop(session_id, None)

    async def _research(self, session_id, generation_id, brief):
        async def event(value):
            await _storage(self.store.activity, session_id, generation_id, value)

        async def checkpoint(phase, **options):
            if generation_id in self.cancel_requested:
                raise asyncio.CancelledError
            return await _storage(lambda: self.store.work(session_id, generation_id, phase, **options))

        async def invoke(stage, item, context):
            packet = {key: value for key, value in brief.items() if not key.startswith("_")}
            packet["feedback"] = context["feedback"]
            packet["sources"] = self._model_sources(item["research"]["sources"])
            previous = {**copy.deepcopy(item["result"]), "context": context}
            reuse = None
            if stage == "literature" and self.evidence_cache is not None:
                packet, previous = extraction_packet(packet, previous)
                context = previous["context"]
                reuse = self.evidence_cache.request(session_id, generation_id, item["research"]["round"], packet, previous, event)

            async def reserve_native(_):
                # Cache lookup/native configuration has no model turn. Only a
                # miss owning its lease reaches this durable dispatch boundary.
                await reuse.before_dispatch()
                await checkpoint(stage, model=True)

            try:
                for attempt in range(2):
                    await checkpoint(stage, model=reuse is None)
                    if stage == "literature" and attempt == 0:
                        await _storage(self.store.freeze_packet, session_id, generation_id, stage, packet["sources"], context)
                    await event({"agent": "literature" if stage == "literature" else "idea",
                                 "type": "citation_repair_started" if attempt else "task_started", "task": stage,
                                 "summary": "Re-extracting exact quotations once from the same frozen source packet; dispatch requires another reserved native job."
                                 if attempt else f"Starting {stage} task for research round {item['research']['round']}"})
                    args = (stage, copy.deepcopy(packet), copy.deepcopy(previous))
                    if reuse:
                        generated = await self.generate(*args, event, cache_request=reuse, on_dispatch=reserve_native)
                    else:
                        # Retain the original three-argument adapter contract.
                        try:
                            inspect.signature(self.generate).bind(*args, event)
                            accepts_events = True
                        except TypeError:
                            accepts_events = False
                        generated = await self.generate(*args, event) if accepts_events else await self.generate(*args)
                    async with self.lock:
                        if generation_id in self.cancel_requested:
                            raise asyncio.CancelledError
                        item = await _storage(self.store.save_stage, session_id, generation_id, stage, generated, stage == "literature")
                        repair = item["research"].get("pending_repair")
                        if not repair and reuse:
                            await reuse.commit(item)
                    if not repair:
                        return item
                    await event({"agent": "literature", "type": "evidence_validation_rejected", "task": stage,
                                 "summary": "Generated evidence failed strict source-reference or exact-quotation checks; the rejected attempt was retained and no evidence was published."})
                    previous["repair"] = copy.deepcopy(repair)
                raise IdeaError("invalid_output", "Literature repair limit reached without validated evidence")
            finally:
                if reuse:
                    await _await_owned(asyncio.create_task(reuse.close()))

        item = await _storage(self.store.get, session_id)
        seed = item["research"].get("prior_context", {})
        prior_review = seed.get("review")
        requested = list((prior_review or {}).get("followup_queries", []))[:3]
        mode = "read_existing" if item["research"].get("reused_source_packets") else "retrieve_more"
        for round_index in range(item["research"]["max_rounds"]):
            item = await checkpoint("ideas" if mode == "revise_ideas" else "literature" if mode == "read_existing" else "searching", new_round=True)
            research = item["research"]
            feedback = copy.deepcopy(brief.get("feedback", [])[-3:])
            context = {"round": research["round"], "goal_revision": item["brief_revision"], "feedback": feedback,
                       "requested_queries": requested, "prior_searches": research["searches"][-6:] or seed.get("searches", []),
                       "prior_gaps": research["coverage_gaps"] or seed.get("coverage_gaps", []),
                       "prior_review": prior_review,
                       "limits": {"max_queries": min(3, max(0, research["limits"]["max_search_queries"] - research["used"]["searches"])),
                                  "max_papers": 2}}
            if mode not in ("revise_ideas", "read_existing"):
                if not context["limits"]["max_queries"]:
                    await _storage(self.store.finish_research, session_id, generation_id, "evidence_limited", "search_budget_exhausted")
                    return
                await checkpoint("searching", model=True)
                await event({"agent": "literature", "type": "task_started", "task": "discovery",
                             "summary": "Searching for original sources and missing or opposing evidence"})
                discovery = await self.discover(copy.deepcopy(brief), context, event)
                item = await _storage(self.store.discovery, session_id, generation_id, discovery)
                research = item["research"]
                if research["used"]["searches"] > research["limits"]["max_search_queries"]:
                    await _storage(self.store.finish_research, session_id, generation_id, "evidence_limited", "observed_search_budget_exceeded")
                    return
                remaining = max(0, research["limits"]["max_sources"] - research["used"]["reads"])
                if not remaining:
                    await _storage(self.store.finish_research, session_id, generation_id, "evidence_limited", "source_read_budget_exhausted")
                    return
                item = await checkpoint("reading")
                retrieval_brief = copy.deepcopy(brief)
                retrieval_brief["reading_focus"] = "\n".join((prior_review or {}).get("missing_evidence", [])[:5] + context["prior_gaps"][:5])[:6000]
                retrieval_brief["_retrieval_limits"] = {"max_sources": min(remaining, max(1, math.ceil(remaining / (research["max_rounds"] - round_index)))),
                                                        "max_packet_bytes": 100 * 1024 // research["max_rounds"]}
                reading = await self.retrieve(discovery["papers"], retrieval_brief, event)
                item = await _storage(self.store.retrieval, session_id, generation_id, reading)
                context.update(coverage_gaps=item["research"]["coverage_gaps"], search_summary=item["research"].get("search_summary", ""))
                item = await invoke("literature", item, context)
                await event({"agent": "literature", "type": "handoff", "task": "literature",
                             "summary": f"Published {len(item['result']['literature']['evidence'])} source-linked evidence cards to Idea Agent; inference remains unassessed"})
            elif mode == "read_existing":
                context.update(coverage_gaps=research["coverage_gaps"], search_summary="Reusing versioned, previously retrieved public source packets; extracting for the updated brief.")
                await event({"agent": "literature", "type": "source_context_reused", "task": "literature",
                             "summary": "Reading saved source versions against the changed priorities; previous synthesis is not treated as a new observation"})
                item = await invoke("literature", item, context)
            item = await invoke("ideas", item, context)
            item = await invoke("review", item, context)
            review = item["result"]["review"]
            read_sources = {paper.get("id") for paper in item["research"]["papers"] if paper.get("status") == "read"}
            read_evidence = {evidence["id"] for evidence in item["result"]["literature"]["evidence"] if evidence["source_id"] in read_sources}
            recommendation = next(direction for direction in item["result"]["ideas"]["directions"] if direction["id"] == review["recommendation_id"])
            grounded = bool(set(recommendation["evidence_ids"]) & read_evidence and item["research"]["searches"])
            mode = review["disposition"]
            if mode == "needs_input" or any(question["required"] for question in review["questions"]):
                mode = "needs_input"
            elif not grounded:
                mode = "retrieve_more"
            item = await _storage(self.store.round_result, session_id, generation_id, mode)
            if mode == "ready" and grounded:
                await _storage(self.store.finish_research, session_id, generation_id, "ready_for_choice", "reviewer_accepted_comparison", review["questions"])
                return
            if mode == "needs_input":
                await _storage(self.store.finish_research, session_id, generation_id, "needs_input", "required_human_decision", review["questions"])
                return
            prior_review = review
            requested = review["followup_queries"][:3]
            if mode == "retrieve_more":
                seen = {str(search.get("query", "")).strip().lower() for search in item["research"]["searches"] if isinstance(search, dict)}
                requested = [query for query in requested if query.strip().lower() not in seen]
                if not requested:
                    focus = "contradictory findings and limitations primary sources" if round_index == 0 else "independent replication original methods and full text"
                    gap = " ".join(review["missing_evidence"][:2])[:200]
                    requested = [f"{brief['goal'][:500]} {gap} {focus}"]
                await event({"agent": "idea", "type": "followup_requested", "task": "review",
                             "summary": "Reviewer requested materially different source discovery before a decision", "query": requested[0]})
        item = await _storage(self.store.get, session_id)
        await _storage(self.store.finish_research, session_id, generation_id, "evidence_limited", "bounded_rounds_exhausted",
                       ((item["result"] or {}).get("review") or {}).get("questions", []))

    @staticmethod
    def _model_sources(sources):
        """Share the byte budget fairly, including sources added by retrieval."""
        data = [source["text"].encode("utf-8") for source in sources]
        allowances, remaining = [0] * len(sources), 100 * 1024
        # Small packets keep their full text; long packets share the remaining
        # capacity equally. Source order cannot starve later retrieval results.
        for count, index in enumerate(sorted(range(len(sources)), key=lambda i: len(data[i]))):
            allowances[index] = min(len(data[index]), remaining // (len(sources) - count))
            remaining -= allowances[index]
        result = []
        for source, encoded, allowance in zip(sources, data, allowances):
            projected = copy.deepcopy(source)
            if len(encoded) > allowance:
                projected["text"] = encoded[:allowance].decode("utf-8", errors="ignore")
                marker = source.get("provenance", {}).get("gap_marker", "")
                if marker:
                    boundary = source["text"].rfind(marker, 0, len(projected["text"]) + len(marker))
                    if 0 <= boundary < len(projected["text"]) <= boundary + len(marker):
                        projected["text"] = projected["text"][:boundary]
                coverage = source.get("coverage", {})
                projected["coverage"] = {**coverage, "kind": "partial", "truncated": True, "model_packet_truncated": True,
                                         "model_projection": {"source_packet_start": 0, "source_packet_end": len(projected["text"]), "source_packet_total": len(source["text"])},
                                         "limitations": list(coverage.get("limitations", [])) + ["Model packet byte limit; saved source packet remains inspectable"]}
                if "spans" in coverage:
                    spans, characters_left = [], len(projected["text"])
                    for span in coverage["spans"]:
                        included = min(span["end"] - span["start"], max(0, characters_left))
                        if included:
                            spans.append({"start": span["start"], "end": span["start"] + included})
                        characters_left -= included + len(marker)
                        if characters_left <= 0:
                            break
                    projected["coverage"]["spans"] = spans
                    projected["coverage"]["characters_in_packet"] = sum(span["end"] - span["start"] for span in spans)
                else:
                    projected["coverage"]["characters_in_packet"] = len(projected["text"])
            result.append(projected)
        return result

    async def cancel(self, session_id, request):
        if not isinstance(request, dict) or set(request) != {"expected_revision"}:
            raise IdeaError("invalid_request", "Provide expected_revision")
        async with self.lock:
            item = await _storage(self.store.get, session_id)
            self.store._revision(item, request["expected_revision"])
            if item["status"] == "cancelled":
                return item
            if item["status"] != "running":
                raise IdeaError("invalid_state", "Only a running generation can be cancelled")
            task = self.jobs.get(session_id)
            if task is None:
                raise IdeaError("invalid_state", "Generation is not owned by this running controller")
            if item["generation_id"] not in self.cancel_requested:
                self.cancel_requested.add(item["generation_id"])
                task.cancel()
        # Do not report cancelled until generate() has unwound and closed.
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():
                raise
        # Also handles a task cancelled before its coroutine first started.
        result = await _storage(self.store.stop, session_id, item["generation_id"], "cancelled")
        self.cancel_requested.discard(item["generation_id"])
        if self.jobs.get(session_id) is task:
            self.jobs.pop(session_id, None)
        return result

    async def decision(self, session_id, request):
        async with self.lock:
            return await _storage(self.store.decision, session_id, request)

    async def followup(self, session_id, request):
        async with self.lock:
            return await _storage(self.store.followup, session_id, request)

    async def shutdown(self):
        async with self.lock:
            self.closed = True
            jobs = list(self.jobs.items())
        tasks = [task for _, task in jobs]
        for session_id, task in jobs:
            item = await _storage(self.store.get, session_id)
            if item["generation_id"] not in self.cancel_requested:
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for session_id, task in jobs:
            item = await _storage(self.store.get, session_id)
            await _storage(self.store.stop, session_id, item["generation_id"], "interrupted")
            if self.jobs.get(session_id) is task:
                self.jobs.pop(session_id, None)


def create_router(*, storage_root, authorize, generate, discover=None, retrieve=None, evidence_cache_factory=None,
                  source_library_root=None):
    """Create routes without opening storage or starting a provider."""
    controller = None
    initialization = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        yield
        if controller is not None:
            await controller.shutdown()

    router = APIRouter(prefix="/api/research/ideas", lifespan=lifespan)

    async def service():
        nonlocal controller
        async with initialization:
            if controller is None:
                def initialize():
                    try:
                        cache = evidence_cache_factory() if evidence_cache_factory else None
                    except Exception:
                        # Optional optimization storage must not block research
                        # reads or expose exception details from local paths.
                        cache = UnavailableLiteratureEvidenceCache()
                    return IdeaController(storage_root, generate, discover, retrieve, cache)
                controller = await _storage(initialize)
        return controller

    async def body(request, limit=MAX_BODY_BYTES):
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise HTTPException(415, "Use application/json", headers=HEADERS)

        async def collect():
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > limit:
                    raise HTTPException(413, "Idea request is too large", headers=HEADERS)
                data.extend(chunk)
            return data

        try:
            value = json.loads(await asyncio.wait_for(collect(), 5))
        except asyncio.TimeoutError:
            raise HTTPException(408, "Idea request body was not received", headers=HEADERS) from None
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(400, "Invalid JSON request", headers=HEADERS) from None
        if not isinstance(value, dict):
            raise HTTPException(400, "Expected a JSON object", headers=HEADERS)
        return value

    async def response(operation):
        try:
            return JSONResponse(await operation(), headers=HEADERS)
        except IdeaError as exc:
            status = {"not_found": 404, "stale_revision": 409, "idempotency_conflict": 409,
                      "invalid_state": 409, "busy": 409, "unsupported_schema": 409,
                      "ambiguous_source": 409, "source_packet_limit": 413,
                      "storage_error": 503}.get(exc.code, 400)
            raise HTTPException(status, {"code": exc.code, "message": exc.message}, headers=HEADERS) from None

    @router.get("")
    async def listing(request: Request):
        authorize(request)
        async def operation():
            return await _storage((await service()).store.list)
        return await response(operation)

    @router.post("")
    async def create(request: Request):
        authorize(request)
        value = await body(request)
        async def operation():
            return await _storage((await service()).store.create, value)
        return await response(operation)

    @router.get("/{session_id}")
    async def get(session_id: str, request: Request):
        authorize(request)
        async def operation():
            return await _storage((await service()).store.get, session_id)
        return await response(operation)

    @router.get("/{session_id}/history")
    async def history(session_id: str, request: Request):
        authorize(request)
        async def operation():
            return await _storage((await service()).store.history, session_id)
        return await response(operation)

    @router.get("/{session_id}/papers/{source_id}")
    async def paper(session_id: str, source_id: str, request: Request, source_hash: str | None = None,
                    generation_id: str | None = None, provenance_hash: str | None = None):
        authorize(request)
        async def operation():
            return await _storage(lambda: source_packet(storage_root, session_id, source_id, source_hash,
                generation_id=generation_id, provenance_hash_value=provenance_hash))
        return await response(operation)

    @router.get("/{session_id}/papers/{source_id}/raw")
    async def original_file(session_id: str, source_id: str, request: Request, source_hash: str | None = None,
                            generation_id: str | None = None, provenance_hash: str | None = None):
        authorize(request)
        async def operation():
            if not isinstance(source_hash, str) or re.fullmatch(r"[a-f0-9]{64}", source_hash) is None:
                raise IdeaError("invalid_request", "Provide the exact lowercase source packet SHA-256")
            def read_original():
                identity = {"version": 1, "session_id": session_id, "source_id": source_id, "source_hash": source_hash}
                try:
                    saved = source_packet(storage_root, session_id, source_id, source_hash,
                        generation_id=generation_id, provenance_hash_value=provenance_hash)
                except IdeaError as exc:
                    if exc.code == "ambiguous_source":
                        return {**identity, "status": "not_retained", "reason": "ambiguous_original_reference"}
                    if exc.code == "corrupt_source":
                        return {**identity, "status": "corrupt", "reason": "invalid_original_file_reference"}
                    raise
                identity.update(generation_id=saved["generation_id"], provenance_hash=saved["provenance_hash"])
                provenance = saved["source"].get("provenance", {})
                if not isinstance(provenance, dict):
                    provenance = {}
                reference = provenance.get("raw_document")
                if reference is None or isinstance(reference, dict) and reference.get("status") == "not_retained":
                    return {**identity, "status": "not_retained", "reason": "original_fetch_has_no_retained_raw_reference"}
                if not isinstance(reference, dict):
                    return {**identity, "status": "corrupt", "reason": "invalid_original_file_reference"}
                if source_library_root is None:
                    return {**identity, "status": "missing", "reason": "source_archive_not_configured"}
                if reference.get("sha256") != provenance.get("content_hash"):
                    return {**identity, "status": "corrupt", "reason": "packet_and_original_content_hash_differ"}
                try:
                    value = RawSourceArchive.open_existing(source_library_root).read(reference, include_body=True)
                except (RawArchiveError, OSError, sqlite3.Error):
                    return {**identity, "status": "corrupt", "reason": "original_file_reference_could_not_be_verified"}
                if "sha256" in value:
                    value["content_hash"] = value.pop("sha256")
                return {**value, **identity}
            return await _storage(read_original)
        return await response(operation)

    @router.get("/{session_id}/activities")
    async def activities(session_id: str, request: Request, before: int | None = None):
        authorize(request)
        async def operation():
            return await _storage((await service()).store.activities, session_id, before)
        return await response(operation)

    @router.get("/{session_id}/attempts/{attempt_id}")
    async def stage_attempt(session_id: str, attempt_id: str, request: Request):
        authorize(request)
        async def operation():
            return await _storage((await service()).store.stage_attempt, session_id, attempt_id)
        return await response(operation)

    @router.post("/{session_id}/generate")
    async def start(session_id: str, request: Request):
        authorize(request)
        value = await body(request, 2048)
        async def operation():
            return await (await service()).start(session_id, value)
        return await response(operation)

    @router.post("/{session_id}/cancel")
    async def cancel(session_id: str, request: Request):
        authorize(request)
        value = await body(request, 2048)
        async def operation():
            return await (await service()).cancel(session_id, value)
        return await response(operation)

    @router.post("/{session_id}/decision")
    async def decision(session_id: str, request: Request):
        authorize(request)
        value = await body(request, 60000)
        async def operation():
            return await (await service()).decision(session_id, value)
        return await response(operation)

    @router.post("/{session_id}/followup")
    async def followup(session_id: str, request: Request):
        authorize(request)
        value = await body(request, 220000)
        async def operation():
            return await (await service()).followup(session_id, value)
        return await response(operation)

    return router
