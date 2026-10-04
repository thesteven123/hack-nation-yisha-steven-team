import hashlib
import json
import multiprocessing
import os
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from research_actions import (
    MAX_QUOTE_BYTES, MAX_SOURCE_BYTES, MAX_SPANS, REQUIRED_PROTOCOLS,
    ResearchActionStore, ResearchError,
)


def _run_in_process(arguments):
    root, action_id = arguments
    return ResearchActionStore(Path(root)).run(action_id)


def _crash_during_run(root, action_id):
    original_event = ResearchActionStore._event

    def crash_before_commit(db, current_action_id, kind, data):
        if kind == "completed":
            os._exit(73)
        return original_event(db, current_action_id, kind, data)

    with patch.object(ResearchActionStore, "_event", side_effect=crash_before_commit):
        ResearchActionStore(Path(root)).run(action_id)


class ResearchActionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "research"
        self.store = ResearchActionStore(self.root)

    def request(self, key="request-1", **source_updates):
        source = {"uri": "fixture:paper-v1", "text": "Introduction\nA measured result.\nLimitations\n",
                  "coverage": "excerpt", "missing_sections": ["Methods", "Supplement"]}
        source.update(source_updates)
        return {"idempotency_key": key,
                "goal": {"id": "goal-1", "revision": 1, "question": "Where does this quote occur?",
                         "completion_criterion": "Report exact spans and coverage gaps."},
                "source": source, "quote": "A measured result."}

    def assert_error(self, code, operation):
        with self.assertRaises(ResearchError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def counts(self):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            return {name: db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                    for name in ("actions", "events", "artifacts", "parse_cache")}

    def test_plan_freezes_goal_method_criteria_source_and_budget_before_run(self):
        request = self.request()
        plan = self.store.plan(request)
        self.assertEqual(plan["status"], "planned")
        self.assertIsNone(plan["result"])
        self.assertEqual([event["type"] for event in plan["events"]], ["planned"])
        self.assertEqual(plan["spec"]["goal"], request["goal"])
        self.assertEqual(plan["spec"]["required_protocols"], list(REQUIRED_PROTOCOLS))
        self.assertEqual(plan["spec"]["budget"]["external_requests"], 0)
        self.assertFalse(plan["spec"]["method"]["scientific_claim_validation"])
        self.assertEqual(hashlib.sha256(self.store.artifact(plan["spec_hash"])).hexdigest(), plan["spec_hash"])
        self.assertEqual(self.store.artifact(plan["spec"]["source"]["digest"]), request["source"]["text"].encode())
        request["goal"]["question"] = "Changed by caller"
        request["source"]["missing_sections"].append("Discussion")
        self.assertEqual(self.store.get(plan["action_id"]), plan)
        completed = self.store.run(plan["action_id"])
        self.assertEqual(completed["spec"], plan["spec"])
        self.assertEqual(completed["spec_hash"], plan["spec_hash"])

    def test_idempotency_reuses_request_and_rejects_any_payload_change(self):
        plan = self.store.plan(self.request())
        self.assertEqual(self.store.plan(self.request()), plan)
        changes = [self.request(text="Other source"), self.request(uri="fixture:other")]
        revised = self.request()
        revised["goal"]["revision"] = 2
        changes.append(revised)
        changed_quote = self.request()
        changed_quote["quote"] = "Limitations"
        changes.append(changed_quote)
        for request in changes:
            self.assert_error("idempotency_conflict", lambda: self.store.plan(request))
        self.assertEqual(self.counts()["actions"], 1)

    def test_input_bounds_and_no_external_action_or_caller_budget(self):
        invalid = []
        for field in ("action", "command", "budget", "authorization"):
            request = self.request()
            request[field] = "external"
            invalid.append(request)
        for revision in (0, -1, True, "1", 2147483648):
            request = self.request()
            request["goal"]["revision"] = revision
            invalid.append(request)
        for quote in ("", " " * 2, "x" * (MAX_QUOTE_BYTES + 1), "\ud800"):
            request = self.request()
            request["quote"] = quote
            invalid.append(request)
        invalid.extend([
            self.request(text="x" * (MAX_SOURCE_BYTES + 1)),
            self.request(text="界" * (MAX_SOURCE_BYTES // 3 + 1)),
            self.request(coverage="abstract"), self.request(missing_sections="unknown"),
            self.request(missing_sections=["s"] * 101),
            self.request(coverage="full_text"), self.request(uri=""),
        ])
        for request in invalid:
            with self.subTest(request_fields=list(request)):
                self.assert_error("invalid_request", lambda: self.store.plan(request))
        self.assertEqual(self.counts()["actions"], 0)
        bounded = self.request(text="x" * MAX_SOURCE_BYTES)
        bounded["quote"] = "x" * MAX_QUOTE_BYTES
        self.assertEqual(self.store.plan(bounded)["status"], "planned")

    def test_unicode_spans_and_artifacts_are_exact(self):
        request = self.request(text="α🙂\n中文\nend", missing_sections=["Unknown remainder"])
        request["quote"] = "🙂\n中文"
        result = self.store.run(self.store.plan(request)["action_id"])["result"]
        self.assertEqual(result["source_support"], "supported")
        self.assertEqual(result["inference_validity"], "cannot_determine")
        self.assertEqual(result["observation"]["spans"], [{
            "char_start": 1, "char_end": 5, "byte_start": 2, "byte_end": 13,
            "line_start": 1, "line_end": 2,
        }])
        self.assertEqual(result["observation"]["missing_sections"], ["Unknown remainder"])
        self.assertFalse(result["observation"]["is_fresh_replicate"])
        self.assertEqual(result["cost"]["external_requests"], 0)
        self.assertEqual(result["cost"]["model_tokens"], 0)
        self.assertEqual(result["cost"]["monetary_cost"], 0)
        self.assertGreaterEqual(result["cost"]["wall_ms"], 0)
        self.assertEqual(json.loads(self.store.artifact(result["artifact_digests"]["observation"])), result["observation"])

    def test_absent_quote_and_empty_source_are_completed_not_negative_evidence(self):
        for index, source in enumerate(("Other contents", "")):
            action = self.store.plan(self.request(key=str(index), text=source))
            completed = self.store.run(action["action_id"])
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["result"]["source_support"], "cannot_determine")
            self.assertEqual(completed["result"]["inference_validity"], "cannot_determine")
            self.assertTrue(completed["result"]["qc"]["passed"])
            self.assertFalse(completed["result"]["qc"]["exact_match_present"])
            self.assertEqual(completed["result"]["observation"]["spans"], [])

    def test_match_count_includes_overlaps_and_response_is_bounded(self):
        request = self.request(text="a" * 1000)
        request["quote"] = "aa"
        observation = self.store.run(self.store.plan(request)["action_id"])["result"]["observation"]
        self.assertEqual(observation["match_count"], 999)
        self.assertEqual(len(observation["spans"]), MAX_SPANS)
        self.assertTrue(observation["spans_truncated"])

    def test_maximum_repetitive_input_completes_without_quadratic_rescanning(self):
        request = self.request(text="a" * MAX_SOURCE_BYTES)
        request["quote"] = "a" * MAX_QUOTE_BYTES
        plan = self.store.plan(request)
        started = time.perf_counter()
        observation = self.store.run(plan["action_id"])["result"]["observation"]
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 4.0, f"Maximum repetitive action took {elapsed:.3f}s")
        self.assertEqual(observation["match_count"], MAX_SOURCE_BYTES - MAX_QUOTE_BYTES + 1)
        self.assertEqual(len(observation["spans"]), MAX_SPANS)
        self.assertTrue(observation["spans_truncated"])
        self.assertEqual(observation["spans"][-1]["char_start"], MAX_SPANS - 1)

    def test_unicode_overlapping_spans_keep_exact_byte_and_line_offsets(self):
        request = self.request(text="prefix\n🙂🙂🙂\n")
        request["quote"] = "🙂🙂"
        observation = self.store.run(self.store.plan(request)["action_id"])["result"]["observation"]
        self.assertEqual(observation["match_count"], 2)
        self.assertEqual(observation["spans"], [
            {"char_start": 7, "char_end": 9, "byte_start": 7, "byte_end": 15, "line_start": 2, "line_end": 2},
            {"char_start": 8, "char_end": 10, "byte_start": 11, "byte_end": 19, "line_start": 2, "line_end": 2},
        ])

    def test_repeat_and_restart_return_identical_committed_result(self):
        action_id = self.store.plan(self.request())["action_id"]
        first = self.store.run(action_id)
        counts = self.counts()
        with patch.object(ResearchActionStore, "_observe", side_effect=AssertionError("must not rerun")):
            self.assertEqual(self.store.run(action_id), first)
            restarted = ResearchActionStore(self.root)
            self.assertEqual(restarted.run(action_id), first)
            self.assertEqual(restarted.plan(self.request()), first)
        self.assertEqual(self.counts(), counts)
        self.assertEqual([e["type"] for e in first["events"]], ["planned", "execution_started", "completed"])

    def test_concurrent_processes_commit_one_result(self):
        action_id = self.store.plan(self.request())["action_id"]
        with multiprocessing.get_context("spawn").Pool(3) as pool:
            results = pool.map(_run_in_process, [(str(self.root), action_id)] * 6)
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(self.counts()["events"], 3)
        self.assertEqual(self.counts()["parse_cache"], 1)

    def test_concurrent_planning_uses_one_logical_action(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: ResearchActionStore(self.root).plan(self.request()), range(8)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(self.counts()["actions"], 1)

    def test_parse_reuse_still_executes_new_observation_and_preserves_goal(self):
        first = self.store.run(self.store.plan(self.request())["action_id"])
        new_request = self.request(key="request-2")
        new_request["goal"]["revision"] = 2
        plan = self.store.plan(new_request)
        with patch.object(ResearchActionStore, "_observe", wraps=ResearchActionStore._observe) as observed:
            second = self.store.run(plan["action_id"])
        observed.assert_called_once()
        self.assertFalse(first["result"]["cache"]["parsed_source_reused"])
        self.assertTrue(second["result"]["cache"]["parsed_source_reused"])
        self.assertFalse(second["result"]["cache"]["observation_reused"])
        self.assertEqual(second["spec"]["goal"], new_request["goal"])
        self.assertNotEqual(first["result"]["artifact_digests"]["observation"],
                            second["result"]["artifact_digests"]["observation"])

    def test_changed_source_uri_coverage_or_missing_sections_invalidate_cache(self):
        first = self.store.run(self.store.plan(self.request())["action_id"])
        variants = [self.request(key="text", text="Changed version"),
                    self.request(key="uri", uri="fixture:other-access-scope"),
                    self.request(key="coverage", coverage="full_text", missing_sections=[]),
                    self.request(key="missing", missing_sections=["Different gap"])]
        for request in variants:
            result = self.store.run(self.store.plan(request)["action_id"])["result"]
            self.assertFalse(result["cache"]["parsed_source_reused"])
        self.assertEqual(self.store.get(first["action_id"]), first)
        self.assertEqual(self.store.artifact(first["spec"]["source"]["digest"]), self.request()["source"]["text"].encode())

    def test_artifact_corruption_is_detected_without_committing_execution(self):
        plan = self.store.plan(self.request())
        digest = plan["spec"]["source"]["digest"]
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("UPDATE artifacts SET data=? WHERE digest=?", (b"corrupted", digest))
        self.assert_error("integrity_error", lambda: self.store.artifact(digest))
        self.assert_error("integrity_error", lambda: self.store.run(plan["action_id"]))
        self.assertEqual(self.store.get(plan["action_id"]), plan)

    def test_failed_run_write_rolls_back_every_result_and_can_resume(self):
        plan = self.store.plan(self.request())
        before = self.counts()
        original_event = ResearchActionStore._event

        def fail_final_event(db, action_id, kind, data):
            if kind == "completed":
                raise sqlite3.OperationalError("simulated disk failure")
            return original_event(db, action_id, kind, data)

        with patch.object(ResearchActionStore, "_event", side_effect=fail_final_event):
            self.assert_error("storage_error", lambda: self.store.run(plan["action_id"]))
        self.assertEqual(self.counts(), before)
        self.assertEqual(self.store.get(plan["action_id"]), plan)
        self.assertEqual(ResearchActionStore(self.root).run(plan["action_id"])["status"], "completed")

    def test_failed_plan_write_leaves_no_partial_artifacts(self):
        before = self.counts()
        with patch.object(ResearchActionStore, "_event", side_effect=sqlite3.OperationalError("simulated failure")):
            self.assert_error("storage_error", lambda: self.store.plan(self.request()))
        self.assertEqual(self.counts(), before)

    def test_process_crash_rolls_back_and_restart_resumes(self):
        plan = self.store.plan(self.request())
        before = self.counts()
        process = multiprocessing.get_context("spawn").Process(
            target=_crash_during_run, args=(str(self.root), plan["action_id"]))
        process.start()
        process.join(15)
        if process.is_alive():
            process.terminate()
            process.join(5)
            self.fail("Isolated crash test did not finish")
        self.assertEqual(process.exitcode, 73)
        self.assertEqual(self.counts(), before)
        resumed = ResearchActionStore(self.root)
        self.assertEqual(resumed.get(plan["action_id"]), plan)
        self.assertEqual(resumed.run(plan["action_id"])["status"], "completed")

    def test_schema_and_unknown_ids_fail_closed(self):
        self.assert_error("not_found", lambda: self.store.get("../../file"))
        self.assert_error("not_found", lambda: self.store.run("0" * 32))
        self.assert_error("not_found", lambda: self.store.artifact("0" * 64))
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute("PRAGMA user_version = 99")
        self.assert_error("unsupported_schema", lambda: ResearchActionStore(self.root))
        self.assert_error("unsupported_schema", lambda: self.store.plan(self.request()))


if __name__ == "__main__":
    unittest.main()
