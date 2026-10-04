"""Packet regressions use private local stores; no provider calls or live state."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import research_model_jobs as jobs
from research_lab import ResearchLabStore
from tests.test_research_lab import numeric_request, source_request


class ResearchModelPacketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lab = ResearchLabStore(self.root / "lab")

    def mutate(self, item, method, key, **extra):
        return getattr(self.lab, method)(item["id"], {"expected_revision": item["revision"], "idempotency_key": key, **extra})

    def select_and_pause(self, feedback="Exact choice: inspect source TWO. 原样反馈 β\nKeep this choice."):
        item = self.lab.create(source_request())
        selected = item["current_plan"]["candidates"][1]
        item = self.mutate(item, "decide", "select", kind="select", selected_action_id=selected["id"], feedback=feedback)
        decision = copy.deepcopy(item["decisions"][-1])
        for i in range(4):
            item = self.mutate(item, "decide", f"pause-{i}", kind="defer", feedback=f"Pause {i}; retain selection")
            item = self.mutate(item, "advance", f"resume-{i}")
        return item, decision

    def four_source_rounds(self, quote="Quotation absent from all supplied sources"):
        request = source_request()
        request["inputs"]["sources"] = [{**request["inputs"]["sources"][0], "id": f"source-{i}"} for i in range(5)]
        request["inputs"]["quote"] = quote
        item = self.lab.create(request)
        for i in range(4):
            if i:
                item = self.mutate(item, "advance", f"advance-{i}")
            item = self.mutate(item, "run", f"run-{i}")
        return item

    def test_prepared_packet_retains_active_selection_after_history_compaction(self):
        item, decision = self.select_and_pause()
        self.assertNotIn(decision, item["decisions"])
        generator = AsyncMock()
        controller = jobs.ResearchModelJobs(self.root / "jobs", generate=generator)
        receipt = controller.prepare({"campaign_id": item["id"], "expected_revision": item["revision"],
                                      "role": "planner", "idempotency_key": "planner"}, resolve_snapshot=self.lab.get)
        packet = controller.artifact(receipt["id"], receipt["packet_ref"])["content"]
        selected = packet["active_selection"]
        self.assertEqual(selected["reason"], decision["feedback"])
        self.assertEqual(selected["action_id"], decision["selected_action_id"])
        self.assertEqual(selected["decision_id"], decision["id"])
        self.assertEqual(selected["decision_revision"], decision["campaign_revision"] + 1)
        self.assertEqual(self.lab.artifact(item["id"], selected["decision_artifact"])["content"], decision)
        self.assertIsNone(selected["decision_record"])
        self.assertFalse(selected["decision_record_in_packet"])
        self.assertEqual(packet["human_decision_coverage"]["total"], 5)
        self.assertFalse(receipt["context"]["silent_truncation"])
        generator.assert_not_called()

    def test_empty_feedback_not_reconstructed_from_display_reason_and_pause_not_authority(self):
        item, decision = self.select_and_pause("")
        item = self.mutate(item, "decide", "paused-again", kind="defer", feedback="Wait")
        selected = jobs.build_packet(item, "planner")["active_selection"]
        self.assertEqual(selected["reason"], "Explicit human selection")
        self.assertFalse(selected["execution_eligible"])
        self.assertIsNone(selected["decision_record"])
        self.assertEqual(self.lab.artifact(item["id"], selected["decision_artifact"])["content"]["feedback"], "")
        self.assertNotIn("feedback", selected)

    def test_active_selection_rejects_stale_plan_brief_input_or_candidate(self):
        item, _ = self.select_and_pause()
        for key, value in (("goal_revision", 999), ("input_artifact", "0" * 64), ("selected_action_id", "invented")):
            changed = copy.deepcopy(item)
            changed["current_plan"][key] = value
            with self.subTest(key=key), self.assertRaises(jobs.ModelJobError) as caught:
                jobs.build_packet(changed, "planner")
            self.assertEqual(caught.exception.code, "stale_plan")

    def test_all_four_current_observations_are_included_for_analysis_and_review(self):
        item = self.four_source_rounds()
        for role in ("analyst", "reviewer"):
            packet = jobs.build_packet(item, role)
            self.assertEqual([r["run_id"] for r in packet["observations"]], [r["run"]["id"] for r in item["rounds"]])
            self.assertEqual(packet["observation_coverage"]["included_observations"], 4)
            self.assertEqual(packet["observation_coverage"]["total_campaign_rounds"], 4)
            self.assertTrue(packet["observation_coverage"]["current_scope_complete"])
            self.assertEqual(packet["omitted_observations"], [])
            for observed, source in zip(packet["observations"], item["rounds"]):
                self.assertEqual(observed["data"], source["observation"]["data"])
                self.assertEqual(observed["qc"], source["qc"])
            _, context = jobs.prompt_for(role, packet)
            self.assertFalse(context["silent_truncation"])

    def test_planner_omissions_and_upstream_projection_have_fixed_refs_and_counts(self):
        item = self.four_source_rounds()
        planner = jobs.build_packet(item, "planner")
        self.assertEqual(planner["observations"], [])
        self.assertEqual(planner["observation_coverage"]["omitted_observations"], 4)
        self.assertFalse(planner["observation_coverage"]["current_scope_complete"])
        for ref in planner["omitted_observations"]:
            self.assertEqual(self.lab.artifact(item["id"], ref["artifact"])["content"]["run"]["id"], ref["run_id"])
        projected = copy.deepcopy(item)
        omitted = projected["rounds"].pop(0)
        projected["omitted_rounds"] = [{"index": omitted["index"], "run_id": omitted["run"]["id"], "artifact": omitted["artifact"]}]
        packet = jobs.build_packet(projected, "analyst")
        self.assertEqual(packet["observation_coverage"]["total_campaign_rounds"], 4)
        self.assertEqual(packet["observation_coverage"]["included_observations"], 3)
        self.assertFalse(packet["observation_coverage"]["current_scope_complete"])
        self.assertEqual(packet["omitted_observations"][0]["artifact"], omitted["artifact"])
        self.assertEqual(packet["omitted_observations"][0]["current_scope_eligibility"], "unknown_until_retrieved")

    def test_oversized_complete_context_rejected_before_reservation_or_provider(self):
        # Legal source-quote bytes expand under JSON escaping. Four real local
        # results cannot be silently shortened to admit a provider call.
        item = self.four_source_rounds("\0" * 8000)
        generator = AsyncMock()
        controller = jobs.ResearchModelJobs(self.root / "jobs", generate=generator)
        with self.assertRaises(jobs.ModelJobError) as caught:
            controller.prepare({"campaign_id": item["id"], "expected_revision": item["revision"],
                                "role": "analyst", "idempotency_key": "oversized"}, resolve_snapshot=self.lab.get)
        self.assertEqual(caught.exception.code, "context_too_large")
        history = controller.list(item["id"])
        self.assertEqual(history["items"], [])
        self.assertEqual((history["quota"]["used"], history["quota"]["reserved"]), (0, 0))
        generator.assert_not_called()

    def test_changed_input_observations_are_disclosed_but_not_current_evidence(self):
        item = self.lab.create(numeric_request())
        item = self.mutate(item, "run", "first")
        old = item["rounds"][0]
        item = self.mutate(item, "correct_inputs", "correct", inputs={**numeric_request()["inputs"], "treatment": [3, 4, 5, 6, 7]}, reason="Correct supplied values")
        item = self.mutate(item, "run", "second")
        packet = jobs.build_packet(item, "analyst")
        self.assertEqual(len(packet["observations"]), 1)
        self.assertEqual(packet["historical_observations"][0]["run_id"], old["run"]["id"])
        self.assertEqual(packet["historical_observations"][0]["reason"], "different_brief_or_input_version")
        self.assertTrue(packet["observation_coverage"]["current_scope_complete"])


if __name__ == "__main__":
    unittest.main()
