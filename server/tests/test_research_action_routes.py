"""Research transport with the production native guard and disposable state."""
import hashlib
import tempfile
from pathlib import Path
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from research_action_routes import create_research_action_router, PROTOCOLS
from tests import test_codex_auth_isolated as auth_fixture


NATIVE = {"X-AgentsDock-Token": "synthetic-native-token"}
INPUT = {
    "idempotency_key": "transport-reference-1",
    "goal": {"id": "reference", "revision": 1, "question": "Where is the reported number?",
             "completion_criterion": "Find the exact source span, retaining inference uncertainty."},
    "source": {"uri": "fixture://source/v1", "text": "Setup.\nThe value is 12.\nMore context.",
               "coverage": "excerpt", "missing_sections": ["appendix"]},
    "quote": "The value is 12.",
}


class ResearchRouteTests(unittest.TestCase):
    def setUp(self):
        fixture = auth_fixture.CodexAuthTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.ns = fixture.ns
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "research"
        self.client = self.make_client()

    def make_client(self):
        app = FastAPI()
        app.middleware("http")(self.ns["require_agent_token"])
        app.include_router(create_research_action_router(
            storage_root=self.root, authorize=self.ns["require_native_admin_control"]))
        client = TestClient(app)
        self.addCleanup(client.close)
        return client

    def test_authorization_precedes_parsing_and_storage(self):
        for headers, suffix, status in [
            ({}, "", 401),
            ({}, "?token=synthetic-native-token", 401),
            ({**NATIVE, "Origin": "https://example.invalid"}, "", 403),
            ({**NATIVE, "Sec-Fetch-Mode": "cors"}, "", 403),
        ]:
            with self.subTest(headers=headers):
                response = self.client.post("/api/research/actions" + suffix, headers=headers, content="bad json")
                self.assertEqual(response.status_code, status, response.text)
                self.assertFalse(self.root.exists())
        self.assertEqual(self.client.options("/api/research/actions", headers=NATIVE).status_code, 403)
        self.ns["AGENT_TOKEN"] = ""
        self.assertEqual(self.client.post("/api/research/actions", headers=NATIVE, json=INPUT).status_code, 503)
        self.assertFalse(self.root.exists())

    def test_freeze_run_restart_and_artifacts(self):
        response = self.client.post("/api/research/actions", headers=NATIVE, json=INPUT)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        planned = response.json()
        self.assertEqual(planned["status"], "planned")
        path = "/api/research/actions/" + planned["action_id"]
        stale = self.client.post(path + "/run", headers=NATIVE, json={"spec_hash": "stale"})
        self.assertEqual(stale.status_code, 409)
        completed = self.client.post(path + "/run", headers=NATIVE, json={"spec_hash": planned["spec_hash"]})
        self.assertEqual(completed.status_code, 200, completed.text)
        result = completed.json()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["spec"]["goal"], INPUT["goal"])
        fresh = self.make_client()
        replay = fresh.post(path + "/run", headers=NATIVE, json={"spec_hash": planned["spec_hash"]})
        self.assertEqual(replay.json(), result)
        self.assertEqual(fresh.get(path, headers=NATIVE).json(), result)
        digest = hashlib.sha256(INPUT["source"]["text"].encode()).hexdigest()
        source = fresh.get("/api/research/artifacts/" + digest, headers=NATIVE)
        self.assertEqual(source.content.decode(), INPUT["source"]["text"])
        self.assertEqual(source.headers["x-content-sha256"], digest)

    def test_conflict_body_validation_and_protocol_on_demand(self):
        self.client.post("/api/research/actions", headers=NATIVE, json=INPUT).raise_for_status()
        reply = self.client.post("/api/research/actions", headers=NATIVE, json={**INPUT, "quote": "different"})
        self.assertEqual(reply.status_code, 409, reply.text)
        self.assertEqual(self.client.post("/api/research/actions", headers=NATIVE, content="{}").status_code, 415)
        self.assertEqual(self.client.post("/api/research/actions", headers=NATIVE, json=[]).status_code, 400)
        oversized = self.client.post("/api/research/actions", headers=NATIVE, json={**INPUT,
            "source": {**INPUT["source"], "text": "x" * (1024 * 1024 + 1)}})
        self.assertEqual(oversized.status_code, 400, oversized.text[:200])
        for protocol_id in PROTOCOLS:
            section = self.client.get("/api/research/protocols/" + protocol_id, headers=NATIVE)
            self.assertEqual(section.status_code, 200)
            self.assertEqual(section.json()["id"], protocol_id)
            self.assertEqual(section.json()["representation"], "implementation_summary")
        self.assertEqual(self.client.get("/api/research/protocols/unknown", headers=NATIVE).status_code, 404)


if __name__ == "__main__":
    unittest.main()
