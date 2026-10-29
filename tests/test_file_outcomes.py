import unittest
from apps.control_api.file_outcomes import file_outcomes


class FileOutcomeTests(unittest.TestCase):
    def test_failed_source_does_not_change_other_source_outcome_or_finding(self):
        snapshot = {"files": [{"file_id": "bad", "original_name": "坏文件.pdf"}, {"file_id": "good", "original_name": "记录.txt"}],
                    "evidence": [{"evidence_id": "ev", "file_id": "good", "source_role": "actual_record"}]}
        run = {"work_units": [{"kind": "parse_file", "state": "failed", "input": {"file_id": "bad"}, "error": "pdf_renderer_child_crashed"},
                              {"kind": "parse_file", "state": "succeeded", "input": {"file_id": "good"}}]}
        result = {"findings": [{"code": "stop", "evidence_refs": ["ev"], "detail": "明确未停工"}]}
        rows = file_outcomes(run, snapshot, result)
        self.assertEqual(rows["counts"], {"processing_failed": 1, "business_issue": 1})
        self.assertEqual(rows["items"][0]["failures"][0]["reason"], "pdf_renderer_child_crashed")
        self.assertEqual(rows["items"][1]["findings"][0]["code"], "stop")

    def test_successful_ocr_is_still_candidate_not_compliance(self):
        result = file_outcomes({}, {"files": [{"file_id": "f"}], "evidence": [{"evidence_id": "e", "file_id": "f", "quality": "model_candidate"}]}, {})
        self.assertEqual(result["items"][0]["status"], "candidate_review")
        self.assertTrue(result["processed_does_not_mean_compliant"])

    def test_clean_processed_file_is_collapsed_without_claiming_compliance(self):
        result = file_outcomes({"work_units": [{"kind": "parse_file", "state": "succeeded", "input": {"file_id": "f"}}]},
                               {"files": [{"file_id": "f"}], "coverage": [{"file_id": "f", "unit_id": "document", "state": "readable"}]}, {})
        self.assertFalse(result["items"][0]["notable"])
        self.assertIn("不等于", result["items"][0]["description"])
