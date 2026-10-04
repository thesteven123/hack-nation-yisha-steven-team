# Original public source bytes

The public Literature reader retains bounded original HTTP response bodies in
the source library, separately from parsed text and exact model projections.
The bytes are the identity-encoded response body actually received, up to
10 MiB, not a reconstructed PDF/HTML file or a screenshot. This does not expand
the reader's public-URL, DNS, redirect, MIME, timeout or parser permissions.

`idea_source_archive.RawSourceArchive` adds two tables to `sources.sqlite3`:

- `raw_sources(digest,body,bytes,retained_at)` stores one immutable body per
  SHA-256. Default archive ceiling is 512 MiB; service configuration may select
  a bounded ceiling. Full storage refuses a new body and never evicts old ones.
- `raw_fetches(id,digest,requested_url,final_url,mime,downloaded_at,parse_version,
  parse_key,parse_status,parse_error)` records each actual fetch. Status is
  downloaded, parsed, failed or cancelled. Identical bytes at another URL/MIME
  share the body but keep independent fetch receipts. Parse success does not
  imply the worker retained its publication lease or published a source version.

Body and initial fetch receipt are saved atomically before parsing. Parser
failure/cancellation preserves both; cancellation joins in-flight storage work.
Published source packets carry a service-derived
`raw_document:{status:'retained',sha256,bytes,fetch_id,mime}` reference. Original
storage failure is explicit `not_retained` with a coverage limitation; already
readable parsed material is not represented as reconstructible original bytes.
Unavailable papers retain any actual failed `raw_fetches` separately from text
evidence. They do not become usable quote sources merely because bytes exist.

No historical Idea rows or source version JSON are backfilled. A missing old
reference means `not_retained` even if a later request fetches identical bytes
or the same URL. Fetch URL receipts may advance normally on a new fetch; the
immutable old versions remain unchanged. Restoring all research stores includes
retained raw BLOBs and receipts; per-version completeness must stay explicit.

## Authorized native Save As

`GET /api/research/ideas/{session_id}/papers/{source_id}/raw?source_hash={sha256}`
requires native administrative authorization and an exact lowercase SHA-256 of
the requested source **packet text**. It resolves that session's saved or frozen
historical packet first. The service derives the original body reference and
content hash from its provenance; arbitrary global hash lookups are unsupported.

The response always includes `version:1,status,session_id,source_id,source_hash`.
Successful retained data adds `content_hash,bytes,mime,fetch_id,verified:true,
body_base64`, and optional fixed `reason`/fetch `receipt` metadata. Here
`content_hash` identifies original bytes, while `source_hash` identifies the
quoted text projection. Metadata and body hash/length are checked again on read.
`not_retained`, `missing` and `corrupt` responses contain no body. The read facade
does not create directories, migrate tables, recover state or fetch a URL.

The native client admits at most 16 MiB of JSON, decodes and independently
verifies SHA-256/length in its main process, and uses an explicit Save As dialog.
It never sends Base64 to the renderer or automatically opens/executes the saved
HTML/PDF. An unavailable paper with a failed parse has no text packet hash; its
archived fetch is preserved for backup but is not exposed through this exact
packet download endpoint. A future failed-fetch download requires a separate
session-owned receipt lookup, not substitution of an original-byte hash for a
packet hash.

`create_router(..., source_library_root=...)` is the only new service hook.
Root integration supplies `STATE_DIR / 'research' / 'library'`. New and legacy
source files are distinguished by actual retention receipts, not UI assumptions.

Tests cover original byte equality, failure/cancellation retention, commit races,
same-byte URL/MIME separation, corrupt/missing data, no eviction, old row
preservation, pure read behavior and authenticated session/history-scoped HTTP.
Native UI Save As and full offline backup are separate acceptance evidence.
