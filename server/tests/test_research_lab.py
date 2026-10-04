import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from research_lab import LabError, PairedNumericAdapter, ResearchLabStore, MAX_DETAIL_BYTES


def source_request(key="source"):
    return {"idempotency_key": key, "entry": "goal", "brief": {
        "goal": "Locate evidence for the stated quote", "hypothesis": "", "success_criteria": "Inspect occurrence and context", "constraints": "Supplied sources only"},
        "adapter_id": "source_evidence", "inputs": {"quote": "β improves", "sources": [
            {"id": "s1", "title": "First", "uri": "https://example.invalid/a", "text": "No relevant passage.", "coverage": "excerpt", "missing_sections": ["Appendix"]},
            {"id": "s2", "title": "Second", "uri": "https://example.invalid/b", "text": "Only under the tested conditions, β improves performance. However, replication is required.", "coverage": "full_text", "missing_sections": []}]},
        "budget": {"max_actions": 6, "max_rounds": 6}}


def numeric_request(key="numeric"):
    return {"idempotency_key": key, "entry": "hypothesis", "brief": {
        "goal": "Characterize supplied paired differences", "hypothesis": "The supplied mean exceeds one unit", "success_criteria": "Estimate and assess sensitivity", "constraints": "Do not infer causality"},
        "adapter_id": "paired_numeric", "inputs": {"baseline": [1, 2, 3, 4, 5], "treatment": [4, 5, 6, 7, 8], "unit": "points", "minimum_effect": 1},
        "budget": {"max_actions": 6, "max_rounds": 6}}


class ResearchLabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "lab"
        self.store = ResearchLabStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def mutate(self, item, method, key, **extra):
        return getattr(self.store, method)(item["id"], {"expected_revision": item["revision"], "idempotency_key": key, **extra})

    def test_create_frozen_plan_and_artifacts(self):
        request = source_request()
        item = self.store.create(request)
        request["inputs"]["sources"][0]["text"] = "changed"
        plan = item["current_plan"]
        self.assertEqual(plan["selection_origin"], "policy")
        self.assertEqual(len(plan["candidates"]), 2)
        spec = plan["candidates"][0]
        frozen = self.store.artifact(item["id"], spec["spec_artifact"])["content"]
        self.assertEqual(frozen["question"], item["brief"]["goal"])
        self.assertEqual(frozen["task_packet"]["goal"]["success_criterion"], item["brief"]["success_criteria"])
        self.assertEqual(frozen["task_packet"]["constraints"]["remaining_actions"], 6)
        self.assertTrue(all(p["requirements"] for p in frozen["task_packet"]["protocol_sections"]))
        self.assertEqual(len(frozen["execution_environment"]["files_sha256"]["research_lab.py"]), 64)
        self.assertEqual(len(frozen["execution_environment"]["files_sha256"]["uv.lock"]), 64)
        self.assertTrue(frozen["qc_rules"])
        self.assertTrue(frozen["interpretation_rules"])
        self.assertEqual(self.store.artifact(item["id"], item["input_artifact"])["content"]["sources"][0]["text"], "No relevant passage.")

    def test_create_key_replay_conflict_and_restart(self):
        req = numeric_request()
        first = self.store.create(req)
        self.assertEqual(ResearchLabStore(self.root).create(req), first)
        req["brief"]["goal"] += " changed"
        with self.assertRaisesRegex(LabError, "different request"):
            self.store.create(req)

    def test_numeric_two_result_dependent_rounds(self):
        item = self.store.create(numeric_request())
        first = self.mutate(item, "run", "r1")
        self.assertEqual(first["status"], "awaiting_next")
        self.assertEqual(first["review"]["next_method"], "leave_one_out")
        self.assertEqual(first["rounds"][0]["observation"]["data"]["mean_difference"], 3)
        second_plan = self.mutate(first, "advance", "next")
        self.assertEqual(second_plan["current_plan"]["candidates"][0]["method"], "leave_one_out")
        done = self.mutate(second_plan, "run", "r2")
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["budget"]["used_actions"], 2)
        self.assertEqual(done["budget"]["reserved_actions"], 0)
        self.assertTrue(done["rounds"][1]["observation"]["data"]["all_above_threshold"])
        self.assertFalse(done["rounds"][1]["run"]["is_independent_replicate"])
        self.assertEqual(done["rounds"][1]["analysis"]["inference_validity"], "cannot_determine")
        self.assertEqual(len(done["review"]["input_refs"]), 4)
        comparison = done["comparisons"][0]
        self.assertEqual(comparison["comparability"], "same_inputs_different_analysis")
        self.assertFalse(comparison["eligible_for_pooled_confirmation"])
        self.assertEqual(comparison["run_ids"], [r["run"]["id"] for r in done["rounds"]])
        self.assertIn("sensitivity_range", comparison["changed_observation_fields"])

    def test_different_first_observation_changes_next_method(self):
        req = numeric_request()
        req["inputs"]["treatment"] = [1, 2, 3, 4, 50]
        item = self.mutate(self.store.create(req), "run", "r1")
        self.assertEqual(item["review"]["next_method"], "extreme_sensitivity")
        item = self.mutate(item, "advance", "next")
        item = self.mutate(item, "run", "r2")
        data = item["rounds"][1]["observation"]["data"]
        self.assertEqual(data["omitted_pair_index"], 4)
        self.assertTrue(data["threshold_conclusion_changed"])

    def test_source_missing_then_next_supplied_source_then_context(self):
        item = self.mutate(self.store.create(source_request()), "run", "r1")
        self.assertEqual(item["claims"][0]["source_support"], "cannot_determine")
        item = self.mutate(item, "advance", "n1")
        self.assertEqual(item["current_plan"]["candidates"][0]["parameters"]["source_id"], "s2")
        item = self.mutate(item, "run", "r2")
        span = item["rounds"][1]["observation"]["data"]["spans"][0]
        self.assertEqual(span["byte_end"] - span["byte_start"], len("β improves".encode()))
        self.assertEqual(item["review"]["next_method"], "source_context")
        item = self.mutate(item, "advance", "n2")
        item = self.mutate(item, "run", "r3")
        self.assertEqual(item["status"], "completed")
        self.assertIn("however", item["rounds"][2]["observation"]["data"]["inspection_terms"])
        self.assertTrue(all(c["inference_validity"] == "cannot_determine" for c in item["claims"]))

    def test_actual_human_selection_changes_executed_source(self):
        item = self.store.create(source_request())
        selected = item["current_plan"]["candidates"][1]["id"]
        item = self.mutate(item, "decide", "choose", kind="select", selected_action_id=selected, feedback="Read the primary source first")
        self.assertEqual(item["decisions"][0]["feedback"], "Read the primary source first")
        item = self.mutate(item, "run", "r1")
        self.assertEqual(item["rounds"][0]["observation"]["data"]["source_id"], "s2")
        self.assertEqual(item["rounds"][0]["plan"]["selection_origin"], "human")

    def test_goal_revision_invalidates_stale_choice_and_updates_frozen_action(self):
        item = self.store.create(numeric_request())
        stale = {"expected_revision": item["revision"], "idempotency_key": "stale", "kind": "select", "selected_action_id": item["current_plan"]["selected_action_id"], "feedback": ""}
        revised = self.mutate(item, "decide", "revise", kind="revise", goal="A new explicit goal", feedback="Scope correction")
        self.assertEqual(revised["current_plan"]["candidates"][0]["question"], "A new explicit goal")
        self.assertEqual(revised["brief"]["revision"], 2)
        with self.assertRaisesRegex(LabError, "reload"):
            self.store.decide(item["id"], stale)

    def test_duplicate_execution_replays_exact_response_after_later_revision(self):
        item = self.store.create(numeric_request())
        req = {"expected_revision": item["revision"], "idempotency_key": "run"}
        first = self.store.run(item["id"], req)
        self.mutate(first, "advance", "next")
        replay = ResearchLabStore(self.root).run(item["id"], req)
        self.assertEqual(first, replay)
        self.assertEqual(len(self.store.get(item["id"])["rounds"]), 1)

    def test_concurrent_run_and_last_budget_slot(self):
        req = numeric_request()
        req["budget"] = {"max_actions": 1, "max_rounds": 2}
        item = self.store.create(req)
        payload = {"expected_revision": item["revision"], "idempotency_key": "same"}
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: ResearchLabStore(self.root).run(item["id"], payload), range(3)))
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(results[0]["budget"]["used_actions"], 1)
        self.assertEqual(results[0]["stop_reason"], "budget_exhausted")
        self.assertTrue(results[0]["review"]["uncertainties"])

    def test_qc_failure_is_completed_computation_not_negative_evidence(self):
        req = numeric_request()
        req["inputs"]["baseline"] = [1, 2]
        req["inputs"]["treatment"] = [0, 0]
        item = self.mutate(self.store.create(req), "run", "run")
        self.assertEqual(item["rounds"][0]["run"]["status"], "completed")
        self.assertFalse(item["rounds"][0]["qc"]["passed"])
        self.assertEqual(item["claims"][0]["source_support"], "cannot_determine")
        self.assertEqual(item["status"], "needs_input")

    def test_adapter_failure_retains_attempt_and_cost_without_secrets(self):
        item = self.store.create(numeric_request())
        with patch.object(PairedNumericAdapter, "execute", side_effect=RuntimeError("SECRET")):
            result = self.mutate(item, "run", "run")
        self.assertEqual(result["rounds"][0]["run"]["status"], "failed")
        self.assertEqual(result["budget"]["used_actions"], 1)
        self.assertNotIn("SECRET", json.dumps(result))
        self.assertEqual(result["claims"][0]["source_support"], "cannot_determine")

    def test_process_death_before_commit_rolls_back_identity_usage_and_artifacts(self):
        item = self.store.create(numeric_request())
        with closing(sqlite3.connect(self.store.path)) as db, db:
            before = db.execute("SELECT count(*) FROM artifacts").fetchone()[0]
        with patch.object(PairedNumericAdapter, "execute", side_effect=SystemExit("crash")):
            with self.assertRaises(SystemExit):
                self.mutate(item, "run", "run")
        reopened = ResearchLabStore(self.root)
        self.assertEqual(reopened.get(item["id"]), item)
        with closing(sqlite3.connect(self.store.path)) as db, db:
            self.assertEqual(before, db.execute("SELECT count(*) FROM artifacts").fetchone()[0])
        result = reopened.run(item["id"], {"expected_revision": 1, "idempotency_key": "run"})
        self.assertEqual(result["budget"]["used_actions"], 1)

    def test_commit_failure_rolls_back_published_results(self):
        item = self.store.create(numeric_request())
        with patch.object(self.store, "_save", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaisesRegex(LabError, "rolled back"):
                self.mutate(item, "run", "run")
        self.assertEqual(self.store.get(item["id"]), item)

    def test_corrected_input_invalidates_claims_preserves_historical_artifact(self):
        item = self.mutate(self.store.create(numeric_request()), "run", "r1")
        old_digest = item["input_artifact"]
        corrected = numeric_request()["inputs"]
        corrected["treatment"][0] = 100
        item = self.mutate(item, "correct_inputs", "correct", inputs=corrected, reason="Corrected measurement transcription")
        self.assertEqual(item["claims"][0]["status"], "needs_revalidation")
        self.assertNotEqual(item["input_artifact"], old_digest)
        self.assertEqual(self.store.artifact(item["id"], old_digest)["content"]["treatment"][0], 4)
        item = self.mutate(item, "run", "revalidate")
        self.assertEqual(item["claims"][0]["status"], "needs_revalidation")
        self.assertEqual(item["claims"][1]["status"], "current")

    def test_correction_persists_even_when_budget_exhausted(self):
        req = numeric_request()
        req["budget"]["max_actions"] = 1
        item = self.mutate(self.store.create(req), "run", "r1")
        values = numeric_request()["inputs"]
        values["treatment"][0] = 100
        item = self.mutate(item, "correct_inputs", "correction", inputs=values, reason="Corrected value")
        self.assertEqual(item["claims"][0]["status"], "needs_revalidation")
        self.assertIsNone(item["current_plan"])
        self.assertEqual(item["stop_reason"], "budget_exhausted")

    def test_artifact_scope_and_integrity(self):
        a = self.store.create(source_request())
        b = self.store.create(numeric_request())
        with self.assertRaisesRegex(LabError, "does not belong"):
            self.store.artifact(b["id"], a["input_artifact"])
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE artifacts SET data=? WHERE digest=?", (b"{}", a["input_artifact"]))
        with self.assertRaisesRegex(LabError, "hash"):
            self.mutate(a, "run", "r1")

    def test_unsupported_schema_and_adapter_version_fail_closed(self):
        item = self.store.create(numeric_request())
        with patch.dict(self.store.adapters, {"paired_numeric": None}):
            with self.assertRaisesRegex(LabError, "version"):
                self.mutate(item, "run", "r1")
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(LabError, "schema"):
            ResearchLabStore(self.root)

    def test_read_only_listing_history_never_runs_and_is_compact(self):
        item = self.store.create(source_request())
        self.assertEqual(self.store.list()["items"][0]["rounds"], [])
        self.assertEqual(len(self.store.history(item["id"])["items"]), 1)
        self.assertEqual(self.store.get(item["id"])["budget"]["used_actions"], 0)

    def test_defer_resume_and_stop_do_not_execute(self):
        item = self.store.create(numeric_request())
        frozen_id = item["current_plan"]["id"]
        item = self.mutate(item, "decide", "defer", kind="defer", feedback="Later")
        item = self.mutate(item, "advance", "resume")
        self.assertEqual(item["current_plan"]["id"], frozen_id)
        item = self.mutate(item, "run", "r1")
        item = self.mutate(item, "decide", "defer2", kind="defer", feedback="Wait for review")
        item = self.mutate(item, "advance", "resume2")
        self.assertEqual(item["status"], "planned")
        item = self.mutate(item, "decide", "stop", kind="stop", feedback="Enough")
        with self.assertRaisesRegex(LabError, "current frozen"):
            self.mutate(item, "run", "cannot")
        self.assertEqual(item["budget"]["used_actions"], 1)

    def test_validation_bounds_and_no_external_action_types(self):
        bad = []
        for path, value in [("adapter_id", "shell"), ("adapter_id", []), ("entry", "external")]:
            req = numeric_request(); req[path] = value; bad.append(req)
        for value in (float("nan"), float("inf"), True, 10**100, 10**1000):
            req = numeric_request(); req["inputs"]["baseline"][0] = value; bad.append(req)
        req = numeric_request(); req["budget"]["max_actions"] = 13; bad.append(req)
        req = numeric_request(); req["brief"]["hypothesis"] = ""; bad.append(req)
        req = source_request(); req["inputs"]["quote"] = ""; bad.append(req)
        req = source_request(); req["inputs"]["sources"][0]["text"] = "a" * 153601; bad.append(req)
        req = source_request(); req["inputs"]["sources"][1]["id"] = "s1"; bad.append(req)
        req = source_request(); req["inputs"]["sources"][0]["provenance"] = {"bad": "\ud800"}; bad.append(req)
        for req in bad:
            with self.subTest(request=req["adapter_id"]):
                with self.assertRaises(LabError):
                    self.store.create(req)

    def test_origin_requires_authoritative_resolver_after_dedupe(self):
        request = source_request()
        request["origin"] = {"kind": "idea", "session_id": "session", "generation_id": "generation", "selected_ids": ["D1"], "decision_revision": 3}
        with self.assertRaisesRegex(LabError, "verification"):
            self.store.create(request)
        seen = []
        def resolve(body):
            seen.append(body)
            body["inputs"]["sources"][0]["provenance"] = {"verified_by": "service"}
            return body
        result = self.store.create(request, resolve_origin=resolve)
        self.assertEqual(len(seen), 1)
        self.assertEqual(self.store.artifact(result["id"], result["input_artifact"])["content"]["sources"][0]["provenance"]["verified_by"], "service")
        self.assertEqual(self.store.create(request), result)
        self.assertEqual(len(seen), 1)

    def test_shared_artifact_reuse_does_not_copy_observations(self):
        a = self.mutate(self.store.create(numeric_request("a")), "run", "r1")
        b = self.store.create(numeric_request("b"))
        self.assertEqual(a["input_artifact"], b["input_artifact"])
        self.assertEqual(b["rounds"], [])
        b = self.mutate(b, "run", "r2")
        self.assertNotEqual(a["rounds"][0]["run"]["id"], b["rounds"][0]["run"]["id"])
        self.assertFalse(b["rounds"][0]["run"]["is_independent_replicate"])

    def test_numeric_support_label_attaches_to_tested_threshold_proposition(self):
        req = numeric_request()
        req["inputs"]["treatment"] = [-2, -1, 0, 1, 2]
        item = self.mutate(self.store.create(req), "run", "run")
        claim = item["rounds"][0]["analysis"]
        self.assertIn("exceeds 1 points", claim["claim"])
        self.assertEqual(claim["source_support"], "contradicted")
        self.assertIn("-3", item["rounds"][0]["observation"]["data"]["descriptive_fact"])

    def test_omission_markers_are_not_contiguous_source_evidence(self):
        for provenance in ({"gap_marker": "[SOURCE OMISSION]"}, {"original_provenance": {"gap_marker": "[SOURCE OMISSION]"}}):
            req = source_request(str(len(provenance.get("gap_marker", ""))))
            req["inputs"]["sources"] = [req["inputs"]["sources"][0]]
            req["inputs"]["sources"][0].update(text="left[SOURCE OMISSION]right", provenance=provenance)
            req["inputs"]["quote"] = "left[SOURCE OMISSION]right"
            item = self.mutate(self.store.create(req), "run", "run")
            data = item["rounds"][0]["observation"]["data"]
            self.assertEqual(data["packet_match_count"], 1)
            self.assertEqual(data["match_count"], 0)
            self.assertEqual(data["rejected_gap_matches"], 1)
            self.assertEqual(item["claims"][0]["source_support"], "cannot_determine")

    def test_valid_matches_on_either_side_of_gap_remain_inspectable(self):
        req = source_request()
        req["inputs"]["sources"][0].update(text="β improves [GAP] β improves", provenance={"gap_marker": "[GAP]"})
        item = self.mutate(self.store.create(req), "run", "run")
        self.assertEqual(item["rounds"][0]["observation"]["data"]["match_count"], 2)

    def test_changed_execution_fingerprint_requires_explicit_refreeze(self):
        item = self.store.create(numeric_request())
        with patch("research_lab._environment", return_value={"changed": True}):
            with self.assertRaisesRegex(LabError, "Python changed"):
                self.mutate(item, "run", "run")
        self.assertEqual(self.store.get(item["id"])["budget"]["used_actions"], 0)
        with patch("research_lab._environment", return_value={"changed": True}):
            with self.assertRaisesRegex(LabError, "after import"):
                self.store.create(numeric_request("new-plan"))

    def test_post_outcome_brief_revision_retains_old_results_and_marks_exploratory(self):
        item = self.mutate(self.store.create(numeric_request()), "run", "run")
        old_run = copy.deepcopy(item["rounds"][0])
        item = self.mutate(item, "decide", "revise", kind="revise", feedback="Narrow the scope", hypothesis="A different hypothesis",
                           success_criteria="Assess changed question", constraints="Exploratory only")
        self.assertEqual(item["brief"]["hypothesis"], "A different hypothesis")
        self.assertEqual(item["hypotheses"][0]["origin"], "human_post_outcome")
        self.assertEqual(item["claims"][0]["status"], "needs_revalidation")
        self.assertEqual(item["rounds"][0], old_run)
        self.assertEqual(item["current_plan"]["candidates"][0]["research_mode"], "post_outcome_exploratory")
        item = self.mutate(item, "run", "run-new")
        self.assertEqual(item["comparisons"][0]["comparability"], "changed_goal_not_like_for_like")
        self.assertTrue(any("exploratory" in x for x in item["rounds"][1]["analysis"]["limitations"]))

    def test_corrected_source_is_not_presented_as_original_verified_packet(self):
        req = source_request()
        req["inputs"]["sources"][0]["provenance"] = {"kind": "idea_frozen_packet", "source_hash": "a" * 64, "gap_marker": "[GAP]"}
        item = self.store.create(req)
        values = copy.deepcopy(req["inputs"])
        values["sources"][0]["text"] = "β improves"
        values["sources"][1]["provenance"] = {"kind": "fake_verified_source"}
        item = self.mutate(item, "correct_inputs", "correct", inputs=values, reason="Manual transcription correction")
        saved = self.store.artifact(item["id"], item["input_artifact"])["content"]
        self.assertEqual(saved["sources"][0]["provenance"]["kind"], "user_corrected")
        self.assertEqual(saved["sources"][0]["provenance"]["verification"], "not_source_verified")
        self.assertNotIn("provenance", saved["sources"][1])

    def test_correction_preserves_every_previously_known_omission_marker(self):
        req = source_request()
        req["inputs"]["sources"][0].update(text="a[GAP1]b[GAP2]c", provenance={"gap_marker": "[GAP1]", "original_provenance": {"gap_marker": "[GAP2]"}})
        item = self.store.create(req)
        values = copy.deepcopy(req["inputs"])
        values["quote"] = "b[GAP2]c"
        values["sources"][0]["text"] += " updated"
        item = self.mutate(item, "correct_inputs", "correct", inputs=values, reason="Corrected suffix")
        item = self.mutate(item, "run", "run")
        self.assertEqual(item["rounds"][0]["observation"]["data"]["rejected_gap_matches"], 1)
        self.assertEqual(item["claims"][0]["source_support"], "cannot_determine")

    def test_post_execution_stage_failures_preserve_real_observation(self):
        for stage in ("quality", "analyze", "review"):
            with self.subTest(stage=stage):
                item = self.store.create(numeric_request(stage))
                with patch.object(PairedNumericAdapter, stage, side_effect=RuntimeError("secret internal detail")):
                    item = self.mutate(item, "run", "run")
                result = item["rounds"][0]
                self.assertEqual(result["observation"]["data"]["mean_difference"], 3)
                self.assertEqual(result["run"]["status"], "completed")
                self.assertEqual(len(result["run"]["stage_errors"]), 1)
                self.assertEqual(item["status"], "needs_input")
                self.assertNotIn("secret internal detail", json.dumps(item))
                if stage == "review":
                    self.assertTrue(result["qc"]["passed"])
                    self.assertEqual(result["analysis"]["source_support"], "supported")

    def test_revised_actual_decision_is_present_in_new_worker_packet(self):
        item = self.store.create(numeric_request())
        item = self.mutate(item, "decide", "revise", kind="revise", goal="Use a narrower question", feedback="Focus on the measured units")
        actual = item["decisions"][-1]
        packet = self.store.artifact(item["id"], item["current_plan"]["candidates"][0]["spec_artifact"])["content"]["task_packet"]
        self.assertIn(actual["id"], [x["id"] for x in packet["relevant_state"]["decisions"]])

    def test_post_outcome_exploratory_status_persists_in_followup(self):
        item = self.mutate(self.store.create(numeric_request()), "run", "initial")
        item = self.mutate(item, "decide", "revise", kind="revise", success_criteria="Changed after viewing results", feedback="Exploratory change")
        item = self.mutate(item, "run", "posthoc")
        item = self.mutate(item, "advance", "continue")
        self.assertTrue(all(a["research_mode"] == "post_outcome_exploratory" for a in item["current_plan"]["candidates"]))

    def test_standalone_source_cannot_claim_authoritative_idea_provenance(self):
        req = source_request()
        req["inputs"]["sources"][0]["provenance"] = {"kind": "idea_frozen_packet", "verified": True, "gap_marker": "[OMISSION]"}
        item = self.store.create(req)
        provenance = self.store.artifact(item["id"], item["input_artifact"])["content"]["sources"][0]["provenance"]
        self.assertEqual(provenance["kind"], "user_supplied")
        self.assertEqual(provenance["verification"], "not_source_verified")
        self.assertIn("[OMISSION]", provenance["gap_markers"])

    def test_dispatch_packet_records_actual_selection_without_changing_scientific_spec(self):
        item = self.store.create(source_request())
        spec = item["current_plan"]["candidates"][1]
        frozen = self.store.artifact(item["id"], spec["spec_artifact"])
        item = self.mutate(item, "decide", "select", kind="select", selected_action_id=spec["id"],
                           feedback="Inspect limitations before interpreting this selected source")
        decision = copy.deepcopy(item["decisions"][-1])
        adapter = self.store.adapters["source_evidence"]
        with patch.object(adapter, "execute", wraps=adapter.execute) as execute:
            result = self.mutate(item, "run", "selected-run")
        run = result["rounds"][-1]["run"]
        dispatch = self.store.artifact(item["id"], run["dispatch_artifact"])["content"]
        self.assertEqual(dispatch["scientific_spec_artifact"], spec["spec_artifact"])
        self.assertEqual(dispatch["task_packet"], execute.call_args.args[1]["task_packet"])
        self.assertIn(decision, dispatch["task_packet"]["relevant_state"]["decisions"])
        self.assertEqual(dispatch["task_packet"]["actual_selection"]["decision_revision"], item["revision"])
        self.assertEqual(self.store.artifact(item["id"], spec["spec_artifact"]), frozen)
        self.assertEqual(result["rounds"][-1]["observation"]["data"]["source_id"], "s2")

    def long_history(self, fill="x", rounds=4):
        request = source_request()
        source = request["inputs"]["sources"][0]
        request["inputs"]["sources"] = [{**copy.deepcopy(source), "id": f"source-{i}"} for i in range(5)]
        request["inputs"]["quote"] = "No relevant passage."
        item = self.store.create(request)
        changes = {key: key + fill * 6900 for key in ("goal", "hypothesis", "success_criteria", "constraints")}
        for index in range(3):
            item = self.mutate(item, "decide", f"prepare-{index}", kind="revise", feedback=fill * 6900, **changes)
        sizes = []
        for index in range(rounds):
            if index:
                item = self.mutate(item, "decide", f"refreeze-{index}", kind="revise", feedback=fill * 6900, **changes)
            prior_revision = item["revision"]
            item = self.mutate(item, "run", f"run-{index}")
            sizes.append(len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode()))
            replay = self.store.run(item["id"], {"expected_revision": prior_revision, "idempotency_key": f"run-{index}"})
            self.assertEqual(replay, item)
            self.assertEqual(self.store.get(item["id"]), item)
        return item, sizes

    def test_dispatch_retains_selected_decision_beyond_recent_history(self):
        item = self.store.create(source_request())
        selected = item["current_plan"]["candidates"][1]["id"]
        item = self.mutate(item, "decide", "select", kind="select", selected_action_id=selected, feedback="Inspect this source")
        decision = copy.deepcopy(item["decisions"][-1])
        for index in range(4):
            item = self.mutate(item, "decide", f"defer-{index}", kind="defer", feedback="Continue after a pause")
            item = self.mutate(item, "advance", f"resume-{index}")
        self.assertNotIn(decision, item["decisions"])
        done = self.mutate(item, "run", "after-pauses")
        dispatch = self.store.artifact(done["id"], done["rounds"][-1]["run"]["dispatch_artifact"])["content"]
        self.assertIn(decision, dispatch["task_packet"]["relevant_state"]["decisions"])
        self.assertEqual(dispatch["task_packet"]["actual_selection"]["decision_id"], decision["id"])

    def test_valid_long_history_is_bounded_and_complete_rules_remain_addressable(self):
        item, sizes = self.long_history()
        self.assertTrue(all(size <= MAX_DETAIL_BYTES for size in sizes), sizes)
        self.assertEqual(item["total_rounds"], 4)
        self.assertEqual(len(item["rounds"]), 4)
        self.assertTrue(item["has_more_decisions"])
        self.assertEqual(item["total_decisions"], 6)
        self.assertTrue(all("task_packet" not in a for r in item["rounds"] for a in r["plan"]["candidates"]))
        for round_record in item["rounds"]:
            record = self.store.artifact(item["id"], round_record["artifact"])["content"]
            self.assertEqual(record["observation"], round_record["observation"])
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertLess(len(db.execute("SELECT payload FROM campaigns WHERE id=?", (item["id"],)).fetchone()[0].encode()), MAX_DETAIL_BYTES)
            for row in db.execute("SELECT payload FROM versions WHERE campaign_id=?", (item["id"],)):
                event = json.loads(row[0])["events"][-1]
                self.assertEqual(self.store.artifact(item["id"], event["artifact"])["content"]["data"], event["data"])

    def test_control_character_json_expansion_preserves_exact_text_and_explicit_round_refs(self):
        item, sizes = self.long_history("\0", rounds=6)
        self.assertTrue(all(size <= MAX_DETAIL_BYTES for size in sizes), sizes)
        self.assertEqual(item["brief"]["goal"], "goal" + "\0" * 6900)
        self.assertEqual(item["total_rounds"], 6)
        self.assertEqual(len(item["rounds"]) + len(item["omitted_rounds"]), 6)
        self.assertEqual(self.store.project(item), item)
        for ref in item["omitted_rounds"]:
            content = self.store.artifact(item["id"], ref["artifact"])["content"]
            self.assertEqual(content["run"]["id"], ref["run_id"])
            self.assertLess(len(json.dumps(content).encode()), MAX_DETAIL_BYTES)

    def test_legacy_large_snapshots_project_without_rewriting_raw_records(self):
        item, _ = self.long_history()
        with closing(sqlite3.connect(self.store.path)) as db:
            raw = json.loads(db.execute("SELECT payload FROM campaigns WHERE id=?", (item["id"],)).fetchone()[0])
            for plan in [raw["current_plan"], *[r["plan"] for r in raw["rounds"]]]:
                for spec in plan["candidates"]:
                    spec["task_packet"] = self.store.artifact(item["id"], spec["spec_artifact"])["content"]["task_packet"]
            for record in raw["rounds"]:
                record.pop("artifact", None)
            payload = json.dumps(raw, ensure_ascii=False)
            self.assertGreater(len(payload.encode()), 4 * 1024 * 1024)
            db.execute("UPDATE campaigns SET payload=? WHERE id=?", (payload, item["id"]))
            db.commit()
        original_hash = hashlib.sha256(payload.encode()).hexdigest()
        projected = self.store.get(item["id"])
        self.assertLess(len(json.dumps(projected).encode()), MAX_DETAIL_BYTES)
        self.assertEqual(projected["rounds"][-1]["observation"], raw["rounds"][-1]["observation"])
        content = self.store.artifact(item["id"], projected["rounds"][0]["artifact"])["content"]
        self.assertEqual(content["run"]["id"], raw["rounds"][0]["run"]["id"])
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(hashlib.sha256(db.execute("SELECT payload FROM campaigns WHERE id=?", (item["id"],)).fetchone()[0].encode()).hexdigest(), original_hash)

    def test_large_exact_context_observations_fold_whole_rounds_with_inspectable_artifacts(self):
        request = source_request()
        quote = "\0" * 16000
        request["inputs"]["quote"] = quote
        request["inputs"]["sources"] = [{**request["inputs"]["sources"][0], "id": f"s{i}", "text": quote * (5 if i == 0 else 1)} for i in range(5)]
        request["brief"] = {key: key + "\0" * 6900 for key in request["brief"]}
        item = self.store.create(request)
        for cycle in range(3):
            if cycle:
                item = self.mutate(item, "decide", f"revise-{cycle}", kind="revise", feedback="\0" * 6900, **request["brief"])
            item = self.mutate(item, "run", f"exact-{cycle}")
            item = self.mutate(item, "advance", f"context-{cycle}")
            item = self.mutate(item, "run", f"context-run-{cycle}")
        self.assertTrue(item["has_more_rounds"])
        self.assertEqual(item["total_rounds"], 6)
        self.assertEqual(item["rounds"][-1]["observation"]["data"]["quote"], quote)
        self.assertEqual(self.store.project(item), item)
        self.assertLess(len(json.dumps(item).encode()), MAX_DETAIL_BYTES)
        for reference in item["omitted_rounds"]:
            record = self.store.artifact(item["id"], reference["artifact"])["content"]
            self.assertEqual(record["run"]["id"], reference["run_id"])
            self.assertEqual(record["observation"]["data"]["quote"], quote)
            self.assertLess(len(json.dumps(record).encode()), MAX_DETAIL_BYTES)


if __name__ == "__main__":
    unittest.main()
