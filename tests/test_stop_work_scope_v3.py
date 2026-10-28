"""User-confirmed three-step stop/evacuation rules, separate from legacy v2."""
import unittest
from hashlib import sha256

from apps.control_api.domain.evaluator import evaluate_run as evaluate_native_run
from apps.control_api.domain.scope_checks import evaluate_scope
from apps.control_api.domain.rulebook import DEFAULT_RULEBOOK_VERSION, load_rulebook
from apps.control_api.domain.rules import evaluate_documents
from tests.test_domain_rules import action, event, fact, run_snapshot


def chain(*, warning=False, danger=True, relief=False):
    source = event()
    if warning:
        source["fields"] = {"trigger_type": "warning"}
    records = [source]
    if warning:
        point = {"method": "应力监测法", "metric": "煤体应力", "point": "P1"}
        records += [fact("measurement", "monitor", {**point, "stage": "warning", "value": "11", "unit": "MPa"}),
                    fact("threshold", "approved-threshold", {**point, "value": "10", "unit": "MPa",
                        "approved": True, "valid_from": "2026-09-23", "valid_to": "2026-12-31"}, role="requirement")]
    records.append(action("hazard_analysis", 1, hazard_confirmed=danger))
    records.extend(action(name, minute, **({"route_conforms": True} if name == "evacuate" else {}))
                   for minute, name in ((2, "stop"), (3, "power_off"), (4, "evacuate"), (5, "report")))
    if relief:
        records += [fact("design", "design", {"approved": True, "target_scope": "W1", "valid_from": "2026-09-23",
                       "valid_to": "2026-12-31", "relief_methods": ["钻孔卸压"], "relief_area": "W1",
                       "relief_parameters": [{"name": "孔深", "operator": ">=", "value": "20", "unit": "m"}]}, role="requirement"),
                    action("relief", 6, method="钻孔卸压", relief_area="W1", parameters=[{"name": "孔深", "value": "20", "unit": "m"}],
                           acceptance_passed=True, acceptance_signer="验收人", acceptance_time="2026-10-01T08:07:00")]
    return records


def evaluate(records, **overrides):
    run, snapshot = run_snapshot("stop_work_unload", records, **overrides)
    snapshot["rule_baseline"] = load_rulebook(DEFAULT_RULEBOOK_VERSION)
    return evaluate_documents(run, snapshot)


class StopWorkV3Tests(unittest.TestCase):
    def test_monitoring_trigger_and_complete_measures_have_no_issue(self):
        result = evaluate(chain(warning=True, relief=True))
        self.assertEqual(result["status"], "pass", result["coverage_gaps"])
        self.assertEqual(result["classification"], "no_issue_observed")
        self.assertEqual([], result["findings"])
        self.assertFalse(result["law_refs"], "user rule must not inherit an unreviewed statutory citation")

    def test_dynamic_requires_positive_same_event_danger_assessment(self):
        records = chain()
        self.assertEqual("pass", evaluate(records)["status"])
        no_analysis = [record for record in records if record["fact_id"] != "hazard_analysis"]
        result = evaluate(no_analysis)
        self.assertEqual("pending", result["status"])
        self.assertFalse(result["findings"])

    def test_threshold_is_strictly_greater_including_drill_index(self):
        for method, metric, unit in (("应力监测法", "煤体应力", "MPa"), ("钻屑法", "钻粉率指数", "1")):
            with self.subTest(method=method):
                records = chain(warning=True)
                for row in records:
                    if row["kind"] in {"measurement", "threshold"}:
                        row["fields"].update(method=method, metric=metric, unit=unit, value="10")
                result = evaluate(records)
                self.assertEqual("pending", result["status"])
                self.assertFalse(any(p["code"] == "threshold_trigger" and p["state"] == "true" for p in result["propositions"]))

    def test_explicit_missing_stop_with_danger_is_major_candidate(self):
        records = chain(warning=True, relief=True)
        stop = next(row for row in records if row["fact_id"] == "stop")
        stop["fields"]["status"] = "not_done"
        result = evaluate(records)
        self.assertEqual("no_pass", result["status"])
        self.assertEqual("suspected_major_hazard", result["classification"])
        self.assertTrue(result["human_confirmation_required"])

    def test_proven_continued_mining_before_actual_stop_is_major_candidate(self):
        records = chain(warning=True, relief=True) + [action("continue_work", 2)]
        next(row for row in records if row["fact_id"] == "stop")["time"] = "2026-10-01T08:06:00"
        result = evaluate(records)
        self.assertEqual("no_pass", result["status"])
        self.assertEqual("suspected_major_hazard", result["classification"])

    def test_absent_stop_record_is_review_gap_not_negative(self):
        records = [row for row in chain(warning=True, relief=True) if row["fact_id"] != "stop"]
        result = evaluate(records)
        self.assertEqual("pending", result["status"])
        self.assertFalse(result["findings"])

    def test_only_power_or_dispatch_failure_is_violation_candidate(self):
        for name in ("power_off", "report"):
            with self.subTest(name=name):
                records = chain(warning=True, danger=False)
                next(row for row in records if row["fact_id"] == name)["fields"]["status"] = "not_done"
                result = evaluate(records)
                self.assertEqual("no_pass", result["status"])
                self.assertEqual("suspected_violation", result["classification"])
                self.assertFalse(result["human_confirmation_required"])

    def test_continued_work_without_relief_evidence_remains_pending(self):
        records = chain(warning=True) + [action("continue_work", 8)]
        result = evaluate(records)
        self.assertEqual("pending", result["status"])
        self.assertFalse(result["findings"])

    def test_explicit_unsuitable_relief_and_later_mining_is_major_candidate(self):
        records = chain(warning=True, relief=True) + [action("continue_work", 9)]
        next(row for row in records if row["fact_id"] == "relief")["fields"]["method"] = "未批准工法"
        result = evaluate(records)
        self.assertEqual("no_pass", result["status"])
        self.assertEqual("suspected_major_hazard", result["classification"])

    def test_unverified_design_does_not_escalate_unsuitable_relief(self):
        records = [record for record in chain(warning=True, relief=True) if record["fact_id"] != "design"]
        records.append(action("continue_work", 9))
        next(row for row in records if row["fact_id"] == "relief")["fields"]["acceptance_passed"] = False
        result = evaluate(records)
        self.assertEqual("pending", result["status"])
        self.assertFalse(result["findings"])

    def test_explicit_no_relief_covering_actual_mining_is_major_candidate(self):
        denied = action("relief", 7)
        denied["fields"].update(status="not_done", status_valid_from="2026-10-01T08:07:00",
                                status_valid_to="2026-10-01T08:10:00")
        result = evaluate(chain(warning=True) + [denied, action("continue_work", 8)])
        self.assertEqual("no_pass", result["status"], result["coverage_gaps"])
        self.assertTrue(any(f["code"] == "relief_violation" for f in result["findings"]))
        self.assertEqual("suspected_major_hazard", result["classification"])

    def test_wrong_evacuation_route_is_major_candidate_but_missing_route_is_gap(self):
        records = chain(warning=True, relief=True)
        evacuation = next(row for row in records if row["fact_id"] == "evacuate")
        evacuation["fields"]["route_conforms"] = False
        result = evaluate(records)
        self.assertEqual("suspected_major_hazard", result["classification"])
        self.assertTrue(any(f["code"] == "evacuate" for f in result["findings"]))
        evacuation["fields"].pop("route_conforms")
        result = evaluate(records)
        self.assertEqual("pending", result["status"])

    def test_unreadable_or_out_of_scope_evidence_cannot_be_called_no_issue(self):
        result = evaluate(chain(warning=True, relief=True), coverage=[{"file_id": "F1", "unit_id": "page:1", "state": "unreadable"}])
        self.assertEqual("unverifiable", result["status"])
        self.assertFalse(result["findings"])

    def test_no_hazard_warning_does_not_require_relief(self):
        result = evaluate(chain(warning=True, danger=False))
        self.assertEqual("pass", result["status"], result["coverage_gaps"])
        self.assertFalse(next(p for p in result["propositions"] if p["code"] == "warning_danger_confirmed")["applicable"])

    def test_native_six_group_records_reach_v3_without_model_verdict(self):
        lines = [
            ("prevention_daily", "actual_record", "事件编号:E1;工作面:W1;时间:2026-10-01T08:00:00;记录类型:预警"),
            ("prevention_daily", "actual_record", "事件编号:E1;工作面:W1;时间:2026-10-01T08:00:00;阶段:预警;方法:应力监测法;指标名称:煤体应力;测点:P1;数值:11;单位:MPa"),
            ("threshold", "requirement", "事件编号:E1;工作面:W1;时间:2026-10-01T08:00:00;方法:应力监测法;指标名称:煤体应力;测点:P1;数值:10;单位:MPa;批复:是;有效起始:2026-09-23;有效截止:2026-12-31"),
            ("prevention_daily", "actual_record", "事件编号:E1;工作面:W1;时间:2026-10-01T08:01:00;动作:危险研判;状态:已完成;执行人角色:operator;目标范围:W1;危险研判:有危险"),
            ("dispatch_daily", "actual_record", "事件编号:E1;工作面:W1;时间:2026-10-01T08:02:00;动作:停工;状态:已完成;执行人角色:operator;目标范围:W1"),
            ("dispatch_daily", "actual_record", "事件编号:E1;工作面:W1;时间:2026-10-01T08:03:00;动作:断电;状态:已完成;执行人角色:operator;目标范围:W1"),
            ("dispatch_daily", "actual_record", "事件编号:E1;工作面:W1;时间:2026-10-01T08:04:00;动作:撤人;状态:已完成;执行人角色:operator;目标范围:W1;路线符合:是"),
            ("dispatch_daily", "actual_record", "事件编号:E1;工作面:W1;时间:2026-10-01T08:05:00;动作:报告;状态:已完成;执行人角色:operator;目标范围:W1"),
            ("design", "requirement", '事件编号:E1;工作面:W1;时间:2026-10-01T08:00:00;记录类型:防冲设计;批复:是;目标范围:W1;有效起始:2026-09-23;有效截止:2026-12-31;解危方法全集:["钻孔卸压"];解危区域:W1;解危参数要求:[{"name":"孔深","operator":">=","value":"20","unit":"m"}]'),
            ("relief_acceptance", "actual_record", '事件编号:E1;工作面:W1;时间:2026-10-01T08:06:00;动作:卸压;状态:已完成;执行人角色:operator;目标范围:W1;方法:钻孔卸压;解危区域:W1;施工参数:[{"name":"孔深","value":"20","unit":"m"}];验收通过:是;验收人:李工;验收时间:2026-10-01T08:07:00'),
        ]
        files, evidence = [], []
        for index, (group, role, text) in enumerate(lines):
            file_id, digest = f"f{index}", sha256(text.encode()).hexdigest()
            files.append({"file_id": file_id, "content_hash": digest, "material_group": group})
            evidence.append({"evidence_id": f"e{index}", "file_id": file_id, "document_id": file_id,
                             "document_version": digest, "content_hash": digest, "source_role": role,
                             "material_group": group, "text": text, "quote": text,
                             "locator": {"line": 1}})
        result = evaluate_native_run({"run_id": "run-native", "mine_id": "M1", "item_code": "stop_work_unload", "check_cycle": "2026-10"},
                                     {"run_id": "run-native", "mine_id": "M1", "snapshot_id": "snapshot-native",
                                      "source_complete": True, "files": files, "evidence": evidence, "coverage_gaps": []})
        self.assertEqual("pass", result["status"], result["coverage_gaps"])
        self.assertEqual("no_issue_observed", result["classification"])

    def test_v3_never_executes_legacy_risk_signature_or_personnel_checks(self):
        run, snapshot = run_snapshot("stop_work_unload", chain(warning=True, relief=True))
        snapshot["rule_baseline"] = load_rulebook(DEFAULT_RULEBOOK_VERSION)
        scoped = evaluate_scope(run, snapshot)
        self.assertEqual([], scoped["propositions"])
        self.assertEqual([], scoped["gaps"])


if __name__ == "__main__":
    unittest.main()
