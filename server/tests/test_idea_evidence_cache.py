import asyncio
import ast
import copy
import hashlib
import json
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI, HTTPException

import idea_evidence_cache as reuse
import idea_generation
from idea_lab_routes import IdeaController, create_router
from tests.test_idea_lab import brief, generated
from tests import test_idea_generation as native_fixture

MODEL = {"backend": "codex", "resolved_model": "test-model", "model": "test-model",
         "model_resolution": "thread_start", "configuration_hash": "c" * 64, "cli": "codex-cli 0.160.0"}


class EvidenceReuseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = reuse.LiteratureEvidenceCache(self.root / "cache", self.root / "ideas", namespace="test-admin")
        self.calls, self.requests = [], []
        self.delay, self.reject = None, 0
        self.model = copy.deepcopy(MODEL)
        self.source = brief()["sources"][0]
        self.source.update(provenance={"content_hash": "a" * 64, "parser_version": "test-v1", "retrieved_at": "first",
                                       "cache_hit": False, "discovery": {"tool_call_id": "first"}},
                           coverage={"kind": "partial", "limitations": ["Fixture excerpt"], "truncated": False})
        self.counter = 0

        async def discover(current, context, on_event):
            self.counter += 1
            return {"papers": [{"title": "same", "url": "https://example.org/paper"}],
                    "searches": [{"query": "fixture", "query_count": 1, "tool_call_id": str(self.counter)}],
                    "summary": "Dynamic search summary " + str(self.counter), "gaps": [],
                    "usage": {"total_tokens": 11}, "provider": {"backend": "fixture"}}

        async def read(papers, current, on_event):
            source = copy.deepcopy(self.source)
            source["provenance"].update(retrieved_at=str(self.counter), cache_hit=self.counter > 1,
                                        discovery={"tool_call_id": str(self.counter)})
            return {"sources": [source], "papers": [{"id": source["id"], "status": "read", "coverage": source["coverage"],
                     "title": source["title"], "url": source["uri"], "cache_hit": self.counter > 1}],
                    "coverage_gaps": ["Only this excerpt was read"], "usage": {"requests": 0}}

        async def generate(stage, current, previous, on_event, *, cache_request=None, on_dispatch=None):
            if cache_request:
                self.requests.append(cache_request)
                try:
                    await cache_request.resolve(copy.deepcopy(self.model))
                except reuse.LiteratureCacheHit as hit:
                    return hit.generated
                await on_dispatch({})
            self.calls.append(stage)
            if stage == "literature" and self.delay:
                await self.delay.wait()
            result = generated(stage)
            result["provider"] = {**self.model, "stop_reason": None}
            result["usage"] = {"total_tokens": 31, "monetary_cost": None}
            if stage == "literature" and self.reject:
                self.reject -= 1
                result["output"]["evidence"][0]["quote"] = "No such exact quote"
            return cache_request.annotate(result) if cache_request else result

        self.controller = IdeaController(self.root / "ideas", generate, discover, read, self.cache)
        self.addAsyncCleanup(self.controller.shutdown)

    def create(self, key, mutate=None):
        data = brief(False)
        if mutate:
            mutate(data)
        return self.controller.store.create({"idempotency_key": key, "brief": data})

    async def run_item(self, item):
        await self.controller.start(item["id"], {"idempotency_key": "run", "expected_revision": item["revision"]})
        await self.controller.jobs[item["id"]]
        return self.controller.store.get(item["id"])

    @staticmethod
    def receipt(item):
        return next(row["provider"]["literature_cache"] for row in item["usage"] if row["stage"] == "literature")

    async def test_second_group_hits_accepted_origin_and_reserves_no_extraction_job(self):
        first = await self.run_item(self.create("first"))
        second = await self.run_item(self.create("second"))
        self.assertEqual(first["research"]["readiness"], "ready_for_choice")
        self.assertEqual(second["research"]["readiness"], "ready_for_choice")
        self.assertEqual(self.calls.count("literature"), 1)
        self.assertEqual(first["research"]["used"]["model_calls"], 4)
        self.assertEqual(second["research"]["used"]["model_calls"], 3)
        a, b = self.receipt(first), self.receipt(second)
        self.assertEqual((a["status"], b["status"]), ("miss", "hit"))
        self.assertEqual(a["key"], b["key"])
        self.assertEqual(b["origin"]["session_id"], first["id"])
        self.assertEqual(b["avoided_model_jobs"], 1)
        entry = next(row for row in second["usage"] if row["stage"] == "literature")
        self.assertEqual(entry["usage"]["total_tokens"], 0)
        self.assertEqual(entry["provider"]["origin_usage"]["total_tokens"], 31)
        self.assertIsNone(entry["provider"]["origin_usage"]["monetary_cost"])
        self.assertEqual(first["result"]["literature"], second["result"]["literature"])
        self.assertEqual(second["research"]["validation_attempts"][0]["status"], "accepted")

    async def test_concurrent_requests_join_one_actual_extraction(self):
        self.delay = asyncio.Event()
        a, b = self.create("a"), self.create("b")
        first = asyncio.create_task(self.run_item(a))
        while not self.calls.count("literature"):
            await asyncio.sleep(.01)
        second = asyncio.create_task(self.run_item(b))
        while not any(request.waited for request in self.requests):
            await asyncio.sleep(.01)
        self.delay.set()
        x, y = await asyncio.gather(first, second)
        self.assertEqual(self.calls.count("literature"), 1)
        self.assertEqual(self.receipt(y)["status"], "join")
        self.assertEqual(y["research"]["used"]["model_calls"], 3)
        self.assertEqual(self.receipt(y)["origin"]["session_id"], x["id"])

    async def test_scope_brief_model_parser_coverage_and_source_changes_miss(self):
        await self.run_item(self.create("base"))
        cases = [lambda: self.model.update(resolved_model="other", model="other"),
                 lambda: self.model.update(configuration_hash="d" * 64),
                 lambda: self.source["provenance"].update(parser_version="test-v2"),
                 lambda: self.source["coverage"].update(limitations=["Different coverage"]),
                 lambda: self.source.update(text=self.source["text"] + " New source version."),
                 lambda: setattr(self.controller, "evidence_cache", reuse.LiteratureEvidenceCache(self.root / "cache", self.root / "ideas", namespace="other-admin"))]
        for index, mutate in enumerate(cases):
            mutate()
            result = await self.run_item(self.create(str(index)))
            self.assertEqual(self.receipt(result)["status"], "miss")
        result = await self.run_item(self.create("goal", lambda data: data.update(goal="Changed scientific question")))
        self.assertEqual(self.receipt(result)["status"], "miss")
        self.assertEqual(self.calls.count("literature"), 8)

    async def test_repair_is_budgeted_and_only_accepted_attempt_is_reused(self):
        self.reject = 1
        first = await self.run_item(self.create("repair"))
        second = await self.run_item(self.create("reuse"))
        self.assertEqual(first["research"]["used"]["model_calls"], 5)
        self.assertEqual(second["research"]["used"]["model_calls"], 3)
        attempts = first["research"]["validation_attempts"]
        self.assertEqual([row["status"] for row in attempts[:2]], ["rejected", "accepted"])
        self.assertEqual(self.receipt(second)["origin"]["attempt_id"], attempts[1]["id"])
        self.assertEqual(self.calls.count("literature"), 2)

    async def test_publish_failure_preserves_accepted_literature_and_continues(self):
        with patch.object(self.cache, "publish", side_effect=reuse.EvidenceCacheError("cache_bounds", "Synthetic bound")):
            first = await self.run_item(self.create("publish-error"))
        self.assertEqual(first["research"]["readiness"], "ready_for_choice")
        self.assertEqual(first["research"]["validation_attempts"][0]["status"], "accepted")
        self.assertTrue(any(event["type"] == "literature_cache_publish_failed" for event in first["research"]["activities"]))
        second = await self.run_item(self.create("must-miss"))
        self.assertEqual(self.receipt(second)["status"], "miss")
        self.assertEqual(self.calls.count("literature"), 2)

    async def test_lookup_failure_bypasses_cache_without_bypassing_budget(self):
        with patch.object(self.cache, "acquire", side_effect=sqlite3.OperationalError("synthetic")):
            result = await self.run_item(self.create("lookup-error"))
        self.assertEqual(result["research"]["readiness"], "ready_for_choice")
        self.assertEqual(self.receipt(result)["status"], "bypass")
        self.assertEqual(self.receipt(result)["avoided_model_jobs"], 0)
        self.assertEqual(result["research"]["used"]["model_calls"], 4)

    async def test_cancelled_owner_releases_lease_and_does_not_publish(self):
        self.delay = asyncio.Event()
        item = self.create("cancel")
        await self.controller.start(item["id"], {"idempotency_key": "run", "expected_revision": item["revision"]})
        while not self.calls.count("literature"):
            await asyncio.sleep(.01)
        current = self.controller.store.get(item["id"])
        await self.controller.cancel(item["id"], {"expected_revision": current["revision"]})
        with self.cache._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM entries WHERE owner IS NOT NULL OR accepted IS NOT NULL").fetchone()[0], 0)
        self.delay = None
        result = await self.run_item(self.create("after-cancel"))
        self.assertEqual(self.receipt(result)["status"], "miss")

    async def test_expired_fence_cannot_publish_over_new_owner(self):
        self.delay = asyncio.Event()
        first = asyncio.create_task(self.run_item(self.create("stale-owner")))
        while not self.calls.count("literature"):
            await asyncio.sleep(.01)
        request = self.requests[0]
        with self.cache._db() as db:
            db.execute("UPDATE entries SET epoch=epoch+1,owner='different-owner',expires=?", (self.cache.clock() + 40,))
        self.delay.set()
        result = await first
        self.assertEqual(result["research"]["readiness"], "ready_for_choice")
        self.assertTrue(any(event["type"] == "literature_cache_stale_rejected" for event in result["research"]["activities"]))
        with self.cache._db() as db:
            row = db.execute("SELECT owner,accepted FROM entries WHERE key=?", (request.claim["key"],)).fetchone()
            self.assertEqual(row[0], "different-owner")
            self.assertIsNone(row[1])

    async def test_corrupt_cached_output_rejected_and_actual_origin_preserved(self):
        first = await self.run_item(self.create("source"))
        origin = copy.deepcopy(first["result"]["literature"])
        with self.cache._db() as db:
            row = db.execute("SELECT key,accepted FROM entries").fetchone()
            data = json.loads(row[1]); data["output"]["evidence"][0]["quote"] = "Fabricated quote"
            db.execute("UPDATE entries SET accepted=? WHERE key=?", (json.dumps(data), row[0]))
        second = await self.run_item(self.create("corrupt"))
        self.assertEqual(self.receipt(second)["status"], "miss")
        self.assertTrue(self.receipt(second)["stale_rejected"])
        self.assertEqual(self.controller.store.get(first["id"])["result"]["literature"], origin)

    async def test_deleted_accepted_origin_is_not_treated_as_reuse_authority(self):
        first = await self.run_item(self.create("original"))
        origin = first["research"]["validation_attempts"][0]["id"]
        with sqlite3.connect(self.controller.store.path) as db:
            db.execute("DELETE FROM stage_attempts WHERE id=?", (origin,))
        second = await self.run_item(self.create("orphan"))
        self.assertEqual(self.receipt(second)["status"], "miss")
        self.assertTrue(self.receipt(second)["stale_rejected"])
        self.assertEqual(self.calls.count("literature"), 2)

    async def test_schema_recipe_and_feedback_changes_miss(self):
        await self.run_item(self.create("original"))
        with patch.object(reuse, "RECIPE", reuse.RECIPE + ";new-version"):
            changed_recipe = await self.run_item(self.create("recipe"))
        self.assertEqual(self.receipt(changed_recipe)["status"], "miss")
        schema = copy.deepcopy(reuse.idea_lab.GENERATION_SCHEMAS["literature"])
        schema["description"] = "New extraction schema revision"
        with patch.dict(reuse.idea_lab.GENERATION_SCHEMAS, literature=schema):
            changed_schema = await self.run_item(self.create("schema"))
        self.assertEqual(self.receipt(changed_schema)["status"], "miss")
        self.assertEqual(self.calls.count("literature"), 3)
        one = {"context": {"feedback": [{"text": "Inspect counterevidence"}], "prior_review": None}}
        two = {"context": {"feedback": [{"text": "Inspect qualifications"}], "prior_review": None}}
        a, pa = reuse.extraction_packet(brief(), one)
        b, pb = reuse.extraction_packet(brief(), two)
        self.assertNotEqual(idea_generation.stage_prompt("literature", a, pa), idea_generation.stage_prompt("literature", b, pb))

    async def test_cancel_between_acquire_commit_and_delivery_releases_exact_owner(self):
        entered, release = threading.Event(), threading.Event()
        acquire = self.cache.acquire
        def blocked(*args):
            result = acquire(*args)
            entered.set()
            release.wait(3)
            return result
        item = self.create("admission-cancel")
        with patch.object(self.cache, "acquire", side_effect=blocked):
            await self.controller.start(item["id"], {"idempotency_key": "run", "expected_revision": item["revision"]})
            await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3)
            current = self.controller.store.get(item["id"])
            cancelling = asyncio.create_task(self.controller.cancel(item["id"], {"expected_revision": current["revision"]}))
            await asyncio.sleep(.02)
            self.assertFalse(cancelling.done())
            release.set()
            await cancelling
        self.assertEqual(self.calls.count("literature"), 0)
        with self.cache._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM entries WHERE owner IS NOT NULL").fetchone()[0], 0)

    async def test_expired_owner_takeover_is_miss_not_join_or_saved_work(self):
        accepted = await self.run_item(self.create("before-crash"))
        with self.cache._db() as db:
            db.execute("UPDATE entries SET accepted=NULL,owner='dead-owner',expires=0")
        after = await self.run_item(self.create("recovery"))
        self.assertEqual(self.receipt(after)["status"], "miss")
        self.assertEqual(self.receipt(after)["avoided_model_jobs"], 0)
        self.assertEqual(after["research"]["used"]["model_calls"], 4)
        self.assertEqual(self.calls.count("literature"), 2)
        self.assertEqual(self.controller.store.get(accepted["id"])["result"], accepted["result"])

    async def test_empty_evidence_and_final_rejection_are_not_published(self):
        self.reject = 2
        failed = await self.run_item(self.create("bad-quotes"))
        self.assertEqual(failed["status"], "failed")
        with self.cache._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM entries WHERE accepted IS NOT NULL").fetchone()[0], 0)

    async def test_unresolved_model_configuration_bypasses_reuse_explicitly(self):
        self.model["configuration_hash"] = None
        result = await self.run_item(self.create("unknown-config"))
        self.assertEqual(self.receipt(result)["status"], "bypass")
        self.assertEqual(result["research"]["used"]["model_calls"], 4)
        with self.cache._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM entries").fetchone()[0], 0)

    async def test_wait_timeout_never_dispatches_a_duplicate_extraction(self):
        self.delay = asyncio.Event()
        first = asyncio.create_task(self.run_item(self.create("owner")))
        while not self.calls.count("literature"):
            await asyncio.sleep(.01)
        with patch.object(reuse, "WAIT_SECONDS", 0):
            second = await self.run_item(self.create("bounded-waiter"))
        self.assertEqual(second["status"], "failed")
        self.assertIn("cache_wait_timeout", second["error"])
        self.assertEqual(self.calls.count("literature"), 1)
        self.assertEqual(second["research"]["used"]["model_calls"], 1)
        self.delay.set()
        await first

    async def test_failed_factory_keeps_authorized_existing_reads_and_budgeted_generation(self):
        existing = await self.run_item(self.create("accepted-before-cache-break"))
        before = copy.deepcopy(existing)
        factory = Mock(side_effect=sqlite3.DatabaseError("PRIVATE_PATH_OR_DIAGNOSTIC"))
        def authorize(request):
            if request.headers.get("authorization") != "Bearer synthetic-only":
                raise HTTPException(403, "Native authorization required")
        app = FastAPI()
        app.include_router(create_router(storage_root=self.root / "ideas", authorize=authorize,
            generate=self.controller.generate, discover=self.controller.discover, retrieve=self.controller.retrieve,
            evidence_cache_factory=factory))
        base = "/api/research/ideas"
        async with app.router.lifespan_context(app), httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://isolated") as client:
            self.assertEqual((await client.get(base)).status_code, 403)
            factory.assert_not_called()
            client.headers["authorization"] = "Bearer synthetic-only"
            response = await client.get(f"{base}/{existing['id']}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), before)
            history = await client.get(f"{base}/{existing['id']}/history")
            self.assertEqual(history.status_code, 200)
            response = await client.post(base, json={"idempotency_key": "new-with-unavailable-cache", "brief": brief(False)})
            self.assertEqual(response.status_code, 200, response.text)
            item = response.json()
            response = await client.post(f"{base}/{item['id']}/generate", json={"idempotency_key": "run", "expected_revision": item["revision"]})
            self.assertEqual(response.status_code, 200, response.text)
            for _ in range(200):
                item = (await client.get(f"{base}/{item['id']}")).json()
                if item["status"] != "running":
                    break
                await asyncio.sleep(.01)
            self.assertEqual(item["research"]["readiness"], "ready_for_choice")
            receipt = self.receipt(item)
            self.assertEqual((receipt["status"], receipt["reason"], receipt["avoided_model_jobs"]),
                             ("bypass", "cache_initialization_unavailable", 0))
            self.assertIsNone(receipt["scope"])
            self.assertEqual(item["research"]["used"]["model_calls"], 4)
            self.assertNotIn("PRIVATE_PATH_OR_DIAGNOSTIC", json.dumps(item))
            factory.assert_called_once()


class NativeReuseBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_wrapper_and_lazy_factory_forward_exact_protocol(self):
        # Extract only this definition/call: importing agent_server would run
        # full-service initialization and would invalidate isolation evidence.
        source = Path(__file__).resolve().parents[1] / "agent_server.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        definition = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                          and node.name == "generate_idea_lab_stage")
        native = AsyncMock(return_value={"output": {}, "usage": {}, "provider": {}})
        namespace = {"idea_generation": SimpleNamespace(generate_idea_stage=native),
                     "CODEX_BIN": "fixture-codex", "runner_env": lambda: {"FIXTURE": "only"}}
        exec(compile(ast.Module(body=[definition], type_ignores=[]), str(source), "exec"), namespace)
        callback, request, reserve = object(), object(), object()
        await namespace["generate_idea_lab_stage"]("literature", {"goal": "g"}, {}, callback,
                                                    cache_request=request, on_dispatch=reserve)
        native.assert_awaited_once_with("literature", {"goal": "g"}, {}, on_event=callback,
            executable="fixture-codex", model=None, env={"FIXTURE": "only"}, cache_request=request, on_dispatch=reserve)
        call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name) and node.func.id == "create_idea_lab_router")
        constructor = Mock(return_value=object())
        authorize, discover, retrieve = object(), object(), object()
        namespace.update(STATE_DIR=Path("/isolated-state"), require_native_admin_control=authorize,
            discover_idea_literature=discover, retrieve_idea_literature=retrieve,
            idea_evidence_cache=SimpleNamespace(LiteratureEvidenceCache=constructor),
            create_idea_lab_router=lambda **kwargs: kwargs)
        kwargs = eval(compile(ast.Expression(body=call), str(source), "eval"), namespace)
        constructor.assert_not_called()
        self.assertIs(kwargs["authorize"], authorize)
        self.assertIs(kwargs["generate"], namespace["generate_idea_lab_stage"])
        self.assertIs(kwargs["discover"], discover)
        self.assertIs(kwargs["retrieve"], retrieve)
        self.assertEqual(kwargs["source_library_root"], Path("/isolated-state/research/library"))
        kwargs["evidence_cache_factory"]()
        constructor.assert_called_once_with(Path("/isolated-state/research/evidence-cache"),
            Path("/isolated-state/research/ideas"), namespace="native-admin-local")

    async def test_cache_hit_follows_native_preflight_but_does_not_start_turn(self):
        fixture = native_fixture.NativeIdeaTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        expected = {"output": native_fixture.LITERATURE, "usage": {"total_tokens": 0},
                    "provider": {"backend": "literature_artifact_cache"}}
        class Request:
            async def resolve(self, model):
                self.model = model
                raise reuse.LiteratureCacheHit(expected)
        request = Request()
        async def forbidden(_):
            self.fail("Cache hit must not reserve a model job")
        answer = await idea_generation.generate_idea_stage("literature", native_fixture.BRIEF, {},
            executable="synthetic", model=None, env={}, cache_request=request, on_dispatch=forbidden)
        self.assertEqual(answer, expected)
        self.assertEqual(request.model["resolved_model"], "resolved-native-model")
        self.assertRegex(request.model["configuration_hash"], r"^[a-f0-9]{64}$")
        fixture.client.start_thread.assert_awaited_once()
        fixture.client.start_turn.assert_not_called()
        fixture.client.close.assert_awaited_once()

    async def test_preflight_rejects_non_ephemeral_before_cache_lookup(self):
        fixture = native_fixture.NativeIdeaTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.client.read_thread.return_value = {"ephemeral": False, "path": "synthetic/persisted"}
        class Request:
            async def resolve(self, _):
                raise AssertionError("Cache lookup must follow isolation verification")
        with self.assertRaisesRegex(idea_generation.IdeaGenerationError, "ephemeral"):
            await idea_generation.generate_idea_stage("literature", native_fixture.BRIEF, {},
                executable="synthetic", model=None, env={}, cache_request=Request())
        fixture.client.start_turn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
