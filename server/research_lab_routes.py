"""Native administration transport for bounded, service-owned research campaigns.

Idea imports resolve exact frozen evidence packets. Client-provided source text
cannot become an authenticated Idea source just by adding an origin label.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sqlite3
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from idea_lab import IdeaError, IdeaStore
from research_lab import LabError, ResearchLabStore
from research_lab_inspection import ResearchLabInspector

HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_BRANCH_BODY_BYTES = 384 * 1024


class IdeaImport:
    """Read authoritative choices and exact evidence; never generate or retrieve."""
    def __init__(self, store):
        self.store = store

    def seed(self, session_id):
        item = self.store.get(session_id)
        decision = item.get("decision") or {}
        if (item["status"] != "completed" or decision.get("kind") not in ("select", "combine")
                or decision.get("generation_id") != item["generation_id"]):
            raise LabError("stale_origin", "Save a current Idea direction selection before creating a research task")
        selected = [decision["selected_id"]] if decision["kind"] == "select" else decision["selected_ids"]
        directions = ((item.get("result") or {}).get("ideas") or {}).get("directions", [])
        choices = [d for d in directions if d["id"] in selected]
        if len(choices) != len(selected):
            raise LabError("stale_origin", "The chosen Idea directions no longer match the saved result")
        evidence_ids = {e for d in choices for e in d.get("evidence_ids", [])}
        evidence = [e for e in ((item.get("result") or {}).get("literature") or {}).get("evidence", [])
                    if e["id"] in evidence_ids]
        references = {}
        for entry in evidence:
            references.setdefault((entry["source_id"], entry.get("source_hash")), []).append(entry["id"])
        eligible = [pair for pair in references if pair[1]]
        included = set(eligible[:5])
        omitted = [pair for pair in references if pair not in included]
        import_coverage = {"referenced_sources": len(references), "imported_sources": len(included),
            "omitted_sources": len(omitted), "omitted_source_ids": list(dict.fromkeys(pair[0] for pair in omitted)),
            "omitted_evidence_ids": [identity for pair in omitted for identity in references[pair]],
            "selection_policy": "first_seen_bounded_subset", "complete": not omitted,
            "scope": "selected_direction_source_versions_only",
            "limitation": "A complete import includes all referenced source versions, not complete scientific evidence. A bounded subset may omit counterevidence; selection is not a relevance ranking."}
        coverage_metadata = {key: import_coverage[key] for key in
                             ("referenced_sources", "imported_sources", "omitted_sources", "selection_policy", "complete", "scope")}
        coverage_metadata["details_hash"] = hashlib.sha256(json.dumps(import_coverage, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        sources, seen, quote = [], set(), ""
        for entry in evidence:
            digest = entry.get("source_hash")
            if not digest:
                continue  # Legacy unversioned evidence cannot authenticate an import.
            pair = (entry["source_id"], digest)
            if pair in seen or pair not in included:
                continue
            packet = self.store.paper(session_id, pair[0], digest)
            source = packet["source"]
            actual = hashlib.sha256(source["text"].encode("utf-8")).hexdigest()
            if actual != digest or entry["quote"] not in source["text"]:
                raise LabError("integrity_error", "The frozen Idea evidence no longer matches its source")
            # A source ID can have multiple role projections. The imported ID
            # includes its full digest so these versions cannot be conflated.
            imported_id = "idea_" + hashlib.sha256(json.dumps(pair).encode()).hexdigest()
            coverage = source.get("coverage", {})
            limitations = (coverage.get("limitations", []) if isinstance(coverage, dict) else [])
            full = coverage == "full_text" or (isinstance(coverage, dict) and coverage.get("kind") == "full_text")
            missing = [str(x).encode("utf-8")[:256].decode("utf-8", errors="ignore") for x in limitations if str(x).strip()][:20]
            full = full and not missing
            if not full and not missing:
                missing = ["Only the exact frozen Idea text packet is available; original completeness is unverified."]
            sources.append({"id": imported_id, "title": source["title"], "uri": source["uri"],
                "text": source["text"], "coverage": "full_text" if full else "excerpt", "missing_sections": missing,
                "provenance": {"kind": "idea_frozen_packet", "session_id": session_id,
                    "generation_id": item["generation_id"], "decision_revision": item["revision"],
                    "decision_at": decision["at"], "source_id": source["id"], "source_hash": actual,
                    "evidence_id": entry["id"], "span": entry.get("span"),
                    "gap_marker": source.get("provenance", {}).get("gap_marker"),
                    "import_coverage": coverage_metadata,
                    "coverage": coverage, "original_provenance": source.get("provenance", {})}})
            seen.add(pair)
            if not quote:
                quote = entry["quote"]
        if not sources:
            raise LabError("needs_evidence", "This direction has no exact frozen evidence to import; add evidence or start a separate task")
        if sum(len(s["text"].encode("utf-8")) for s in sources) > 150 * 1024:
            raise LabError("invalid_request", "The selected frozen evidence exceeds the research input limit")
        # A concurrent choice/revision must not produce a mixed-version seed.
        if self.store.get(session_id)["revision"] != item["revision"]:
            raise LabError("stale_origin", "The Idea choice changed while importing; inspect it again")
        brief = item["brief"]
        return {"origin": {"kind": "idea", "session_id": session_id,
                           "generation_id": item["generation_id"], "selected_ids": selected,
                           "decision_revision": item["revision"]},
                "brief": {"goal": brief["goal"], "hypothesis": brief.get("hypothesis", ""),
                          "success_criteria": brief.get("success_criteria") or "Inspect the selected source and its limitations before planning further work.",
                          "constraints": self.coverage_notice(brief.get("constraints", ""), import_coverage)},
                "import_coverage": import_coverage,
                "inputs": {"quote": quote, "sources": sources}}

    @staticmethod
    def coverage_notice(constraints, coverage):
        if not coverage["omitted_sources"]:
            return constraints
        note = (f"[Idea import coverage] Only {coverage['imported_sources']}/{coverage['referenced_sources']} referenced source versions are imported. "
                "The bounded first-seen subset is not a relevance ranking and may omit counterevidence; it is not complete evidence for the selected direction.")
        if not isinstance(constraints, str):
            raise LabError("invalid_request", "Research constraints must be text")
        result = constraints if note in constraints else constraints + ("\n" if constraints else "") + note
        if len(result.encode("utf-8")) > 8000:
            raise LabError("invalid_request", "Shorten the research constraints to retain the mandatory source-import coverage notice")
        return result

    def resolve(self, request):
        origin = request["origin"]
        if (not isinstance(origin, dict) or set(origin) != {"kind", "session_id", "generation_id", "selected_ids", "decision_revision"}
                or origin.get("kind") != "idea" or not isinstance(origin.get("session_id"), str)
                or type(origin.get("decision_revision")) is not int or origin["decision_revision"] < 1):
            raise LabError("invalid_request", "Provide a complete selected Idea origin")
        seed = self.seed(origin["session_id"])
        if origin != seed["origin"]:
            raise LabError("stale_origin", "The Idea origin does not match the current saved choice")
        result = copy.deepcopy(request)
        if request["adapter_id"] == "source_evidence":
            inputs = request.get("inputs")
            supplied = inputs.get("sources") if isinstance(inputs, dict) else None
            if not isinstance(supplied, list) or len(supplied) != len(seed["inputs"]["sources"]):
                raise LabError("stale_origin", "Import the exact frozen sources for this Idea choice")
            # Provenance is always regenerated from the source store. All other
            # supplied fields must match; editable quote/goal remain user input.
            for actual, expected in zip(supplied, seed["inputs"]["sources"]):
                if (not isinstance(actual, dict)
                        or {k: v for k, v in actual.items() if k != "provenance"}
                        != {k: v for k, v in expected.items() if k != "provenance"}):
                    raise LabError("stale_origin", "Source text or coverage differs from the frozen Idea evidence")
            result["inputs"]["sources"] = seed["inputs"]["sources"]
            if not isinstance(result.get("brief"), dict):
                raise LabError("invalid_request", "Provide a research brief")
            result["brief"]["constraints"] = self.coverage_notice(result["brief"].get("constraints"), seed["import_coverage"])
        return result


def create_research_lab_router(*, storage_root, authorize, idea_root=None, dependency_factory=None):
    router = APIRouter(prefix="/api/research/lab")
    cached_store = cached_ideas = cached_inspector = None
    lock = threading.Lock()
    active = 0

    def store():
        nonlocal cached_store
        with lock:
            if cached_store is None:
                cached_store = ResearchLabStore(storage_root)
            return cached_store

    def imports():
        nonlocal cached_ideas
        if idea_root is None:
            raise LabError("unsupported_origin", "Idea import is unavailable on this server")
        with lock:
            if cached_ideas is None:
                cached_ideas = IdeaImport(IdeaStore(idea_root))
            return cached_ideas

    def inspector():
        nonlocal cached_inspector
        store()  # Only an authorized request may initialize the empty workspace.
        with lock:
            if cached_inspector is None:
                cached_inspector = ResearchLabInspector(storage_root)
            return cached_inspector

    def current(campaign_id):
        item = store().get(campaign_id)
        return dependency_factory().decorate(item) if dependency_factory else item

    def changed(operation):
        result = operation()
        if dependency_factory:
            dependencies = dependency_factory()
            dependencies.recover_lab_corrections()
            dependencies.register_campaign(result["id"])
            dependencies.drain()
        # Preserve the immutable core response before its bounded presentation.
        # A subsequent GET supplies current dependency status, not past state.
        return result

    async def worker(operation):
        nonlocal active
        if active >= 4:
            raise HTTPException(503, "Research workspace is busy; retry shortly", headers={**HEADERS, "Retry-After": "2"})
        active += 1
        task = asyncio.create_task(asyncio.to_thread(operation))

        def finished(completed):
            nonlocal active
            active -= 1
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)
        try:
            return await asyncio.shield(task)
        except (LabError, IdeaError) as exc:
            status = {"not_found": 404, "stale_revision": 409, "stale_origin": 409, "revision_conflict": 409,
                      "environment_changed": 409, "invalid_state": 409, "deleted": 409,
                      "branch_not_found": 404, "branch_required": 409, "branch_scope_changed": 409,
                      "branch_revision_conflict": 409, "branch_blocked": 409,
                      "dependency_stale": 409, "dependency_pending": 409, "dependency_limit": 413,
                      "too_large": 413, "response_too_large": 413,
                      "idempotency_conflict": 409, "unsupported_schema": 409,
                      "integrity_error": 503, "storage_error": 503, "busy": 409}.get(exc.code, 400)
            raise HTTPException(status, {"code": exc.code, "message": exc.message}, headers=HEADERS) from None
        except (sqlite3.Error, OSError):
            raise HTTPException(503, "Research storage is unavailable; retry with the same request identity", headers=HEADERS) from None

    async def body(request, limit=MAX_BODY_BYTES):
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise HTTPException(415, "Use application/json", headers=HEADERS)

        async def collect():
            value = bytearray()
            async for chunk in request.stream():
                if len(value) + len(chunk) > limit:
                    raise HTTPException(413, "Research request is too large", headers=HEADERS)
                value.extend(chunk)
            return value
        try:
            value = json.loads(await asyncio.wait_for(collect(), timeout=5))
        except asyncio.TimeoutError:
            raise HTTPException(408, "Research request body was not received", headers=HEADERS) from None
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(400, "Invalid JSON request", headers=HEADERS) from None
        if not isinstance(value, dict):
            raise HTTPException(400, "Expected a JSON object", headers=HEADERS)
        return value

    async def reply(operation):
        return JSONResponse(await worker(operation), headers=HEADERS)

    async def campaign_reply(operation):
        # Apply the same bounded projection to both fresh results and old raw
        # durable replay receipts. Full immutable bytes remain in core export.
        return await reply(lambda: ResearchLabStore.project(operation()))

    def branch_current(campaign_id):
        if dependency_factory is None:
            # No service authority means an explicitly unknown, closed gate.
            return store().branch_snapshot(campaign_id)
        with dependency_factory().branch_guard(campaign_id, write=False) as dependencies:
            return store().branch_snapshot(campaign_id, dependency_state=dependencies)

    def branch_changed(campaign_id, branch_id, value, method, operation):
        target = store()
        replay = target.replay_branch_mutation(campaign_id, operation, value, branch_id=branch_id)
        if replay is not None:
            return replay

        def mutate(dependencies=None):
            arguments = (campaign_id, value) if branch_id is None else (campaign_id, branch_id, value)
            return getattr(target, method)(*arguments, dependency_state=dependencies)

        if dependency_factory is None:
            return mutate()
        # A global run guard would incorrectly stop B for a pending answer on A.
        # The branch callback checks the exact relevant scope inside the Lab txn.
        with dependency_factory().branch_guard(campaign_id, write=True) as dependencies:
            result = mutate(dependencies)
        return changed(lambda: result)

    @router.get("/capabilities")
    async def capabilities(request: Request):
        authorize(request)
        return await reply(lambda: store().capabilities())

    @router.get("/idea-seed/{session_id}")
    async def idea_seed(session_id: str, request: Request):
        authorize(request)
        return await reply(lambda: imports().seed(session_id))

    @router.get("/protocols/{identifier}")
    async def protocol(identifier: str, request: Request):
        authorize(request)
        return await reply(lambda: inspector().protocol(identifier))

    @router.get("")
    async def listing(request: Request, before: str | None = None, limit: int = 50):
        authorize(request)
        return await reply(lambda: inspector().list(before=before, limit=limit))

    @router.post("")
    async def create(request: Request):
        authorize(request)
        value = await body(request)
        return await campaign_reply(lambda: changed(lambda: store().create(value, resolve_origin=lambda payload: imports().resolve(payload))))

    @router.get("/trash")
    async def trash_listing(request: Request, before: str | None = None, limit: int = 50):
        authorize(request)
        return await reply(lambda: inspector().list(before=before, limit=limit, trashed=True))

    @router.post("/{campaign_id}/trash")
    async def move_to_trash(campaign_id: str, request: Request):
        authorize(request)
        value = await body(request, 2048)
        return await campaign_reply(lambda: store().set_deleted(campaign_id, value, True))

    @router.post("/{campaign_id}/restore")
    async def restore(campaign_id: str, request: Request):
        authorize(request)
        value = await body(request, 2048)
        return await campaign_reply(lambda: store().set_deleted(campaign_id, value, False))

    @router.get("/{campaign_id}")
    async def get(campaign_id: str, request: Request):
        authorize(request)
        return await campaign_reply(lambda: current(campaign_id))

    @router.get("/{campaign_id}/history")
    async def history(campaign_id: str, request: Request, before: str | None = None, limit: int = 50):
        authorize(request)
        return await reply(lambda: inspector().history(campaign_id, before=before, limit=limit))

    @router.get("/{campaign_id}/branches")
    async def branches(campaign_id: str, request: Request):
        authorize(request)
        return await campaign_reply(lambda: branch_current(campaign_id))

    @router.post("/{campaign_id}/branches")
    async def enable_branches(campaign_id: str, request: Request):
        authorize(request)
        value = await body(request, MAX_BRANCH_BODY_BYTES)
        return await campaign_reply(lambda: branch_changed(campaign_id, None, value, "enable_branches", "branches_enabled"))

    @router.post("/{campaign_id}/branches/{branch_id}/plan")
    async def branch_plan(campaign_id: str, branch_id: str, request: Request):
        authorize(request)
        value = await body(request, MAX_BRANCH_BODY_BYTES)
        return await campaign_reply(lambda: branch_changed(campaign_id, branch_id, value, "plan_branch", "branch_planned"))

    @router.post("/{campaign_id}/branches/{branch_id}/answers")
    async def branch_answers(campaign_id: str, branch_id: str, request: Request):
        authorize(request)
        value = await body(request, MAX_BRANCH_BODY_BYTES)
        return await campaign_reply(lambda: branch_changed(campaign_id, branch_id, value, "answer_branch", "branch_answered"))

    @router.post("/{campaign_id}/branches/{branch_id}/decision")
    async def branch_decision(campaign_id: str, branch_id: str, request: Request):
        authorize(request)
        value = await body(request, MAX_BRANCH_BODY_BYTES)
        return await campaign_reply(lambda: branch_changed(campaign_id, branch_id, value, "decide_branch", "branch_selected"))

    @router.post("/{campaign_id}/branches/{branch_id}/control")
    async def branch_control(campaign_id: str, branch_id: str, request: Request):
        authorize(request)
        value = await body(request, MAX_BRANCH_BODY_BYTES)
        return await campaign_reply(lambda: branch_changed(campaign_id, branch_id, value, "control_branch", "branch_controlled"))

    @router.post("/{campaign_id}/branches/{branch_id}/run")
    async def branch_run(campaign_id: str, branch_id: str, request: Request):
        authorize(request)
        value = await body(request, MAX_BRANCH_BODY_BYTES)
        return await campaign_reply(lambda: branch_changed(campaign_id, branch_id, value, "run_branch", "branch_action_executed"))

    @router.get("/{campaign_id}/export")
    async def export(campaign_id: str, request: Request):
        authorize(request)
        return await reply(lambda: inspector().export_campaign(campaign_id))

    @router.get("/{campaign_id}/artifacts/{digest}")
    async def artifact(campaign_id: str, digest: str, request: Request):
        authorize(request)
        return await reply(lambda: store().artifact(campaign_id, digest))

    @router.post("/{campaign_id}/decision")
    async def decide(campaign_id: str, request: Request):
        authorize(request)
        # A bounded HypothesisSet plus all five 8 KiB brief/feedback fields
        # can exceed 300 KiB when legal characters require JSON escaping.
        value = await body(request, 384 * 1024)
        return await campaign_reply(lambda: changed(lambda: store().decide(campaign_id, value)))

    @router.post("/{campaign_id}/run")
    async def run(campaign_id: str, request: Request):
        authorize(request)
        value = await body(request, 1024)

        def execute():
            if dependency_factory is None:
                return store().run(campaign_id, value)
            dependencies = dependency_factory()
            replay = dependencies.replay_lab_mutation(campaign_id, "action_executed", value)
            if replay is not None:
                return replay
            with dependencies.run_guard(campaign_id):
                result = store().run(campaign_id, value)
            return changed(lambda: result)
        return await campaign_reply(execute)

    @router.post("/{campaign_id}/continue")
    async def advance(campaign_id: str, request: Request):
        authorize(request)
        value = await body(request, 1024)
        return await campaign_reply(lambda: changed(lambda: store().advance(campaign_id, value)))

    @router.post("/{campaign_id}/correct-inputs")
    async def correct(campaign_id: str, request: Request):
        authorize(request)
        value = await body(request)
        if "shared_source_id" in value:
            if dependency_factory is None:
                raise HTTPException(409, "Shared source corrections are unavailable on this server", headers=HEADERS)
            source_id = value.pop("shared_source_id")
            return await campaign_reply(lambda: changed(lambda: dependency_factory().apply_lab_source_correction(
                campaign_id, value, source_id, correct_inputs=store().correct_inputs)))
        return await campaign_reply(lambda: changed(lambda: store().correct_inputs(campaign_id, value)))

    @router.get("/{campaign_id}/dependencies/export")
    async def dependency_export(campaign_id: str, request: Request):
        authorize(request)
        if dependency_factory is None:
            raise HTTPException(404, "Shared source dependencies are unavailable on this server", headers=HEADERS)
        return await reply(lambda: dependency_factory().scoped_export(campaign_id))

    @router.post("/{campaign_id}/dependencies/reconcile")
    async def reconcile(campaign_id: str, request: Request):
        authorize(request)
        if await body(request, 1024) != {}:
            raise HTTPException(400, "Dependency synchronization body must be empty JSON", headers=HEADERS)
        if dependency_factory is None:
            raise HTTPException(404, "Shared source dependencies are unavailable on this server", headers=HEADERS)

        def sync():
            dependencies = dependency_factory()
            dependencies.reconcile_campaign(campaign_id)
            return current(campaign_id)
        return await campaign_reply(sync)

    return router
