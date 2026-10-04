"""Native HTTP correction admission using real isolated Idea/Lab/sidecar stores."""
import copy
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from research_dependencies import ResearchDependencies
from research_lab import ResearchLabStore
from research_lab_routes import create_research_lab_router
from tests.test_research_lab import source_request
from tests import test_research_lab_routes as route_fixture
from tests.test_research_lab_routes import BASE, NATIVE, creation


class ResearchDependencyRouteTests(unittest.TestCase):
    def setUp(self):
        self.fixture = route_fixture.ResearchLabRouteTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bridge = None
        self.path = self.fixture.root.parent / "dependencies"

        def dependencies():
            if self.bridge is None:
                self.bridge = ResearchDependencies(self.path, self.fixture.ideas, self.fixture.root)
            return self.bridge

        app = FastAPI()
        app.middleware("http")(self.fixture.ns["require_agent_token"])
        app.include_router(create_research_lab_router(storage_root=self.fixture.root,
            idea_root=self.fixture.ideas, authorize=self.fixture.ns["require_native_admin_control"],
            dependency_factory=dependencies))
        self.client = self.enterContext(TestClient(app))

    def post(self, suffix, body, status=200):
        response = self.client.post(BASE + suffix, headers=NATIVE, json=body)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def get(self, suffix):
        response = self.client.get(BASE + suffix, headers=NATIVE)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_shared_correction_blocks_new_work_but_not_saved_replay(self):
        _, idea = self.fixture.complete_idea(projected=True)
        seed = self.get("/idea-seed/" + idea["id"])
        request = {**source_request("source-a"), **{key: seed[key] for key in ("origin", "brief", "inputs")}}
        first = self.post("", request)
        second = self.post("", {**request, "idempotency_key": "source-b"})
        unrelated = self.post("", creation())
        a, b = "/" + first["id"], "/" + second["id"]
        run_request = {"expected_revision": second["revision"], "idempotency_key": "run-once"}
        observed = self.post(b + "/run", run_request)
        plan = self.post(b + "/continue", {"expected_revision": observed["revision"], "idempotency_key": "continue"})
        corrected = copy.deepcopy(seed["inputs"])
        corrected["sources"][0]["text"] += " Explicit synthetic source correction."
        correction = {"expected_revision": first["revision"], "idempotency_key": "shared-correction",
            "inputs": corrected, "reason": "Synthetic correction of this exact imported source version",
            "shared_source_id": corrected["sources"][0]["id"]}
        saved = self.post(a + "/correct-inputs", correction)
        self.assertEqual(self.post(a + "/correct-inputs", correction), saved)
        visible = self.get(b)
        self.assertFalse(visible["dependency_status"]["run_allowed"])
        self.assertEqual(visible["claims"][0]["status"], "needs_revalidation")
        self.post(b + "/run", {"expected_revision": plan["revision"], "idempotency_key": "blocked-run"}, 409)
        self.assertEqual(self.post(b + "/run", run_request), observed)
        self.assertEqual(len(self.get(b)["rounds"]), 1)
        original = ResearchLabStore(self.fixture.root).get(second["id"])
        self.assertNotEqual(original["claims"][0]["status"], "needs_revalidation")
        self.assertTrue(self.get("/" + unrelated["id"])["dependency_status"]["run_allowed"])
        sidecar = self.get(b + "/dependencies/export")
        self.assertEqual(sidecar["data"]["campaign_id"], second["id"])
        self.assertTrue(sidecar["data"]["corrections"])
        self.assertTrue(self.get(a)["dependency_status"]["run_allowed"])

    def test_explicit_reconciliation_auth_and_no_execution(self):
        item = self.fixture.post("", creation())
        path = "/" + item["id"]
        for headers, expected in [({}, 401), ({**NATIVE, "Origin": "https://example.invalid"}, 403)]:
            response = self.client.post(BASE + path + "/dependencies/reconcile", headers=headers, json={})
            self.assertEqual(response.status_code, expected)
            self.assertFalse(self.path.exists())
        before = self.get(path)
        self.assertEqual(before["dependency_status"]["state"], "unregistered")
        self.post(path + "/dependencies/reconcile", {"run": True}, 400)
        after = self.post(path + "/dependencies/reconcile", {})
        self.assertEqual(after["revision"], item["revision"])
        self.assertEqual(after["budget"]["used_actions"], 0)
        self.assertTrue(after["dependency_status"]["run_allowed"])
        self.assertEqual(after["rounds"], [])


if __name__ == "__main__":
    unittest.main()
