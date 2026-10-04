# Accepted Literature evidence reuse

Implements the research-artifact layer of Literature and Cache v0.5 for the
Idea Literature extraction stage. This is separate from source/PDF parsing
reuse and provider prompt caching. Reuse is not independent replication,
inference validation, a fresh search, or a new observation.

Design source: [Literature and Cache](https://chatgpt.com/space/page_1775310b16d08191906760157d513776),
blocks `56149a61-18ff-4a16-8e3b-5df955e6c6a0` (layer conditions),
`5ab6332f-3aa3-4f18-9027-83f00c5ed81e` (scope and deterministic keys),
`e645e138-46d4-44a0-a681-184a5f7bfd38` (leases and validated publication).
This document describes implementation; those design requirements alone are
not implementation or native acceptance evidence.

## Authority and exact key

`LiteratureEvidenceCache(root, idea_root, namespace=...)` is constructed by the
authorized service. The current `native-admin-local` namespace means one local
authenticated administrative authority. It is not multi-user isolation. New
users, evaluations or policy runs require separately configured scopes and
storage; credentials and renderer fields never select or infer cache scope.

The canonical SHA-256 key includes namespace, versioned recipe/protocol,
loaded provider/validator/cache code hashes, JSON schema hash, resolved native
model selector, CLI version and configuration fingerprint, exact prompt hash,
brief/hypothesis/feedback/revision context, source identities and text hashes,
parser/version provenance, coverage and limitations. Changed scientific inputs,
requested coverage, source versions, parser, model, schema or recipe miss.
An equal URL or similar goal does not establish interchangeability.

The same extraction projection is used for the native prompt, frozen validation
packet and cache key. Only retrieval timestamps, fetch-cache bookkeeping and
search-operation receipts are removed from this extraction projection. Actual
discovery receipts remain in each research record; a prompt flag acknowledges
them without inventing corpus-wide absence/completeness claims. Round ordinal
and remaining dispatch budgets are operational controller state, not extraction
instructions. Source text/ID/title/URI, coverage, scientific provenance, goal,
constraints, hypothesis, prior review/gaps and feedback remain exact. We do not
ignore prompt fields in the key while still sending them to the model.

## Accepted origin and lifecycle

SQLite atomically admits one owner per exact key with a 45-second renewable
lease and monotonically increasing fence. Concurrent requests wait at most
100 seconds, then stop without dispatching a duplicate extraction. Successful
waiters record `join`; immediate reuse records `hit`; expired-owner takeover is
`miss`, not saved work. Crashes can duplicate external computation after expiry;
no exactly-once provider claim is made. A stale owner cannot publish over a newer
fence. Cancellation joins pending SQLite writes before releasing its exact owner.

Each miss must reserve the normal durable model-job budget immediately before
native `turn/start`. The optional exact-quote correction remains at most one
additional budgeted turn using the same frozen packet and pinned selector.
Only a successful service `save_stage` permits cache publication. The origin
must still exist as an accepted stage attempt in the Idea database; its output
hash and frozen input digest must match. Hits revalidate exact source quotes and
pass through the receiving generation's ordinary `save_stage` again. Historical
outputs are not rewritten. Empty evidence is never cached as proof of absence.

Lookup, initialization, record-size or publication failure does not erase an
accepted result. Optional storage failure records `bypass` with zero saved jobs
and dispatches under the normal budget; failed publication records an activity
but preserves the current accepted output. An observed live owner/lost fence
does not bypass into a competing dispatch. Initialization failure disables
reuse for that service instance, while authorized existing reads remain usable.
Stored cache records are bounded to 256 KiB; provider prompt/output bounds stay
200/128 KiB. No private exception text is surfaced as the fallback reason.

## Receipts and service hooks

`generate_idea_stage(..., cache_request=None, on_dispatch=None)` performs native
configuration and ephemeral-thread preflight, then cache resolution, then calls
`on_dispatch` only before a real model turn. The service wrapper forwards these
hooks; the router lazily constructs the cache only after authorization.

`usage[].provider.literature_cache` is
`{version,status,key,scope,request_id,waited,stale_rejected,reason,artifact_hash,origin,avoided_model_jobs}`.
Status is `miss|hit|join|bypass`; key/scope may be null when unavailable. Origin is
`{session_id,generation_id,round,attempt_id,packet_digest}` or null. Only hit/join
save one Literature inference job. Miss means extraction was required, not that
cache publication succeeded. The accepted origin's provider/usage are retained
separately as `origin_provider`/`origin_usage`; unknown origin cost stays unknown.
Current hit tokens are zero because that extraction turn was skipped. Native
preflight still occurs, so this does not mean zero network requests or zero
other group work. Current thread IDs are never invented from origin IDs.

## Reproducible acceptance boundaries

Two UI groups with identical goal/constraints and a fixed single public paper
can hit only if the actual discovered/read source packet and extraction context
are identical. Different titles, source versions, parser coverage or review gaps
must miss. Do not relax the key to make a demonstration hit.

A deterministic native integration harness can instead create two new sessions
in a private temporary IdeaStore/cache, supply one explicitly fictional memo,
call the actual native Literature provider, and publish through real
`freeze_packet`/`save_stage`. The second identical packet must do native preflight
and then reuse with no extraction turn. Export actual attempts, usage, source
spans and cache origin. This proves native Literature/cache integration only;
it is not UI, discovery, Idea/review, scientific validity or full-project
acceptance. The harness must be executed explicitly and separately from tests.

`tests.test_idea_evidence_cache` uses synthetic providers and private temporary
stores to cover scoped keys, two-group hit, concurrent join, lease fencing,
cancel/admission races, corruption/orphaned origins, bounded repair, fallback,
authorization and production-wrapper AST wiring. Native acceptance remains a
separate recorded run.
