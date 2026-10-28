import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError

from apps.control_api.providers import ProviderPool, recognize_pages
from apps.control_api.quality import validate_field_candidate, validate_review_evidence


class ProviderQualityTests(unittest.TestCase):
    def test_retry_then_success_and_fallback_quality_candidate(self):
        calls = []
        def transport(url, payload, timeout):
            calls.append(url)
            if len(calls) == 1: raise TimeoutError("temporary")
            return {"text": "", "blocks": [], "confidence": 0.1}
        result = recognize_pages([{"file_id": "f", "page": 1, "image_bytes": b"x"}], settings={"endpoint": "http://primary", "fallback_provider": {"endpoint": "http://fallback"}, "max_attempts": 2}, pool=ProviderPool(max_concurrency=1), transport=transport)
        self.assertEqual(calls, ["http://primary", "http://primary", "http://fallback"])
        self.assertEqual({item["provider"] for item in result["candidates"]}, {"external", "fallback"})
        self.assertTrue(all(item["quality"] == "candidate" for item in result["candidates"]))

    def test_cancel_does_not_publish_late_candidate(self):
        result = recognize_pages([{"file_id": "f", "page": 1, "image_bytes": b"x"}], settings={"endpoint": "http://test", "cancelled": lambda: True}, transport=lambda *_: {"text": "late", "blocks": []})
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["attempts"][0]["status"], "cancelled")

    def test_field_validation_requires_quote_type_and_source_role(self):
        evidence = {"e1": {"text": "工作面 W1 已撤人", "source_role": "actual_record"}}
        candidate = {"fields": {"workface": "W1", "status": True}, "evidence_refs": ["e1"], "field_evidence": {"workface": [{"evidence_id": "e1", "quote": "工作面 W1"}], "status": [{"evidence_id": "e1", "quote": "已撤人"}]}}
        result = validate_field_candidate(candidate, evidence, {"source_roles": ["actual_record"], "fields": {"workface": {"type": "text"}, "status": {"type": "bool", "true_values": ["已撤人"], "false_values": ["未撤人"]}}})
        self.assertEqual(result["validation_status"], "source_bound")

    def test_schema_error_not_retried_and_http_5xx_is_retried(self):
        for error, expected in [(ValueError("bad schema"), 1), (HTTPError("http://test", 503, "busy", {}, None), 2)]:
            calls = []
            def transport(*_):
                calls.append(1)
                raise error
            result = recognize_pages([{"file_id": "f", "page": 1, "image_bytes": b"x"}], settings={"endpoint": "http://test"}, transport=transport)
            self.assertEqual(len(calls), expected)
            self.assertEqual(len(result["attempts"]), expected)
            self.assertEqual(result["candidates"], [])

    def test_late_inflight_response_retains_permit_and_writes_no_cache(self):
        entered, finish, cancel = threading.Event(), threading.Event(), threading.Event()
        pool = ProviderPool(max_concurrency=1)
        result = {}
        def transport(*_):
            entered.set()
            finish.wait(2)
            return {"text": "late response", "blocks": []}
        with tempfile.TemporaryDirectory() as tmp:
            settings = {"endpoint": "http://test", "cache_dir": tmp, "cancelled": cancel.is_set}
            thread = threading.Thread(target=lambda: result.update(recognize_pages([{"file_id": "f", "page": 1, "image_bytes": b"x"}], settings=settings, pool=pool, transport=transport)))
            thread.start()
            self.assertTrue(entered.wait(1))
            cancel.set()
            self.assertFalse(pool._semaphore.acquire(blocking=False), "inflight call retains its concurrency permit")
            finish.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result["candidates"], [])
            self.assertEqual(list(Path(tmp).glob("*.json")), [])

    def test_provider_disagreement_and_page_budget_are_explicit(self):
        result = recognize_pages([{"file_id": "f", "page": n, "image_bytes": b"x"} for n in (1, 2)], settings={"endpoint": "http://primary", "max_total_pages": 1, "fallback_provider": {"endpoint": "http://fallback"}}, transport=lambda url, *_: {"text": "12" if url.endswith("primary") else "21", "blocks": [], "confidence": 0.2})
        self.assertEqual(len(result["candidates"]), 2)
        self.assertTrue(all(item["conflicted"] and item["needs_review"] for item in result["candidates"]))
        self.assertTrue(any(item.get("error") == "page_budget_exhausted" for item in result["errors"]))

    def test_field_conflict_and_ocr_candidates_are_not_promoted(self):
        candidate = {"fields": {"done": True}, "evidence_refs": ["e"], "field_evidence": {"done": [{"evidence_id": "e", "quote": "已撤人，未撤人"}]}}
        contract = {"source_roles": ["actual_record"], "fields": {"done": {"type": "bool", "true_values": ["已撤人"], "false_values": ["未撤人"]}}}
        result = validate_field_candidate(candidate, {"e": {"text": "已撤人，未撤人", "source_role": "actual_record"}}, contract)
        self.assertEqual(result["validation_status"], "candidate")
        candidate["field_evidence"]["done"][0]["quote"] = "已撤人"
        result = validate_field_candidate(candidate, {"e": {"text": "已撤人", "source_role": "actual_record", "quality": "model_candidate"}}, contract)
        self.assertEqual(result["validation_status"], "candidate")

    def test_review_cannot_change_result(self):
        with self.assertRaises(ValueError): validate_review_evidence({"review_id": "r", "candidate_id": "c", "status": "pass", "fields": {"x": ["e"]}})
        self.assertEqual(validate_review_evidence({"review_id": "r", "candidate_id": "c", "fields": {"x": ["e"]}})["state"], "recorded")


if __name__ == "__main__": unittest.main()
