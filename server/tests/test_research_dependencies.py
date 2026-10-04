"""Real local stores, explicit corrections and recoverable outbox delivery."""
import copy
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from idea_lab import IdeaStore, STAGES
from research_dependencies import ResearchDependencies
from research_lab import LabError, ResearchLabStore
from research_lab_routes import IdeaImport
from tests.test_idea_lab import brief, generated
from tests.test_research_lab import source_request


class ResearchDependencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ideas = IdeaStore(self.root / "ideas")
        self.lab = ResearchLabStore(self.root / "lab")
        self.bridge = ResearchDependencies(self.root / "dependencies", self.root / "ideas", self.root / "lab")
        self.sequence = 0

    def idea(self, prior=None, *, text=None, finding=None, two_cards=False):
        self.sequence += 1
        key = str(self.sequence)
        if prior:
            item = self.ideas.followup(prior["id"], {"expected_revision": prior["revision"], "idempotency_key": "follow-" + key,
                "feedback": "Explicitly inspect the changed fixture record", "mode": "research"})
        else:
            item = self.ideas.create({"idempotency_key": "idea-" + key, "brief": brief()})
        item, _ = self.ideas.begin(item["id"], {"idempotency_key": "begin-" + key, "expected_revision": item["revision"]}, full_research=True)
        source = {**brief()["sources"][0], "text": text or brief()["sources"][0]["text"],
                  "coverage": {"kind": "partial", "limitations": ["Synthetic fixture excerpt"]}}
        self.ideas.work(item["id"], item["generation_id"], "literature", model=True)
        self.ideas.freeze_packet(item["id"], item["generation_id"], "literature", [source], {})
        for stage in STAGES:
            if stage != "literature":
                self.ideas.work(item["id"], item["generation_id"], stage, model=True)
            answer = generated(stage)
            if stage == "literature":
                answer["output"]["evidence"][0]["quote"] = source["text"].split(". ")[0] + "."
                if finding:
                    answer["output"]["evidence"][0]["finding"] = finding
                if two_cards:
                    card = copy.deepcopy(answer["output"]["evidence"][0])
                    card.update(id="e2", finding="A second selected interpretation")
                    answer["output"]["evidence"].append(card)
            if stage == "ideas" and two_cards:
                for direction in answer["output"]["directions"]:
                    direction["evidence_ids"] = ["e1", "e2"]
            item = self.ideas.save_stage(item["id"], item["generation_id"], stage, answer)
        item = self.ideas.finish_research(item["id"], item["generation_id"], "ready_for_choice", "Synthetic fixture only")
        return self.ideas.decision(item["id"], {"expected_revision": item["revision"], "kind": "select", "selected_id": "d1",
                                               "feedback": "Inspect this fixture"})

    def campaign(self, idea, key):
        importer = IdeaImport(self.ideas)
        seed = importer.seed(idea["id"])
        request = {**source_request(key), **{k: seed[k] for k in ("origin", "brief", "inputs")}}
        return self.lab.create(request, resolve_origin=importer.resolve)

    def execute(self, item, key="run"):
        with self.bridge.run_guard(item["id"]):
            return self.lab.run(item["id"], {"expected_revision": item["revision"], "idempotency_key": key})

    def correct(self, item, text, key="correct", *, title=None):
        inputs = self.lab.artifact(item["id"], item["input_artifact"])["content"]
        inputs["sources"][0]["text"] = text
        if title:
            inputs["sources"][0]["title"] = title
        return self.lab.correct_inputs(item["id"], {"expected_revision": item["revision"], "idempotency_key": key,
                                                   "reason": "Explicit source correction", "inputs": inputs})

    def event(self, old, new, key="correction", evidence="e1"):
        return self.bridge.record_idea_correction(old["id"], old["revision"], evidence, new["revision"], evidence,
                                                  idempotency_key=key, reason="Explicit fixture correction, truth is not assessed")

    def assert_code(self, code, call):
        with self.assertRaises(LabError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def counts(self):
        result = []
        for path, table in ((self.bridge.producer, "corrections"), (self.bridge.producer, "outbox"), (self.bridge.consumer, "marks")):
            with closing(sqlite3.connect(path)) as db:
                result.append(db.execute("SELECT count(*) FROM " + table).fetchone()[0])
        return result

    def correction_request(self, item, key="intent"):
        inputs = self.lab.artifact(item["id"], item["input_artifact"])["content"]
        source_id = inputs["sources"][0]["id"]
        inputs["sources"][0]["text"] = "The original source value has been corrected by the local researcher."
        return {"expected_revision": item["revision"], "idempotency_key": key, "reason": "Explicit shared source correction", "inputs": inputs}, source_id

    def test_explicit_event_blocks_before_delivery_without_mutating_core_or_budget(self):
        old = self.idea()
        a, b = self.campaign(old, "a"), self.campaign(old, "b")
        a = self.execute(a)
        self.bridge.register_campaign(b["id"])
        new = self.idea(old, text="Measured value decreased. The mechanism remains uncertain.")
        self.assertTrue(self.bridge.snapshot(b["id"])["run_allowed"])  # A newer same-URI version is not itself a correction.
        original = self.lab.get(a["id"])
        event = self.event(old, new)
        self.assertEqual(event["scope"], "source_version")
        self.assertEqual(event["scientific_validity"], "not_assessed")
        view = self.bridge.decorate(original)
        self.assertEqual(view["claims"][0]["status"], "needs_revalidation")
        self.assertEqual(view["claims"][0]["source_support"], original["claims"][0]["source_support"])
        self.assertEqual(self.lab.get(a["id"]), original)
        self.assertEqual(self.bridge.snapshot(b["id"])["pending_deliveries"], 1)
        self.assert_code("dependency_stale", lambda: self.execute(b))
        self.assertEqual(self.lab.get(b["id"])["budget"]["used_actions"], 0)
        self.assertEqual(self.bridge.drain(limit=1)["processed_targets"], 1)
        self.assertTrue(self.bridge.drain(limit=1)["has_more"] is False)
        self.assertEqual(self.bridge.snapshot(a["id"])["pending_deliveries"], 0)
        self.assertEqual(self.counts(), [1, 1, 2])
        self.assertEqual(self.bridge.drain()["processed_targets"], 0)

    def test_delivery_crash_after_consumer_commit_recovers_exactly_one_receipt(self):
        old = self.idea()
        item = self.campaign(old, "a")
        self.bridge.register_campaign(item["id"])
        new = self.idea(old, finding="The old interpretation needs revision")
        self.event(old, new)
        def fail(_):
            raise RuntimeError("injected after consumer commit")
        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.bridge.drain(after_publish=fail)
        self.assertEqual(self.counts(), [1, 1, 1])
        restarted = ResearchDependencies(self.root / "dependencies", self.root / "ideas", self.root / "lab")
        self.assertFalse(restarted.drain()["has_more"])
        self.assertEqual(self.counts(), [1, 1, 1])
        self.assertFalse(restarted.snapshot(item["id"])["run_allowed"])

    def test_late_registration_rescans_completed_outbox(self):
        old = self.idea()
        first, late = self.campaign(old, "first"), self.campaign(old, "late")
        self.bridge.register_campaign(first["id"])
        new = self.idea(old, finding="Correct the prior interpretation")
        self.event(old, new)
        self.bridge.drain()
        self.bridge.register_campaign(late["id"])
        self.assertFalse(self.bridge.snapshot(late["id"])["run_allowed"])
        self.assertFalse(self.bridge.drain()["has_more"])
        self.assertEqual(self.counts(), [1, 1, 2])

    def test_idempotent_declaration_conflict_and_invalid_record_refs(self):
        old = self.idea()
        new = self.idea(old, finding="A revised interpretation")
        event = self.event(old, new)
        self.assertEqual(self.event(old, new), event)
        self.assert_code("idempotency_conflict", lambda: self.bridge.record_idea_correction(old["id"], old["revision"], "e1", new["revision"], "e1",
            idempotency_key="correction", reason="A different declaration"))
        self.assert_code("not_found", lambda: self.bridge.record_idea_correction(old["id"], old["revision"], "e1", 2147483647, "e1",
            idempotency_key="missing", reason="Missing records"))
        self.assert_code("integrity_error", lambda: self.bridge.record_idea_correction(old["id"], old["revision"], "missing", new["revision"], "e1",
            idempotency_key="missing-evidence", reason="Missing evidence"))
        self.assertEqual(self.counts(), [1, 1, 0])

    def test_user_correction_rebinds_current_but_keeps_old_claims_stale(self):
        old = self.idea()
        item = self.execute(self.campaign(old, "a"))
        new = self.idea(old, text="Measured value decreased. The mechanism remains uncertain.")
        self.event(old, new)
        corrected = self.correct(item, "The supplied value was corrected by its local user.")
        self.assertFalse(self.bridge.snapshot(item["id"])["registered"])
        self.bridge.register_campaign(item["id"])
        status = self.bridge.snapshot(item["id"])
        self.assertTrue(status["run_allowed"])
        self.assertEqual(status["state"], "needs_revalidation")
        self.assertEqual(status["affected_claim_ids"], [item["claims"][0]["id"]])
        self.assertEqual(self.execute(corrected, "new-run")["budget"]["used_actions"], 2)

    def test_metadata_only_correction_cannot_clear_stale_source(self):
        old = self.idea()
        item = self.campaign(old, "a")
        self.bridge.register_campaign(item["id"])
        new = self.idea(old, finding="The interpretation was wrong")
        self.event(old, new)
        changed = self.correct(item, brief()["sources"][0]["text"], title="Renamed source")
        self.bridge.register_campaign(item["id"])
        self.assert_code("dependency_stale", lambda: self.execute(changed))

    def test_pre_index_metadata_edit_retains_original_scoped_cas_dependency(self):
        old = self.idea()
        item = self.campaign(old, "a")
        changed = self.correct(item, brief()["sources"][0]["text"], title="Edited before dependency service existed")
        new = self.idea(old, finding="The original interpretation needs revision")
        self.event(old, new)
        self.bridge.register_campaign(item["id"])
        self.assert_code("dependency_stale", lambda: self.execute(changed))

    def test_correction_to_nonrepresentative_selected_evidence_is_not_lost(self):
        old = self.idea(two_cards=True)
        item = self.campaign(old, "a")
        self.bridge.register_campaign(item["id"])
        new = self.idea(old, text="Measured value decreased. The mechanism remains uncertain.", two_cards=True)
        event = self.event(old, new, evidence="e2")
        self.assertFalse(self.bridge.snapshot(item["id"])["run_allowed"])
        self.assertEqual(event["old"]["evidence_id"], "e2")

    def test_lab_correction_uses_committed_cas_lineage_and_cross_campaign_scope(self):
        old = self.idea()
        a, b = self.campaign(old, "a"), self.campaign(old, "b")
        for item in (a, b):
            self.bridge.register_campaign(item["id"])
        new = self.correct(a, "This local source text was explicitly corrected.")
        sid = self.lab.artifact(a["id"], a["input_artifact"])["content"]["sources"][0]["id"]
        event = self.bridge.record_lab_source_correction(a["id"], a["revision"], new["revision"], sid,
                                                         idempotency_key="local-bridge", reason="Correct original quoted value")
        self.assertEqual(event["scope"], "source_version")
        self.assertEqual(event["source_action"]["event_id"], new["events"][-1]["id"])
        self.assertFalse(self.bridge.snapshot(b["id"])["run_allowed"])
        self.bridge.register_campaign(a["id"])
        self.assertTrue(self.bridge.snapshot(a["id"])["run_allowed"])
        self.assert_code("integrity_error", lambda: self.bridge.record_lab_source_correction(a["id"], 1, 3, sid,
            idempotency_key="non-adjacent", reason="Not an actual action"))

    def test_untrusted_same_url_and_same_text_are_local_only(self):
        request = source_request("a")
        a = self.lab.create(request)
        b = self.lab.create({**request, "idempotency_key": "b"})
        for item in (a, b):
            self.bridge.register_campaign(item["id"])
        new = self.correct(a, "Changed local user content.")
        event = self.bridge.record_lab_source_correction(a["id"], a["revision"], new["revision"], "s1",
                                                         idempotency_key="local", reason="Local record corrected")
        self.assertEqual(event["scope"], "local_input")
        self.assertTrue(self.bridge.snapshot(b["id"])["run_allowed"])
        self.bridge.register_campaign(a["id"])
        self.assertTrue(self.bridge.snapshot(a["id"])["run_allowed"])

    def test_projection_is_read_only_and_namespaces_are_separate(self):
        old = self.idea()
        item = self.campaign(old, "a")
        paths = [self.bridge.producer, self.bridge.consumer, self.root / "lab" / "lab.sqlite3"]
        hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
        self.assertEqual(self.bridge.decorate(item)["dependency_status"]["state"], "unregistered")
        self.assertEqual(hashes, [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths])
        self.bridge.register_campaign(item["id"])
        other = ResearchDependencies(self.root / "dependencies", self.root / "ideas", self.root / "lab", namespace="other-admin-test")
        other.register_campaign(item["id"])
        new = self.idea(old, finding="Changed interpretation")
        self.event(old, new)
        self.assertFalse(self.bridge.snapshot(item["id"])["run_allowed"])
        self.assertTrue(other.snapshot(item["id"])["run_allowed"])

    def test_guard_serializes_correction_with_local_run(self):
        old = self.idea()
        item = self.campaign(old, "a")
        new = self.idea(old, finding="Changed interpretation")
        entered = threading.Event()
        finished = threading.Event()
        def declare():
            entered.set()
            result = self.event(old, new)
            finished.set()
            return result
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.bridge.run_guard(item["id"]):
                future = pool.submit(declare)
                self.assertTrue(entered.wait(2))
                self.assertFalse(finished.wait(.05))
                executed = self.lab.run(item["id"], {"expected_revision": item["revision"], "idempotency_key": "guarded"})
            future.result(timeout=5)
        self.assertEqual(executed["budget"]["used_actions"], 1)
        self.assertFalse(self.bridge.snapshot(item["id"])["run_allowed"])

    def test_export_is_separate_scoped_complete_sidecar_without_other_campaigns(self):
        old = self.idea()
        a, b = self.campaign(old, "a"), self.campaign(old, "b")
        for item in (a, b):
            self.bridge.register_campaign(item["id"])
        new = self.idea(old, finding="Changed interpretation")
        self.event(old, new)
        self.bridge.drain()
        bundle = self.bridge.scoped_export(a["id"])
        self.assertEqual(bundle["format"], "agentsdock-dependencies/1")
        self.assertNotIn(b["id"], json.dumps(bundle))
        self.assertEqual(len(bundle["data"]["receipts"]), 1)
        value = {k: v for k, v in bundle.items() if k != "sha256"}
        self.assertEqual(hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), bundle["sha256"])

    def test_intent_before_core_commit_blocks_and_retries_original_identity(self):
        old = self.idea()
        a, b = self.campaign(old, "a"), self.campaign(old, "b")
        self.bridge.register_campaign(b["id"])
        request, source = self.correction_request(a)
        intent = self.bridge.prepare_lab_correction(a["id"], request, source)
        self.assertEqual(self.bridge.snapshot(b["id"])["state"], "correction_pending")
        self.assert_code("dependency_stale", lambda: self.execute(b))
        recovery = self.bridge.recover_lab_corrections()
        self.assertEqual(recovery["pending_intents"], 1)
        self.assertEqual(recovery["items"][0]["id"], intent["id"])
        self.assertEqual(self.lab.get(a["id"]), a)  # Recovery did not execute the request.
        final = self.bridge.apply_lab_source_correction(a["id"], request, source, correct_inputs=self.lab.correct_inputs)
        self.assertEqual(final["revision"], a["revision"] + 1)
        self.assertEqual(self.bridge.apply_lab_source_correction(a["id"], request, source, correct_inputs=self.lab.correct_inputs), final)
        self.assertEqual(self.bridge.recover_lab_corrections()["pending_intents"], 0)
        self.assertFalse(self.bridge.snapshot(b["id"])["run_allowed"])
        self.assertEqual(self.counts(), [1, 1, 0])

    def test_intent_after_core_commit_recovers_without_reexecuting_or_spending(self):
        old = self.idea()
        a, b = self.campaign(old, "a"), self.campaign(old, "b")
        self.bridge.register_campaign(b["id"])
        request, source = self.correction_request(a)
        self.bridge.prepare_lab_correction(a["id"], request, source)
        committed = self.lab.correct_inputs(a["id"], request)  # Crash before bridge completion.
        restarted = ResearchDependencies(self.root / "dependencies", self.root / "ideas", self.root / "lab")
        self.assertEqual(restarted.recover_lab_corrections()["pending_intents"], 0)
        self.assertEqual(self.lab.get(a["id"]), committed)
        self.assertEqual(committed["budget"]["used_actions"], 0)
        self.assertFalse(restarted.snapshot(b["id"])["run_allowed"])
        self.assertEqual(self.counts(), [1, 1, 0])

    def test_known_core_rejection_clears_only_uncommitted_intent(self):
        old = self.idea()
        a, b = self.campaign(old, "a"), self.campaign(old, "b")
        self.bridge.register_campaign(b["id"])
        request, source = self.correction_request(a)
        request["inputs"]["unexpected"] = True
        self.assert_code("invalid_request", lambda: self.bridge.apply_lab_source_correction(a["id"], request, source, correct_inputs=self.lab.correct_inputs))
        self.assertEqual(self.bridge.recover_lab_corrections()["pending_intents"], 0)
        self.assertTrue(self.bridge.snapshot(b["id"])["run_allowed"])
        self.assertEqual(self.counts(), [0, 0, 0])
        self.assertEqual(self.lab.get(a["id"]), a)

    def test_unknown_error_after_commit_keeps_actual_receipt_recoverable(self):
        old = self.idea()
        a = self.campaign(old, "a")
        request, source = self.correction_request(a)
        def fail_after_commit(cid, body):
            self.lab.correct_inputs(cid, body)
            raise RuntimeError("process exit after commit")
        with self.assertRaisesRegex(RuntimeError, "process exit"):
            self.bridge.apply_lab_source_correction(a["id"], request, source, correct_inputs=fail_after_commit)
        self.assertEqual(self.counts(), [0, 0, 0])
        self.assertEqual(self.bridge.recover_lab_corrections()["pending_intents"], 0)
        self.assertEqual(self.counts(), [1, 1, 0])

    def test_recovery_pending_front_does_not_starve_later_committed_intent(self):
        old = self.idea()
        a, b = self.campaign(old, "a"), self.campaign(old, "b")
        request_a, source_a = self.correction_request(a, "a-intent")
        request_b, source_b = self.correction_request(b, "b-intent")
        self.bridge.prepare_lab_correction(a["id"], request_a, source_a)
        self.bridge.prepare_lab_correction(b["id"], request_b, source_b)
        self.lab.correct_inputs(b["id"], request_b)
        self.assertEqual(self.bridge.recover_lab_corrections(limit=1)["items"][0]["state"], "pending")
        self.assertEqual(self.bridge.recover_lab_corrections(limit=1)["items"][0]["state"], "completed")
        self.assertEqual(self.counts(), [1, 1, 0])

    def test_existing_run_replay_is_read_only_even_after_correction(self):
        old = self.idea()
        item = self.campaign(old, "a")
        request = {"expected_revision": item["revision"], "idempotency_key": "run"}
        result = self.execute(item)
        changed = self.idea(old, finding="Revised interpretation")
        self.event(old, changed)
        self.assertEqual(self.bridge.replay_lab_mutation(item["id"], "action_executed", request), result)
        self.assertIsNone(self.bridge.replay_lab_mutation(item["id"], "action_executed", {**request, "idempotency_key": "new"}))
        self.assert_code("idempotency_conflict", lambda: self.bridge.replay_lab_mutation(item["id"], "action_executed", {**request, "expected_revision": 99}))
        self.assert_code("dependency_stale", lambda: self.bridge.admission(item["id"]))
        self.assertEqual(self.lab.get(item["id"])["budget"]["used_actions"], 1)

    def test_export_too_large_and_traversal_limits_are_explicit(self):
        from unittest.mock import patch
        old = self.idea()
        item = self.campaign(old, "a")
        self.bridge.register_campaign(item["id"])
        with patch("research_dependencies.MAX_EXPORT", 20):
            self.assert_code("dependency_limit", lambda: self.bridge.scoped_export(item["id"]))
        self.assert_code("invalid_request", lambda: self.bridge.drain(limit=101))
        self.assert_code("invalid_request", lambda: self.bridge.recover_lab_corrections(limit=True))

    def test_pre_revision_idea_import_is_local_unverified_and_not_rewritten(self):
        old = self.idea()
        # Construct the historical origin shape in a disposable fixture. The
        # current transport correctly refuses to create this legacy shape.
        request = source_request("legacy")
        seed = IdeaImport(self.ideas).seed(old["id"])
        request.update({k: seed[k] for k in ("origin", "brief", "inputs")})
        # The old immutable snapshot predates stricter create validation; write
        # that shape only in this disposable fixture's existing version payload.
        item = self.lab.create(request, resolve_origin=IdeaImport(self.ideas).resolve)
        legacy_source = copy.deepcopy(seed["inputs"]["sources"][0])
        legacy_source["provenance"].pop("decision_revision")
        self.assertIsNone(self.bridge._trusted_source(item, legacy_source))
        with closing(sqlite3.connect(self.root / "lab" / "lab.sqlite3")) as db:
            legacy = copy.deepcopy(item)
            legacy["origin"].pop("decision_revision")
            raw = json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            db.execute("UPDATE campaigns SET payload=? WHERE id=?", (raw, item["id"]))
            db.execute("UPDATE versions SET payload=? WHERE campaign_id=?", (raw, item["id"]))
            db.commit()
        original = self.lab.get(item["id"])
        result = self.bridge.reconcile_campaign(item["id"])
        self.assertTrue(result["dependency_status"]["run_allowed"])
        self.assertEqual(self.lab.get(item["id"]), original)
        new = self.idea(old, finding="Correction cannot establish missing old decision identity")
        self.event(old, new)
        self.assertTrue(self.bridge.snapshot(item["id"])["run_allowed"])
        self.assertEqual(self.execute(original)["budget"]["used_actions"], 1)

    def test_shared_apply_rejects_ambiguous_multi_source_edit_before_intent(self):
        item = self.lab.create(source_request())
        request, source = self.correction_request(item)
        request["inputs"]["sources"][1]["text"] = "Another changed source."
        self.assert_code("invalid_request", lambda: self.bridge.apply_lab_source_correction(item["id"], request, source, correct_inputs=self.lab.correct_inputs))
        self.assertEqual(self.bridge.recover_lab_corrections()["pending_intents"], 0)
        self.assertEqual(self.lab.get(item["id"]), item)

    def test_crash_after_event_registration_replays_same_intent_event(self):
        old = self.idea()
        item = self.campaign(old, "a")
        request, source = self.correction_request(item)
        intent = self.bridge.prepare_lab_correction(item["id"], request, source)
        new = self.lab.correct_inputs(item["id"], request)
        event = self.bridge.record_lab_source_correction(item["id"], item["revision"], new["revision"], source,
            idempotency_key="intent-" + intent["id"], reason=request["reason"])
        self.assertEqual(self.bridge.recover_lab_corrections()["items"][0]["event_id"], event["id"])
        self.assertEqual(self.counts(), [1, 1, 0])

    def test_missing_consumer_fails_closed_and_is_not_recreated_by_read_or_restart(self):
        item = self.lab.create(source_request())
        self.bridge.register_campaign(item["id"])
        self.bridge.consumer.unlink()
        self.assert_code("storage_error", lambda: self.bridge.snapshot(item["id"]))
        self.assertFalse(self.bridge.consumer.exists())
        self.assert_code("storage_error", lambda: ResearchDependencies(self.root / "dependencies", self.root / "ideas", self.root / "lab"))
        self.assertFalse(self.bridge.consumer.exists())

    def test_large_legacy_snapshots_are_projected_without_losing_source_dependencies(self):
        old = self.idea()
        item = self.campaign(old, "a")
        # Legacy payload growth was repeated frozen task-packet context and event
        # history, not the input CAS. Preserve those raw records while inspecting
        # exactly the identity/lineage fields required by this service.
        with closing(sqlite3.connect(self.root / "lab" / "lab.sqlite3")) as db:
            large = copy.deepcopy(item)
            large["current_plan"]["legacy_repeated_task_packet"] = "context" * 900000
            raw = json.dumps(large)
            self.assertGreater(len(raw), 4 * 1024 * 1024)
            db.execute("UPDATE campaigns SET payload=? WHERE id=?", (raw, item["id"]))
            db.execute("UPDATE versions SET payload=? WHERE campaign_id=?", (raw, item["id"]))
            db.commit()
        with closing(sqlite3.connect(self.root / "ideas" / "ideas.sqlite3")) as db:
            row = db.execute("SELECT data FROM versions WHERE session_id=? AND revision=?", (old["id"], old["revision"])).fetchone()
            saved = json.loads(row[0])
            saved["events"].append({"type": "legacy_history", "text": "history" * 900000})
            db.execute("UPDATE versions SET data=? WHERE session_id=? AND revision=?", (json.dumps(saved), old["id"], old["revision"]))
            db.commit()
        self.bridge.register_campaign(item["id"])
        self.assertTrue(self.bridge.snapshot(item["id"])["run_allowed"])
        new = self.idea(old, finding="Later correction still reaches the large old snapshot")
        self.event(old, new)
        self.assertFalse(self.bridge.snapshot(item["id"])["run_allowed"])
        with closing(sqlite3.connect(self.root / "lab" / "lab.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT payload FROM campaigns WHERE id=?", (item["id"],)).fetchone()[0], raw)


if __name__ == "__main__":
    unittest.main()
