# Implementation status

This is a modular hackathon starter, not a full implementation of every agent and integration in the design spec. Use one slice at a time and retain the human approval boundary.

## Implemented

- Phase 0 foundation: ResearchBrief seed, SQLite schema/repository, experiment config model, audit events, proposal validation.
- A local Streamlit flow to propose, explicitly approve, and separately start an experiment.
- A seeded PPO/Reacher-v5 runner with training/evaluation noise, actuator-saturation penalty/metric, version capture, and model/log artifacts.
- Deterministic, threshold-based critic with matched-baseline and three-distinct-seed checks.
- Human-reviewed CSV/JSON/Markdown intake: schema/profile warnings, optional numeric ranges, exact-byte storage, SHA-256, provenance, and audit events. Uploads are never silently cleaned or sent externally.
- Decision cards with two-to-four explicit alternatives, risks/cost/evidence fields, scoped human answers, and deferral that leaves the record pending. Answers do not silently edit the brief or authorize experiments.
- Scientist interpretation records stored separately from deterministic critic verdicts; each is audit logged and can be revised without overwriting history.
- Versioned ResearchBrief editing, structured hypotheses, explicit protocol review, and links from approved hypotheses to proposals. Brief edits make existing proposals stale and invalidate old hypothesis links.
- Public-page Firecrawl adapter with URL/SSRF guardrails, source-quality metadata, immutable captured Markdown, hash verification, a pending review queue, and explicit human approve/reject actions. Captured sources are labeled as external prior evidence for method design only.
- Focused tests for validation, wrappers, repository approval, critic grouping, upload preservation/review, decision scope, interpretation history, target-version authorization, and mocked evidence capture.

## Next slices — do not bundle into one change

1. **Evidence snapshots and retrieval:** persist the exact evidence/run/critic IDs and ResearchBrief version behind each recommendation; add Moss indexing/search behind a separate adapter. Add Bright Data only if source discovery or fallback is needed.
2. **Agent layer:** start with constrained research/planning outputs validated by deterministic services. Agents must not execute shell commands or self-authorize experiments.
3. **Narration and presentation:** optional ElevenLabs narration of validated results, then polish the dashboard and demo flow.

For each slice: read the matching design-spec sections, change only the necessary module(s), add focused tests, and verify before starting the next slice. Firecrawl is the only external adapter wired into this slice; its API key is optional for initial use.
