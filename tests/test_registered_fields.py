import unittest

from apps.control_api.validator import validate_candidate
from packages.contracts.models import CandidateEnvelope


def evidence(text="事件编号:E1;工作面:W1;状态:已撤人"):
    return {"e1": {"evidence_id": "e1", "document_id": "d1", "document_version": "v1", "content_hash": "h1", "source_role": "actual_record", "text": text, "mine_id": "m1", "run_id": "r1"}}


def make_contract():
    return {"source_roles": ["actual_record"], "fields": {"workface": {"type": "text", "required": True, "source_label": "工作面"}, "evacuated": {"type": "bool", "true_values": ["已撤人"], "false_values": ["未撤人"]}}}


def candidate(*, fields, quote="事件编号:E1;工作面:W1;状态:已撤人", status="candidate", refs=("e1",)):
    return {"candidate_id": "c1", "attempt_id": "a1", "run_id": "r1", "rule_revision": "rev1", "proposition_id": "p1", "kind": "action", "provider": "qwen", "model": "local", "prompt_revision": "p1", "fields": fields, "status": status, "evidence_refs": list(refs), "field_evidence": {name: [{"evidence_id": "e1", "quote": quote}] for name in fields}}


class RegisteredFieldTests(unittest.TestCase):
    def validate(self, value, sources=None, fact_contract=None):
        return validate_candidate(value, evidence_by_id=sources or evidence(), allowed_documents={"v1"}, allowed_hashes={"h1"}, run={"run_id": "r1", "mine_id": "m1", "rule_revision": "rev1"}, fact_contract=fact_contract or make_contract())

    def test_anchor_does_not_promote_domain_verified(self):
        result = self.validate(candidate(fields={"workface": "W1", "evacuated": True}))
        self.assertEqual(result["validation_status"], "source_bound")
        self.assertFalse(result["verified"])
        self.assertEqual(result["facts"], [])
        self.assertEqual(result["verification_scope"], "source_anchor_only")
        self.assertIn("workface", result["anchored_fields"])
        self.assertTrue(result["field_contract_revision"])

    def test_wrong_value_and_missing_field_reference_stay_candidate(self):
        wrong = candidate(fields={"workface": "W2", "evacuated": True})
        self.assertEqual(self.validate(wrong)["validation_status"], "candidate")
        missing = candidate(fields={"workface": "W1", "evacuated": True}, refs=())
        self.assertEqual(self.validate(missing)["validation_status"], "candidate")

    def test_expanded_envelope_references_are_normalized_to_ids(self):
        value = candidate(fields={"workface": "W1", "evacuated": True})
        value["evidence_refs"] = [{"evidence_id": "e1", "document_id": "d1", "document_version": "v1", "content_hash": "h1", "locator": {"page": 1}, "quote": "工作面:W1", "source_role": "actual_record"}]
        result = self.validate(CandidateEnvelope.model_validate(value))
        self.assertEqual(result["validation_status"], "source_bound")
        self.assertFalse(result["verified"])

    def test_model_status_claim_is_ignored(self):
        result = self.validate(candidate(fields={"workface": "W1", "evacuated": True}, status="verified"))
        self.assertEqual(result["validation_status"], "candidate")
        self.assertIn("model_verification_or_status_claim_ignored", result["reasons"])

    def test_statement_or_model_candidate_cannot_upgrade(self):
        sources = evidence()
        sources["e1"]["source_role"] = "statement"
        result = self.validate(candidate(fields={"workface": "W1", "evacuated": True}), sources)
        self.assertEqual(result["validation_status"], "candidate")
        sources["e1"]["source_role"] = "actual_record"
        sources["e1"]["quality"] = "model_candidate"
        self.assertEqual(self.validate(candidate(fields={"workface": "W1", "evacuated": True}), sources)["validation_status"], "candidate")

    def test_conflicting_boolean_source_stays_candidate(self):
        sources = evidence("工作面 W1 状态:已撤人，未撤人")
        result = self.validate(candidate(fields={"workface": "W1", "evacuated": True}, quote="工作面 W1 状态:已撤人，未撤人"), sources)
        self.assertEqual(result["validation_status"], "candidate")
        self.assertTrue(any("field_conflicted" in reason for reason in result["reasons"]))


if __name__ == "__main__":
    unittest.main()
