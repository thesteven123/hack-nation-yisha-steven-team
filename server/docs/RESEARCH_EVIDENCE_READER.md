# E4 bounded evidence reader

Status: **independent helper and contract tests only**. The existing native
Research jobs do not yet expose this tool. No wrapper, job store, routes, runtime
manifest, provider permissions or existing records are changed by this increment.
Source tests and a generated native schema are not real-provider or UI acceptance.

The v0.5 [Controller protocol](https://chatgpt.com/space/page_19aae84300808191b273f940de3e040e)
requires fixed references for further retrieval, necessary evidence to remain
accessible, relevant revision checks, and staged retrieval or task splitting when
context cannot fit. Delivery and inference validity are different facts. This
helper records exact evidence delivery; it does not establish semantic reading,
scientific correctness, chosen-model token admission or method adequacy.

## Frozen service input

```python
manifest = freeze_manifest(
    campaign_id, task_id,
    scope={"campaign_id": campaign_id, "revision": expected_revision},
    requirements=[{
        "artifact_ref": exact_sha256,
        "required": True,
        "reason": "Source supporting the specified current claim",
        "relevance": "current",  # current | historical | protocol
    }],
    resolve_artifact=lab.artifact,
)
```

Only a service-owned packet builder chooses the requirements and scope. They are
not accepted from a renderer, model, URL, source document or arbitrary hash list.
The synchronous resolver must enforce campaign ownership and return
`{sha256,content}` from the authoritative store. Its content uses sorted, compact,
finite UTF-8 JSON and must match the exact SHA-256. It must itself be bounded and
read-only; do not instantiate a recovering store inside it. The helper has no
filesystem, network, database, credential, shell or subprocess capability.

The returned version-1 manifest contains campaign/task identity, a frozen scope
and scope hash, per-artifact reason/relevance/required flag, byte length and all
contiguous part hashes, plus immutable limits and its own SHA-256. Persist the
manifest inside the owning job's frozen packet before dispatch. No original
artifact or source is rewritten. Later reads verify the same document and part
hashes again. A different version of the same URL is not an allowed replacement.

The generic scope JSON can carry the proposed E9 branch vector instead of a
whole-campaign revision. The service supplies and checks that vector; the reader
does not infer branch authority or globally invalidate unrelated work. A future
branch manifest must include its root context/authority, branch input and exact
dependency references. It must not let a model request a different branch.

All currently selected artifacts are bounded to 64 total parts and 512 KiB of
serialized tool-result envelopes. Each UTF-8-safe part is at most 8 KiB before
envelope escaping, and each complete serialized result at most 64 KiB. The
manifest is at most 64 KiB and scope at most 16 KiB. Over-limit tasks fail before
native dispatch; the controller must explicitly split/narrow them. Required
evidence is never silently clipped. Optional entries may remain unread, but are
still included in the fixed manifest admission bound.

This byte budget is an engineering ceiling, not a model token count. Initial
prompt bytes and subsequent result bytes must be reported separately by the
integration. `token_count:null` remains truthful: the inspected native protocol
has no chosen-model tokenizer RPC. Native cumulative token receipts after
dispatch remain separate and must not be replaced with these byte counts.

## One read-only native tool

The local Codex 0.160.0 generated experimental schema supports
`thread/start.dynamicTools`, `item/tool/call` and a `DynamicToolCallResponse`.
The [official app-server documentation](https://learn.chatgpt.com/docs/app-server#dynamic-tool-calls-experimental)
describes experimental dynamic tool requests and their started/completed events.
Actual tool visibility with the selected model and the current isolated runtime
has not yet been exercised for this helper.

The sole declared tool is the flat function `read_research_evidence` with exactly:

```json
{"artifact_ref":"64 lowercase hex characters","part_index":0}
```

There is no campaign, path, URL, query, arbitrary JSON pointer, executable or
authentication token argument. The callback is bound to the service's manifest
and one actual native thread/turn. Requests outside that exact binding, tool,
namespace, hash or integer part range are rejected. A call ID cannot be rebound
to different arguments. All other approvals, shell, file, web, MCP and app tools
must remain disabled/declined in the future wrapper integration.

```python
reader = EvidenceReader(
    manifest,
    resolve_artifact=lab.artifact,
    check_scope=check_current_service_scope,
    persist_receipt=persist_owned_job_receipt,
)
reader.bind(actual_thread_id, actual_turn_id)
response = await reader.respond(rpc_request_id, "item/tool/call", params)
await reader.acknowledge(native_item_completed_params)
coverage = await reader.require_complete()
```

`check_scope(frozen_scope)` may be synchronous or async and must return exactly
`True` only while the same job owner, relevant revisions, dependency state and
authorization remain eligible. Any other return/exception fails closed. It runs
before reading, after reading and after durable preparation. The service's final
publication lock/guard remains necessary; these checks do not replace an atomic
current-scope check through result commit. Never hold model or Lab database locks
across native network work. Keep the established producer → Lab → model lock
order when adding manifest preparation and receipt writes.

Bind only actual native IDs. An RPC request can race `turn/start` returning; the
wrapper must wait for its owned turn binding with a bounded gate, never adopt the
request's claimed IDs as authority. The planned event integration must match the
request `callId` to `dynamicToolCall.id`, and accept a completion only when its
tool/arguments/status/success/contentItems match the exact prepared response.
If the runtime omits these confirmation fields, coverage stays incomplete; do
not infer delivery from a model's final answer or its self-reported citations.

Responses use `{success:true,contentItems:[{type:'inputText',text:...}]}`. The text
is canonical JSON carrying the task/manifest/artifact identities, full artifact
length, exact part offsets and hash, relevance/required flag, and part text.
`authority:'untrusted_evidence_only'` is explicit. Instructions inside retrieved
content grant no authority. Only JSON/text artifacts are exposed; raw downloaded
PDF/HTML bytes and executable files are not automatically opened or executed.

## Durable delivery receipts and failure

`persist_receipt(receipt,response_or_none)` must persist the receipt and, for a
prepared response, its exact private CAS body before returning exactly `True`.
The callback must enforce job ownership and use an atomic/idempotent commit; it
must not log evidence bodies to default UI activity. Synchronous and async
callbacks are supported. Receipt fields include task/campaign/scope/manifest,
native thread/turn/call, sequence/previous-receipt/SHA-256, artifact/part/range,
response hash and actual serialized result length. There is no credential field.

`evidence_response_prepared` means content was durably prepared, not necessarily
sent. Only a matching native completion creates
`evidence_response_confirmed` and updates delivered part coverage. A failed
socket write or absent completion leaves the required parts missing. Repeated
requests consume prepared-response count/byte budget, including the same call
ID. Duplicate completions are idempotent; overlapping/repeated parts never
inflate distinct evidence-byte coverage. Confirmed response bytes count unique
confirmed native call IDs, not inferred socket traffic or model-token cost.

`coverage()` is an informational, pure local snapshot. Its
`required_delivery_complete` reports only actual confirmed parts; it is **not**
an admission/publication decision. Use `await require_complete()` before
acceptance, then the controller's existing atomic final scope/publication guard.
Missing required parts raises `required_evidence_missing`. The integration must
preserve raw output and actual/unknown usage, expose an incomplete/needs-input
outcome, and not publish a completed interpretation. This helper does not itself
change job statuses or scientific records.

`cancel()` immediately prevents new returned content. A later valid native
completion may still record delivery with `eligible_at_confirmation:false`;
this cannot restore publication eligibility. Cancellation or exception during
receipt persistence makes the chain unusable (`receipt_persistence_unknown` or
`receipt_persistence_failed`), because a durable write outcome may be uncertain.
Do not continue or fork that chain. The service owns cancellation-safe completion
of its persistence operation and must preserve any committed receipts on restart.
An interrupted reader is not resumed into a new native turn or silently retried.

## Acceptance still required

Focused helper tests exercise exact Unicode reconstruction, foreign references,
forged confirmations, immutable hashes, duplicate/concurrent/out-of-order parts,
serialized limits, cancellation, stale scope, persistence failures and receipt
chains. They do not exercise the existing app-server wrapper or product routes.

Before claiming product availability, connect the service-owned manifest and
durable job receipts, declare this one tool with experimental capability, and
handle only its native request/events. Verify rejected output and usage survive
all failures, six-job quota behavior is unchanged, and old jobs remain immutable.
Use an isolated native acceptance task containing an unguessable fictional fact
in at least two required pages absent from the initial prompt. Confirm actual
tool requests/completions, hashes/coverage and the fact in the final output.
Then exercise the real UI with a scoped historical observation reference and
check cancellation/correction isolation. A successful tool read still does not
prove scientific validity or complete chosen-model tokenizer admission.
