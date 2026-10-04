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
- Deterministic Coordinator/Planner using the ResearchBrief, run history, active proposals, pending decisions, and critic reports. It plans a baseline, matched replications, and only the smallest permitted noise intervention; suggestions receive transparent weighted priority scores and validated configs. Saving a suggestion creates only a draft proposal and draft hypothesis; proposal approval remains blocked until the scientist separately reviews the hypothesis. Pending decisions do not block independent baseline work, and the Planner never runs training.
- Optional snapshot-grounded LLM synthesis through an OpenAI-compatible JSON endpoint. The adapter has no tools; the service validates strict config, active-brief limits, matched control, seeds, thresholds, and snapshot citations. Saving atomically creates only an unapproved proposal and draft hypothesis, both linked to the selected immutable snapshot.
- Lab Director summaries grounded in stored exact metrics, distinct condition seeds, critic verdicts, separate scientist interpretations, and currently valid proposal approvals. Optional ElevenLabs MP3 generation requires reviewed text and explicit consent; stale state is rejected, audio/text manifests are hashed and audited, and generated audio is ignored by Git. No narration grants execution permission.
- Installation-wide Settings UI for tokens, endpoints, models, voice IDs, and Moss project/index settings. Tokens are never prefilled; saved overrides apply to new requests immediately without mutating process environment variables. Owner-only local storage is atomic, unencrypted, and ignored by Git; disable and restore actions are explicit. Remote endpoints require HTTPS, and HTTP redirects are blocked for credential-bearing LLM, Firecrawl, and ElevenLabs calls. Saving settings has no provider calls or research-authorization side effects.
- Focused tests for validation, wrappers, repository approval, critic grouping, upload preservation/review, decision scope, interpretation history, target-version authorization, evidence capture, immutable snapshots, mocked Moss retrieval, and planner decision paths.

## Next slices — do not bundle into one change

1. **Presentation polish:** improve dashboard navigation and the hackathon demo flow without changing authorization or research semantics.

For each slice: read the matching design-spec sections, change only the necessary module(s), add focused tests, and verify before starting the next slice. Firecrawl works without a key for initial use; Moss requires the optional `retrieval` extra plus `MOSS_PROJECT_ID` and `MOSS_PROJECT_KEY`; LLM synthesis requires `SCIENTESIS_LLM_API_KEY` and `SCIENTESIS_LLM_MODEL` only when used; ElevenLabs audio requires `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID` only when used.
