# Native Research Role Jobs

This optional service layer adds real native Codex tasks for a Scientist/Planner, Analyst and Reviewer. It is separate from deterministic Research Lab observations, QC, plans and rule reviews. Each role uses a fresh ephemeral native thread/turn, no tools, no file/environment access and the existing authenticated native provider. It cannot execute a candidate, change machine values, extend permissions, or silently turn an unsupported scientific goal into an adapter demonstration.

Implementation: `research_model_jobs.py`. It reuses the low-level native transport in `idea_generation._generate` with its own instructions and `ROLE_SCHEMAS`; it does not use Idea's role schemas or histories. There is no real model call on construction or any GET operation.

## Service integration

Create one cached controller per native service and private storage root, and call `await shutdown()` from the service lifespan. Authorize every route before touching this service. Do not construct one controller per request: initialization intentionally marks an earlier process's running jobs interrupted, without resubmitting them.

```python
controller = ResearchModelJobs(private_model_job_root)

# Explicit POST only. Never accept a renderer-built packet, candidate array,
# usage record, model interpretation or permission expansion.
receipt = await asyncio.to_thread(
    lambda: controller.prepare(
        {"campaign_id": campaign_id, "expected_revision": expected_revision,
         "role": "planner", "idempotency_key": request_key},
        resolve_snapshot=lab_store.get,
    )
)

# The server resolves these native settings. Credentials/env are never saved.
receipt = await controller.start(
    receipt["id"], executable=native_executable, model=configured_model,
    env=isolated_native_env,
    current_revision=lambda campaign: lab_store.get(campaign)["revision"],
)
# Poll controller.get(job_id); GET never retries. controller.run(...) is the
# equivalent wait-for-completion helper for non-HTTP integration/tests.
```

`prepare` synchronously invokes the authoritative snapshot resolver after idempotency lookup. The exact original request key/payload returns the existing receipt before checking whether the campaign has subsequently changed. A changed payload with the same key conflicts. The resolver and `current_revision` callbacks must be service-bound synchronous readers, never callables or data supplied by the renderer.

Public surface:

| Method | Behavior |
| --- | --- |
| `prepare(request, *, resolve_snapshot)` | Freeze service-derived role packet and reserve one job slot; no provider call |
| `replay(request)` | Return an existing exact-key receipt or null using a read transaction; do not reserve or dispatch |
| `await start(job_id, *, executable, model=None, env=None, current_revision)` | Recheck current revision, atomically admit one native attempt, own its task, return running receipt |
| `await run(job_id, **start_options)` | Start if still planned and wait for its owned task |
| `get(job_id)` | Read current durable receipt |
| `list(campaign_id, *, before=None, limit=50)` | `{items:[receipt],has_more,next_before,quota:{limit_jobs,used,reserved,resolved_model}}`; positive row cursor, page size 1–50 |
| `artifact(job_id, sha256)` | Read the job's exact packet, accepted output or retained rejected structured output; foreign refs rejected |
| `await cancel(job_id)` | Cancel planned reservation or join the owned provider cleanup before acknowledging cancellation |
| `await shutdown()` | Stop admission and cancel/join owned tasks |

Errors use `ModelJobError.code/message`; raw provider exceptions are not included in user-facing messages. Suggested HTTP mapping: not_found→404, conflicts/stale/busy/model_changed→409, exhausted→409 or429, invalid_* / missing_observation / context_too_large→400, storage/provider failure→503. `max_jobs_per_campaign` may be set to 1–6 at controller construction; an existing campaign retains its stored ceiling. Root service authorization is still required.

The HTTP router accepts `dependency_factory` as a service-owned optional dependency provider; no dependency callback, packet or permission can arrive in request JSON. Its `run_guard(campaign_id)` covers each new packet/reservation and planned-to-running admission in producer-lock → model-store order. Completed-result publication uses a separate service-bound `publication_guard(campaign_id)` injected into `ResearchModelJobs`: it acquires dependency producer → Lab `BEGIN IMMEDIATE` → model-store commit, reading the actual current revision inside the Lab lock. Every core mutation uses that same Lab write lock, so a revision cannot change between the publication check and commit. The earlier advisory revision is not accepted as proof of currentness. The HTTP service supplies this Lab revision guard even when no dependency provider is configured; direct controller integrations must supply it to claim atomic current-revision publication. These short critical sections run off the event loop and never span the native call. `admission(campaign_id)` additionally checks before native turn dispatch and after the provider returns. Pending durable correction intents and committed source corrections both prevent publication; available raw output and usage remain inspectable. Historical receipt replay precedes these checks and remains read-only.

`GET /api/research/lab/{campaign_id}/model-jobs` accepts only optional `before` and `limit`; the cursor is a positive signed-64-bit integer and limit is 1–50. Invalid or repeated parameters return 400 after native authorization. A repeated POST to that collection returns its existing receipt even if still planned. Explicit `POST /{job_id}/start` with `{}` resumes a planned reservation through current guards; GET never resumes it. A terminal job's repeated start also returns its existing receipt. The router lazily caches one controller per service lifespan and joins its owned tasks on exit, while synchronous initialization/configuration work runs off the event loop.

## Receipt and quota

```ts
type ResearchRole = 'planner'|'analyst'|'reviewer';
interface ModelJobReceipt {
  id:string; campaign_id:string; campaign_revision:number; brief_revision:number;
  role:ResearchRole;
  status:'planned'|'running'|'completed'|'failed'|'cancelled'|'interrupted'|'stale';
  created_at:string; updated_at:string; packet_ref:string;
  output_ref:string|null; raw_output_ref:string|null; output:Record<string,unknown>|null;
  usage:Record<string,unknown>; provider:Record<string,unknown>|null;
  context:{bytes:number;max_bytes:number;token_count:null;token_count_status:string;silent_truncation:false};
  error:null|{code:string;message:string}; attempt_count:0|1;
  events:Record<string,unknown>[]; has_more_events:boolean; owner:string|null;
}
```

Each campaign has at most six used plus reserved native job slots. Preparation reserves; dispatch moves one reserved slot to used atomically. Planned cancellation releases its reservation. Any started, failed, unknown or interrupted attempt retains its used unit; no automatic retries exist. Explicitly requesting a new job requires a new idempotency key and remaining quota. Global concurrent ownership is bounded to four jobs, with at most one running job per campaign.

Restart turns old running jobs into interrupted receipts; no submission occurs on reads/startup. This records unknown completion and preserves quota rather than claiming external exactly-once execution. A stale answer retains its raw structured output, usage and provider receipt, but cannot publish a current recommendation. Failure to read the current revision, or cancellation after receiving a provider response, also retains that actual response and usage without publishing it. Accepted historical receipts still retain their original campaign revision: consumers must not present them as applying to a later revised campaign. Job history is paginated, including every cancelled reservation even when there are more than fifty; GET, list and artifact reads use read transactions.

Reported native cumulative tokens are persisted as notifications arrive, without summing repeated totals. A subsequent error, timeout or cancellation preserves already reported fields with `complete:false`; unreported fields and money remain null. The actual thread/turn identity survives failure. The configured model selector returned by native `thread/start` is persisted and pinned before the first `turn/start`, including when later setup or generation fails. Later roles verify that selector before dispatch; it is not a claim to observe immutable weights or every backend request. An unresolved alias prevents the role turn from starting. A configured model change requires a new campaign or future explicit model-version transition mechanism.

## Role packets and machine/model separation

Packets are projected from the authoritative current snapshot. They include goal/success criterion/hypothesis, scope and remaining local actions, allowed adapter capabilities, frozen candidate IDs/rules/input refs, exact applicable v0.5 protocol blocks with Page/block IDs and canonical-JSON SHA-256 hashes. Analyst/Reviewer receive every visible current-input/current-brief observation and its matching claims, with no last-N cutoff. The controller permits at most six rounds. Planner receives frozen candidates and the local rule review rather than observation bodies, with every omitted observation explicitly referenced. Reviewer may receive one accepted Analyst interpretation from the same campaign revision, explicitly labeled as a model interpretation. Provider logs and parent chats are not context.

`active_selection` preserves the current plan/action, selection origin and exact stored selection reason, decision ID/revision/CAS reference, execution eligibility and optional complete decision body. It is included only after the selected plan and candidate match the current brief/input. The complete body is marked absent when it has left the recent decision window; the service does not reconstruct it from its display reason. In particular, an empty submitted feedback is not replaced by a claim that generated display text was the person's exact words. Pausing/resuming or requesting a model role does not grant new authority. `human_decision_coverage` discloses the total decision count and recent bodies included.

`observation_coverage` records total campaign rounds, visible rounds, current visible rounds, included observations, omitted/historical counts and whether the current scope is complete. `omitted_observations` contains fixed run/round/observation references and the omission reason. When the incoming bounded snapshot already folded a round, its current-brief/input eligibility is explicitly unknown until retrieval; its content is not inspected evidence. `historical_observations` identifies visible records excluded for different brief/input versions. Models cannot treat any of these uninspected references as evidence. These fields disclose bounded projection; this layer does not automatically retrieve omitted records.

When the snapshot declares a structured `hypothesis_set`, the native packet includes its full frozen contents, `hypothesis_set_artifact`, and `hypothesis_set_status`. Candidates, assumptions, scope, predictions, weakening conditions and supporting/opposing references remain human declarations with `scientific_status:'not_validated'`. A changed goal or input can leave the old set `needs_revalidation`; its original visibility/time is not silently rewritten. The legacy `goal.hypothesis` string remains a one-sentence summary and is not an automatically validated candidate. The set is subject to the same explicit whole-context size rejection below.

The prompt has a 160 KiB UTF-8 ceiling and rejects oversize packets without truncating, committing a quota reservation, or invoking a provider. Earlier valid observations are not silently removed to admit a task. The chosen-model tokenizer is unavailable here: `token_count:null` and `chosen_model_tokenizer_not_available` state that limitation. Actual provider token receipts after execution are separate from preflight context counting. This is not full implementation of the design's chosen-model tokenizer admission requirement.

- **Planner:** typed status recommendation/needs_input/needs_method; assesses every actual candidate once, selects only an applicable current executable ID, explains limitations and rejection reasons, asks at most three questions. It cannot fabricate method parameters or run the selected action.
- **Analyst:** typed status interpreted/needs_input/not_applicable; each finding names a real run and bounded field paths. The service attaches `machine_fact_refs` by resolving values from the frozen packet. Model-supplied statistical value fields are forbidden by schema. QC-invalid/failed runs support only QC/coverage/status limitations, not statistical findings. Narrative interpretation remains model-authored and is not mechanically proven correct.
- **Reviewer:** typed status reviewed/needs_input/not_applicable; each critique points to an existing matching claim/run, distinguishes source-support assessment from inference gaps, and proposes freeze_next_plan/ask_human/stop/needs_method. It cannot approve invalidated claims, edit QC, rewrite a threshold, or create fresh scientific evidence.

Accepted outputs add `layer:'model_interpretation'`, `machine_records_modified:false`, and `scientific_validation:'not_established'`. They remain separate receipts; applying a candidate recommendation still requires the current service-owned frozen plan and an explicit actual human decision. The three contexts are independent tasks, not evidence of statistically independent errors.

Received final responses are privately persisted under `raw_output_ref` before validation or turn settlement. Schema-rejected JSON retains its parsed value; malformed JSON retains the exact text in `{format:'unparsed_native_text',text:string}`. Raw content is excluded from default event summaries and failure messages and requires authenticated job-scoped artifact inspection. Native text is bounded to 128 KiB, raw artifact serialization to 1 MiB, and accepted role output remains bounded to 64 KiB. No received final response means no invented raw artifact. Invalid, incomplete or stale output never becomes a current recommendation.

## Verification boundary

The focused suite uses fake native responses and temporary stores. It covers real service packet construction, six-job reservation/settlement, replay before revision checks, no execution on reads, invalid IDs/fields, stale-before/after generation, usage retention on rejected output, model selector pinning, restart interruption without retry, double cancellation waiting for cleanup, and separate role inputs. Root integration owns real native acceptance. The module has no tools and cannot perform arbitrary scientific research by itself; unsupported methods remain an explicit outcome.
