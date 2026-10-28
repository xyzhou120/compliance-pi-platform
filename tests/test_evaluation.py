import unittest

from apps.control_api.evaluation import evaluate_run


class EvaluationTests(unittest.TestCase):
    def test_missing_source_cannot_pass(self):
        result = evaluate_run({"run_id":"r1", "mine_id":"m1", "item_code":"stop_work_unload", "check_cycle":"2026-10"}, {"run_id":"r1", "mine_id":"m1", "source_complete":False, "facts":[], "coverage_gaps":[]})
        self.assertEqual(result["status"], "pending")

    def test_unregistered_fact_claim_is_ignored(self):
        result = evaluate_run({"run_id":"r1", "mine_id":"m1", "item_code":"stop_work_unload", "check_cycle":"2026-10"}, {"run_id":"r1", "mine_id":"m1", "source_complete":True, "facts":[{"kind":"event","event_id":"e1","workface":"A","source_role":"actual_record","fields":{"event_type":"warning"},"evidence_refs":["ev"]},{"kind":"action","event_id":"e1","workface":"A","source_role":"actual_record","fields":{"action":"evacuate","status":False},"evidence_refs":["ev2"]}],"coverage_gaps":[]})
        self.assertEqual(result["status"], "pending")


if __name__ == "__main__":
    unittest.main()
