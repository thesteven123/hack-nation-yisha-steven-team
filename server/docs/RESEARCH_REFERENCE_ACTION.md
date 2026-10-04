# AI Lab reference action (Stage 1)

This source-only slice adds a bounded, deterministic source-span check to
AgentsServer. It locates an exact quotation in supplied UTF-8 text and saves
the frozen specification, goal revision, observations, QC, costs and artifacts.
It does not fetch a URL, run a model, validate scientific truth, or establish
whether an inference is sound. It is not a complete research campaign engine.

## Contract

All `/api/research/` routes use the existing native administration guard.
Supply exactly one supported native token header. URL tokens and browser
requests cannot authorize them; unauthenticated server mode is insufficient.
These are server-owner operations, not general chat-scoped provider tools.
Client/provider credentials and existing sessions are unchanged.

| Request | Behavior |
| --- | --- |
| `POST /api/research/actions` | Validate and persist a frozen action; return its ID and `spec_hash`. |
| `GET /api/research/actions/{id}` | Read the specification, result and event history. |
| `POST /api/research/actions/{id}/run` | Accept `{"spec_hash":"…"}` and run the bounded local check. A different hash is rejected. |
| `GET /api/research/artifacts/{sha256}` | Download a verified content-addressed artifact. |
| `GET /api/research/protocols/{id}` | Retrieve one reviewed protocol summary and its original Page/block references. |

Planning input (all fields are required, unknown fields are rejected):

```json
{
  "idempotency_key": "source-check-1",
  "goal": {
    "id": "reference-workflow",
    "revision": 1,
    "question": "Where does the source report the measured value?",
    "completion_criterion": "Locate the exact passage and retain interpretation uncertainty."
  },
  "source": {
    "uri": "fixture://report/v1",
    "text": "Methods.\nThe measured value was 12.\n",
    "coverage": "excerpt",
    "missing_sections": ["appendix"]
  },
  "quote": "The measured value was 12."
}
```

The URI is provenance supplied by the caller, not proof of retrieval or
authenticity. Coverage also describes supplied material; original completeness
is unverified even when the caller says `full_text`. The goal is frozen for
this action. Changing a research goal requires a new action; mutable campaign
goals, decisions and cross-action authorization are outside this slice.

The server enforces a 1 MiB source ceiling, 16 KiB quote ceiling and a maximum
of 100 returned locations. It counts all matches, including overlaps, and
discloses location truncation. Character and UTF-8 byte offsets are zero-based,
end-exclusive; LF-delimited line numbers are one-based. Execution never obeys
instructions embedded in supplied text.

`completed` means the mechanical action committed. QC checks input integrity
and parser completeness for the supplied text. `source_support: supported`
applies only to the proposition that the exact quotation occurs in that text.
If absent, support is `cannot_determine`; this is not negative scientific
evidence. `inference_validity` is always `cannot_determine`. Costs record local
computation duration and zero external requests/model tokens/monetary spend;
they do not price host resources or include HTTP queue/serialization time.

## Persistence, reuse and recovery

The lazy store owns `STATE_DIR/research/research.sqlite3`, schema version 1.
Sources, parsed representations, specifications, observations, QC and results
are SHA-256 addressed BLOBs in the same database. State and event history
commit atomically. Artifact reads verify their hashes. Existing session/job
schemas are not migrated. Unknown research schemas fail closed.

An idempotency key names one exact request. Reuse with changed inputs fails
with 409. Running an already completed ID returns the original result and
cost record. A new action can reuse parsing only when source content, source
URI, coverage/missing sections and parser version match. It computes a new
observation and explicitly reports parsing reuse; it is not an independent
replicate. The store is scoped to one authenticated server owner. Multi-user
campaign and benchmark isolation must be designed before exposing this API
to less privileged callers.

The frozen plan commits before execution. Bounded computation and publication
then occur within one SQLite write transaction. A crash rolls that transaction
back; repeating the request may recompute local work but cannot publish two
results. This intentionally narrow guarantee does not extend to external
tools, jobs, charges, or model requests. A future executor needs attempt
identities, reservations and explicit unknown-outcome reconciliation.

Back up the SQLite database using SQLite's backup API, or copy it only after
stopping the isolated development process. Restore into the same research
subdirectory with a compatible schema. For rollback, revert the source changes
and preserve this separate database for later inspection; old server code
ignores it. Do not remove existing session/job databases.

## Validation and availability

From `server/`, using the prescribed isolated Python 3.13 environment:

```sh
python -B -m unittest tests.test_research_actions tests.test_research_action_routes -v
python -B scripts/smoke_research_action.py --output /path/to/new-evidence-directory
```

The smoke script starts an isolated loopback HTTP process using the real
`agent_server:app`, production authorization and research persistence. It
freezes a reference README and acceptance expectations before running,
restarts that process and saves inspectable artifacts. Its ASGI lifespan is
disabled to exclude unrelated provider/scheduler/update workers; it does not
exercise their startup or the desktop UI. No production installation or
configuration is required or changed.

The native AgentsDock journey and any future MCP/provider integration remain
separate acceptance work. Existing Workspace inspection can be reused once
there is an explicitly integrated artifact-export path. This API does not yet
publish research artifacts to a chat or add a research UI.

Next: connect an isolated client to an explicit action/inspection flow, then
validate a contrasting numerical-data task family before adding comparisons,
human choices, or adaptive research rounds. This slice makes no scientific
performance or acceleration claim.
