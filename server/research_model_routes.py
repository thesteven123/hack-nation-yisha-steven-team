"""Native-only, explicitly requested model roles over saved research campaigns."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager, nullcontext
import json
import re
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from research_lab import LabError, ResearchLabStore
from research_model_jobs import ModelJobError, ResearchModelJobs

HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
WAIT_SECONDS = 20


def create_research_model_router(*, storage_root, lab_root, authorize, native_options, generate=None,
                                dependency_factory=None):
    cached_lab = cached_jobs = None
    mutex = threading.Lock()
    active_waits = 0
    closing = False

    @contextmanager
    def dependency_guard(campaign_id):
        try:
            with dependency_factory().run_guard(campaign_id):
                yield
        except LabError as exc:
            raise ModelJobError(exc.code, exc.message) from None

    def dependency_check(campaign_id):
        try:
            return dependency_factory().admission(campaign_id)
        except LabError as exc:
            raise ModelJobError(exc.code, exc.message) from None

    @contextmanager
    def branch_guard(campaign_id, branch_id):
        if dependency_factory is None:
            raise ModelJobError("branch_scope_unknown", "Branch execution requires service dependency admission")
        try:
            with dependency_factory().branch_guard(campaign_id) as dependencies:
                with lab().branch_scope_guard(campaign_id, branch_id, dependency_state=dependencies) as snapshot:
                    yield snapshot
        except LabError as exc:
            raise ModelJobError(exc.code, exc.message) from None

    def project_job(item):
        if not item.get("branch_id"):
            return item
        current = "unknown"
        try:
            if dependency_factory:
                with dependency_factory().branch_guard(item["campaign_id"], write=False) as dependencies:
                    snapshot = lab().branch_model_snapshot(item["campaign_id"], item["branch_id"], dependency_state=dependencies)
                    current = "current" if snapshot["branch_scope"] == item["branch_scope"] else "stale"
        except LabError as exc:
            current = "stale" if exc.code in {"branch_blocked", "branch_scope_changed", "branch_not_found", "dependency_stale"} else "unknown"
        return {**item, "scope_status": current}

    @contextmanager
    def publication_guard(campaign_id):
        # Hold neither lock over provider work. Every core mutation already uses
        # BEGIN IMMEDIATE, so this lock stabilizes its revision while the model
        # receipt is published without changing any core records.
        try:
            with dependency_guard(campaign_id) if dependency_factory else nullcontext():
                with lab()._db(write=True) as db:
                    row = db.execute("SELECT json_extract(payload,'$.revision') FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
                    if row is None:
                        raise LabError("not_found", "Campaign was not found during model publication")
                    yield row[0]
        except LabError as exc:
            raise ModelJobError(exc.code, exc.message) from None

    def lab():
        nonlocal cached_lab
        if cached_lab is not None:
            return cached_lab
        with mutex:
            if closing:
                raise ModelJobError("closed", "Native role service is shutting down")
            if cached_lab is None:
                cached_lab = ResearchLabStore(lab_root)
            return cached_lab

    def jobs():
        nonlocal cached_jobs
        if cached_jobs is not None:
            return cached_jobs
        with mutex:
            if closing:
                raise ModelJobError("closed", "Native role service is shutting down")
            if cached_jobs is None:
                kwargs = {"generate": generate} if generate else {}
                kwargs["publication_guard"] = publication_guard
                kwargs["branch_guard"] = branch_guard
                if dependency_factory is not None:
                    kwargs.update(admission_guard=dependency_guard, dependency_check=dependency_check)
                cached_jobs = ResearchModelJobs(storage_root, **kwargs)
            return cached_jobs

    @asynccontextmanager
    async def lifespan(app):
        nonlocal closing, cached_jobs
        closing = False
        try:
            yield
        finally:
            closing = True
            # Join any in-progress lazy construction off the event loop before
            # shutting down its owned tasks, including when lifespan exits badly.
            def initialized():
                with mutex:
                    return cached_jobs
            controller = await asyncio.to_thread(initialized)
            if controller is not None:
                await controller.shutdown()
            cached_jobs = None

    router = APIRouter(prefix="/api/research/lab", lifespan=lifespan)

    async def invoke(operation):
        try:
            return await operation()
        except (LabError, ModelJobError) as exc:
            status = {"not_found": 404, "revision_conflict": 409, "idempotency_conflict": 409,
                      "model_changed": 409, "busy": 409, "closed": 503,
                      "budget_exhausted": 409, "unsupported_schema": 409,
                      "dependency_stale": 409, "dependency_limit": 409,
                      "branch_scope_changed": 409, "branch_blocked": 409, "branch_scope_unknown": 409,
                      "branch_required": 409, "branch_not_found": 404,
                      "storage_error": 503, "integrity_error": 503,
                      "output_too_large": 413, "context_too_large": 413}.get(exc.code, 400)
            raise HTTPException(status, {"code": exc.code, "message": exc.message}, headers=HEADERS) from None
        except OSError:
            raise HTTPException(503, "Native role storage is unavailable; inspect the existing request before retrying", headers=HEADERS) from None

    async def read(operation):
        return await invoke(lambda: asyncio.to_thread(operation))

    async def body(request):
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise HTTPException(415, "Use application/json", headers=HEADERS)
        async def collect():
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > 4096:
                    raise HTTPException(413, "Native role request exceeds 4 KiB", headers=HEADERS)
                data.extend(chunk)
            return data
        try:
            value = json.loads(await asyncio.wait_for(collect(), 5))
        except asyncio.TimeoutError:
            raise HTTPException(408, "Native role request body timed out", headers=HEADERS) from None
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(400, "Invalid JSON request", headers=HEADERS) from None
        if not isinstance(value, dict):
            raise HTTPException(400, "Expected a JSON object", headers=HEADERS)
        return value

    def scoped(campaign_id, job_id):
        lab().get(campaign_id)
        item = jobs().get(job_id)
        if item["campaign_id"] != campaign_id:
            raise ModelJobError("not_found", "Native role does not belong to this research campaign")
        return project_job(item)

    def pagination(request):
        pairs = list(request.query_params.multi_items())
        if any(key not in {"before", "limit", "branch_id"} for key, _ in pairs) or len({key for key, _ in pairs}) != len(pairs):
            raise HTTPException(400, "Use only one before cursor and one limit", headers=HEADERS)
        values = {}
        for key, value in pairs:
            if key == "branch_id":
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value) is None:
                    raise HTTPException(400, "Invalid branch filter", headers=HEADERS)
                values[key] = value
                continue
            if re.fullmatch(r"[1-9][0-9]{0,18}", value) is None or int(value) > (50 if key == "limit" else 2**63 - 1):
                raise HTTPException(400, "before must be a positive cursor; limit must be 1–50", headers=HEADERS)
            values[key] = int(value)
        return values

    async def start_planned(item):
        if item["status"] != "planned":
            return item  # Replay must not require fresh native configuration.
        options = await read(native_options)
        return await invoke(lambda: jobs().start(item["id"], **options,
            current_revision=lambda cid: lab().get(cid)["revision"]))

    @router.get("/{campaign_id}/model-jobs")
    async def listing(campaign_id: str, request: Request):
        authorize(request)
        page = pagination(request)
        await read(lambda: lab().get(campaign_id))
        def snapshot():
            result = jobs().list(campaign_id, **page)
            result["items"] = [project_job(item) for item in result["items"]]
            return result
        return JSONResponse(await read(snapshot), headers=HEADERS)

    @router.post("/{campaign_id}/model-jobs")
    async def create(campaign_id: str, request: Request):
        authorize(request)
        value = await body(request)
        expected = ({"role", "idempotency_key", "branch_id", "expected_branch_revision", "expected_authority_epoch"}
                    if "branch_id" in value else {"role", "expected_revision", "idempotency_key"})
        if set(value) != expected:
            raise HTTPException(400, "Supply only role, expected_revision and idempotency_key", headers=HEADERS)
        # A replay is read-only, including a reserved planned job. Recovering a
        # planned reservation requires its explicit /start action and guards.
        request_value = {**value, "campaign_id": campaign_id}
        replay = await read(lambda: jobs().replay(request_value))
        if replay is not None:
            return JSONResponse(replay, headers=HEADERS)
        item = await read(lambda: jobs().prepare(request_value, resolve_snapshot=lab().get))
        result = await start_planned(item)
        return JSONResponse(result, headers=HEADERS)

    @router.get("/{campaign_id}/model-jobs/{job_id}")
    async def get(campaign_id: str, job_id: str, request: Request):
        authorize(request)
        return JSONResponse(await read(lambda: scoped(campaign_id, job_id)), headers=HEADERS)

    @router.get("/{campaign_id}/model-jobs/{job_id}/wait")
    async def wait(campaign_id: str, job_id: str, request: Request):
        nonlocal active_waits
        authorize(request)
        item = await read(lambda: scoped(campaign_id, job_id))
        if item["status"] != "running":
            return JSONResponse(item, headers=HEADERS)
        if active_waits >= 8:
            raise HTTPException(429, "Too many pending native role waits", headers={**HEADERS, "Retry-After": "20"})
        active_waits += 1
        try:
            owned = jobs().jobs.get(job_id)
            if owned is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(owned), timeout=WAIT_SECONDS)
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    if not owned.done():
                        raise  # disconnected waiter must not cancel native work
            else:
                # A concurrently finishing task will publish before returning;
                # never turn an unexpectedly ownerless running row into a hot poll.
                await asyncio.sleep(WAIT_SECONDS)
            return JSONResponse(await read(lambda: scoped(campaign_id, job_id)), headers=HEADERS)
        finally:
            active_waits -= 1

    @router.post("/{campaign_id}/model-jobs/{job_id}/cancel")
    async def cancel(campaign_id: str, job_id: str, request: Request):
        authorize(request)
        if await body(request) != {}:
            raise HTTPException(400, "Cancellation body must be empty JSON", headers=HEADERS)
        await read(lambda: scoped(campaign_id, job_id))
        return JSONResponse(await invoke(lambda: jobs().cancel(job_id)), headers=HEADERS)

    @router.post("/{campaign_id}/model-jobs/{job_id}/start")
    async def start(campaign_id: str, job_id: str, request: Request):
        authorize(request)
        if await body(request) != {}:
            raise HTTPException(400, "Start body must be empty JSON", headers=HEADERS)
        item = await read(lambda: scoped(campaign_id, job_id))
        return JSONResponse(await start_planned(item), headers=HEADERS)

    @router.get("/{campaign_id}/model-jobs/{job_id}/artifacts/{digest}")
    async def artifact(campaign_id: str, job_id: str, digest: str, request: Request):
        authorize(request)
        await read(lambda: scoped(campaign_id, job_id))
        return JSONResponse(await read(lambda: jobs().artifact(job_id, digest)), headers=HEADERS)

    return router
