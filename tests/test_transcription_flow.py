"""Independent review -> durable evidence -> merged coverage, without real services."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from apps.control_api.documents import acquire_run
from apps.control_api.evidence import EvidenceService
from apps.control_api.quality import sign_transcription_review
from apps.control_api.settings import Settings
from apps.control_api.store import StaleLease, Store
from apps.control_api.worker import normalize_run


KEY = "independent-review-flow-test-key"
TEXT = "独立复核原文：工作面 W1 实测值 12 MPa"


@unittest.skipUnless(importlib.util.find_spec("pypdfium2") and importlib.util.find_spec("PIL"), "PDF rendering extra required")
class TranscriptionFlowTests(unittest.TestCase):
    def setUp(self):
        from pypdf import PdfWriter

        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(self.root / "scan.pdf")
        (self.root / "manifest.json").write_text(json.dumps({"files": [{"path": "scan.pdf", "file_id": "f1", "source_role": "actual_record"}]}), encoding="utf-8")
        self.external_receipts = self.root / "server-owned-reviews.json"
        self.settings = Settings(input_root=self.root, storage_dir=self.root / "artifacts", evidence_review_key=KEY,
                                 evidence_review_receipts=self.external_receipts, ocr_endpoint="http://isolated-provider.invalid/ocr")
        self.store = Store(self.root / "ledger.sqlite3")
        self.plan = {"planRevision": "review-flow-plan", "requiredPropositions": [], "materialRequirements": [], "budgets": {"maxPages": 10}}
        task = self.store.accept({"mineId": "m1", "checkCycle": "2026-10", "executionId": "review-flow", "items": [{"itemCode": "stop_work_unload"}]}, {"stop_work_unload": self.plan})
        self.run = normalize_run(self.store.claim("owner", lease_seconds=300, run_id=task["runs"][0]["run_id"]))
        self.rid, self.fence = self.run["run_id"], self.run["fence"]
        self.run["scope"] = {"manifest": str(self.root / "manifest.json")}
        self.snapshot = acquire_run(self.run, self.settings.document_settings())
        self.service = EvidenceService(self.store, self.settings)

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def receipt(self, *, text=TEXT, review_id="review1"):
        return sign_transcription_review({"review_id": review_id, "authority": "independent-test-reviewer", "reviewer": "reviewer1", "policy_revision": "transcription-policy-v1",
                                          "file_id": "f1", "content_hash": self.snapshot["files"][0]["content_hash"], "page": 1,
                                          "text": text, "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(), "source_role": "actual_record"}, KEY)

    def freeze_and_recognize(self, receipts):
        self.external_receipts.write_text(json.dumps(receipts, ensure_ascii=False), encoding="utf-8")
        self.snapshot["transcription_review_receipts"] = deepcopy(receipts)
        self.store.save_snapshot(self.rid, "owner", self.fence, self.snapshot)
        # The real recognition path still renders the PDF, acquires the ledger
        # permit and persists a work unit; only external HTTP is substituted.
        with patch.object(self.service.provider_pool, "call", return_value={"text": "OCR待核候选：工作面 W1 实测值 21 MPa", "blocks": [], "elapsed_ms": 1, "queue_ms": 0}) as provider:
            recognized = self.service.recognize(self.rid, {"pages": [{"file_id": "f1", "page": 1}]}, worker_id="owner", fence=self.fence)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(recognized["evidence"][0]["quality"], "model_candidate")
        self.assertEqual(recognized["evidence"][0]["source_role"], "statement")
        return recognized

    def test_review_is_registered_reused_and_closes_only_merged_coverage(self):
        self.freeze_and_recognize([self.receipt()])
        result = self.service.apply_reviews(self.rid, worker_id="owner", fence=self.fence)
        self.assertFalse(result["reused"])
        record = result["evidence"][0]
        stored = self.store.get_run(self.rid)
        self.assertIn(record["evidence_id"], {item["evidence_id"] for item in stored["evidence"]})
        self.assertTrue(record["derived_from"])
        self.assertEqual(record["text"], TEXT)
        self.assertEqual(record["quality"], "independently_reviewed")
        self.assertEqual(record["source_role"], "actual_record")
        self.assertEqual(stored["snapshot"]["coverage"], self.snapshot["coverage"], "immutable native coverage is not rewritten")
        merged = self.service.snapshot(normalize_run(stored))
        self.assertEqual(merged["coverage"][0]["state"], "readable")
        self.assertEqual(merged["coverage"][0]["reason"], "independent_review")
        self.service.search(self.rid, "", file_id="f1", limit=100, worker_id="owner", fence=self.fence)
        audited = self.service.audit(self.rid, frozen=False)
        self.assertEqual(audited["units"][0]["parse_state"], "readable")
        self.assertEqual(audited["units"][0]["state"], "searched_hit")
        repeated = self.service.apply_reviews(self.rid, worker_id="owner", fence=self.fence)
        self.assertTrue(repeated["reused"])
        self.assertEqual(repeated["evidence"], result["evidence"])
        reviews = [unit for unit in self.store.list_work_units(self.rid) if unit["kind"] == "review_transcription"]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["attempt"], 1)

    def test_completed_run_uses_persisted_review_even_when_external_receipt_or_key_changes(self):
        receipts = [self.receipt()]
        self.freeze_and_recognize(receipts)
        applied = self.service.apply_reviews(self.rid, worker_id="owner", fence=self.fence)
        self.service.search(self.rid, "", file_id="f1", limit=100, worker_id="owner", fence=self.fence)
        audit = self.service.audit(self.rid, frozen=False)
        final = {"run_id": self.rid, "status": "pending", "description": "Only transcription was reviewed", "evidence_refs": [applied["evidence"][0]["evidence_id"]], "coverage_gaps": ["scope_proof_missing"], "coverage_audit": audit}
        self.store.finalize(self.rid, "owner", self.fence, final)
        before = self.store.get_run(self.rid)
        merged_before = self.service.snapshot(normalize_run(before))
        self.external_receipts.write_text(json.dumps([self.receipt(text="后来替换的回执", review_id="later-review")], ensure_ascii=False), encoding="utf-8")
        changed_service = EvidenceService(self.store, replace(self.settings, evidence_review_key="rotated-or-wrong-key"))
        merged_after = changed_service.snapshot(normalize_run(self.store.get_run(self.rid)))
        self.assertEqual(merged_before["evidence"], merged_after["evidence"])
        self.assertEqual(merged_before["coverage"], merged_after["coverage"])
        self.assertEqual(changed_service.audit(self.rid), audit)
        with self.assertRaises(StaleLease):
            changed_service.apply_reviews(self.rid, worker_id="owner", fence=self.fence)
        after = self.store.get_run(self.rid)
        self.assertEqual(before["result"], after["result"])
        self.assertEqual(before["evidence"], after["evidence"])

    def test_conflicting_independent_receipts_are_persisted_as_statements(self):
        self.freeze_and_recognize([self.receipt(), self.receipt(text="独立复核相反值：工作面 W1 实测值 21 MPa", review_id="review2")])
        applied = self.service.apply_reviews(self.rid, worker_id="owner", fence=self.fence)
        self.assertEqual(len(applied["evidence"]), 2)
        self.assertTrue(all(record["source_role"] == "statement" and record["quality"] == "conflicted_review" for record in applied["evidence"]))
        merged = self.service.snapshot(normalize_run(self.store.get_run(self.rid)))
        self.assertEqual(merged["coverage"][0]["state"], "conflicted")
        audited = self.service.audit(self.rid, frozen=False)
        self.assertEqual(audited["units"][0]["state"], "conflicted")

    def test_append_only_candidate_review_does_not_change_result_status(self):
        self.freeze_and_recognize([self.receipt()])
        reviewed = self.service.apply_reviews(self.rid, worker_id="owner", fence=self.fence)
        evidence_id = reviewed["evidence"][0]["evidence_id"]
        self.store.save_candidates(self.rid, "owner", self.fence, [{"candidate_id": "candidate1", "candidate": {"candidate_id": "candidate1", "kind": "measurement"}}])
        self.store.finalize(self.rid, "owner", self.fence, {"run_id": self.rid, "status": "pending", "description": "Frozen outcome", "evidence_refs": [evidence_id], "coverage_gaps": ["scope_proof_missing"]})
        before = deepcopy(self.store.get_run(self.rid)["result"])
        payload = {"review_id": "field-note", "candidate_id": "candidate1", "fields": {"measurement": [evidence_id]}, "comment": "Independent transcription reference"}
        first = self.store.append_review(self.rid, payload)
        self.assertEqual(first["state"], "recorded")
        self.assertEqual(self.store.append_review(self.rid, payload), first)
        with self.assertRaises(ValueError):
            self.store.append_review(self.rid, {**payload, "review_id": "illegal-verdict", "status": "pass"})
        self.assertEqual(self.store.get_run(self.rid)["result"], before)


if __name__ == "__main__":
    unittest.main()
