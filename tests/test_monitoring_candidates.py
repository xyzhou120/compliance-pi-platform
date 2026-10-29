import unittest

from apps.control_api.domain.monitoring_candidates import extract_monitoring_candidates, adjacent_probe_groups


class MonitoringCandidateTests(unittest.TestCase):
    def test_image_only_evidence_keeps_unresolved_gap_without_fact(self):
        result = extract_monitoring_candidates([], coverage=[{"file_id": "f1", "unit_id": "page:1", "state": "unreadable"}])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["gaps"][0]["reason"], "monitoring_source_unavailable:unreadable")
        self.assertIsNone(result["trigger"])
        self.assertFalse(result["verified"])

    def test_table_candidate_preserves_cell_values_and_provenance(self):
        result = extract_monitoring_candidates([{"evidence_id": "e2", "file_id": "f2", "text": "",
            "source_role": "actual_record", "locator": {"sheet": "日报", "row": 4},
            "metadata": {"headers": ["工作面", "地点", "探头编号", "监测技术", "dev值", "单位", "时间"],
                         "cells": ["403105", "回风巷", "D18", "地音", "260", "%", "2026-08-08 08:00"]}}])
        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["probe_id"], "D18")
        self.assertEqual(candidate["dev_value"], "260")
        self.assertEqual(candidate["time_precision"], "exact_time")
        self.assertFalse(candidate["shift_derived"])
        self.assertIsNone(candidate["trigger"])

    def test_structured_chart_points_are_candidates_only(self):
        result = extract_monitoring_candidates([{"evidence_id": "e3", "file_id": "f3", "text": "",
            "locator": {"page": 2}, "metadata": {"chart_points": [
                {"probe_id": "D18", "workface": "402101", "location": "运输巷", "technology": "地音",
                 "dev_value": "220", "unit": "%", "sample_time": "2026-08-08 00:00", "bbox": [1, 2, 3, 4]}
            ]}}])
        candidate = result["candidates"][0]
        self.assertEqual(candidate["source"]["method"], "structured_chart")
        self.assertEqual(candidate["bbox"], [1, 2, 3, 4])
        self.assertEqual(candidate["time_precision"], "exact_time")
        self.assertFalse(candidate["validated"])

    def test_same_location_distinct_probe_convention_group(self):
        evidence = [{"evidence_id": "e1", "file_id": "f", "metadata": {"chart_points": [
            {"probe_id": "D18", "workface": "402101", "location": "运输巷", "technology": "地音", "dev_value": "220", "unit": "%", "sample_time": "2026-08-08 08:00"},
            {"probe_id": "D29", "workface": "402101", "location": "运输巷", "technology": "地音", "dev_value": "240", "unit": "%", "sample_time": "2026-08-08 08:00"}
        ]}}]
        candidates = extract_monitoring_candidates(evidence)["candidates"]
        grouped = adjacent_probe_groups(candidates)
        self.assertEqual(len(grouped["groups"]), 1)
        self.assertEqual(grouped["groups"][0]["probe_ids"], ["D18", "D29"])
        self.assertFalse(grouped["groups"][0]["adjacent_proven"])
        self.assertIsNone(grouped["groups"][0]["trigger"])

    def test_same_probe_is_not_an_adjacent_probe_group(self):
        evidence = [{"evidence_id": "e1", "file_id": "f", "metadata": {"chart_points": [
            {"probe_id": "D18", "workface": "402101", "location": "运输巷", "technology": "地音", "dev_value": "220", "unit": "%", "sample_time": "2026-08-08 08:00"},
            {"probe_id": "D18", "workface": "402101", "location": "运输巷", "technology": "地音", "dev_value": "240", "unit": "%", "sample_time": "2026-08-08 16:00"}
        ]}}]
        self.assertEqual(adjacent_probe_groups(extract_monitoring_candidates(evidence)["candidates"])["groups"], [])

    def test_text_candidate_never_infers_shift_or_trigger(self):
        result = extract_monitoring_candidates([{"evidence_id": "e4", "file_id": "f", "text": "地音 D18 dev=260%，工作面403105，运输巷", "locator": {"page": 3}}])
        self.assertEqual(result["candidates"][0]["time_precision"], "unknown")
        self.assertFalse(result["candidates"][0]["shift_derived"])
        self.assertEqual(result["candidates"][0]["probe_id"], "D18")
        self.assertIsNone(result["trigger"])

    def test_candidate_quote_and_count_are_bounded(self):
        entry = {"evidence_id": "e", "file_id": "f", "text": "地音 D18 dev=260%，工作面403105，运输巷 " + "x" * 2000,
                 "locator": {"page": 1}}
        result = extract_monitoring_candidates([entry] * 2100)
        self.assertLessEqual(len(result["candidates"]), 2000)
        self.assertGreater(result["truncated_count"], 0)
        self.assertLessEqual(len(result["candidates"][0]["raw_text"]), 500)

    def test_date_only_or_inferred_time_never_forms_probe_pair(self):
        points = [
            {"probe_id": "D18", "workface": "W", "location": "L", "technology": "地音", "dev_value": "100", "unit": "%", "sample_time": "2026-08-08"},
            {"probe_id": "D29", "workface": "W", "location": "L", "technology": "地音", "dev_value": "200", "unit": "%", "sample_time": "2026-08-08"},
        ]
        entry = {"evidence_id": "e", "metadata": {"chart_points": points}}
        self.assertEqual(adjacent_probe_groups(extract_monitoring_candidates([entry])["candidates"])["groups"], [])
        for point in points: point.update(sample_time="2026-08-08 08:00", shift_derived=True)
        self.assertEqual(adjacent_probe_groups(extract_monitoring_candidates([entry])["candidates"])["groups"], [])

    def test_zero_value_and_unique_chart_point_ids(self):
        entry = {"evidence_id": "e", "metadata": {"chart_points": [
            {"probe_id": "D18", "workface": "W", "location": "L", "technology": "地音", "dev_value": 0, "unit": "%", "sample_time": "2026-08-08 08:00"},
            {"probe_id": "D18", "workface": "W", "location": "L", "technology": "地音", "dev_value": 100, "unit": "%", "sample_time": "2026-08-08 16:00"}]}}
        rows = extract_monitoring_candidates([entry])["candidates"]
        self.assertEqual(rows[0]["dev_value"], "0")
        self.assertNotEqual(rows[0]["candidate_id"], rows[1]["candidate_id"])

    def test_time_only_does_not_pair_different_days(self):
        entry = {"evidence_id": "e", "metadata": {"chart_points": [
            {"probe_id": probe, "workface": "W", "location": "L", "technology": "地音",
             "dev_value": "120", "unit": "%", "sample_time": "08:00"} for probe in ("D18", "D29")]}}
        rows = extract_monitoring_candidates([entry])["candidates"]
        self.assertEqual(rows[0]["time_precision"], "unknown")
        self.assertEqual(adjacent_probe_groups(rows)["groups"], [])


if __name__ == "__main__":
    unittest.main()
