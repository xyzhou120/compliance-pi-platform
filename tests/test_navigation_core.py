from copy import deepcopy
import unittest

from apps.control_api.navigation import (answer_question, compare_rule_plans, compare_runs,
                                         compare_versions, expand_event_context)


def sample():
    evidence = [dict(evidence_id="ev1", file_id="f1", content_hash="hash1", document_version="hash1",
                     text="实际撤人记录", locator={"page": 1}, source_role="actual_record", run_id="r1", mine_id="m1"),
                dict(evidence_id="ev2", file_id="f2", content_hash="hash2", document_version="hash2",
                     text="另一事件记录", locator={"page": 2}, source_role="actual_record", run_id="r1", mine_id="m1")]
    snapshot = {"run_id": "r1", "mine_id": "m1", "files": [
        {"file_id": "f1", "original_name": "记录1.txt", "content_hash": "hash1", "path": "secret/path"},
        {"file_id": "f2", "original_name": "记录2.txt", "content_hash": "hash2"}], "evidence": evidence}
    run = {"run_id": "r1", "mine_id": "m1", "item_code": "sample_check", "plan": {"planRevision": "rule1"},
           "snapshot": snapshot, "result": {"status": "pending", "coverage_gaps": ["E1/W1: 缺少卸压记录", "E2/W2: 缺证"],
           "propositions": [{"code": "evacuation", "event_id": "E1", "workface": "W1", "state": "true", "evidence_refs": ["ev1"]},
                            {"code": "evacuation", "event_id": "E2", "workface": "W2", "state": "missing", "evidence_refs": ["ev2"]}],
           "association_links": [{"event_id": "E1", "reason": "explicit"}]},
           "candidates": [{"candidate_id": "c1", "fields": {"event_id": "E1"}, "evidence_refs": ["ev1"]}]}
    return run, snapshot


class NavigationCoreTests(unittest.TestCase):
    def test_event_context_is_scoped_and_does_not_change_inputs(self):
        run, snapshot = sample()
        before = deepcopy((run, snapshot))
        context = expand_event_context(run, snapshot, "E1")
        self.assertEqual(context["evidence_refs"], ["ev1"])
        self.assertEqual(len(context["propositions"]), 1)
        self.assertEqual(context["gaps"], ["E1/W1: 缺少卸压记录"])
        self.assertEqual((run, snapshot), before)
        self.assertNotIn("path", context["evidence"][0])

    def test_wrong_run_or_mine_evidence_is_rejected(self):
        run, snapshot = sample()
        snapshot["evidence"][0]["mine_id"] = "other"
        with self.assertRaisesRegex(ValueError, "owner mismatch"):
            answer_question(run, snapshot, "为什么待核验")

    def test_identical_bytes_are_not_independent_corroboration(self):
        _, snapshot = sample()
        snapshot["files"][1]["content_hash"] = "hash1"
        result = compare_versions(snapshot, ["f1", "f2"])
        self.assertTrue(result["comparisons"][0]["same_content"])
        self.assertFalse(result["comparisons"][0]["independent_corroboration"])
        self.assertFalse(result["authority_selected"])
        self.assertNotIn("secret/path", str(result))

    def test_unknown_file_never_enters_version_comparison(self):
        _, snapshot = sample()
        with self.assertRaises(ValueError):
            compare_versions(snapshot, ["f1", "outside"])

    def test_compare_runs_rejects_other_mine_or_item(self):
        run, _ = sample()
        for key in ("mine_id", "item_code"):
            other = deepcopy(run)
            other[key] = "other"
            with self.assertRaises(ValueError):
                compare_runs(run, other)

    def test_compare_runs_reports_rules_files_and_propositions_without_mutation(self):
        before, _ = sample()
        after = deepcopy(before)
        after["run_id"] = "r2"
        after["snapshot"]["run_id"] = "r2"
        for evidence in after["snapshot"]["evidence"]:
            evidence["run_id"] = "r2"
        after["plan"]["planRevision"] = "rule2"
        after["snapshot"]["files"][0]["content_hash"] = "newhash"
        after["snapshot"]["evidence"][0]["content_hash"] = "newhash"
        after["result"]["propositions"][0]["state"] = "missing"
        original = deepcopy((before, after))
        result = compare_runs(before, after)
        self.assertTrue(result["rule_changed"])
        self.assertEqual(len(result["files"]["changed"]), 1)
        self.assertEqual(len(result["propositions"]["changed"]), 1)
        self.assertFalse(result["history_modified"])
        self.assertEqual((before, after), original)

    def test_proposition_answer_has_registered_citations_and_state_label(self):
        run, snapshot = sample()
        result = answer_question(run, snapshot, "撤人命题", proposition_id="evacuation")
        self.assertIn("已证明", result["answer"])
        self.assertEqual(result["evidence_refs"], ["ev1", "ev2"])
        self.assertFalse(result["business_status_generated"])

    def test_unknown_and_new_material_questions_do_not_invent_facts(self):
        run, snapshot = sample()
        unknown = answer_question(run, snapshot, "任意无法回答的业务判断")
        self.assertEqual(unknown["knowledge_kind"], "unsupported_question")
        new = answer_question(run, snapshot, "有没有新资料")
        self.assertEqual(new["knowledge_kind"], "scope_notice")
        self.assertIn("新", new["answer"])
        self.assertEqual(new["evidence_refs"], [])
        self.assertIn("未查询全局来源目录", " ".join(new["limitations"]))

    def test_missing_reference_cannot_appear_as_citation(self):
        run, snapshot = sample()
        run["result"]["propositions"][0]["evidence_refs"] = ["outside"]
        result = answer_question(run, snapshot, "evacuation")
        self.assertNotIn("outside", result["evidence_refs"])
        self.assertIn("未登记引用", " ".join(result["limitations"]))

    def test_evidence_cannot_reference_a_different_frozen_version(self):
        run, snapshot = sample()
        snapshot["evidence"][0]["content_hash"] = "different-version"
        with self.assertRaisesRegex(ValueError, "version mismatch"):
            expand_event_context(run, snapshot, "E1")

    def test_revision_only_change_still_requires_new_execution(self):
        old = {"itemCode": "sample_check", "planRevision": "p1"}
        result = compare_rule_plans(old, {**old, "planRevision": "p2"})
        self.assertTrue(result["requires_new_execution"])

    def test_rule_comparison_requires_new_execution_and_preserves_history(self):
        old = {"itemCode": "sample_check", "planRevision": "p1", "requiredPropositions": ["a"]}
        new = {**old, "planRevision": "p2", "requiredPropositions": ["a", "b"]}
        result = compare_rule_plans(old, new)
        self.assertTrue(result["requires_new_execution"])
        self.assertEqual(result["changes"][0]["field"], "requiredPropositions")
        self.assertFalse(result["replay"]["history_modified"])


if __name__ == "__main__":
    unittest.main()
