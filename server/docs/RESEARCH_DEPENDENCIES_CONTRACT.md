# Explicit source corrections and local research dependencies

Version: `research-dependencies/1`, schema 1. This module supplies the service
bridge. Its existence alone does **not** complete protocol L7: the authenticated
UI declaration, admission checks, recovery, export presentation, and actual
integration acceptance are separate work.

## Authority and scientific meaning

`ResearchDependencies(root, idea_root, lab_root, namespace="native-local")`
operates within one local native administrator's storage. Namespaces isolate
service indexes; they are not a multi-user permission system. The original Idea
and Research Lab databases are opened read-only by this module. A correction
marks dependent material for revalidation; it does not establish that the old
claim is false or the new claim is true. It grants no model, shell, network,
experiment, or additional budget authority.

Only service code supplies persisted record identities. Renderer URLs, hashes,
and provenance declarations cannot create shared source identity. Shared lineage
requires the exact saved Idea choice revision, generation, selected direction's
EvidenceCards, frozen source hash, and exact quote span. All selected cards for
the imported packet are indexed, including cards omitted from its representative
provenance field. A new source version, matching URL, or equal text alone is not
a correction. Imports predating authenticated `decision_revision` remain local
and readable; the service does not backfill authority or modify their records.

## Public service interface

- `register_campaign(campaign_id)` reconciles the current CAS input and bounded
  historical CAS inputs into the service index, including edits before this
  index existed. Changed user text is unverified.
  A metadata-only edit or renamed source with the same previously indexed bytes
  retains the old dependency. This method does not execute research.
- `record_idea_correction(session_id, old_revision, old_evidence_id,
  new_revision, new_evidence_id, *, idempotency_key, reason)` records an explicit
  service declaration referring to real immutable EvidenceCards. Changed source
  bytes affect consumers of that source version; interpretation-only corrections
  affect consumers of the specified EvidenceCard. The same request key replays
  its event; different request content conflicts.
- `record_lab_source_correction(campaign_id, old_revision, new_revision,
  source_id, *, idempotency_key, reason)` verifies adjacent committed CAS versions
  and their actual `inputs_corrected` event. Verified old Idea lineage plus
  changed text and core-created `user_corrected.derived_from` permits a shared
  source-version event. Otherwise the event is local to that old campaign input.
  This low-level method is for explicit historical declarations or recovery;
  use the intent wrapper for a new UI correction.
- `apply_lab_source_correction(campaign_id, request, source_id,
  *, correct_inputs=store.correct_inputs)` uses the exact core request shape
  `{expected_revision, idempotency_key, inputs, reason}` and returns the original
  core campaign response. HTTP-only `shared_source_id`, if used, is stripped by
  the authorized service before this call. Exactly the named source text must
  change and the source-ID set must remain unchanged. The callback is the trusted
  synchronous local core method, never a renderer-provided function.
- `prepare_lab_correction(campaign_id, request, source_id)` persists the wrapper's
  intent without executing it. `recover_lab_corrections(limit=50)` reads actual
  core mutation receipts and finishes matching events. Missing receipts stay
  pending; recovery never reruns a request. Results contain `items`,
  `pending_intents`, `has_more`, and `execution_performed:false`. `has_more` can
  mean waiting for the user's exact request retry, so do not busy-loop on it.
  Bounded recovery rotates its scan so an older unresolved intent cannot hide a
  later committed one. A conflicting receipt remains visible as attention needed.
- `drain(limit=50)` delivers event/input marks and returns `processed_targets`,
  `pending_events`, and `has_more`. Limits are 1–100. Delivery can be resumed after
  failure; duplicate publication verifies and reuses its existing receipt.
- `snapshot(campaign_id)` and `decorate(campaign_snapshot)` are read-only.
  `decorate` verifies that the supplied revision is still current, copies it,
  adds `dependency_status`, and overlays affected claim status with
  `needs_revalidation`. Persisted claims, CAS artifacts, observations, source
  support, inference validity, and budgets are unchanged.
- `admission(campaign_id)` performs a read-only check and raises
  `LabError("dependency_stale", ...)` if unregistered or blocked. It is not an
  atomic execution reservation.
- `with run_guard(campaign_id): ...` reconciles lineage and holds the correction
  producer write lock over a bounded local reservation or commit. Admission
  includes committed corrections not yet delivered and unresolved intents, so
  there is no outbox-delivery gap. Lock order is producer → core/model DB; never
  acquire this guard from an already-held core/model write transaction. Never
  hold it across model or external network work. Native jobs need checks at
  preparation, start, and result publication/application. An obsolete output may
  be retained as a historical receipt but cannot authorize a current action.
- `replay_lab_mutation(campaign_id, event, request)` uses the actual core replay
  implementation read-only and returns a saved response or `None`. Allowed
  internal event names are `action_executed`, `next_plan_frozen`,
  `inputs_corrected`, and `human_decision`. Routes check pure replay before new
  admission; a later correction cannot rerun or erase an old successful request.
- `reconcile_campaign(campaign_id)` explicitly registers metadata, recovers up
  to 50 existing correction intents, and drains up to 50 delivery targets. It
  returns `{campaign_id, dependency_status, recovery, delivery,
  execution_performed:false}`. An authorized UI can expose this as “Sync source
  status”; GET does not silently reconcile. Recovery/delivery share this local
  native namespace and can process its related queued work.

## Durable boundaries and recovery

The producer stores correction declarations and their outbox rows in one SQLite
transaction. The separate consumer stores one stable receipt per namespace,
event, campaign and input. Consumer commit precedes producer acknowledgement;
a failure between them replays without duplicate marks. Late campaign
registration reschedules existing events, which remain safe to redeliver.

A new shared Lab correction first persists its intent and request fingerprint,
then calls the existing core transaction, then verifies its real mutation receipt
and publishes the correction event. A process exit before core commit leaves a
pending intent and blocks affected old inputs. A process exit after commit is
recovered using the original response; no execution is repeated. A process exit
after event creation but before intent acknowledgement reuses the same stable
event. A known core validation failure with no receipt rejects its intent;
unknown failures remain pending. This is a recoverable cross-database protocol,
not a claim of one atomic transaction across the stores.

The two sidecar databases are a matched pair, private on supported filesystems.
Missing either one fails closed; read paths do not recreate empty state.
Symlinked storage paths are rejected. Restoring a filesystem snapshot must retain
the pair and the corresponding core/Idea stores. Initial setup interrupted
between database creation requires explicit repair; it is not silently reset.

## Read model and limits

`dependency_status` always includes `namespace`, `scope`, `registered`, `state`,
`current_input_blocked`, `run_allowed`, `affected_claim_ids`, `pending_deliveries`,
`corrections`, `total_corrections`, `has_more`, `pending_intents`,
`scientific_validity:"not_assessed"`, and `execution_performed:false`.

State is `unregistered`, `current`, `correction_pending`, or
`needs_revalidation`. A corrected current input may have `run_allowed:true`
while historical claims still need revalidation. UI controls use `run_allowed`,
not the state name alone. Missing status in a legacy response is unknown, not
proof of currentness. The projection never promotes new text to verified evidence.

Traversal caps are 16 historical input artifacts, 100 dependency edges,
1,000 relevant correction events, 100 pending intents per campaign, and 10,000
consumer receipts. Limits fail explicitly rather than silently truncating the
authoritative check. The display includes the first 50 correction summaries and
declares its total. Rebinding to changed source text is explicit through core
correction; same-byte edits cannot clear stale status. A newly selected corrected
Idea interpretation can seed a new campaign; this bridge does not rewrite an
existing campaign's original Idea choice or silently reauthenticate it.

Legacy campaign and Idea snapshot rows are projected in SQLite to their required
identity, decision, EvidenceCard, source, claim/input, and last-event fields before
Python materializes them. The raw row ceiling is 64 MiB and the resulting service
projection ceiling is 4 MiB; exceeding either fails explicitly. This supports old
large repeated-context histories without rewriting them or silently skipping
their source dependencies. Public core replay uses the core's bounded `project`
view, preserving its explicit history references and leaving saved replies intact.

## Export scope

`scoped_export(campaign_id)` returns an independent
`agentsdock-dependencies/1` JSON bundle, at most 1 MiB, with `data`, `scope`,
`authentication`, and `sha256`. It contains only that campaign's dependency edges,
relevant correction declarations/intents, its consumer receipts, and current
projection. Related source-action IDs can identify the campaign that originally
declared a correction; its source content, claims, and unrelated records are not
exported. Oversize export fails without producing a partial bundle. Its checksum
is integrity metadata, not a signature or authority. This bundle is inspection
data, not an executable import endpoint.

The existing core campaign bundle does **not** include this sidecar or native
model-job receipts. UI must identify the scope of each export. Independent core
restore must not imply restoration of dependency protection.

## Verification

`python -m unittest tests.test_research_dependencies -q` covers real disposable
Idea/Lab stores, exact lineage, no inference from same URLs, historical claims,
unprocessed-event admission, intent crashes before/after core commit and after
event creation, consumer-commit/producer-ack failure, idempotent replay, late
registration, bounded recovery fairness, namespace separation, old revision-less
imports, scoped export, missing stores, and producer-lock serialization.
These tests make no provider calls and do not touch installed or live research
records. Native UI integration acceptance remains separate.
