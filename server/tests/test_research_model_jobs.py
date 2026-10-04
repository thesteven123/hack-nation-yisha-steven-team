import asyncio
import copy
from contextlib import contextmanager
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import research_model_jobs as jobs
import idea_generation as native_transport
from research_lab import ResearchLabStore
from tests.test_research_lab import numeric_request
from tests.test_idea_generation import protocol


def output_for(role, packet):
    if role == "planner":
        return {"status": "recommendation", "selected_action_id": packet["candidates"][0]["id"], "reason": "The method addresses supplied data within scope",
                "candidates": [{"action_id": c["id"], "applicability": "applicable", "goal_relevance": "Bounded descriptive analysis",
                                "expected_learning": "Understand fixed-input behavior", "limitations": ["No causal inference"],
                                "rejected_reason": "" if i == 0 else "Prefer the summary before sensitivity"} for i, c in enumerate(packet["candidates"])],
                "missing_inputs": [], "questions": [], "scope_note": "Recommendation only; no execution authorization"}
    if role == "analyst":
        observation = packet["observations"][0]
        return {"status": "interpreted", "summary": "The machine result describes these pairs only",
                "findings": [{"id": "finding1", "run_id": observation["run_id"], "field_paths": ["data.mean_difference"],
                              "interpretation": "A descriptive association, not a causal effect", "limitations": ["Sampling mechanism unverified"]}],
                "hypothesis_assessment": {"status": "bounded_evidence_only", "reason": "Only fixed supplied data were analyzed"}, "missing_inputs": []}
    claim = packet["claims"][0]
    return {"status": "reviewed", "summary": "Inference still requires evidence", "critiques": [{"claim_id": claim["id"], "run_id": claim["run_id"],
            "assessment": "needs_more_evidence", "source_support_assessment": "bounded_machine_evidence", "inference_assessment": "missing_premise",
            "concern": "Independent sampling and causal assumptions are missing", "suggested_check": "Inspect the data collection design"}],
            "next_action": "ask_human", "next_action_reason": "Clarify the sampling process", "unresolved": ["Generalization"], "questions": ["How were pairs collected?"]}


def envelope(output, *, selector="model-A", tokens=31):
    return {"output": output, "usage": {"available": True, "complete": True, "input_tokens": tokens - 1, "output_tokens": 1,
                                      "total_tokens": tokens, "monetary_cost": None},
            "provider": {"backend": "codex", "model": selector, "requested_model": "native-default", "resolved_model": selector,
                         "model_resolution": "thread_start", "execution_model_observed": False, "structured_output_valid": True,
                         "thread_id": "independent-thread", "turn_id": "independent-turn", "ephemeral": True}}


class ModelJobTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.lab = ResearchLabStore(self.root / "lab")
        self.snapshot = self.lab.create(numeric_request())
        self.calls = []
        async def generate(role, packet, on_event, **kwargs):
            self.calls.append((role, copy.deepcopy(packet), kwargs))
            await on_event({"type": "provider_task_started", "thread_id": f"thread-{len(self.calls)}", "turn_id": f"turn-{len(self.calls)}", "summary": "Started"})
            return envelope(output_for(role, packet))
        self.controller = jobs.ResearchModelJobs(self.root / "jobs", generate=generate)

    async def asyncTearDown(self):
        await self.controller.shutdown()
        self.temp.cleanup()

    def prepare(self, role="planner", key="job"):
        return self.controller.prepare({"campaign_id": self.snapshot["id"], "expected_revision": self.snapshot["revision"], "role": role,
                                        "idempotency_key": key}, resolve_snapshot=self.lab.get)

    def options(self):
        return {"executable": "fake-native", "env": {}, "current_revision": lambda campaign: self.lab.get(campaign)["revision"]}

    def execute_machine(self):
        self.snapshot = self.lab.run(self.snapshot["id"], {"expected_revision": self.snapshot["revision"], "idempotency_key": "machine"})

    async def test_service_bound_packet_readonly_get_and_exact_key_replay(self):
        item = self.prepare()
        packet = self.controller.artifact(item["id"], item["packet_ref"])["content"]
        self.assertEqual(packet["goal"]["objective"], self.snapshot["brief"]["goal"])
        self.assertEqual(packet["candidates"][0]["id"], self.snapshot["current_plan"]["candidates"][0]["id"])
        self.assertEqual(self.controller.get(item["id"]), item)
        self.assertEqual(self.calls, [])
        result = await self.controller.run(item["id"], **self.options())
        self.assertEqual(result["status"], "completed")
        self.snapshot = self.lab.decide(self.snapshot["id"], {"expected_revision": self.snapshot["revision"], "idempotency_key": "revision",
                                                            "kind": "revise", "goal": "New goal", "feedback": "Changed"})
        replay = self.controller.prepare({"campaign_id": item["campaign_id"], "expected_revision": item["campaign_revision"], "role": "planner", "idempotency_key": "job"},
                                         resolve_snapshot=lambda _: self.fail("Replay must precede revision lookup"))
        self.assertEqual(replay, result)
        await self.controller.run(item["id"], **self.options())
        self.assertEqual(len(self.calls), 1)

    async def test_renderer_packet_or_changed_idempotency_payload_rejected(self):
        request = {"campaign_id": self.snapshot["id"], "expected_revision": 1, "role": "planner", "idempotency_key": "job"}
        with self.assertRaises(jobs.ModelJobError):
            self.controller.prepare({**request, "packet": {"forged": True}}, resolve_snapshot=self.lab.get)
        self.prepare()
        with self.assertRaisesRegex(jobs.ModelJobError, "another request"):
            self.controller.prepare({**request, "role": "analyst"}, resolve_snapshot=self.lab.get)

    async def test_protocol_scope_hash_context_bound_and_unavailable_tokenizer_explicit(self):
        item = self.prepare()
        packet = self.controller.artifact(item["id"], item["packet_ref"])["content"]
        self.assertEqual({s["id"] for s in packet["protocol_sections"]}, {"planning-v0.5", "records-v0.5", "human-v0.5"})
        self.assertTrue(all(s["sha256"] == jobs._hash(s["excerpt"]) for s in packet["protocol_sections"]))
        self.assertIsNone(item["context"]["token_count"])
        packet["goal"]["objective"] = "a" * jobs.MAX_CONTEXT_BYTES
        with self.assertRaisesRegex(jobs.ModelJobError, "160 KiB"):
            jobs.prompt_for("planner", packet)
        packet = jobs.build_packet(self.snapshot, "planner")
        packet["protocol_sections"][0]["excerpt"] = "tampered"
        with self.assertRaisesRegex(jobs.ModelJobError, "hash"):
            jobs.prompt_for("planner", packet)

    async def test_roles_have_separate_contexts_and_machine_values_are_server_owned(self):
        planner = await self.controller.run(self.prepare()["id"], **self.options())
        self.execute_machine()
        analyst = await self.controller.run(self.prepare("analyst", "analysis")["id"], **self.options())
        reviewer = await self.controller.run(self.prepare("reviewer", "review")["id"], **self.options())
        self.assertEqual([c[0] for c in self.calls], ["planner", "analyst", "reviewer"])
        self.assertEqual(self.calls[0][1]["observations"], [])
        self.assertEqual(self.calls[1][1]["prior_interpretations"], [])
        self.assertEqual(self.calls[2][1]["prior_interpretations"][0]["job_id"], analyst["id"])
        self.assertEqual(analyst["output"]["machine_fact_refs"][0]["value"], 3)
        self.assertEqual(analyst["output"]["scientific_validation"], "not_established")
        self.assertFalse(reviewer["output"]["machine_records_modified"])
        self.assertEqual(planner["usage"]["total_tokens"], 31)
        self.assertIsNone(planner["usage"]["monetary_cost"])
        self.assertEqual(self.calls[1][2]["expected_model"], "model-A")
        self.assertEqual(self.lab.get(self.snapshot["id"]), self.snapshot)

    async def test_missing_observation_blocks_analyst_without_reserving_job(self):
        with self.assertRaisesRegex(jobs.ModelJobError, "actual observation"):
            self.prepare("analyst")
        self.assertEqual(self.controller.list(self.snapshot["id"])["quota"]["reserved"], 0)

    async def test_six_job_limit_replays_and_planned_cancellation_releases_reservation(self):
        prepared = [self.prepare(key=f"job-{i}") for i in range(6)]
        with self.assertRaisesRegex(jobs.ModelJobError, "quota"):
            self.prepare(key="seventh")
        cancelled = await self.controller.cancel(prepared[0]["id"])
        self.assertEqual(cancelled["attempt_count"], 0)
        self.assertEqual(self.controller.list(self.snapshot["id"])["quota"]["reserved"], 5)
        replacement = self.prepare(key="replacement")
        for item in prepared[1:] + [replacement]:
            await self.controller.run(item["id"], **self.options())
        quota = self.controller.list(self.snapshot["id"])["quota"]
        self.assertEqual((quota["used"], quota["reserved"]), (6, 0))
        with self.assertRaisesRegex(jobs.ModelJobError, "quota"):
            self.prepare(key="eighth")

    async def test_unknown_action_and_invented_machine_fields_are_rejected_and_retained(self):
        async def invalid(role, packet, on_event, **kwargs):
            result = output_for(role, packet)
            result["selected_action_id"] = "invented"
            return envelope(result, tokens=55)
        self.controller.generate = invalid
        item = await self.controller.run(self.prepare()["id"], **self.options())
        self.assertEqual(item["status"], "failed")
        self.assertIsNone(item["output"])
        self.assertEqual(item["usage"]["total_tokens"], 55)
        self.assertEqual(self.controller.artifact(item["id"], item["raw_output_ref"])["content"]["selected_action_id"], "invented")
        self.execute_machine()
        packet = jobs.build_packet(self.snapshot, "analyst")
        output = output_for("analyst", packet)
        output["findings"][0]["field_paths"] = ["data.invented_p_value"]
        with self.assertRaisesRegex(jobs.ModelJobError, "missing machine field"):
            jobs.validate_output("analyst", output, packet)
        output = output_for("analyst", packet)
        output["findings"][0]["statistic"] = 999
        with self.assertRaisesRegex(jobs.ModelJobError, "schema"):
            jobs.validate_output("analyst", output, packet)

    async def test_out_of_scope_planner_can_say_needs_method_without_running(self):
        async def unsupported(role, packet, on_event, **kwargs):
            value = output_for(role, packet)
            value.update(status="needs_method", selected_action_id=None, reason="A controlled physical intervention is outside these adapters")
            for candidate in value["candidates"]:
                candidate["applicability"] = "not_applicable"
            return envelope(value)
        self.controller.generate = unsupported
        result = await self.controller.run(self.prepare()["id"], **self.options())
        self.assertEqual(result["output"]["status"], "needs_method")
        self.assertEqual(self.lab.get(self.snapshot["id"])["budget"]["used_actions"], 0)

    async def test_qc_invalid_data_cannot_be_promoted_to_model_statistical_finding(self):
        request = numeric_request("small")
        request["inputs"]["baseline"] = [1, 2]
        request["inputs"]["treatment"] = [4, 5]
        self.snapshot = self.lab.create(request)
        self.execute_machine()
        packet = jobs.build_packet(self.snapshot, "analyst")
        with self.assertRaisesRegex(jobs.ModelJobError, "QC-invalid"):
            jobs.validate_output("analyst", output_for("analyst", packet), packet)
        value = output_for("analyst", packet)
        value["findings"][0]["field_paths"] = ["qc.passed"]
        validated = jobs.validate_output("analyst", value, packet)
        self.assertFalse(validated["machine_fact_refs"][0]["value"])

    async def test_stale_before_dispatch_releases_reservation_without_call(self):
        item = self.prepare()
        options = self.options()
        options["current_revision"] = lambda _: 999
        result = await self.controller.run(item["id"], **options)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(self.calls, [])
        quota = self.controller.list(self.snapshot["id"])["quota"]
        self.assertEqual((quota["used"], quota["reserved"]), (0, 0))

    async def test_stale_after_generation_retains_receipt_without_publishing(self):
        item = self.prepare()
        revisions = iter([1, 2])
        options = self.options()
        options["current_revision"] = lambda _: next(revisions)
        result = await self.controller.run(item["id"], **options)
        self.assertEqual(result["status"], "stale")
        self.assertIsNone(result["output"])
        self.assertIsNotNone(result["raw_output_ref"])
        self.assertEqual(result["usage"]["total_tokens"], 31)

    async def test_revision_read_failure_after_generation_retains_actual_output_and_usage(self):
        item = self.prepare()
        calls = 0
        def revision(_):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise RuntimeError("Revision store unavailable")
            return 1
        options = self.options()
        options["current_revision"] = revision
        result = await self.controller.run(item["id"], **options)
        self.assertEqual(result["error"]["code"], "revision_check_failed")
        self.assertIsNone(result["output"])
        self.assertEqual(result["usage"]["total_tokens"], 31)
        self.assertEqual(result["provider"]["resolved_model"], "model-A")
        self.assertEqual(self.controller.artifact(item["id"], result["raw_output_ref"])["content"]["status"], "recommendation")

    async def test_dependency_guard_covers_local_commits_only_and_replay_bypasses_it(self):
        held, blocked = False, False
        visits = []
        @contextmanager
        def guard(campaign):
            nonlocal held
            if blocked:
                raise jobs.ModelJobError("dependency_stale", "Pending correction")
            self.assertFalse(held)
            held = True
            visits.append(campaign)
            try:
                yield
            finally:
                held = False
        self.controller.admission_guard = guard
        original = self.controller.generate
        async def generate(*args, **kwargs):
            self.assertFalse(held, "No producer lock may span the native call")
            return await original(*args, **kwargs)
        self.controller.generate = generate
        item = self.prepare()
        result = await self.controller.run(item["id"], **self.options())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(visits, [self.snapshot["id"]] * 3)
        blocked = True
        self.assertEqual(self.prepare(), result)
        self.assertEqual(await self.controller.start(item["id"], **self.options()), result)
        with self.assertRaisesRegex(jobs.ModelJobError, "Pending correction"):
            self.prepare(key="new-blocked")
        self.assertEqual(self.controller.list(self.snapshot["id"])["quota"]["used"], 1)

    async def test_publication_guard_closes_dependency_check_to_commit_race(self):
        blocked = False
        @contextmanager
        def guard(_):
            if blocked:
                raise jobs.ModelJobError("dependency_stale", "Correction committed after the read check")
            yield
        def checked_then_corrected(_):
            nonlocal blocked
            blocked = True
        self.controller.admission_guard = guard
        self.controller.dependency_check = checked_then_corrected
        result = await self.controller.run(self.prepare()["id"], **self.options())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "dependency_stale")
        self.assertIsNone(result["output"])
        self.assertIsNotNone(result["raw_output_ref"])
        self.assertEqual(result["usage"]["total_tokens"], 31)

    async def test_cancelled_history_pagination_retains_all_receipts(self):
        ids = []
        for index in range(53):
            item = self.prepare(key=f"cancelled-{index}")
            ids.append(item["id"])
            await self.controller.cancel(item["id"])
        first = self.controller.list(self.snapshot["id"])
        second = self.controller.list(self.snapshot["id"], before=first["next_before"])
        self.assertTrue(first["has_more"])
        self.assertFalse(second["has_more"])
        self.assertEqual({x["id"] for x in first["items"] + second["items"]}, set(ids))
        self.assertEqual(first["quota"]["used"], 0)

    async def test_cancellation_after_provider_response_retains_actual_receipt(self):
        item = self.prepare()
        checking, release = threading.Event(), threading.Event()
        calls = 0
        def revision(_):
            nonlocal calls
            calls += 1
            if calls > 1:
                checking.set()
                release.wait(5)
            return 1
        options = self.options()
        options["current_revision"] = revision
        await self.controller.start(item["id"], **options)
        try:
            self.assertTrue(await asyncio.to_thread(checking.wait, 5))
            cancelling = asyncio.create_task(self.controller.cancel(item["id"]))
            await asyncio.sleep(0.01)
            self.assertFalse(cancelling.done())
        finally:
            release.set()
        result = await cancelling
        self.assertEqual(result["status"], "cancelled")
        self.assertIsNone(result["output"])
        self.assertEqual(result["usage"]["total_tokens"], 31)
        self.assertIsNotNone(result["raw_output_ref"])

    async def test_unresolved_or_changed_model_not_published(self):
        async def unresolved(role, packet, on_event, **kwargs):
            result = envelope(output_for(role, packet))
            result["provider"].update(resolved_model=None, model_resolution="unresolved_alias")
            return result
        self.controller.generate = unresolved
        result = await self.controller.run(self.prepare()["id"], **self.options())
        self.assertEqual(result["error"]["code"], "model_identity_unresolved")
        self.assertEqual(result["usage"]["total_tokens"], 31)

    async def test_resolved_selector_is_pinned_even_when_later_generation_fails(self):
        async def fails_after_resolution(role, packet, on_event, **kwargs):
            await on_event({"type": "provider_model_resolved", "provider": envelope({})["provider"]})
            raise RuntimeError("failure after thread setup")
        self.controller.generate = fails_after_resolution
        result = await self.controller.run(self.prepare()["id"], **self.options())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["provider"]["resolved_model"], "model-A")
        self.assertIsNone(result["usage"]["total_tokens"])
        self.assertEqual(self.controller.list(self.snapshot["id"])["quota"]["resolved_model"], "model-A")
        next_job = self.prepare(key="next")
        with self.assertRaisesRegex(jobs.ModelJobError, "pinned"):
            await self.controller.start(next_job["id"], **self.options(), model="model-B")

    async def test_restart_interrupts_unknown_attempt_without_auto_retry(self):
        item = self.prepare()
        self.controller._admit(item["id"], item["campaign_revision"], None)
        reopened = jobs.ResearchModelJobs(self.root / "jobs", generate=AsyncMock())
        result = await reopened.run(item["id"], **self.options())
        self.assertEqual(result["status"], "interrupted")
        self.assertIsNone(result["usage"]["total_tokens"])
        reopened.generate.assert_not_called()
        self.assertEqual(reopened.list(item["campaign_id"])["quota"]["used"], 1)
        await reopened.shutdown()

    async def test_double_cancel_waits_for_owned_provider_cleanup(self):
        started, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def slow(role, packet, on_event, **kwargs):
            try:
                started.set()
                await asyncio.Future()
            finally:
                await release.wait()
                cleaned.set()
        self.controller.generate = slow
        item = self.prepare()
        await self.controller.start(item["id"], **self.options())
        await started.wait()
        first = asyncio.create_task(self.controller.cancel(item["id"]))
        await asyncio.sleep(0)
        second = asyncio.create_task(self.controller.cancel(item["id"]))
        await asyncio.sleep(0.01)
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        release.set()
        a, b = await asyncio.gather(first, second)
        self.assertTrue(cleaned.is_set())
        self.assertEqual((a["status"], b["status"]), ("cancelled", "cancelled"))
        self.assertFalse(self.controller.jobs)

    async def test_unknown_provider_failure_uses_content_free_error_and_unknown_usage(self):
        self.controller.generate = AsyncMock(side_effect=RuntimeError("SECRET detail"))
        result = await self.controller.run(self.prepare()["id"], **self.options())
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["usage"]["total_tokens"])
        self.assertNotIn("SECRET", json.dumps(result))

    async def test_artifact_cannot_be_read_through_other_job(self):
        a = self.prepare(key="a")
        self.execute_machine()
        b = self.prepare("analyst", "b")
        with self.assertRaisesRegex(jobs.ModelJobError, "not attached"):
            self.controller.artifact(b["id"], a["packet_ref"])

    async def test_native_generator_uses_role_schema_and_explicit_no_tools(self):
        packet = jobs.build_packet(self.snapshot, "planner")
        native = AsyncMock(return_value=envelope(output_for("planner", packet)))
        with patch.object(jobs, "_generate", native):
            result = await jobs.generate_research_role("planner", packet, executable="fake", env={}, expected_model="model-A")
        call = native.call_args
        self.assertEqual(call.args[0], "planner")
        self.assertEqual(call.args[2], jobs.ROLE_SCHEMAS["planner"])
        self.assertFalse(call.kwargs["web_search"])
        self.assertEqual(call.kwargs["expected_model"], "model-A")
        self.assertTrue(callable(call.kwargs["on_model_resolved"]))
        self.assertIn("Do not use tools", call.kwargs["instructions"])
        self.assertIsNone(result["context"]["token_count"])

    async def test_real_transport_hook_runs_before_turn_and_missing_selector_blocks_turn(self):
        async def request(method, params):
            if method == "account/read":
                return {"account": {"type": "chatgpt"}}
            return {"config": {"mcp_servers": {}}}
        client = SimpleNamespace(request=AsyncMock(side_effect=request), started_model="resolved-A", start_thread=AsyncMock(return_value="thread"),
            start_turn=AsyncMock(), read_thread=AsyncMock(return_value={"ephemeral": True, "path": None}), close=AsyncMock())
        async def command(argv, **options):
            if "--version" in argv:
                return "codex-cli 0.160.0"
            target = Path(argv[-1]); target.mkdir()
            (target / "codex_app_server_protocol.v2.schemas.json").write_text(json.dumps(protocol()))
            return ""
        seen = []
        async def resolved(value):
            seen.append(value)
            self.assertFalse(client.start_turn.called)
            raise jobs.ModelJobError("test_stop", "Stop before turn")
        with patch.object(native_transport, "IdeaCodexAppServerClient", return_value=client), patch.object(native_transport, "run_isolated_command", side_effect=command):
            with self.assertRaisesRegex(jobs.ModelJobError, "Stop before turn"):
                await native_transport._generate("planner", "packet", jobs.ROLE_SCHEMAS["planner"], executable="fake", model=None, env={},
                    instructions="isolated", on_model_resolved=resolved)
            self.assertEqual(seen[0]["resolved_model"], "resolved-A")
            client.started_model = None
            with self.assertRaisesRegex(native_transport.IdeaGenerationError, "unresolved"):
                await native_transport._generate("planner", "packet", jobs.ROLE_SCHEMAS["planner"], executable="fake", model=None, env={},
                    instructions="isolated", on_model_resolved=resolved)
        client.start_turn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
