import unittest

from apps.control_api.planner import evaluate_coverage


class PlannerTests(unittest.TestCase):
    def test_missing_evidence_never_passes(self):
        result = evaluate_coverage(item_code="stop_work_unload", source_complete=False, proven=set(), false=set(), gaps=["not_searched"], evidence_refs=[])
        self.assertEqual(result["status"], "pending")

    def test_false_claim_cannot_become_no_pass(self):
        result = evaluate_coverage(item_code="stop_work_unload", source_complete=True, proven=set(), false={"evacuation_executed"}, gaps=[], evidence_refs=["e1"])
        self.assertEqual(result["status"], "pending")


if __name__ == "__main__":
    unittest.main()
