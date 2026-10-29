from copy import deepcopy
import unittest

from apps.control_api.domain.rules import gate_conflicted_evidence, evaluate_documents
from apps.control_api.domain.source_relations import analyze_source_relations, relation_explanation
from apps.control_api.navigation import answer_question
from tests.test_domain_rules import action, fact, run_snapshot, stop_chain


class SourceRelationTests(unittest.TestCase):
    def snapshot(self, facts=None):
        _, snapshot = run_snapshot("stop_work_unload", facts or stop_chain())
        snapshot["files"] = [dict(file_id="a", content_hash="hash-a", original_name="日报.pdf",
                                  report_date="2026/10/01", material_group="prevention_daily"),
                             dict(file_id="b", content_hash="hash-b", original_name="日报（新）.pdf",
                                  report_date="2026/10/01", material_group="prevention_daily")]
        for evidence in snapshot["evidence"]:
            evidence.update(file_id="a", content_hash="hash-a")
        return snapshot

    def result(self, snapshot):
        return dict(status="pass", findings=[], candidate_findings=[], coverage_gaps=[],
                    evidence_refs=[e["evidence_id"] for e in snapshot["evidence"]],
                    propositions=[dict(code=name, state="true", event_id="E1", workface="W1", evidence_refs=[name])
                                  for name in ("stop", "power_off", "evacuate", "report")])

    def test_same_day_category_different_bytes_do_not_block(self):
        snapshot = self.snapshot()
        before = deepcopy(snapshot)
        result = gate_conflicted_evidence(self.result(snapshot), snapshot)
        self.assertEqual(result["status"], "pass")
        self.assertFalse(result.get("version_conflicts"))
        self.assertEqual(before, snapshot)

    def test_opposite_same_instant_action_only_blocks_related_proposition(self):
        negative = action("stop", 1)
        negative.update(fact_id="negative-stop", evidence_refs=["negative-stop"])
        negative["fields"]["status"] = "not_done"
        snapshot = self.snapshot(stop_chain() + [negative])
        snapshot["evidence"][-1].update(file_id="b", content_hash="hash-b")
        result = self.result(snapshot)
        result["propositions"][0]["evidence_refs"].append("negative-stop")
        gated = gate_conflicted_evidence(result, snapshot)
        self.assertEqual(gated["propositions"][0]["state"], "missing")
        self.assertTrue(all(p["state"] == "true" for p in gated["propositions"][1:]))
        self.assertTrue(gated["source_relations"]["counts"]["fact_conflict"])
        self.assertEqual(gated, gate_conflicted_evidence(gated, snapshot))

    def test_different_weekly_records_same_report_date_are_parallel(self):
        snapshot = self.snapshot()
        for file, name in zip(snapshot["files"], ("验收记录10月5日-10月11日.pdf", "验收记录10月12日-10月18日.pdf")):
            file.update(original_name=name, material_group="relief_acceptance", report_date="2026/10/26")
        audit = analyze_source_relations(snapshot)
        self.assertEqual(audit["counts"]["parallel"], 1)
        self.assertEqual(audit["counts"]["fact_conflict"], 0)
        self.assertEqual(audit["counts"]["version_candidate"], 0)
        self.assertEqual(audit["relations"][0]["files"][0]["filename_period_hints"], ["10月5日-10月11日"])

    def test_latest_filename_is_advisory_and_identical_hash_is_duplicate(self):
        snapshot = self.snapshot()
        audit = analyze_source_relations(snapshot)
        self.assertEqual(audit["counts"]["version_candidate"], 1)
        self.assertFalse(audit["authority_selected"])
        self.assertFalse(any(row["blocks_verdict"] for row in audit["relations"]))
        snapshot["files"][1]["content_hash"] = "hash-a"
        audit = analyze_source_relations(snapshot)
        self.assertEqual(audit["counts"]["duplicate"], 1)
        self.assertEqual(audit["counts"]["version_candidate"], 0)

    def test_different_action_time_or_workface_or_candidate_does_not_conflict(self):
        for mode in ("time", "workface", "candidate"):
            negative = action("stop", 1)
            negative.update(fact_id="negative-stop", evidence_refs=["negative-stop"])
            negative["fields"]["status"] = "not_done"
            snapshot = self.snapshot(stop_chain() + [negative])
            snapshot["evidence"][-1].update(file_id="b", content_hash="hash-b")
            if mode == "time": negative["time"] = "2026-10-01T08:02:00"
            elif mode == "workface": negative["workface"] = "W2"
            else: negative["status"] = "candidate"
            # run_snapshot keeps the input facts, so this changes only the candidate fact.
            self.assertEqual(analyze_source_relations(snapshot)["counts"]["fact_conflict"], 0, mode)

    def test_incompatible_or_equivalent_threshold_units_not_false_conflict(self):
        fields = dict(method="应力监测法", metric="煤体应力", point="P1", value="10", unit="MPa",
                      approved=True, valid_from="2026-09-01", valid_to="2026-12-31")
        for value, unit, expected in (("10000", "kPa", 0), ("12", "MPa", 1), ("10", "m", 0)):
            facts = [fact("threshold", "left", fields.copy(), role="requirement"),
                     fact("threshold", "right", {**fields, "value": value, "unit": unit}, role="requirement")]
            snapshot = self.snapshot(facts)
            snapshot["evidence"][-1].update(file_id="b", content_hash="hash-b")
            audit = analyze_source_relations(snapshot)
            self.assertEqual(audit["counts"]["fact_conflict"], expected, (value, unit))

    def test_unrelated_proven_failure_survives_conflict(self):
        negative = action("stop", 1)
        negative.update(fact_id="negative-stop", evidence_refs=["negative-stop"])
        negative["fields"]["status"] = "not_done"
        snapshot = self.snapshot(stop_chain() + [negative])
        snapshot["evidence"][-1].update(file_id="b", content_hash="hash-b")
        result = self.result(snapshot)
        result.update(status="no_pass", findings=[{**result["propositions"][1], "state": "false"}])
        gated = gate_conflicted_evidence(result, snapshot)
        self.assertEqual(gated["status"], "no_pass")
        self.assertEqual([f["code"] for f in gated["findings"]], ["power_off"])

    def test_conflict_question_names_files_values_and_citations(self):
        negative = action("stop", 1)
        negative.update(fact_id="negative-stop", evidence_refs=["negative-stop"])
        negative["fields"]["status"] = "not_done"
        snapshot = self.snapshot(stop_chain() + [negative])
        snapshot["evidence"][-1].update(file_id="b", content_hash="hash-b")
        gated = gate_conflicted_evidence(self.result(snapshot), snapshot)
        answer = answer_question({"run_id": "run-a", "result": gated, "snapshot": snapshot}, snapshot, "具体是什么冲突")
        self.assertIn("日报（新）.pdf", answer["answer"])
        self.assertIn("明确未实施", answer["answer"])
        self.assertEqual(set(answer["evidence_refs"]), {"stop", "negative-stop"})

    def test_full_evaluator_does_not_poison_same_day_multifile(self):
        snapshot = self.snapshot()
        run, _ = run_snapshot("stop_work_unload", snapshot["facts"])
        result = evaluate_documents(run, snapshot)
        self.assertEqual(result["status"], "pass", result.get("coverage_gaps"))
        self.assertEqual(result["source_relations"]["counts"]["fact_conflict"], 0)

    def test_new_audit_without_conflicts_never_claims_legacy_conflict(self):
        snapshot = self.snapshot()
        result = self.result(snapshot)
        result["source_relations"] = {"counts": {}, "relations": []}
        answer = answer_question({"run_id": "run-a", "result": result}, snapshot, "有版本冲突吗")
        self.assertIn("没有记录", answer["answer"])
        self.assertNotIn("旧策略", answer["answer"])

    def test_version_candidate_does_not_hide_actual_gap_in_question(self):
        snapshot = self.snapshot()
        result = self.result(snapshot)
        result.update(status="pending", description="待补证", detail="识别服务失败，缺少事件依据",
                      coverage_gaps=["provider_failed"], source_relations=analyze_source_relations(snapshot))
        answer = answer_question({"run_id": "run-a", "result": result}, snapshot, "为什么待核验")
        self.assertIn("识别服务失败", answer["answer"])
        self.assertIn("页面处理失败", answer["answer"])

    def test_review_plan_names_blocking_layers_and_targets(self):
        from apps.control_api.review_plan import build_review_plan
        snapshot = self.snapshot()
        snapshot["coverage_audit"] = {"source_complete": False, "units": [
            {"file_id": "a", "unit_id": "page:2", "state": "unreadable"},
            {"file_id": "b", "unit_id": "document", "state": "not_indexed"},
        ]}
        run = {"work_units": [{"state": "failed", "error": "provider_failed",
                                "input": {"file_id": "a", "page": 2}}]}
        plan = build_review_plan(run, snapshot, {"propositions": []})
        self.assertEqual(plan["blocking_layer"], "evidence_collection")
        self.assertTrue(any(action["code"] == "business_rule_not_started" for action in plan["actions"]))
        provider = next(action for action in plan["actions"] if action["code"] == "provider_failed")
        self.assertEqual(provider["targets"][0]["file_name"], "日报.pdf")

    def test_dynamic_trigger_dependency_isolated_but_independent_warning_survives(self):
        left = action("hazard_analysis", 1, hazard_confirmed=True)
        right = action("hazard_analysis", 1, hazard_confirmed=False)
        left["fields"]["trigger_type"] = right["fields"]["trigger_type"] = "dynamic"
        right.update(fact_id="analysis-negative", evidence_refs=["analysis-negative"])
        snapshot = self.snapshot([left, right])
        snapshot["evidence"][-1].update(file_id="b", content_hash="hash-b")
        for refs, affected in ((["hazard_analysis"], True), (["independent-warning"], False)):
            result = self.result(snapshot)
            result["propositions"] = [dict(code="trigger", state="true", event_id="E1", workface="W1", evidence_refs=refs)]
            gated = gate_conflicted_evidence(result, snapshot)
            self.assertEqual(gated["propositions"][0]["state"], "missing" if affected else "true")


if __name__ == "__main__":
    unittest.main()
