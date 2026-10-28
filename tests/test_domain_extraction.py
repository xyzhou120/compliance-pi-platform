import json
import unittest

from apps.control_api.domain.fact_extraction import extract_facts


def row(cells, headers, *, role="actual_record", eid="e1", **metadata):
    return {"evidence_id": eid, "text": "\t".join(str(x) for x in cells), "kind": "table_row",
            "source_role": role, "file_id": "f1", "locator": {"table": 1, "row": 2},
            "metadata": {"headers": headers, "cells": cells, **metadata}}


def record(text, *, role="actual_record", eid="e1"):
    return {"evidence_id": eid, "text": text, "kind": "paragraph", "source_role": role,
            "file_id": "f1", "locator": {"paragraph": 1}, "metadata": {}}


ANCHORS = ["事件编号", "工作面", "时间"]
VALUES = ["E1", "W1", "2026年10月23日 10:00:01"]


class FactExtractionTests(unittest.TestCase):
    def test_real_dispatch_title_header_and_activity_keep_cell_sources(self):
        title = ["陕西彬长孟村矿业有限公司调度台账\n2026年10月14日", "", "", "", "", ""]
        headers = ["区队", "工作面（作业点）\n名    称", "班次", "出勤人数", "产量（t）/进尺（m）/作业内容", "备注"]
        rows = [title, headers,
                ["综采队", "401104工作面", "零点班", "19", "正常生产", ""],
                ["", "", "八点班", "22", "", ""],
                ["综掘二队", "中央带式输送机大巷（延伸）", "零点班", "14", "9.9m/正常掘进", ""]]
        evidence = []
        for number, cells in zip((1, 3, 4, 5, 7), rows):
            e = row(cells, title, eid=f"row-{number}")
            e.update(kind="sheet_row", locator={"sheet": "孟村", "row": number})
            evidence.append(e)
        result = extract_facts(evidence, allow_unbound=True)
        self.assertEqual(len(result["facts"]), 2)
        for fact in result["facts"]:
            self.assertEqual(fact["time"], "2026-10-14")
            self.assertIsNone(fact["event_id"])
            self.assertEqual(fact["fields"], {"action": "continue_work", "status": "done"})
            self.assertTrue({"row-1", "row-3"} <= set(fact["evidence_refs"]))
            self.assertEqual(fact["sources"][0]["file_id"], "f1")
            self.assertIn(fact["sources"][0]["locator"]["row"], (4, 7))
        # 未知列是格式信息，不再计入证据缺口（否则真实台账结构性无法通过）
        # 出勤人数现在是已知别名（进入 attendance_count），不再算未知表头
        self.assertTrue(any("unknown_headers:区队,班次,备注" in g for g in result["advisories"]))
        self.assertFalse(any("unknown_headers" in g for g in result["gaps"]))

    def test_sheet_report_context_does_not_cross_file_or_sheet(self):
        title = row(["调度日报 2026年10月14日", "", "", ""], ["title"], eid="title")
        title["locator"] = {"sheet": "A", "row": 1}
        records = [title]
        for file_id, sheet in (("f1", "B"), ("f2", "A")):
            e = row(["401104工作面", "正常生产"], ["工作面", "作业内容"], eid=file_id + sheet)
            e.update(file_id=file_id, locator={"sheet": sheet, "row": 4})
            records.append(e)
        self.assertEqual(extract_facts(records, allow_unbound=True)["facts"], [])

    def test_unknown_duplicate_columns_do_not_hide_recognized_fields(self):
        result = extract_facts([row(VALUES + ["已停工", "甲", "乙", "", "尾"],
                                   ANCHORS + ["动作", "备注", "备注", "", ""])])
        self.assertEqual(result["facts"][0]["fields"]["action"], "stop")
        self.assertTrue(any("unknown_headers:备注,备注" in g for g in result["advisories"]))

    def test_pdf_inline_explicit_labels_and_narrow_body_record(self):
        result = extract_facts([record("工作面：401104工作面 时间：2026年10月14日 08:10 动作：已停工", eid="pdf-line")], allow_unbound=True)
        fact, = result["facts"]
        self.assertEqual(fact["time"], "2026-10-14T08:10")
        self.assertEqual(fact["fields"]["action"], "stop")
        self.assertEqual(fact["sources"], [{"evidence_id": "pdf-line", "file_id": "f1", "locator": {"paragraph": 1}}])
        result = extract_facts([record("2026年10月14日 401104工作面已撤人。")], allow_unbound=True)
        self.assertEqual(result["facts"][0]["fields"]["action"], "evacuate")
        for text in ("2026年10月14日 401104工作面计划已停工", "401104工作面已停工", "2026年10月14日 正常生产",
                     "2026年10月14日 401104工作面未正常生产", "2026年10月14日 401104工作面未确认是否已停工",
                     "2026年10月14日 401104工作面已停工，401105工作面已撤人"):
            self.assertEqual(extract_facts([record(text)], allow_unbound=True)["facts"], [])

    def test_chinese_event_date_and_evidence(self):
        result = extract_facts([row(VALUES + ["动力现象", "有危险"], ANCHORS + ["记录类型", "危险研判"])])
        fact, = result["facts"]
        self.assertEqual(fact["kind"], "event")
        self.assertEqual(fact["time"], "2026-10-23T10:00:01")
        self.assertEqual(fact["fields"], {"trigger_type": "dynamic", "hazard_confirmed": True})
        self.assertEqual(fact["evidence_refs"], ["e1"])
        self.assertEqual(result["gaps"], [])

    def test_zero_decimal_original_unit_and_stage(self):
        for zero in (0, "0", "0.000"):
            with self.subTest(zero=zero):
                result = extract_facts([row(VALUES + ["效果检验", "钻屑法", "钻屑量", "P1", zero, "kg/m"],
                                           ANCHORS + ["阶段", "方法", "指标", "测点", "实测值", "单位"])])
                fact, = result["facts"]
                self.assertEqual(fact["kind"], "measurement")
                self.assertEqual(fact["fields"]["value"], str(zero))
                self.assertEqual(fact["fields"]["unit"], "kg/m")
                self.assertEqual(fact["fields"]["stage"], "effect")

    def test_statement_and_regulation_never_execution(self):
        for role in ("statement", "regulation", "requirement"):
            result = extract_facts([row(VALUES + ["动力现象", "已停工", "已完成"],
                                       ANCHORS + ["类型", "动作", "状态"], role=role)])
            self.assertFalse(any(f["kind"] in {"action", "event"} for f in result["facts"]))

    def test_actual_record_normative_language_is_only_candidate(self):
        for phrase in ("应当停工", "计划停工", "未检索到停工记录", "未找到停工记录", "建议停工"):
            result = extract_facts([record("事件编号:E1；工作面:W1；时间:2026-10-23；动作:" + phrase)])
            self.assertEqual(result["facts"], [])
            self.assertTrue(result["candidates"])

    def test_explicit_done_and_negative(self):
        for action, status, expected in (("停工", "已完成", "done"), ("已停工", "", "done"),
                                         ("未停工", "", "not_done"), ("停工", "否", "not_done")):
            result = extract_facts([row(VALUES + [action, status, "是", "W1", "是"],
                                       ANCHORS + ["动作", "状态", "立即", "目标范围", "路线符合"])])
            fact, = result["facts"]
            self.assertEqual(fact["fields"]["status"], expected)
            self.assertEqual(fact["fields"]["action"], "stop")

    def test_uncertain_status_and_negative_boolean_not_true(self):
        for status in ("未确认", "尚未确认", "无法确认", "不能确认", "未见批准", "未检索到"):
            result = extract_facts([row(VALUES + ["停工", status, status], ANCHORS + ["动作", "状态", "立即"])])
            self.assertEqual(result["facts"], [])
        for approval in ("未批复", "未批准", "否", "假", "未见批准", "未确认"):
            result = extract_facts([row(VALUES + ["法", "指标A", "P1", "1", approval],
                                       ANCHORS + ["方法", "指标", "测点", "数值", "已批准"], role="requirement")])
            self.assertIsNot(result["facts"][0]["fields"]["approved"], True)

    def test_records_do_not_inherit_identifiers_across_lines_or_rows(self):
        result = extract_facts([record("事件编号:E1；工作面:W1；时间:2026-10-23\n动作:已停工"),
                               row(["", "", "", "已停工"], ANCHORS + ["动作"])])
        self.assertFalse(any(f["kind"] == "action" for f in result["facts"]))

    def test_table_misalignment_and_conflicting_aliases_are_gaps(self):
        for evidence in (row(VALUES + ["停工"], ANCHORS),
                         row(VALUES + ["W2", "已停工"], ANCHORS + ["地点", "动作"]),
                         row(VALUES + ["W2", "已停工"], ANCHORS + ["工作面", "动作"])):
            result = extract_facts([evidence])
            self.assertEqual(result["facts"], [])
            self.assertTrue(result["gaps"])

    def test_source_content_gaps_block_facts_and_retain_candidates(self):
        for gap in ("tracked_changes_unresolved", "formula_without_cached_value:A2", "embedded_object_requires_extraction"):
            result = extract_facts([row(VALUES + ["动力现象"], ANCHORS + ["类型"], gaps=[gap])])
            self.assertEqual(result["facts"], [])
            self.assertIn(gap, result["gaps"][0])
            self.assertEqual(result["candidates"][0]["evidence_refs"], ["e1"])

    def test_headers_are_skipped_and_unknown_header_reported(self):
        headers = ANCHORS + ["动作"]
        result = extract_facts([row(headers, headers)])
        self.assertEqual(result["facts"], [])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["gaps"], [])
        self.assertEqual(result["advisories"], [])
        result = extract_facts([row(VALUES + ["内容"], ANCHORS + ["新列"])])
        self.assertTrue(any("unknown_headers:新列" in gap for gap in result["advisories"]))

    def test_requirement_list_is_explicit_never_inferred(self):
        required = [{"method": "钻屑法", "metric": "钻屑量", "point": "P1"}]
        result = extract_facts([row(VALUES + [json.dumps(required, ensure_ascii=False), "已批复"],
                                   ANCHORS + ["指标全集", "批复"], role="requirement")])
        fact, = result["facts"]
        self.assertEqual(fact["fields"], {"required_points": required, "approved": True})
        self.assertEqual(fact["event_id"], "E1")
        self.assertEqual(fact["workface"], "W1")
        result = extract_facts([row(VALUES + ["钻屑法", "钻屑量", "P1", "1"],
                                   ANCHORS + ["方法", "指标", "测点", "实测值"])])
        self.assertFalse(any(f["kind"] == "requirements" for f in result["facts"]))

    def test_requirement_validity_is_retained_and_invalid_bound_is_a_gap(self):
        required = [{"method": "钻屑法", "metric": "钻屑量", "point": "P1"}]
        result = extract_facts([row(VALUES + [json.dumps(required, ensure_ascii=False), "已批复", "2026年10月1日", "2026-10-31"],
                                   ANCHORS + ["指标全集", "批复", "有效起始", "有效截止"], role="requirement")])
        fact, = result["facts"]
        self.assertEqual(fact["fields"]["valid_from"], "2026-10-01")
        self.assertEqual(fact["fields"]["valid_to"], "2026-10-31")
        result = extract_facts([row(VALUES + [json.dumps(required, ensure_ascii=False), "已批复", "2026年13月1日"],
                                   ANCHORS + ["指标全集", "批复", "有效起始"], role="requirement")])
        self.assertEqual(result["facts"][0]["fields"]["valid_from"], "2026年13月1日")
        self.assertTrue(any("invalid_date:valid_from" in gap for gap in result["gaps"]))

    def test_future_requirement_validity_is_not_replaced_by_event_date(self):
        required = [{"method": "钻屑法", "metric": "钻屑量", "point": "P1"}]
        result = extract_facts([row(VALUES + [json.dumps(required, ensure_ascii=False), "已批复", "2026年11月1日", "2026年11月30日"],
                                   ANCHORS + ["指标全集", "批复", "有效起始", "有效截止"], role="requirement")])
        fact, = result["facts"]
        self.assertEqual(fact["time"], "2026-10-23T10:00:01")
        self.assertEqual(fact["fields"]["valid_from"], "2026-11-01")
        self.assertEqual(fact["fields"]["valid_to"], "2026-11-30")
        self.assertEqual(result["gaps"], [])

    def test_missing_requirement_validity_is_not_invented(self):
        required = [{"method": "钻屑法", "metric": "钻屑量", "point": "P1"}]
        result = extract_facts([row(VALUES + [json.dumps(required, ensure_ascii=False), "已批复"],
                                   ANCHORS + ["指标全集", "批复"], role="requirement")])
        fields = result["facts"][0]["fields"]
        self.assertNotIn("valid_from", fields)
        self.assertNotIn("valid_to", fields)

    def test_requirement_and_threshold_invalid_end_bounds_are_not_silently_open(self):
        required = [{"method": "钻屑法", "metric": "钻屑量", "point": "P1"}]
        for extra_headers, extra_values in ((["指标全集"], [json.dumps(required, ensure_ascii=False)]),
                                            (["指标", "数值"], ["钻屑量", "2.00"])):
            result = extract_facts([row(VALUES + extra_values + ["已批复", "2026年1月1日", "日期不明"],
                                       ANCHORS + extra_headers + ["批复", "有效起始", "有效截止"], role="requirement")])
            self.assertEqual(result["facts"][0]["fields"]["valid_to"], "日期不明")
            self.assertTrue(any("invalid_date:valid_to" in gap for gap in result["gaps"]))
            self.assertEqual(result["candidates"][0]["evidence_refs"], ["e1"])

    def test_threshold_has_approved_validity_but_no_invented_unit(self):
        result = extract_facts([row(VALUES + ["法", "指标A", "P1", "2.00", "2026年1月1日", "已批准"],
                                   ANCHORS + ["方法", "指标", "测点", "数值", "有效起始", "已批准"], role="requirement")])
        fact, = result["facts"]
        self.assertEqual(fact["kind"], "threshold")
        self.assertEqual(fact["fields"]["valid_from"], "2026-01-01")
        self.assertEqual(fact["fields"]["value"], "2.00")
        self.assertNotIn("unit", fact["fields"])

    def test_exact_actor_roles_only(self):
        for actor, expected in (("技术负责人", "technical_head"), ("调度室", "dispatch"),
                                 ("副技术负责人", "副技术负责人"), ("非技术负责人", "非技术负责人")):
            result = extract_facts([row(VALUES + ["已停工", actor], ANCHORS + ["动作", "执行人角色"])])
            self.assertEqual(result["facts"][0]["fields"]["actor_role"], expected)

    def test_iso_timezone_and_date_precision_preserved(self):
        for value in ("2026-10-23", "2026-10-23T10:00+08:00", "2026-10-23T10:00:01Z"):
            result = extract_facts([row(["E1", "W1", value, "动力现象"], ANCHORS + ["类型"])])
            self.assertEqual(result["facts"][0]["time"], value)

    def test_duplicate_fact_merges_refs_but_keeps_source_roles(self):
        a = row(VALUES + ["动力现象"], ANCHORS + ["类型"], eid="e1")
        b = row(VALUES + ["动力现象"], ANCHORS + ["类型"], eid="e2")
        result = extract_facts([a, b, a])
        fact, = result["facts"]
        self.assertEqual(fact["evidence_refs"], ["e1", "e2"])

    def test_valid_explicit_record_is_not_unresolved_candidate(self):
        result = extract_facts([record("事件编号:E1；工作面:W1；时间:2026-10-23；动作:已停工")])
        self.assertEqual(result["facts"][0]["fields"]["action"], "stop")
        self.assertEqual(result["candidates"], [])

    def test_natural_prose_retains_quote_and_never_guesses(self):
        text = "十月工作面监测有预警，后已停工撤人，报告称手续齐全。"
        result = extract_facts([record(text)])
        self.assertEqual(result["facts"], [])
        self.assertEqual(result["candidates"][0]["quote"], text)
        self.assertEqual(result["candidates"][0]["evidence_refs"], ["e1"])

    def test_duplicate_explicit_key_and_action_status_conflict_are_not_facts(self):
        result = extract_facts([record("事件编号:E1；工作面:W1；工作面:W2；时间:2026-10-23；动作:已停工")])
        self.assertEqual(result["facts"], [])
        self.assertTrue(any("conflicting_columns" in gap for gap in result["gaps"]))
        for action, status in (("未停工", "已完成"), ("已停工", "否")):
            result = extract_facts([row(VALUES + [action, status], ANCHORS + ["动作", "状态"])])
            self.assertEqual(result["facts"], [])
            self.assertTrue(any("conflicting_action_status" in gap for gap in result["gaps"]))

    def test_action_substrings_are_not_implicitly_done(self):
        for action in ("不停工", "似已停工", "停工情况不详", "未明确是否停工"):
            result = extract_facts([row(VALUES + [action, "已完成"], ANCHORS + ["动作", "状态"])])
            self.assertEqual(result["facts"], [])

    def test_hazard_analysis_is_action_not_a_duplicate_trigger(self):
        result = extract_facts([row(VALUES + ["危险研判", "已完成", "无危险"],
                                   ANCHORS + ["动作", "状态", "危险研判"])])
        fact, = result["facts"]
        self.assertEqual(fact["kind"], "action")
        self.assertEqual(fact["fields"]["action"], "hazard_analysis")
        self.assertIs(fact["fields"]["hazard_confirmed"], False)

    def test_warning_measurement_type_does_not_invent_duplicate_event(self):
        result = extract_facts([row(VALUES + ["预警监测", "法", "指标A", "P1", "1"],
                                   ANCHORS + ["类型", "方法", "指标", "测点", "实测值"])])
        fact, = result["facts"]
        self.assertEqual(fact["kind"], "measurement")
        self.assertEqual(fact["fields"]["stage"], "warning")

    def test_risk_assessment_without_event_number_and_with_explicit_scope(self):
        headers = ["记录类型", "工作面", "有效起始", "有效截止", "批准", "冲击危险", "危险等级", "适用范围"]
        values = ["冲击危险性评价", "W1", "2026年10月1日", "2026-10-31", "是", 0, "无", "W1全区域"]
        result = extract_facts([row(values, headers, role="requirement")])
        fact, = result["facts"]
        self.assertEqual(fact["kind"], "risk_assessment")
        self.assertIsNone(fact["event_id"])
        self.assertIsNone(fact["time"])
        self.assertEqual(fact["fields"], {"approved": True, "impact_risk": False, "risk_grade": "无",
            "target_scope": "W1全区域", "valid_from": "2026-10-01", "valid_to": "2026-10-31"})
        self.assertEqual(result["gaps"], [])
        for role in ("actual_record", "statement", "regulation"):
            self.assertFalse(any(f["kind"] == "risk_assessment" for f in
                extract_facts([row(values, headers, role=role)])["facts"]))

    def test_complete_design_preserves_explicit_parameters_and_zero_limit(self):
        parameters = [{"name": "孔深", "operator": ">=", "value": "20.00", "unit": "m"}]
        result = extract_facts([row(["防冲专项措施", "W1", "2026-10-01", "2026-10-31", "是", "W1",
             "张三", '["钻孔卸压", "爆破卸压"]', json.dumps(parameters, ensure_ascii=False), "A", "B", 0, False],
             ["记录类型", "工作面", "有效起始", "有效截止", "批准", "适用范围", "技术负责人", "解危方法全集",
              "解危参数要求", "解危区域", "撤离区域", "解危限员", "无关设备停运要求"], role="requirement")])
        fact, = result["facts"]
        self.assertEqual(fact["kind"], "design")
        self.assertEqual(fact["fields"], {"approved": True, "target_scope": "W1", "valid_from": "2026-10-01",
             "valid_to": "2026-10-31", "technical_head": "张三", "relief_methods": ["钻孔卸压", "爆破卸压"],
             "relief_parameters": parameters, "relief_area": "A", "withdrawal_area": "B", "max_personnel": "0",
             "unrelated_equipment_stop_required": False})
        self.assertEqual(result["gaps"], [])

    def test_bad_parameter_json_never_invents_units_values_or_operators(self):
        for item in ({"name": "孔深", "operator": ">=", "value": 20},
                     {"name": "孔深", "operator": "约", "value": 20, "unit": "m"},
                     {"name": "孔深", "operator": ">=", "value": "NaN", "unit": "m"},
                     {"name": "孔深", "operator": ">=", "value": float("inf"), "unit": "m"},
                     {"name": "孔深", "operator": ">=", "value": True, "unit": "m"}):
            result = extract_facts([row(["防冲设计", "W1", json.dumps([item])],
                ["记录类型", "工作面", "解危参数要求"], role="requirement")])
            self.assertNotIn("relief_parameters", result["facts"][0]["fields"])
            self.assertTrue(any("invalid_json:relief_parameters" in gap for gap in result["gaps"]))
        for value in ('["钻孔卸压", null]', "钻孔卸压", "[]", '[{"method":"钻孔卸压"}]'):
            result = extract_facts([row(["防冲设计", value], ["记录类型", "解危方法全集"], role="requirement")])
            self.assertNotIn("relief_methods", result["facts"][0]["fields"])
            self.assertTrue(any("invalid_json:relief_methods" in gap for gap in result["gaps"]))

    def test_missing_design_fields_and_unknown_booleans_are_not_guessed(self):
        result = extract_facts([row(["防冲设计", "W1", "未知", None],
            ["记录类型", "工作面", "批准", "解危限员"], role="requirement")])
        self.assertEqual(result["facts"][0]["fields"], {})
        self.assertTrue(any("unresolved_boolean:approved" in gap for gap in result["gaps"]))
        for approval in ("未批准", "未见批准", "无法确认", "尚未确认"):
            result = extract_facts([row(["危险性评价", approval], ["记录类型", "批准"], role="requirement")])
            self.assertIsNot(result["facts"][0]["fields"].get("approved"), True)

    def test_parameter_json_preserves_precision_and_rejects_duplicate_keys(self):
        raw = '[{"name":"孔深","value":0.123456789012345678901,"unit":"m"}]'
        result = extract_facts([row(VALUES + ["已解危", raw], ANCHORS + ["动作", "施工参数"])])
        self.assertEqual(result["facts"][0]["fields"]["parameters"][0]["value"], "0.123456789012345678901")
        raw = '[{"name":"孔深","value":1,"value":20,"unit":"m"}]'
        result = extract_facts([row(VALUES + ["已解危", raw], ANCHORS + ["动作", "施工参数"])])
        self.assertNotIn("parameters", result["facts"][0]["fields"])
        self.assertTrue(any("invalid_json:parameters" in gap for gap in result["gaps"]))

    def test_relief_parameters_acceptance_and_personnel_fields(self):
        parameters = [{"name": "孔深", "value": "0", "unit": "m"}]
        result = extract_facts([row(VALUES + ["已解危", "钻孔卸压", json.dumps(parameters), "A", "否", "李四",
            "2026-10-23 10:01:00", 0, "否"], ANCHORS + ["动作", "解危方法", "施工参数", "解危区域",
            "验收通过", "验收人", "验收时间", "解危人数", "无关设备已停"])])
        self.assertEqual(result["facts"][0]["fields"], {"action": "relief", "status": "done", "method": "钻孔卸压",
            "parameters": parameters, "relief_area": "A", "acceptance_passed": False, "acceptance_signer": "李四",
            "acceptance_time": "2026-10-23T10:01:00", "personnel_count": "0", "unrelated_equipment_stopped": False})
        self.assertEqual(result["gaps"], [])
        bad = extract_facts([row(VALUES + ["已解危", '[{"name":"孔深","value":1}]', "-1"],
            ANCHORS + ["动作", "施工参数", "解危人数"])])
        self.assertNotIn("parameters", bad["facts"][0]["fields"])
        self.assertNotIn("personnel_count", bad["facts"][0]["fields"])
        self.assertTrue(any("invalid_count:personnel_count" in gap for gap in bad["gaps"]))

    def test_evacuation_counts_and_area_are_facts_not_default_compliance(self):
        result = extract_facts([row(VALUES + ["已撤人", "B", 12, 0, "否"],
            ANCHORS + ["动作", "撤离区域", "应撤人数", "实撤人数", "路线符合"])])
        self.assertEqual(result["facts"][0]["fields"], {"action": "evacuate", "status": "done",
            "withdrawal_area": "B", "expected_personnel": "12", "withdrawn_personnel": "0", "route_conforms": False})

    def test_signatures_need_explicit_presence_independent_of_names(self):
        for action in ("危险研判", "技术确认"):
            result = extract_facts([row(VALUES + [action, "是", "张三"], ANCHORS + ["动作", "状态", "签字人"])])
            self.assertEqual(result["facts"][0]["fields"]["signer"], "张三")
            self.assertNotIn("signature_present", result["facts"][0]["fields"])
            for signature in ("未签字", "未见签字", "不确定", "无法确认", "否", 0):
                result = extract_facts([row(VALUES + [action, "是", "张三", signature],
                    ANCHORS + ["动作", "状态", "签字人", "签名存在"])])
                self.assertIsNot(result["facts"][0]["fields"].get("signature_present"), True)
        result = extract_facts([row(VALUES + ["已危险研判", "张三", "是", "否", "2026-10-23 10:01:00"],
            ANCHORS + ["动作", "签字人", "签名存在", "已告知", "告知时间"])])
        self.assertTrue(result["facts"][0]["fields"]["signature_present"])
        self.assertFalse(result["facts"][0]["fields"]["notified"])
        self.assertEqual(result["facts"][0]["fields"]["notification_time"], "2026-10-23T10:01:00")
        result = extract_facts([row(VALUES + ["已技术确认", "张三", "已签名", "2026-10-23 10:01:00"],
            ANCHORS + ["动作", "签字人", "签名存在", "签署时间"])])
        self.assertEqual(result["facts"][0]["fields"]["signed_at"], "2026-10-23T10:01:00")

    def test_reference_time_is_preserved_on_every_fact_kind(self):
        common = ["关联事件时间"]
        reference = ["2026年10月23日 10:00:01"]
        samples = [
            ("actual_record", ["动力现象"], ["记录类型"], "event"),
            ("actual_record", ["已停工"], ["动作"], "action"),
            ("actual_record", ["法", "指标", "点", "0"], ["方法", "指标", "测点", "数值"], "measurement"),
            ("requirement", ["法", "指标", "点", "1"], ["方法", "指标", "测点", "数值"], "threshold"),
            ("requirement", ['[{"method":"法","metric":"指标","point":"点"}]'], ["指标全集"], "requirements"),
            ("requirement", ["危险性评价"], ["记录类型"], "risk_assessment"),
            ("requirement", ["防冲设计"], ["记录类型"], "design"),
        ]
        for role, values, headers, kind in samples:
            result = extract_facts([row(VALUES + values + reference, ANCHORS + headers + common, role=role)])
            self.assertEqual(result["facts"][0]["kind"], kind)
            self.assertEqual(result["facts"][0]["fields"]["event_time_reference"], "2026-10-23T10:00:01")


if __name__ == "__main__":
    unittest.main()
