import base64
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException

import idea_literature as reader
from idea_lab import IdeaStore
from idea_lab_routes import create_router
from tests.test_idea_lab import brief
from tests.test_idea_literature import BODY

from idea_source_archive import RawArchiveError, RawSourceArchive, MAX_RAW_BYTES


class RawArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = RawSourceArchive(self.root)

    def retain(self, body=b"<html>Public fictional acceptance document.</html>", **overrides):
        return self.archive.retain(body, **{**dict(requested_url="https://example.org/start",
            final_url="https://example.org/paper", mime="text/html; charset=utf-8", parse_version="fixture-v1"), **overrides})

    def test_exact_bytes_hash_and_separate_mime_url_receipts(self):
        raw = b"%PDF-fixture-bytes-\x00\xff\r\n"
        a = self.retain(raw, mime="application/pdf")
        b = self.retain(raw, mime="application/octet-stream", final_url="https://example.org/other")
        self.assertEqual(a["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertNotEqual(a["fetch_id"], b["fetch_id"])
        with self.archive._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM raw_sources").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM raw_fetches").fetchone()[0], 2)
        result = self.archive.read(a, include_body=True)
        self.assertTrue(result["verified"])
        self.assertEqual(base64.b64decode(result["body_base64"], validate=True), raw)
        self.assertNotIn("body_base64", self.archive.read(a))
        self.assertEqual(self.archive.read(b)["receipt"]["mime"], "application/octet-stream")

    def test_download_success_parse_failure_or_cancel_keeps_original_bytes(self):
        for status, error in (("failed", "parse_rejected"), ("cancelled", "parse_cancelled")):
            ref = self.retain()
            self.archive.finish_parse(ref["fetch_id"], status=status, error=error)
            result = self.archive.read(ref, include_body=True)
            self.assertEqual(result["status"], "retained")
            self.assertEqual(result["receipt"]["parse_status"], status)
            self.assertTrue(result["body_base64"])
            self.archive.finish_parse(ref["fetch_id"], status=status, error=error)
            with self.assertRaisesRegex(RawArchiveError, "already_finished"):
                self.archive.finish_parse(ref["fetch_id"], status="parsed", parse_key="another-parse")

    def test_hash_corruption_and_length_mismatch_never_return_body(self):
        ref = self.retain(b"one version")
        with self.archive._db() as db:
            db.execute("UPDATE raw_sources SET body=?", (b"bad version",))
        result = self.archive.read(ref, include_body=True)
        self.assertEqual(result["status"], "corrupt")
        self.assertNotIn("body_base64", result)
        with self.assertRaisesRegex(RawArchiveError, "corrupt"):
            self.retain(b"one version")
        with self.archive._db() as db:
            db.execute("UPDATE raw_sources SET body=?,bytes=?", (b"one version", 999))
        self.assertEqual(self.archive.read(ref)["status"], "corrupt")

    def test_legacy_rows_unchanged_and_future_same_url_not_original_retention(self):
        legacy = json.dumps({"id": "old", "url": "https://example.org/paper", "text": "saved parsed text",
                             "content_hash": hashlib.sha256(b"old bytes").hexdigest()})
        with self.archive._db() as db:
            db.execute("CREATE TABLE versions(key TEXT PRIMARY KEY,content_hash TEXT,parser TEXT,data TEXT)")
            db.execute("INSERT INTO versions VALUES('old','old-digest','old-parser',?)", (legacy,))
        RawSourceArchive(self.root)
        self.retain(b"new bytes")
        self.retain(b"old bytes")
        with self.archive._db() as db:
            self.assertEqual(db.execute("SELECT data FROM versions WHERE key='old'").fetchone()[0], legacy)
        self.assertEqual(self.archive.read(json.loads(legacy).get("raw_document"))["status"], "not_retained")
        self.assertNotIn("body_base64", self.archive.read(None, include_body=True))

    def test_reference_cannot_substitute_fetch_hash_or_mime(self):
        a, b = self.retain(b"original bytes"), self.retain(b"another version")
        wrong = {**a, "fetch_id": b["fetch_id"]}
        self.assertEqual(self.archive.read(wrong, include_body=True)["status"], "corrupt")
        self.assertEqual(self.archive.read({**a, "mime": "text/plain"})["status"], "corrupt")
        with self.assertRaisesRegex(RawArchiveError, "invalid_raw_reference"):
            self.archive.read(a["sha256"])

    def test_missing_blob_and_bound_failures_are_explicit(self):
        ref = self.retain()
        with sqlite3.connect(self.archive.path) as db:
            db.execute("DELETE FROM raw_sources")
        self.assertEqual(self.archive.read(ref)["status"], "missing")
        for body in (b"", "not-bytes", b"x" * (MAX_RAW_BYTES + 1)):
            with self.assertRaisesRegex(RawArchiveError, "invalid_raw_size"):
                self.retain(body)
        with self.assertRaisesRegex(RawArchiveError, "invalid_raw_reference"):
            self.archive.read({**ref, "bytes": True})

    def test_archive_quota_is_atomic_and_never_evicts_existing_artifact(self):
        archive = RawSourceArchive(self.root, max_archive_bytes=5)
        args = dict(requested_url="https://example.org/a", final_url="https://example.org/a", mime="", parse_version="v1")
        ref = archive.retain(b"first", **args)
        with self.assertRaisesRegex(RawArchiveError, "raw_archive_full"):
            archive.retain(b"next", **args)
        self.assertEqual(archive.read(ref)["status"], "retained")
        with archive._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM raw_fetches").fetchone()[0], 1)

    def test_symlinked_database_is_rejected_without_touching_target(self):
        root = self.root / "other"
        root.mkdir()
        target = self.root / "private-file"
        target.write_bytes(b"unchanged")
        (root / "sources.sqlite3").symlink_to(target)
        with self.assertRaisesRegex(RawArchiveError, "unsafe_archive_path"):
            RawSourceArchive(root)
        self.assertEqual(target.read_bytes(), b"unchanged")

    def test_read_facade_never_creates_or_migrates_missing_legacy_storage(self):
        missing = self.root / "nonexistent"
        reader = RawSourceArchive.open_existing(missing)
        self.assertEqual(reader.read(None)["status"], "not_retained")
        self.assertEqual(reader.read(self.retain())["status"], "missing")
        self.assertFalse(missing.exists())
        legacy = self.root / "legacy"
        legacy.mkdir()
        with sqlite3.connect(legacy / "sources.sqlite3") as db:
            db.execute("CREATE TABLE versions(key TEXT)")
        before = (legacy / "sources.sqlite3").read_bytes()
        self.assertEqual(RawSourceArchive.open_existing(legacy).read(self.retain())["status"], "missing")
        self.assertEqual((legacy / "sources.sqlite3").read_bytes(), before)


class RawArchiveIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.tmp)
        self.library = self.root / "library"
        self.download = self.enterContext(patch.object(reader, "_download", AsyncMock(return_value=(BODY, "text/html", "https://example.org/paper"))))

    async def test_production_reader_preserves_received_bytes_and_failed_parse_receipts(self):
        library = reader.SourceLibrary(self.library)
        result = await library.read("https://example.org/paper")
        raw = RawSourceArchive.open_existing(self.library).read(result["raw_document"], include_body=True)
        self.assertEqual(base64.b64decode(raw["body_base64"]), BODY)
        self.assertEqual(raw["receipt"]["parse_status"], "parsed")
        self.download.return_value = (b"Unreadable public file", "application/octet-stream", "https://example.org/blocked")
        failed = await reader.retrieve_papers([{"url": "https://example.org/blocked"}], {}, library_root=self.library)
        self.assertEqual(failed["sources"], [])
        receipt = failed["papers"][0]["raw_fetches"][0]
        raw = RawSourceArchive.open_existing(self.library).read(receipt["raw_document"], include_body=True)
        self.assertEqual(raw["receipt"]["parse_status"], "failed")
        self.assertEqual(base64.b64decode(raw["body_base64"]), b"Unreadable public file")
        self.assertIsNone(failed["papers"][0]["content_hash"])

    async def test_parse_cancellation_retains_bytes_and_terminal_fetch_status(self):
        library = reader.SourceLibrary(self.library)
        entered = asyncio.Event()
        async def parse(*_):
            entered.set()
            await asyncio.Future()
        with patch.object(reader, "_parse", side_effect=parse):
            task = asyncio.create_task(library.read("https://example.org/paper"))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        with sqlite3.connect(library.path) as db:
            self.assertEqual(db.execute("SELECT parse_status FROM raw_fetches").fetchone()[0], "cancelled")
            self.assertEqual(db.execute("SELECT body FROM raw_sources").fetchone()[0], BODY)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM versions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM leases").fetchone()[0], 0)

    async def test_repeated_cancel_joins_raw_commit_before_recording_cancelled_fetch(self):
        library = reader.SourceLibrary(self.library)
        committed, release = threading.Event(), threading.Event()
        retain = RawSourceArchive.retain
        def delayed(archive, *args, **kwargs):
            reference = retain(archive, *args, **kwargs)
            committed.set()
            release.wait(3)
            return reference
        with patch.object(RawSourceArchive, "retain", delayed):
            task = asyncio.create_task(library.read("https://example.org/paper"))
            await asyncio.wait_for(asyncio.to_thread(committed.wait, 2), 3)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(.02)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        with sqlite3.connect(library.path) as db:
            self.assertEqual(db.execute("SELECT parse_status FROM raw_fetches").fetchone()[0], "cancelled")
            self.assertEqual(db.execute("SELECT body FROM raw_sources").fetchone()[0], BODY)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM versions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM leases").fetchone()[0], 0)

    async def test_existing_parsed_cache_read_stays_not_retained_without_row_rewrite(self):
        library = reader.SourceLibrary(self.library)
        first = await library.read("https://example.org/paper")
        with library._db() as db:
            for table in ("versions", "urls"):
                field = "data" if table == "versions" else "receipt"
                old = json.loads(db.execute(f"SELECT {field} FROM {table}").fetchone()[0])
                old.pop("raw_document")
                db.execute(f"UPDATE {table} SET {field}=?", (json.dumps(old),))
            rows_before = db.execute("SELECT data FROM versions").fetchall(), db.execute("SELECT receipt FROM urls").fetchall()
        result = await library.read("https://example.org/paper")
        self.assertEqual(result["raw_document"]["status"], "not_retained")
        self.assertEqual(self.download.await_count, 1)
        with library._db() as db:
            self.assertEqual(rows_before, (db.execute("SELECT data FROM versions").fetchall(), db.execute("SELECT receipt FROM urls").fetchall()))

    async def test_archive_unavailable_keeps_real_text_with_explicit_retention_gap(self):
        with patch.object(RawSourceArchive, "retain", side_effect=RawArchiveError("raw_archive_full")):
            result = await reader.retrieve_papers([{"url": "https://example.org/paper"}], {}, library_root=self.library)
        source = result["sources"][0]
        self.assertEqual(source["provenance"]["raw_document"]["status"], "not_retained")
        self.assertIn("Observed result", source["text"])
        self.assertIn("Original downloaded bytes were not retained", " ".join(source["coverage"]["limitations"]))

    async def make_routes(self):
        store = IdeaStore(self.root / "ideas")
        item = store.create({"idempotency_key": "new-source", "brief": brief(False)})
        item, _ = store.begin(item["id"], {"idempotency_key": "read", "expected_revision": item["revision"]}, full_research=True)
        reading = await reader.retrieve_papers([{"url": "https://example.org/paper"}], {}, library_root=self.library)
        item = store.retrieval(item["id"], item["generation_id"], reading)
        source = reading["sources"][0]
        store.work(item["id"], item["generation_id"], "literature", new_round=True)
        store.freeze_packet(item["id"], item["generation_id"], "literature", [source], {})
        item = store.stop(item["id"], item["generation_id"], "interrupted", "Fixture ends after actual reader")
        def authorize(request):
            if request.headers.get("authorization") != "Bearer isolated-fixture":
                raise HTTPException(403, "Native authorization required")
        async def forbidden(*_):
            raise AssertionError("Raw file reads cannot call a provider")
        app = FastAPI()
        app.include_router(create_router(storage_root=self.root / "ideas", authorize=authorize, generate=forbidden,
                                        source_library_root=self.library))
        lifespan = app.router.lifespan_context(app)
        await lifespan.__aenter__()
        self.addAsyncCleanup(lifespan.__aexit__, None, None, None)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture", headers={"authorization": "Bearer isolated-fixture"})
        self.addAsyncCleanup(client.aclose)
        return store, item, source, client

    async def test_raw_http_is_exact_session_scoped_history_read_only_and_no_render(self):
        store, item, source, client = await self.make_routes()
        old_rows = store.history(item["id"])
        digest = hashlib.sha256(source["text"].encode()).hexdigest()
        path = f"/api/research/ideas/{item['id']}/papers/{source['id']}/raw"
        response = await client.get(path, params={"source_hash": digest})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["status"], "retained")
        self.assertEqual(result["session_id"], item["id"])
        self.assertEqual(result["source_hash"], digest)
        self.assertNotEqual(result["content_hash"], digest)
        self.assertEqual(hashlib.sha256(base64.b64decode(result["body_base64"])).hexdigest(), result["content_hash"])
        self.assertLess(len(response.content), 16 * 1024 * 1024)
        self.assertEqual(response.headers["content-type"], "application/json")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual((await client.get(path, params={"source_hash": digest}, headers={"authorization": "wrong"})).status_code, 403)
        self.assertEqual((await client.get(path)).status_code, 400)
        self.assertEqual((await client.get(path, params={"source_hash": result["content_hash"]})).status_code, 404)
        self.assertEqual((await client.get(path, params={"source_hash": digest.upper()})).status_code, 400)
        other = store.create({"idempotency_key": "other-session", "brief": brief(False)})
        self.assertEqual((await client.get(path.replace(item["id"], other["id"]), params={"source_hash": digest})).status_code, 404)
        self.assertEqual(store.history(item["id"]), old_rows)
        self.assertEqual(self.download.await_count, 1)

    async def test_legacy_http_and_corrupt_archive_never_return_original_body(self):
        store, item, source, client = await self.make_routes()
        legacy = store.create({"idempotency_key": "legacy", "brief": brief()})
        old = legacy["brief"]["sources"][0]
        response = await client.get(f"/api/research/ideas/{legacy['id']}/papers/{old['id']}/raw",
            params={"source_hash": hashlib.sha256(old["text"].encode()).hexdigest()})
        self.assertEqual(response.json()["status"], "not_retained")
        self.assertNotIn("body_base64", response.json())
        with sqlite3.connect(self.library / "sources.sqlite3") as db:
            db.execute("UPDATE raw_sources SET body=?", (b"corrupt",))
        response = await client.get(f"/api/research/ideas/{item['id']}/papers/{source['id']}/raw",
            params={"source_hash": hashlib.sha256(source["text"].encode()).hexdigest()})
        self.assertEqual(response.json()["status"], "corrupt")
        self.assertNotIn("body_base64", response.json())

    async def test_malformed_legacy_raw_reference_returns_corrupt_not_server_error(self):
        store, item, source, client = await self.make_routes()
        # Corrupt the real persisted packet: the readonly route deliberately no
        # longer initializes or calls the operational IdeaStore.paper method.
        saved = store.get(item['id'])
        saved['research']['sources'][0]['provenance']['raw_document'] = ['invalid legacy value']
        with sqlite3.connect(store.path) as db:
            db.execute('UPDATE sessions SET data=? WHERE id=?', (json.dumps(saved), item['id']))
        response = await client.get(f"/api/research/ideas/{item['id']}/papers/{source['id']}/raw",
            params={"source_hash": hashlib.sha256(source["text"].encode()).hexdigest()})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "corrupt")
        self.assertNotIn("body_base64", response.json())


if __name__ == "__main__":
    unittest.main()
