from __future__ import annotations

import math


TEXT_LIMITS = {
    "project_title": 160,
    "research_question": 3000,
    "objective": 3000,
    "primary_outcome": 500,
    "safety_metric_definition": 1500,
    "notes": 5000,
}


def validate_research_brief(current: dict, candidate: dict, runs_used: int = 0) -> dict:
    if not isinstance(candidate, dict) or set(candidate) != set(current):
        raise ValueError("Edit the existing ResearchBrief fields; fields cannot be added or removed in this MVP.")
    if candidate.get("environment") != "Reacher-v5" or candidate.get("algorithm") != "PPO":
        raise ValueError("This MVP is fixed to Gymnasium Reacher-v5 with PPO.")

    normalized = dict(candidate)
    for field, limit in TEXT_LIMITS.items():
        value = candidate.get(field)
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
            raise ValueError(f"{field.replace('_', ' ').title()} must be non-empty and at most {limit} characters.")
        normalized[field] = value.strip()

    normalized["secondary_outcomes"] = _unique_text_list(candidate.get("secondary_outcomes"), "Secondary outcomes", 12)
    normalized["training_seeds"] = _unique_int_list(candidate.get("training_seeds"), "Training seeds", 0, 100000)

    minimum_seeds = _integer(candidate.get("minimum_seeds_for_claim"), "Minimum seeds", 1, 30)
    if minimum_seeds > len(normalized["training_seeds"]):
        raise ValueError("The claim threshold cannot exceed the number of configured training seeds.")
    normalized["minimum_seeds_for_claim"] = minimum_seeds

    normalized["allowed_noise_values"] = _unique_number_list(candidate.get("allowed_noise_values"), "Allowed noise values", 0.0, 1.0)
    normalized["allowed_safety_penalties"] = _unique_number_list(candidate.get("allowed_safety_penalties"), "Allowed safety penalties", 0.0, 100.0)
    if 0.0 not in normalized["allowed_noise_values"] or 0.0 not in normalized["allowed_safety_penalties"]:
        raise ValueError("Keep a zero-noise baseline and a zero-penalty baseline available.")

    normalized["experiment_budget"] = _integer(candidate.get("experiment_budget"), "Experiment budget", 1, 100)
    if normalized["experiment_budget"] < runs_used:
        raise ValueError("Experiment budget cannot be lower than the number of runs already reserved.")
    normalized["max_training_steps_per_run"] = _integer(candidate.get("max_training_steps_per_run"), "Maximum training steps", 1000, 1000000)
    normalized["training_steps_per_run"] = _integer(candidate.get("training_steps_per_run"), "Training steps per run", 1000, normalized["max_training_steps_per_run"])
    normalized["evaluation_episodes"] = _integer(candidate.get("evaluation_episodes"), "Evaluation episodes", 1, 1000)

    for field, low, high in (
        ("success_distance_threshold", 0.001, 2.0),
        ("minimum_noisy_success_improvement", 0.0, 1.0),
        ("max_clean_success_decline", 0.0, 1.0),
        ("actuator_saturation_threshold", 0.0, 1.0),
        ("safety_regression_tolerance", 0.0, 1.0),
    ):
        normalized[field] = _number(candidate.get(field), field.replace("_", " ").title(), low, high)

    normalized["version"] = current["version"]
    if normalized == current:
        raise ValueError("No ResearchBrief changes to save.")
    return normalized


def _integer(value, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer from {minimum} to {maximum}.")
    return value


def _number(value, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be a finite number from {minimum:g} to {maximum:g}.")
    return float(value)


def _unique_number_list(values, label: str, minimum: float, maximum: float) -> list[float]:
    if not isinstance(values, list) or not 1 <= len(values) <= 20:
        raise ValueError(f"{label} must contain 1–20 values.")
    normalized = [_number(value, label, minimum, maximum) for value in values]
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} cannot contain duplicates.")
    return sorted(normalized)


def _unique_int_list(values, label: str, minimum: int, maximum: int) -> list[int]:
    if not isinstance(values, list) or not 1 <= len(values) <= 30:
        raise ValueError(f"{label} must contain 1–30 values.")
    normalized = [_integer(value, label, minimum, maximum) for value in values]
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} cannot contain duplicates.")
    return normalized


def _unique_text_list(values, label: str, maximum: int) -> list[str]:
    if not isinstance(values, list) or len(values) > maximum:
        raise ValueError(f"{label} must contain at most {maximum} items.")
    normalized = []
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 500:
            raise ValueError(f"{label} must contain non-empty items of at most 500 characters.")
        normalized.append(value.strip())
    if len(set(item.casefold() for item in normalized)) != len(normalized):
        raise ValueError(f"{label} cannot contain duplicates.")
    return normalized
