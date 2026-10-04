"""Exercise real native parsing and role settlement with a synthetic RPC peer."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import research_model_jobs as jobs
from research_lab import ResearchLabStore
from tests import test_idea_generation as native_fixture
from tests.test_research_lab import numeric_request


class NativeReceiptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.native = native_fixture.NativeIdeaTests()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.lab = ResearchLabStore(self.root / "lab")
        self.campaign = self.lab.create(numeric_request())
        self.controller = jobs.ResearchModelJobs(self.root / "jobs")

    async def asyncTearDown(self):
        await self.controller.shutdown()

    def prepare(self):
        return self.controller.prepare({"campaign_id": self.campaign["id"], "expected_revision": 1,
            "role": "planner", "idempotency_key": "native-receipt"}, resolve_snapshot=self.lab.get)

    def options(self):
        return {"executable": "synthetic-never-executed", "env": {},
                "current_revision": lambda cid: self.lab.get(cid)["revision"]}

    @staticmethod
    def meter(total=140):
        return {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"total": {
            "inputTokens": 120, "outputTokens": total - 120, "totalTokens": total}}}}

    @staticmethod
    def answer(text):
        return {"method": "item/completed", "params": {"item": {
            "type": "agentMessage", "id": "answer", "phase": "final_answer", "text": text}}}

    def assert_failed_receipt(self, result, *, code, complete=False):
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], code)
        self.assertIsNone(result["output"])
        self.assertIsNone(result["output_ref"])
        self.assertEqual(result["usage"]["total_tokens"], 140)
        self.assertEqual(result["usage"]["complete"], complete)
        self.assertEqual(result["provider"]["turn_id"], "native-idea-turn")
        self.native.client.start_turn.assert_awaited_once()
        self.native.client.close.assert_awaited_once()

    async def test_error_after_metering_retains_known_usage_and_identity(self):
        self.native.turn.next_notification.side_effect = [self.meter(),
            {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"total": {}}}},
            {"method": "error", "params": {"message": "private provider diagnostic"}}]
        result = await self.controller.run(self.prepare()["id"], **self.options())
        self.assert_failed_receipt(result, code="provider_failed")
        self.assertTrue(result["usage"]["available"])
        self.assertIsNone(result["usage"]["monetary_cost"])
        self.assertNotIn("private provider diagnostic", json.dumps(result))

    async def test_schema_rejected_object_retained_without_default_event_leak(self):
        rejected = {"status": "recommendation", "reason": "Rejected private attempt"}
        self.native.events(output=rejected)
        result = await self.controller.run(self.prepare()["id"], **self.options())
        self.assert_failed_receipt(result, code="invalid_output", complete=True)
        self.assertEqual(self.controller.artifact(result["id"], result["raw_output_ref"])["content"], rejected)
        self.assertNotIn("Rejected private attempt", json.dumps(result))
        self.assertNotIn("raw_output", json.dumps(result["events"]))

    async def test_malformed_native_text_retained_exactly(self):
        text = 'Not JSON\n{"broken": true'
        self.native.turn.next_notification.side_effect = [self.meter(), self.answer(text),
            {"method": "turn/completed", "params": {"turn": {"status": "completed"}}}]
        result = await self.controller.run(self.prepare()["id"], **self.options())
        self.assert_failed_receipt(result, code="invalid_output", complete=True)
        raw = self.controller.artifact(result["id"], result["raw_output_ref"])["content"]
        self.assertEqual(raw, {"format": "unparsed_native_text", "text": text})

    async def test_error_after_response_preserves_raw_above_public_output_limit(self):
        rejected = {"unexpected": "x" * (80 * 1024)}
        self.native.turn.next_notification.side_effect = [self.meter(), self.answer(json.dumps(rejected)),
            {"method": "error", "params": {}}]
        result = await self.controller.run(self.prepare()["id"], **self.options())
        self.assert_failed_receipt(result, code="provider_failed")
        self.assertEqual(self.controller.artifact(result["id"], result["raw_output_ref"])["content"], rejected)

    async def test_timeout_retains_observed_partial_usage(self):
        called = 0
        async def next_notification():
            nonlocal called
            called += 1
            if called == 1:
                return self.meter()
            await asyncio.Event().wait()
        self.native.turn.next_notification.side_effect = next_notification
        with patch.object(jobs, "TIMEOUT_SECONDS", .5):
            result = await self.controller.run(self.prepare()["id"], **self.options())
        self.assert_failed_receipt(result, code="provider_timeout")
        self.native.turn.interrupt.assert_awaited_once()

    async def test_cancel_after_response_keeps_private_raw_and_partial_usage(self):
        waiting = asyncio.Event()
        rejected = {"pending": "private unfinished output"}
        notifications = iter([self.meter(), self.answer(json.dumps(rejected))])
        async def next_notification():
            value = next(notifications, None)
            if value is not None:
                return value
            waiting.set()
            await asyncio.Event().wait()
        self.native.turn.next_notification.side_effect = next_notification
        prepared = self.prepare()
        await self.controller.start(prepared["id"], **self.options())
        await asyncio.wait_for(waiting.wait(), 2)
        result = await self.controller.cancel(prepared["id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["usage"]["total_tokens"], 140)
        self.assertFalse(result["usage"]["complete"])
        self.assertIsNone(result["output"])
        self.assertEqual(self.controller.artifact(result["id"], result["raw_output_ref"])["content"], rejected)
        self.native.client.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
