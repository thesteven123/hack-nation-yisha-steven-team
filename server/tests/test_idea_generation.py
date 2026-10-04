"""Idea provider contract with fake native protocol, no account/network calls."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import idea_generation as ideas
import idea_discovery as discovery
from idea_lab import GENERATION_SCHEMAS

NATIVE_IDEA_CLIENT = ideas.IdeaCodexAppServerClient


BRIEF = {"goal": "比较两个研究方向", "hypothesis": "", "constraints": "Only supplied evidence",
         "sources": [{"id": "s1", "title": "Supplied source", "uri": "fixture:s1", "text": "Quoted fact."}]}
LITERATURE = {"summary": "Supplied evidence only", "evidence": [
    {"id": "e1", "source_id": "s1", "quote": "Quoted fact.", "finding": "Reported fact", "limitation": "Not independently replicated",
     "conditions": "Unreported", "assumptions": ["Unreported"], "interpretation": "Inference remains unverified"}],
    "gaps": ["No replication"]}
REPAIR = {"attempt_id": "prior-rejected-attempt", "number": 1,
          "diagnostics": {"kind": "evidence_reference_validation", "failure_codes": ["quote_not_exact"]},
          "provider": {"backend": "codex", "model": "original-native-model", "resolved_model": "original-native-model",
                       "model_resolution": "thread_start"}}


def protocol():
    return {"definitions": {
        "ThreadStartParams": {"properties": {"ephemeral": {"type": "boolean"},
            **{key: {} for key in ("runtimeWorkspaceRoots", "baseInstructions", "developerInstructions", "config", "sandbox", "approvalPolicy")}}},
        "TurnStartParams": {"properties": {"outputSchema": {}, "environments": {
            "type": ["array", "null"], "description": "Empty disables environment access for this turn."}}},
    }}


class ContextTests(unittest.TestCase):
    def packet(self, stage, previous):
        return json.loads(ideas.stage_prompt(stage, BRIEF, previous).rsplit("\n\n", 1)[1])

    def test_each_role_gets_only_its_bounded_packet(self):
        previous = {"literature": LITERATURE, "ideas": {"directions": []}, "review": {"secret_later_context": "omit"}}
        literature = self.packet("literature", previous)
        self.assertEqual(set(literature), {"brief", "sources"})
        self.assertNotIn("sources", literature["brief"])
        proposed = self.packet("ideas", previous)
        self.assertEqual(set(proposed), {"brief", "literature"})
        reviewed = self.packet("review", previous)
        self.assertEqual(set(reviewed), {"brief", "literature", "ideas"})
        self.assertNotIn("secret_later_context", json.dumps(reviewed))

    def test_goal_only_and_untrusted_data_instructions(self):
        prompt = ideas.stage_prompt("literature", {**BRIEF, "sources": []}, {})
        self.assertEqual(json.loads(prompt.rsplit("\n\n", 1)[1])["sources"], [])
        self.assertIn("not instructions or permission", prompt)
        self.assertIn("never substitute remembered or invented", prompt)
        self.assertIn("do not authorize experiment execution", prompt)

    def test_context_rejection_never_silently_truncates(self):
        with self.assertRaises(ideas.IdeaGenerationError) as caught:
            ideas.stage_prompt("literature", {**BRIEF, "goal": "x" * ideas.MAX_PROMPT_BYTES}, {})
        self.assertEqual(caught.exception.code, "packet_too_large")
        with self.assertRaises(ideas.IdeaGenerationError):
            ideas.stage_prompt("review", BRIEF, {"literature": LITERATURE})

    def test_repair_context_is_explicit_and_does_not_include_rejected_answer_as_evidence(self):
        packet = self.packet("literature", {"repair": {**REPAIR, "output": {"private_invalid_quote": "do not copy"}}})
        self.assertEqual(packet["sources"], BRIEF["sources"])
        self.assertEqual(packet["repair"]["attempt_id"], REPAIR["attempt_id"])
        self.assertEqual(packet["repair"]["diagnostics"], REPAIR["diagnostics"])
        self.assertNotIn("private_invalid_quote", json.dumps(packet))
        self.assertIn("strict exact quotations", packet["repair"]["instruction"])
        with self.assertRaises(ideas.IdeaGenerationError):
            ideas.stage_prompt("literature", BRIEF, {"repair": {**REPAIR, "number": 2}})
        with self.assertRaises(ideas.IdeaGenerationError):
            ideas.stage_prompt("ideas", BRIEF, {"literature": LITERATURE, "repair": REPAIR})


class NativeIdeaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.schema = protocol()
        self.calls = []

        async def request(method, params):
            if method == "account/read":
                self.assertEqual(params, {"refreshToken": False})
                return {"account": {"type": "chatgpt"}}
            if method == "modelProvider/capabilities/read":
                return {"webSearch": True, "namespaceTools": True}
            self.assertEqual(method, "config/read")
            return {"config": {"mcp_servers": {"inherited.server": {"url": "never-connect"}}}}

        self.client = SimpleNamespace(request=AsyncMock(side_effect=request),
            start_thread=AsyncMock(return_value="fresh-idea-thread"),
            started_model="resolved-native-model",
            read_thread=AsyncMock(return_value={"ephemeral": True, "path": None}), close=AsyncMock())
        self.turn = SimpleNamespace(turn_id="native-idea-turn", close=AsyncMock(), interrupt=AsyncMock(), next_notification=AsyncMock())
        self.client.start_turn = AsyncMock(return_value=self.turn)
        self.factory = self.enterContext(patch.object(ideas, "IdeaCodexAppServerClient", return_value=self.client))

        async def command(argv, **options):
            self.calls.append((argv, options))
            if "--version" in argv:
                return "codex-cli 0.160.0\n"
            target = Path(argv[-1])
            target.mkdir()
            (target / "codex_app_server_protocol.v2.schemas.json").write_text(json.dumps(self.schema))
            return ""

        self.enterContext(patch.object(ideas, "run_isolated_command", side_effect=command))
        self.events()

    def events(self, output=LITERATURE, usage=True):
        events = []
        if usage:
            events.append({"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"last": {
                "inputTokens": 100, "cachedInputTokens": 20, "outputTokens": 40, "totalTokens": 140,
                "reasoningOutputTokens": True}}}})
        events.extend([
            {"method": "item/completed", "params": {"item": {"type": "agentMessage", "id": "answer",
               "phase": "final_answer", "text": json.dumps(output)}}},
            {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
        ])
        self.turn.next_notification.side_effect = events

    async def generate(self):
        return await ideas.generate_idea_stage("literature", BRIEF, {}, executable="synthetic-codex", model=None,
            env={"HOME": "/native-auth-home", "CODEX_HOME": "/native-auth-home/.codex",
                 "AGENT_TOKEN": "never-inherit", "OPENAI_API_KEY": "never-use-raw-key"})

    async def test_structured_result_native_auth_isolation_and_usage(self):
        result = await self.generate()
        self.assertEqual(set(result), {"output", "usage", "provider"})
        self.assertEqual(result["output"], LITERATURE)
        self.assertEqual(result["usage"]["input_tokens"], 100)
        self.assertIsNone(result["usage"]["reasoning_output_tokens"])
        self.assertIsNone(result["usage"]["monetary_cost"])
        self.assertEqual(result["provider"]["cli"], "codex-cli 0.160.0")
        self.assertEqual(result["provider"]["model"], "resolved-native-model")
        self.assertEqual(result["provider"]["requested_model"], "native-default")
        self.assertEqual(result["provider"]["resolved_model"], "resolved-native-model")
        self.assertEqual(result["provider"]["model_resolution"], "thread_start")
        self.assertFalse(result["provider"]["execution_model_observed"])
        env = self.factory.call_args.kwargs["env_factory"]()
        self.assertEqual(env["HOME"], "/native-auth-home")
        self.assertEqual(env["CODEX_HOME"], "/native-auth-home/.codex")
        self.assertNotIn("AGENT_TOKEN", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        params = self.client.start_thread.call_args.args[0]
        self.assertTrue(params["ephemeral"])
        self.assertEqual(params["runtimeWorkspaceRoots"], [])
        self.assertEqual(params["sandbox"], "read-only")
        self.assertEqual(params["config"]["mcp_servers"], {"inherited.server": {"enabled": False}})
        for feature in ("features.shell_tool", "features.hooks", "features.apps", "features.multi_agent"):
            self.assertFalse(params["config"][feature])
        self.assertEqual(self.client.start_turn.call_args.kwargs["overrides"],
                         {"environments": [], "outputSchema": GENERATION_SCHEMAS["literature"]})
        self.client.close.assert_awaited_once()
        self.turn.close.assert_awaited_once()
        self.turn.interrupt.assert_not_awaited()
        self.assertFalse(Path(params["cwd"]).exists())

    async def test_missing_usage_stays_unknown(self):
        self.events(usage=False)
        result = await self.generate()
        self.assertFalse(result["usage"]["available"])
        self.assertIsNone(result["usage"]["total_tokens"])

    async def test_idea_client_retains_original_thread_start_resolution(self):
        client = object.__new__(NATIVE_IDEA_CLIENT)
        response = {"thread": {"id": "native-id"}, "model": "native-resolved-code"}
        with patch.object(ideas.CodexAppServerClient, "request", AsyncMock(return_value=response)) as request:
            actual = await client.request("thread/start", {"model": "requested-alias"}, timeout=5)
        self.assertIs(actual, response)
        self.assertEqual(client.started_model, "native-resolved-code")
        request.assert_awaited_once_with("thread/start", {"model": "requested-alias"}, timeout=5)

    async def test_explicit_repair_preserves_original_native_model_selection(self):
        self.client.started_model = "original-native-model"
        result = await ideas.generate_idea_stage("literature", BRIEF, {"repair": REPAIR},
                                                 executable="synthetic", model="changed-selection", env={})
        self.assertEqual(self.client.start_thread.call_args.args[0]["model"], "original-native-model")
        self.assertEqual(result["provider"]["model"], "original-native-model")
        self.assertEqual(result["provider"]["repair_of"], REPAIR["attempt_id"])
        self.client.start_thread.assert_awaited_once()

    async def test_default_alias_repair_uses_saved_resolution_and_rejects_changed_resolution(self):
        repair = {**REPAIR, "provider": {**REPAIR["provider"], "requested_model": "native-default"}}
        self.client.started_model = "unexpected-new-resolution"
        with self.assertRaises(ideas.IdeaGenerationError) as caught:
            await ideas.generate_idea_stage("literature", BRIEF, {"repair": repair},
                                             executable="synthetic", model=None, env={})
        self.assertEqual(caught.exception.code, "repair_model_changed")
        self.assertEqual(self.client.start_thread.call_args.args[0]["model"], "original-native-model")
        self.client.start_turn.assert_not_awaited()
        self.client.close.assert_awaited_once()

    async def test_unresolved_default_is_explicit_and_never_automatically_repaired(self):
        self.client.started_model = None
        result = await self.generate()
        self.assertEqual(result["provider"]["model_resolution"], "unresolved_alias")
        self.assertIsNone(result["provider"]["resolved_model"])
        self.factory.reset_mock()
        with self.assertRaises(ideas.IdeaGenerationError) as caught:
            await ideas.generate_idea_stage("literature", BRIEF, {"repair": {**REPAIR, "provider": result["provider"]}},
                                             executable="synthetic", model="new-default", env={})
        self.assertEqual(caught.exception.code, "invalid_repair")
        self.factory.assert_not_called()

    async def test_repair_without_original_provider_selection_never_dispatches(self):
        with self.assertRaises(ideas.IdeaGenerationError) as caught:
            await ideas.generate_idea_stage("literature", BRIEF, {"repair": {**REPAIR, "provider": {}}},
                                             executable="synthetic", model=None, env={})
        self.assertEqual(caught.exception.code, "invalid_repair")
        self.factory.assert_not_called()

    async def test_actual_identity_callbacks_and_cumulative_usage(self):
        self.events()
        events = list(self.turn.next_notification.side_effect)
        events[0]["params"]["tokenUsage"]["total"] = {"inputTokens": 300, "outputTokens": 80, "totalTokens": 380}
        self.turn.next_notification.side_effect = events
        callback = AsyncMock()
        result = await ideas.generate_idea_stage("literature", BRIEF, {}, callback,
                                                 executable="synthetic", model=None, env={})
        self.assertEqual(result["usage"]["total_tokens"], 380)
        self.assertEqual(result["provider"]["turn_id"], "native-idea-turn")
        sent = [call.args[0] for call in callback.await_args_list]
        self.assertEqual([event["type"] for event in sent], ["provider_task_started", "provider_usage_updated",
                                                            "provider_output_received", "provider_task_completed"])
        self.assertFalse(sent[1]["usage"]["complete"])
        self.assertEqual(sent[2]["raw_output"], LITERATURE)
        self.assertTrue(all(event["thread_id"] == "fresh-idea-thread" for event in sent))
        self.assertTrue(all(event["agent"] == "literature" for event in sent))

    async def test_discovery_host_keeps_environment_and_other_tools_disabled_on_budget_stop(self):
        self.turn.next_notification.side_effect = [
            {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"total": {"totalTokens": 200}}}},
            {"method": "item/completed", "params": {"item": {"type": "webSearch", "id": "native-web-1", "results": []}}},
        ]
        observer = AsyncMock(return_value=False)
        events = []
        result = await ideas._generate("discovery", "public research", {"type": "object"},
            executable="synthetic", model=None, env={}, instructions="web only", web_search=True,
            web_observer=observer, lenient_output=True, on_event=events.append)
        config = self.client.start_thread.call_args.args[0]["config"]
        self.assertTrue(config["features.code_mode_host"])
        self.assertTrue(config["features.standalone_web_search"])
        for feature in ("shell_tool", "apps", "plugins", "hooks", "multi_agent", "computer_use", "browser_use"):
            self.assertFalse(config["features." + feature])
        self.assertEqual(config["web_search"], "live")
        self.assertEqual(self.client.start_turn.call_args.kwargs["overrides"]["environments"], [])
        self.assertEqual(result["provider"]["stop_reason"], "search_budget")
        self.assertEqual(result["usage"]["total_tokens"], 200)
        self.assertFalse(result["usage"]["complete"])
        self.turn.interrupt.assert_awaited_once()
        self.client.close.assert_awaited_once()
        self.assertEqual(events[-1]["type"], "provider_task_stopped")

    def search_at_budget(self):
        return {"method": "item/completed", "params": {"item": {
            "type": "webSearch", "id": "native-web-1", "query": "three related queries",
            "action": {"type": "search", "queries": ["nearest work", "competing method", "counterevidence"]},
            "results": [
                {"type": "text_result", "title": "General research index", "url": "https://example.org/research"},
                {"type": "text_result", "title": "Specific original paper", "url": "https://arxiv.org/abs/1706.03762"},
            ]}}}

    async def discover(self, callback=None):
        return await discovery.discover_idea_sources(
            {"goal": "Compare attention mechanisms"}, {"limits": {"max_queries": 3, "max_papers": 1}}, callback,
            executable="synthetic", model=None, env={})

    async def test_exact_search_budget_allows_native_ranking_final_answer_and_usage(self):
        selected = {"selected": [{"url": "https://arxiv.org/abs/1706.03762", "reason": "Closest original method"}],
                    "summary": "Ranked the original work above the general index.", "gaps": []}
        self.turn.next_notification.side_effect = [
            self.search_at_budget(),
            {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"total": {"totalTokens": 987}}}},
            {"method": "item/completed", "params": {"item": {"type": "agentMessage", "id": "answer",
                "phase": "final_answer", "text": json.dumps(selected)}}},
            {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
        ]
        answer = await self.discover()
        self.assertEqual(answer["papers"][0]["reason"], "Closest original method")
        self.assertEqual(answer["summary"], selected["summary"])
        self.assertEqual(answer["searches"][0]["query_count"], 3)
        self.assertEqual(answer["usage"]["total_tokens"], 987)
        self.assertTrue(answer["usage"]["complete"])
        self.assertIsNone(answer["provider"]["stop_reason"])
        self.turn.interrupt.assert_not_awaited()

    async def test_new_web_operation_after_exact_budget_stops_with_unknown_attempt_receipt(self):
        self.turn.next_notification.side_effect = [
            self.search_at_budget(),
            {"method": "item/started", "params": {"item": {"type": "webSearch", "id": "extra-operation"}}},
            AssertionError("Must stop before awaiting any further provider event"),
        ]
        events = []
        answer = await self.discover(events.append)
        attempt = answer["searches"][-1]
        self.assertEqual(attempt["id"], "extra-operation")
        self.assertEqual(attempt["status"], "interrupted_before_query_receipt")
        self.assertFalse(attempt["query_count_known"])
        self.assertEqual(attempt["queries"], [])
        self.assertEqual(sum(receipt["query_count"] for receipt in answer["searches"]), 3)
        self.assertEqual(answer["provider"]["stop_reason"], "search_budget")
        self.assertFalse(answer["usage"]["complete"])
        self.assertIn("extra query count is unknown", answer["gaps"][-1])
        self.assertTrue(any(event["type"] == "web_search_stopped" for event in events))
        self.turn.interrupt.assert_awaited_once()
        self.assertEqual(self.turn.next_notification.await_count, 2)

    async def test_absent_native_login_does_not_generate(self):
        self.client.request.side_effect = None
        self.client.request.return_value = {"account": {"type": "apiKey"}}
        with self.assertRaises(ideas.IdeaGenerationError) as caught:
            await self.generate()
        self.assertEqual(caught.exception.code, "native_login_required")
        self.client.start_thread.assert_not_awaited()
        self.client.close.assert_awaited_once()

    async def test_missing_structured_protocol_stops_before_provider_start(self):
        del self.schema["definitions"]["TurnStartParams"]["properties"]["outputSchema"]
        with self.assertRaises(ideas.IdeaGenerationError) as caught:
            await self.generate()
        self.assertEqual(caught.exception.code, "unsupported_protocol")
        self.factory.assert_not_called()

    async def test_unconfirmed_ephemeral_stops_before_turn(self):
        self.client.read_thread.return_value = {"ephemeral": False, "path": "/private/history"}
        with self.assertRaises(ideas.IdeaGenerationError):
            await self.generate()
        self.client.start_turn.assert_not_awaited()
        self.client.close.assert_awaited_once()

    async def test_invalid_output_fails_without_exposing_text(self):
        self.events(output={"private-injected-text": "not-a-literature-result"})
        with self.assertRaises(ideas.IdeaGenerationError) as caught:
            await self.generate()
        self.assertEqual(caught.exception.code, "invalid_output")
        self.assertNotIn("private-injected-text", str(caught.exception))
        self.client.close.assert_awaited_once()

    async def test_tool_event_interrupts_owned_turn(self):
        self.turn.next_notification.side_effect = [{"method": "item/started", "params": {"item": {"type": "commandExecution"}}}]
        with self.assertRaises(ideas.IdeaGenerationError) as caught:
            await self.generate()
        self.assertEqual(caught.exception.code, "unexpected_tool")
        self.turn.interrupt.assert_awaited_once()
        self.client.close.assert_awaited_once()

    async def test_cancel_interrupts_and_closes_owned_process(self):
        self.turn.next_notification.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.generate()
        self.turn.interrupt.assert_awaited_once()
        self.client.close.assert_awaited_once()

    async def test_timeout_closes_and_does_not_retry(self):
        async def wait():
            await asyncio.sleep(10)
        self.turn.next_notification.side_effect = wait
        with patch.object(ideas, "TIMEOUT_SECONDS", 0.05):
            with self.assertRaises(ideas.IdeaGenerationError) as caught:
                await self.generate()
        self.assertEqual(caught.exception.code, "provider_timeout")
        self.client.start_turn.assert_awaited_once()
        self.turn.interrupt.assert_awaited_once()
        self.client.close.assert_awaited_once()

    async def test_turn_close_error_still_reaps_client(self):
        self.turn.close.side_effect = RuntimeError("synthetic cleanup error")
        with self.assertRaises(RuntimeError):
            await self.generate()
        self.client.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
