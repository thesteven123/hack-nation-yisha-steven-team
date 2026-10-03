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
- Immutable evidence snapshots recording the selected Moss document, approved evidence, finished experiment, and critic-report IDs plus the active ResearchBrief version. Snapshot rows are append-only and can be linked to proposals or decision cards only within the same project and brief version.
- Isolated optional Moss adapter for explicit corpus sync and project-scoped semantic/hybrid search. The curated corpus excludes raw uploaded datasets; syncing and querying are user-triggered, and retrieved IDs can be frozen into snapshots.
- Focused tests for validation, wrappers, repository approval, critic grouping, upload preservation/review, decision scope, interpretation history, target-version authorization, evidence capture, immutable snapshots, and mocked Moss retrieval.

## Next slices — do not bundle into one change

1. **Agent layer:** start with constrained research/planning outputs validated by deterministic services. Agents must not execute shell commands or self-authorize experiments.
2. **Narration and presentation:** optional ElevenLabs narration of validated results, then polish the dashboard and demo flow.

For each slice: read the matching design-spec sections, change only the necessary module(s), add focused tests, and verify before starting the next slice. Firecrawl works without a key for initial use; Moss requires the optional `retrieval` extra plus `MOSS_PROJECT_ID` and `MOSS_PROJECT_KEY`.
