"""Native, authenticated access to the bounded AI Lab reference action.

The supplied source is data. It cannot authorize tools, network access or spending.
No provider or session APIs are invoked by this router.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from research_actions import ResearchActionStore, ResearchError


# Reviewed sections only; task records refer to these and retrieve them on demand.
# These are bounded implementation summaries, not copies of the full protocols.
PROTOCOLS = {
    "literature-cache-v0.5": {
        "source": "https://chatgpt.com/space/page_1775310b16d08191906760157d513776",
        "blocks": ["56149a61-18ff-4a16-8e3b-5df955e6c6a0", "aef9775f-abae-414e-b871-bafb72b0606b"],
        "requirements": [
            "Reuse parsing only with matching content hash, parser configuration and coverage.",
            "Retain access scope and source identity; visible coverage gaps qualify every result.",
            "A missing quote in supplied material is not proof of absence in the original source.",
        ],
    },
    "records-v0.5": {
        "source": "https://chatgpt.com/space/page_0481c0d21b8c8191a1d00519ce96d9b0",
        "blocks": ["e078a290-5374-4a0a-b326-c5ccdc43df31"],
        "requirements": [
            "Keep execution, observation, QC, source support and inference validity separate.",
            "This adapter establishes only exact occurrence in supplied text, not scientific truth.",
            "Retain frozen inputs, goal revision, method, limitations, artifacts and costs.",
        ],
    },
    "execution-v0.5": {
        "source": "https://chatgpt.com/space/page_7110b10c78948191a844b4548fdce56c",
        "blocks": ["77c08e1c-8f5f-4b55-978c-57212ba780f4"],
        "requirements": [
            "Persist logical identity, request hash and idempotency key before execution.",
            "A repeated request returns the same run; changed inputs require a new identity.",
            "Only local deterministic work is implemented. External execution requires a separate recovery contract.",
        ],
    },
}

HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
# JSON escaping can expand one source byte to six bytes. The decoded input is
# separately bounded by the store, so valid escaped UTF-8 sources still fit.
MAX_BODY_BYTES = 7 * 1024 * 1024


def create_research_action_router(*, storage_root, authorize) -> APIRouter:
    """Use the server's native authorization; never initialize storage on import."""
    router = APIRouter(prefix="/api/research")
    cached_store = None
    store_lock = threading.Lock()
    active = 0

    def store():
        nonlocal cached_store
        with store_lock:
            if cached_store is None:
                cached_store = ResearchActionStore(storage_root)
            return cached_store

    async def worker(operation):
        nonlocal active
        if active >= 4:
            raise HTTPException(503, "Research store is busy; retry shortly", headers={**HEADERS, "Retry-After": "2"})
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
        except ResearchError as exc:
            status = {"not_found": 404, "idempotency_conflict": 409,
                      "unsupported_schema": 409, "integrity_error": 503,
                      "storage_error": 503}.get(exc.code, 400)
            raise HTTPException(status, {"code": exc.code, "message": exc.message}, headers=HEADERS) from None
        except (sqlite3.Error, OSError):
            # Do not expose storage paths/source contents through an error body.
            raise HTTPException(503, "Research storage is unavailable; the request can be retried", headers=HEADERS) from None

    async def body(request, limit=MAX_BODY_BYTES):
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise HTTPException(415, "Use application/json", headers=HEADERS)

        async def collect():
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > limit:
                    raise HTTPException(413, "Research request is too large", headers=HEADERS)
                data.extend(chunk)
            return data

        try:
            raw = await asyncio.wait_for(collect(), timeout=5)
            value = json.loads(raw)
        except asyncio.TimeoutError:
            raise HTTPException(408, "Research request body was not received", headers=HEADERS) from None
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(400, "Invalid JSON request", headers=HEADERS) from None
        if not isinstance(value, dict):
            raise HTTPException(400, "Expected a JSON object", headers=HEADERS)
        return value

    @router.post("/actions")
    async def plan(request: Request):
        authorize(request)
        value = await body(request)
        return JSONResponse(await worker(lambda: store().plan(value)), headers=HEADERS)

    @router.get("/actions/{action_id}")
    async def get(action_id: str, request: Request):
        authorize(request)
        return JSONResponse(await worker(lambda: store().get(action_id)), headers=HEADERS)

    @router.post("/actions/{action_id}/run")
    async def run(action_id: str, request: Request):
        authorize(request)
        value = await body(request, 1024)
        if set(value) != {"spec_hash"} or not isinstance(value["spec_hash"], str):
            raise HTTPException(400, "Provide the frozen spec_hash", headers=HEADERS)

        def execute():
            current = store().get(action_id)
            if value["spec_hash"] != current["spec_hash"]:
                raise HTTPException(409, "Frozen specification differs; inspect the action before running", headers=HEADERS)
            return store().run(action_id)

        return JSONResponse(await worker(execute), headers=HEADERS)

    @router.get("/artifacts/{digest}")
    async def artifact(digest: str, request: Request):
        authorize(request)
        value = await worker(lambda: store().artifact(digest))
        return Response(value, media_type="application/octet-stream", headers={
            **HEADERS, "Content-Disposition": "attachment", "X-Content-SHA256": digest,
        })

    @router.get("/protocols/{protocol_id}")
    async def protocol(protocol_id: str, request: Request):
        authorize(request)
        if protocol_id not in PROTOCOLS:
            raise HTTPException(404, "Protocol section not available", headers=HEADERS)
        return JSONResponse({"id": protocol_id, "design_version": "0.5",
                             "representation": "implementation_summary", **PROTOCOLS[protocol_id]}, headers=HEADERS)

    return router
