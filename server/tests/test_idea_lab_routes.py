import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException

from idea_lab import IdeaError, IdeaStore
from idea_lab_routes import create_router
from tests.test_idea_lab import brief, generated


class IdeaRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "state"
        self.calls = []
        self.block = False
        self.entered, self.closed = asyncio.Event(), asyncio.Event()

        async def generate(stage, current_brief, previous):
            self.calls.append(stage)
            if self.block:
                self.entered.set()
                try:
                    await asyncio.Future()
                finally:
                    self.closed.set()
            return generated(stage, bool(current_brief["sources"]))

        def authorize(request):
            if request.headers.get("authorization") != "Bearer test-only":
                raise HTTPException(403, "Native authorization required")

        self.app = FastAPI()
        self.app.include_router(create_router(storage_root=self.root, authorize=authorize, generate=generate))
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://isolated",
                                        headers={"authorization": "Bearer test-only"})
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.lifespan.__aexit__, None, None, None)
        self.base = "/api/research/ideas"

    async def create(self, sources=True):
        response = await self.client.post(self.base, json={"idempotency_key": "c", "brief": brief(sources)})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    async def wait_for_status(self, session_id, status):
        for _ in range(100):
            response = await self.client.get(f"{self.base}/{session_id}")
            self.assertEqual(response.status_code, 200, response.text)
            if response.json()["status"] == status:
                return response.json()
            await asyncio.sleep(0.005)
        self.fail(f"Idea session did not reach {status}")

    async def test_unauthorized_requests_do_not_initialize_or_generate(self):
        for method, path in (("get", self.base), ("post", self.base),
                             ("post", self.base + "/any/generate"), ("post", self.base + "/any/cancel"),
                             ("get", self.base + "/any/attempts/unknown")):
            response = await self.client.request(method, path, headers={"authorization": "wrong"})
            self.assertEqual(response.status_code, 403)
        self.assertFalse(self.root.exists())
        self.assertEqual(self.calls, [])

    async def test_real_routes_create_generate_read_choose_and_history(self):
        item = await self.create()
        generated_request = {"idempotency_key": "g", "expected_revision": item["revision"]}
        response = await self.client.post(f"{self.base}/{item['id']}/generate", json=generated_request)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "running")
        completed = await self.wait_for_status(item["id"], "completed")
        self.assertEqual(self.calls, ["literature", "ideas", "review"])
        response = await self.client.post(f"{self.base}/{item['id']}/generate", json=generated_request)
        self.assertEqual(response.json(), completed)
        decision = {"expected_revision": completed["revision"], "kind": "select", "selected_id": "d2", "feedback": "Equipment fits."}
        response = await self.client.post(f"{self.base}/{item['id']}/decision", json=decision)
        self.assertEqual(response.status_code, 200, response.text)
        selected = response.json()
        self.assertEqual(selected["brief"]["goal"], completed["result"]["ideas"]["directions"][1]["question"])
        self.assertEqual(self.calls, ["literature", "ideas", "review"])
        history = (await self.client.get(f"{self.base}/{item['id']}/history")).json()
        self.assertEqual(history["items"][1]["result"], completed["result"])
        listing = (await self.client.get(self.base)).json()
        self.assertEqual(listing["items"][0]["id"], selected["id"])
        self.assertTrue(listing["items"][0]["summary_only"])
        self.assertFalse(listing["has_more"])
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_get_and_list_never_generate(self):
        item = await self.create()
        for _ in range(3):
            self.assertEqual((await self.client.get(f"{self.base}/{item['id']}")).json()["status"], "draft")
            self.assertEqual(len((await self.client.get(self.base)).json()["items"]), 1)
        self.assertEqual(self.calls, [])

    async def test_paper_hash_query_is_exact_read_only_and_bounded(self):
        item = await self.create()
        source = item["brief"]["sources"][0]
        digest = hashlib.sha256(source["text"].encode()).hexdigest()
        path = f"{self.base}/{item['id']}/papers/{source['id']}"
        response = await self.client.get(path, params={"source_hash": digest})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["source"], source)
        self.assertEqual(response.json()["packet_hash"], digest)
        self.assertEqual((await self.client.get(path, params={"source_hash": "0" * 64})).status_code, 404)
        self.assertEqual((await self.client.get(path, params={"source_hash": "../private"})).status_code, 400)
        self.assertEqual((await self.client.get(path, headers={"authorization": "wrong"})).status_code, 403)
        self.assertEqual(self.calls, [])

    async def test_rejected_artifact_requires_explicit_authorized_session_scoped_read(self):
        item = await self.create()
        store = IdeaStore(self.root)
        item, _ = store.begin(item["id"], {"idempotency_key": "fixture", "expected_revision": 1})
        answer = generated("literature")
        answer["output"]["evidence"][0]["quote"] = "RAW_REJECTED_QUOTE"
        with self.assertRaises(IdeaError):
            store.save_stage(item["id"], item["generation_id"], "literature", answer)
        current = (await self.client.get(f"{self.base}/{item['id']}")).json()
        self.assertNotIn("RAW_REJECTED_QUOTE", str(current))
        attempt_id = current["research"]["validation_attempts"][0]["id"]
        path = f"{self.base}/{item['id']}/attempts/{attempt_id}"
        self.assertEqual((await self.client.get(path, headers={"authorization": "wrong"})).status_code, 403)
        response = await self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["output"], answer["output"])
        other = store.create({"idempotency_key": "other", "brief": brief()})
        self.assertEqual((await self.client.get(f"{self.base}/{other['id']}/attempts/{attempt_id}")).status_code, 404)
        self.assertEqual(self.calls, [])

    async def test_stale_choice_and_unknown_source_fields_are_rejected(self):
        item = await self.create()
        response = await self.client.post(f"{self.base}/{item['id']}/decision", json={
            "expected_revision": 999, "kind": "revise", "selected_id": None, "feedback": "New question"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "stale_revision")
        unsafe = brief()
        unsafe["command"] = "run a tool"
        response = await self.client.post(self.base, json={"idempotency_key": "new", "brief": unsafe})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.calls, [])

    async def test_cancel_waits_for_provider_cleanup_and_deduplicated_start_does_not_restart(self):
        self.block = True
        item = await self.create()
        request = {"idempotency_key": "g", "expected_revision": 1}
        started = (await self.client.post(f"{self.base}/{item['id']}/generate", json=request)).json()
        await asyncio.wait_for(self.entered.wait(), 3)
        response = await self.client.post(f"{self.base}/{item['id']}/cancel", json={"expected_revision": started["revision"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "cancelled")
        self.assertTrue(self.closed.is_set())
        response = await self.client.post(f"{self.base}/{item['id']}/generate", json=request)
        self.assertEqual(response.json()["status"], "cancelled")
        self.assertEqual(self.calls, ["literature"])

    async def test_router_lifespan_closes_provider_and_marks_interrupted(self):
        self.block = True
        item = await self.create()
        await self.client.post(f"{self.base}/{item['id']}/generate", json={"idempotency_key": "g", "expected_revision": 1})
        await asyncio.wait_for(self.entered.wait(), 3)
        await self.lifespan.__aexit__(None, None, None)
        self.assertTrue(self.closed.is_set())
        self.assertEqual(IdeaStore(self.root).get(item["id"])["status"], "interrupted")

    async def test_empty_source_workflow_reports_provisional_ideas(self):
        item = await self.create(sources=False)
        await self.client.post(f"{self.base}/{item['id']}/generate", json={"idempotency_key": "g", "expected_revision": 1})
        completed = await self.wait_for_status(item["id"], "completed")
        self.assertEqual(completed["result"]["literature"]["evidence"], [])
        self.assertTrue(all(direction["grounding"] == "provisional" for direction in completed["result"]["ideas"]["directions"]))

    async def test_body_type_and_size_are_bounded(self):
        response = await self.client.post(self.base, content="{}", headers={"content-type": "text/plain"})
        self.assertEqual(response.status_code, 415)
        response = await self.client.post(self.base, content="not json", headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 400)
        response = await self.client.post(self.base, content=b" " * (2 * 1024 * 1024 + 1), headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 413)


if __name__ == "__main__":
    unittest.main()
