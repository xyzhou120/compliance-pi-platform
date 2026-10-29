import unittest

from apps.control_api.evaluation import evaluate_run


class EvaluationTests(unittest.TestCase):
    def test_missing_source_cannot_pass(self):
        result = evaluate_run({"run_id":"r1", "mine_id":"m1", "item_code":"stop_work_unload", "check_cycle":"2026-10"}, {"run_id":"r1", "mine_id":"m1", "source_complete":False, "facts":[], "coverage_gaps":[]})
        self.assertEqual(result["status"], "unverifiable")

    def test_unregistered_fact_claim_is_ignored(self):
        result = evaluate_run({"run_id":"r1", "mine_id":"m1", "item_code":"stop_work_unload", "check_cycle":"2026-10"}, {"run_id":"r1", "mine_id":"m1", "source_complete":True, "facts":[{"kind":"event","event_id":"e1","workface":"A","source_role":"actual_record","fields":{"event_type":"warning"},"evidence_refs":["ev"]},{"kind":"action","event_id":"e1","workface":"A","source_role":"actual_record","fields":{"action":"evacuate","status":False},"evidence_refs":["ev2"]}],"coverage_gaps":[]})
        self.assertEqual(result["status"], "unverifiable")

    def test_processing_error_is_technical_failure_not_inspector_review(self):
        run = {"run_id": "r1", "mine_id": "m1", "item_code": "stop_work_unload", "check_cycle": "2026-10"}
        snapshot = {"run_id": "r1", "mine_id": "m1", "source_complete": False,
                    "files": [{"file_id": "f1", "content_hash": "hash", "original_name": "日报.pdf"}],
                    "coverage": [{"file_id": "f1", "unit_id": "page:1", "state": "provider_failed"}],
                    "coverage_audit": {"source_complete": False, "units": [{"file_id": "f1", "unit_id": "page:1", "state": "provider_failed"}]},
                    "evidence": [], "coverage_gaps": ["provider_failed:f1/page:1"]}
        result = evaluate_run(run, snapshot)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["decision_class"], "system_processing_incomplete")


if __name__ == "__main__":
    unittest.main()
