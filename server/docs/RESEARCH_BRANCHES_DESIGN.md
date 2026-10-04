# E9: bounded branches within one research campaign

Status: **integrated in the maintained server source**. E9 now participates in
the Research Lab core, native model jobs, service dependency checks, and routes
protected by native-admin authorization. Lab routes support branch inspection
and explicit enable, plan, answers, decision, control, and run operations.
Native model-job routes accept branch scope and project its currentness while
retaining the campaign's shared quota and service-owned publication guards.

Actual native-provider and real Research UI acceptance remain **pending**.
Source integration and synthetic tests do not establish that the app workflow
or provider boundary is ready. The design and limits below remain applicable.

## Requirement and supported scope

The v0.5 [Human Decisions protocol](https://chatgpt.com/space/page_736305b6caa4819188817c21e16b6b62),
block `04646bc1-dfeb-4512-a87b-0a5c760d0179`, requires a missing answer to block
dependent work while independent, already-authorized work continues. The
[Controller protocol](https://chatgpt.com/space/page_19aae84300808191b273f940de3e040e),
blocks `e40c252f-4e4f-485d-b1cf-877611af0a97` and
`f2bd6050-af77-4da9-91c6-779d4843d4ad`, requires relevant revision checks and
atomic shared reservations. Execution blocks
`611f566d-d59e-4899-a82c-688232901a74` and
`3aec37e9-630f-4a8c-b251-b40dfcc4a865` require authoritative publication and
acknowledged cancellation. These are engineering requirements, not scientific
evaluation claims.

The proposed first executable scope is at most three explicit branches inside
**one campaign**, using that campaign's existing adapter and subsets of its
authorized methods. Source branches select supplied source IDs and freeze an
exact derived input packet. Numeric branches initially use the same frozen
paired dataset and allowed analyses. Shared inputs are disclosed; running two
branches never creates independent replication. No branch gets its own extra
campaign, model quota, arbitrary execution, network retrieval, or authority.

## Proposed request and record

Explicit enable request:

```json
{
  "expected_revision": 8,
  "idempotency_key": "enable-branches",
  "branches": [{
    "id": "source-a",
    "title": "Check supplied source A",
    "question": "Does this exact supplied passage occur?",
    "success_criterion": "Record occurrence and coverage limitations",
    "methods": ["exact_quote"],
    "source_ids": ["s1"],
    "depends_on": [],
    "questions": [{"id": "scope", "prompt": "Which scope should this action use?", "required": true}]
  }]
}
```

`source_ids` is omitted for numeric branches. Each dependency is
`{branch_id,require:"completed"|"qc_passed"}`; the three-node graph must be acyclic.
There are at most three consolidated questions across a checkpoint. Proposals
cannot submit artifact hashes, replacement source text, a quota, an authorization
epoch, or an execution capability. Native authorization remains a route guard.

The additive `branch_set` record contains campaign ID, schema version, one
`authority_epoch`, `root_control`, and branch records. Each branch has its own
research-context revision, exact question/criterion, methods, source selectors,
input artifact, root context hash, questions and exact answer artifacts,
dependencies, control state, and latest fixed result reference. Historical runs
remain in the **single campaign run ledger**; new runs record `branch_id`.
Branch cards point to these records instead of copying observations.

The helper deliberately has no budget field per branch. Local actions/rounds use
the campaign's existing totals. Native jobs retain the existing campaign quota
row, including all old used and reserved slots. Cancellation does not refund an
already-used native job or replace missing token usage with zero.

## Relevant revision vector

`brief.revision` currently also changes for an input correction. Comparing that
number globally would incorrectly invalidate source B when only source A was
corrected. Branch scope therefore binds:

```text
campaign_id + branch_id + branch_revision
root_context_hash + authority_epoch
input_artifact + exact dependency result refs + question/answer hash
```

`root_context_hash` includes the root goal, hypothesis, success criterion,
constraints, method authorization, adapter manifest and HypothesisSet artifact.
It excludes the general snapshot counter and overall input digest. The original
snapshot/brief revision is still retained as audit context, not used as a proxy
for relevance. Root scientific/authorization/HypothesisSet changes affect all
branches. Source selectors must still resolve against the **current authoritative
root input** on admission and acceptance: changed projected content, missing
sources, unknown registration, or a relevant pending correction blocks work.
Unchanged source B retains its projection digest; it is never silently rebound to
old source A or to a missing packet.

Local pause, answer, replan and result acceptance change that branch's revision.
Operational activity alone does not change it, so admission does not stale its
own packet. A root pause/resume/stop increments `authority_epoch`. Root stop
blocks all new work/publication. A branch pause blocks it and declared dependents,
while unrelated branches remain eligible. Changing an upstream question or answer
retains its old result as history but prevents presenting it as the answer to the
new question. Unknown/cancellation-pending owned work retains ownership and any
unsettled reservation until the executor acknowledges cleanup/outcome.

## Minimal product integration points

| Existing file/interface | Required change |
| --- | --- |
| `research_lab.py` `_mutate`/new branch methods | Keep one campaign transaction and global revision/event/replay ledger. Enabling uses `expected_revision`; branch controls use expected branch revision plus root scope. Pure key replay happens before new revision/permission checks. Persist helper results and CAS in this transaction. |
| `research_lab.py` `_plan` | Extract a context-based planner taking branch question, input artifact, allowed methods and previous branch runs. Filter candidates by the branch method subset. Freeze root goal **and** branch contribution, scope vector, exact answers, dependencies and HypothesisSet reference in spec/TaskPacket. No candidate means explicit `needs_method`/input, not fabricated execution. |
| `research_lab.py` `run` | Reuse the real adapter `execute→quality→analyze→review` path with the branch's frozen input/spec. Under the existing transaction check shared remaining actions/rounds and reserve once; append one run to global rounds, record branch ownership/ref, settle once. Preserve stage failures and original observations. Comparisons use explicit chosen run refs, not whichever unrelated branch ran last. |
| `research_lab_routes.py` | Proposed `/lab/{cid}/branches` GET/POST and `/branches/{bid}/decision`, `/plan`, `/run`. Each mutation retains native-admin authorization and idempotency. Exact URL/body contract needs root approval before client work. |
| `research_dependencies.py` | Register every service-created branch projection under the same campaign, and check the requested input/dependency digests. Add scoped registration keyed by campaign/scope/input; the existing single `registrations(campaign_id)` cannot attest three distinct current inputs. Preserve historical edges and producer/consumer pairs. Pending durable intents block matching inputs before delivery. |
| `research_model_jobs.py` | Add optional branch identity and frozen relevant scope to service-derived packets/receipts. Keep one quota per campaign and immutable job identity. Legacy jobs keep whole-campaign revision behavior; new branch jobs compare their scope. Permit at most one active model job per branch within the existing global worker bound, rather than treating unrelated branch work as the same campaign slot. |
| `research_model_routes.py` publication guard | Keep producer → Lab write lock → model commit. Resolve current relevant scope **inside** the guard and hold it stable through publication. A reread before the lock is insufficient. Stale output/actual usage remain diagnostic, never a current recommendation. |
| inspection/backup/runtime fingerprints | Recognize new fixed refs and branch ledger invariants, retain old records unchanged, include helper in loaded-code fingerprint, verify whole-domain restore and idempotent replay. |
| typed IPC/renderer | Show one shared budget, branch-specific status/next action, consolidated required questions, explicit enable/select/run, and global run references. Do not label a queued cancellation as stopped. |

For native tasks, the model-job database remains the authoritative owner of job
status/reservations. A branch's `owned_work` is a service projection of that record;
do not create a second independently committed quota/ownership ledger in Lab.
Local bounded actions can keep ownership/reservation/completion in their single
Lab transaction. Cross-store publication still follows the established lock order
and must not hold a SQLite lock over model/network work.

## Old campaigns and default lane

Reading an old campaign does not synthesize and persist a branch set, rewrite its
payload, relabel old runs, or change any saved idempotent response. With no branch
set the existing one-lane routes work exactly as before. The UI can describe this
as the current single workflow without pretending it is a saved branch record.

Only an explicit enable mutation creates the additive branch set. Existing runs
are recorded as `historical_unassigned_run_ids`; their original payloads and CAS
remain byte-identical. Their evidence may be explicitly referenced, never silently
assigned to a newly invented branch. Enabling after outcomes is marked exploratory.
Old frozen plans and actual selections remain inspectable history; each new branch
requires a new current plan. After explicit enable, new run/plan requests must name
a branch. Missing branch identity is an actionable error rather than silently
choosing one. Old request keys still replay their original response before any of
these new checks. This is a proposed compatibility policy, not a migration applied
to current user records.

## Acceptance before claiming E9

The new helper tests demonstrate A waiting for a required answer, real local
calculation on independent B, C gated on A's fixed result and QC, source-specific
correction/missing-source blocking, unchanged B scope, root epoch invalidation,
late answer rejection, exact answers, owned cancellation/unknown outcome, and a
shared admission check under a temporary SQLite write transaction. They do not
establish durable product integration.

Required next acceptance is through the actual native client/routes: create one
campaign with A/B/C; leave A pending; explicitly run B and observe exactly one
global action consumed; answer A and run it; inspect C's frozen dependency refs;
correct A and verify A/C block while B remains eligible. Exercise two concurrent
branch admissions against one remaining shared slot, pause/cancel a native role
without affecting another branch, reject a stale publication under the stable
scope guard, restart/restore and replay without another execution, and prove old
unmodified campaign payloads remain identical. No scientific generalization or
arbitrary-method support follows from this engineering acceptance.
