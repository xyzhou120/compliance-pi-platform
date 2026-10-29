import unittest

from apps.control_api.domain.threshold_profiles import compare_threshold_profiles, extract_threshold_profiles


def file_row(fid, name, digest):
    return {"file_id": fid, "original_name": name, "content_hash": digest, "source_role": "requirement"}


def evidence(eid, fid, text, page=3, quality=None):
    row = {"evidence_id": eid, "file_id": fid, "source_role": "requirement", "text": text,
           "quote": text, "locator": {"page": page}}
    if quality:
        row["quality"] = quality
    return row


class ThresholdProfileTests(unittest.TestCase):
    def test_keeps_original_grade_and_extracts_two_independent_ground_sound_branches(self):
        files = [file_row("v1", "指标修订版.pdf", "h1")]
        text = ("二、地音监测预警指标。a.无 dev<25%。c.中 单个探头连续两个班均100%≤dev<200%；"
                "或同一顺槽相邻两个探头均100%≤dev<200%。d.强 单个探头连续两个班均dev≥200%。")
        profile = extract_threshold_profiles(files, [evidence("e1", "v1", text)])
        self.assertEqual(profile["status"], "parsed")
        self.assertEqual({row["grade_label"] for row in profile["rows"]}, {"a", "c", "d"})
        rows = {row["grade_label"]: row for row in profile["rows"]}
        self.assertIn("same_probe_consecutive_shifts", rows["c"]["condition_flags"])
        self.assertIn("adjacent_probes_same_location", rows["c"]["condition_flags"])
        self.assertEqual(rows["d"]["numeric_bounds"][0]["value"], 200.0)
        self.assertFalse(profile["authority_selected"])

    def test_color_labels_are_retained_without_mapping_to_cd(self):
        files = [file_row("v1", "蓝黄橙红指标.pdf", "h1")]
        text = "地音预警等级：蓝级 dev<25%；黄级 25%≤dev<100%；橙级单个探头连续两个班均100%≤dev<200%；红级 dev≥200%。"
        profile = extract_threshold_profiles(files, [evidence("e1", "v1", text)])
        labels = {row["grade_label"] for row in profile["rows"]}
        self.assertIn("蓝", labels)
        self.assertIn("黄", labels)
        self.assertIn("橙", labels)
        self.assertIn("红", labels)
        self.assertNotIn("C", labels)
        self.assertNotIn("D", labels)

    def test_candidate_model_text_does_not_define_threshold(self):
        files = [file_row("v1", "指标.pdf", "h1")]
        text = "c.中 单个探头连续两个班均100%≤dev<200%。"
        profile = extract_threshold_profiles(files, [evidence("e1", "v1", text, quality="model_candidate")])
        self.assertEqual(profile["status"], "unresolved")
        self.assertTrue(any(gap["reason"] == "threshold_document_no_native_text" for gap in profile["gaps"]))

    def test_repeated_reports_are_duplicate_or_equivalent_without_latest_selection(self):
        files = [file_row("v1", "指标1.pdf", "same"), file_row("v2", "指标2.pdf", "same"), file_row("v3", "指标3.pdf", "new")]
        text = "二、地音监测预警指标。橙级单个探头连续两个班均100%≤dev<200%。"
        profile = extract_threshold_profiles(files, [evidence("e1", "v1", text), evidence("e2", "v2", text), evidence("e3", "v3", text)])
        comparison = compare_threshold_profiles(profile)
        self.assertEqual(len(comparison["comparisons"]), 1)
        self.assertEqual(comparison["comparisons"][0]["classification"], "equivalent_versions")
        self.assertFalse(comparison["authority_selected"])

    def test_conflicting_report_conditions_remain_unresolved(self):
        files = [file_row("v1", "指标旧版.pdf", "h1"), file_row("v2", "指标新版.pdf", "h2")]
        evidence_rows = [evidence("e1", "v1", "二、地音监测预警指标。橙级 dev≥100%。"),
                         evidence("e2", "v2", "二、地音监测预警指标。橙级 dev≥200%。")]
        comparison = compare_threshold_profiles(extract_threshold_profiles(files, evidence_rows))
        self.assertTrue(comparison["has_unresolved_conflict"])
        self.assertFalse(comparison["authority_selected"])

    def test_missing_native_text_is_explicit_gap(self):
        profile = extract_threshold_profiles([file_row("v1", "扫描指标.pdf", "h1")], [])
        self.assertEqual(profile["status"], "unresolved")
        self.assertEqual(profile["gaps"][0]["reason"], "threshold_document_no_native_text")

    def test_branch_keeps_mixed_two_shift_condition_as_source_text(self):
        files = [file_row("v1", "指标.pdf", "h1")]
        text = "地音监测预警指标。d.强 单个探头连续两个班其中一个班100%≤dev<200%，另一个班dev≥200%；或同一顺槽相邻两个探头其中一个探头100%≤dev<200%，另一个探头dev≥200%。"
        profile = extract_threshold_profiles(files, [evidence("e1", "v1", text)])
        row = next(row for row in profile["rows"] if row["grade_label"] == "d")
        self.assertTrue(any(branch["branch_type"] == "same_probe_consecutive_shifts" for branch in row["branches"]))
        self.assertTrue(any(branch["branch_type"] == "adjacent_probes_same_location" for branch in row["branches"]))
        self.assertTrue(any("另一个" in branch["condition_text"] for branch in row["branches"]))

    def test_explicit_sections_do_not_assign_microseismic_levels_to_ground_sound(self):
        files = [file_row("v1", "混合指标.pdf", "h1")]
        text = "一、微震预警指标。a.无 每日总能量<3.8×10^4J。二、地音监测预警指标。a.无 dev<25%。"
        profile = extract_threshold_profiles(files, [evidence("e1", "v1", text)])
        self.assertIn("microseismic", {row["technology"] for row in profile["rows"]})
        self.assertIn("ground_sound", {row["technology"] for row in profile["rows"]})

    def test_next_page_prefix_belongs_to_previous_technology(self):
        files = [file_row("v1", "跨页指标.pdf", "h1")]
        data = [evidence("e1", "v1", "一、微震预警指标\n说明。", page=1),
                evidence("e2", "v1", "a.无 每日总能量<3.8×10^4J。\n二、地音监测预警指标\na.无 dev<25%。", page=2)]
        profile = extract_threshold_profiles(files, data)
        self.assertEqual({row["technology"] for row in profile["rows"]}, {"microseismic", "ground_sound"})
        self.assertEqual({tuple(row["evidence_refs"]) for row in profile["rows"]}, {("e2",)})


if __name__ == "__main__":
    unittest.main()
