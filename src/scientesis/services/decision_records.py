from __future__ import annotations

import math


_ALLOWED_COST_FIELDS = {"experiments", "training_steps"}


def validate_decision_draft(question: str, options: list[dict]) -> list[dict]:
    if not isinstance(question, str) or not question.strip():
        raise ValueError("Enter a concrete decision question.")
    if len(question.strip()) > 1000:
        raise ValueError("Keep the decision question under 1,000 characters.")
    if not isinstance(options, list) or not 2 <= len(options) <= 4:
        raise ValueError("A decision card must contain two to four alternatives.")

    normalized = []
    labels = set()
    recommended_count = 0
    modify_target_count = 0
    for index, option in enumerate(options, start=1):
        if not isinstance(option, dict):
            raise ValueError(f"Alternative {index} must be an object.")
        label = _required_text(option.get("label"), f"Alternative {index} needs a label.", 200)
        rationale = _required_text(option.get("rationale"), f"Alternative {index} needs a rationale.", 3000)
        normalized_label = label.casefold()
        if normalized_label in labels:
            raise ValueError("Alternative labels must be unique.")
        labels.add(normalized_label)
        benefits = _text_list(option.get("benefits", []), f"Benefits for alternative {index}")
        risks = _text_list(option.get("risks", []), f"Risks for alternative {index}")
        evidence_refs = _text_list(option.get("evidence_refs", []), f"Evidence references for alternative {index}")
        cost = option.get("cost_estimate", {})
        if not isinstance(cost, dict) or set(cost) - _ALLOWED_COST_FIELDS:
            raise ValueError("Cost estimates may contain only experiments and training_steps.")
        normalized_cost = {}
        for field, value in cost.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"Cost estimate {field!r} must be a finite non-negative number.")
            normalized_cost[field] = int(value) if isinstance(value, int) or value.is_integer() else float(value)
        recommended = option.get("is_recommended", False)
        modify_target = option.get("is_modify_target_option", False)
        if not isinstance(recommended, bool) or not isinstance(modify_target, bool):
            raise ValueError("Recommendation and modify-target flags must be true or false.")
        recommended_count += int(recommended)
        modify_target_count += int(modify_target)
        normalized.append(
            {
                "label": label,
                "rationale": rationale,
                "benefits": benefits,
                "risks": risks,
                "cost_estimate": normalized_cost,
                "evidence_refs": evidence_refs,
                "is_recommended": recommended,
                "is_modify_target_option": modify_target,
            }
        )
    if recommended_count > 1:
        raise ValueError("Mark at most one alternative as recommended.")
    if modify_target_count != 1:
        raise ValueError("Include exactly one 'modify research target' alternative.")
    return normalized


def _required_text(value, message: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(message)
    result = value.strip()
    if len(result) > limit:
        raise ValueError(f"Text must be no longer than {limit} characters.")
    return result


def _text_list(value, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError(f"{label} must be a list of at most 20 items.")
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item.strip()) > 500:
            raise ValueError(f"{label} must contain non-empty text items of at most 500 characters.")
        result.append(item.strip())
    return result
