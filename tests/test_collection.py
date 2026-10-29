import json
from pathlib import Path
import tempfile
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from apps.control_api.collection import collect_native_files, collect_remaining_pages
from apps.control_api.documents import acquire_run
from apps.control_api.evidence import EvidenceService
from apps.control_api.settings import Settings
from apps.control_api.store import Store, StaleLease
from apps.control_api.worker import normalize_run, run_once


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "ledger.db")
        self.settings = Settings(input_root=self.root, storage_dir=self.root / "store", provider_concurrency=2)
        # Seventy independent sources: the first deliberately fails to parse.
        files = []
        for index in range(70):
            fid = f"file-{index}"
            (self.root / (fid + ".txt")).write_text("有效原文 " + str(index), encoding="utf-8")
            files.append({"path": fid + ".txt", "file_id": fid, "source_role": "actual_record"})
        (self.root / "manifest.json").write_text(json.dumps({"files": files}), encoding="utf-8")
        accepted = self.store.accept({"mineId": "m", "checkCycle": "2026-10", "executionId": "batch",
                                     "items": [{"itemCode": "stop_work_unload"}]}, {})
        self.rid = accepted["runs"][0]["run_id"]
        self.run = normalize_run(self.store.claim("owner", run_id=self.rid))
        snapshot = acquire_run({**self.run, "scope": {"manifest": str(self.root / "manifest.json")}},
                               {**self.settings.document_settings(), "inventory_only": True})
        self.store.save_snapshot(self.rid, "owner", self.run["fence"], snapshot)
        self.service = EvidenceService(self.store, self.settings)

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def test_one_failed_file_does_not_prevent_other_sixty_nine(self):
        from apps.control_api.evidence import parse_frozen_file
        def parse(snapshot, fid, settings):
            if fid == "file-0": raise ValueError("synthetic_document_failure")
            return parse_frozen_file(snapshot, fid, settings)
        with patch("apps.control_api.evidence.parse_frozen_file", side_effect=parse):
            rows = collect_native_files(self.service, self.rid, "owner", self.run["fence"],
                                        deadline=time.monotonic()+60, check=lambda: None)
        self.assertEqual(len(rows), 70)
        self.assertEqual(sum(bool(row.get("unit_failed")) for row in rows), 1)
        work = self.store.list_work_units(self.rid)
        self.assertEqual(sum(row["state"] == "succeeded" for row in work), 69)
        self.assertEqual(sum(row["state"] == "failed" for row in work), 1)
        self.assertEqual(len(self.store.get_run(self.rid)["evidence"]), 69)

    def test_single_page_exception_does_not_lose_successful_page(self):
        self.service.settings = replace(self.settings, ocr_endpoint="http://unused.invalid/ocr")
        def operation(_rid, _owner, _fence, _kind, inputs, _operation):
            if inputs["page"] == 1: raise OSError("synthetic page failure")
            return {"evidence": [{"evidence_id": "second-page"}], "attempts": [], "gaps": []}
        with patch.object(self.service, "_unit", side_effect=operation):
            result = self.service.recognize(self.rid, {"pages": [{"file_id": "file-0", "page": 1}, {"file_id": "file-1", "page": 2}]},
                                            worker_id="owner", fence=self.run["fence"])
        self.assertEqual(result["evidence"], [{"evidence_id": "second-page"}])
        self.assertEqual(result["gaps"][0]["page"], 1)

    def test_stale_lease_is_global_and_must_not_be_swallowed(self):
        with patch.object(self.service, "parse", side_effect=StaleLease("owner lost")):
            with self.assertRaises(StaleLease):
                self.service.inspect_documents(self.rid, ["file-0", "file-1"], worker_id="owner", fence=self.run["fence"])

    def test_deadline_reports_unattempted_files_instead_of_claiming_complete(self):
        rows = collect_native_files(self.service, self.rid, "owner", self.run["fence"], deadline=0, check=lambda: None)
        self.assertEqual(rows, [])
        gaps = self.store.get_run(self.rid)["gaps"]
        self.assertEqual(len(gaps[0]["file_ids"]), 70)
        self.assertEqual(gaps[0]["reason"], "collection_deadline")


if __name__ == "__main__":
    unittest.main()
