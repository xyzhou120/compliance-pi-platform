from copy import deepcopy
import hashlib
import unittest

from apps.control_api.quality import apply_transcription_reviews, sign_transcription_review


KEY = "independent-review-test-key"


def snapshot():
    return {"run_id": "r1", "mine_id": "m1", "snapshot_id": "s1", "rule_revision": "rev1",
            "files": [{"file_id": "f1", "content_hash": "hash1", "source_role": "actual_record", "format": "pdf", "page_count": 2}],
            "coverage": [{"file_id": "f1", "unit_id": "page:1", "state": "unreadable"}, {"file_id": "f1", "unit_id": "page:2", "state": "not_indexed"}],
            "evidence": [{"evidence_id": "ocr1", "file_id": "f1", "content_hash": "hash1", "quality": "model_candidate", "source_role": "statement", "locator": {"page": 1}, "text": "unverified OCR", "run_id": "r1", "mine_id": "m1"}]}


def payload(**updates):
    text = "工作面 W1 实测值 12 MPa"
    return {"review_id": "review1", "authority": "independent-review-service", "reviewer": "reviewer1", "policy_revision": "policy-v1",
            "file_id": "f1", "content_hash": "hash1", "page": 1, "text": text,
            "text_hash": hashlib.sha256(text.encode()).hexdigest(), "source_role": "actual_record", **updates}


class QualityReceiptTests(unittest.TestCase):
    def test_signed_independent_review_creates_new_traceable_evidence(self):
        original = snapshot()
        before = deepcopy(original)
        receipt = sign_transcription_review(payload(), KEY)
        result = apply_transcription_reviews(original, [receipt, receipt], KEY)
        self.assertEqual(original, before)
        self.assertEqual(result["gaps"], [])
        self.assertEqual(len(result["evidence"]), 1)
        item = result["evidence"][0]
        self.assertEqual(item["derived_from"], ["ocr1"])
        self.assertEqual(item["quality"], "independently_reviewed")
        self.assertEqual(item["origin_document_version"], "hash1")
        self.assertEqual(item["locator"], {"kind": "reviewed_transcription", "page": 1})
        self.assertEqual(item["source_role"], "actual_record")
        self.assertEqual(result["coverage_updates"][0]["state"], "readable")
        self.assertEqual(result["coverage_updates"][0]["reason"], "independent_review")
        self.assertEqual(result["coverage_updates"][0]["native_state"], "unreadable")

    def test_tamper_missing_key_and_bad_text_hash_do_not_publish(self):
        receipt = sign_transcription_review(payload(), KEY)
        tampered = deepcopy(receipt)
        tampered["payload"]["text"] = "changed"
        invalid_hash = sign_transcription_review(payload(text_hash="wrong"), KEY)
        for receipts, key, reason in [([tampered], KEY, "signature_mismatch"), ([receipt], "", "verification_key_missing"), ([invalid_hash], KEY, "text_hash_mismatch")]:
            with self.subTest(reason=reason):
                result = apply_transcription_reviews(snapshot(), receipts, key)
                self.assertEqual(result["evidence"], [])
                self.assertTrue(any(reason in gap for gap in result["gaps"]))

    def test_non_frozen_page_file_or_role_stays_gap(self):
        for updates, reason in [({"page": 3}, "page_outside_frozen_coverage"), ({"page": True}, "page_outside_frozen_coverage"), ({"file_id": "other"}, "frozen_file_hash_mismatch"), ({"content_hash": "other"}, "frozen_file_hash_mismatch"), ({"source_role": "requirement"}, "source_role_mismatch")]:
            with self.subTest(updates=updates):
                result = apply_transcription_reviews(snapshot(), [sign_transcription_review(payload(**updates), KEY)], KEY)
                self.assertEqual(result["evidence"], [])
                self.assertTrue(any(reason in gap for gap in result["gaps"]))

    def test_registered_candidate_and_nonempty_text_are_required(self):
        for source, updates, reason in [(dict(snapshot(), evidence=[]), {}, "model_candidate_source_missing"), (snapshot(), {"text": " "}, "invalid_payload"), (snapshot(), {"verified": True}, "invalid_payload")]:
            result = apply_transcription_reviews(source, [sign_transcription_review(payload(**updates), KEY)], KEY)
            self.assertEqual(result["evidence"], [])
            self.assertTrue(any(reason in gap for gap in result["gaps"]))

    def test_conflicting_independent_reviews_cannot_close_page(self):
        text2 = "工作面 W1 实测值 21 MPa"
        receipts = [sign_transcription_review(payload(), KEY), sign_transcription_review(payload(review_id="review2", text=text2, text_hash=hashlib.sha256(text2.encode()).hexdigest()), KEY)]
        result = apply_transcription_reviews(snapshot(), receipts, KEY)
        self.assertEqual(len(result["evidence"]), 2)
        self.assertTrue(any("reviewed_transcriptions_conflict" in gap for gap in result["gaps"]))
        self.assertTrue(all(item["state"] == "conflicted" for item in result["coverage_updates"]))
        self.assertTrue(all(item["source_role"] == "statement" and item["quality"] == "conflicted_review" for item in result["evidence"]))

    def test_reused_review_identity_with_changed_content_is_quarantined(self):
        text2 = "工作面 W1 实测值 21 MPa"
        receipts = [sign_transcription_review(payload(), KEY), sign_transcription_review(payload(text=text2, text_hash=hashlib.sha256(text2.encode()).hexdigest()), KEY)]
        result = apply_transcription_reviews(snapshot(), receipts, KEY)
        self.assertTrue(any("review_identity_conflict" in gap for gap in result["gaps"]))
        self.assertEqual(result["evidence"][0]["source_role"], "statement")
        self.assertEqual(result["coverage_updates"][0]["state"], "conflicted")


if __name__ == "__main__":
    unittest.main()
