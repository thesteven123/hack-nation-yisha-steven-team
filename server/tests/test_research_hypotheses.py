import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from research_hypotheses import HypothesisError, freeze_set, validate_set
from research_lab import LabError, ResearchLabStore
from research_lab_inspection import ResearchLabInspector, restore_campaign, verify_bundle
from research_model_jobs import build_packet
from tests.test_research_lab import numeric_request, source_request


def hypothesis_request():
    return {"candidates": [{"id": "h1", "statement": "The supplied difference is due to an intervention",
        "assumptions": ["The units are comparable"], "scope": "The supplied dataset only",
        "supporting_evidence": [{"kind": "input"}], "opposing_evidence": [],
        "predictions": ["The treatment-minus-baseline difference is positive"],
        "weakening_conditions": ["The apparent difference disappears under a justified sensitivity check"]}],
        "open_alternative": "A measurement artifact or unmeasured process may explain the difference"}


class HypothesisHelperTests(unittest.TestCase):
    def freeze(self, request, **kwargs):
        context = {"campaign_id": "campaign_test", "revision": 1, "brief_revision": 1, "introduced_at": "2026-10-04T00:00:00Z",
            "input_artifact": "a" * 64, "inputs": {}, "rounds": []}
        return freeze_set(request, **{**context, **kwargs})

    def test_optional_exploratory_and_no_invented_fields(self):
        self.assertIsNone(validate_set(None))
        self.assertIsNone(self.freeze(None))
        value = hypothesis_request()
        original = copy.deepcopy(value)
        record = self.freeze(value)
        self.assertEqual(value, original)
        self.assertEqual(record["revision"], 1)
        self.assertFalse(record["post_outcome"])
        self.assertEqual(record["scientific_status"], "not_validated")
        self.assertEqual(record["visible_evidence_refs"], [{"kind": "input", "artifact": "a" * 64}])
        self.assertEqual(record["candidates"][0]["supporting_evidence"], record["visible_evidence_refs"])

    def test_existing_source_and_observation_are_resolved_without_claiming_support(self):
        request = hypothesis_request()
        request["candidates"][0]["supporting_evidence"] = [{"kind": "source", "source_id": "s1"}]
        request["candidates"][0]["opposing_evidence"] = [{"kind": "observation", "run_id": "r1"}]
        record = self.freeze(request, revision=2, brief_revision=3, previous_artifact="d" * 64,
            inputs={"sources": [{"id": "s1", "text": "β", "coverage": "excerpt", "missing_sections": ["Appendix"]}]},
            rounds=[{"run": {"id": "r1", "observation_artifact": "b" * 64, "input_artifact": "c" * 64, "status": "completed"},
                     "plan": {"goal_revision": 2}, "qc": {"passed": False}}])
        self.assertTrue(record["post_outcome"])
        self.assertEqual(record["previous_artifact"], "d" * 64)
        source = record["candidates"][0]["supporting_evidence"][0]
        observation = record["candidates"][0]["opposing_evidence"][0]
        self.assertEqual(source["source_hash"], hashlib.sha256("β".encode()).hexdigest())
        self.assertEqual(source["missing_sections"], ["Appendix"])
        self.assertFalse(observation["qc_passed"])
        self.assertEqual(observation["input_artifact"], "c" * 64)
        self.assertNotIn("supported", record.values())

    def test_missing_or_foreign_refs_cannot_be_declared_visible(self):
        for selector in ({"kind": "source", "source_id": "foreign"}, {"kind": "observation", "run_id": "foreign"}):
            request = hypothesis_request()
            request["candidates"][0]["supporting_evidence"] = [selector]
            with self.subTest(selector=selector), self.assertRaises(HypothesisError):
                self.freeze(request)

    def test_no_client_hash_timestamp_version_or_status_authority(self):
        for key, value in (("introduced_at", "yesterday"), ("revision", 99), ("visible_evidence_refs", []), ("scientific_status", "supported")):
            request = {**hypothesis_request(), key: value}
            with self.subTest(key=key), self.assertRaises(HypothesisError):
                validate_set(request)
        request = hypothesis_request()
        request["candidates"][0]["supporting_evidence"][0]["artifact"] = "forged"
        with self.assertRaises(HypothesisError):
            validate_set(request)

    def test_unique_ids_reference_counts_and_required_discriminating_fields(self):
        cases = []
        request = hypothesis_request(); request["candidates"] *= 2; cases.append(request)
        for field in ("predictions", "weakening_conditions"):
            request = hypothesis_request(); request["candidates"][0][field] = []; cases.append(request)
        request = hypothesis_request(); request["candidates"][0]["supporting_evidence"] *= 2; cases.append(request)
        request = hypothesis_request(); request["open_alternative"] = ""; cases.append(request)
        request = hypothesis_request(); request["candidates"][0]["id"] = "invalid id"; cases.append(request)
        for request in cases:
            with self.subTest(request=request), self.assertRaises(HypothesisError):
                validate_set(request)

    def test_utf8_and_aggregate_json_bounds_include_escaping(self):
        request = hypothesis_request()
        request["candidates"][0]["statement"] = "测" * 1400
        with self.assertRaises(HypothesisError):
            validate_set(request)
        request = hypothesis_request()
        request["candidates"][0]["assumptions"] = ["\0" * 1000] * 8
        request["candidates"][0]["predictions"] = ["\0" * 1000] * 6
        with self.assertRaisesRegex(HypothesisError, "64 KiB"):
            validate_set(request)

    def test_resolved_reference_metadata_is_bounded_without_silent_truncation(self):
        request = hypothesis_request()
        request["candidates"] = [{**copy.deepcopy(request["candidates"][0]), "id": f"h{i}",
            "supporting_evidence": [{"kind": "source", "source_id": f"s{j}"} for j in range(5)],
            "opposing_evidence": [{"kind": "source", "source_id": f"s{j}"} for j in range(5)]} for i in range(4)]
        sources = [{"id": f"s{i}", "text": "x", "coverage": "excerpt", "missing_sections": ["\0" * 256] * 30} for i in range(5)]
        with self.assertRaisesRegex(HypothesisError, "512 KiB"):
            self.freeze(request, inputs={"sources": sources})


class HypothesisStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ResearchLabStore(self.root / "lab")

    def create(self):
        request = numeric_request()
        request["brief"]["hypothesis"] = ""
        request["hypothesis_set"] = hypothesis_request()
        return self.store.create(request)

    def mutate(self, item, method, key, **extra):
        return getattr(self.store, method)(item["id"], {"expected_revision": item["revision"], "idempotency_key": key, **extra})

    def test_set_only_hypothesis_entry_freezes_refs_in_plan_run_and_model(self):
        item = self.create()
        digest = item["hypothesis_set_artifact"]
        original = self.store.artifact(item["id"], digest)["content"]
        self.assertEqual(item["hypothesis_set"], original)
        self.assertEqual(item["hypothesis_set_status"], "current_context")
        spec = self.store.artifact(item["id"], item["current_plan"]["candidates"][0]["spec_artifact"])["content"]
        self.assertEqual(spec["hypothesis_set_artifact"], digest)
        self.assertIn(digest, spec["task_packet"]["input_refs"])
        self.assertIn("research_hypotheses.py", spec["execution_environment"]["files_sha256"])
        self.assertEqual(build_packet(item, "planner")["hypothesis_set"], original)
        done = self.mutate(item, "run", "first")
        run = done["rounds"][0]
        self.assertEqual(run["run"]["hypothesis_set_artifact"], digest)
        self.assertIn(digest, run["analysis"]["input_refs"])
        self.assertIn(digest, run["review"]["input_refs"])
        self.assertEqual(run["observation"]["data"]["mean_difference"], 3)
        self.assertEqual(original["scientific_status"], "not_validated")
        self.assertEqual(self.store.artifact(item["id"], digest)["content"], original)

    def test_post_outcome_set_revision_records_prior_visibility_without_rewriting_round(self):
        item = self.create()
        old_digest = item["hypothesis_set_artifact"]
        item = self.mutate(item, "run", "first")
        old_round = copy.deepcopy(item["rounds"][0])
        new = hypothesis_request()
        new["candidates"][0]["opposing_evidence"] = [{"kind": "observation", "run_id": old_round["run"]["id"]}]
        changed = self.mutate(item, "decide", "set-v2", kind="revise", feedback="Treat the new explanation as exploratory", hypothesis_set=new)
        record = changed["hypothesis_set"]
        self.assertEqual(record["revision"], 2)
        self.assertEqual(record["brief_revision"], changed["brief"]["revision"])
        self.assertEqual(record["previous_artifact"], old_digest)
        self.assertTrue(record["post_outcome"])
        self.assertEqual(record["candidates"][0]["opposing_evidence"][0]["artifact"], old_round["run"]["observation_artifact"])
        self.assertEqual(changed["rounds"][0], old_round)
        self.assertTrue(all(c["status"] == "needs_revalidation" for c in changed["claims"]))
        self.assertTrue(all(c["research_mode"] == "post_outcome_exploratory" for c in changed["current_plan"]["candidates"]))
        self.assertFalse(self.store.artifact(item["id"], old_digest)["content"]["post_outcome"])
        replay = self.store.decide(item["id"], {"expected_revision": item["revision"], "idempotency_key": "set-v2", "kind": "revise",
            "feedback": "Treat the new explanation as exploratory", "hypothesis_set": new})
        self.assertEqual(replay, changed)
        self.assertEqual(ResearchLabStore(self.store.root).get(item["id"]), changed)

    def test_omitted_set_preserves_introduction_but_marks_changed_context_for_revalidation(self):
        item = self.create()
        original = copy.deepcopy(item["hypothesis_set"])
        digest = item["hypothesis_set_artifact"]
        item = self.mutate(item, "decide", "new-goal", kind="revise", goal="Inspect the descriptive limitation", feedback="New focus")
        self.assertEqual(item["hypothesis_set"], original)
        self.assertEqual(item["hypothesis_set_artifact"], digest)
        self.assertEqual(item["hypothesis_set_status"], "needs_revalidation")
        item = self.mutate(item, "correct_inputs", "correct", inputs={**numeric_request()["inputs"], "treatment": [3, 4, 5, 6, 7]}, reason="Correct input")
        self.assertEqual(item["hypothesis_set"], original)
        self.assertEqual(item["hypothesis_set_status"], "needs_revalidation")
        self.assertEqual(build_packet(item, "planner")["hypothesis_set_status"], "needs_revalidation")

    def test_goal_entry_can_add_and_remove_set_without_fabricating_legacy_records(self):
        item = self.store.create(source_request())
        self.assertIsNone(item["hypothesis_set"])
        item = self.mutate(item, "decide", "add", kind="revise", feedback="Explicit new hypothesis", hypothesis_set=hypothesis_request())
        old_digest = item["hypothesis_set_artifact"]
        item = self.mutate(item, "decide", "remove", kind="revise", feedback="Return to exploration", hypothesis_set=None)
        self.assertIsNone(item["hypothesis_set"])
        self.assertIsNone(item["hypothesis_set_artifact"])
        self.assertEqual(item["hypothesis_set_status"], "not_specified")
        self.assertEqual(item["hypothesis_set_revision"], 2)
        self.assertEqual(self.store.artifact(item["id"], old_digest)["content"]["revision"], 1)

    def test_invalid_or_foreign_evidence_rolls_back_whole_revision(self):
        item = self.create()
        other = self.store.create(numeric_request("other"))
        other = self.mutate(other, "run", "other-run")
        new = hypothesis_request()
        new["candidates"][0]["supporting_evidence"] = [{"kind": "observation", "run_id": other["rounds"][0]["run"]["id"]}]
        with self.assertRaises(LabError):
            self.mutate(item, "decide", "foreign", kind="revise", feedback="Wrong scope", hypothesis_set=new)
        self.assertEqual(self.store.get(item["id"]), item)
        with self.assertRaises(LabError):
            self.mutate(item, "decide", "empty", kind="revise", feedback="Cannot empty hypothesis entry", hypothesis_set=None)
        self.assertEqual(self.store.get(item["id"]), item)
        with self.assertRaises(LabError):
            self.mutate(item, "decide", "wrong-kind", kind="defer", feedback="No implicit revision", hypothesis_set=new)
        self.assertEqual(self.store.get(item["id"]), item)

    def test_additive_legacy_read_does_not_backfill_hypothesis_set(self):
        item = self.store.create(numeric_request())
        for field in ("hypothesis_set", "hypothesis_set_artifact", "hypothesis_set_revision", "hypothesis_set_status"):
            item.pop(field)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE campaigns SET payload=? WHERE id=?", (json.dumps(item), item["id"]))
        before = hashlib.sha256(self.store.path.read_bytes()).hexdigest()
        self.assertNotIn("hypothesis_set", self.store.get(item["id"]))
        self.assertEqual(hashlib.sha256(self.store.path.read_bytes()).hexdigest(), before)

    def test_new_set_export_restore_replays_without_new_observation(self):
        item = self.create()
        request = {"expected_revision": item["revision"], "idempotency_key": "run"}
        item = self.store.run(item["id"], request)
        bundle = ResearchLabInspector(self.store.root).export_campaign(item["id"])
        self.assertTrue(verify_bundle(bundle)["verified"])
        restore_campaign(bundle, self.root / "restored")
        restored = ResearchLabStore(self.root / "restored")
        self.assertEqual(restored.run(item["id"], request), item)
        self.assertEqual(len(restored.get(item["id"])["rounds"]), 1)

    def test_export_verifies_removed_set_history_and_missing_set_references(self):
        item = self.store.create(source_request())
        item = self.mutate(item, "decide", "add", kind="revise", feedback="Declare", hypothesis_set=hypothesis_request())
        digest = item["hypothesis_set_artifact"]
        item = self.mutate(item, "decide", "clear", kind="revise", feedback="Explore", hypothesis_set=None)
        bundle = ResearchLabInspector(self.store.root).export_campaign(item["id"])
        self.assertTrue(verify_bundle(bundle)["verified"])
        bundle["data"]["artifacts"] = [a for a in bundle["data"]["artifacts"] if a["sha256"] != digest]
        def sha(value):
            return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        bundle["manifest"]["table_hashes"] = {k: sha(v) for k, v in bundle["data"].items()}
        bundle["bundle_sha256"] = sha({k: v for k, v in bundle.items() if k != "bundle_sha256"})
        with self.assertRaisesRegex(LabError, "artifact is missing"):
            verify_bundle(bundle)

    def test_large_legal_escaped_set_keeps_exact_content_within_detail_bound(self):
        value = hypothesis_request()
        value["candidates"][0]["statement"] = "\0" * 4000
        value["candidates"][0]["assumptions"] = ["\0" * 1000] * 6
        encoded = lambda obj: json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.assertGreater(len(encoded(value)), 60000)
        self.assertLessEqual(len(encoded(value)), 64 * 1024)
        item = self.create()
        update = {field: "\0" * 7900 for field in ("goal", "hypothesis", "success_criteria", "constraints", "feedback")}
        request = {"expected_revision": item["revision"], "idempotency_key": "large-set", "kind": "revise", "hypothesis_set": value, **update}
        self.assertGreater(len(encoded(request)), 128 * 1024)
        item = self.store.decide(item["id"], request)
        item = self.mutate(item, "run", "large-run")
        self.assertLessEqual(len(encoded(item)), 3 * 1024 * 1024)
        self.assertEqual(item["hypothesis_set"]["candidates"][0]["statement"], value["candidates"][0]["statement"])
        self.assertEqual(self.store.artifact(item["id"], item["hypothesis_set_artifact"])["content"], item["hypothesis_set"])


if __name__ == "__main__":
    unittest.main()
