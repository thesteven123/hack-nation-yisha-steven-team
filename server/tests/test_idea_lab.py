import asyncio
import hashlib
import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from idea_lab import IdeaError, IdeaStore, STAGES, MAX_STAGE_ARTIFACT_BYTES, MAX_VALIDATION_DIAGNOSTIC_BYTES, validate_brief, validate_output
from idea_lab_routes import IdeaController
from idea_generation import IdeaGenerationError


def brief(sources=True):
    return {"goal": "Find a testable measurement question.", "hypothesis": "A mechanism may explain it.",
            "constraints": "Use a small local pilot.", "sources": [{
                "id": "s1", "title": "Supplied excerpt", "uri": "fixture:source",
                "text": "Measured value increased. The mechanism remains uncertain.",
            }] if sources else []}


def generated(stage, has_evidence=True):
    if stage == "literature":
        output = {"summary": "The excerpt reports an increase, without proving a mechanism.",
                  "evidence": [{"id": "e1", "source_id": "s1", "quote": "Measured value increased.",
                                "finding": "An increase is reported.", "limitation": "Mechanism is unknown.",
                                "conditions": "Conditions are not reported.", "assumptions": [],
                                "interpretation": "This supports occurrence of a reported increase, not its cause."}] if has_evidence else [],
                  "gaps": ["Original methods and independent replication are unavailable."]}
    elif stage == "ideas":
        output = {"directions": [{
            "id": f"d{index}", "title": f"Direction {index}", "question": f"Does factor {index} explain the observed change?",
            "nearest_work": "The supplied excerpt; no novelty search has been performed.",
            "evidence_ids": ["e1"] if has_evidence else [], "counterevidence": ["The mechanism is unmeasured."],
            "value": "Could distinguish explanations.", "uncertainty": "The direction is speculative.",
            "minimal_action": "Design a bounded pilot for approval.", "expected_learning": "Whether the mechanism is plausible.",
            "feasibility": "Requires available measurements.", "cost_risk": "Cost is unestimated; no execution authorized.",
        } for index in (1, 2)], "open_alternative": "Collect more original evidence before choosing."}
    else:
        output = {"recommendation_id": "d1", "reason": "Potentially cheaper to falsify, subject to evidence.",
                  "critiques": [{"direction_id": f"d{index}", "concerns": ["Mechanism is uncertain."],
                                 "test_before_commit": "Inspect original methods."} for index in (1, 2)],
                  "missing_evidence": ["Full methods."], "comparison_summary": "Both remain provisional research proposals.",
                  "disposition": "ready", "followup_queries": [], "questions": []}
    return {"output": output, "usage": {"input_tokens": 10, "output_tokens": 20},
            "provider": {"name": "isolated-test-fixture", "model": "no-model-called"}}


class IdeaStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "ideas"
        self.store = IdeaStore(self.root)

    def create(self, key="create", sources=True):
        return self.store.create({"idempotency_key": key, "brief": brief(sources)})

    def complete(self, item=None):
        item = item or self.create()
        item, fresh = self.store.begin(item["id"], {"idempotency_key": "generate", "expected_revision": item["revision"]})
        self.assertTrue(fresh)
        for stage in STAGES:
            item = self.store.save_stage(item["id"], item["generation_id"], stage, generated(stage, bool(item["brief"]["sources"])))
        return item

    def assert_error(self, code, operation):
        with self.assertRaises(IdeaError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_creation_deduplicates_and_input_bounds_are_enforced(self):
        item = self.create()
        self.assertEqual(self.create(), item)
        altered = brief()
        altered["goal"] = "Different"
        self.assert_error("idempotency_conflict", lambda: self.store.create({"idempotency_key": "create", "brief": altered}))
        invalid = [dict(brief(), goal=""), dict(brief(), goal="a" * 8001), dict(brief(), command="run")]
        duplicate = brief()
        duplicate["sources"] *= 2
        invalid.append(duplicate)
        large = brief()
        large["sources"][0]["text"] = "界" * 10001
        invalid.append(large)
        for value in invalid:
            self.assert_error("invalid_request", lambda: validate_brief(value))

    def test_generation_identity_and_revision_guards(self):
        item = self.create()
        request = {"idempotency_key": "once", "expected_revision": item["revision"]}
        started, fresh = self.store.begin(item["id"], request)
        self.assertTrue(fresh)
        repeated, fresh = self.store.begin(item["id"], request)
        self.assertFalse(fresh)
        self.assertEqual(repeated, started)
        self.assert_error("idempotency_conflict", lambda: self.store.begin(item["id"], dict(request, expected_revision=2)))
        self.assert_error("stale_revision", lambda: self.store.begin(item["id"], dict(request, idempotency_key="different")))
        self.assert_error("busy", lambda: self.store.begin(item["id"], {"idempotency_key": "different", "expected_revision": started["revision"]}))

    def test_three_stages_persist_exact_source_hashes_usage_and_prior_versions(self):
        completed = self.complete()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["revision"], 5)
        self.assertEqual(len(completed["usage"]), 3)
        evidence = completed["result"]["literature"]["evidence"][0]
        self.assertEqual(evidence["source_hash"], hashlib.sha256(brief()["sources"][0]["text"].encode()).hexdigest())
        self.assertEqual(evidence["source_support"], "exact_quote_verified")
        self.assertEqual(evidence["inference_validity"], "not_assessed")
        self.assertEqual(len(self.store.history(completed["id"])["items"]), 5)
        self.assertEqual(IdeaStore(self.root).get(completed["id"]), completed)

    def test_human_select_changes_goal_without_generating_or_authorizing_execution(self):
        completed = self.complete()
        result = self.store.decision(completed["id"], {"expected_revision": completed["revision"], "kind": "select",
                                                     "selected_id": "d2", "feedback": "Prefer the second for my equipment."})
        self.assertEqual(result["brief"]["goal"], completed["result"]["ideas"]["directions"][1]["question"])
        self.assertEqual(result["decision"]["feedback"], "Prefer the second for my equipment.")
        self.assertFalse(result["events"][-1]["execution_authorized"])
        self.assertEqual(len(result["usage"]), 3)
        self.assertEqual(self.store.history(result["id"])["items"][1]["brief"]["goal"], brief()["goal"])
        self.assert_error("stale_revision", lambda: self.store.decision(result["id"], {
            "expected_revision": completed["revision"], "kind": "select", "selected_id": "d1", "feedback": "stale"}))

    def test_revise_preserves_previous_result_in_history_and_defer_preserves_goal(self):
        completed = self.complete()
        revised = self.store.decision(completed["id"], {"expected_revision": completed["revision"], "kind": "revise",
                                                     "selected_id": None, "feedback": "Focus on a different question."})
        self.assertEqual(revised["status"], "draft")
        self.assertEqual(revised["result"], completed["result"])
        self.assertEqual(revised["brief"]["goal"], "Focus on a different question.")
        self.assertEqual(self.store.history(revised["id"])["items"][1]["result"], completed["result"])
        deferred = self.store.decision(revised["id"], {"expected_revision": revised["revision"], "kind": "defer",
                                                    "selected_id": None, "feedback": "Need more evidence."})
        self.assertEqual(deferred["brief"]["goal"], revised["brief"]["goal"])
        self.assertEqual(deferred["brief"]["feedback"][-1]["text"], "Need more evidence.")

    def test_unseen_source_or_misquoted_evidence_is_rejected_with_usage_preserved(self):
        for index, field in enumerate(("source_id", "quote")):
            item = self.create(key=str(index))
            item, _ = self.store.begin(item["id"], {"idempotency_key": "g", "expected_revision": 1})
            answer = generated("literature")
            answer["output"]["evidence"][0][field] = "unseen content"
            self.assert_error("invalid_output", lambda: self.store.save_stage(item["id"], item["generation_id"], "literature", answer))
            failed = self.store.get(item["id"])
            self.assertEqual(failed["status"], "failed")
            self.assertIsNone(failed["result"]["literature"])
            self.assertEqual(len(failed["usage"]), 1)
            self.assertNotIn("unseen content", failed["error"])

    def test_exact_validation_classifies_failures_without_whitespace_or_fuzzy_acceptance(self):
        examples = []
        answer = generated("literature")["output"]
        answer["evidence"][0]["quote"] = "Measured  value increased."
        examples.append((answer, brief(), "quote_not_exact"))
        answer = generated("literature")["output"]
        answer["evidence"][0]["source_id"] = "source-version-not-packet-id"
        examples.append((answer, brief(), "unknown_source_id"))
        answer = generated("literature")["output"]
        answer["evidence"].append(copy.deepcopy(answer["evidence"][0]))
        examples.append((answer, brief(), "duplicate_evidence_id"))
        answer, context = generated("literature")["output"], brief()
        context["sources"][0].update(text="one[GAP]two", provenance={"gap_marker": "[GAP]"})
        answer["evidence"][0]["quote"] = "one[GAP]two"
        examples.append((answer, context, "quote_crosses_omission"))
        for output, context, reason in examples:
            with self.subTest(reason=reason), self.assertRaises(IdeaError) as caught:
                validate_output("literature", output, context, {})
            diagnostic = caught.exception.diagnostics
            self.assertIn(reason, diagnostic["failure_codes"])
            self.assertLessEqual(len(json.dumps(diagnostic, ensure_ascii=False).encode()), MAX_VALIDATION_DIAGNOSTIC_BYTES)
            self.assertTrue(diagnostic["repairable"])

    def test_rejected_output_is_bounded_private_artifact_and_session_scoped(self):
        item = self.create()
        item, _ = self.store.begin(item["id"], {"idempotency_key": "g", "expected_revision": 1})
        answer = generated("literature")
        answer["output"]["evidence"][0]["quote"] = "UNVERIFIED_PRIVATE_TEXT"
        self.assert_error("invalid_output", lambda: self.store.save_stage(item["id"], item["generation_id"], "literature", answer))
        failed = self.store.get(item["id"])
        self.assertNotIn("UNVERIFIED_PRIVATE_TEXT", json.dumps(failed))
        attempt = failed["research"]["validation_attempts"][0]
        self.assertEqual(attempt["diagnostics"]["failure_codes"], ["quote_not_exact"])
        artifact = self.store.stage_attempt(item["id"], attempt["id"])
        self.assertEqual(artifact["output"], answer["output"])
        self.assertEqual(artifact["usage"], answer["usage"])
        self.assertEqual(artifact["provider"], answer["provider"])
        self.assertEqual(IdeaStore(self.root).stage_attempt(item["id"], attempt["id"]), artifact)
        another = self.create("another")
        self.assert_error("not_found", lambda: self.store.stage_attempt(another["id"], attempt["id"]))

    def test_oversized_rejected_artifact_keeps_hash_and_usage_without_unbounded_body(self):
        item = self.create()
        item, _ = self.store.begin(item["id"], {"idempotency_key": "g", "expected_revision": 1})
        answer = generated("literature")
        answer["output"]["evidence"] = [dict(answer["output"]["evidence"][0], id=f"e{i}", quote="q" * 12000) for i in range(20)]
        self.assert_error("invalid_output", lambda: self.store.save_stage(item["id"], item["generation_id"], "literature", answer, True))
        failed = self.store.get(item["id"])
        attempt = self.store.stage_attempt(item["id"], failed["research"]["validation_attempts"][0]["id"])
        self.assertIsNone(attempt["output"])
        self.assertFalse(attempt["output_retained"])
        self.assertGreater(attempt["output_bytes"], MAX_STAGE_ARTIFACT_BYTES)
        self.assertEqual(len(attempt["output_hash"]), 64)
        self.assertEqual(attempt["diagnostics"]["kind"], "output_too_large")
        self.assertEqual(len(failed["usage"]), 1)
        self.assertIsNone(failed["research"]["pending_repair"])

    def test_invalid_direction_evidence_and_review_references_are_rejected(self):
        literature = validate_output("literature", generated("literature")["output"], brief(), {})
        answer = generated("ideas")["output"]
        answer["directions"][0]["evidence_ids"] = ["not-supplied"]
        self.assert_error("invalid_output", lambda: validate_output("ideas", answer, brief(), {"literature": literature}))
        ideas = validate_output("ideas", generated("ideas")["output"], brief(), {"literature": literature})
        answer = generated("review")["output"]
        answer["recommendation_id"] = "absent"
        self.assert_error("invalid_output", lambda: validate_output("review", answer, brief(), {"ideas": ideas}))

    def test_empty_sources_produce_explicit_provisional_directions(self):
        result = self.complete(self.create(sources=False))
        self.assertEqual(result["result"]["literature"]["evidence"], [])
        self.assertTrue(result["result"]["literature"]["gaps"])
        self.assertTrue(all(direction["grounding"] == "provisional" for direction in result["result"]["ideas"]["directions"]))
        answer = generated("literature", False)["output"]
        answer["gaps"] = []
        self.assert_error("invalid_output", lambda: validate_output("literature", answer, brief(False), {}))

    def test_cancelled_or_replaced_generation_cannot_accept_late_outputs(self):
        item = self.create()
        item, _ = self.store.begin(item["id"], {"idempotency_key": "g1", "expected_revision": 1})
        generation_id = item["generation_id"]
        stopped = self.store.stop(item["id"], generation_id, "cancelled")
        self.assert_error("stale_generation", lambda: self.store.save_stage(item["id"], generation_id, "literature", generated("literature")))
        new, _ = self.store.begin(item["id"], {"idempotency_key": "g2", "expected_revision": stopped["revision"]})
        self.assert_error("stale_generation", lambda: self.store.save_stage(item["id"], generation_id, "literature", generated("literature")))
        self.assertEqual(self.store.get(item["id"]), new)

    def test_restart_marks_running_interrupted_without_rerun(self):
        item = self.create()
        item, _ = self.store.begin(item["id"], {"idempotency_key": "g", "expected_revision": 1})
        item = self.store.save_stage(item["id"], item["generation_id"], "literature", generated("literature"))
        restarted = IdeaStore(self.root)
        restarted.interrupt_running()
        result = restarted.get(item["id"])
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["result"], item["result"])
        repeated, fresh = restarted.begin(item["id"], {"idempotency_key": "g", "expected_revision": 1})
        self.assertFalse(fresh)
        self.assertEqual(repeated["status"], "interrupted")

    def test_failed_write_leaves_previous_revision_unchanged(self):
        item = self.create()
        with patch.object(IdeaStore, "_save", side_effect=sqlite3.OperationalError("isolated failure")):
            self.assert_error("storage_error", lambda: self.store.begin(item["id"], {"idempotency_key": "g", "expected_revision": 1}))
        self.assertEqual(self.store.get(item["id"]), item)
        self.assertEqual(len(self.store.history(item["id"])["items"]), 1)


class IdeaControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "ideas"
        self.calls = []

    def controller(self, generate):
        controller = IdeaController(self.root, generate)
        self.addAsyncCleanup(controller.shutdown)
        return controller

    async def test_generation_is_three_sequential_calls_with_saved_context(self):
        async def generate(stage, current_brief, previous):
            self.calls.append(stage)
            if stage == "ideas":
                self.assertEqual(previous["literature"]["evidence"][0]["source_support"], "exact_quote_verified")
            return generated(stage)
        controller = self.controller(generate)
        item = controller.store.create({"idempotency_key": "c", "brief": brief()})
        started = await controller.start(item["id"], {"idempotency_key": "g", "expected_revision": 1})
        task = controller.jobs[item["id"]]
        await task
        self.assertEqual(self.calls, list(STAGES))
        self.assertEqual(controller.store.get(item["id"])["status"], "completed")
        await controller.start(item["id"], {"idempotency_key": "g", "expected_revision": 1})
        self.assertEqual(self.calls, list(STAGES))
        self.assertEqual(started["status"], "running")

    async def test_cancel_ack_waits_for_provider_cleanup_and_preserves_completed_stage(self):
        entered, cleaning, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def generate(stage, current_brief, previous):
            self.calls.append(stage)
            if stage == "ideas":
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    cleaning.set()
                    await closed.wait()
            return generated(stage)
        controller = self.controller(generate)
        item = controller.store.create({"idempotency_key": "c", "brief": brief()})
        await controller.start(item["id"], {"idempotency_key": "g", "expected_revision": 1})
        await asyncio.wait_for(entered.wait(), 3)
        current = controller.store.get(item["id"])
        cancelling = asyncio.create_task(controller.cancel(item["id"], {"expected_revision": current["revision"]}))
        await asyncio.wait_for(cleaning.wait(), 3)
        repeated_cancel = asyncio.create_task(controller.cancel(item["id"], {"expected_revision": current["revision"]}))
        await asyncio.sleep(0.01)
        self.assertFalse(cancelling.done())
        self.assertFalse(repeated_cancel.done())
        self.assertEqual(controller.store.get(item["id"])["status"], "running")
        closed.set()
        result = await asyncio.wait_for(cancelling, 3)
        self.assertEqual(await asyncio.wait_for(repeated_cancel, 3), result)
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNotNone(result["result"]["literature"])
        self.assertIsNone(result["result"]["ideas"])
        self.assertEqual(self.calls, ["literature", "ideas"])

    async def test_disconnected_start_still_assigns_its_durable_generation(self):
        entered, release = threading.Event(), threading.Event()
        provider_entered = asyncio.Event()
        async def generate(stage, current_brief, previous):
            provider_entered.set()
            await asyncio.Future()
        controller = self.controller(generate)
        item = controller.store.create({"idempotency_key": "c", "brief": brief()})
        original_begin = controller.store.begin
        def delayed_begin(*args):
            result = original_begin(*args)
            entered.set()
            if not release.wait(3):
                raise AssertionError("Fixture admission was not released")
            return result
        with patch.object(controller.store, "begin", side_effect=delayed_begin):
            starting = asyncio.create_task(controller.start(item["id"], {"idempotency_key": "g", "expected_revision": 1}))
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            starting.cancel()
            await asyncio.sleep(0)
            starting.cancel()
            await asyncio.sleep(0)
            self.assertFalse(starting.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await starting
        await asyncio.wait_for(provider_entered.wait(), 3)
        self.assertIn(item["id"], controller.jobs)
        self.assertEqual(controller.store.get(item["id"])["status"], "running")

    async def test_provider_failure_is_content_free_and_preserves_prior_outputs(self):
        async def generate(stage, current_brief, previous):
            if stage == "ideas":
                raise RuntimeError("PRIVATE PROMPT OR TOKEN")
            return generated(stage)
        controller = self.controller(generate)
        item = controller.store.create({"idempotency_key": "c", "brief": brief()})
        await controller.start(item["id"], {"idempotency_key": "g", "expected_revision": 1})
        await controller.jobs[item["id"]]
        result = controller.store.get(item["id"])
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("PRIVATE", result["error"])
        self.assertIsNotNone(result["result"]["literature"])

    async def test_safe_native_provider_error_retains_actionable_code(self):
        async def generate(stage, current_brief, previous):
            raise IdeaGenerationError("native_login_required", "Existing Codex ChatGPT login is required")
        controller = self.controller(generate)
        item = controller.store.create({"idempotency_key": "c", "brief": brief()})
        await controller.start(item["id"], {"idempotency_key": "g", "expected_revision": 1})
        await controller.jobs[item["id"]]
        result = controller.store.get(item["id"])
        self.assertEqual(result["error"], "[native_login_required] Existing Codex ChatGPT login is required")

    async def test_shutdown_closes_provider_and_records_interrupted(self):
        entered, closed = asyncio.Event(), asyncio.Event()
        async def generate(stage, current_brief, previous):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                closed.set()
        controller = self.controller(generate)
        item = controller.store.create({"idempotency_key": "c", "brief": brief()})
        await controller.start(item["id"], {"idempotency_key": "g", "expected_revision": 1})
        await asyncio.wait_for(entered.wait(), 3)
        await controller.shutdown()
        self.assertTrue(closed.is_set())
        self.assertEqual(controller.store.get(item["id"])["status"], "interrupted")
        self.assertEqual(controller.jobs, {})


if __name__ == "__main__":
    unittest.main()
