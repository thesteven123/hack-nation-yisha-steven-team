# Idea group: literature, evidence, directions and human decisions

The sidebar's **Idea group** opens without an existing chat. Enter a goal, with
optional hypothesis, constraints, success criteria, preferences and pasted
sources. Start research explicitly. Goal-only briefs trigger actual discovery.

Two visible service-owned members exchange versioned packets. **Literature
Agent** uses native Codex web search, retrieves actual HTML/PDF text, maintains
the shared library and produces evidence cards. **Idea Agent** proposes two or
three alternatives with the same comparison fields: question, nearest work,
evidence, counterevidence, value, uncertainty, minimal informative action,
expected learning, feasibility and cost/risk. Review uses a separate native
context. It can request more retrieval, revise ideas, ask useful human questions
or recommend a decision. Actual thread/turn IDs, queries, source receipts,
messages and handoffs are inspectable. Members execute real jobs; their work
journal is not a fabricated chat transcript.

## Research and stopping

Up to three rounds, twelve native jobs and a bounded source/query budget are
allowed per explicit run. Gaps return to literature search with changed queries.
Zero usable evidence cannot count as an accepted comparison. Readiness separates
researching, ready for choice, evidence limited, needs input, failed and legacy
unsearched. Ready means suitable for choosing a next direction, not proven
scientific conclusions or complete literature coverage.

Native query batches are reported after execution. A watchdog interrupts at the
observed budget boundary and records actual counts and overruns; it does not
claim pre-dispatch enforcement of individual queries. Each native job has a
150-second deadline plus owned cleanup. Jobs can contain multiple provider
requests; cumulative native token usage is used when available, while incomplete
usage and monetary cost remain explicitly unknown.

The first provider is an existing native Codex ChatGPT login. Discovery enables
native web and its required composition host, with empty environment access and
disabled shell/apps/MCP/subagents. Evidence extraction, ideation and review use
tool-free ephemeral structured requests. No raw API key is provisioned; no
unrelated conversations or project files enter the tasks. Other providers need
separate integration and acceptance.

## Sources, evidence and shared reuse

Only retrieved or supplied text enters exact-quote verification; native search
snippets establish discovery only. Evidence retains its immutable packet hash,
character span, conditions, assumptions and interpretation.
`source_support=exact_quote_verified` means occurrence in that source packet;
`inference_validity=not_assessed` is distinct.

If a completed extraction has an incorrect source ID, a non-exact quotation, a
duplicate card ID or a quote crossing omitted text, its evidence is not published.
The local store retains the original bounded structured response and a precise
diagnostic record. Ordinary errors/activity do not display that rejected body.
The controller can re-extract once from the same frozen Literature packet with
the original resolved native model selector; this consumes another job from the existing
budget. The corrected answer must pass the same exact checks. PDF whitespace or
hyphenation is never silently normalized into an alleged verified quotation.
The original native thread response records its configured model separately
from the requested alias. Repair checks the new thread resolves to that same
selector before generation; missing or changed resolution stops repair. These
selectors do not establish immutable model weights or per-turn model telemetry.

Another invalid extraction ends as failed. An exhausted job budget leaves an
evidence-limited result, and provider timeouts/errors are not automatically
retried by this mechanism. Cancellation or restart prevents a pending correction
from publishing or restarting itself. Replacing literature or ideas invalidates
the dependent current output; earlier reviews stay in history. Old failures that
predate response-artifact capture keep their original records and may lack enough
information for a card-level diagnosis.

Public HTTP(S) reading validates every redirect and DNS result, pins the actual
connection to a public IP with the original TLS hostname, and uses no inherited
proxy, browser cookies or credentials. Uncompressed transfers are capped at
10 MiB. HTML parsing removes scripts/navigation. PDF parsing uses an owned
subprocess with time/resource bounds; cancellation joins cleanup. Scanned or
empty PDFs do not count as read. Figures, equation layout and linked supplements
are not silently covered.

The shared SQLite library caches full parsed text by content hash and parser
configuration. URL-specific retrieval receipts remain separate. Atomic leases
fence stale extraction workers; changed source versions/parser configuration
invalidate reuse. URL freshness is 24 hours, with visible retrieval timestamps.
Failed access is not cached as negative evidence. This is source-parsing reuse,
not reuse of question-dependent scientific conclusions.

Bounded task projections declare original spans, omissions and limitations.
Targeted rereading can reuse full cached text while producing a new packet ID;
earlier evidence keeps its own source packet. Quotes cannot cross the explicit
omitted-text marker. Successful download alone is not full-paper coverage.

## Human decisions and continuation

DecisionCards retain brief/option/evidence versions, recommendations, reasons,
next actions and at most three useful questions. Users can select, combine with
an explicit resulting goal, reject, revise or defer. Exact feedback and structured
answers persist without invented rationale. Revision guards reject stale answers.

Explicit Continue can search further, refine alternatives or deepen selected
directions. Both members receive feedback; it does not silently replace the goal.
Prior rounds and decisions survive restarts. Reads, history and startup never
start jobs. Choosing a direction does not execute experiments, send messages,
publish, purchase or deploy anything.

Only the selected explicitly started/opened generation is polled. Stop validates
its identity and awaits owned native/reader cleanup. Process loss becomes
interrupted, never automatic retry. Idempotency prevents duplicate accepted
dispatch, not exactly-once provider billing.

## API, storage and development

All endpoints use native-admin authorization under `/api/research/ideas`:

- GET/POST the root: recent groups / idempotent brief creation.
- GET `/{id}`, `/{id}/history`: current and historical versions.
- GET `/{id}/papers/{source_id}?source_hash={sha256}`: exact frozen evidence text and provenance, including prior generations; omit the hash for the saved reader packet.
- GET `/{id}/attempts/{attempt_id}`: explicit session-scoped inspection of a local structured response and validation diagnostics; not shown by default.
- POST `/{id}/generate`, `/{id}/cancel`: versioned start and joined stop.
- POST `/{id}/decision`: decision type, version and exact user input.
- POST `/{id}/followup`: idempotent feedback/answers, continuation mode and
  optional explicit goal/constraints/selected directions.

Schema 2 lives in `STATE_DIR/research/ideas/ideas.sqlite3`; additive
`stage_attempts` records retain responses up to 128 KiB each and diagnostics up
to 16 KiB, with input/output hashes and provider usage. Source parsing lives
in `STATE_DIR/research/library/sources.sqlite3`. Schema 1 migrates additively and
old results are labelled unsearched. Back up SQLite before upgrades. Downgrades
require the compatible pre-upgrade backup, not editing the schema number.

Use the marked development profile and separate server instance only.
`AGENTSDOCK_IDEA_DEV=1` requires explicit `AGENTSDOCK_USER_DATA` in a marked
`AgentsDockIdeaLabData` directory and dedicated loopback port; it skips URL
registration and updater startup. `scripts/idea_lab_dev.py` isolates HOME, CLI
home, state, config, temp files and tmux, copies native login only, registers no
service and verifies ownership on stop. Installed apps/services are not targets.

Focused tests cover store/controller, native provider/discovery, reader/cache,
development isolation, native IPC and renderer. Mocked tests alone are not
acceptance: exercise the goal-only journey and human continuation through the
actual desktop, IPC, HTTP, native provider and durable store. Record the observed
journey and remaining boundaries in the development log.
