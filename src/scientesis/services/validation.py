from copy import deepcopy


class ProtocolValidationError(ValueError):
    pass


def validate_config(config: dict, brief: dict) -> None:
    required = {
        "environment",
        "algorithm",
        "noise_train",
        "noise_eval",
        "safety_penalty",
        "seed",
        "training_steps",
        "evaluation_episodes",
        "success_distance_threshold",
        "actuator_saturation_threshold",
        "brief_version",
    }
    missing = sorted(required - set(config))
    if missing:
        raise ProtocolValidationError(f"Experiment config is missing fields: {', '.join(missing)}")
    if config["environment"] != brief["environment"]:
        raise ProtocolValidationError("Environment differs from the active ResearchBrief.")
    if config["algorithm"] != brief["algorithm"]:
        raise ProtocolValidationError("Algorithm differs from the active ResearchBrief.")
    if config["noise_train"] not in brief["allowed_noise_values"]:
        raise ProtocolValidationError("Training observation noise is outside the allowed values.")
    if config["noise_eval"] not in brief["allowed_noise_values"]:
        raise ProtocolValidationError("Evaluation observation noise is outside the allowed values.")
    if config["safety_penalty"] not in brief["allowed_safety_penalties"]:
        raise ProtocolValidationError("Safety penalty is outside the allowed values.")
    if config["seed"] not in brief["training_seeds"]:
        raise ProtocolValidationError("Seed is outside the approved replication set.")
    if config["training_steps"] != brief["training_steps_per_run"]:
        raise ProtocolValidationError("Training steps must match the fixed budget in the ResearchBrief.")
    if config["evaluation_episodes"] != brief["evaluation_episodes"]:
        raise ProtocolValidationError("Evaluation episode count must match the ResearchBrief.")
    if config["success_distance_threshold"] != brief["success_distance_threshold"]:
        raise ProtocolValidationError("Success threshold differs from the ResearchBrief.")
    if config["actuator_saturation_threshold"] != brief["actuator_saturation_threshold"]:
        raise ProtocolValidationError("Safety threshold differs from the ResearchBrief.")
    if config["brief_version"] != brief["version"]:
        raise ProtocolValidationError("Experiment references a stale ResearchBrief version.")


def validate_execution_authorization(proposal: dict, brief: dict) -> None:
    if proposal["status"] not in {"approved", "queued"}:
        raise ProtocolValidationError("Experiment does not have explicit human approval.")
    if proposal["research_brief_version"] != brief["version"]:
        raise ProtocolValidationError("ResearchBrief changed; proposal requires revalidation and approval.")
    config = proposal["config"]
    scope = proposal.get("approval_scope")
    if not isinstance(scope, dict) or scope != config:
        raise ProtocolValidationError("Approval scope does not exactly cover this experiment configuration.")
    validate_config(deepcopy(config), brief)
