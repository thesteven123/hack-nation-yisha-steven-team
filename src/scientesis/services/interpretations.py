from __future__ import annotations


ALLOWED_HUMAN_VERDICTS = {"accepted_evidence", "preliminary", "inconclusive", "rejected"}


def validate_human_interpretation(verdict: str, rationale: str | None = None) -> dict:
    if verdict not in ALLOWED_HUMAN_VERDICTS:
        raise ValueError("Choose accepted evidence, preliminary, inconclusive, or rejected.")
    if rationale is not None and not isinstance(rationale, str):
        raise ValueError("The rationale must be text.")
    normalized_rationale = rationale.strip() if rationale else ""
    if len(normalized_rationale) > 5000:
        raise ValueError("Keep the human rationale under 5,000 characters.")
    return {"verdict": verdict, "rationale": normalized_rationale or None}
