from __future__ import annotations

import json


_REQUIRED_PROTOCOL_FIELDS = {"environment", "algorithm", "treatment", "control", "primary_outcome"}


def validate_hypothesis(original_text: str, protocol: dict) -> dict:
    if not isinstance(original_text, str) or not original_text.strip() or len(original_text.strip()) > 3000:
        raise ValueError("A hypothesis must be non-empty and at most 3,000 characters.")
    if not isinstance(protocol, dict) or _REQUIRED_PROTOCOL_FIELDS - set(protocol):
        missing = sorted(_REQUIRED_PROTOCOL_FIELDS - set(protocol)) if isinstance(protocol, dict) else sorted(_REQUIRED_PROTOCOL_FIELDS)
        raise ValueError("The protocol is missing required fields: " + ", ".join(missing) + ".")
    if protocol["environment"] != "Reacher-v5" or protocol["algorithm"] != "PPO":
        raise ValueError("This MVP supports only Reacher-v5 with PPO.")
    for field in ("treatment", "control", "primary_outcome"):
        value = protocol[field]
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 1000:
            raise ValueError(f"Protocol {field.replace('_', ' ')} must be non-empty and at most 1,000 characters.")
    if "seed_protocol" in protocol:
        seeds = protocol["seed_protocol"]
        if not isinstance(seeds, list) or not seeds or any(isinstance(seed, bool) or not isinstance(seed, int) or seed < 0 for seed in seeds):
            raise ValueError("Seed protocol must be a non-empty list of non-negative integers.")
        if len(set(seeds)) != len(seeds):
            raise ValueError("Seed protocol cannot contain duplicate seeds.")
    try:
        json.dumps(protocol, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("Operationalized protocol must contain ordinary JSON values.") from error
    return {**protocol, **{field: protocol[field].strip() for field in ("treatment", "control", "primary_outcome")}}
