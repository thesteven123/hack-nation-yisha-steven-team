# Complete Idea group implementation contract

This revision replaces the supplied-material-only prototype. The Literature
Agent and Idea Agent are visible, service-owned roles. They execute real native
provider jobs and exchange versioned evidence packets; they are not labels for
fabricated conversations. Review is an independent task context of the group.

Required acceptance: a goal without pasted papers triggers actual discovery,
retrieval and reading; query/source/activity receipts are inspectable; gaps
trigger materially different follow-up searches or an explicit bounded unresolved
state; grounded options, source/inference distinction, human choices and exact
follow-up are retained across restarts. Existing records remain readable and are
labelled as unsearched legacy results. Installed application and service remain
untouched. No experiment execution or publishing is part of this group.

## Shared backend callbacks

`discover(brief, context, on_event)` returns `{papers, searches, summary, gaps,
usage, provider}`. Candidate papers are `{title, url, reason}`. Native search/open
receipts establish discovery activity; generated URLs alone are not evidence.
Discovery is a bounded Literature Agent native job with only web tools enabled.

`retrieve(candidates, brief, on_event)` returns `{sources, papers, coverage_gaps,
usage}`. Each source has existing `{id,title,uri,text}` fields plus optional
provenance. Each paper receipt is `{id,title,url,retrieved_at,content_hash,
parser_version,access,coverage,cache_hit,status,error}`. `access` distinguishes
full_text, abstract, partial, unavailable. `status` is read or unavailable.
Only actually retrieved text enters exact quotation validation. The source
library caches full parsed content by source version and parser configuration;
goal-specific bounded projections declare omitted coverage.
New downloads also retain original public response bytes with per-fetch
`raw_document` references; old records without such a reference remain explicitly
not retained. [Original source archive](ORIGINAL_SOURCE_ARCHIVE.md) defines the
bounded storage and exact session/packet-scoped native Save As endpoint.

`generate(stage, brief, previous, on_event)` retains the structured stage
envelope `{output,usage,provider}`. Stages remain literature, ideas and review.
Prompts include bounded prior rounds, actual feedback, evidence and requests.
Native thread/turn IDs and task start/completion appear in activity receipts.
No external action is authorized by a worker result or human direction choice.

Events sent to `on_event` are bounded JSON `{agent,type,summary,query?,url?,
source_id?,thread_id?,turn_id?}`. Agents are literature or idea. The controller
assigns event IDs/times and current generation/round. Activity is real work,
not generated thinking. Review events identify their separate task context.

## Envelope extensions (old fields retained)

`research` contains:

- `version:2`, `round`, `max_rounds` (default 3), `readiness`, `stop_reason`.
- `readiness`: not_started, researching, ready_for_choice, evidence_limited,
  needs_input, failed, legacy_unsearched.
- `limits:{max_rounds,max_model_calls,max_sources,max_search_queries}`;
  `used:{model_calls,searches,reads,cache_hits}`.
- `papers`, `searches`, `activities`, `rounds`, `questions`.
- `agents:[{id,name,role,status,task,thread_id?,turn_id?}]` for Literature/Idea.
- `questions:[{id,question,why,required,options}]`, maximum three per checkpoint.
- `rounds` retains prior literature/idea/review outputs, coverage and disposition.
- `input_packets:[{round,stage,digest,sources:[{id,packet_hash,characters,coverage}]}]`
  identifies the immutable input projection frozen before each literature job.
  Text has a shared 100 KiB UTF-8 budget: small packets remain whole and larger
  supplied/retrieved packets divide remaining capacity fairly. Clipped coverage
  declares actual source spans and `model_projection` offsets; saved larger
  packets remain available. The native stage prompt limit remains 200 KiB.
- `validation_attempts` retains bounded attempt summaries, immutable input
  references, canonical structured-output hashes, usage references and specific
  rejection diagnostics. Raw rejected output is excluded from the default
  session/history response. `pending_repair` identifies one authorized local
  correction of a completed Literature extraction, or is null.

Session phase adds `searching` and `reading`; status adds `needs_input` without
equating missing evidence with provider failure. A completed run still needs an
explicit readiness classification. `ready_for_choice` requires actual searched,
read sources and evidence, plus reviewer acceptance; it never means proven
scientific conclusions. No new model runs occur on reads/startup.

`GET /api/research/ideas/{id}/papers/{source_id}?source_hash={sha256}` resolves
the exact UTF-8 text hash recorded on an EvidenceCard, including immutable role
packets from previous generations. It returns `{source,paper,packet_hash}` plus
`generation_id`, `round`, `revision` and `archived` where applicable. Omitting
the optional hash retrieves the saved reader/supplied packet. An unknown hash
returns 404 and a malformed hash returns 400; lookups are scoped to the session
and use no filesystem paths. `source_hash` hashes packet text, whereas source
provenance `content_hash` identifies the reader's original downloaded bytes.

## Exact evidence rejection and bounded correction

Evidence validation separately identifies `unknown_source_id`,
`quote_not_exact`, `duplicate_evidence_id` and `quote_crosses_omission`.
Whitespace normalization, PDF dehyphenation and fuzzy similarity never establish
an exact quote. Diagnostics contain bounded card indices/IDs, hashes, lengths,
allowed source IDs and failure codes; they do not put rejected quotation bodies
in ordinary activity messages or session errors.

Completed structured responses are stored as local `stage_attempts` artifacts,
up to 128 KiB per canonical JSON output. Larger or invalid JSON responses retain
available hashes/lengths and an explicit missing-artifact reason instead of an
unbounded body. Diagnostics are capped at 16 KiB. Input references identify the
frozen source/context packet, generation, round, brief revision and preceding
result. Accepted and rejected attempts remain distinct and retain provider usage.
`GET /{id}/attempts/{attempt_id}` is an explicit native-admin, session-scoped
inspection route; it returns the original structured output and diagnostic
record. It is not part of the default user interface or log stream.

For a completed Literature output rejected by those mechanical evidence checks,
the controller may re-extract once per frozen Literature packet. It keeps the
same brief, sources, context and original resolved native model selector, adds the
recorded diagnostics, and reserves an additional job from the existing global
twelve-job limit before dispatch. The rejected answer does not enter the Idea
Agent's evidence. A second rejection ends as failed; inability to reserve the
correction ends as evidence limited. A corrected output must pass the unchanged
exact checks. New accepted upstream output clears dependent current ideas/review;
previous rounds remain in history and cannot masquerade as the new review.

The native `thread/start` response supplies the resolved configured selector.
Provider records distinguish `requested_model`, `resolved_model` and
`model_resolution`; this is not per-turn execution telemetry or an immutable
weights version. A repair pins that recorded resolved selector and checks the
new thread's resolution before starting a turn. Missing resolution (including
an unresolved default alias) or a different new resolution stops the repair;
the system never silently re-resolves a default and calls it the same model.

Provider errors, timeout/unknown outcomes and interrupted requests do not trigger
this correction. Cancellation and generation ownership are checked before the
correction and before publishing. Restart interrupts pending work and does not
automatically resume a correction. Older failures without stored response
artifacts remain explicitly undiagnosable at card level; their output is not
reconstructed or overwritten.

Review adds `disposition:ready|retrieve_more|revise_ideas|needs_input`,
`followup_queries:string[]`, and `questions` above. Maximum three rounds and
twelve native jobs per explicit run. A failed search is not negative evidence;
zero usable evidence requires changed queries in a subsequent available round.
Budget exhaustion leaves the unresolved reason and allows explicit continuation.

## Human continuation

`POST /{id}/followup` body:
`{expected_revision,idempotency_key,feedback,mode,goal?,constraints?,selected_ids?}`.
Mode is research, refine or selected. It records the exact user response and
option/evidence/brief versions, preserves previous results, and makes a draft.
The UI then explicitly calls generate from that returned draft as part of the
user's Continue action. A lost/repeated acknowledgement cannot duplicate dispatch.
Feedback is passed to both agents; it does not silently replace the goal.

Existing select/revise/defer remain valid. Add reject and combine with explicit
selected IDs and resulting goal; preserve exact human text and do not invent
their rationale. Expose success criteria/preferences, evidence versions and
next actions through the brief/decision card using backwards-compatible fields.

## Ownership

Literature extraction optionally reuses previously accepted, exactly scoped
EvidenceCards through [the accepted-evidence cache](LITERATURE_EVIDENCE_CACHE.md).
Every receiving generation still freezes its packet and validates/persists the
stage. Reuse receipts distinguish hit/join/miss/bypass; only hit/join avoid one
extraction turn, while native preflight and other group work remain separate.

- Core: store/schema/routes/controller, schemas, budgets and readiness tests.
- Provider: native discovery + generation, actual activity/identity, isolation.
- Reader: actual public-source HTTP/PDF parsing, source cache and provenance.
- Client: visible members, work/handoff log, library/evidence inspection,
  decision card and explicit follow-up; old results clearly labelled unsearched.
