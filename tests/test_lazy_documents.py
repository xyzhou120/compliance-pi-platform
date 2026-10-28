import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from apps.control_api.documents import SourceRejected, acquire_run, parse_frozen_file
from apps.control_api.sources import SourceRouteError, prepare_source


class LazyDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.storage = self.root / "artifacts"
        self.run = {"run_id": "r1", "mine_id": "m1", "check_cycle": "2026-10",
                    "item_code": "stop_work_unload", "environment": "production", "rule_revision": "rev1",
                    "scope": {"manifest": str(self.root / "manifest.json")}}
        self.settings = {"input_root": str(self.root), "storage_dir": str(self.storage), "inventory_only": True}

    def tearDown(self):
        self.temp.cleanup()

    def manifest(self, files, **extra):
        (self.root / "manifest.json").write_text(json.dumps({"files": files, **extra}), encoding="utf-8")

    def test_inventory_freezes_without_extracting_text_and_selected_parse_is_owned(self):
        (self.root / "one.txt").write_text("第一份原文", encoding="utf-8")
        (self.root / "two.txt").write_text("第二份原文", encoding="utf-8")
        self.manifest([{"path": name, "file_id": fid, "source_role": "actual_record", "report_date": "2026-10-01"}
                       for name, fid in (("one.txt", "f1"), ("two.txt", "f2"))])
        with patch("apps.control_api.documents.parse_file", side_effect=AssertionError("eager parse")):
            snapshot = acquire_run(self.run, self.settings)
        self.assertEqual(snapshot["evidence"], [])
        self.assertEqual([unit["state"] for unit in snapshot["coverage"]], ["not_indexed", "not_indexed"])
        self.assertFalse(snapshot["source_complete"])
        before = copy.deepcopy(snapshot)
        parsed = parse_frozen_file(snapshot, "f2", self.settings)
        self.assertEqual(snapshot, before)
        self.assertEqual(parsed["evidence"][0]["text"], "第二份原文")
        self.assertEqual(parsed["evidence"][0]["run_id"], "r1")
        self.assertEqual(parsed["evidence"][0]["mine_code"], "m1")
        self.assertEqual(parsed["evidence"][0]["snapshot_id"], snapshot["snapshot_id"])
        self.assertEqual(parsed["evidence"][0]["metadata"]["report_date"], "2026-10-01")
        with patch("apps.control_api.documents.parse_file", side_effect=AssertionError("cache not reused")):
            cached = parse_frozen_file(snapshot, "f2", self.settings)
        self.assertTrue(cached["cache_hit"])
        self.assertEqual(cached["evidence"], parsed["evidence"])

    def test_pdf_inventory_counts_pages_without_text_extraction(self):
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.add_blank_page(width=100, height=100)
        writer.write(self.root / "two.pdf")
        self.manifest([{"path": "two.pdf", "file_id": "pdf"}])
        with patch("apps.control_api.documents.parse_file", side_effect=AssertionError("eager parse")):
            snapshot = acquire_run(self.run, self.settings)
        self.assertEqual(snapshot["files"][0]["page_count"], 2)
        self.assertEqual([unit["unit_id"] for unit in snapshot["coverage"]], ["page:1", "page:2"])
        self.assertTrue(all(unit["state"] == "not_indexed" for unit in snapshot["coverage"]))

    def test_parse_rejects_unregistered_outside_and_changed_frozen_bytes(self):
        (self.root / "one.txt").write_text("原文", encoding="utf-8")
        self.manifest([{"path": "one.txt", "file_id": "f1"}])
        snapshot = acquire_run(self.run, self.settings)
        with self.assertRaises(SourceRejected):
            parse_frozen_file(snapshot, "other", self.settings)
        outside = copy.deepcopy(snapshot)
        outside["files"][0]["path"] = str(self.root / "one.txt")
        with self.assertRaises(SourceRejected):
            parse_frozen_file(outside, "f1", self.settings)
        Path(snapshot["files"][0]["path"]).write_text("篡改", encoding="utf-8")
        with self.assertRaises(SourceRejected):
            parse_frozen_file(snapshot, "f1", self.settings)

    def test_same_bytes_with_incompatible_roles_stay_statement(self):
        for name in ("one.txt", "two.txt"):
            (self.root / name).write_text("相同原文", encoding="utf-8")
        self.manifest([{"path": "one.txt", "file_id": "f1", "source_role": "actual_record"},
                       {"path": "two.txt", "file_id": "f2", "source_role": "requirement"}])
        snapshot = acquire_run(self.run, self.settings)
        parsed = parse_frozen_file(snapshot, "f1", self.settings)
        self.assertEqual(parsed["evidence"][0]["source_role"], "statement")
        self.assertEqual(parsed["evidence"][0]["source_file_ids"], ["f1", "f2"])
        self.assertTrue(any("source_role_conflict" in gap for gap in parsed["coverage_gaps"]))
        second = parse_frozen_file(snapshot, "f2", self.settings)
        self.assertEqual(second["evidence"], parsed["evidence"], "alias selection must not rewrite a stable evidence identity")

    def test_authority_receipt_is_server_owned_and_never_establishes_completeness(self):
        (self.root / "one.txt").write_text("原文", encoding="utf-8")
        self.manifest([{"path": "one.txt", "file_id": "f1"}], scope_proof=True, source_complete=True)
        receipt = {"payload": {"authority": "test-authority"}, "signature": "not-validated-here"}
        (self.root / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        settings = {**self.settings, "source_config": {"manifests": {"m1": {"2026-10": "manifest.json"}},
                                                       "authority_receipt_file": "receipt.json"}}
        selected = prepare_source({**self.run, "scope_proof_receipt": {"forged": True}}, settings)
        self.assertEqual(selected["scope_proof_receipt"], receipt)
        snapshot = acquire_run(selected, settings)
        self.assertEqual(snapshot["scope_proof"], receipt)
        self.assertEqual(snapshot["manifest_hash"], hashlib.sha256((self.root / "manifest.json").read_bytes()).hexdigest())
        self.assertFalse(snapshot["source_complete"])
        settings["source_config"]["authority_receipt_file"] = str(self.root.parent / "outside.json")
        with self.assertRaises(SourceRouteError):
            prepare_source(self.run, settings)

    def test_manifest_can_carry_untrusted_receipt_without_promoting_source(self):
        (self.root / "one.txt").write_text("原文", encoding="utf-8")
        receipt = {"payload": {"authority": "a"}, "signature": "s"}
        self.manifest([{"path": "one.txt", "file_id": "f1"}], scope_proof=receipt, source_complete=True)
        snapshot = acquire_run(self.run, self.settings)
        self.assertEqual(snapshot["scope_proof"], receipt)
        self.assertFalse(snapshot["source_complete"])


if __name__ == "__main__":
    unittest.main()
