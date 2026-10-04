"""F502: immutable Idea replay and ownership, temporary stores only."""
import json
import sqlite3
import sys
import unittest
from contextlib import closing
from unittest.mock import patch

import research_backup as backup
from idea_lab import IdeaStore, IdeaError
from tests import test_research_backup as fixture
from tests.test_idea_lab import brief, generated


@unittest.skipUnless(sys.platform.startswith("linux"), "Offline backup uses flock/renameat2")
class IdeaReferenceTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.ResearchBackupTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.store = IdeaStore(self.case.root / "ideas")
        self.item = self.store.create({"idempotency_key": "idea", "brief": brief()})

    def mutate(self, sql, values=()):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute(sql, values)

    def fail_backup(self):
        with self.assertRaises(backup.BackupError) as caught:
            self.case.manifest()
        self.assertEqual(caught.exception.code, "integrity_error")
        self.assertFalse(self.case.bundle.exists())
        self.assertFalse(self.case.restored.exists())

    def followup(self, key):
        body = {"expected_revision": self.item["revision"], "idempotency_key": key,
                "mode": "research", "feedback": "Exact human feedback " + key}
        self.item = self.store.followup(self.item["id"], body)
        return body

    def start(self):
        self.start_body = {"idempotency_key": "generate", "expected_revision": self.item["revision"]}
        self.item, _ = self.store.begin(self.item["id"], self.start_body, full_research=True)
        self.gid = self.item["generation_id"]
        self.store.work(self.item["id"], self.gid, "literature", model=True, new_round=True)
        self.store.freeze_packet(self.item["id"], self.gid, "literature", brief()["sources"], {})

    def reject_for_repair(self):
        value = generated("literature")
        value["output"]["evidence"][0]["quote"] = "This does not appear in the source."
        self.item = self.store.save_stage(self.item["id"], self.gid, "literature", value, allow_repair=True)
        return self.item["research"]["pending_repair"]["attempt_id"]

    def test_followup_replay_missing_revision_is_rejected_before_publication(self):
        self.followup("first")
        missing = self.item["revision"]
        self.followup("next")
        self.mutate("DELETE FROM versions WHERE session_id=? AND revision=?", (self.item["id"], missing))
        self.fail_backup()

    def test_followup_replay_wrong_existing_operation_is_rejected(self):
        self.followup("first")
        self.mutate("UPDATE followups SET revision=1")
        self.fail_backup()

    def test_history_identity_and_gap_are_checked_without_a_replay_row(self):
        self.followup("first")
        self.followup("next")
        self.mutate("DELETE FROM followups")
        self.mutate("DELETE FROM versions WHERE revision=2")
        self.fail_backup()

    def test_missing_generation_referenced_only_by_old_history_is_rejected(self):
        self.start()
        old = self.gid
        self.store.stop(self.item["id"], old, "interrupted")
        self.item = self.store.get(self.item["id"])
        self.store.begin(self.item["id"], {"expected_revision": self.item["revision"], "idempotency_key": "next"})
        self.mutate("DELETE FROM stage_packets WHERE generation_id=?", (old,))
        self.mutate("DELETE FROM generations WHERE id=?", (old,))
        self.fail_backup()

    def test_generation_cannot_be_reassigned_to_another_session(self):
        self.start()
        other = self.store.create({"idempotency_key": "other", "brief": brief()})
        self.mutate("UPDATE generations SET session_id=? WHERE id=?", (other["id"], self.gid))
        self.fail_backup()

    def test_frozen_packet_referenced_by_history_cannot_be_removed(self):
        self.start()
        self.mutate("DELETE FROM stage_packets")
        self.fail_backup()

    def test_attempt_missing_or_reassigned_is_rejected(self):
        self.start()
        aid = self.reject_for_repair()
        self.mutate("DELETE FROM stage_attempts WHERE id=?", (aid,))
        self.fail_backup()

    def test_repair_lineage_cannot_point_to_another_attempt(self):
        self.start()
        aid = self.reject_for_repair()
        self.store.work(self.item["id"], self.gid, "literature", model=True)
        self.store.save_stage(self.item["id"], self.gid, "literature", generated("literature"))
        # Modify only the private artifact so all identity columns still match.
        self.mutate("UPDATE stage_attempts SET data=json_set(data,'$.repair_of',?) WHERE id<>?",
                    ("0" * 32, aid))
        self.fail_backup()

    def test_live_activity_tail_and_pending_repair_are_preserved_without_recovery(self):
        self.start()
        self.reject_for_repair()
        self.store.activity(self.item["id"], self.gid, {"agent": "literature", "type": "provider_task_started", "task": "literature"})
        before = self.store.get(self.item["id"])
        self.case.manifest()
        backup.restore_research(self.case.bundle, self.case.restored)
        # Do not instantiate a recovering controller. Store construction itself
        # performs no generation recovery and must retain the entire ledger.
        restored = IdeaStore(self.case.restored / "ideas")
        self.assertEqual(restored.get(self.item["id"]), before)
        self.assertEqual(before["status"], "running")
        self.assertIsNotNone(before["research"]["pending_repair"])

    def test_activity_payload_owner_is_checked_even_after_leaving_snapshot_tail(self):
        self.start()
        for _ in range(81):
            self.store.activity(self.item["id"], self.gid, {"agent": "literature", "type": "receipt"})
        self.mutate("UPDATE activities SET data=json_set(data,'$.generation_id','missing') WHERE seq=1")
        self.fail_backup()

    def test_restore_and_verify_reject_tampered_replay_even_with_updated_file_checksums(self):
        self.followup("first")
        self.followup("second")
        self.case.manifest()
        path = self.case.bundle / "databases/ideas/ideas.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("DELETE FROM versions WHERE revision=2")
        manifest_path = self.case.bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["databases"]["ideas/ideas.sqlite3"] = backup._database_report(path, "ideas")
        manifest_path.write_text(json.dumps(manifest))
        for operation in (lambda: backup.verify_research_backup(self.case.bundle),
                          lambda: backup.restore_research(self.case.bundle, self.case.restored)):
            with self.assertRaises(backup.BackupError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "integrity_error")
        self.assertFalse(self.case.restored.exists())

    def test_valid_replay_and_schema_one_without_optional_tables_remain_supported(self):
        first = self.followup("first")
        saved = self.item
        self.followup("second")
        self.case.manifest()
        backup.restore_research(self.case.bundle, self.case.restored)
        restored = IdeaStore(self.case.restored / "ideas")
        self.assertEqual(restored.followup(saved["id"], first), saved)
        # A separate legacy database retains its original rows and schema.
        legacy = self.case.base / "legacy"
        with backup.research_service_guard(legacy):
            store = IdeaStore(legacy / "ideas")
            item = store.create({"idempotency_key": "legacy", "brief": brief()})
        with closing(sqlite3.connect(store.path)) as db, db:
            for table in backup.OPTIONAL_IDEA_TABLES:
                db.execute('DROP TABLE "' + table + '"')
            db.execute("PRAGMA user_version=1")
        result = backup.backup_research(legacy, self.case.base / "legacy-bundle")
        self.assertEqual(result["checks"]["idea_sessions"], 1)


if __name__ == "__main__":
    unittest.main()
