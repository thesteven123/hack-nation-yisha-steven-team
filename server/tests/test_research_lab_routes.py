"""Actual native guards, durable campaign operations and verified Idea imports."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from idea_lab import IdeaStore, STAGES
from research_lab_routes import create_research_lab_router
from research_lab_inspection import verify_bundle, restore_campaign
from research_lab import ResearchLabStore
from tests import test_codex_auth_isolated as auth_fixture
from tests.test_idea_lab import brief, generated
from tests.test_research_lab import source_request
from tests.test_research_hypotheses import hypothesis_request

NATIVE = {"X-AgentsDock-Token": "synthetic-native-token"}
BASE = "/api/research/lab"


def creation(key="transport-numeric"):
    return {"idempotency_key": key, "entry": "hypothesis", "brief": {
        "goal": "Describe the supplied paired differences", "hypothesis": "Paired values increase",
        "success_criteria": "Inspect the effect interval and sensitivity with the same frozen data",
        "constraints": "Local analysis only; not a causal estimate or independent replication"},
        "adapter_id": "paired_numeric", "inputs": {"baseline": [1, 2, 3, 4], "treatment": [4, 5, 6, 7],
                                                   "unit": "units", "minimum_effect": 1},
        "budget": {"max_actions": 3, "max_rounds": 3}}


class ResearchLabRouteTests(unittest.TestCase):
    def setUp(self):
        fixture = auth_fixture.CodexAuthTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.ns = fixture.ns
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "lab"
        self.ideas = Path(directory.name) / "ideas"
        self.client = self.make_client()

    def make_client(self):
        app = FastAPI()
        app.middleware("http")(self.ns["require_agent_token"])
        app.include_router(create_research_lab_router(storage_root=self.root, idea_root=self.ideas,
                                                     authorize=self.ns["require_native_admin_control"]))
        client = TestClient(app)
        self.addCleanup(client.close)
        return client

    def post(self, path, value, status=200):
        response = self.client.post(BASE + path, headers=NATIVE, json=value)
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def test_authorization_before_store_and_body(self):
        for headers, suffix, status in [({}, "", 401), ({}, "?token=synthetic-native-token", 401),
                ({**NATIVE, "Origin": "https://example.invalid"}, "", 403),
                ({**NATIVE, "Sec-Fetch-Mode": "cors"}, "", 403)]:
            with self.subTest(headers=headers):
                response = self.client.post(BASE + suffix, headers=headers, content="bad json")
                self.assertEqual(response.status_code, status)
                self.assertFalse(self.root.exists())
                self.assertFalse(self.ideas.exists())
        self.assertEqual(self.client.options(BASE, headers=NATIVE).status_code, 403)
        self.ns["AGENT_TOKEN"] = ""
        self.assertEqual(self.client.get(BASE + "/capabilities", headers=NATIVE).status_code, 503)
        self.assertFalse(self.root.exists())

    def test_two_rounds_selection_restart_and_scoped_artifacts(self):
        result = self.post("", creation())
        path = "/" + result["id"]
        self.assertEqual(result["budget"]["used_actions"], 0)
        self.assertGreaterEqual(len(result["current_plan"]["candidates"]), 2)
        selected = self.post(path + "/decision", {"expected_revision": result["revision"],
            "idempotency_key": "choose", "kind": "select", "feedback": "Use the paired interval first",
            "selected_action_id": result["current_plan"]["candidates"][0]["id"]})
        self.assertEqual(selected["current_plan"]["selection_origin"], "human")
        request = {"expected_revision": selected["revision"], "idempotency_key": "run-1"}
        first = self.post(path + "/run", request)
        self.assertEqual(first["budget"]["used_actions"], 1)
        self.assertEqual(first["review"]["next_action"], "continue")
        plan2 = self.post(path + "/continue", {"expected_revision": first["revision"], "idempotency_key": "next"})
        method = next(x for x in plan2["current_plan"]["candidates"] if x["id"] == plan2["current_plan"]["selected_action_id"])["method"]
        self.assertEqual(method, first["review"]["next_method"])
        final = self.post(path + "/run", {"expected_revision": plan2["revision"], "idempotency_key": "run-2"})
        self.assertEqual(len(final["rounds"]), 2)
        self.assertFalse(final["rounds"][1]["run"]["is_independent_replicate"])
        fresh = self.make_client()
        self.assertEqual(fresh.post(BASE + path + "/run", headers=NATIVE, json=request).json(), first)
        self.assertEqual(fresh.get(BASE + path, headers=NATIVE).json(), final)
        self.post(path + "/decision", {"expected_revision": 1, "idempotency_key": "stale", "kind": "stop", "feedback": "stale"}, 409)
        artifact = fresh.get(BASE + path + "/artifacts/" + final["input_artifact"], headers=NATIVE)
        self.assertEqual(artifact.status_code, 200)
        self.assertEqual(artifact.headers["cache-control"], "no-store")
        other = self.post("", {**creation("other"), "inputs": {**creation()["inputs"], "baseline": [7, 8, 9, 10]}})
        self.assertEqual(fresh.get(BASE + "/" + other["id"] + "/artifacts/" + final["input_artifact"], headers=NATIVE).status_code, 404)
        history = fresh.get(BASE + path + "/history", headers=NATIVE).json()
        self.assertGreaterEqual(len(history["items"]), 5)

    def complete_idea(self, projected=False):
        store = IdeaStore(self.ideas)
        item = store.create({"idempotency_key": "idea", "brief": brief()})
        item, _ = store.begin(item["id"], {"idempotency_key": "generate", "expected_revision": 1}, full_research=projected)
        if projected:
            # Reader base and model-facing projection differ. Only the model
            # packet matches the evidence hash; importing by bare ID is wrong.
            source = {**brief()["sources"][0], "text": brief()["sources"][0]["text"] + " Frozen role packet.",
                      "coverage": {"kind": "partial", "limitations": ["Methods missing"]}}
            store.work(item["id"], item["generation_id"], "literature", model=True)
            store.freeze_packet(item["id"], item["generation_id"], "literature", [source], {})
        for stage in STAGES:
            if projected and stage != "literature":
                store.work(item["id"], item["generation_id"], stage, model=True)
            item = store.save_stage(item["id"], item["generation_id"], stage, generated(stage))
        if projected:
            item = store.finish_research(item["id"], item["generation_id"], "ready_for_choice", "Ready for test")
        item = store.decision(item["id"], {"expected_revision": item["revision"], "kind": "select",
                                          "selected_id": "d1", "feedback": "Inspect this direction"})
        return store, item

    def test_hypothesis_revision_body_bound_preserves_legal_escaped_fields(self):
        item = self.post("", creation("large-hypothesis-revision"))
        value = hypothesis_request()
        value["candidates"][0]["statement"] = "\0" * 4000
        value["candidates"][0]["assumptions"] = ["\0" * 1000] * 6
        update = {field: "\0" * 7900 for field in
                  ("goal", "hypothesis", "success_criteria", "constraints", "feedback")}
        request = {"expected_revision": item["revision"], "idempotency_key": "large-set",
                   "kind": "revise", "hypothesis_set": value, **update}
        encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
        self.assertGreater(len(encoded), 128 * 1024)
        self.assertLess(len(encoded), 384 * 1024)
        path = "/" + item["id"]
        response = self.client.post(BASE + path + "/decision", headers={**NATIVE,
            "Content-Type": "application/json"}, content=encoded)
        self.assertEqual(response.status_code, 200, response.text)
        revised = response.json()
        self.assertEqual(revised["hypothesis_set"]["candidates"][0]["statement"],
                         value["candidates"][0]["statement"])
        for field in ("goal", "hypothesis", "success_criteria", "constraints"):
            self.assertEqual(revised["brief"][field], update[field])
        oversized = {**request, "expected_revision": revised["revision"],
                     "idempotency_key": "over-limit", "feedback": "x" * (384 * 1024)}
        self.post(path + "/decision", oversized, 413)
        self.assertEqual(self.client.get(BASE + path, headers=NATIVE).json(), revised)

    def test_native_inspection_pagination_protocol_and_complete_recovery(self):
        first = self.post("", creation("inspection-one"))
        second = self.post("", creation("inspection-two"))
        listing = self.client.get(BASE + "?limit=1", headers=NATIVE).json()
        self.assertEqual(listing["items"][0]["id"], second["id"])
        self.assertTrue(listing["has_more"])
        page2 = self.client.get(BASE, headers=NATIVE,
            params={"limit": 1, "before": listing["next_cursor"]}).json()
        self.assertEqual([x["id"] for x in page2["items"]], [first["id"]])
        self.assertFalse(page2["has_more"])
        path = "/" + first["id"]
        executed = self.post(path + "/run", {"expected_revision": first["revision"], "idempotency_key": "inspection-run"})
        history = self.client.get(BASE + path + "/history?limit=1", headers=NATIVE).json()
        self.assertTrue(history["has_more"])
        older = self.client.get(BASE + path + "/history", headers=NATIVE,
            params={"limit": 1, "before": history["next_cursor"]}).json()
        self.assertFalse(older["has_more"])
        self.assertEqual(self.client.get(BASE + "/" + second["id"] + "/history", headers=NATIVE,
            params={"before": history["next_cursor"]}).status_code, 400)
        for endpoint in ["/protocols/planning-v0.5", path + "/export"]:
            self.assertEqual(self.client.get(BASE + endpoint).status_code, 401)
        protocol = self.client.get(BASE + "/protocols/planning-v0.5", headers=NATIVE)
        self.assertEqual(protocol.status_code, 200)
        self.assertEqual(protocol.json()["source"]["page_id"], "page_d28993a86bbc8191ab71c7b2d4ea397a")
        bundle = self.client.get(BASE + path + "/export", headers=NATIVE)
        self.assertEqual(bundle.status_code, 200, bundle.text)
        self.assertEqual(bundle.headers["cache-control"], "no-store")
        self.assertEqual(verify_bundle(bundle.json())["runs"], 1)
        restored_root = self.root.parent / "restored"
        restored = restore_campaign(bundle.json(), restored_root)
        self.assertTrue(restored["replay_not_executed"])
        self.assertEqual(ResearchLabStore(restored_root).get(first["id"]), executed)

    def test_idea_seed_import_requires_real_choice_and_exact_frozen_source(self):
        store, item = self.complete_idea(projected=True)
        seed = self.client.get(BASE + "/idea-seed/" + item["id"], headers=NATIVE)
        self.assertEqual(seed.status_code, 200, seed.text)
        seed = seed.json()
        self.assertTrue(seed["inputs"]["sources"][0]["text"].endswith("Frozen role packet."))
        source = seed["inputs"]["sources"][0]
        self.assertEqual(source["provenance"]["source_hash"], hashlib.sha256(source["text"].encode()).hexdigest())
        body = {**creation("import"), **{k: seed[k] for k in ("origin", "brief", "inputs")}, "adapter_id": "source_evidence", "entry": "goal"}
        forged = copy.deepcopy(body)
        forged["inputs"]["sources"][0]["text"] = "forged source"
        self.post("", forged, 409)
        imported = self.post("", body)
        self.assertEqual(imported["origin"], seed["origin"])
        store.decision(item["id"], {"expected_revision": item["revision"], "kind": "defer", "selected_id": None, "feedback": "Changed choice"})
        self.assertEqual(self.post("", body), imported)  # old replay remains exact
        self.post("", {**body, "idempotency_key": "new-stale-origin"}, 409)

    def test_same_direction_with_new_human_feedback_invalidates_old_seed(self):
        store, item = self.complete_idea()
        seed = self.client.get(BASE + "/idea-seed/" + item["id"], headers=NATIVE).json()
        self.assertEqual(seed["origin"]["decision_revision"], item["revision"])
        changed = store.decision(item["id"], {"expected_revision": item["revision"], "kind": "select", "selected_id": "d1",
                                               "feedback": "New requirement: inspect opposing evidence first"})
        body = {**source_request("stale-same-id"), **{k: seed[k] for k in ("origin", "brief", "inputs")}}
        self.post("", body, 409)
        current = self.client.get(BASE + "/idea-seed/" + item["id"], headers=NATIVE).json()
        self.assertEqual(current["origin"]["decision_revision"], changed["revision"])
        self.post("", {**body, **{k: current[k] for k in ("origin", "brief", "inputs")}})

    def test_standalone_client_cannot_claim_authoritative_idea_provenance(self):
        body = source_request("forged-standalone")
        body["inputs"]["sources"][0]["provenance"] = {"kind": "idea_frozen_packet", "verification": "source_verified",
            "source_hash": "a" * 64, "session_id": "nonexistent"}
        item = self.post("", body)
        artifact = self.client.get(BASE + "/" + item["id"] + "/artifacts/" + item["input_artifact"], headers=NATIVE).json()
        provenance = artifact["content"]["sources"][0]["provenance"]
        self.assertNotEqual(provenance["kind"], "idea_frozen_packet")
        self.assertEqual(provenance["verification"], "not_source_verified")

    def test_all_required_protocols_resolve_to_explicit_summaries(self):
        capabilities = self.client.get(BASE + "/capabilities", headers=NATIVE).json()
        identifiers = {p for adapter in capabilities["adapters"] for p in adapter["required_protocols"]}
        self.assertIn("literature-cache-v0.5", identifiers)
        for identifier in identifiers:
            response = self.client.get(BASE + "/protocols/" + identifier, headers=NATIVE)
            self.assertEqual(response.status_code, 200, identifier)
            self.assertFalse(response.json()["is_full_source"])
            self.assertTrue(response.json()["content"])

    def test_six_source_seed_discloses_subset_and_retains_notice_at_create(self):
        store = IdeaStore(self.ideas)
        item = store.create({"idempotency_key": "six-sources", "brief": brief()})
        item, _ = store.begin(item["id"], {"idempotency_key": "generate", "expected_revision": item["revision"]}, full_research=True)
        sources = [{"id": f"s{i}", "title": f"Source {i}", "uri": f"fixture:{i}", "text": f"Exact claim from source {i}.",
                    "coverage": {"kind": "partial", "limitations": ["Fixture excerpt"]}} for i in range(6)]
        store.work(item["id"], item["generation_id"], "literature", model=True)
        store.freeze_packet(item["id"], item["generation_id"], "literature", sources, {})
        for stage in STAGES:
            if stage != "literature":
                store.work(item["id"], item["generation_id"], stage, model=True)
            answer = generated(stage)
            if stage == "literature":
                card = answer["output"]["evidence"][0]
                answer["output"]["evidence"] = [{**card, "id": f"e{i}", "source_id": source["id"], "quote": source["text"]}
                                                 for i, source in enumerate(sources)]
            if stage == "ideas":
                for direction in answer["output"]["directions"]:
                    direction["evidence_ids"] = [f"e{i}" for i in range(6)]
            item = store.save_stage(item["id"], item["generation_id"], stage, answer)
        item = store.finish_research(item["id"], item["generation_id"], "ready_for_choice", "Synthetic acceptance")
        item = store.decision(item["id"], {"expected_revision": item["revision"], "kind": "select", "selected_id": "d1", "feedback": "Inspect sources"})
        response = self.client.get(BASE + "/idea-seed/" + item["id"], headers=NATIVE)
        self.assertEqual(response.status_code, 200, response.text)
        seed = response.json()
        coverage = seed["import_coverage"]
        self.assertEqual((coverage["referenced_sources"], coverage["imported_sources"], coverage["omitted_sources"]), (6, 5, 1))
        self.assertEqual(coverage["omitted_source_ids"], ["s5"])
        self.assertEqual(coverage["omitted_evidence_ids"], ["e5"])
        self.assertFalse(coverage["complete"])
        self.assertIn("may omit counterevidence", seed["brief"]["constraints"])
        body = {**source_request("subset-import"), **{k: seed[k] for k in ("origin", "brief", "inputs")}}
        body["brief"]["constraints"] = "Local source inspection only"
        body["inputs"]["sources"][0]["provenance"]["import_coverage"]["complete"] = True
        result = self.post("", body)
        self.assertIn("Only 5/6", result["brief"]["constraints"])
        artifact = self.client.get(BASE + "/" + result["id"] + "/artifacts/" + result["input_artifact"], headers=NATIVE).json()
        self.assertFalse(artifact["content"]["sources"][0]["provenance"]["import_coverage"]["complete"])

    def test_invalid_requests_do_not_create_campaigns(self):
        self.assertEqual(self.client.post(BASE, headers=NATIVE, content="{}").status_code, 415)
        self.assertEqual(self.client.post(BASE, headers=NATIVE, json=[]).status_code, 400)
        for origin in ["x", {}, {"kind": "idea", "session_id": 3}]:
            self.post("", {**creation(), "origin": origin}, 400)
        response = self.client.post(BASE, headers={**NATIVE, "Content-Type": "application/json"}, content="x" * (2 * 1024 * 1024 + 1))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.client.get(BASE, headers=NATIVE).json()["items"], [])


if __name__ == "__main__":
    unittest.main()
