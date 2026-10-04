"""Offline backup boundaries with actual temporary research stores, no network."""
import asyncio
import base64
import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import research_backup as backup
from idea_evidence_cache import LiteratureEvidenceCache
from idea_literature import PARSER_VERSION, SourceLibrary
from idea_lab import IdeaStore, IdeaError, STAGES
from idea_source_archive import RawSourceArchive
from research_actions import ResearchActionStore
from research_dependencies import ResearchDependencies
from research_lab import ResearchLabStore
from research_lab_routes import IdeaImport
from research_model_jobs import ResearchModelJobs
from tests import test_research_dependencies as dependency_fixture
from tests.test_research_lab import numeric_request, source_request
from tests.test_idea_lab import brief, generated
from tests.test_research_model_jobs import output_for


@unittest.skipUnless(sys.platform.startswith("linux"), "Offline publication uses Linux renameat2/flock")
class ResearchBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root, self.bundle, self.restored = (self.base / name for name in ("research", "bundle", "restored"))
        with backup.research_service_guard(self.root):
            self.lab = ResearchLabStore(self.root / "lab")
            self.request = numeric_request()
            self.item = self.lab.create(self.request)

    def error(self, code, callback):
        with self.assertRaises(backup.BackupError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def manifest(self):
        return backup.backup_research(self.root, self.bundle)

    def test_backup_restore_preserves_every_version_artifact_and_idempotent_replay(self):
        body = {"expected_revision": self.item["revision"], "idempotency_key": "first-run"}
        after = self.lab.run(self.item["id"], body)
        manifest = self.manifest()
        self.assertEqual(manifest["scope"]["raw_document_bytes"], "not_retained")
        report = backup.restore_research(self.bundle, self.restored)
        self.assertTrue(report["restored"])
        self.assertFalse(report["store_recovery_executed"])
        self.assertEqual(backup._file_hash(self.restored / "lab/lab.sqlite3"), manifest["databases"]["lab/lab.sqlite3"]["sha256"])
        store = ResearchLabStore(self.restored / "lab")
        self.assertEqual(store.run(self.item["id"], body), after)
        self.assertEqual(store.get(self.item["id"])["budget"]["used_actions"], 1)
        self.assertEqual(store.create(self.request), self.item)

    def test_active_service_blocks_backup_across_processes_then_releases(self):
        with backup.research_service_guard(self.root):
            result = subprocess.run([sys.executable, "-c", "from research_backup import backup_research; import sys; backup_research(sys.argv[1],sys.argv[2])", str(self.root), str(self.bundle)], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("still owns", result.stderr)
            self.assertFalse(self.bundle.exists())
        self.manifest()

    def test_production_guard_survives_lifespan_return_until_actual_process_exit(self):
        script = """import gc,os,sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import research_backup as b
def lifespan():
    b.retain_research_service_guard(sys.argv[1])
lifespan()
gc.collect()
b.retain_research_service_guard(Path(sys.argv[1]) / '.')
with ThreadPoolExecutor(max_workers=4) as pool:
    list(pool.map(b.retain_research_service_guard, [sys.argv[1]] * 12))
assert len(b._PROCESS_GUARDS) == 1
assert not os.get_inheritable(next(iter(b._PROCESS_GUARDS.values()))['descriptor'])
print('lifespan-returned-process-alive', flush=True)
sys.stdin.readline()
"""
        child = subprocess.Popen([sys.executable, "-c", script, str(self.root)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "lifespan-returned-process-alive")
            self.error("maintenance_busy", self.manifest)
            child.communicate("exit\n", timeout=10)
            self.assertEqual(child.returncode, 0)
            self.manifest()
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)

    def test_production_guard_reentry_checks_inode_and_unsafe_alias(self):
        script = """import os,sys
from pathlib import Path
import research_backup as b
root=Path(sys.argv[1])
b.retain_research_service_guard(root)
alias=root.parent / 'research-alias'
alias.symlink_to(root, target_is_directory=True)
try:
    b.retain_research_service_guard(alias)
    raise AssertionError('unsafe alias accepted')
except b.BackupError as exc:
    assert exc.code == 'unsafe_path'
lock=root / b.LOCK
lock.rename(root / 'original-lock')
lock.touch(mode=0o600)
try:
    b.retain_research_service_guard(root)
    raise AssertionError('replaced lock accepted')
except b.BackupError as exc:
    assert exc.code == 'unsafe_path'
print('reentry-failed-closed')
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.root)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "reentry-failed-closed")

    def test_offline_operation_blocks_service_start_and_other_maintenance(self):
        with backup._guard(self.root, service=False):
            self.error("maintenance_busy", lambda: backup.research_service_guard(self.root).__enter__())
            self.error("maintenance_busy", self.manifest)
        with backup.research_service_guard(self.root):
            pass

    def test_uninitialized_legacy_root_refuses_unsafe_offline_claim(self):
        (self.root / backup.LOCK).unlink()
        self.error("maintenance_not_initialized", self.manifest)

    def test_unknown_database_and_credential_file_never_silently_omitted(self):
        secret = self.root / "unknown.sqlite3"
        secret.write_text("not a supported database")
        self.error("unexpected_file", self.manifest)
        secret.unlink()
        (self.root / "auth.json").write_text("a secret must never be packed")
        self.error("unexpected_file", self.manifest)
        self.assertFalse(self.bundle.exists())

    def test_credentials_outside_research_root_not_read_or_archived(self):
        (self.base / "auth.json").write_text("fixture credential sentinel")
        manifest = self.manifest()
        self.assertEqual(manifest["scope"]["credentials_configuration"], "excluded;not_read")
        self.assertNotIn("fixture credential", (self.bundle / "manifest.json").read_text())
        self.assertEqual(sorted(p.relative_to(self.bundle).as_posix() for p in self.bundle.rglob("*") if p.is_file()),
                         ["databases/lab/lab.sqlite3", "manifest.json"])

    def test_no_overwrite_existing_target_or_copy_under_source(self):
        self.bundle.mkdir()
        (self.bundle / "keep").write_text("unchanged")
        self.error("destination_exists", self.manifest)
        self.assertEqual((self.bundle / "keep").read_text(), "unchanged")
        self.error("unsafe_path", lambda: backup.backup_research(self.root, self.root / "nested"))
        self.bundle = self.base / "other-bundle"
        self.manifest()
        self.restored.mkdir()
        self.error("destination_exists", lambda: backup.restore_research(self.bundle, self.restored))

    def test_symlink_and_hardlinked_lock_fail_closed(self):
        alias = self.base / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.error("unsafe_path", lambda: backup.backup_research(alias, self.bundle))
        os.link(self.root / backup.LOCK, self.base / "lock-alias")
        self.error("unsafe_path", self.manifest)

    def test_atomic_publication_race_does_not_overwrite_new_directory(self):
        publish = backup._publish
        def race(stage, target):
            target.mkdir()
            (target / "concurrent-user-file").write_text("keep")
            publish(stage, target)
        with patch.object(backup, "_publish", side_effect=race):
            self.error("destination_exists", self.manifest)
        self.assertEqual((self.bundle / "concurrent-user-file").read_text(), "keep")
        self.assertEqual(len(list(self.bundle.iterdir())), 1)

    def test_failed_snapshot_publishes_no_partial_bundle(self):
        with patch.object(backup, "_backup_database", side_effect=OSError("fixture write failure")):
            self.error("storage_error", self.manifest)
        self.assertFalse(self.bundle.exists())
        self.assertFalse(list(self.base.glob(".research-backup-*")))
        self.manifest()

    def test_missing_or_changed_bundle_file_rejected_without_restore(self):
        self.manifest()
        db = self.bundle / "databases/lab/lab.sqlite3"
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute("UPDATE campaigns SET payload=json_set(payload,'$.brief.goal','tampered')")
        self.error("integrity_error", lambda: backup.restore_research(self.bundle, self.restored))
        self.assertFalse(self.restored.exists())

    def test_manifest_checksum_and_unrecognized_extra_file_rejected(self):
        self.manifest()
        path = self.bundle / "manifest.json"
        original = path.read_bytes()
        value = json.loads(original); value["created_at"] = "changed"
        path.write_bytes(backup._json(value))
        self.error("integrity_error", lambda: backup.verify_research_backup(self.bundle))
        path.write_bytes(original)
        (self.bundle / "databases" / "hidden.key").write_text("never restored")
        self.error("unexpected_file", lambda: backup.restore_research(self.bundle, self.restored))

    def test_new_schema_or_unknown_table_rejected(self):
        with closing(sqlite3.connect(self.lab.path)) as db, db:
            db.execute("PRAGMA user_version=99")
        self.error("unsupported_schema", self.manifest)
        with closing(sqlite3.connect(self.lab.path)) as db, db:
            db.execute("PRAGMA user_version=1")
            db.execute("CREATE TABLE credentials(secret TEXT)")
        self.error("unsupported_schema", self.manifest)

    def test_wal_commits_included_without_modifying_original_payload(self):
        with closing(sqlite3.connect(self.lab.path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA wal_autocheckpoint=0")
            db.execute("BEGIN")
            db.execute("SELECT count(*) FROM campaigns").fetchone()
            item = self.lab.decide(self.item["id"], {"expected_revision": 1, "idempotency_key": "defer", "kind": "defer", "feedback": "Keep exact pending state"})
            self.assertTrue(Path(str(self.lab.path) + "-wal").exists())
            manifest = self.manifest()
            self.assertEqual(self.lab.get(self.item["id"]), item)
            self.assertEqual(manifest["databases"]["lab/lab.sqlite3"]["tables"]["versions"]["rows"], 2)
        backup.restore_research(self.bundle, self.restored)
        self.assertEqual(ResearchLabStore(self.restored / "lab").get(self.item["id"]), item)

    def test_running_native_job_and_reservation_restored_without_recovery_or_zero_usage(self):
        models = ResearchModelJobs(self.root / "model-jobs")
        request = {"campaign_id": self.item["id"], "expected_revision": 1, "role": "planner", "idempotency_key": "running"}
        running = models.prepare(request, resolve_snapshot=self.lab.get)
        models._admit(running["id"], 1, "test-model")
        reserved = models.prepare({**request, "idempotency_key": "reserved"}, resolve_snapshot=self.lab.get)
        before = models.list(self.item["id"])
        self.assertIsNone(before["items"][1]["usage"]["total_tokens"])
        self.manifest()
        backup.restore_research(self.bundle, self.restored)
        with backup._read(self.restored / "model-jobs/model-jobs.sqlite3") as db:
            payload = json.loads(db.execute("SELECT payload FROM jobs WHERE id=?", (running["id"],)).fetchone()[0])
            self.assertEqual(payload["status"], "running")
            self.assertIsNone(payload["usage"]["total_tokens"])
            self.assertEqual(tuple(db.execute("SELECT used,reserved FROM quota").fetchone()), (1, 1))
        # Recovery is an explicit subsequent startup, not part of restoration.
        recovered = ResearchModelJobs(self.restored / "model-jobs")
        self.assertEqual(recovered.get(running["id"])["status"], "interrupted")
        self.assertEqual(recovered.get(reserved["id"])["status"], "planned")
        self.assertEqual(recovered.list(self.item["id"])["quota"], before["quota"])

    def test_mixed_generation_quota_and_missing_cross_store_references_fail(self):
        models = ResearchModelJobs(self.root / "model-jobs")
        models.prepare({"campaign_id": self.item["id"], "expected_revision": 1, "role": "planner", "idempotency_key": "job"}, resolve_snapshot=self.lab.get)
        with closing(sqlite3.connect(models.path)) as db, db:
            db.execute("UPDATE quota SET reserved=0")
        self.error("integrity_error", self.manifest)
        with closing(sqlite3.connect(models.path)) as db, db:
            db.execute("UPDATE quota SET reserved=1")
        self.lab.path.unlink()
        self.error("integrity_error", self.manifest)

    def test_complete_sidecars_dependency_intents_cache_and_library_history(self):
        fixture = dependency_fixture.ResearchDependencyTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root = fixture.root
        with backup.research_service_guard(self.root):
            pass
        old = fixture.idea()
        a, b = fixture.campaign(old, "a"), fixture.campaign(old, "b")
        fixture.execute(a)
        fixture.bridge.register_campaign(b["id"])
        new = fixture.idea(old, finding="An explicit fixture correction")
        fixture.event(old, new)
        fixture.bridge.drain(limit=1)
        request, source_id = fixture.correction_request(b)
        fixture.bridge.prepare_lab_correction(b["id"], request, source_id)
        prior = fixture.bridge.scoped_export(b["id"])
        actions = ResearchActionStore(self.root)
        action = actions.plan({"idempotency_key": "quote", "goal": {"id": "g", "revision": 1, "question": "Locate exact text", "completion_criterion": "Exact occurrence only"},
                               "source": {"uri": "provided:test", "text": "A measured value.", "coverage": "excerpt", "missing_sections": ["rest"]}, "quote": "measured"})
        actions.run(action["action_id"])
        library = SourceLibrary(self.root / "library")
        source = {"id": "paper_fixture", "content_hash": "a" * 64, "parser_version": PARSER_VERSION, "text": "Retained parsed original text " * 8,
                  "title": "Fixture source", "url": "https://example.org/paper", "retrieved_at": "2026-01-01T00:00:00Z", "access": "partial"}
        self.assertTrue(library.acquire(source["url"], "owner"))
        library.publish(source["url"], "owner", source)
        cache = LiteratureEvidenceCache(self.root / "evidence-cache", self.root / "ideas", namespace="fixture-admin")
        with backup._read(fixture.ideas.path) as db:
            attempt = json.loads(db.execute("SELECT data FROM stage_attempts WHERE session_id=? AND stage='literature' ORDER BY rowid LIMIT 1", (old["id"],)).fetchone()[0])
        descriptor = {"fixture": "accepted scoped artifact"}
        accepted = {"artifact_hash": attempt["output_hash"], "output": attempt["output"], "origin": {"session_id": old["id"], "generation_id": old["generation_id"], "round": 1,
                    "attempt_id": attempt["id"], "packet_digest": attempt["input_ref"]["packet_digest"]}}
        with closing(sqlite3.connect(cache.path)) as db, db:
            db.execute("INSERT INTO entries VALUES (?,?,?,?,?,?,?,?)", (backup._sha(backup._json(descriptor)), "fixture-admin", backup._json(descriptor).decode(), 1, None, None, backup._json(accepted).decode(), None))
        manifest = self.manifest()
        self.assertEqual(set(report["kind"] for report in manifest["databases"].values()), {"lab", "ideas", "corrections", "marks", "library", "cache", "actions"})
        self.assertEqual(manifest["checks"]["cache_entries"], 1)
        backup.restore_research(self.bundle, self.restored)
        restored = ResearchDependencies(self.restored / "dependencies", self.restored / "ideas", self.restored / "lab")
        self.assertEqual(restored.scoped_export(b["id"]), prior)
        self.assertFalse(restored.snapshot(b["id"])["run_allowed"])
        for relative, report in manifest["databases"].items():
            self.assertEqual(backup._file_hash(self.restored / relative), report["sha256"])

    def test_partial_dependency_pair_rejected(self):
        deps = ResearchDependencies(self.root / "dependencies", self.root / "ideas", self.root / "lab")
        deps.consumer.unlink()
        self.error("integrity_error", self.manifest)

    def test_corrupt_cas_is_rejected_even_when_database_is_integral(self):
        with closing(sqlite3.connect(self.lab.path)) as db, db:
            db.execute("UPDATE artifacts SET data=? WHERE digest=?", (b'{}', self.item["input_artifact"]))
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.error("integrity_error", self.manifest)
        self.assertFalse(self.bundle.exists())

    def test_cli_verify_restore_and_permissions(self):
        script = Path(__file__).resolve().parents[1] / "research_backup.py"
        for command in (["backup", str(self.root), str(self.bundle)], ["verify", str(self.bundle)], ["restore", str(self.bundle), str(self.restored)]):
            result = subprocess.run([sys.executable, str(script), *command], cwd=self.base, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            json.loads(result.stdout)
        for path in self.restored.rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        self.assertEqual(self.lab.get(self.item["id"]), self.item)

    def test_size_limit_rejects_complete_backup_without_partial_archive(self):
        with patch.object(backup, "MAX_BYTES", 100):
            self.error("too_large", self.manifest)
        self.assertFalse(self.bundle.exists())

    def test_complete_history_is_not_limited_by_scoped_json_export_cap(self):
        item = self.item
        for index in range(100):
            item = self.lab.decide(item["id"], {"expected_revision": item["revision"], "idempotency_key": f"defer-{index}",
                                              "kind": "defer", "feedback": "q" * 6900 + str(index)})
        with backup._read(self.lab.path) as db:
            size = db.execute("SELECT sum(length(payload)) FROM versions").fetchone()[0]
            size += db.execute("SELECT sum(length(response)) FROM mutations").fetchone()[0]
        self.assertGreater(size, 16 * 1024**2)
        manifest = self.manifest()
        self.assertEqual(manifest["databases"]["lab/lab.sqlite3"]["tables"]["versions"]["rows"], 101)
        backup.restore_research(self.bundle, self.restored)
        self.assertEqual(ResearchLabStore(self.restored / "lab").get(item["id"]), item)

    def test_oversized_rejected_idea_preserves_diagnostic_hash_and_usage_without_body(self):
        ideas = IdeaStore(self.root / "ideas")
        item = ideas.create({"idempotency_key": "oversized", "brief": brief()})
        item, _ = ideas.begin(item["id"], {"expected_revision": item["revision"], "idempotency_key": "start"})
        answer = generated("literature")
        answer["output"]["summary"] = "x" * 150000
        answer["usage"] = {"total_tokens": 12345}
        with self.assertRaises(IdeaError):
            ideas.save_stage(item["id"], item["generation_id"], "literature", answer)
        with backup._read(ideas.path) as db:
            prior = db.execute("SELECT data FROM stage_attempts").fetchone()[0]
        manifest = self.manifest()
        self.assertEqual(manifest["checks"]["idea_unretained_attempts"], 1)
        backup.restore_research(self.bundle, self.restored)
        with backup._read(self.restored / "ideas/ideas.sqlite3") as db:
            restored = db.execute("SELECT data FROM stage_attempts").fetchone()[0]
        self.assertEqual(restored, prior)
        record = json.loads(restored)
        self.assertFalse(record["output_retained"])
        self.assertIsNone(record["output"])
        self.assertEqual(record["usage"]["total_tokens"], 12345)

    def test_optional_raw_archive_preserves_old_missing_and_failed_fetch_bytes(self):
        library = SourceLibrary(self.root / "library")
        archive = RawSourceArchive(self.root / "library")
        def publish(identity, body, reference=None):
            source = {"id": identity, "content_hash": hashlib.sha256(body).hexdigest(), "parser_version": PARSER_VERSION,
                      "text": "Parsed text only", "title": "Fixture", "url": "https://example.org/" + identity,
                      "retrieved_at": "2026-01-01T00:00:00Z", "access": "partial"}
            if reference:
                source["raw_document"] = reference
            library.acquire(source["url"], "owner")
            library.publish(source["url"], "owner", source)
        old_bytes = b"old raw HTML bytes"
        publish("old-version", old_bytes)
        old_ref = archive.retain(old_bytes, requested_url="https://example.org/old-version", final_url="https://example.org/old-version", mime="text/html", parse_version=PARSER_VERSION)
        archive.finish_parse(old_ref["fetch_id"], status="parsed", parse_key="not-published-under-expired-lease")
        body = b"<html>Fresh fixture source</html>"
        reference = archive.retain(body, requested_url="https://example.org/new-version", final_url="https://example.org/new-version", mime="text/html", parse_version=PARSER_VERSION)
        archive.finish_parse(reference["fetch_id"], status="parsed", parse_key="new-version")
        publish("new-version", body, reference)
        failed = archive.retain(b"unparsable fixture", requested_url="https://example.org/failed", final_url="https://example.org/failed", mime="application/pdf", parse_version=PARSER_VERSION)
        archive.finish_parse(failed["fetch_id"], status="failed", error="invalid_pdf")
        manifest = self.manifest()
        self.assertEqual(manifest["checks"]["raw_sources"], {"retained_blobs": 3, "retained_bytes": len(old_bytes) + len(body) + len(b"unparsable fixture"),
                                                           "fetch_receipts": 3, "versions_retained": 1, "versions_not_retained": 1})
        backup.restore_research(self.bundle, self.restored)
        with backup._read(self.restored / "library/sources.sqlite3") as db:
            self.assertEqual(db.execute("SELECT body FROM raw_sources WHERE digest=?", (reference["sha256"],)).fetchone()[0], body)
            self.assertEqual(db.execute("SELECT parse_status FROM raw_fetches WHERE id=?", (failed["fetch_id"],)).fetchone()[0], "failed")
            self.assertNotIn("raw_document", json.loads(db.execute("SELECT data FROM versions WHERE key='old-version'").fetchone()[0]))

    def test_raw_only_inflight_archive_supported_and_corrupt_bytes_rejected(self):
        archive = RawSourceArchive(self.root / "library")
        reference = archive.retain(b"download complete parse pending", requested_url="https://example.org/x", final_url="https://example.org/x", mime="text/html", parse_version="fixture-v1")
        self.manifest()
        backup.restore_research(self.bundle, self.restored)
        with backup._read(self.restored / "library/sources.sqlite3") as db:
            self.assertEqual(db.execute("SELECT parse_status FROM raw_fetches").fetchone()[0], "downloaded")
        self.bundle = self.base / "corrupt-bundle"
        with closing(sqlite3.connect(archive.path)) as db, db:
            db.execute("UPDATE raw_sources SET body=? WHERE digest=?", (b"corrupted", reference["sha256"]))
        self.error("integrity_error", self.manifest)

    def test_raw_blob_size_checked_before_python_materialization(self):
        archive = RawSourceArchive(self.root / "library")
        with closing(sqlite3.connect(archive.path)) as db, db:
            db.execute("INSERT INTO raw_sources VALUES (?,zeroblob(?),?,?)", ("a" * 64, backup.MAX_RAW_BYTES + 1, backup.MAX_RAW_BYTES + 1, "fixture"))
        with patch.object(backup, "_cell", side_effect=AssertionError("Oversized BLOB must be rejected before table materialization")):
            self.error("integrity_error", lambda: backup._database_report(archive.path, "library"))

    def raw_idea(self):
        archive = RawSourceArchive(self.root / "library")
        body = b"<html>Raw bytes differ from the projected model text.</html>"
        ref = archive.retain(body, requested_url="https://example.org/raw", final_url="https://example.org/raw",
                             mime="text/html", parse_version=PARSER_VERSION)
        archive.finish_parse(ref["fetch_id"], status="parsed", parse_key="not-published-parsed-version")
        source = copy.deepcopy(brief()["sources"][0])
        source["coverage"] = {"kind": "partial", "limitations": ["Fixture projected excerpt"]}
        source["provenance"] = {"content_hash": ref["sha256"], "raw_document": ref, "parser_version": PARSER_VERSION}
        ideas = IdeaStore(self.root / "ideas")
        item = ideas.create({"idempotency_key": "raw-idea", "brief": brief()})
        item, _ = ideas.begin(item["id"], {"expected_revision": item["revision"], "idempotency_key": "raw-start"}, full_research=True)
        ideas.retrieval(item["id"], item["generation_id"], {"sources": [source], "papers": [
            {"id": source["id"], "status": "read", "content_hash": ref["sha256"], "raw_document": ref}], "coverage_gaps": []})
        ideas.freeze_packet(item["id"], item["generation_id"], "literature", [source], {})
        for stage in STAGES:
            ideas.work(item["id"], item["generation_id"], stage, model=True)
            item = ideas.save_stage(item["id"], item["generation_id"], stage, generated(stage))
        item = ideas.finish_research(item["id"], item["generation_id"], "ready_for_choice", "Fixture only")
        item = ideas.decision(item["id"], {"expected_revision": item["revision"], "kind": "select", "selected_id": "d1", "feedback": "Inspect frozen source"})
        return ideas, item, source, archive, body

    def test_raw_historical_idea_frozen_packet_and_authoritative_import_restore_exactly(self):
        ideas, item, source, archive, body = self.raw_idea()
        importer = IdeaImport(ideas)
        seed = importer.seed(item["id"])
        request = {**source_request("raw-import"), **{k: seed[k] for k in ("origin", "brief", "inputs")}}
        campaign = self.lab.create(request, resolve_origin=importer.resolve)
        self.assertNotEqual(source["provenance"]["content_hash"], hashlib.sha256(source["text"].encode()).hexdigest())
        prior = ideas.get(item["id"])
        next_item = ideas.followup(item["id"], {"expected_revision": item["revision"], "idempotency_key": "next-research", "mode": "research", "feedback": "Check new material"})
        ideas.begin(item["id"], {"expected_revision": next_item["revision"], "idempotency_key": "new-generation"}, full_research=True)
        self.assertNotIn("provenance", ideas.get(item["id"])["research"]["sources"][0])
        self.manifest()
        backup.restore_research(self.bundle, self.restored)
        with backup._read(self.restored / "ideas/ideas.sqlite3") as db:
            self.assertEqual(json.loads(db.execute("SELECT data FROM versions WHERE session_id=? AND revision=?", (item["id"], prior["revision"])).fetchone()[0]), prior)
        self.assertEqual(ResearchLabStore(self.restored / "lab").get(campaign["id"]), campaign)
        retained = RawSourceArchive(self.restored / "library").read(source["provenance"]["raw_document"], include_body=True)
        self.assertEqual(base64.b64decode(retained["body_base64"]), body)

    def test_historical_only_idea_raw_reference_missing_fetch_rejects_backup(self):
        ideas, item, source, archive, _ = self.raw_idea()
        with closing(sqlite3.connect(ideas.path)) as db, db:
            row = db.execute("SELECT revision,data FROM versions WHERE session_id=? AND revision<? AND json_array_length(data,'$.research.papers')>0 LIMIT 1", (item["id"], item["revision"])).fetchone()
            value = json.loads(row[1])
            value["research"]["papers"][0]["raw_document"]["fetch_id"] = "0" * 32
            db.execute("UPDATE versions SET data=? WHERE session_id=? AND revision=?", (json.dumps(value), item["id"], row[0]))
        self.error("integrity_error", self.manifest)
        self.assertFalse(self.bundle.exists())

    def test_new_frozen_packet_only_raw_reference_rejects_missing_archive(self):
        ideas = IdeaStore(self.root / "ideas")
        item = ideas.create({"idempotency_key": "packet-only", "brief": brief()})
        item, _ = ideas.begin(item["id"], {"expected_revision": 1, "idempotency_key": "start"}, full_research=True)
        source = copy.deepcopy(brief()["sources"][0])
        source["provenance"] = {"content_hash": "a" * 64, "raw_document": {
            "status": "retained", "sha256": "a" * 64, "bytes": 12, "fetch_id": "b" * 32, "mime": "text/html"}}
        ideas.freeze_packet(item["id"], item["generation_id"], "literature", [source], {})
        self.error("integrity_error", self.manifest)
        self.assertFalse(self.bundle.exists())

    def test_failed_alternate_raw_fetch_without_source_packet_preserved_and_verified(self):
        archive = RawSourceArchive(self.root / "library")
        ref = archive.retain(b"invalid PDF fixture", requested_url="https://example.org/failure", final_url="https://example.org/failure",
                             mime="application/pdf", parse_version=PARSER_VERSION)
        archive.finish_parse(ref["fetch_id"], status="failed", error="invalid_pdf")
        ideas = IdeaStore(self.root / "ideas")
        item = ideas.create({"idempotency_key": "failed-read", "brief": brief()})
        item, _ = ideas.begin(item["id"], {"expected_revision": 1, "idempotency_key": "start"}, full_research=True)
        paper = {"id": "failed", "status": "unavailable", "raw_fetches": [
            {"url": "https://example.org/failure", "content_hash": ref["sha256"], "raw_document": ref}]}
        item = ideas.retrieval(item["id"], item["generation_id"], {"sources": [], "papers": [paper], "coverage_gaps": ["Parse failed"]})
        self.manifest()
        backup.restore_research(self.bundle, self.restored)
        with backup._read(self.restored / "ideas/ideas.sqlite3") as db:
            self.assertEqual(json.loads(db.execute("SELECT data FROM sessions").fetchone()[0])["research"]["papers"], [paper])
        self.bundle = self.base / "invalid-alternate"
        with closing(sqlite3.connect(archive.path)) as db, db:
            db.execute("UPDATE raw_fetches SET mime='text/plain'")
        self.error("integrity_error", self.manifest)

    def test_same_parsed_version_new_url_receipt_retained_without_upgrading_legacy(self):
        library = SourceLibrary(self.root / "library")
        archive = RawSourceArchive(self.root / "library")
        body = b"Identical HTML fetched through two URLs"
        value = {"id": "shared-version", "content_hash": hashlib.sha256(body).hexdigest(), "parser_version": PARSER_VERSION,
                 "text": "Parsed fixture", "title": "Fixture", "url": "https://example.org/old", "retrieved_at": "fixture", "access": "partial"}
        library.acquire(value["url"], "old"); library.publish(value["url"], "old", value)
        ref = archive.retain(body, requested_url="https://example.org/new", final_url="https://example.org/new", mime="text/html", parse_version=PARSER_VERSION)
        archive.finish_parse(ref["fetch_id"], status="parsed", parse_key=value["id"])
        fresh = {**value, "url": "https://example.org/new", "raw_document": ref}
        library.acquire(fresh["url"], "new"); library.publish(fresh["url"], "new", fresh)
        manifest = self.manifest()
        self.assertEqual(manifest["checks"]["raw_sources"]["versions_not_retained"], 1)
        self.assertEqual(manifest["checks"]["raw_sources"]["versions_retained"], 0)
        self.assertNotIn("raw_document", library.version(value["id"]))
        self.bundle = self.base / "invalid-url-receipt"
        fresh["raw_document"] = {**ref, "bytes": ref["bytes"] + 1}
        with closing(sqlite3.connect(library.path)) as db, db:
            db.execute("UPDATE urls SET receipt=? WHERE url=?", (json.dumps(fresh), fresh["url"]))
        self.error("integrity_error", self.manifest)

    def test_untrusted_metadata_is_not_raw_authority_and_import_must_match_packet(self):
        request = source_request("untrusted")
        request["inputs"]["sources"][0]["provenance"] = {"kind": "idea_frozen_packet", "original_provenance": {
            "content_hash": "a" * 64, "raw_document": {"status": "retained", "fetch_id": "missing"}}}
        self.lab.create(request)
        self.manifest()  # Client metadata is retained as unverified, never dereferenced.
        self.bundle = self.base / "forged-service-import"
        ideas, item, source, archive, _ = self.raw_idea()
        seed = IdeaImport(ideas).seed(item["id"])
        request = {**source_request("forged"), **{k: seed[k] for k in ("origin", "brief", "inputs")}}
        request["inputs"]["sources"][0]["provenance"]["original_provenance"]["raw_document"]["mime"] = "different/type"
        # Simulate a faulty service resolver, not a permitted renderer override.
        self.lab.create(request, resolve_origin=lambda body: body)
        self.error("integrity_error", self.manifest)

    def test_completed_and_invalid_native_raw_receipts_remain_distinct_and_replayable(self):
        models = ResearchModelJobs(self.root / "model-jobs")
        def prepare(key):
            return models.prepare({"campaign_id": self.item["id"], "expected_revision": 1, "role": "planner", "idempotency_key": key}, resolve_snapshot=self.lab.get)
        completed = prepare("complete")
        packet = models.artifact(completed["id"], completed["packet_ref"])["content"]
        models._admit(completed["id"], 1, "fixture-model")
        provider = {"backend": "fixture", "resolved_model": "fixture-model", "model_resolution": "thread_start"}
        done = models._finish(completed["id"], "completed", revision=1,
                              generated={"output": output_for("planner", packet), "usage": {"total_tokens": 77, "complete": True}, "provider": provider})
        self.assertEqual(done["status"], "completed")
        rejected = prepare("invalid")
        models._admit(rejected["id"], 1, "fixture-model")
        failed = models._finish(rejected["id"], "completed", revision=1,
                                generated={"output": {"invalid": "retained original"}, "usage": {"total_tokens": 91, "complete": True}, "provider": provider})
        self.assertEqual(failed["status"], "failed")
        self.assertIsNotNone(failed["raw_output_ref"])
        self.assertIsNone(failed["output_ref"])
        self.manifest()
        backup.restore_research(self.bundle, self.restored)
        restored = ResearchModelJobs(self.restored / "model-jobs")
        self.assertEqual(restored.get(done["id"]), done)
        self.assertEqual(restored.get(failed["id"]), failed)
        self.assertEqual(restored.list(self.item["id"])["quota"]["used"], 2)
        self.assertEqual(restored.artifact(failed["id"], failed["raw_output_ref"])["content"], {"invalid": "retained original"})


if __name__ == "__main__":
    unittest.main()
