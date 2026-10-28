import unittest

from apps.control_api.validator import validate_candidate


SOURCE = "事件编号:E1;工作面:W1;时间:2026-10-01T08:00:00;动作:撤人;状态:已完成;执行人角色:operator;目标范围:W1;路线符合:是;立即:是"


def source(role="actual_record"):
    return {"e1":{"evidence_id":"e1", "file_id":"f1", "document_id":"d1", "document_version":"h1", "content_hash":"h1", "source_role":role, "text":SOURCE, "locator":{"page":1}, "run_id":"r1", "mine_id":"m1"}}


def candidate(status="done"):
    fields = {"event_id":"E1", "workface":"W1", "time":"2026-10-01T08:00:00", "action":"evacuate", "status":status}
    return {"run_id":"r1", "rule_revision":"rev1", "candidate_id":"c1", "attempt_id":"a1", "kind":"action", "fields":fields, "status":"candidate", "evidence_refs":["e1"], "field_evidence":{f"fields.{k}":[{"evidence_id":"e1", "quote":SOURCE}] for k in fields}}


class CandidateBoundaryTests(unittest.TestCase):
    def validate(self, value, evidence=None):
        return validate_candidate(value, evidence_by_id=evidence or source(), run={"run_id":"r1", "mine_id":"m1", "rule_revision":"rev1"})

    def test_only_independently_parsed_source_fact_can_be_promoted(self):
        result = self.validate(candidate())
        self.assertTrue(result["verified"])
        self.assertEqual(result["facts"][0]["fields"]["status"], "done")

    def test_model_cannot_flip_positive_record_into_negative(self):
        result = self.validate(candidate("not_done"))
        self.assertFalse(result["verified"])
        self.assertEqual(result["facts"], [])

    def test_wrong_mine_source_does_not_bind(self):
        evidence = source()
        evidence["e1"]["mine_id"] = "other"
        result = self.validate(candidate(), evidence)
        self.assertFalse(result["verified"])
        self.assertIn("source_owner_mismatch:e1", result["reasons"])

    def test_requirement_cannot_establish_execution(self):
        result = self.validate(candidate(), source("requirement"))
        self.assertFalse(result["verified"])

    def test_quote_outside_source_and_wrong_rule_stay_candidate(self):
        value = candidate()
        value["rule_revision"] = "other"
        value["field_evidence"]["fields.status"][0]["quote"] = "未撤人"
        result = self.validate(value)
        self.assertFalse(result["verified"])
        self.assertIn("rule_revision_mismatch", result["reasons"])


if __name__ == "__main__":
    unittest.main()
