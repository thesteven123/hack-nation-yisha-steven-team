"""Branch contract/helper checks; not product-route or native acceptance."""
import copy
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import research_branches as branches
from research_lab import ResearchLabStore, SourceEvidenceAdapter
from tests.test_research_lab import source_request, numeric_request


def proposals():
    def one(identity, source, questions=None, dependencies=None):
        return {"id": identity, "title": identity, "question": "Inspect this supplied source", "success_criterion": "Report exact occurrence and coverage",
                "methods": ["exact_quote"], "source_ids": [source], "questions": questions or [], "depends_on": dependencies or []}
    return [one("a", "s1", [{"id": "q1", "prompt": "Which supplied-source scope should this action use?", "required": True}]),
            one("b", "s2"), one("c", "s2", dependencies=[{"branch_id": "a", "require": "qc_passed"}])]


class ResearchBranchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.lab = ResearchLabStore(self.root / "lab")
        self.campaign = self.lab.create(source_request())
        self.inputs = self.lab.artifact(self.campaign["id"], self.campaign["input_artifact"])["content"]
        self.artifacts = {}
        self.state = branches.create_set(self.campaign, proposals(), self.inputs, freeze_artifact=self.freeze, at="creation")

    def freeze(self, value):
        digest = branches._hash(value)
        self.artifacts[digest] = copy.deepcopy(value)
        return digest

    def dependency_state(self, state=None):
        return {"registered_input_artifacts": [b["input_artifact"] for b in (state or self.state)["branches"]], "blocked_input_artifacts": []}

    def gate(self, identity, *, campaign=None, state=None, inputs=None, dependencies=None):
        return branches.gate(campaign or self.campaign, state or self.state, identity, inputs or self.inputs, dependencies or self.dependency_state(state))

    def scope(self, identity, *, campaign=None, state=None, inputs=None):
        return branches.freeze_scope(campaign or self.campaign, state or self.state, identity, inputs or self.inputs, self.dependency_state(state))

    def error(self, code, callback):
        with self.assertRaises(branches.BranchError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def answer_a(self):
        self.state, _ = branches.answer_question(self.campaign, self.state, "a", self.state["branches"][0]["revision"], "q1", "Use the existing supplied scope.",
                                                  at="answer", freeze_artifact=self.freeze)

    def execute_actual_adapter(self, identity):
        frozen = self.scope(identity)
        branch = branches._branch(self.state, identity)
        inputs = self.artifacts[branch["input_artifact"]]
        adapter = SourceEvidenceAdapter()
        spec = adapter.candidates(inputs, [], None)[0]
        self.assertIn(spec["method"], branch["methods"])
        work_id = "run_" + identity
        self.state = branches.mark_owned_work(self.state, identity, work_id, "local_action", frozen)
        observation = adapter.execute(inputs, spec)
        qc = adapter.quality(observation, spec)
        record = {"observation": observation, "qc": qc, "run_id": work_id}
        ref = {"run_id": work_id, "round_artifact": self.freeze(record), "observation_artifact": self.freeze(observation),
               "input_artifact": branch["input_artifact"], "status": "completed", "qc_passed": qc["passed"]}
        self.state = branches.accept_local_result(self.campaign, self.state, frozen, self.inputs, self.dependency_state(), ref, at="completed")
        self.state, _ = branches.acknowledge_work(self.state, identity, work_id, "completed", cleanup_acknowledged=True)
        return observation

    def test_three_branches_are_one_campaign_with_one_budget_and_precise_projections(self):
        self.assertEqual(self.state["campaign_id"], self.campaign["id"])
        self.assertEqual(len(self.state["branches"]), 3)
        self.assertTrue(all("budget" not in b for b in self.state["branches"]))
        self.assertEqual(self.artifacts[self.state["branches"][0]["input_artifact"]]["sources"][0]["id"], "s1")
        self.assertEqual(self.campaign["budget"]["used_actions"], 0)
        self.assertFalse(self.state["automatic_dispatch"])

    def test_required_answer_blocks_only_a_and_dependent_c_while_b_runs_real_calculation(self):
        self.assertFalse(self.gate("a")["allowed"])
        self.assertTrue(self.gate("b")["allowed"])
        self.assertFalse(self.gate("c")["allowed"])
        result = self.execute_actual_adapter("b")
        self.assertEqual(result["data"]["match_count"], 1)
        self.assertIsNone(self.state["branches"][0]["latest_result"])
        self.assertIsNone(self.state["branches"][2]["latest_result"])

    def test_actual_answer_is_exact_versioned_and_not_scientific_validation(self):
        before = self.scope("b")
        value = "  Provided scope only; do not fetch a new source.  "
        self.state, record = branches.answer_question(self.campaign, self.state, "a", 1, "q1", value, at="t", freeze_artifact=self.freeze)
        self.assertEqual(record["answer"], value)
        self.assertEqual(record["scientific_status"], "human_statement_not_validated")
        self.assertTrue(self.gate("a")["allowed"])
        self.assertEqual(before, self.scope("b"))
        self.error("branch_revision_conflict", lambda: branches.answer_question(self.campaign, self.state, "a", 1, "q1", "late", at="t", freeze_artifact=self.freeze))

    def test_dependent_branch_uses_fixed_completed_qc_reference_not_just_upstream_status(self):
        self.answer_a()
        self.execute_actual_adapter("a")
        self.assertTrue(self.gate("c")["allowed"])
        frozen = self.scope("c")
        reference = frozen["dependency_refs"][0]
        self.assertEqual(reference["run_id"], "run_a")
        self.assertIn(reference["observation_artifact"], self.artifacts)
        changed = copy.deepcopy(self.state)
        changed["branches"][0]["latest_result"]["qc_passed"] = False
        self.assertFalse(self.gate("c", state=changed)["allowed"])

    def test_current_source_correction_blocks_a_and_c_but_unrelated_b_scope_survives(self):
        self.answer_a(); self.execute_actual_adapter("a")
        original_b = self.scope("b")
        changed_inputs = copy.deepcopy(self.inputs)
        changed_inputs["sources"][0]["text"] = "Corrected source one"
        changed = copy.deepcopy(self.campaign)
        changed["input_artifact"] = self.freeze(changed_inputs)
        changed["brief"]["revision"] += 1
        changed["revision"] += 1
        self.assertFalse(self.gate("a", campaign=changed, inputs=changed_inputs)["allowed"])
        self.assertFalse(self.gate("c", campaign=changed, inputs=changed_inputs)["allowed"])
        self.assertEqual(original_b, self.scope("b", campaign=changed, inputs=changed_inputs))

    def test_missing_current_source_never_falls_back_to_historical_packet(self):
        current = copy.deepcopy(self.inputs)
        current["sources"] = current["sources"][:1]
        campaign = copy.deepcopy(self.campaign)
        campaign["input_artifact"] = self.freeze(current)
        codes = {b["code"] for b in self.gate("b", campaign=campaign, inputs=current)["blockers"]}
        self.assertIn("branch_input_missing", codes)

    def test_pending_shared_correction_and_unregistered_branch_fail_closed_selectively(self):
        dependencies = self.dependency_state()
        dependencies["blocked_input_artifacts"] = [self.state["branches"][0]["input_artifact"]]
        self.assertFalse(self.gate("a", dependencies=dependencies)["allowed"])
        self.assertTrue(self.gate("b", dependencies=dependencies)["allowed"])
        self.assertFalse(self.gate("b", dependencies={"registered_input_artifacts": [], "blocked_input_artifacts": []})["allowed"])

    def test_root_scientific_context_and_authority_changes_stale_every_prepared_branch(self):
        frozen = self.scope("b")
        changed = copy.deepcopy(self.campaign)
        changed["brief"]["success_criteria"] = "A new criterion after prior planning"
        self.assertFalse(self.gate("b", campaign=changed)["allowed"])
        for operation in ("pause", "stop"):
            state = branches.control_root(self.state, operation)
            self.assertFalse(self.gate("b", state=state)["allowed"])
        state = branches.control_root(branches.control_root(self.state, "pause"), "resume")
        self.error("branch_scope_changed", lambda: branches.check_scope(self.campaign, state, frozen, self.inputs, self.dependency_state(state)))

    def test_branch_pause_does_not_invalidate_independent_work(self):
        frozen = self.scope("b")
        state = branches.control_branch(self.state, "a", 1, "pause", at="pause")
        self.assertEqual(self.scope("b", state=state), frozen)
        self.assertFalse(self.gate("a", state=state)["allowed"])

    def test_owned_cancellation_unknown_cleanup_preserves_ownership_and_does_not_refund_used_jobs(self):
        frozen = self.scope("b")
        state = branches.mark_owned_work(self.state, "b", "job-b", "native_role", frozen)
        self.error("work_not_owned", lambda: branches.request_cancel(state, "b", "foreign-job", at="cancel"))
        state = branches.request_cancel(state, "b", "job-b", at="cancel")
        state, report = branches.acknowledge_work(state, "b", "job-b", "unknown_outcome", cleanup_acknowledged=False)
        self.assertFalse(report["may_release_unused_reservation"])
        self.assertIsNotNone(branches._branch(state, "b")["owned_work"])
        self.assertTrue(report["model_used_slots_are_not_refunded"])
        self.error("branch_blocked", lambda: branches.check_scope(self.campaign, state, frozen, self.inputs, self.dependency_state(state)))
        state, report = branches.acknowledge_work(state, "b", "job-b", "cancelled", cleanup_acknowledged=True)
        self.assertTrue(report["acknowledged"])
        self.assertIsNone(branches._branch(state, "b")["owned_work"])
        self.assertEqual(branches._branch(state, "b")["control"], "paused")

    def test_rebind_preserves_historical_result_and_invalidates_old_answer_context(self):
        self.answer_a(); self.execute_actual_adapter("a")
        before = copy.deepcopy(self.state["branches"][0]["latest_result"])
        state = branches.rebind_branch(self.campaign, self.state, "a", self.state["branches"][0]["revision"], self.inputs,
                                       changes={"question": "A materially changed branch question"}, at="revise", freeze_artifact=self.freeze)
        self.assertEqual(state["branches"][0]["latest_result"], before)
        self.assertFalse(self.gate("a", state=state)["allowed"])
        self.assertTrue(self.gate("b", state=state)["allowed"])

    def test_revised_upstream_question_cannot_reuse_old_result_for_dependent_branch(self):
        proposal = proposals()
        proposal[2]["depends_on"] = [{"branch_id": "b", "require": "qc_passed"}]
        self.state = branches.create_set(self.campaign, proposal, self.inputs, at="new", freeze_artifact=self.freeze)
        self.execute_actual_adapter("b")
        self.assertTrue(self.gate("c")["allowed"])
        self.state = branches.rebind_branch(self.campaign, self.state, "b", self.state["branches"][1]["revision"], self.inputs,
                                            at="new-question", freeze_artifact=self.freeze, changes={"question": "A different intended proposition"})
        self.assertFalse(self.gate("c")["allowed"])
        self.assertIsNotNone(self.state["branches"][1]["latest_result"])

    def test_unsupported_methods_budget_fields_cycles_and_four_questions_rejected(self):
        cases = []
        p = proposals(); p[0]["methods"] = ["arbitrary_code"]; cases.append(p)
        p = proposals(); p[0]["budget"] = {"max_jobs": 6}; cases.append(p)
        p = proposals(); p[0]["depends_on"] = [{"branch_id": "c", "require": "completed"}]; cases.append(p)
        p = proposals(); p[1]["questions"] = [{"id": "q" + str(i), "prompt": "Question", "required": True} for i in range(3)]; cases.append(p)
        for value in cases:
            self.error("invalid_branch", lambda: branches.create_set(self.campaign, value, self.inputs, at="x", freeze_artifact=self.freeze))
        self.assertNotIn("branch_set", self.campaign)

    def test_unrelated_campaign_revision_does_not_stale_scope_but_mutated_digest_does(self):
        frozen = self.scope("b")
        campaign = copy.deepcopy(self.campaign); campaign["revision"] += 50
        self.assertEqual(branches.check_scope(campaign, self.state, frozen, self.inputs, self.dependency_state()), frozen)
        corrupted = {**frozen, "sha256": "0" * 64}
        self.error("branch_scope_changed", lambda: branches.check_scope(campaign, self.state, corrupted, self.inputs, self.dependency_state()))

    def test_actual_adapter_method_output_is_scoped_and_not_independent_replication(self):
        observation = self.execute_actual_adapter("b")
        result = self.state["branches"][1]["latest_result"]
        self.assertEqual(self.artifacts[result["observation_artifact"]], observation)
        self.assertEqual(result["input_artifact"], self.state["branches"][1]["input_artifact"])
        self.assertEqual(self.campaign["budget"]["used_actions"], 0)  # Pure helper does not pretend it persisted execution.

    def test_shared_local_admission_under_existing_sqlite_transaction_cannot_oversubscribe(self):
        request = source_request("one-unit"); request["budget"] = {"max_actions": 1, "max_rounds": 1}
        campaign = self.lab.create(request)
        path = self.root / "transaction-fixture.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE state(payload TEXT)")
            db.execute("INSERT INTO state VALUES(?)", (json.dumps(campaign),))
        def reserve(_):
            with closing(sqlite3.connect(path, timeout=3)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                current = json.loads(db.execute("SELECT payload FROM state").fetchone()[0])
                try:
                    branches.check_shared_budget(current)
                except branches.BranchError as error:
                    return error.code
                current["budget"]["reserved_actions"] += 1
                db.execute("UPDATE state SET payload=?", (json.dumps(current),))
                return "reserved"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, ("a", "b")))
        self.assertCountEqual(results, ["reserved", "budget_exhausted"])
        self.error("budget_exhausted", lambda: branches.check_shared_budget(campaign, native_quota={"used": 5, "reserved": 1, "limit_jobs": 6}))

    def test_numeric_branch_reuses_parent_data_without_new_source_schema_or_permissions(self):
        campaign = self.lab.create(numeric_request())
        inputs = self.lab.artifact(campaign["id"], campaign["input_artifact"])["content"]
        proposal = proposals()[0]; proposal.pop("source_ids")
        proposal["methods"] = [campaign["brief"]["authorized_actions"][0]]
        state = branches.create_set(campaign, [proposal], inputs, at="numeric", freeze_artifact=self.freeze)
        self.assertEqual(state["branches"][0]["input_artifact"], campaign["input_artifact"])
        self.assertEqual(campaign["budget"]["used_actions"], 0)


if __name__ == "__main__":
    unittest.main()
