"""Real native guards/HTTP lifecycle with an explicitly fake model boundary."""
import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from research_lab import ResearchLabStore
from research_dependencies import ResearchDependencies
from research_model_jobs import ResearchModelJobs
from research_model_routes import create_research_model_router
from tests import test_codex_auth_isolated as auth_fixture
from tests.test_research_lab import numeric_request, source_request
from tests.test_research_model_jobs import envelope, output_for

NATIVE = {"X-AgentsDock-Token": "synthetic-native-token"}
BASE = "/api/research/lab"


class ResearchModelRouteTests(unittest.TestCase):
    def setUp(self):
        fixture = auth_fixture.CodexAuthTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.ns = fixture.ns
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.lab = ResearchLabStore(self.root / "lab")
        self.snapshot = self.lab.create(numeric_request())
        self.calls, self.delay, self.cleaned = [], .02, False
        self.provider_started = threading.Event()
        self.options = lambda: {"executable": "fake-native", "env": {}}
        async def generate(role, packet, on_event, **kwargs):
            self.calls.append((role, copy.deepcopy(packet)))
            self.provider_started.set()
            try:
                await asyncio.sleep(self.delay)
                return envelope(output_for(role, packet))
            finally:
                self.cleaned = True
        self.generate = generate
        self.app = self.make_app(self.root / "jobs")
        self.client = self.enterContext(TestClient(self.app))
        self.prefix = BASE + "/" + self.snapshot["id"] + "/model-jobs"

    def make_app(self, storage_root, dependency_factory=None):
        app = FastAPI()
        app.middleware("http")(self.ns["require_agent_token"])
        app.include_router(create_research_model_router(
            storage_root=storage_root, lab_root=self.root / "lab",
            authorize=self.ns["require_native_admin_control"],
            native_options=lambda: self.options(), generate=self.generate, dependency_factory=dependency_factory))
        return app

    def seed_planned(self, key="seed"):
        store = ResearchModelJobs(self.root / "jobs", generate=self.generate)
        return store.prepare({**self.request(idempotency_key=key), "campaign_id": self.snapshot["id"]}, resolve_snapshot=self.lab.get)

    def request(self, **changes):
        return {"role": "planner", "expected_revision": self.snapshot["revision"], "idempotency_key": "native-plan", **changes}

    def create(self, **changes):
        response = self.client.post(self.prefix, headers=NATIVE, json=self.request(**changes))
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_native_auth_body_validation_and_read_only_listing(self):
        self.assertFalse((self.root / "jobs").exists())
        for headers, status in [({}, 401), ({**NATIVE, "Origin": "https://example.invalid"}, 403),
                                ({**NATIVE, "Sec-Fetch-Mode": "cors"}, 403)]:
            response = self.client.post(self.prefix, headers=headers, content="invalid")
            self.assertEqual(response.status_code, status)
            self.assertFalse((self.root / "jobs").exists())
        response = self.client.post(self.prefix, headers=NATIVE, json={**self.request(), "packet": {}})
        self.assertEqual(response.status_code, 400)
        response = self.client.post(self.prefix, headers={**NATIVE, "Content-Type": "application/json"}, content="x" * 4097)
        self.assertEqual(response.status_code, 413)
        listing = self.client.get(self.prefix, headers=NATIVE)
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json()["items"], [])
        self.assertEqual(self.calls, [])

    def test_explicit_job_wait_exact_replay_and_scoped_packet(self):
        item = self.create()
        self.assertEqual(item["attempt_count"], 1)
        path = self.prefix + "/" + item["id"]
        done = self.client.get(path + "/wait", headers=NATIVE)
        self.assertEqual(done.status_code, 200, done.text)
        done = done.json()
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["output"]["layer"], "model_interpretation")
        self.assertEqual(self.create(), done)
        self.assertEqual(len(self.calls), 1)
        packet = self.client.get(path + "/artifacts/" + done["packet_ref"], headers=NATIVE)
        self.assertEqual(packet.status_code, 200)
        self.assertEqual(packet.json()["content"]["campaign_id"], self.snapshot["id"])
        self.assertEqual(packet.headers["cache-control"], "no-store")
        other = self.lab.create(numeric_request("other"))
        wrong = BASE + "/" + other["id"] + "/model-jobs/" + item["id"]
        for suffix in ["", "/wait", "/artifacts/" + done["packet_ref"]]:
            self.assertEqual(self.client.get(wrong + suffix, headers=NATIVE).status_code, 404)
        self.assertEqual(self.client.post(wrong + "/cancel", headers=NATIVE, json={}).status_code, 404)
        self.assertEqual(self.client.post(wrong + "/start", headers=NATIVE, json={}).status_code, 404)
        self.assertEqual(len(self.calls), 1)

    def test_pagination_reaches_all_cancelled_reservations_without_generation(self):
        store = ResearchModelJobs(self.root / "jobs", generate=self.generate)
        ids = []
        for index in range(53):
            item = store.prepare({**self.request(idempotency_key=f"seed-{index}"), "campaign_id": self.snapshot["id"]}, resolve_snapshot=self.lab.get)
            ids.append(item["id"])
            store._cancel_planned(item["id"])
        first = self.client.get(self.prefix + "?limit=50", headers=NATIVE).json()
        second = self.client.get(self.prefix + f"?before={first['next_before']}&limit=3", headers=NATIVE).json()
        self.assertEqual(len(first["items"]), 50)
        self.assertEqual(len(second["items"]), 3)
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertIsNone(second["next_before"])
        self.assertEqual({x["id"] for x in first["items"] + second["items"]}, set(ids))
        self.assertEqual(self.calls, [])

    def test_pagination_validation_runs_after_native_guard_before_storage(self):
        for query in ["before=0", "before=-1", "before=1.5", "before=", "before=9223372036854775808",
                      "limit=0", "limit=51", "limit=true", "limit=01", "before=1&before=2", "limit=1&limit=2", "other=1"]:
            with self.subTest(query=query):
                response = self.client.get(self.prefix + "?" + query, headers=NATIVE)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertFalse((self.root / "jobs").exists())
        self.assertEqual(self.client.get(self.prefix + "?limit=bad").status_code, 401)
        self.assertEqual(self.client.get(self.prefix + "?limit=bad", headers={**NATIVE, "Origin": "https://example.invalid"}).status_code, 403)
        self.assertEqual(self.calls, [])

    def test_planned_job_requires_explicit_start_and_replay_survives_revision_and_config_change(self):
        item = self.seed_planned()
        path = self.prefix + "/" + item["id"]
        self.assertEqual(self.client.get(path, headers=NATIVE).json()["status"], "planned")
        self.assertEqual(self.client.get(path + "/wait", headers=NATIVE).json()["status"], "planned")
        self.assertEqual(self.create(idempotency_key="seed"), item)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.client.post(path + "/start", headers=NATIVE, json={"packet": {}}).status_code, 400)
        started = self.client.post(path + "/start", headers=NATIVE, json={})
        self.assertEqual(started.status_code, 200, started.text)
        done = self.client.get(path + "/wait", headers=NATIVE).json()
        self.assertEqual(done["status"], "completed")
        self.lab.decide(self.snapshot["id"], {"idempotency_key": "revise", "expected_revision": 1,
            "kind": "revise", "feedback": "New goal", "goal": "A revised descriptive goal"})
        def unavailable():
            raise OSError("Native config temporarily unavailable")
        self.options = unavailable
        self.assertEqual(self.client.post(path + "/start", headers=NATIVE, json={}).json(), done)
        self.assertEqual(self.create(idempotency_key="seed"), done)
        self.assertEqual(len(self.calls), 1)

    def test_start_of_stale_planned_job_never_calls_generator(self):
        item = self.seed_planned()
        self.lab.decide(self.snapshot["id"], {"idempotency_key": "revise", "expected_revision": 1,
            "kind": "revise", "feedback": "New goal", "goal": "A revised descriptive goal"})
        response = self.client.post(self.prefix + "/" + item["id"] + "/start", headers=NATIVE, json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "stale")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.client.get(self.prefix, headers=NATIVE).json()["quota"]["used"], 0)

    def test_core_revision_change_after_advisory_read_cannot_publish_old_recommendation(self):
        original = ResearchModelJobs._finish
        def changed_before_guard(controller, job_id, status, **kwargs):
            if status == "completed":
                current = self.lab.get(self.snapshot["id"])
                self.lab.decide(current["id"], {"expected_revision": current["revision"], "idempotency_key": "between-read-and-publish",
                    "kind": "revise", "feedback": "A new goal arrived at the publication boundary", "goal": "A changed research objective"})
            return original(controller, job_id, status, **kwargs)
        with patch.object(ResearchModelJobs, "_finish", changed_before_guard):
            item = self.create()
            result = self.client.get(self.prefix + "/" + item["id"] + "/wait", headers=NATIVE).json()
        self.assertEqual(result["status"], "stale")
        self.assertIsNone(result["output"])
        self.assertIsNotNone(result["raw_output_ref"])
        self.assertEqual(result["usage"]["total_tokens"], 31)
        self.assertEqual(result["campaign_revision"], 1)
        self.assertEqual(self.lab.get(self.snapshot["id"])["revision"], 2)

    def test_core_writer_is_serialized_until_model_publication_commit(self):
        entered, finished = threading.Event(), threading.Event()
        original = ResearchModelJobs._finish_guarded
        future = None
        with ThreadPoolExecutor(max_workers=1) as pool:
            def revise():
                entered.set()
                result = self.lab.decide(self.snapshot["id"], {"expected_revision": 1, "idempotency_key": "racing-core-write",
                    "kind": "revise", "feedback": "Core writer races publication", "goal": "Goal after the completed receipt"})
                finished.set()
                return result
            def local_commit(controller, job_id, status, **kwargs):
                nonlocal future
                if status == "completed":
                    future = pool.submit(revise)
                    self.assertTrue(entered.wait(2))
                    self.assertFalse(finished.wait(.05), "Core revision must stay stable through model-store commit")
                return original(controller, job_id, status, **kwargs)
            with patch.object(ResearchModelJobs, "_finish_guarded", local_commit):
                item = self.create()
                result = self.client.get(self.prefix + "/" + item["id"] + "/wait", headers=NATIVE).json()
            self.assertIsNotNone(future)
            revised = future.result(timeout=5)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["campaign_revision"], 1)
        self.assertEqual(revised["revision"], 2)

    def test_slow_native_configuration_does_not_block_other_http_reads(self):
        entered, release = threading.Event(), threading.Event()
        def slow_options():
            entered.set()
            release.wait(3)
            return {"executable": "fake-native", "env": {}}
        self.options = slow_options
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.client.post, self.prefix, headers=NATIVE, json=self.request())
            try:
                self.assertTrue(entered.wait(3))
                started = time.monotonic()
                listed = self.client.get(self.prefix, headers=NATIVE)
                self.assertLess(time.monotonic() - started, 1.0)
                self.assertEqual(listed.status_code, 200)
                self.assertEqual(listed.json()["items"][0]["status"], "planned")
            finally:
                release.set()
            self.assertEqual(pending.result(3).status_code, 200)

    def test_lifespan_owns_single_controller_and_joins_provider_on_exit(self):
        self.delay = 10
        app = self.make_app(self.root / "lifecycle-jobs")
        with patch("research_model_routes.ResearchModelJobs", wraps=ResearchModelJobs) as factory:
            with TestClient(app) as client:
                for _ in range(3):
                    self.assertEqual(client.get(self.prefix, headers=NATIVE).status_code, 200)
                started = client.post(self.prefix, headers=NATIVE, json=self.request())
                self.assertEqual(started.status_code, 200)
                self.assertEqual(factory.call_count, 1)
                self.assertTrue(self.provider_started.wait(3))
                self.assertFalse(self.cleaned)
            self.assertTrue(self.cleaned)
            # A new lifespan creates a new owner, without rerunning the old job.
            with TestClient(app) as client:
                listing = client.get(self.prefix, headers=NATIVE).json()
                self.assertEqual(listing["items"][0]["status"], "cancelled")
                self.assertEqual(factory.call_count, 2)
            self.assertEqual(len(self.calls), 1)

    def dependency_fixture(self, *, seed_planned=False):
        source = self.lab.create(source_request("dependency-source"))
        bridge = ResearchDependencies(self.root / "dependencies", self.root / "ideas", self.root / "lab")
        root = self.root / "dependency-jobs"
        request = {"role": "planner", "expected_revision": source["revision"], "idempotency_key": "dependency-job"}
        item = None
        if seed_planned:
            store = ResearchModelJobs(root, admission_guard=bridge.run_guard)
            item = store.prepare({**request, "campaign_id": source["id"]}, resolve_snapshot=self.lab.get)
        app = self.make_app(root, dependency_factory=lambda: bridge)
        client = self.enterContext(TestClient(app))
        prefix = BASE + "/" + source["id"] + "/model-jobs"
        def block():
            inputs = self.lab.artifact(source["id"], source["input_artifact"])["content"]
            inputs["sources"][0]["text"] += " A proposed source correction."
            bridge.prepare_lab_correction(source["id"], {"expected_revision": source["revision"], "idempotency_key": "pending-correction",
                "inputs": inputs, "reason": "Pending correction must block new interpretation"}, inputs["sources"][0]["id"])
        return client, prefix, request, block, item

    def test_pending_dependency_blocks_new_prepare_but_preserves_pure_replay(self):
        client, prefix, request, block, _ = self.dependency_fixture()
        item = client.post(prefix, headers=NATIVE, json=request).json()
        done = client.get(prefix + "/" + item["id"] + "/wait", headers=NATIVE).json()
        self.assertEqual(done["status"], "completed")
        block()
        self.assertEqual(client.post(prefix, headers=NATIVE, json=request).json(), done)
        rejected = client.post(prefix, headers=NATIVE, json={**request, "idempotency_key": "another-job"})
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(rejected.json()["detail"]["code"], "dependency_stale")
        listing = client.get(prefix, headers=NATIVE).json()
        self.assertEqual((len(listing["items"]), listing["quota"]["used"], listing["quota"]["reserved"]), (1, 1, 0))
        self.assertEqual(len(self.calls), 1)

    def test_pending_dependency_blocks_explicit_start_without_reserving_an_attempt(self):
        client, prefix, request, block, item = self.dependency_fixture(seed_planned=True)
        block()
        self.assertEqual(client.post(prefix, headers=NATIVE, json=request).json(), item)
        rejected = client.post(prefix + "/" + item["id"] + "/start", headers=NATIVE, json={})
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(rejected.json()["detail"]["code"], "dependency_stale")
        listing = client.get(prefix, headers=NATIVE).json()
        self.assertEqual((listing["items"][0]["status"], listing["quota"]["used"], listing["quota"]["reserved"]), ("planned", 0, 1))
        self.assertEqual(self.calls, [])

    def test_pending_dependency_after_dispatch_retains_native_receipt_without_publishing(self):
        self.delay = .2
        client, prefix, request, block, _ = self.dependency_fixture()
        item = client.post(prefix, headers=NATIVE, json=request).json()
        self.assertTrue(self.provider_started.wait(3))
        block()
        done = client.get(prefix + "/" + item["id"] + "/wait", headers=NATIVE).json()
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["error"]["code"], "dependency_stale")
        self.assertIsNone(done["output"])
        self.assertIsNotNone(done["raw_output_ref"])
        self.assertEqual(done["usage"]["total_tokens"], 31)
        self.assertEqual(len(self.calls), 1)

    def test_bounded_wait_does_not_cancel_provider_and_cancel_joins(self):
        self.delay = 10
        item = self.create()
        path = self.prefix + "/" + item["id"]
        with patch("research_model_routes.WAIT_SECONDS", .08):
            started = time.monotonic()
            waited = self.client.get(path + "/wait", headers=NATIVE)
            elapsed = time.monotonic() - started
        self.assertEqual(waited.json()["status"], "running")
        self.assertGreaterEqual(elapsed, .07)
        self.assertFalse(self.cleaned)
        cancelled = self.client.post(path + "/cancel", headers=NATIVE, json={})
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertTrue(self.cleaned)
        self.assertEqual(self.create()["status"], "cancelled")
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
