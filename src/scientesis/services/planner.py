from __future__ import annotations

from scientesis.domain.models import ExperimentConfig
from scientesis.services.hypotheses import validate_hypothesis
from scientesis.services.validation import validate_config


_SCORE_WEIGHTS = {
    "uncertainty_reduction": 0.4,
    "robustness_value": 0.3,
    "safety_value": 0.2,
    "compute_cost": -0.1,
}


def plan_next_work(repository, project_id: str | None = None) -> dict:
    project_id = project_id or repository.get_project_id()
    brief = repository.get_active_brief(project_id)
    runs = repository.list_runs(project_id)
    completed = [run for run in runs if run["status"] == "completed"]
    proposals = repository.list_proposals(project_id)
    decisions = repository.list_decisions(project_id)
    reports = repository.list_critic_reports(project_id)
    pending_decisions = [decision for decision in decisions if decision["status"] == "pending"]
    active_run_proposals = {
        run["proposal_id"]
        for run in runs
        if run["status"] in {"queued", "reserved", "running"} and run.get("proposal_id")
    }
    active_proposals = [
        proposal
        for proposal in proposals
        if proposal["research_brief_version"] == brief["version"]
        and (
            proposal["status"] in {"proposed", "reviewed", "approved"}
            or (proposal["status"] == "queued" and proposal["id"] in active_run_proposals)
        )
    ]

    context = {
        "project_id": project_id,
        "research_brief_version": brief["version"],
        "runs_used": len(runs),
        "budget_remaining": max(0, brief["experiment_budget"] - len(runs)),
        "completed_runs": len(completed),
        "minimum_seeds": brief["minimum_seeds_for_claim"],
        "pending_decisions": [
            {"id": decision["id"], "question": decision["question"]}
            for decision in pending_decisions
        ],
    }

    if active_proposals:
        proposal = active_proposals[0]
        return _plan(
            "existing_work",
            "review_existing_proposal",
            "A proposal for the current ResearchBrief is already open. Review that exact proposal instead of creating a duplicate.",
            context,
            proposal_id=proposal["id"],
            proposal_status=proposal["status"],
        )

    if context["budget_remaining"] <= 0:
        return _plan(
            "budget_reached",
            "hold",
            "The active ResearchBrief experiment budget is fully reserved. A scientist must revise the brief before more runs can be proposed.",
            context,
        )

    allowed_noise = brief["allowed_noise_values"]
    treatment_noise = min((value for value in allowed_noise if value > 0), default=None)
    evaluation_noise = treatment_noise if treatment_noise is not None else 0.0
    minimum_seeds = brief["minimum_seeds_for_claim"]
    baseline_runs = _condition_runs(runs, brief, 0.0, evaluation_noise, 0.0)
    baseline_completed = [run for run in baseline_runs if run["status"] == "completed"]
    baseline_completed_seeds = {run["config"]["seed"] for run in baseline_completed}
    baseline_active_seeds = {
        run["config"]["seed"]
        for run in baseline_runs
        if run["status"] in {"queued", "reserved", "running"}
    }
    context["baseline_completed_seeds"] = _ordered_seeds(brief, baseline_completed_seeds)

    if len(baseline_completed_seeds) < minimum_seeds:
        seed = _next_seed(brief["training_seeds"], baseline_completed_seeds, baseline_active_seeds)
        if seed is None:
            return _decision_needed(
                context,
                "Every configured seed for the zero-training-noise control has either completed or is already active, but the claim threshold is not met.",
            )
        config = _config(brief, 0.0, evaluation_noise, 0.0, seed)
        action = "establish_baseline" if not baseline_completed else "replicate_baseline"
        reason = (
            f"Establish the zero-training-noise control with seed {seed}. The runner evaluates each policy under clean and {evaluation_noise:.3f} noisy observations."
            if action == "establish_baseline"
            else f"The baseline has {len(baseline_completed_seeds)} of {minimum_seeds} required completed seeds; replicate it with seed {seed}."
        )
        return _plan(action, action, reason, context, config=config, priority=_priority(action, brief))

    if treatment_noise is None:
        return _decision_needed(
            context,
            "The ResearchBrief permits no nonzero training-noise condition, so the proposed intervention would be outside its authorized scope.",
        )

    paired_seeds = _ordered_seeds(brief, baseline_completed_seeds)[:minimum_seeds]
    treatment_runs = _condition_runs(runs, brief, treatment_noise, evaluation_noise, 0.0)
    treatment_completed = [
        run
        for run in treatment_runs
        if run["status"] == "completed" and run["config"]["seed"] in paired_seeds
    ]
    treatment_completed_seeds = {run["config"]["seed"] for run in treatment_completed}
    treatment_active_seeds = {
        run["config"]["seed"]
        for run in treatment_runs
        if run["status"] in {"queued", "reserved", "running"} and run["config"]["seed"] in paired_seeds
    }
    context["paired_seed_set"] = paired_seeds
    context["treatment_completed_seeds"] = _ordered_seeds(brief, treatment_completed_seeds)

    if len(treatment_completed_seeds) < minimum_seeds:
        seed = _next_seed(paired_seeds, treatment_completed_seeds, treatment_active_seeds)
        if seed is None:
            return _decision_needed(
                context,
                "The baseline is complete, but no unused matched seed remains for this intervention within the current ResearchBrief.",
            )
        config = _config(brief, treatment_noise, evaluation_noise, 0.0, seed)
        action = "test_training_noise" if not treatment_completed else "replicate_training_noise"
        hypothesis = _hypothesis(brief, treatment_noise, evaluation_noise, paired_seeds)
        reason = (
            f"Test the smallest permitted nonzero training-noise condition ({treatment_noise:.3f}) against the zero-noise baseline, starting with matched seed {seed}."
            if action == "test_training_noise"
            else f"The matched treatment has {len(treatment_completed_seeds)} of {minimum_seeds} required completed seeds; replicate it with baseline seed {seed}."
        )
        return _plan(
            action,
            action,
            reason,
            context,
            config=config,
            hypothesis=hypothesis,
            priority=_priority(action, brief),
        )

    matched_baseline = [run for run in baseline_completed if run["config"]["seed"] in paired_seeds]
    required_run_ids = {
        run["id"] for run in matched_baseline + treatment_completed
    }
    matching_reports = [
        report
        for report in reports
        if required_run_ids <= set(report["experiment_run_ids"])
    ]
    if not matching_reports:
        candidate = next(run for run in reversed(treatment_completed) if run["config"]["seed"] in paired_seeds)
        return _plan(
            "critic_required",
            "run_deterministic_critic",
            "The matched baseline and treatment have enough completed seeds. Run the deterministic critic before interpreting or extending the result.",
            context,
            run_id=candidate["id"],
            priority=_priority("run_deterministic_critic", brief),
        )

    report = max(matching_reports, key=lambda item: item["created_at"])
    if report["verdict"] == "supported":
        return _plan(
            "human_interpretation_required",
            "record_scientist_interpretation",
            "The deterministic critic found the predefined thresholds met. A scientist must record an interpretation; the planner will not declare the result or authorize another experiment.",
            context,
            critic_report_id=report["id"],
            priority=_priority("record_scientist_interpretation", brief),
        )
    return _plan(
        "scientist_review_required",
        "review_critic_report",
        f"The latest matching critic verdict is {report['verdict']}. Review its findings and choose any new direction explicitly; the planner will not invent a follow-up condition.",
        context,
        critic_report_id=report["id"],
        priority=_priority("review_critic_report", brief),
    )


def _condition_runs(runs: list[dict], brief: dict, noise_train: float, noise_eval: float, safety_penalty: float) -> list[dict]:
    return [
        run
        for run in runs
        if run["config"].get("brief_version") == brief["version"]
        and run["config"].get("environment") == brief["environment"]
        and run["config"].get("algorithm") == brief["algorithm"]
        and run["config"].get("training_steps") == brief["training_steps_per_run"]
        and run["config"].get("evaluation_episodes") == brief["evaluation_episodes"]
        and run["config"].get("success_distance_threshold") == brief["success_distance_threshold"]
        and run["config"].get("actuator_saturation_threshold") == brief["actuator_saturation_threshold"]
        and run["config"].get("noise_train") == noise_train
        and run["config"].get("noise_eval") == noise_eval
        and run["config"].get("safety_penalty") == safety_penalty
    ]


def _config(brief: dict, noise_train: float, noise_eval: float, safety_penalty: float, seed: int) -> dict:
    config = ExperimentConfig.from_brief(brief, noise_train, noise_eval, safety_penalty, seed).to_dict()
    validate_config(config, brief)
    return config


def _next_seed(allowed: list[int], completed: set[int], active: set[int]) -> int | None:
    return next((seed for seed in allowed if seed not in completed and seed not in active), None)


def _ordered_seeds(brief: dict, seeds: set[int]) -> list[int]:
    return [seed for seed in brief["training_seeds"] if seed in seeds]


def _hypothesis(brief: dict, noise: float, evaluation_noise: float, seeds: list[int]) -> dict:
    original_text = (
        f"Training with observation noise {noise:.3f} will improve noisy-evaluation success by at least "
        f"{brief['minimum_noisy_success_improvement']:.3f} versus the matched zero-training-noise baseline, "
        f"while clean success declines by no more than {brief['max_clean_success_decline']:.3f} and safety-proxy events "
        f"increase by no more than {brief['safety_regression_tolerance']:.3f} per episode."
    )
    protocol = validate_hypothesis(
        original_text,
        {
            "environment": brief["environment"],
            "algorithm": brief["algorithm"],
            "treatment": f"Train with observation noise {noise:.3f}; evaluate with noise {evaluation_noise:.3f} and under the clean condition.",
            "control": f"Train with observation noise 0.000; use the same evaluation protocol and matched seeds {seeds}.",
            "primary_outcome": brief["primary_outcome"],
            "seed_protocol": list(seeds),
            "success_criteria": original_text,
        },
    )
    return {"original_text": original_text, "protocol": protocol}


def _decision_needed(context: dict, reason: str) -> dict:
    pending = context["pending_decisions"]
    if pending:
        return _plan(
            "decision_pending",
            "await_human_decision",
            f"{reason} An existing decision is already pending; the planner will not create a duplicate or infer an answer.",
            context,
            pending_decision_id=pending[0]["id"],
        )
    if context["budget_remaining"] <= 0:
        return _plan("budget_reached", "hold", reason, context)
    options = [
        {
            "label": "Modify the ResearchBrief target",
            "rationale": "A scientist may explicitly add or redefine a permitted intervention and acceptance criterion before new proposals are generated.",
            "benefits": ["Makes the next comparison explicit and reviewable"],
            "risks": ["Changes the active research target and makes older proposals stale"],
            "cost_estimate": {"experiments": 0, "training_steps": 0},
            "evidence_refs": [],
            "is_recommended": False,
            "is_modify_target_option": True,
        },
        {
            "label": "Keep the current target and stop the comparison",
            "rationale": "No authorized intervention remains in the active ResearchBrief; the project can pause without widening its scope.",
            "benefits": ["Preserves the existing scope"],
            "risks": ["Leaves the planned comparison incomplete"],
            "cost_estimate": {"experiments": 0, "training_steps": 0},
            "evidence_refs": [],
            "is_recommended": False,
            "is_modify_target_option": False,
        },
        {
            "label": "Defer and revisit after reviewing evidence",
            "rationale": "Preserve the target and make no change until a scientist has a concrete alternative.",
            "benefits": ["Avoids an ungrounded target change"],
            "risks": ["No comparative run will be proposed meanwhile"],
            "cost_estimate": {"experiments": 0, "training_steps": 0},
            "evidence_refs": [],
            "is_recommended": False,
            "is_modify_target_option": False,
        },
    ]
    return _plan(
        "decision_required",
        "create_decision_draft",
        reason,
        context,
        decision_draft={
            "question": "How should the scientist resolve the remaining ResearchBrief scope limitation?",
            "decision_type": "research_direction",
            "options": options,
        },
    )


def _priority(action: str, brief: dict) -> dict:
    profiles = {
        "establish_baseline": (0.95, 0.45, 0.70),
        "replicate_baseline": (0.95, 0.55, 0.70),
        "test_training_noise": (0.90, 0.85, 0.70),
        "replicate_training_noise": (0.95, 0.90, 0.80),
        "run_deterministic_critic": (0.95, 0.75, 0.75),
        "record_scientist_interpretation": (0.55, 0.60, 0.35),
        "review_critic_report": (0.80, 0.65, 0.65),
    }
    uncertainty, robustness, safety = profiles.get(action, (0.50, 0.50, 0.50))
    cost_action = action in {
        "establish_baseline",
        "replicate_baseline",
        "test_training_noise",
        "replicate_training_noise",
    }
    compute_cost = min(1.0, brief["training_steps_per_run"] / brief["max_training_steps_per_run"]) if cost_action else 0.0
    components = {
        "uncertainty_reduction": uncertainty,
        "robustness_value": robustness,
        "safety_value": safety,
        "compute_cost": round(compute_cost, 3),
    }
    components["score"] = round(sum(_SCORE_WEIGHTS[key] * components[key] for key in _SCORE_WEIGHTS), 3)
    components["weights"] = dict(_SCORE_WEIGHTS)
    return components


def _plan(status: str, action: str, reason: str, context: dict, **values) -> dict:
    return {
        "status": status,
        "action": action,
        "reason": reason,
        "context": context,
        **values,
    }
