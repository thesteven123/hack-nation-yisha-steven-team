# Offline research backup and isolated restoration

This tool covers the complete recognized **research data root**, including
shared sidecars. It complements the scoped Lab campaign export; that export
alone cannot restore Idea origins, native receipts, or shared correction state.
It does not back up the rest of AgentsDock, credentials, provider authentication,
or application configuration. User-entered research text is preserved verbatim,
so the archive is private research data rather than a sanitized public export.

The v0.5 Controller requirement is `page_19aae84300808191b273f940de3e040e`, block
`8a2e51a4-1787-4109-b356-fe8f0767b0d6`: preserve records/events/originals, isolate
credentials, and test restoration. This implementation retains all currently
stored source text and frozen packets. It also supports the additive
`raw_sources`/`raw_fetches` archive when present. Legacy source versions without
an explicit retained-fetch reference stay `not_retained`, even if a later fetch
archived identical bytes. The manifest reports counts for retained original
BLOBs/bytes, fetch receipts, and retained/not-retained parsed versions. With no
retained originals it reports `raw_document_bytes:not_retained`; otherwise it
points to those per-fetch counts. A failed parse's retained download is preserved
without being labeled successfully read or published.
A checksum does not establish scientific validity, archive authenticity, or
protection from an actor who can replace both archive and manifest.

## Cooperative offline boundary

Every production process that writes the same root must retain this shared lock
from before opening a research store through **OS process exit**:

```python
from research_backup import retain_research_service_guard

# First step of service lifespan/startup; deliberately no finally/exit release.
retain_research_service_guard(STATE_DIR / "research")
```

The existing server has bounded shutdown and may leave storage finalizers alive
after its lifespan returns. Consequently, a lifespan-scoped context alone is
insufficient. `retain_research_service_guard` keeps a non-inheritable numeric file
descriptor in a mutex-protected registry; no context finalizer or atexit hook
releases it. The OS closes it on process exit. Repeated admission for a canonical
root reuses the descriptor only after checking root/lock inode identities and
safe paths again. Replacement locks and symlink aliases fail closed. The original
`research_service_guard` context manager remains available for temporary tools
and tests whose scope joins all owned work before returning.

The guard is a nonblocking POSIX `flock` on `.maintenance.lock`. Multiple normal
service holders are permitted. Backup obtains an exclusive lock and refuses
while any holder remains; new service startup also refuses during maintenance.
The tool never stops a service, kills a process, or calls a provider. A legacy
root without the lock is rejected: first deploy the maintenance-aware service,
then explicitly stop the intended development instance before backing it up.
Do not manually create/unlink the lock to bypass this check. Previously running
software that never adopted the guard is outside the cooperation contract.

Use a local Linux filesystem. Publication uses Linux `renameat2(RENAME_NOREPLACE)`;
unsupported systems fail closed. Cross-host/network filesystem lock behavior is
not validated. Root paths and artifacts cannot traverse symlinks. The lock must
be a private regular file, not a hard link. Backup and restore destinations must
not exist and must be outside their source root. Existing targets are never
overwritten, including a target created concurrently with publication.

## Commands and Python interface

```bash
python research_backup.py backup /isolated/research /backups/new-research-bundle
python research_backup.py verify /backups/new-research-bundle
python research_backup.py restore /backups/new-research-bundle /isolated/new-research
```

Corresponding synchronous functions are `backup_research(source_root, bundle_dir)`,
`verify_research_backup(bundle_dir)`, and `restore_research(bundle_dir, new_root)`.
The destination parent must exist. Operations return JSON-compatible reports;
`BackupError` exposes a content-free `code` and `message`. CLI failures exit 1.
There is deliberately no HTTP restore or restore-over-live endpoint.

The explicit inventory is:

| Store | Relative database path |
| --- | --- |
| First-stage source actions | `research.sqlite3` |
| Lab campaigns, versions, events, replay receipts and CAS | `lab/lab.sqlite3` |
| Idea sessions, generations, history, source packets and validation attempts | `ideas/ideas.sqlite3` |
| Native jobs, original outputs, usage, selected models and quota ledger | `model-jobs/model-jobs.sqlite3` |
| Parsed source versions, fetch receipts, reading leases and optional raw archive | `library/sources.sqlite3` |
| Scoped accepted literature extraction cache and request receipts | `evidence-cache/evidence-cache.sqlite3` |
| Correction producer and consumer, for every namespace | `dependencies/<sha256>/{corrections,marks}.sqlite3` |

Absent optional stores are recorded, not created. If either half of a dependency
pair is present, both are required. Unknown files/databases, unsupported schema
versions/tables, corrupt CAS values, and missing durable references reject the
whole backup; nothing is silently skipped. Known SQLite WAL/SHM/journal files
are recognized as storage sidecars. Their committed contents enter each snapshot
through the SQLite backup API rather than by copying a live database file.

The whole operation has a 2 GiB/64 database limit and each SQLite copy a 120 second
bound. Exceeding a bound rejects the complete operation, never returns a truncated
archive. These are offline storage bounds, not model token counts.

## Verification and restoration semantics

`manifest.json` records per-database byte hashes, schema hashes, every table's
row count and logical hash, missing optional stores, and validation counts. All
table rows, indexes and SQLite sequence state are copied. Database integrity and
foreign keys are checked. Additional checks cover Lab immutable versions and
CAS references, native task/observation/interpretation origins and quota totals,
Idea generations/packets/attempts, correction origins/receipts/intents, parsed
source versions, raw bytes/hash/length/fetch references, and accepted
literature-cache origins. Rejected Idea output whose body was intentionally not
retained preserves its hash/size/usage diagnostics and is counted separately;
the archive cannot reconstruct that body. No check reclassifies a
claim as scientifically supported or changes a current dependency decision.

Raw-reference checks include every immutable Idea revision's source provenance
and paper receipts, failed alternative fetches, and frozen role source packets.
The verifier also follows imported Lab input history back to the recorded Idea
human choice, selected evidence and exact frozen source provenance before
checking its original-byte reference. The model packet text hash and downloaded
byte hash have different meanings and are never substituted for each other.
User-supplied metadata is not recursively interpreted as an authenticated raw
reference. A legacy parsed version remains `not_retained` even if a newer URL
receipt for that same parsed content has retained bytes; each receipt is checked
in its own historical context. A parsed raw fetch need not have a published
source version, since publication can be refused after parsing or cancelled.

Backup and restore use private staging directories, fsync the files/directories,
then atomically publish without replacement. Verification compares the complete
inventory and table summaries; damaged, missing, extra, or mixed-version content
fails. The verifier uses read-only SQLite connections and does not instantiate
operational stores. Restoration checks the staged copies again before publication.

Restoration preserves running/planned/cancelled states, pending correction
intents, undelivered outbox entries, leases, native reservations and unknown usage
**exactly**. It neither reconciles them nor replays an execution. A later explicit
service startup follows the existing recovery rules: abandoned running native
work becomes interrupted without resubmission; retained planned work still needs
an explicit start. The archive remains immutable evidence of pre-startup state.
Restoring data does not install old code, authenticate a provider or authorize new
research. Existing loaded-code fingerprint and capability guards remain in force.

Automated coverage is in `tests/test_research_backup.py` using temporary real
stores and fake/local receipts only. Actual development-instance stop/backup/
isolated-restore acceptance is separate and must be recorded after lifecycle
integration; these tests alone do not establish that an existing server honors
the guard.
