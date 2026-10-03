from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime


def critique_run(repository, run_id: str) -> dict:
    current = repository.get_run(run_id)
    if not current or current["status"] != "completed":
        raise ValueError("Only completed experiment runs can be critiqued.")

    project_id = current["project_id"]
    brief = repository.get_active_brief(project_id)
    config = current["config"]
    all_runs = repository.list_completed_runs(project_id)
    candidate_runs = [run for run in all_runs if _same_variant(run["config"], config)]
    candidate_seeds = {run["config"]["seed"] for run in candidate_runs}
    findings = []
    limitations = ["Results are from simulation only; they do not establish physical-robot safety or deployment readiness."]
    verdict = "provisional"
    related_ids = [run["id"] for run in candidate_runs]
    next_action = {"action": "replicate", "reason": "Collect results across at least three distinct training seeds."}

    if not _has_required_metrics(current["metrics"]):
        verdict = "invalid_comparison"
        findings.append("Required success and safety metrics are missing.")
        limitations.append("The run cannot be interpreted until required metrics are available.")
        next_action = {"action": "repair_or_rerun", "reason": "Required metrics were not recorded."}
    elif len(candidate_seeds) < brief["minimum_seeds_for_claim"]:
        findings.append(f"This condition has {len(candidate_seeds)} distinct seed(s); at least {brief['minimum_seeds_for_claim']} are required for a claim.")
    elif config["noise_train"] == 0:
        findings.append("A replicated baseline is characterized, but no training-noise intervention is being tested in this condition.")
        next_action = {"action": "propose_intervention", "reason": "Compare a permitted nonzero training-noise condition against the baseline."}
    else:
        baseline_config = dict(config)
        baseline_config["noise_train"] = 0.0
        baseline_runs = [run for run in all_runs if _same_variant(run["config"], baseline_config)]
        baseline_seeds = {run["config"]["seed"] for run in baseline_runs}
        related_ids = sorted(set(related_ids + [run["id"] for run in baseline_runs]))

        if len(baseline_seeds) < brief["minimum_seeds_for_claim"]:
            findings.append("The intervention has at least three seeds, but a matched baseline with three distinct seeds is not yet available.")
            next_action = {"action": "replicate_baseline", "reason": "Run the matching zero-noise baseline seeds before comparison."}
        else:
            candidate = _mean_metrics(candidate_runs, "noisy")
            baseline = _mean_metrics(baseline_runs, "noisy")
            candidate_clean = _mean_metrics(candidate_runs, "clean")
            baseline_clean = _mean_metrics(baseline_runs, "clean")
            success_gain = candidate["success_rate"] - baseline["success_rate"]
            clean_decline = baseline_clean["success_rate"] - candidate_clean["success_rate"]
            safety_increase = candidate["safety_violations_per_episode"] - baseline["safety_violations_per_episode"]
            findings.extend([
                f"Noisy-evaluation success-rate difference versus matched baseline: {success_gain:+.3f}.",
                f"Clean-evaluation success-rate decline versus matched baseline: {clean_decline:+.3f}.",
                f"Safety-proxy events per episode difference versus matched baseline: {safety_increase:+.3f}.",
            ])
            passed = (
                success_gain >= brief["minimum_noisy_success_improvement"]
                and clean_decline <= brief["max_clean_success_decline"]
                and safety_increase <= brief["safety_regression_tolerance"]
            )
            if passed:
                verdict = "supported"
                findings.append("The predefined improvement and safety-proxy thresholds were met across the available seeds.")
                next_action = {"action": "human_interpretation", "reason": "A scientist must decide whether to accept this as evidence."}
            else:
                verdict = "inconclusive"
                findings.append("At least one predefined performance or safety-proxy threshold was not met.")
                next_action = {"action": "review_or_refine", "reason": "Inspect the tradeoff and choose a bounded next experiment."}

    report = {
        "id": f"CRIT-{datetime.now(UTC).strftime('%Y%m%d')}-{run_id.removeprefix('EXP-')}",
        "project_id": project_id,
        "experiment_run_ids": related_ids,
        "verdict": verdict,
        "findings": findings,
        "limitations": limitations,
        "recommended_next_action": next_action,
        "created_by": "deterministic_critic",
        "created_at": datetime.now(UTC).isoformat(),
    }
    repository.save_critic_report(report)
    return report


def _same_variant(left: dict, right: dict) -> bool:
    ignored = {"seed"}
    return {key: value for key, value in left.items() if key not in ignored} == {
        key: value for key, value in right.items() if key not in ignored
    }


def _has_required_metrics(metrics: dict) -> bool:
    return all(
        key in metrics
        for key in (
            "clean_success_rate",
            "noisy_success_rate",
            "clean_safety_violations_per_episode",
            "noisy_safety_violations_per_episode",
        )
    )


def _mean_metrics(runs: list[dict], condition: str) -> dict:
    values = defaultdict(list)
    for run in runs:
        metrics = run["metrics"]
        values["success_rate"].append(metrics[f"{condition}_success_rate"])
        values["safety_violations_per_episode"].append(metrics[f"{condition}_safety_violations_per_episode"])
    return {key: sum(items) / len(items) for key, items in values.items()}
