import copy
import json
import tempfile
import unittest
from pathlib import Path

from research_lab_evaluation import RULES, fixtures, public_packet, run_evaluation, run_policy, score


class ResearchLabEvaluationTests(unittest.TestCase):
    def test_oracle_excluded_and_development_disjoint(self):
        cases = fixtures()
        self.assertEqual(len([x for x in cases if x["split"] == "held_out"]), 8)
        self.assertEqual(len({x["id"] for x in cases}), len(cases))
        for case in cases:
            packet = public_packet(case)
            self.assertNotIn("oracle", packet)
            self.assertNotIn("expected_support", json.dumps(packet))

    def test_frozen_rules_all_instances_costs_and_failure_denominator(self):
        with tempfile.TemporaryDirectory() as root:
            report = run_evaluation(Path(root) / "evaluation")
            self.assertEqual(report["policy_instances"], 40)
            self.assertEqual(len(report["rules_hash"]), 64)
            for record in report["records"]:
                self.assertEqual(record["rules_hash"], report["rules_hash"])
                self.assertLessEqual(record["cost"]["action_units"], RULES["max_actions_per_instance"])
                self.assertEqual(record["score"]["fact_mismatches"], [])
                self.assertEqual(record["score"]["qc_mismatches"], [])
                self.assertEqual(record["score"]["unsupported_claims"], [])
                self.assertEqual(record["cost"]["external_requests"], 0)
                self.assertEqual(record["cost"]["model_tokens"], 0)
            with self.assertRaisesRegex(ValueError, "immutable"):
                run_evaluation(Path(root) / "evaluation")

    def test_result_dependent_policy_and_memory_ablation_are_observed(self):
        case = next(x for x in fixtures() if x["id"] == "source_first")
        with tempfile.TemporaryDirectory() as root:
            adaptive = run_policy(public_packet(case), "adaptive", Path(root) / "adaptive")
            memory_off = run_policy(public_packet(case), "adaptive", Path(root) / "no-memory", memory=False)
        self.assertEqual([r["observation"]["kind"] for r in adaptive["rounds"]], ["exact_quote", "source_context"])
        self.assertEqual([r["observation"]["kind"] for r in memory_off["rounds"]], ["exact_quote", "exact_quote"])
        self.assertTrue(score(case, adaptive)["bounded_goal_covered"])
        self.assertFalse(score(case, memory_off)["bounded_goal_covered"])

    def test_cold_storage_ablation_does_not_change_observation_values(self):
        case = next(x for x in fixtures() if x["id"] == "numeric_positive")
        with tempfile.TemporaryDirectory() as root:
            warm = run_policy(public_packet(case), "fixed", Path(root) / "warm")
            cold = run_policy(public_packet(case), "fixed", Path(root) / "cold", warm_storage=False)
        self.assertEqual([r["observation"] for r in warm["rounds"]], [r["observation"] for r in cold["rounds"]])
        self.assertEqual(warm["storage"]["input_artifact_copies"], 1)
        self.assertEqual(cold["storage"]["input_artifact_copies"], 2)

    def test_scorer_records_unsupported_claims_and_execution_failure(self):
        case = next(x for x in fixtures() if x["id"] == "numeric_negative")
        with tempfile.TemporaryDirectory() as root:
            result = run_policy(public_packet(case), "simple", Path(root) / "instance")
        invalid = copy.deepcopy(result)
        invalid["rounds"][0]["analysis"]["inference_validity"] = "supported"
        invalid["attempts"].append({"status": "failed", "error_code": "injected_failure"})
        assessed = score(case, invalid)
        self.assertEqual(len(assessed["unsupported_claims"]), 1)
        self.assertEqual(assessed["execution_failures"], 1)
        self.assertEqual(assessed["attempted_actions"], 2)


if __name__ == "__main__":
    unittest.main()
