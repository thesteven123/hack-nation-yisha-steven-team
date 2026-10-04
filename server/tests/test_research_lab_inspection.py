import base64
import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from research_lab import LabError, ResearchLabStore
from research_lab_inspection import ResearchLabInspector, restore_campaign, verify_bundle
from tests.test_research_lab import numeric_request, source_request


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def resign(bundle):
    bundle["manifest"]["table_hashes"] = {k: digest(v) for k, v in bundle["data"].items()}
    bundle["bundle_sha256"] = digest({k: v for k, v in bundle.items() if k != "bundle_sha256"})
    return bundle


class InspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = ResearchLabStore(self.root / "source")
        self.inspect = ResearchLabInspector(self.store.root)

    def tearDown(self):
        self.temp.cleanup()

    def run_one(self):
        created = self.store.create(numeric_request())
        request = {"idempotency_key": "run-once", "expected_revision": created["revision"]}
        result = self.store.run(created["id"], request)
        return created, request, result

    def test_inspection_missing_store_never_creates_it(self):
        root = self.root / "missing"
        with self.assertRaisesRegex(LabError, "does not exist"):
            ResearchLabInspector(root).list()
        self.assertFalse(root.exists())

    def test_list_equal_timestamp_keyset_and_compact_payload(self):
        with patch("research_lab._now", return_value="2026-10-03T00:00:00+00:00"):
            ids = {self.store.create(numeric_request(str(i)))["id"] for i in range(5)}
        seen, before = [], None
        while True:
            page = self.inspect.list(before, 2)
            seen.extend(x["id"] for x in page["items"])
            self.assertTrue(all(x["rounds"] == [] and x["comparisons"] == [] and x["events"] == [] and x["current_plan"] is None for x in page["items"]))
            if not page["has_more"]:
                self.assertIsNone(page["next_cursor"])
                break
            before = page["next_cursor"]
        self.assertEqual(set(seen), ids)
        self.assertEqual(len(seen), len(ids))

    def test_history_page_scope_and_new_revision(self):
        item = self.store.create(numeric_request())
        for i in range(4):
            item = self.store.decide(item["id"], {"kind": "revise", "goal": str(i), "feedback": "",
                "idempotency_key": str(i), "expected_revision": item["revision"]})
        first = self.inspect.history(item["id"], limit=2)
        self.assertEqual([r["revision"] for r in first["items"]], [5, 4])
        other = self.store.create(numeric_request("other"))
        with self.assertRaisesRegex(LabError, "scoped"):
            self.inspect.history(other["id"], first["next_cursor"])
        self.store.decide(item["id"], {"kind": "stop", "feedback": "done", "idempotency_key": "stop", "expected_revision": 5})
        next_page = self.inspect.history(item["id"], first["next_cursor"], 2)
        self.assertEqual([r["revision"] for r in next_page["items"]], [3, 2])
        final = self.inspect.history(item["id"], next_page["next_cursor"], 2)
        self.assertEqual([r["revision"] for r in final["items"]], [1])
        self.assertFalse(final["has_more"])

    def test_bad_cursors_and_limits_fail_closed(self):
        for value in ("!", "a" * 1025, "e30", 123):
            with self.assertRaises(LabError):
                self.inspect.list(value)
        for value in (0, 51, True, "10"):
            with self.assertRaises(LabError):
                self.inspect.list(limit=value)

    def test_history_full_event_artifact_resolves_and_missing_reference_fails_export_verification(self):
        item = self.store.create(numeric_request())
        revised = self.store.decide(item["id"], {"kind": "revise", "goal": "A new objective", "feedback": "Exact decision rationale",
            "expected_revision": item["revision"], "idempotency_key": "event-history"})
        entry = self.inspect.history(item["id"], limit=1)["items"][0]
        self.assertEqual(entry["event_artifact"], revised["events"][-1]["artifact"])
        artifact = self.store.artifact(item["id"], entry["event_artifact"])["content"]
        self.assertEqual(artifact["id"], revised["events"][-1]["id"])
        bundle = self.inspect.export_campaign(item["id"])
        self.assertTrue(verify_bundle(bundle)["verified"])
        bundle["data"]["artifacts"] = [a for a in bundle["data"]["artifacts"] if a["sha256"] != entry["event_artifact"]]
        with self.assertRaisesRegex(LabError, "artifact"):
            verify_bundle(resign(bundle))

    def test_dispatch_and_selected_decision_artifacts_are_required_in_complete_bundle(self):
        item = self.store.create(numeric_request())
        chosen = self.store.decide(item["id"], {"expected_revision": item["revision"], "idempotency_key": "select-before-export",
            "kind": "select", "feedback": "Inspect this frozen candidate", "selected_action_id": item["current_plan"]["candidates"][0]["id"]})
        result = self.store.run(item["id"], {"expected_revision": chosen["revision"], "idempotency_key": "run-export"})
        bundle = self.inspect.export_campaign(item["id"])
        for digest in (result["rounds"][0]["run"]["dispatch_artifact"], chosen["current_plan"]["decision_artifact"]):
            altered = copy.deepcopy(bundle)
            altered["data"]["artifacts"] = [a for a in altered["data"]["artifacts"] if a["sha256"] != digest]
            with self.assertRaisesRegex(LabError, "missing"):
                verify_bundle(resign(altered))

    def test_byte_limit_returns_continuation_without_omitting_an_entry(self):
        for i in range(3):
            self.store.create(numeric_request(str(i)))
        page = self.inspect.list(limit=1)
        size = len(json.dumps(page["items"][0], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        with patch("research_lab_inspection.MAX_PAGE_BYTES", size + 10):
            first = self.inspect.list()
            self.assertEqual(len(first["items"]), 1)
            self.assertTrue(first["has_more"])
            second = self.inspect.list(first["next_cursor"])
            self.assertNotEqual(first["items"][0]["id"], second["items"][0]["id"])

    def test_consistent_scoped_export_restore_and_exact_replay(self):
        created, request, result = self.run_one()
        other = self.store.create(source_request("private-other"))
        before = hashlib.sha256(self.store.path.read_bytes()).hexdigest()
        bundle = self.inspect.export_campaign(created["id"])
        encoded = json.dumps(bundle)
        self.assertNotIn(other["id"], encoded)
        self.assertNotIn("No relevant passage.", encoded)
        report = verify_bundle(bundle)
        self.assertEqual(report["runs"], 1)
        self.assertEqual(report["revision"], 2)
        self.assertEqual(before, hashlib.sha256(self.store.path.read_bytes()).hexdigest())
        target = self.root / "isolated-restore"
        restore_campaign(bundle, target)
        restored = ResearchLabStore(target)
        self.assertEqual(restored.get(created["id"]), result)
        self.assertEqual(restored.run(created["id"], request), result)
        self.assertEqual(len(restored.get(created["id"])["rounds"]), 1)
        self.assertEqual(ResearchLabInspector(target).export_campaign(created["id"])["bundle_sha256"], bundle["bundle_sha256"])
        self.assertEqual(len(restored.list()["items"]), 1)

    def test_complete_export_includes_old_corrected_inputs_and_replay_versions(self):
        created, _, result = self.run_one()
        old = result["input_artifact"]
        inputs = numeric_request()["inputs"]
        inputs["treatment"][0] = 99
        result = self.store.correct_inputs(created["id"], {"idempotency_key": "correct", "expected_revision": 2,
            "inputs": inputs, "reason": "Fix transcription"})
        bundle = self.inspect.export_campaign(created["id"])
        self.assertIn(old, {a["sha256"] for a in bundle["data"]["artifacts"]})
        restore_campaign(bundle, self.root / "copy")
        self.assertEqual(ResearchLabStore(self.root / "copy").get(created["id"]), result)

    def test_artifact_corruption_rejected_even_after_outer_hash_recomputed(self):
        created, _, _ = self.run_one()
        bundle = self.inspect.export_campaign(created["id"])
        bundle["data"]["artifacts"][0]["data"] = base64.b64encode(b"{}").decode()
        with self.assertRaisesRegex(LabError, "immutable digest"):
            verify_bundle(resign(bundle))
        self.assertFalse((self.root / "bad").exists())

    def test_missing_version_artifact_or_replay_fails_complete_backup_validation(self):
        created, _, _ = self.run_one()
        original = self.inspect.export_campaign(created["id"])
        for key in ("versions", "artifacts", "mutations", "events"):
            bundle = copy.deepcopy(original)
            if key == "artifacts":
                wanted = original["data"]["campaign"]["input_artifact"]
                bundle["data"][key] = [a for a in bundle["data"][key] if a["sha256"] != wanted]
            else:
                bundle["data"][key].pop()
            with self.subTest(key=key), self.assertRaises(LabError):
                verify_bundle(resign(bundle))

    def test_replay_set_must_cover_every_committed_revision(self):
        created, _, _ = self.run_one()
        bundle = self.inspect.export_campaign(created["id"])
        prior = bundle["data"]["versions"][0]["payload"]
        for mutation in bundle["data"]["mutations"]:
            mutation["response"] = copy.deepcopy(prior)
        with self.assertRaises(LabError):
            verify_bundle(resign(bundle))

    def test_user_provenance_keys_are_not_misread_as_internal_artifact_refs(self):
        request = source_request()
        request["inputs"]["sources"][0]["provenance"] = {"input_refs": ["external-paper-ref"]}
        item = self.store.create(request)
        bundle = self.inspect.export_campaign(item["id"])
        self.assertTrue(verify_bundle(bundle)["verified"])

    def test_permission_expansion_and_cross_campaign_state_fail_restore(self):
        created, _, _ = self.run_one()
        bundle = self.inspect.export_campaign(created["id"])
        bad = copy.deepcopy(bundle)
        bad["data"]["campaign"]["brief"]["authorized_actions"].append("shell")
        # Synchronize snapshots to prove the permission guard, not just the hash,
        # blocks expanded authority in a coherently rehashed import.
        bad["data"]["versions"][-1]["payload"] = copy.deepcopy(bad["data"]["campaign"])
        for mutation in bad["data"]["mutations"]:
            if mutation["response"]["revision"] == 2:
                mutation["response"] = copy.deepcopy(bad["data"]["campaign"])
        with self.assertRaisesRegex(LabError, "permissions"):
            restore_campaign(resign(bad), self.root / "unsafe")
        self.assertFalse((self.root / "unsafe").exists())
        bad = copy.deepcopy(bundle)
        bad["data"]["mutations"][0]["scope"] = "another-campaign"
        with self.assertRaises(LabError):
            verify_bundle(resign(bad))

    def test_restore_never_overwrites_or_merges_existing_directory(self):
        created, _, _ = self.run_one()
        bundle = self.inspect.export_campaign(created["id"])
        target = self.root / "occupied"
        target.mkdir()
        marker = target / "keep.txt"
        marker.write_text("preserve")
        with self.assertRaisesRegex(LabError, "empty isolated"):
            restore_campaign(bundle, target)
        self.assertEqual(marker.read_text(), "preserve")
        with self.assertRaises(LabError):
            restore_campaign(bundle, self.store.root)

    def test_symlinked_source_or_restore_ancestor_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.store.root, target_is_directory=True)
        with self.assertRaisesRegex(LabError, "symlinks"):
            ResearchLabInspector(alias).list()
        item = self.store.create(numeric_request())
        bundle = self.inspect.export_campaign(item["id"])
        with self.assertRaisesRegex(LabError, "symlinks"):
            restore_campaign(bundle, alias / "nested")

    def test_oversized_export_explicitly_fails_without_partial_bundle(self):
        created, _, _ = self.run_one()
        with patch("research_lab_inspection.MAX_BUNDLE_BYTES", 500):
            with self.assertRaisesRegex(LabError, "limit"):
                self.inspect.export_campaign(created["id"])

    def test_protocol_content_is_versioned_summary_with_real_source_metadata(self):
        for name in ("planning-v0.5", "records-v0.5", "execution-v0.5", "analysis-review-v0.5", "literature-cache-v0.5"):
            protocol = self.inspect.protocol(name)
            self.assertFalse(protocol["is_full_source"])
            self.assertGreater(len(protocol["content"]), 250)
            self.assertEqual(protocol["summary_sha256"], digest({k: v for k, v in protocol.items() if k != "summary_sha256"}))
            self.assertTrue(protocol["source"]["url"].startswith("https://chatgpt.com/space/page_"))
            self.assertTrue(all(len(b["hash"]) == 64 for b in protocol["source"]["blocks"]))
            protocol["source"]["blocks"].clear()
            self.assertTrue(self.inspect.protocol(name)["source"]["blocks"])
        with self.assertRaisesRegex(LabError, "No local"):
            self.inspect.protocol("../../auth")


if __name__ == "__main__":
    unittest.main()
