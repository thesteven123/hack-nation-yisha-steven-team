"""Bounded offline engineering evaluation; never a scientific-performance claim.

Run with --output <new isolated directory>. Policies receive only public input
and their own permitted observations. Known fixture oracles remain in the
scorer. There is no model, external tool, hidden-mechanism benchmark, training,
or statistically powered policy ranking here.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import time
from pathlib import Path

from research_lab import LabError, ResearchLabStore, LOADED_EXECUTION_ENVIRONMENT

VERSION = "research-lab-offline-evaluation/1"
POLICIES = {
    "fixed": "Precommit source 1 then source 2; numeric paired summary then extreme-pair sensitivity, independent of outcomes",
    "adaptive": "Run the first action, then follow the persisted review's next method using its actual observation",
    "simple": "One source check or one numerical extreme-pair sensitivity action; keep unused quota visible",
}
RULES = {
    "version": VERSION,
    "purpose": "Engineering correctness and bounded-policy behavior on disjoint known fixtures, not scientific generalization",
    "primary_checks": ["exact local fact accuracy", "QC eligibility accuracy", "no unsupported inference claims", "goal coverage under explicit fixture criteria"],
    "max_actions_per_instance": 2,
    "max_rounds_per_campaign": 2,
    "same_initial_inputs": True,
    "same_adapter_versions": True,
    "same_permissions": "Local supplied-input adapters only; no model, network, human assistance, hidden oracle access or repair",
    "failure_rule": "Retain failures, no-output and unresolved records in denominators; no silent retries",
    "uncertainty_rule": "Report exact counts only; no significance, sample-size adequacy, population or acceleration claim",
    "scorer_access": "Expected values are passed only to the scorer, never the policy function",
    "split": "Development example is disjoint from eight held-out known fixtures; not a blinded or novel scientific test set",
    "policy_costs": "Report actual action units, local compute wall time and end-to-end wall time; zero external/model/$ by construction",
    "ablations": {
        "adaptive_memory_off": "Withhold prior observation/review from the second decision, causing fresh initial planning; no implicit state copied",
        "fixed_cold_storage": "Use a fresh service store for each precommitted action; compare physical input artifact deduplication with fixed warm storage",
    },
    "unverified": ["human active time", "human preference alignment", "native model planner/evaluator quality", "literature semantic extraction accuracy",
                   "external execution recovery", "hidden evaluator isolation against arbitrary agents", "causal discovery", "independent replication",
                   "scientific novelty", "10x acceleration", "all-domain generalization"],
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _brief(goal, hypothesis=""):
    return {"goal": goal, "hypothesis": hypothesis, "success_criteria": "Inspect local facts and frozen QC, then assess the bounded second action",
            "constraints": "Offline fixture; no causal, population, novelty or independent confirmation claims"}


def _source(identifier, texts, counts, *, quote="effect", gap=None):
    sources = [{"id": f"s{i+1}", "title": f"Fixture source {i+1}", "uri": f"fixture://{identifier}/s{i+1}",
                "text": text, "coverage": "excerpt", "missing_sections": ["Independent context outside the fixture"]} for i, text in enumerate(texts)]
    if gap:
        sources[0]["provenance"] = {"gap_marker": gap}
    return {"id": identifier, "split": "held_out", "adapter_id": "source_evidence",
            "brief": _brief("Locate the requested quote and inspect context if present, otherwise exhaust supplied sources"),
            "inputs": {"quote": quote, "sources": sources},
            "oracle": {"legal_match_counts": dict(zip((s["id"] for s in sources), counts))}}


def _numeric(identifier, baseline, treatment, expected_mean, expected_qc, expected_support):
    return {"id": identifier, "split": "held_out", "adapter_id": "paired_numeric",
            "brief": _brief("Estimate paired difference and inspect its sensitivity", "The supplied mean exceeds one point"),
            "inputs": {"baseline": baseline, "treatment": treatment, "unit": "points", "minimum_effect": 1},
            "oracle": {"mean_difference": expected_mean, "qc_passed": expected_qc, "summary_support": expected_support}}


def fixtures():
    """Known correctness fixtures. They are not generated or tuned by outcomes."""
    development = _source("dev_source", ["example quote", "unrelated"], [1, 0], quote="example")
    development["split"] = "development"
    return [development,
            _source("source_first", ["The effect is limited to this fixture.", "No matching passage."], [1, 0]),
            _source("source_second", ["No matching passage.", "An effect appears here; however, limits apply."], [0, 1]),
            _source("source_absent", ["No matching passage.", "Another unrelated passage."], [0, 0]),
            _source("source_gap", ["left[OMITTED]right", "Unrelated."], [0, 0], quote="left[OMITTED]right", gap="[OMITTED]"),
            _numeric("numeric_positive", [1, 2, 3, 4, 5], [4, 5, 6, 7, 8], 3, True, "supported"),
            _numeric("numeric_negative", [1, 2, 3, 4, 5], [-2, -1, 0, 1, 2], -3, True, "contradicted"),
            _numeric("numeric_inconclusive", [0, 0, 0, 0], [-10, 10, -10, 10], 0, True, "cannot_determine"),
            _numeric("numeric_qc_failed", [1, 2], [4, 5], 3, False, "cannot_determine")]


def public_packet(fixture):
    return {key: copy.deepcopy(fixture[key]) for key in ("id", "adapter_id", "brief", "inputs")}


def _request(packet, key, max_actions=2):
    return {"idempotency_key": key, "entry": "hypothesis" if packet["brief"]["hypothesis"] else "goal",
            "brief": copy.deepcopy(packet["brief"]), "adapter_id": packet["adapter_id"], "inputs": copy.deepcopy(packet["inputs"]),
            "budget": {"max_actions": max_actions, "max_rounds": 2}}


def run_policy(packet, policy, root, *, memory=True, warm_storage=True):
    """Public inputs only. This function has no fixture oracle parameter."""
    if policy not in POLICIES:
        raise ValueError("Unknown frozen policy")
    root = Path(root)
    attempts, rounds, campaign_ids, input_storage = [], [], [], {}
    started = time.perf_counter()
    operations = 0

    def new_campaign(index, max_actions):
        store_root = root / ("shared_store" if warm_storage else f"store_{index}")
        store = ResearchLabStore(store_root)
        item = store.create(_request(packet, f"create-{index}", max_actions))
        campaign_ids.append(item["id"])
        input_storage[(str(store_root), item["input_artifact"])] = len(_json(packet["inputs"]).encode("utf-8"))
        return store, item

    def execute(store, item, index, candidate_index=0):
        nonlocal operations
        if operations >= RULES["max_actions_per_instance"]:
            raise RuntimeError("Policy attempted to exceed the frozen evaluator quota")
        candidates = item["current_plan"]["candidates"]
        choice = candidates[candidate_index]
        if choice["id"] != item["current_plan"]["selected_action_id"]:
            item = store.decide(item["id"], {"expected_revision": item["revision"], "idempotency_key": f"policy-select-{index}",
                "kind": "select", "selected_action_id": choice["id"], "feedback": f"Offline {policy} policy selection; this is an automated fixture, not a human study"})
        operations += 1
        try:
            item = store.run(item["id"], {"expected_revision": item["revision"], "idempotency_key": f"run-{index}"})
            record = item["rounds"][-1]
            rounds.append(record)
            attempts.append({"status": record["run"]["status"], "run_id": record["run"]["id"], "campaign_id": item["id"],
                             "method": record["observation"]["kind"], "input_artifact": item["input_artifact"],
                             "observation_artifact": record["run"]["observation_artifact"]})
        except LabError as exc:
            attempts.append({"status": "failed", "error_code": exc.code, "campaign_id": item["id"], "method": choice["method"]})
            return None
        return item

    store, item = new_campaign(0, 2 if policy == "adaptive" and memory else 1)
    if policy == "simple":
        item = execute(store, item, 0, 1 if packet["adapter_id"] == "paired_numeric" else 0)
    elif policy == "fixed":
        item = execute(store, item, 0)
        if packet["adapter_id"] == "paired_numeric" or len(packet["inputs"]["sources"]) > 1:
            store, second = new_campaign(1, 1)
            execute(store, second, 1, 1)
    else:
        item = execute(store, item, 0)
        if item and item["status"] == "awaiting_next":
            if memory:
                item = store.advance(item["id"], {"expected_revision": item["revision"], "idempotency_key": "advance-1"})
                if item["status"] == "planned":
                    execute(store, item, 1)
            else:
                store, second = new_campaign(1, 1)
                execute(store, second, 1)
        elif item and not memory and operations < 2:
            # The one-action subcampaign has hit its quota. The memory ablation
            # receives no outcome/review at the second decision and repeats its
            # initial plan under the instance's remaining authorized unit.
            store, second = new_campaign(1, 1)
            execute(store, second, 1)
    return {"policy": policy, "memory": memory, "warm_storage": warm_storage,
            "campaign_ids": campaign_ids, "attempts": attempts, "rounds": rounds,
            "cost": {"action_units": sum(r["run"]["usage"]["actions"] for r in rounds), "attempted_action_units": operations,
                     "model_tokens": 0, "external_requests": 0, "monetary_cost": 0,
                     "action_wall_ms": round(sum(r["run"]["usage"]["wall_ms"] for r in rounds), 3),
                     "end_to_end_wall_ms": round((time.perf_counter() - started) * 1000, 3)},
            "storage": {"input_artifact_copies": len(input_storage), "input_content_bytes": sum(input_storage.values()),
                        "scope": "CAS input storage only; not provider prompt cache or shared literature extraction"}}


def score(fixture, result):
    oracle = fixture["oracle"]
    mismatches, qc_mismatches, unsupported = [], [], []
    valid, inconclusive = 0, 0
    source_checked, source_context = set(), set()
    methods = set()
    for record in result["rounds"]:
        run_id = record["run"]["id"]
        observation = record["observation"]
        data = observation["data"]
        methods.add(observation["kind"])
        if record["run"]["status"] != "completed":
            continue
        valid += 1
        if record["analysis"]["inference_validity"] != "cannot_determine":
            unsupported.append({"run_id": run_id, "reason": "These fixtures do not support scientific inference validity"})
        if record["analysis"]["source_support"] == "cannot_determine":
            inconclusive += 1
        if fixture["adapter_id"] == "source_evidence":
            source_id = data["source_id"]
            expected = oracle["legal_match_counts"][source_id]
            source_checked.add(source_id)
            if observation["kind"] == "source_context":
                source_context.add(source_id)
            if data["match_count"] != expected:
                mismatches.append({"run_id": run_id, "field": "legal_match_count", "expected": expected, "observed": data["match_count"]})
            if record["analysis"]["source_support"] == "supported" and expected == 0:
                unsupported.append({"run_id": run_id, "reason": "No legal continuous source occurrence exists"})
            if not record["qc"]["passed"]:
                qc_mismatches.append({"run_id": run_id, "expected": True, "observed": False})
        else:
            if not math.isclose(data["mean_difference"], oracle["mean_difference"], rel_tol=1e-12, abs_tol=1e-12):
                mismatches.append({"run_id": run_id, "field": "mean_difference", "expected": oracle["mean_difference"], "observed": data["mean_difference"]})
            if record["qc"]["passed"] != oracle["qc_passed"]:
                qc_mismatches.append({"run_id": run_id, "expected": oracle["qc_passed"], "observed": record["qc"]["passed"]})
            expected_label = oracle["summary_support"] if observation["kind"] == "paired_summary" else "cannot_determine"
            if record["analysis"]["source_support"] != expected_label:
                unsupported.append({"run_id": run_id, "reason": "Label disagrees with the frozen fixture proposition rule", "expected": expected_label})
    if fixture["adapter_id"] == "source_evidence":
        positives = {key for key, count in oracle["legal_match_counts"].items() if count}
        resolved = bool(positives & source_context) if positives else set(oracle["legal_match_counts"]) <= source_checked
    else:
        resolved = "paired_summary" in methods and (bool({"leave_one_out", "extreme_sensitivity"} & methods) or not oracle["qc_passed"])
    return {"attempted_actions": len(result["attempts"]), "completed_observations": valid,
            "execution_failures": sum(a["status"] != "completed" for a in result["attempts"]),
            "inconclusive_claims": inconclusive, "fact_mismatches": mismatches, "qc_mismatches": qc_mismatches,
            "unsupported_claims": unsupported, "bounded_goal_covered": resolved,
            "scientific_goal_proven": False, "fixture_is_independent_statistical_unit": True}


def run_evaluation(output_root, *, include_ablations=True):
    output_root = Path(output_root).absolute()
    if output_root.exists() and any(output_root.iterdir()):
        raise ValueError("Use a new empty output directory; existing evaluation artifacts are immutable")
    output_root.mkdir(parents=True, exist_ok=True)
    cases = fixtures()
    rules = copy.deepcopy(RULES)
    rules["execution_environment"] = copy.deepcopy(LOADED_EXECUTION_ENVIRONMENT)
    rules["evaluation_code_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    rules.update(policies=POLICIES, fixture_manifest=[{"id": f["id"], "split": f["split"], "input_hash": _hash(public_packet(f)),
                                                    "oracle_hash": _hash(f["oracle"])} for f in cases])
    frozen_hash = _hash(rules)
    (output_root / "frozen-rules.json").write_text(json.dumps({"sha256": frozen_hash, "rules": rules}, indent=2), encoding="utf-8")
    records = []
    variants = [(p, True, True, p) for p in POLICIES]
    if include_ablations:
        variants += [("adaptive", False, True, "adaptive_memory_off"), ("fixed", True, False, "fixed_cold_storage")]
    for fixture in cases:
        if fixture["split"] != "held_out":
            continue
        for policy, memory, warm, name in variants:
            result = run_policy(public_packet(fixture), policy, output_root / "instances" / fixture["id"] / name,
                                memory=memory, warm_storage=warm)
            report = {"fixture_id": fixture["id"], "variant": name, "rules_hash": frozen_hash,
                      "public_input_hash": _hash(public_packet(fixture)), "score": score(fixture, result), **result}
            records.append(report)
    summaries = {}
    for *_, name in variants:
        selected = [r for r in records if r["variant"] == name]
        summaries[name] = {"instances": len(selected), "bounded_goals_covered": sum(r["score"]["bounded_goal_covered"] for r in selected),
            "fact_mismatches": sum(len(r["score"]["fact_mismatches"]) for r in selected),
            "qc_mismatches": sum(len(r["score"]["qc_mismatches"]) for r in selected),
            "unsupported_claims": sum(len(r["score"]["unsupported_claims"]) for r in selected),
            "execution_failures": sum(r["score"]["execution_failures"] for r in selected),
            "inconclusive_claims": sum(r["score"]["inconclusive_claims"] for r in selected),
            "action_units": sum(r["cost"]["action_units"] for r in selected),
            "input_artifact_copies": sum(r["storage"]["input_artifact_copies"] for r in selected),
            "end_to_end_wall_ms": round(sum(r["cost"]["end_to_end_wall_ms"] for r in selected), 3)}
    final = {"version": VERSION, "rules_hash": frozen_hash, "held_out_known_fixtures": 8,
             "policy_instances": len(records), "summaries": summaries, "records": records,
             "unverified": rules["unverified"], "interpretation": "Exact engineering counts on known fixtures; no statistical superiority, scientific novelty or acceleration claim"}
    (output_root / "report.json").write_text(json.dumps(final, ensure_ascii=False, indent=2), encoding="utf-8")
    return final


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = run_evaluation(args.output)
    print(json.dumps({"rules_hash": report["rules_hash"], "policy_instances": report["policy_instances"], "summaries": report["summaries"]}, indent=2))
