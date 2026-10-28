"""Behavioral boundary tests for evidence-based deterministic rule evaluation."""
from copy import deepcopy
import unittest

from apps.control_api.domain.contracts import InvalidRequest, validate_result
from apps.control_api.domain.rules import evaluate_documents, normalize_method


def fact(kind, number, fields, when="2026-10-01T08:00:00", role="actual_record", **scope):
    return {"fact_id": str(number), "kind": kind, "event_id": "E1", "workface": "W1",
            "time": when, "source_role": role, "evidence_refs": [str(number)], "fields": fields, **scope}


def action(name, minute, **fields):
    return fact("action", name, {"action": name, "status": "done", "actor_role": "operator",
                                 "target_scope": "W1", **fields}, f"2026-10-01T08:{minute:02d}:00")


def event(**fields):
    return fact("event", "event", {"trigger_type": "dynamic", "hazard_confirmed": True, **fields})


def run_snapshot(item, facts, **overrides):
    run = {"run_id": "run-a", "spec": {"source_system": "local", "mine_code": "M1", "environment": "local",
           "item_code": item, "check_cycle": "2026-10", "definition_version": "statutory-local-v1"}}
    owner = {"run_id": "run-a", "source_system": "local", "mine_code": "M1", "environment": "local"}
    snapshot = {**owner, "snapshot_id": "snapshot-a", "files": [{"file_id": "F1", "content_hash": "hash"}],
                "evidence": [{**owner, "snapshot_id": "snapshot-a", "evidence_id": f["fact_id"],
                              "content_hash": "hash", "source_role": f["source_role"], "text": "raw actual record", "locator": {"row": i}}
                             for i, f in enumerate(facts)],
                "facts": facts, "source_complete": True, "coverage_gaps": [],
                "observation": {"start": "2026-10-01T00:00:00", "end": "2026-10-01T23:59:59"}, **overrides}
    return run, snapshot


def evaluate_chain(item, chain, **snapshot_fields):
    """Module-level helper for test classes that are not RulesTests."""
    run, snapshot = run_snapshot(item, chain, **snapshot_fields)
    result = evaluate_documents(run, snapshot)
    validate_result(result, run, snapshot)
    return result


def stop_chain():
    return [event(), *(action(name, 1, immediate=True, **({"route_conforms": True} if name == "evacuate" else {}))
                       for name in ("stop", "power_off", "evacuate", "report")), action("relief", 5)]


def effect_chain(value="8", threshold="10", unit="MPa", threshold_unit="MPa"):
    point = {"method": "应力监测法", "metric": "煤体应力", "point": "P1"}
    return [event(), action("relief", 1), action("inspection", 2),
            fact("requirements", "points", {"required_points": [point], "approved": True}, role="requirement"),
            fact("threshold", "threshold", {**point, "value": threshold, "unit": threshold_unit, "approved": True,
                                             "valid_from": "2026-09-23"}, role="requirement"),
            fact("measurement", "measurement", {**point, "stage": "effect", "value": value, "unit": unit}, "2026-10-01T08:03:00"),
            action("technical_confirm", 4, actor_role="technical_head"), action("dispatch_release", 5, actor_role="dispatch"),
            action("resume", 6)]


class RulesTests(unittest.TestCase):
    def evaluate(self, item, facts, **snapshot_fields):
        run, snapshot = run_snapshot(item, facts, **snapshot_fields)
        result = evaluate_documents(run, snapshot)
        validate_result(result, run, snapshot)
        self.assertEqual("local-documents", result["profile"])
        self.assertEqual(run["spec"]["definition_version"], result["definition_version"])
        self.assertTrue(all(set(p["evidence_refs"]) <= {e["evidence_id"] for e in snapshot["evidence"]} for p in result["propositions"]))
        return result

    def test_complete_actual_stop_and_effect_chains_pass(self):
        for item, chain in (("stop_work_unload", stop_chain()), ("relief_effects", effect_chain())):
            with self.subTest(item=item):
                result = self.evaluate(item, chain)
                self.assertEqual("pass", result["status"], result["coverage_gaps"])
                self.assertTrue(result["law_refs"])

    def test_rulebook_unregistered_statutes_are_never_cited(self):
        # 规则表 I2/I3 只登记了 19/55/57/64/65/75/78/108 等条；27/59 是历史硬编码
        for item, chain in (("stop_work_unload", stop_chain()), ("relief_effects", effect_chain())):
            with self.subTest(item=item):
                result = self.evaluate(item, chain)
                articles = {law["article"] for law in result["law_refs"]}
                self.assertNotIn("第27条", articles)
                self.assertNotIn("第59条", articles)
                for proposition in result["propositions"]:
                    self.assertNotIn("第27条", {law["article"] for law in proposition["law_refs"]})

    def test_percent_and_unitless_threshold_are_never_silently_converted(self):
        # 1.2 % 与阈值 1（无量纲）本是不同刻度：历史实现会换算成 0.012 < 1 并判为
        # "低于阈值"（假 pass）。刻度不一致时必须拒绝比较，只允许 pending。
        result = self.evaluate("relief_effects",
                               effect_chain(value="1.2", threshold="1", unit="%", threshold_unit="1"))
        self.assertEqual("pending", result["status"])
        self.assertEqual([], result["findings"])
        threshold = next(p for p in result["propositions"] if p["code"] == "applicable_threshold")
        self.assertEqual("missing", threshold["state"])
        self.assertIn("scale", threshold["detail"])
        self.assertTrue(any("刻度" in note for note in result["limitations"]))

    def test_same_scale_units_still_compare_after_conversion(self):
        for unit, threshold_unit, value, threshold in (("MPa", "kPa", "8", "10000"), ("m", "mm", "2", "3500")):
            with self.subTest(unit=unit):
                chain = effect_chain(value=value, threshold=threshold, unit=unit, threshold_unit=threshold_unit)
                self.assertEqual("pass", self.evaluate("relief_effects", chain)["status"])

    def test_missing_stop_record_and_requirement_cannot_prove_execution(self):
        for mode in ("remove", "requirement", "statement"):
            chain = stop_chain()
            stop = next(f for f in chain if f["fact_id"] == "stop")
            if mode == "remove": chain.remove(stop)
            else: stop["source_role"] = mode
            self.assertEqual("pending", self.evaluate("stop_work_unload", chain)["status"])

    def test_positive_continued_work_is_no_pass_and_retains_other_gaps(self):
        chain = stop_chain() + [action("continue_work", 2)]
        chain = [f for f in chain if f["fact_id"] != "report"]
        result = self.evaluate("stop_work_unload", chain, source_complete=False, coverage_gaps=["missing source page"])
        self.assertEqual("no_pass", result["status"])
        self.assertIn("missing source page", result["coverage_gaps"])
        self.assertTrue(any(p["code"] == "continued_after_trigger" for p in result["findings"]))

    def test_continue_before_trigger_or_unrelated_event_cannot_create_violation(self):
        chain = stop_chain() + [action("continue_work", 0)]
        chain[-1]["time"] = "2026-10-01T07:59:00"
        self.assertNotEqual("no_pass", self.evaluate("stop_work_unload", chain)["status"])
        chain[-1]["event_id"] = "other-event"
        self.assertEqual("pending", self.evaluate("stop_work_unload", chain)["status"])

    def test_equal_warning_does_not_trigger_except_drill_powder_index(self):
        for method, metric, unit, status in (("应力监测法", "煤体应力", "MPa", "pending"),
                                             ("钻屑法", "钻粉率指数", "1", "pass")):
            chain = stop_chain()
            chain[0]["fields"]["trigger_type"] = "warning"
            fields = {"method": method, "metric": metric, "point": "P1", "value": "10", "unit": unit}
            chain += [fact("measurement", "warning", {**fields, "stage": "warning"}),
                      fact("threshold", "threshold", {**fields, "approved": True, "valid_from": "2026-09-23"}, role="requirement")]
            self.assertEqual(status, self.evaluate("stop_work_unload", chain)["status"])

    def test_warning_without_danger_analysis_does_not_assume_relief(self):
        chain = stop_chain()
        chain[0]["fields"].pop("hazard_confirmed")
        self.assertEqual("pending", self.evaluate("stop_work_unload", chain)["status"])

    def draft_baseline(self):
        return {"version": "impact-1023-draft-v1", "revision": "draft-revision",
                "status": "development-draft", "source": {"sheet": "AI深度监察"}}

    def test_draft_action_precedence_rejects_stop_after_evacuation(self):
        for later_name, minute in (("evacuate", "03"), ("relief", "06")):
            with self.subTest(later=later_name):
                chain = stop_chain()
                for fact_item in chain:
                    if fact_item["fact_id"] in {"stop", "power_off"}:
                        fact_item["time"] = f"2026-10-01T08:{minute}:00"
                self.assertEqual("pass", self.evaluate("stop_work_unload", chain)["status"])
                result = self.evaluate("stop_work_unload", chain, rule_baseline=self.draft_baseline())
                self.assertEqual("no_pass", result["status"])
                for first_name in ("stop", "power_off"):
                    finding = next(p for p in result["findings"] if p["code"] == f"draft_{first_name}_not_after_{later_name}")
                    self.assertTrue({first_name, later_name} <= set(finding["evidence_refs"]))
                    self.assertEqual(["AI深度监察!E2", "AI深度监察!F2"], finding["rule_refs"][0]["source_cells"])

    def test_draft_action_precedence_allows_equal_precise_timestamps(self):
        chain = stop_chain()
        for fact_item in chain:
            if fact_item["fact_id"] in {"stop", "power_off", "evacuate", "relief"}:
                fact_item["time"] = "2026-10-01T08:01:00"
        result = self.evaluate("stop_work_unload", chain, rule_baseline=self.draft_baseline())
        self.assertEqual("pass", result["status"], result["coverage_gaps"])
        self.assertTrue(all(p["state"] == "true" for p in result["propositions"]
                            if p["code"].startswith("draft_")))

    def test_draft_action_precedence_does_not_invent_order_from_dates(self):
        chain = stop_chain()
        for fact_item in chain:
            if fact_item["fact_id"] in {"stop", "power_off", "evacuate", "relief"}:
                fact_item["time"] = "2026-10-01"
        result = self.evaluate("stop_work_unload", chain, rule_baseline=self.draft_baseline())
        self.assertEqual("pending", result["status"])
        self.assertTrue(any(p["code"] == "draft_stop_not_after_evacuate" and p["state"] == "missing"
                            for p in result["propositions"]))

    def test_independent_hazard_analysis_proves_work_before_relief_is_no_pass(self):
        chain = [f for f in stop_chain() if f["fact_id"] != "relief"]
        chain[0]["fields"].pop("hazard_confirmed")
        chain += [action("hazard_analysis", 1, hazard_confirmed=True), action("continue_work", 2), action("relief", 5)]
        result = self.evaluate("stop_work_unload", chain)
        self.assertEqual("no_pass", result["status"])
        finding = next(p for p in result["findings"] if p["code"] == "continued_after_trigger")
        self.assertTrue({"event", "hazard_analysis", "continue_work", "relief"} <= set(finding["evidence_refs"]))

    def test_conflicting_hazard_analyses_do_not_choose_a_value(self):
        chain = [f for f in stop_chain() if f["fact_id"] != "relief"]
        chain[0]["fields"].pop("hazard_confirmed")
        negative = action("hazard_analysis", 2, hazard_confirmed=False)
        negative.update(fact_id="negative-analysis", evidence_refs=["negative-analysis"])
        chain += [action("hazard_analysis", 1, hazard_confirmed=True), negative,
                  action("continue_work", 3), action("relief", 5)]
        result = self.evaluate("stop_work_unload", chain)
        self.assertEqual("pending", result["status"])
        self.assertFalse(result["findings"])
        self.assertTrue(any(p["code"] == "hazard_analysis" and p["state"] == "missing"
                            for p in result["propositions"]))

    def test_future_hazard_analysis_cannot_prove_earlier_continuation(self):
        chain = [f for f in stop_chain() if f["fact_id"] != "relief"]
        chain[0]["fields"].pop("hazard_confirmed")
        chain += [action("continue_work", 2), action("hazard_analysis", 5, hazard_confirmed=True), action("relief", 6)]
        result = self.evaluate("stop_work_unload", chain)
        self.assertEqual("pending", result["status"])
        self.assertFalse(result["findings"])

    def test_event_analysis_conflict_does_not_hide_immediate_action_failure(self):
        chain = stop_chain() + [action("hazard_analysis", 1, hazard_confirmed=False), action("continue_work", 2)]
        result = self.evaluate("stop_work_unload", chain)
        self.assertEqual("pending", result["status"])
        self.assertFalse(result["findings"])
        next(f for f in chain if f["fact_id"] == "stop")["fields"]["immediate"] = False
        result = self.evaluate("stop_work_unload", chain)
        self.assertEqual("no_pass", result["status"])
        self.assertEqual(["stop_immediate"], [p["code"] for p in result["findings"]])

    def test_immediate_and_evacuation_route_and_actor_are_required(self):
        for field, target in (("immediate", "stop"), ("route_conforms", "evacuate"), ("actor_role", "report"), ("target_scope", "stop")):
            chain = stop_chain()
            next(f for f in chain if f["fact_id"] == target)["fields"].pop(field)
            self.assertEqual("pending", self.evaluate("stop_work_unload", chain)["status"])

    def test_day_precision_does_not_prove_same_day_sequence(self):
        chain = stop_chain()
        for f in chain: f["time"] = "2026-10-01"
        self.assertEqual("pending", self.evaluate("stop_work_unload", chain)["status"])

    def test_equal_action_timestamps_do_not_establish_a_sequence(self):
        chain = effect_chain()
        for f in chain:
            if f["kind"] in {"action", "measurement"}:
                f["time"] = "2026-10-01T08:03:00"
        result = self.evaluate("relief_effects", chain)
        self.assertEqual("pending", result["status"])
        self.assertFalse(result["findings"])
        self.assertTrue(any("precision" in str(g) for g in result["coverage_gaps"]))

    def test_single_day_observation_includes_all_events_on_that_date(self):
        for item, chain in (("stop_work_unload", stop_chain()), ("relief_effects", effect_chain())):
            with self.subTest(item=item):
                result = self.evaluate(item, chain, observation={"start": "2026-10-01", "end": "2026-10-01"})
                self.assertEqual("pass", result["status"], result["coverage_gaps"])
                result = self.evaluate(item, chain, observation={"start": "2026-10-02", "end": "2026-10-02"})
                self.assertEqual("pending", result["status"])

    def test_threshold_validity_includes_exact_datetime_endpoints(self):
        for boundary in ("valid_from", "valid_to"):
            with self.subTest(boundary=boundary):
                chain = effect_chain()
                threshold = next(f for f in chain if f["fact_id"] == "threshold")
                threshold["fields"][boundary] = "2026-10-01T08:03:00"
                result = self.evaluate("relief_effects", chain)
                self.assertEqual("pass", result["status"], result["coverage_gaps"])

    def test_datetime_observation_endpoints_are_inclusive(self):
        result = self.evaluate("relief_effects", effect_chain(),
                               observation={"start": "2026-10-01T08:00:00", "end": "2026-10-01T08:06:00"})
        self.assertEqual("pass", result["status"], result["coverage_gaps"])

    def test_effect_equals_or_exceeds_threshold_with_resumption_is_no_pass(self):
        for value in ("10", "10.0001"):
            result = self.evaluate("relief_effects", effect_chain(value))
            self.assertEqual("no_pass", result["status"])
            self.assertTrue(all(f["major_hazard_classification"] == "not_assessed" for f in result["findings"]))

    def test_no_resume_with_unsafe_or_missing_records_remains_pending(self):
        chain = [f for f in effect_chain("10") if f["fact_id"] != "resume"]
        next(f for f in chain if f["fact_id"] == "technical_confirm")["fields"]["status"] = "not_done"
        self.assertEqual("pending", self.evaluate("relief_effects", chain)["status"])

    def test_missing_authorization_is_gap_not_positive_violation(self):
        chain = [f for f in effect_chain() if f["fact_id"] != "dispatch_release"]
        self.assertEqual("pending", self.evaluate("relief_effects", chain)["status"])
        chain.append(action("dispatch_release", 8, actor_role="dispatch"))
        self.assertEqual("no_pass", self.evaluate("relief_effects", chain)["status"])

    def test_explicit_negative_authorization_with_resume_is_no_pass(self):
        chain = effect_chain()
        next(f for f in chain if f["fact_id"] == "dispatch_release")["fields"]["status"] = "not_done"
        # A point denial at 08:05 does not prove the 08:06 resumption lacked a release.
        self.assertEqual("pending", self.evaluate("relief_effects", chain)["status"])
        denied = next(f for f in chain if f["fact_id"] == "dispatch_release")
        denied["time"] = "2026-10-01T08:06:00"
        self.assertEqual("no_pass", self.evaluate("relief_effects", chain)["status"])

    def test_measurement_before_inspection_is_unresolved_without_interval_semantics(self):
        chain = effect_chain()
        next(f for f in chain if f["fact_id"] == "measurement")["time"] = "2026-10-01T08:01:30"
        self.assertEqual("pending", self.evaluate("relief_effects", chain)["status"])

    def test_wrong_scope_denial_cannot_make_other_workface_resume_no_pass(self):
        chain = effect_chain()
        denied = next(f for f in chain if f["fact_id"] == "dispatch_release")
        denied["fields"]["status"] = "not_done"
        denied["fields"]["target_scope"] = "W2"
        self.assertEqual("pending", self.evaluate("relief_effects", chain)["status"])

    def test_later_or_wrong_responsibility_denial_does_not_prove_earlier_failure(self):
        for mode in ("future", "wrong_role", "before_event"):
            with self.subTest(mode=mode):
                chain = effect_chain()
                denied = next(f for f in chain if f["fact_id"] == "dispatch_release")
                denied["fields"]["status"] = "not_done"
                denied["time"] = {"future": "2026-10-01T08:20:00", "before_event": "2026-10-01T07:59:00"}.get(mode, "2026-10-01T08:06:00")
                if mode == "wrong_role": denied["fields"]["actor_role"] = "operator"
                self.assertEqual("pending", self.evaluate("relief_effects", chain)["status"])

    def test_explicit_negative_status_interval_can_cover_actual_resumption(self):
        chain = effect_chain()
        denied = next(f for f in chain if f["fact_id"] == "dispatch_release")
        denied["fields"].update(status="not_done", status_valid_from="2026-10-01T08:05:00", status_valid_to="2026-10-01T08:07:00")
        self.assertEqual("no_pass", self.evaluate("relief_effects", chain)["status"])
        denied["fields"]["status_valid_to"] = "2026-10-01T08:05:30"
        self.assertEqual("pending", self.evaluate("relief_effects", chain)["status"])

    def test_explicit_requirement_validity_excludes_past_measurement(self):
        chain = effect_chain()
        requirement = next(f for f in chain if f["fact_id"] == "points")
        requirement["fields"]["valid_from"] = "2026-10-02"
        self.assertEqual("pending", self.evaluate("relief_effects", chain)["status"])

    def test_requirement_validity_boundaries_and_invalid_dates(self):
        for field, value, expected in (("valid_from", "2026-10-01T08:03:00", "pass"),
                                       ("valid_to", "2026-10-01T08:03:00", "pass"),
                                       ("valid_to", "2026-09-30", "pending"),
                                       ("valid_from", "not-a-date", "pending"),
                                       ("valid_to", "not-a-date", "pending")):
            with self.subTest(field=field, value=value):
                chain = effect_chain()
                next(f for f in chain if f["fact_id"] == "points")["fields"][field] = value
                self.assertEqual(expected, self.evaluate("relief_effects", chain)["status"])

    def test_undated_required_point_approval_exposes_its_applicability_limit(self):
        result = self.evaluate("relief_effects", effect_chain())
        self.assertEqual("pass", result["status"])
        self.assertTrue(any("no validity dates" in note for note in result["limitations"]))

    def test_required_points_full_set_and_approved_basis_are_necessary(self):
        chain = effect_chain()
        req = next(f for f in chain if f["fact_id"] == "points")
        req["fields"]["required_points"].append({"method": "钻屑法", "metric": "钻粉量", "point": "P2"})
        self.assertEqual("pending", self.evaluate("relief_effects", chain)["status"])
        req["fields"]["approved"] = False
        self.assertEqual("pending", self.evaluate("relief_effects", chain)["status"])

    def test_expired_conflicting_or_intraday_future_thresholds_do_not_pass(self):
        for mode in ("expired", "conflict", "future"):
            chain = effect_chain()
            threshold = next(f for f in chain if f["fact_id"] == "threshold")
            if mode == "expired": threshold["fields"]["valid_to"] = "2026-09-30"
            elif mode == "future": threshold["fields"]["valid_from"] = "2026-10-01T10:00:00"
            else:
                duplicate = deepcopy(threshold)
                duplicate.update(fact_id="other-threshold", evidence_refs=["other-threshold"])
                duplicate["fields"]["value"] = "12"
                chain.append(duplicate)
            self.assertEqual("pending", self.evaluate("relief_effects", chain)["status"])

    def test_decimal_unit_conversion_and_incompatible_dimension(self):
        self.assertEqual("pass", self.evaluate("relief_effects", effect_chain("8000", "10", "kPa", "MPa"))["status"])
        self.assertEqual("no_pass", self.evaluate("relief_effects", effect_chain("10000", "10", "kPa", "MPa"))["status"])
        for unit in ("kg/m", "unsupported", "NaN"):
            self.assertEqual("pending", self.evaluate("relief_effects", effect_chain(unit=unit))["status"])

    def test_drill_powder_cannot_be_measured_in_pressure(self):
        chain = effect_chain()
        for f in chain:
            if f["kind"] in {"threshold", "measurement"}: f["fields"].update(method="钻屑法", metric="钻粉率指数")
            if f["kind"] == "requirements": f["fields"]["required_points"][0].update(method="钻屑法", metric="钻粉率指数")
        self.assertEqual("pending", self.evaluate("relief_effects", chain)["status"])

    def test_cross_workface_and_event_measurement_cannot_complete_required_points(self):
        for field in ("event_id", "workface"):
            chain = effect_chain()
            next(f for f in chain if f["fact_id"] == "measurement")[field] = "other"
            self.assertEqual("pending", self.evaluate("relief_effects", chain)["status"])

    def test_summary_or_regulation_measurement_cannot_substitute_raw_value(self):
        for role in ("statement", "regulation", "requirement"):
            chain = effect_chain()
            next(f for f in chain if f["fact_id"] == "measurement")["source_role"] = role
            self.assertEqual("pending", self.evaluate("relief_effects", chain)["status"])

    def test_historical_rule_date_and_unknown_event_do_not_auto_pass(self):
        chain = stop_chain()
        chain[0]["time"] = "2026-09-22T08:00:00"
        self.assertEqual("pending", self.evaluate("stop_work_unload", chain)["status"])
        self.assertEqual("pending", self.evaluate("stop_work_unload", [])["status"])

    def test_source_and_content_coverage_gate_pass_and_unreadability(self):
        self.assertEqual("pending", self.evaluate("stop_work_unload", stop_chain(), source_complete=False)["status"])
        self.assertEqual("pending", self.evaluate("stop_work_unload", stop_chain(), coverage_gaps=["unobserved page"])["status"])
        self.assertEqual("unverifiable", self.evaluate("stop_work_unload", stop_chain(), coverage_gaps=["image_requires_ocr"])["status"])

    def test_registered_evidence_and_owner_are_enforced(self):
        run, snapshot = run_snapshot("stop_work_unload", stop_chain())
        snapshot["facts"][0]["evidence_refs"] = ["not-in-snapshot"]
        with self.assertRaises(InvalidRequest): evaluate_documents(run, snapshot)
        run, snapshot = run_snapshot("stop_work_unload", stop_chain())
        snapshot["mine_code"] = "other"
        with self.assertRaises(InvalidRequest): evaluate_documents(run, snapshot)

    def test_all_event_groups_are_checked_and_historical_gap_does_not_hide_current_violation(self):
        chain = stop_chain() + [action("continue_work", 2)]
        old = fact("event", "old", {"trigger_type": "dynamic"}, "2026-09-01T08:00:00", event_id="old-event")
        result = self.evaluate("stop_work_unload", chain + [old])
        self.assertEqual("no_pass", result["status"])
        self.assertTrue(any("historical" in str(g) for g in result["coverage_gaps"]))
        self.assertEqual(2, len(result["events"]))

    def test_uncited_and_conflicting_records_cannot_pass(self):
        run, snapshot = run_snapshot("stop_work_unload", stop_chain())
        snapshot["facts"][1]["evidence_refs"] = []
        with self.assertRaises(InvalidRequest):
            evaluate_documents(run, snapshot)
        chain = stop_chain()
        negative = deepcopy(chain[1])
        negative.update(fact_id="denied-stop", evidence_refs=["denied-stop"])
        negative["fields"]["status"] = "not_done"
        self.assertEqual("pending", self.evaluate("stop_work_unload", chain + [negative])["status"])

    def test_positive_problem_takes_precedence_over_unreadability_without_discarding_it(self):
        result = self.evaluate("relief_effects", effect_chain("10"), coverage_gaps=["image_requires_ocr"])
        self.assertEqual("no_pass", result["status"])
        self.assertIn("image_requires_ocr", result["coverage_gaps"])

    def test_resumption_same_day_does_not_make_threshold_equality_a_positive_violation(self):
        chain = effect_chain("10")
        next(f for f in chain if f["fact_id"] == "resume")["time"] = "2026-10-01"
        self.assertEqual("pending", self.evaluate("relief_effects", chain)["status"])

    def test_explicitly_delayed_response_is_a_positive_problem(self):
        chain = stop_chain()
        next(f for f in chain if f["fact_id"] == "stop")["fields"]["immediate"] = False
        self.assertEqual("no_pass", self.evaluate("stop_work_unload", chain)["status"])

    def test_generic_report_of_success_cannot_replace_required_raw_metrics(self):
        chain = [f for f in effect_chain() if f["kind"] != "measurement"]
        chain.append(fact("measurement", "summary", {"stage": "effect", "value": "合格"}, role="statement"))
        self.assertEqual("pending", self.evaluate("relief_effects", chain)["status"])

    def test_method_normalization_is_bounded(self):
        self.assertEqual("应力监测法", normalize_method(" 应力监测 "))
        self.assertNotEqual(normalize_method("微震监测"), normalize_method("应力监测"))

class GradeThresholdTests(unittest.TestCase):
    """色阶阈值（蓝/黄/橙/红）必须按事件危险等级取档，取不到就不比较。"""

    def graded_chain(self, *, event_grade, measured="13"):
        chain = effect_chain(value=measured, threshold="10")
        event_fact = next(f for f in chain if f["kind"] == "event")
        if event_grade is not None:
            event_fact["fields"]["risk_grade"] = event_grade
        chain = [f for f in chain if f["fact_id"] != "threshold"]
        point = {"method": "应力监测法", "metric": "煤体应力", "point": "P1"}
        for index, (grade, value) in enumerate((("蓝", "3"), ("红", "12"))):
            chain.append(fact("threshold", "threshold-" + grade,
                              {**point, "value": value, "unit": "MPa", "approved": True,
                               "valid_from": "2026-09-23", "grade": grade},
                              role="requirement")
                         )
        return chain

    def test_strong_risk_uses_the_red_threshold(self):
        result = evaluate_chain("relief_effects", self.graded_chain(event_grade="强冲击危险"))
        # 实测 13 > 红档 12，不低于阈值 → 已复工即为不合规
        self.assertEqual("no_pass", result["status"], result["coverage_gaps"])
        used = {ref for finding in result["findings"] for ref in finding["evidence_refs"]}
        self.assertIn("threshold-红", used)
        self.assertNotIn("threshold-蓝", used)

    def test_missing_event_grade_refuses_the_comparison(self):
        result = evaluate_chain("relief_effects", self.graded_chain(event_grade=None))
        self.assertEqual("pending", result["status"])
        threshold = next(p for p in result["propositions"] if p["code"] == "applicable_threshold")
        self.assertEqual("missing", threshold["state"])
        self.assertIn("色阶", threshold["detail"])
        self.assertTrue(any("色阶" in note for note in result["limitations"]))

    def test_grade_without_matching_bucket_is_not_compared(self):
        result = evaluate_chain("relief_effects", self.graded_chain(event_grade="中等冲击危险"))
        self.assertEqual("pending", result["status"])
        threshold = next(p for p in result["propositions"] if p["code"] == "applicable_threshold")
        self.assertIn("匹配", threshold["detail"])


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
