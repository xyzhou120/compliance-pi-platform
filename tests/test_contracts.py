import unittest

from packages.contracts.models import CandidateEnvelope, EvidenceRef, InspectionTask, ResultStatus


class ContractTests(unittest.TestCase):
    def test_task_aliases_and_candidate(self):
        task = InspectionTask.model_validate({"mineId": "m1", "checkCycle": "2026-10", "items": [{"itemCode": "stop_work_unload"}]})
        candidate = CandidateEnvelope(
            candidate_id="c1", attempt_id="a1", run_id="r1", rule_revision="rev1",
            proposition_id="trigger_confirmed", kind="event", provider="local", model="qwen",
            prompt_revision="p1", evidence_refs=[EvidenceRef(evidence_id="e1", document_id="d1", document_version="v1", content_hash="h", locator={"page": 1}, source_role="actual_record")]
        )
        self.assertEqual(task.mine_id, "m1")
        self.assertEqual(candidate.status, "candidate")
        self.assertEqual(ResultStatus.PENDING.value, "pending")


if __name__ == "__main__":
    unittest.main()
