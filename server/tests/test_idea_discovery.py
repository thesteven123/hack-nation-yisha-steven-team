"""Native-search receipts and discovery boundaries, without network/model calls."""
import asyncio
import copy
import json
import unittest
from unittest.mock import patch

import idea_discovery as discovery
from idea_generation import IdeaGenerationError


BRIEF = {"goal": "Compare attention mechanisms", "hypothesis": "", "constraints": "", "sources": [{"text": "do not disclose this source body to search"}]}
CONTEXT = {"round": 1, "goal_revision": 1, "feedback": [], "requested_queries": [],
           "prior_searches": [], "prior_gaps": [], "prior_review": None, "limits": {"max_queries": 3, "max_papers": 2}}


def packet(call_id="native-search-1", *, results=None, queries=None):
    return {"agent": "literature", "task": "discovery", "thread_id": "actual-thread", "turn_id": "actual-turn",
            "method": "item/completed", "item": {"type": "webSearch", "id": call_id,
                "query": "attention mechanisms original paper",
                "action": {"type": "search", "queries": queries or ["attention mechanisms original paper"]},
                "results": results if results is not None else [
                    {"type": "text_result", "title": "Attention paper", "url": "https://arxiv.org/abs/1706.03762",
                     "ref_id": "turn0academia0", "snippet": "A real native search snippet"}]}}


def result(selected=None, *, valid=True, stopped=None):
    return {"output": {"selected": selected or [], "summary": "Native search summary", "gaps": []} if valid else {},
            "usage": {"available": True, "total_tokens": 900, "complete": stopped is None},
            "provider": {"backend": "codex", "thread_id": "actual-thread", "turn_id": "actual-turn",
                         "structured_output_valid": valid, "stop_reason": stopped}}


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidates_only_from_native_results_not_generated_urls(self):
        events = []
        receipts = discovery.SearchReceipts(CONTEXT, events.append, BRIEF)
        await receipts.observe(packet())
        answer = receipts.finish(result([
            {"url": "https://invented.invalid/paper", "reason": "invented"},
            {"url": "https://arxiv.org/abs/1706.03762", "reason": "Actual nearest work"},
        ]))
        self.assertEqual(len(answer["papers"]), 1)
        self.assertEqual(answer["papers"][0]["url"], "https://arxiv.org/abs/1706.03762")
        self.assertEqual(answer["papers"][0]["reason"], "Actual nearest work")
        self.assertEqual(answer["papers"][0]["ref_id"], "turn0academia0")
        self.assertEqual(answer["searches"][0]["query_count"], 1)
        self.assertEqual([event["type"] for event in events], ["web_search_completed", "source_discovered"])
        self.assertTrue(all(event["thread_id"] == "actual-thread" for event in events))

    async def test_invalid_summary_preserves_received_sources_without_fabrication(self):
        receipts = discovery.SearchReceipts(CONTEXT)
        await receipts.observe(packet())
        answer = receipts.finish(result(valid=False))
        self.assertEqual(len(answer["papers"]), 1)
        self.assertIn("invalid", answer["gaps"][-1])
        self.assertIn("1 native web operations", answer["summary"])

    async def test_batch_budget_overrun_records_actual_count_and_keeps_sources(self):
        context = {**CONTEXT, "limits": {"max_queries": 1, "max_papers": 1}}
        receipts = discovery.SearchReceipts(context)
        keep_going = await receipts.observe(packet(queries=["nearest work", "counter evidence"]))
        self.assertFalse(keep_going)
        answer = receipts.finish(result(valid=False, stopped="search_budget"))
        self.assertEqual(answer["searches"][0]["query_count"], 2)
        self.assertTrue(answer["searches"][0]["budget_overrun"])
        self.assertEqual(len(answer["papers"]), 1)
        self.assertEqual(answer["usage"]["total_tokens"], 900)
        self.assertFalse(answer["usage"]["complete"])

    async def test_repeated_native_receipt_is_idempotent_and_changed_receipt_rejected(self):
        receipts = discovery.SearchReceipts(CONTEXT)
        native = packet()
        await receipts.observe(native)
        await receipts.observe(native)
        self.assertEqual(len(receipts.searches), 1)
        changed = copy.deepcopy(native)
        changed["item"]["query"] = "changed"
        with self.assertRaisesRegex(IdeaGenerationError, "different receipt"):
            await receipts.observe(changed)

    async def test_zero_web_events_fails_instead_of_supplied_only_fallback(self):
        receipts = discovery.SearchReceipts(CONTEXT)
        with self.assertRaises(IdeaGenerationError) as caught:
            receipts.finish(result([{"url": "https://arxiv.org/abs/1706.03762", "reason": "from memory"}]))
        self.assertEqual(caught.exception.code, "search_unavailable")

    async def test_actual_empty_results_remain_a_recorded_search_with_explicit_gap(self):
        receipts = discovery.SearchReceipts(CONTEXT)
        await receipts.observe(packet(results=[]))
        answer = receipts.finish(result())
        self.assertEqual(answer["papers"], [])
        self.assertEqual(len(answer["searches"]), 1)
        self.assertIn("not evidence", answer["gaps"][-1])

    async def test_candidate_bounds_reject_non_http_and_deduplicate_same_arxiv_work(self):
        receipts = discovery.SearchReceipts(CONTEXT, brief=BRIEF)
        native = packet(results=[
            {"type": "text_result", "title": "abstract", "url": "https://arxiv.org/abs/1706.03762"},
            {"type": "text_result", "title": "Attention paper", "url": "https://arxiv.org/pdf/1706.03762"},
            {"type": "text_result", "title": "Counter evidence", "url": "https://openreview.net/forum?id=example"},
            {"type": "text_result", "title": "local", "url": "file:///etc/passwd"},
            {"type": "text_result", "title": "bad", "url": "https://user:secret@example.com/"},
        ])
        await receipts.observe(native)
        answer = receipts.finish(result(valid=False))
        self.assertEqual([candidate["title"] for candidate in answer["papers"]], ["Attention paper", "Counter evidence"])
        self.assertEqual(len(answer["searches"][0]["urls"]), 3)

    async def test_multilingual_fallback_uses_real_queries_and_prefers_papers_over_catalogs_and_social(self):
        context = {**CONTEXT, "requested_queries": ["diversity collapse large language models"]}
        receipts = discovery.SearchReceipts(context, brief={"goal": "比较 Anthropic AI 生成科研想法的多样性与价值"})
        noisy = "Anthropic diverse scientific hypothesis search agents diversity collapse large language models"
        await receipts.observe(packet(queries=["diverse scientific hypothesis search agents"], results=[
            {"type": "text_result", "title": "Anthropic Research", "url": "https://www.anthropic.com/research", "snippet": noisy},
            {"type": "text_result", "title": noisy, "url": "https://www.reddit.com/r/AI/comments/example"},
            {"type": "text_result", "title": noisy, "url": "https://news.ycombinator.com/item?id=1"},
            {"type": "text_result", "title": noisy, "url": "https://example.org/company"},
            {"type": "text_result", "title": "Unrelated fluid dynamics", "url": "https://arxiv.org/abs/0000.00001"},
            {"type": "text_result", "title": "Towards Diverse Scientific Hypothesis Search", "url": "https://arxiv.org/abs/0000.00002"},
            {"type": "text_result", "title": "Diversity Collapse in Large Language Models", "url": "https://aclanthology.org/2025.fixture-1.1/"},
        ]))
        answer = receipts.finish(result(valid=False))
        self.assertEqual({paper["url"] for paper in answer["papers"]},
                         {"https://arxiv.org/abs/0000.00002", "https://aclanthology.org/2025.fixture-1.1/"})
        self.assertTrue(all(paper["selection_method"] == "heuristic_fallback" for paper in answer["papers"]))
        self.assertTrue(any("heuristic" in gap for gap in answer["gaps"]))

    async def test_fallback_prefers_specific_official_research_article_over_its_directory(self):
        context = {**CONTEXT, "limits": {"max_queries": 3, "max_papers": 1}}
        receipts = discovery.SearchReceipts(context, brief={"goal": "比较 Anthropic 的科研助手"})
        await receipts.observe(packet(queries=["scientific assistants evaluation"], results=[
            {"type": "text_result", "title": "Anthropic scientific assistants evaluation", "url": "https://anthropic.com/"},
            {"type": "text_result", "title": "Anthropic Research", "url": "https://anthropic.com/research"},
            {"type": "text_result", "title": "Scientific assistants evaluation", "url": "https://anthropic.com/research/fixture-evaluation"},
        ]))
        answer = receipts.finish(result(valid=False))
        self.assertEqual(answer["papers"][0]["url"], "https://anthropic.com/research/fixture-evaluation")

    async def test_prompt_contains_adaptive_context_and_exact_human_feedback_without_source_bodies(self):
        context = {**CONTEXT, "requested_queries": ["counterevidence against hypothesis"],
                   "feedback": [{"text": "focus on low-cost tests", "mode": "refine"}]}
        prompt = discovery.discovery_prompt(BRIEF, context)
        parsed = json.loads(prompt.rsplit("\n\n", 1)[1])
        self.assertNotIn("sources", parsed["brief"])
        self.assertEqual(parsed["context"], context)
        self.assertEqual(parsed["budget"]["max_queries"], 3)
        self.assertIn("materially change", prompt)
        with self.assertRaises(IdeaGenerationError):
            discovery.discovery_prompt(BRIEF, {**context, "feedback": ["x" * discovery.MAX_PROMPT_BYTES]})
        with self.assertRaises(IdeaGenerationError):
            discovery.discovery_prompt(BRIEF, {**context, "limits": {"max_queries": 0}})

    async def test_discovery_wrapper_awaits_real_event_callback_and_passes_caps(self):
        events = []
        async def receive(event):
            await asyncio.sleep(0)
            events.append(event)
        async def generate(stage, prompt, schema, **kwargs):
            self.assertEqual(stage, "discovery")
            self.assertTrue(kwargs["web_search"])
            self.assertEqual(kwargs["max_searches"], 3)
            await kwargs["web_observer"](packet())
            return result()
        with patch.object(discovery, "_generate", side_effect=generate):
            answer = await discovery.discover_idea_sources(BRIEF, CONTEXT, receive, executable="synthetic", model=None, env={})
        self.assertEqual(len(answer["papers"]), 1)
        self.assertEqual(len(events), 2)

    async def test_cancellation_is_not_converted_to_failed_search_or_retried(self):
        with patch.object(discovery, "_generate", side_effect=asyncio.CancelledError()) as generate:
            with self.assertRaises(asyncio.CancelledError):
                await discovery.discover_idea_sources(BRIEF, CONTEXT, executable="synthetic", model=None, env={})
        generate.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
