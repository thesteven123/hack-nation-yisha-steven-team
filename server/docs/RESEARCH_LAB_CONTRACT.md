# Research Lab bounded adapter contract

v0.5 implementation contract, 2026-10-03. This is a service-owned research workspace, separate from Idea discovery. The two supplied adapters execute deterministic local analyses only. They do not execute arbitrary code, physical experiments, external jobs, or claim scientific validity from an exact quote. The adapter registry and common controller are extensible; the current verified scope is these two families.

## API and public Python surface

Prefix `/api/research/lab`. Every route requires native administrator authorization. `ResearchLabStore(root: Path)` owns a separate `lab.sqlite3`; `LabError.code/message` is safe to return. Methods are synchronous bounded operations, called with `asyncio.to_thread` by routes. No provider callbacks.

| HTTP | Store method | Request/result |
| --- | --- | --- |
| GET `/capabilities` | `capabilities()` | `{schema_version:1, adapters:AdapterManifest[], execution:{...}}` |
| GET `/idea-seed/{session_id}` | authoritative Idea store reader | `{origin,brief,inputs:{quote,sources}}`; requires saved current select/combine, resolves exact evidence source hashes, no provider/network execution |
| GET `` | `ResearchLabInspector.list(before?,limit?)` | `{items:CampaignSnapshot[],has_more:boolean,next_cursor:string|null,consistency:string}` (1–50 compact summaries; no inputs) |
| POST `` | `create(body)` | create fields below → full CampaignSnapshot with first plan frozen |
| GET `/{id}` | `get(id)` | full CampaignSnapshot |
| GET `/{id}/history` | `ResearchLabInspector.history(id,before?,limit?)` | `{items:[{revision,at,event,brief,status}],has_more:boolean,next_cursor:string|null,consistency:string}` newest 1–50 |
| GET `/{id}/export` | `ResearchLabInspector.export_campaign(id)` | complete single-campaign snapshot bundle, SHA-256 manifest, 16 MiB limit; oversize fails 413 without a partial backup |
| GET `/protocols/{identifier}` | `ResearchLabInspector.protocol(identifier)` | versioned implementation summary, exact design Page/block/hash references; explicitly not the full source protocol |
| GET `/{id}/artifacts/{sha256}` | `artifact(id,sha256)` | `{sha256,media_type:'application/json',content:unknown}` (campaign-scoped, hash checked) |
| POST `/{id}/decision` | `decide(id,body)` | common mutation fields + `{kind:'select'|'revise'|'defer'|'stop',selected_action_id?:string,goal?:string,feedback:string}` → snapshot |
| POST `/{id}/run` | `run(id,body)` | common mutation fields; executes currently selected action once → snapshot |
| POST `/{id}/continue` | `advance(id,body)` | common mutation fields; freezes next plan selected by review, does not execute → snapshot |
| POST `/{id}/correct-inputs` | `correct_inputs(id,body)` | common mutation fields + `{inputs,reason:string}`; preserves old artifacts, marks prior dependent claims `needs_revalidation`, freezes a new plan |

Common mutation fields: `{expected_revision:positiveInteger,idempotency_key:nonemptyString}`. Repeated same key/payload returns the original committed response, even if later revisions exist. Changed payload conflicts. Stale new requests conflict. All writes/usage/state/artifacts/event/outbox entries commit atomically. Local computation is inside one bounded transaction: process death before commit rolls back; retry uses the same identity; no external exactly-once claim. Unknown external outcomes are unsupported rather than silently retried.

Read pagination uses an opaque `before` query cursor. List traversal is live keyset order `(updated_at,id)`; concurrent changes may move entries. History traversal is immutable revision order and a cursor is scoped to exactly one campaign. A byte-bound page also exposes continuation. Read-only export uses a SQLite read transaction and includes every version, event, idempotent response and referenced artifact for that campaign. `verify_bundle(bundle)` checks structural integrity and checksums; these are not authenticity signatures. `restore_campaign(bundle,destination)` is offline-only and requires a new empty isolated directory. There is no HTTP restore operation, no imported credentials and no automatic execution after restore.

Create: `{idempotency_key,entry:'goal'|'hypothesis',brief:{goal:string,hypothesis:string,success_criteria:string,constraints:string},adapter_id:'source_evidence'|'paired_numeric',inputs:AdapterInputs,budget:{max_actions:integer[1,12],max_rounds:integer[1,6]},origin?:{kind:'idea',session_id:string,generation_id:string,selected_ids:string[]}}`. All four brief strings are required; hypothesis may be empty for goal entry but must be nonempty for hypothesis entry. Goal/success criteria nonempty. Creation authorizes only selected adapter's local actions within the stated action/round budget, never external work. Inferred/default policy selections are marked separately from actual human decisions.

An Idea seed previews an existing saved choice. Create rechecks that choice and exact source packet contents; client provenance is replaced with service-derived provenance. The quote and task goal may be explicitly edited. A changed source requires a separate supplied-source task or an input correction, never an assertion that the altered text came from the original paper. `create(body, resolve_origin=...)` invokes this service resolver inside the first creation transaction, after idempotency lookup; an exact replay returns the original response even if the Idea selection later changes. An origin without a resolver is rejected.

`source_evidence` inputs: `{quote:string,sources:[{id:string,title:string,uri:string,text:string,coverage:'full_text'|'excerpt',missing_sections:string[],provenance?:object}]}`. 1–5 supplied/imported sources, total ≤150 KiB UTF-8, quote ≤16 KiB. Source URIs are labels, never fetched. Imported Idea evidence retains original provenance in immutable input artifact. Round one checks an exact quote in a selected supplied source. A hit leads to bounded surrounding context/qualification inspection; a miss leads to checking the next supplied source if one exists. Missing accessible text never refutes a scientific claim. Full-source bodies are available through the scoped input artifact.

`paired_numeric` inputs: `{baseline:number[],treatment:number[],unit:string,minimum_effect:number}`. Equal length 2–10,000, finite bounded numbers, minimum_effect ≥0. Round one computes paired differences and a descriptive normal-approximation interval. It is not a causal estimate and assumes no independence merely from array position. Next action is chosen from the observed interval: leave-one-pair-out stability when all effects exceed the frozen threshold, otherwise extreme-pair sensitivity. Both use the same fixed dataset; neither counts as an independent replicate. Adapter-specific QC and interpretation limits are frozen before execution.

## Full snapshot shape

```ts
interface CampaignSnapshot {
  schema_version: 1; id: string; revision: number; created_at: string; updated_at: string;
  entry: 'goal'|'hypothesis'; status: 'planned'|'awaiting_next'|'completed'|'needs_input'|'stopped';
  brief: {goal:string;hypothesis:string;success_criteria:string;constraints:string;revision:number;
    authorized_actions:string[]; stop_rules:string[]};
  origin: null|{kind:'idea';session_id:string;generation_id:string;selected_ids:string[]};
  adapter: AdapterManifest; input_artifact: string; input_summary: Record<string,unknown>;
  hypotheses: {id:string;statement:string;status:string;origin:string;weakening_condition:string}[];
  budget: {max_actions:number;max_rounds:number;used_actions:number;reserved_actions:number;
    remaining_actions:number;model_tokens:number;external_requests:number;monetary_cost:number;wall_ms:number};
  current_plan: null|{id:string;round:number;goal_revision:number;input_artifact:string;
    candidates:ActionSpec[];selected_action_id:string;selection_origin:'policy'|'human';
    selection_reason:string;comparison_criteria:string[];frozen_at:string};
  rounds: ResearchRound[]; decisions: Record<string,unknown>[];
  claims: {id:string;text:string;status:'current'|'needs_revalidation';source_support:string;
    inference_validity:string;conditions:string[];input_artifact:string;run_id:string}[];
  review: null|{next_action:'continue'|'stop'|'needs_input';reason:string;next_method:string|null;
    uncertainties:string[];alternatives:string[];input_refs:string[]};
  stop_reason:string|null; events:{id:string;seq:number;type:string;at:string;revision:number;data:Record<string,unknown>}[];
  has_more_events:boolean;
}
interface AdapterManifest {
  id:string;version:string;title:string;task_family:string;methods:string[];input_schema:Record<string,unknown>;
  observation_schema:Record<string,unknown>;applicability:string[];limitations:string[];
  required_protocols:string[];execution:'local_deterministic';fresh_replicates:false;
}
interface ActionSpec {
  id:string;method:string;title:string;question:string;goal_revision:number;input_artifact:string;
  parameters:Record<string,unknown>;expected_learning:string;cost:{actions:1;external_requests:0;model_tokens:0};
  qc_rules:string[];interpretation_rules:string[];stop_rules:string[];adapter_version:string;
  protocol_refs:string[];spec_artifact:string;
}
interface ResearchRound {
  index:number;plan:CampaignSnapshot['current_plan'];
  run:{id:string;attempt_id:string;generation:1;status:'completed'|'failed';spec_artifact:string;
    observation_artifact:string;input_artifact:string;replicate_id:null;is_independent_replicate:false;
    started_at:string;completed_at:string;usage:{actions:1;external_requests:0;model_tokens:0;monetary_cost:0;wall_ms:number}};
  observation:{kind:string;data:Record<string,unknown>;coverage:Record<string,unknown>};
  qc:{passed:boolean;checks:{name:string;passed:boolean;detail:string}[]};
  analysis:{claim:string;source_support:string;inference_validity:string;limitations:string[];input_refs:string[]};
  review:NonNullable<CampaignSnapshot['review']>;
}
```

List entries use the same envelope but `rounds`, `events`, `decisions`, `claims` are empty; `current_plan` is null. Open detail before decisions. History summaries retain exact brief revisions, with full immutable provenance accessible through round/artifact references. Corrected input does not erase prior records or silently revalidate old claims.

The frozen action includes exact input digest, adapter version, interpretation/QC/stop rules and protocol references. Analyst and reviewer are separate deterministic functions with separately recorded input references. They are not independent model agents and do not establish independent error rates. The review selects from actual observations and QC; it never treats execution success as scientific support. All zero costs are measured architectural facts of these local functions, not placeholders for unknown model usage.

## Additive record fields and brief revisions

The following fields extend schema 1 without rewriting existing campaigns. New clients should tolerate absent additive fields in old snapshots. New `current_plan.candidates[]` contain:

```ts
research_mode: 'predeclared_local_analysis'|'post_outcome_exploratory';
execution_environment: {
  python:string; implementation:string; cache_tag:string;
  files_sha256:Record<string,string|null>; missing_files:string[]; scope:string;
};
task_packet: {
  task_id:string; role:'local_research_worker'; brief_revision:number; snapshot_revision:number;
  goal:{objective:string;success_criterion:string};
  task:{method:string;contribution:string};
  relevant_state:{decisions:unknown[];previous_review:unknown;previous_run_ids:string[]};
  evidence:{input_artifact:string;input_summary:Record<string,unknown>};
  constraints:{scope:string;authorized_actions:string[];remaining_actions:number;remaining_rounds:number};
  output:{required:string[];completion_criterion:string}; stop_or_ask:string[];
  protocol_sections:{id:string;version:'0.5';requirements:string[]}[]; input_refs:string[];
};
```

Source hashes cover the actual module files `research_lab.py`, `research_actions.py`, `research_hypotheses.py`, `uv.lock`, and `pyproject.toml`; missing files are explicit. Fingerprints are fixed at module import. Both plan freezing and execution compare current disk fingerprints with the loaded fingerprint and reject changes, so a running old Python function cannot be represented by a newly edited file hash. An operational restart is required after deployment, then an explicit new plan if its frozen fingerprint differs. No environment variable, credential, private executable path or model token estimate is collected.

Each `round.run.runtime` has `{python,adapter_version,execution_environment}`. Each new snapshot has `comparisons` (empty on compact list):

```ts
{
  id:string; run_ids:[string,string]; rule:string; frozen_criteria:string[];
  comparability:'same_inputs_different_analysis'|'changed_inputs_not_like_for_like'|'changed_goal_not_like_for_like';
  eligible_for_pooled_confirmation:false;
  source_support:{before:string;after:string}; inference_validity:{before:string;after:string};
  propositions:string[]; changed_observation_fields:string[]; limitations:string[];
  unresolved:string[]; input_refs:string[]; artifact:string;
}[]
```

Comparisons refer to actual earlier/current runs and a content-addressed comparison artifact. Shared input data is not independent replication. Different questions or input versions are explicitly not a like-for-like comparison. A changed label applies only to its displayed proposition.

`decision` with `kind:'revise'` accepts optional `goal`, `hypothesis`, `success_criteria`, `constraints` and `hypothesis_set` in addition to the common mutation fields and feedback. Supply at least one explicit brief field or `hypothesis_set`. Omitted fields remain unchanged; hypothesis entry must retain a nonempty legacy hypothesis summary or an explicit non-null hypothesis set. Revision records preserve exact field changes and previous values. Previously published claims become `needs_revalidation`; historical rounds remain unchanged. A plan frozen after goal/input revisions following results is marked `post_outcome_exploratory`, and its analysis cannot be represented as independent confirmation.

Input corrections also preserve lineage. Changed source text/URI/coverage is stored as `provenance.kind:'user_corrected'`, `verification:'not_source_verified'`, with `derived_from` pointing to the prior input artifact and source hash. Unchanged sources retain their stored provenance rather than accepting caller-supplied replacement metadata. Quotes crossing a declared omission marker (including an imported original marker) are rejected as continuous source evidence. Observation data exposes `packet_match_count`, valid `match_count` and `rejected_gap_matches` separately.

Numerical `observation.data` additionally includes `descriptive_fact` and `criterion_result:'lower_bound_exceeds_threshold'|'upper_bound_below_threshold'|'interval_overlaps_threshold'`. The support label attaches to the explicit proposition that the mean exceeds the frozen threshold; it does not contradict a correctly reported observed mean or validate an arbitrary user hypothesis.

## Offline engineering evaluation

`python -m research_lab_evaluation --output <new empty isolated directory>` freezes evaluation rules before any policy execution and saves `frozen-rules.json`, per-instance service databases, and `report.json`. It runs eight known held-out fixtures across the source/numeric families, separately from a development example. Policies are precommitted fixed, observation-dependent adaptive, and one-action simple baselines under the same two-action ceiling. Two narrow ablations actually withhold previous result/review at the next decision or disable cross-action CAS input deduplication. No oracle is passed to policy functions.

Reported fields include actual/attempted action units, local and end-to-end wall time, fact/QC mismatches, failed/no-output attempts, inconclusive and unsupported claims, bounded goal coverage, and physical input artifact copies. It is not a blinded model evaluation or hidden-mechanism benchmark. Human time/alignment, native model planning quality, literature extraction quality, scientific novelty, generalization, causal discovery, independence and acceleration remain explicitly unverified.

## Independent verification corrections

New Idea-origin requests additionally require `origin.decision_revision`, the positive integer revision of the actual selected Idea decision. The server resolver verifies it along with the generation, selected directions and exact source packets. A replayed create key still returns its original durable response before reevaluating current Idea state. Without an authoritative origin, source provenance is labeled `user_supplied` / `not_source_verified`; client metadata does not become a verified Idea source.

Local machine execution, QC, analysis and review have separate failure boundaries. If a later stage fails, its content-free error appears in `round.run.stage_errors`; the already completed observation and other successful stages remain intact. `run.status:'completed'` means the local machine operation completed, while the campaign can still require input because analysis or review failed. A revised decision is included in the newly frozen worker packet, and a post-outcome exploratory branch retains that label on subsequent plans.

Optional real native Scientist/Planner, Analyst and Reviewer jobs are documented separately in [RESEARCH_MODEL_JOBS.md](RESEARCH_MODEL_JOBS.md). They read authoritative frozen records and publish model interpretations as separate receipts. They do not replace machine observations or QC, and their recommendations do not dispatch an action.

## Actual dispatch and bounded detail projection

New `run.dispatch_artifact` points to `{schema_version:1,run_id,attempt_id,scientific_spec_artifact,task_packet}`. This packet is frozen after the actual human selection and is the packet sent to the adapter. Its `actual_selection` contains the selected action/plan, exact selection reason, decision ID/revision and optional decision artifact. Relevant recent decisions retain their exact feedback; the effective selection remains addressable even after it leaves the recent-decision display. The scientific `run.spec_artifact` is unchanged from planning: dispatch context does not rewrite scientific rules after selection or observation.

Embedded `current_plan.candidates[]` and historical round candidates omit their duplicate `task_packet`; the full frozen packet remains in each `spec_artifact`. Snapshots retain the latest three exact decisions and latest ten exact events by default. Additive fields `total_decisions` and `has_more_decisions` describe omitted decisions; immutable revision history exposes `event_artifact` for new revisions, with the complete event/decision/feedback. Old schema-1 history may lack that optional reference and remains unchanged on disk.

`ResearchLabStore.project(snapshot)` is the public, pure, idempotent detail projection, also applied to unchanged legacy idempotent responses. The native envelope is at most 3 MiB, below the existing 4 MiB transport ceiling. It preserves all current candidates and normally all six possible rounds. If exact JSON serialization exceeds the ceiling, it removes older whole events/decisions and then older whole rounds, preserving at least the latest complete round. No scientific text, number, quote or QC result is cut off. Use `total_rounds` for counts/budgets rather than the number of visible rounds. `has_more_rounds` and `omitted_rounds:[{index,run_id,artifact}]` identify every folded round; each hash resolves through the existing campaign-scoped artifact endpoint to its complete bounded round with original observation/QC/analysis/review and frozen spec refs. A round's `artifact` excludes only repeated task packets, which retain their own references.

These fields are optional for old clients/records. Original SQLite versions and mutation receipts are not rewritten by GET or replay. Legacy round projections can be read as deterministic scoped virtual artifacts; a later explicit mutation persists the same content hash. Export preserves the original durable records and is not passed through the wire projection. Replaying an old mutation can therefore have a smaller HTTP representation while retaining its original scientific values, run IDs and stored response.

## Versioned hypothesis sets

Create and `decision(kind:'revise')` accept an optional top-level `hypothesis_set`. Omission keeps an existing set unchanged; null explicitly returns to no structured set. Goal-only exploratory campaigns may remain null or introduce a set later. A hypothesis-led entry requires either its existing nonempty `brief.hypothesis` summary or a non-null set. Old `hypotheses` arrays remain legacy records and are not transformed into detailed assumptions/evidence/predictions on read.

```ts
type HypothesisEvidenceSelector =
  | {kind:'input'}
  | {kind:'source';source_id:string}
  | {kind:'observation';run_id:string};
interface HypothesisSetRequest {
  candidates:{
    id:string; statement:string; assumptions:string[]; scope:string;
    supporting_evidence:HypothesisEvidenceSelector[];
    opposing_evidence:HypothesisEvidenceSelector[];
    predictions:string[]; weakening_conditions:string[];
  }[];
  open_alternative:string;
}
```

There are 1–4 candidates with unique ASCII identifiers of at most 64 characters. Statement/scope/open-alternative text has UTF-8 limits of 4000/2000/2000 bytes. Assumptions contain 0–8 strings; predictions and weakening conditions contain 1–6 each; each string is nonempty and at most 1000 bytes. Supporting and opposing lists each contain at most eight distinct selectors. The complete submitted set is at most 64 KiB of canonical UTF-8 JSON, including escaping. No renderer-supplied timestamp, version, scientific status, raw hash or foreign observation is accepted.

The service resolves selectors against that campaign's current input/source IDs and existing immutable run observations. Resolved references retain the selector fields plus:

- Input: `artifact` is the exact input CAS digest.
- Source: `artifact` is the containing input digest, plus `source_hash`, `coverage`, and complete `missing_sections`.
- Observation: `artifact` is the observation/QC digest, plus `input_artifact`, `brief_revision`, `run_status`, and `qc_passed`.

The resulting `HypothesisSet` includes these resolved candidate references and `id`, `revision`, `brief_revision`, `introduced_at`, `origin:'human'`, `post_outcome`, `input_artifact`, `previous_artifact`, `visible_evidence_refs`, `scientific_status:'not_validated'`, and an explicit evidence-scope limitation. `visible_evidence_refs` freezes all supplied input/source and already existing observation references at that version's introduction. It does not claim the person read every referenced item. The resolved record has a 512 KiB ceiling; oversized metadata is rejected atomically, never silently clipped.

The snapshot adds `hypothesis_set`, `hypothesis_set_artifact`, a monotonic `hypothesis_set_revision` (including explicit clears), and `hypothesis_set_status:'current_context'|'needs_revalidation'|'not_specified'`. Every explicit replacement freezes a new artifact and brief revision. Old sets, runs, decisions, spec hashes and replay responses remain immutable. If the brief or inputs change while the set is omitted, its original introduction metadata/artifact remain unchanged and its context status becomes `needs_revalidation`. Explicit post-observation versions have `post_outcome:true`; they cannot be presented as predeclared confirmation. These additions are absent in old snapshots until an authorized mutation explicitly introduces them; GET performs no migration or backfill.

`brief.hypothesis` remains a compatible human-authored one-sentence summary; it is not automatically copied to or from the structured candidates. When a set exists, it is the detailed candidate declaration. Neither this summary nor supporting/opposing classifications are machine scientific judgments. The service verifies referent existence and preserves observed QC; it does not verify the correctness of the person's classification, causality, likelihood, or truth of a hypothesis.

Frozen ActionSpecs and runs contain `hypothesis_set_artifact` when applicable. Local TaskPackets carry the set ID/version, candidate statements, context status and exact artifact reference. The full record remains accessible through the scoped artifact endpoint, and analysis/review input refs include it. Native role packets contain the full current frozen set, its status and artifact; their existing 160 KiB prompt admission can explicitly reject oversized context. Hypothesis records add no execution permission, method, local action, model call or budget. Core export verifies these CAS references and supports offline restore without executing new work; separate model/dependency sidecar limits remain unchanged.
