import unittest

from apps.control_api.validator import validate_candidate


def candidate(**kwargs):
    return {"candidate_id":"c1", "attempt_id":"a1", "run_id":"r1", "rule_revision":"rev1", "proposition_id":"trigger_confirmed", "kind":"event", "provider":"qwen", "model":"qwen-local", "prompt_revision":"p1", "fields":{"event_id":"e"}, "field_evidence":{"event_id":[{"evidence_id":kwargs["evidence_id"], "quote":kwargs.get("quote", "原文")}]}, "evidence_refs":[kwargs]}


class ValidatorTests(unittest.TestCase):
    def test_binds_candidate_to_frozen_document_but_requires_semantic_source_match(self):
        ref = {"evidence_id":"e1", "document_id":"d1", "document_version":"v1", "content_hash":"h1", "locator":{"page":1}, "quote":"原文", "source_role":"actual_record"}
        result = validate_candidate(candidate(**ref), evidence_by_id={"e1":{"evidence_id":"e1", "document_id":"d1", "document_version":"v1", "content_hash":"h1", "text":"原文", "mine_id":"m1", "run_id":"r1"}}, allowed_documents={"v1"}, allowed_hashes={"h1"}, run={"run_id":"r1", "mine_id":"m1", "rule_revision":"rev1"})
        self.assertFalse(result["verified"])
        self.assertIn("semantic_fields_not_independently_verified", result["reasons"])

    def test_rejects_out_of_scope_document(self):
        ref = {"evidence_id":"e1", "document_id":"d1", "document_version":"v2", "content_hash":"h1", "locator":{"page":1}, "quote":"原文", "source_role":"actual_record"}
        result = validate_candidate(candidate(**ref), evidence_by_id={"e1":{"evidence_id":"e1", "document_id":"d1", "document_version":"v2", "content_hash":"h1", "text":"原文"}}, allowed_documents={"v1"}, allowed_hashes={"h1"})
        self.assertFalse(result["verified"])
        self.assertIn("document_outside_scope:e1", result["reasons"])


if __name__ == "__main__":
    unittest.main()
