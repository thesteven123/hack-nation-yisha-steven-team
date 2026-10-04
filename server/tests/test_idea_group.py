"""Bounded complete-group orchestration with deterministic native/reader fixtures."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from idea_lab import IdeaError, IdeaStore, LIMITS, validate_output
from idea_lab_routes import IdeaController
from idea_generation import MAX_PROMPT_BYTES, stage_prompt
from idea_generation import IdeaGenerationError
from tests.test_idea_lab import brief, generated


class IdeaGroupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "research"
        self.search_contexts = []
        self.model_packets = []
        self.reading_briefs = []
        self.reviews = ["ready"]
        self.empty = False
        self.questions = []
        self.provisional = False

        async def discover(current_brief, context, on_event):
            self.search_contexts.append(copy.deepcopy(context))
            await on_event({"agent": "literature", "type": "native_search", "summary": "Native fixture search completed",
                            "thread_id": "fixture-search-thread", "turn_id": "fixture-search-turn", "query": "original measurement source"})
            return {"papers": [{"title": "Actual fixture bytes", "url": "https://example.org/paper", "reason": "Relevant original source"}],
                    "searches": [{"query": (context["requested_queries"] or ["initial query"])[0], "query_count": 2,
                                  "queries": ["original measurement source", "opposing measurement result"], "receipt_hash": "fixture-receipt"}],
                    "summary": "Bounded native search", "gaps": [], "usage": {"total_tokens": 100}, "provider": {"backend": "fixture"}}

        async def retrieve(candidates, current_brief, on_event):
            self.reading_briefs.append(copy.deepcopy(current_brief))
            await on_event({"agent": "literature", "type": "source_read", "summary": "Parsed fixture source", "source_id": "s1"})
            source = brief()["sources"][0]
            source.update(coverage={"kind": "partial", "truncated": False, "limitations": ["Fixture excerpt"]},
                          provenance={"content_hash": hashlib.sha256(source["text"].encode()).hexdigest(), "parser_version": "fixture-v1"})
            return {"sources": [] if self.empty else [source], "papers": [{"id": "s1", "title": "Actual fixture bytes", "url": candidates[0]["url"],
                    "status": "unavailable" if self.empty else "read", "access": "unavailable" if self.empty else "partial",
                    "coverage": source["coverage"], "content_hash": source["provenance"]["content_hash"], "parser_version": "fixture-v1",
                    "retrieved_at": "2030-01-01T00:00:00Z", "cache_hit": len(self.reading_briefs) > 1, "error": "Unavailable" if self.empty else None}],
                    "coverage_gaps": ["Methods unavailable"] if self.empty else ["Original methods need verification"], "usage": {"requests": 1}}

        async def generate(stage, current_brief, previous, on_event):
            self.model_packets.append((stage, copy.deepcopy(current_brief), copy.deepcopy(previous)))
            # A provider call must never hold the storage write transaction.
            self.controller.store.get(self.session_id)
            await on_event({"agent": "literature" if stage == "literature" else "idea", "type": "native_task", "task": stage,
                            "summary": "Executed fixture native role", "thread_id": "fixture-" + stage, "turn_id": "fixture-turn"})
            answer = generated(stage, not self.empty)
            if stage == "ideas" and self.provisional:
                for direction in answer["output"]["directions"]:
                    direction["evidence_ids"] = []
            if stage == "review":
                disposition = self.reviews.pop(0) if len(self.reviews) > 1 else self.reviews[0]
                answer["output"].update(disposition=disposition, followup_queries=["measurement contradictory primary study"] if disposition == "retrieve_more" else [],
                                        questions=copy.deepcopy(self.questions))
            return answer

        self.controller = IdeaController(self.root, generate, discover, retrieve)
        self.addAsyncCleanup(self.controller.shutdown)
        item = self.controller.store.create({"idempotency_key": "create", "brief": brief(False)})
        self.session_id = item["id"]

    async def run_generation(self, key="generate"):
        item = self.controller.store.get(self.session_id)
        await self.controller.start(self.session_id, {"idempotency_key": key, "expected_revision": item["revision"]})
        await self.controller.jobs[self.session_id]
        return self.controller.store.get(self.session_id)

    async def test_goal_only_discovers_reads_hands_off_and_prepares_grounded_choice(self):
        result = await self.run_generation()
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(result["research"]["readiness"], "ready_for_choice")
        self.assertEqual(result["research"]["used"], {"model_calls": 4, "searches": 2, "reads": 1, "cache_hits": 0})
        self.assertEqual(len(result["research"]["rounds"]), 1)
        self.assertTrue(any(event["type"] == "handoff" for event in result["research"]["activities"]))
        self.assertEqual(result["decision_card"]["brief_revision"], 1)
        self.assertFalse(result["decision_card"]["execution_authorized"])
        source = self.controller.store.paper(self.session_id, "s1")["source"]
        evidence = result["result"]["literature"]["evidence"][0]
        self.assertEqual(source["text"][evidence["span"]["start"]:evidence["span"]["end"]], evidence["quote"])
        self.assertEqual(evidence["inference_validity"], "not_assessed")

    def reject_extractions(self, count):
        original = self.controller.generate
        calls = []
        async def generate(stage, current_brief, previous, on_event):
            answer = await original(stage, current_brief, previous, on_event)
            if stage == "literature":
                calls.append((copy.deepcopy(current_brief), copy.deepcopy(previous)))
                if len(calls) <= count:
                    answer["output"]["evidence"][0]["quote"] = "This is a paraphrase, not a source quotation."
            return answer
        self.controller.generate = generate
        return calls

    async def test_quote_failure_repairs_once_on_same_packet_and_accounts_for_both_jobs(self):
        calls = self.reject_extractions(1)
        result = await self.run_generation()
        self.assertEqual(result["research"]["readiness"], "ready_for_choice")
        self.assertEqual(result["research"]["used"]["model_calls"], 5)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], calls[1][0])
        self.assertEqual(calls[0][1]["context"], calls[1][1]["context"])
        self.assertNotIn("repair", calls[0][1])
        repair = calls[1][1]["repair"]
        self.assertEqual(repair["diagnostics"]["failure_codes"], ["quote_not_exact"])
        attempts = [entry for entry in result["research"]["validation_attempts"] if entry["stage"] == "literature"]
        self.assertEqual([entry["status"] for entry in attempts], ["rejected", "accepted"])
        self.assertEqual(attempts[1]["repair_of"], attempts[0]["id"])
        self.assertEqual(attempts[0]["input_ref"]["packet_digest"], attempts[1]["input_ref"]["packet_digest"])
        self.assertEqual(repair["attempt_id"], attempts[0]["id"])
        self.assertEqual(len([usage for usage in result["usage"] if usage["stage"] == "literature"]), 2)
        self.assertEqual(len([event for event in result["research"]["activities"] if event["type"] == "citation_repair_started"]), 1)
        rejected = self.controller.store.stage_attempt(self.session_id, attempts[0]["id"])
        self.assertIn("paraphrase", rejected["output"]["evidence"][0]["quote"])
        evidence = result["result"]["literature"]["evidence"][0]
        self.assertEqual(evidence["quote"], "Measured value increased.")
        self.assertEqual(evidence["source_support"], "exact_quote_verified")

    async def test_repeated_invalid_quotes_stop_after_one_repair_without_publishing(self):
        calls = self.reject_extractions(10)
        result = await self.run_generation()
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["research"]["stop_reason"], "evidence_validation_failed")
        self.assertEqual(result["research"]["used"]["model_calls"], 3)
        self.assertIsNone(result["result"]["literature"])
        self.assertIsNone(result["result"]["ideas"])
        self.assertIsNone(result["research"]["pending_repair"])
        self.assertEqual([entry["status"] for entry in result["research"]["validation_attempts"]], ["rejected", "rejected"])
        self.assertTrue(all(agent["status"] == "failed" and agent["task"] is None for agent in result["research"]["agents"]))

    async def test_repair_never_exceeds_global_native_job_budget(self):
        calls = self.reject_extractions(1)
        with patch.dict(LIMITS, max_model_calls=2):
            result = await self.run_generation()
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["research"]["used"]["model_calls"], 2)
        self.assertEqual(result["research"]["readiness"], "evidence_limited")
        self.assertEqual(result["research"]["stop_reason"], "budget_exhausted")
        self.assertIsNone(result["result"]["literature"])
        self.assertIsNone(result["research"]["pending_repair"])
        self.assertEqual(len(result["research"]["validation_attempts"]), 1)

    async def test_exhausted_budget_never_pairs_new_ideas_with_previous_round_review(self):
        self.reviews = ["retrieve_more"]
        self.reject_extractions(1)
        with patch.dict(LIMITS, max_model_calls=8):
            result = await self.run_generation()
        # Round one costs five jobs including repair. Round two can search,
        # extract and propose, but has no reservation left for a fresh review.
        self.assertEqual(result["research"]["used"]["model_calls"], 8)
        self.assertEqual(result["research"]["readiness"], "evidence_limited")
        self.assertIsNotNone(result["result"]["ideas"])
        self.assertIsNone(result["result"]["review"])
        self.assertIsNone(result["decision_card"]["recommendation_id"])
        self.assertEqual(result["decision_card"]["reason"], "")
        self.assertEqual(result["research"]["rounds"][0]["review"]["recommendation_id"], "d1")

    async def test_cancelled_repair_cannot_publish_even_if_provider_returns_a_late_answer(self):
        original = self.controller.generate
        repairing, cleaned = asyncio.Event(), asyncio.Event()
        async def generate(stage, current_brief, previous, on_event):
            if previous.get("repair"):
                repairing.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    return generated("literature")
                finally:
                    cleaned.set()
            answer = await original(stage, current_brief, previous, on_event)
            if stage == "literature":
                answer["output"]["evidence"][0]["quote"] = "Unmatched quote"
            return answer
        self.controller.generate = generate
        item = self.controller.store.get(self.session_id)
        await self.controller.start(self.session_id, {"idempotency_key": "g", "expected_revision": item["revision"]})
        await asyncio.wait_for(repairing.wait(), 3)
        current = self.controller.store.get(self.session_id)
        stopped = await self.controller.cancel(self.session_id, {"expected_revision": current["revision"]})
        self.assertTrue(cleaned.is_set())
        self.assertEqual(stopped["status"], "cancelled")
        self.assertIsNone(stopped["result"]["literature"])
        self.assertIsNone(stopped["research"]["pending_repair"])
        self.assertEqual(len(stopped["research"]["validation_attempts"]), 1)
        self.assertNotIn(self.session_id, self.controller.jobs)

    async def test_provider_timeout_does_not_trigger_evidence_repair(self):
        calls = []
        async def generate(stage, current_brief, previous, on_event):
            calls.append(stage)
            raise IdeaGenerationError("provider_timeout", "Outcome or usage may be incomplete")
        self.controller.generate = generate
        result = await self.run_generation()
        self.assertEqual(calls, ["literature"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["research"]["used"]["model_calls"], 2)
        self.assertIsNone(result["research"]["pending_repair"])
        self.assertEqual(result["research"]["validation_attempts"], [])

    async def test_reviewer_gap_causes_changed_search_and_targeted_reading(self):
        self.reviews = ["retrieve_more", "ready"]
        result = await self.run_generation()
        self.assertEqual(result["research"]["round"], 2)
        self.assertEqual(result["research"]["used"]["model_calls"], 8)
        self.assertEqual(self.search_contexts[1]["requested_queries"], ["measurement contradictory primary study"])
        self.assertIn("Full methods", self.reading_briefs[1]["reading_focus"])
        self.assertEqual(result["research"]["rounds"][0]["disposition"], "retrieve_more")
        self.assertEqual(result["research"]["used"]["cache_hits"], 1)

    async def test_completed_review_does_not_work_during_next_round_reading(self):
        self.reviews = ["retrieve_more", "ready"]
        original_generate, original_retrieve = self.controller.generate, self.controller.retrieve
        reading, release = asyncio.Event(), asyncio.Event()
        after_followup = []

        async def generate(stage, current_brief, previous, on_event):
            result = await original_generate(stage, current_brief, previous, on_event)
            await on_event({"agent": "literature" if stage == "literature" else "idea",
                            "type": "provider_task_completed", "task": stage, "summary": "Native fixture completed"})
            return result

        async def retrieve(candidates, current_brief, on_event):
            if len(self.reading_briefs) == 1:
                reading.set()
                await release.wait()
            return await original_retrieve(candidates, current_brief, on_event)

        original_activity = self.controller.store.activity
        def activity(session_id, generation_id, event):
            original_activity(session_id, generation_id, event)
            if event.get("type") == "followup_requested":
                after_followup.append(self.controller.store.get(session_id)["research"]["agents"])

        self.controller.generate, self.controller.retrieve = generate, retrieve
        self.controller.store.activity = activity
        await self.controller.start(self.session_id, {"idempotency_key": "member-state", "expected_revision": 1})
        task = self.controller.jobs[self.session_id]
        try:
            await asyncio.wait_for(reading.wait(), 3)
            item = self.controller.store.get(self.session_id)
            members = {agent["id"]: agent for agent in item["research"]["agents"]}
            self.assertEqual(item["research"]["round"], 2)
            self.assertEqual((members["literature"]["status"], members["literature"]["task"]), ("working", "reading"))
            self.assertEqual((members["idea"]["status"], members["idea"]["task"]), ("idle", None))
            reviewer = next(agent for agent in after_followup[0] if agent["id"] == "idea")
            self.assertEqual((reviewer["status"], reviewer["task"]), ("idle", None))
            self.assertTrue(any(event["type"] == "followup_requested" for event in item["research"]["activities"]))
        finally:
            release.set()
            await task

    async def test_no_evidence_searches_again_and_stops_truthfully_at_budget(self):
        self.empty = True
        result = await self.run_generation()
        self.assertEqual(len(self.search_contexts), 3)
        self.assertNotEqual(self.search_contexts[1]["requested_queries"], self.search_contexts[2]["requested_queries"])
        self.assertEqual(result["research"]["used"]["model_calls"], 12)
        self.assertEqual(result["research"]["readiness"], "evidence_limited")
        self.assertEqual(result["research"]["stop_reason"], "bounded_rounds_exhausted")
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(result["result"]["literature"]["evidence"], [])

    async def test_unrelated_evidence_does_not_make_unlinked_recommendation_ready(self):
        self.provisional = True
        result = await self.run_generation()
        self.assertTrue(result["result"]["literature"]["evidence"])
        self.assertEqual(result["research"]["readiness"], "evidence_limited")
        self.assertEqual(len(self.search_contexts), 3)

    async def test_required_human_context_stops_before_more_search_even_without_evidence(self):
        self.empty = True
        self.reviews = ["needs_input"]
        self.questions = [{"id": "q1", "question": "Which organism is in scope?", "why": "The answer changes source selection", "required": True, "options": []}]
        result = await self.run_generation()
        self.assertEqual(len(self.search_contexts), 1)
        self.assertEqual(result["research"]["readiness"], "needs_input")
        self.assertEqual(result["research"]["stop_reason"], "required_human_decision")

    async def test_refinement_reuses_actual_sources_but_extracts_for_new_priorities(self):
        result = await self.run_generation()
        request = {"expected_revision": result["revision"], "idempotency_key": "followup", "feedback": "Prioritize low cost.", "mode": "refine"}
        draft = await self.controller.followup(self.session_id, request)
        self.assertEqual(draft["brief"]["goal"], brief(False)["goal"])
        self.assertEqual(draft["result"], result["result"])
        revised = await self.run_generation("generation-2")
        self.assertEqual(len(self.search_contexts), 1)
        self.assertEqual(revised["research"]["used"]["model_calls"], 3)
        self.assertEqual(revised["research"]["used"]["searches"], 0)
        self.assertEqual(revised["research"]["readiness"], "ready_for_choice")
        self.assertEqual(self.model_packets[-3][0], "literature")
        self.assertEqual(self.model_packets[-3][1]["feedback"][-1]["text"], "Prioritize low cost.")
        self.assertEqual(await self.controller.followup(self.session_id, request), draft)
        self.assertEqual(len(self.search_contexts), 1)

    async def test_required_answer_is_retained_and_not_invented(self):
        self.reviews = ["needs_input"]
        self.questions = [{"id": "q1", "question": "Which resource constraint applies?", "why": "Changes feasibility", "required": True, "options": ["CPU only", "GPU available"]}]
        result = await self.run_generation()
        self.assertEqual(result["research"]["readiness"], "needs_input")
        request = {"expected_revision": result["revision"], "idempotency_key": "f", "feedback": "", "mode": "refine"}
        with self.assertRaises(IdeaError):
            await self.controller.followup(self.session_id, request)
        request["answers"] = [{"question_id": "q1", "answer": "CPU only"}]
        draft = await self.controller.followup(self.session_id, request)
        self.assertEqual(draft["decision"]["answers"], request["answers"])
        self.assertEqual(draft["decision"]["feedback"], "")
        self.assertEqual(draft["brief"]["feedback"][-1]["answers"], request["answers"])
        self.assertEqual(draft["status"], "draft")

    async def test_explicit_continue_can_keep_feedback_empty_without_changing_goal(self):
        result = await self.run_generation()
        draft = await self.controller.followup(self.session_id, {"expected_revision": result["revision"], "idempotency_key": "f", "feedback": "", "mode": "research"})
        self.assertEqual(draft["brief"]["goal"], result["brief"]["goal"])
        self.assertEqual(draft["decision"]["feedback"], "")
        self.assertEqual(len(self.search_contexts), 1)

    async def test_activity_journal_is_paginated_while_envelope_remains_bounded(self):
        item, _ = self.controller.store.begin(self.session_id, {"idempotency_key": "g", "expected_revision": 1}, full_research=True)
        for index in range(110):
            self.controller.store.activity(self.session_id, item["generation_id"], {"agent": "literature", "type": "receipt", "summary": str(index)})
        current = self.controller.store.get(self.session_id)
        self.assertEqual(len(current["research"]["activities"]), 80)
        first = self.controller.store.activities(self.session_id)
        second = self.controller.store.activities(self.session_id, first["next_before"])
        self.assertEqual(len(first["items"]) + len(second["items"]), 110)
        self.assertFalse(second["has_more"])

    async def test_answered_decision_question_does_not_block_later_continuation(self):
        self.reviews = ["needs_input"]
        self.questions = [{"id": "q1", "question": "What equipment is available?", "why": "Changes feasibility", "required": True, "options": []}]
        result = await self.run_generation()
        selected = await self.controller.decision(self.session_id, {"expected_revision": result["revision"], "kind": "select", "selected_id": "d1", "feedback": "",
            "answers": [{"question_id": "q1", "answer": "CPU only"}]})
        self.assertEqual(selected["research"]["questions"], [])
        self.assertEqual(selected["decision"]["answered_questions"][0]["id"], "q1")
        draft = await self.controller.followup(self.session_id, {"expected_revision": selected["revision"], "idempotency_key": "later", "feedback": "Find methods.", "mode": "research"})
        self.assertEqual(draft["status"], "draft")

    async def test_previous_source_packet_remains_readable_after_new_run_starts(self):
        result = await self.run_generation()
        expected = self.controller.store.paper(self.session_id, "s1")["source"]
        draft = await self.controller.followup(self.session_id, {"expected_revision": result["revision"], "idempotency_key": "again", "feedback": "", "mode": "research"})
        self.controller.store.begin(self.session_id, {"expected_revision": draft["revision"], "idempotency_key": "new"}, full_research=True)
        archived = self.controller.store.paper(self.session_id, "s1")
        self.assertTrue(archived["archived"])
        self.assertEqual(archived["source"], expected)

    def freeze_projected_sources(self, *, quote_at_start=False):
        store = self.controller.store
        item, _ = store.begin(self.session_id, {"expected_revision": 1, "idempotency_key": "bounded"}, full_research=True)
        generation = item["generation_id"]
        quote = "Measured value increased."
        sources = [{"id": f"s{index}", "title": "Long original source", "uri": f"fixture:{index}", "text": "a" * 30000,
                    "coverage": {"kind": "full_text", "truncated": False, "limitations": []}} for index in range(1, 5)]
        sources[-1]["text"] = quote + "a" * (30000 - len(quote)) if quote_at_start else "a" * (30000 - len(quote)) + quote
        store.retrieval(self.session_id, generation, {"sources": sources, "papers": [], "coverage_gaps": [], "usage": {}})
        store.work(self.session_id, generation, "literature", model=True, new_round=True)
        projected = self.controller._model_sources(sources)
        store.freeze_packet(self.session_id, generation, "literature", projected, {"round": 1})
        answer = generated("literature")
        answer["output"]["evidence"][0]["source_id"] = "s4"
        return store, generation, projected, answer

    async def test_quote_outside_actual_sent_packet_is_rejected_with_terminal_agents(self):
        store, generation, projected, answer = self.freeze_projected_sources()
        self.assertNotIn(answer["output"]["evidence"][0]["quote"], projected[-1]["text"])
        store.activity(self.session_id, generation, {"agent": "literature", "type": "provider_task_started", "task": "literature", "summary": "Actual work"})
        with self.assertRaises(IdeaError):
            store.save_stage(self.session_id, generation, "literature", answer)
        failed = store.get(self.session_id)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["research"]["readiness"], "failed")
        self.assertTrue(all(agent["status"] == "failed" and agent["task"] is None for agent in failed["research"]["agents"]))

    async def test_quote_in_truncated_packet_retains_actual_projection_coverage(self):
        store, generation, projected, answer = self.freeze_projected_sources(quote_at_start=True)
        result = store.save_stage(self.session_id, generation, "literature", answer)
        evidence = result["result"]["literature"]["evidence"][0]
        self.assertEqual(evidence["coverage"]["kind"], "partial")
        self.assertTrue(evidence["coverage"]["model_packet_truncated"])
        self.assertEqual(evidence["source_hash"], hashlib.sha256(projected[-1]["text"].encode()).hexdigest())
        exact = store.paper(self.session_id, "s4", evidence["source_hash"])
        self.assertEqual(exact["source"], projected[-1])
        self.assertEqual(exact["packet_hash"], evidence["source_hash"])
        self.assertEqual(exact["generation_id"], generation)
        self.assertNotEqual(exact["packet_hash"], store.paper(self.session_id, "s4")["packet_hash"])

    async def test_hash_resolves_original_model_packet_after_new_generation_and_is_session_scoped(self):
        store, generation, projected, answer = self.freeze_projected_sources(quote_at_start=True)
        result = store.save_stage(self.session_id, generation, "literature", answer)
        evidence = result["result"]["literature"]["evidence"][0]
        stopped = store.stop(self.session_id, generation, "cancelled")
        store.begin(self.session_id, {"expected_revision": stopped["revision"], "idempotency_key": "new"}, full_research=True)
        exact = store.paper(self.session_id, "s4", evidence["source_hash"])
        self.assertTrue(exact["archived"])
        self.assertEqual(exact["source"], projected[-1])
        self.assertEqual(exact["packet_hash"], evidence["source_hash"])
        other = store.create({"idempotency_key": "unrelated", "brief": brief(False)})
        with self.assertRaises(IdeaError) as caught:
            store.paper(other["id"], "s4", evidence["source_hash"])
        self.assertEqual(caught.exception.code, "not_found")

    async def test_fair_packet_keeps_five_large_pastes_and_two_retrieved_sources_with_exact_coverage(self):
        marker = "\n[... omitted source text; not a continuous quotation ...]\n"
        sources = [{"id": f"paste{index}", "title": "Pasted material", "uri": f"paste:{index}", "text": "甲" * 10000}
                   for index in range(5)]
        original = "a" * 9000 + "unread" * 333 + "xx" + "b" * 9000
        sources.append({"id": "retrieved1", "title": "Retrieved passages", "uri": "https://example.org/one",
                        "text": original[:9000] + marker + original[11000:], "provenance": {"gap_marker": marker},
                        "coverage": {"kind": "partial", "truncated": True, "characters_total": len(original),
                                     "characters_in_packet": 18000, "limitations": ["Declared gap"],
                                     "spans": [{"start": 0, "end": 9000}, {"start": 11000, "end": 20000}]}})
        second = "Measured value increased." + "é" * 8970
        sources.append({"id": "retrieved2", "title": "Latest retrieved source", "uri": "https://example.org/two", "text": second,
                        "coverage": {"kind": "full_text", "truncated": False, "characters_total": len(second),
                                     "characters_in_packet": len(second), "limitations": [], "spans": [{"start": 0, "end": len(second)}]}})
        saved = copy.deepcopy(sources)
        projected = self.controller._model_sources(sources)
        self.assertEqual(sources, saved)
        self.assertEqual({source["id"] for source in projected}, {source["id"] for source in sources})
        self.assertLessEqual(sum(len(source["text"].encode()) for source in projected), 100 * 1024)
        prompt = stage_prompt("literature", {**brief(False), "sources": projected}, {"context": {"round": 1}})
        self.assertLessEqual(len(prompt.encode()), MAX_PROMPT_BYTES)
        self.assertTrue(all(len(source["text"].encode()) >= 14000 for source in projected))
        for source in projected:
            self.assertEqual(source["coverage"]["kind"], "partial")
            self.assertTrue(source["coverage"]["model_packet_truncated"])
            self.assertEqual(source["coverage"]["model_projection"]["source_packet_end"], len(source["text"]))
        retrieved = projected[-2]
        spans = retrieved["coverage"]["spans"]
        self.assertEqual(marker.join(original[span["start"]:span["end"]] for span in spans), retrieved["text"])
        self.assertEqual(sum(span["end"] - span["start"] for span in spans), retrieved["coverage"]["characters_in_packet"])
        output = generated("literature")["output"]
        template = output["evidence"][0]
        output["evidence"] = [{**template, "id": f"e{index}", "source_id": source["id"], "quote": source["text"][:12]}
                              for index, source in enumerate(projected)]
        checked = validate_output("literature", output, {"sources": projected}, {})
        for source, evidence in zip(projected, checked["evidence"]):
            self.assertEqual(source["text"][evidence["span"]["start"]:evidence["span"]["end"]], evidence["quote"])
            self.assertEqual(hashlib.sha256(source["text"].encode()).hexdigest(), evidence["source_hash"])

    async def test_restart_clears_working_member_tasks(self):
        item, _ = self.controller.store.begin(self.session_id, {"expected_revision": 1, "idempotency_key": "interrupted"}, full_research=True)
        self.controller.store.activity(self.session_id, item["generation_id"], {"agent": "idea", "type": "provider_task_started", "task": "review", "summary": "Working"})
        self.controller.store.interrupt_running()
        result = self.controller.store.get(self.session_id)
        self.assertEqual(result["status"], "interrupted")
        self.assertTrue(all(agent["task"] is None and agent["status"] == "interrupted" for agent in result["research"]["agents"]))


class IdeaMigrationTests(unittest.TestCase):
    def test_schema_one_records_are_preserved_and_never_researched_on_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "ideas.sqlite3"
            item = {"id": "a" * 32, "revision": 5, "created_at": "before", "updated_at": "before", "brief": brief(False),
                    "status": "completed", "phase": None, "generation_id": "old", "result": {"literature": {"summary": "Old supplied-only result"}},
                    "decision": None, "error": None, "usage": [], "events": []}
            encoded = json.dumps(item)
            db = sqlite3.connect(path)
            try:
                db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY,create_key TEXT UNIQUE,request_hash TEXT,updated_at TEXT,data TEXT)")
                db.execute("CREATE TABLE versions(session_id TEXT,revision INTEGER,data TEXT,PRIMARY KEY(session_id,revision))")
                db.execute("CREATE TABLE generations(id TEXT PRIMARY KEY,session_id TEXT,request_key TEXT,request_hash TEXT,status TEXT,brief TEXT,UNIQUE(session_id,request_key))")
                db.execute("INSERT INTO sessions VALUES(?,?,?,?,?)", (item["id"], "old", "hash", "before", encoded))
                db.execute("INSERT INTO versions VALUES(?,?,?)", (item["id"], 5, encoded))
                db.execute("PRAGMA user_version=1")
                db.commit()
            finally:
                db.close()
            migrated = IdeaStore(root).get(item["id"])
            self.assertEqual(migrated["result"], item["result"])
            self.assertEqual(migrated["revision"], 5)
            self.assertEqual(migrated["research"]["readiness"], "legacy_unsearched")
            db = sqlite3.connect(path)
            try:
                self.assertEqual(db.execute("SELECT data FROM sessions").fetchone()[0], encoded)
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
