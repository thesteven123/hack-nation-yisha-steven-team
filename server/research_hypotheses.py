"""Bounded human-authored hypothesis records, never scientific adjudication.

Pure validation/freezing helpers. The Lab owns transactions, identities, time,
artifact storage, authorization and input visibility; clients own none of these.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re

MAX_SET_BYTES = 64 * 1024
MAX_FROZEN_BYTES = 512 * 1024


class HypothesisError(ValueError):
    pass


def _encoded(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise HypothesisError("Hypothesis records require finite UTF-8 JSON") from exc


def _object(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise HypothesisError("Hypothesis fields do not match the declared schema")
    return value


def _text(value, limit):
    try:
        valid = isinstance(value, str) and bool(value.strip()) and len(value.encode("utf-8")) <= limit
    except UnicodeError:
        valid = False
    if not valid:
        raise HypothesisError("Hypothesis text must be nonempty and within its UTF-8 byte bound")
    return value


def _texts(value, limit, maximum, minimum=0):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise HypothesisError("Hypothesis text lists exceed their declared count bounds")
    return [_text(item, limit) for item in value]


def _selector(value):
    if not isinstance(value, dict):
        raise HypothesisError("Evidence must select an existing input, source or observation")
    kind = value.get("kind")
    if kind == "input":
        _object(value, {"kind"})
        return {"kind": kind}
    if kind in ("source", "observation"):
        field = "source_id" if kind == "source" else "run_id"
        _object(value, {"kind", field})
        return {"kind": kind, field: _text(value[field], 128)}
    raise HypothesisError("Unsupported hypothesis evidence selector")


def _selectors(value):
    if not isinstance(value, list) or len(value) > 8:
        raise HypothesisError("At most eight supporting or opposing evidence references are allowed")
    result = [_selector(item) for item in value]
    if len({_encoded(item) for item in result}) != len(result):
        raise HypothesisError("Duplicate hypothesis evidence selectors are not allowed")
    return result


def validate_set(value):
    """Validate client-authored fields only; null is an explicit exploratory mode."""
    if value is None:
        return None
    if len(_encoded(value)) > MAX_SET_BYTES:
        raise HypothesisError("Hypothesis set exceeds its 64 KiB JSON byte limit")
    _object(value, {"candidates", "open_alternative"})
    if not isinstance(value["candidates"], list) or not 1 <= len(value["candidates"]) <= 4:
        raise HypothesisError("A hypothesis set requires one to four candidate explanations")
    candidates = []
    for item in value["candidates"]:
        _object(item, {"id", "statement", "assumptions", "scope", "supporting_evidence", "opposing_evidence", "predictions", "weakening_conditions"})
        identifier = _text(item["id"], 64)
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", identifier) is None:
            raise HypothesisError("Hypothesis IDs must contain bounded letters, digits, dots, dashes or underscores")
        candidates.append({"id": identifier, "statement": _text(item["statement"], 4000),
            "assumptions": _texts(item["assumptions"], 1000, 8), "scope": _text(item["scope"], 2000),
            "supporting_evidence": _selectors(item["supporting_evidence"]),
            "opposing_evidence": _selectors(item["opposing_evidence"]),
            "predictions": _texts(item["predictions"], 1000, 6, 1),
            "weakening_conditions": _texts(item["weakening_conditions"], 1000, 6, 1)})
    if len({item["id"] for item in candidates}) != len(candidates):
        raise HypothesisError("Hypothesis IDs must be unique within a set")
    return {"candidates": candidates, "open_alternative": _text(value["open_alternative"], 2000)}


def visible_evidence(*, input_artifact, inputs, rounds):
    """References to content already owned by this campaign, not inferred support."""
    result = [{"kind": "input", "artifact": input_artifact}]
    for source in inputs.get("sources", []):
        result.append({"kind": "source", "source_id": source["id"], "artifact": input_artifact,
            "source_hash": hashlib.sha256(source["text"].encode("utf-8")).hexdigest(),
            "coverage": source["coverage"], "missing_sections": copy.deepcopy(source["missing_sections"])})
    for item in rounds:
        result.append({"kind": "observation", "run_id": item["run"]["id"], "artifact": item["run"]["observation_artifact"],
            "input_artifact": item["run"]["input_artifact"], "brief_revision": item["plan"]["goal_revision"],
            "run_status": item["run"]["status"], "qc_passed": item["qc"]["passed"]})
    return result


def freeze_set(value, *, campaign_id, revision, brief_revision, introduced_at,
               input_artifact, inputs, rounds, previous_artifact=None):
    """Attach service-supplied identity and the exact visibility at introduction.

Supporting/opposing classifications are human declarations. Referent existence
does not verify that it supports the proposed scientific explanation.
"""
    requested = validate_set(value)
    if requested is None:
        return None
    references = visible_evidence(input_artifact=input_artifact, inputs=inputs, rounds=rounds)
    lookup = {_encoded({k: ref[k] for k in ("kind", "source_id", "run_id") if k in ref}): ref for ref in references}
    candidates = copy.deepcopy(requested["candidates"])
    for candidate in candidates:
        for field in ("supporting_evidence", "opposing_evidence"):
            selected = []
            for selector in candidate[field]:
                reference = lookup.get(_encoded(selector))
                if reference is None:
                    raise HypothesisError("Hypothesis evidence must name a source or observation already visible in this campaign")
                selected.append(copy.deepcopy(reference))
            candidate[field] = selected
    record = {"id": campaign_id + ":hypotheses", "revision": revision, "brief_revision": brief_revision,
        "introduced_at": introduced_at, "origin": "human", "post_outcome": bool(rounds),
        "previous_artifact": previous_artifact, "input_artifact": input_artifact,
        "candidates": candidates, "open_alternative": requested["open_alternative"],
        "visible_evidence_refs": references, "scientific_status": "not_validated",
        "evidence_scope": "Supporting and opposing links are human-declared interpretations; existing references and QC are not proof of a hypothesis"}
    if len(_encoded(record)) > MAX_FROZEN_BYTES:
        raise HypothesisError("Resolved hypothesis evidence exceeds its 512 KiB record limit; narrow the explicitly selected evidence")
    return record
