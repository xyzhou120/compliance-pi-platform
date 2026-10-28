from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from apps.control_api.acceptance import compare_annotations, split_corpus


class AnnotationScoringTests(unittest.TestCase):
    def gold(self):
        return {"adjudication": "adjudicated", "facts": [
            {"case_id": "c1", "field": "amount", "value": 12, "evidence_refs": ["e1"], "locator": {"page": 1}},
            {"case_id": "c1", "field": "unit", "value": "m", "evidence_refs": ["e2"], "locator": {"page": 2}}],
            "cases": [{"case_id": "c1", "status": "no_pass", "issues": ["too_short", "missing_action"]}]}

    def observed(self):
        return {"facts": [
            {"case_id": "c1", "field": "amount", "value": 12, "verified": True, "evidence_refs": ["e1"], "locator": {"page": 1}},
            {"case_id": "c1", "field": "unit", "value": "m", "verified": True, "evidence_refs": ["e2"], "locator": {"page": 2}}],
            "cases": [{"case_id": "c1", "status": "no_pass", "issues": ["too_short", "missing_action"]}]}

    def test_field_case_issue_and_evidence_metrics_are_independent(self):
        gold, observed = self.gold(), self.observed()
        observed["facts"][1].update(value="cm", locator={"page": 3})
        observed["facts"].append({"case_id": "c1", "field": "invented", "value": 99, "verified": True})
        observed["cases"][0].update(status="pass", issues=["too_short", "unrelated"])
        report = compare_annotations(gold, observed)
        self.assertEqual(report["fields"]["precision"], 1 / 3)
        self.assertEqual(report["fields"]["recall"], 0.5)
        self.assertEqual(report["fields"]["exact_match"], 0)
        self.assertEqual(report["fields"]["wrong_values"], 1)
        self.assertEqual(report["evidence"]["location_accuracy"], 0.5)
        self.assertEqual(report["cases"]["missed_problem_cases"], 1)
        self.assertEqual(report["issues"]["precision"], 0.5)
        self.assertEqual(report["issues"]["recall"], 0.5)
        self.assertEqual(report["issues"]["missed"], 1)
        self.assertIsNone(report["definitions"]["thresholds"])

    def test_reordering_annotations_does_not_change_report_or_mutate_input(self):
        gold, observed = self.gold(), self.observed()
        before = deepcopy((gold, observed))
        expected = compare_annotations(gold, observed)
        self.assertEqual((gold, observed), before)
        gold["facts"].reverse()
        observed["facts"].reverse()
        observed["cases"][0]["issues"].reverse()
        self.assertEqual(compare_annotations(gold, observed), expected)
        self.assertEqual(expected["fields"]["exact_match"], 1)

    def test_duplicates_are_rejected_even_if_unresolved_or_candidate(self):
        for side, collection in (("gold", "facts"), ("observed", "facts"), ("gold", "cases"), ("observed", "cases")):
            gold, observed = self.gold(), self.observed()
            document = gold if side == "gold" else observed
            document[collection].append(deepcopy(document[collection][0]))
            with self.subTest(side=side, collection=collection), self.assertRaisesRegex(ValueError, "duplicate"):
                compare_annotations(gold, observed)

    def test_unadjudicated_disagreements_are_excluded_and_unknown_denominators_are_null(self):
        gold = {"facts": [{"case_id": "c1", "field": "n", "value": 1}],
                "cases": [{"case_id": "c1", "status": "pass", "adjudication": "disputed"}]}
        report = compare_annotations(gold, {"facts": [{"case_id": "c1", "field": "n", "value": 1, "verified": True}]})
        self.assertIsNone(report["fields"]["precision"])
        self.assertIsNone(report["fields"]["recall"])
        self.assertIsNone(report["fields"]["exact_match"])
        self.assertIsNone(report["cases"]["status_accuracy"])
        self.assertIsNone(report["cases"]["false_no_pass"])
        self.assertIsNone(report["evidence"]["location_accuracy"])
        self.assertIsNone(report["issues"]["recall"])
        self.assertEqual(report["exclusions"]["gold_facts_unresolved"], 1)
        self.assertEqual(report["exclusions"]["gold_cases_unresolved"], 1)

    def test_row_disagreement_overrides_global_label_until_explicitly_adjudicated(self):
        gold, observed = self.gold(), self.observed()
        gold["facts"][1]["annotator_disagreement"] = True
        report = compare_annotations(gold, observed)
        self.assertEqual(report["fields"]["gold_count"], 1)
        self.assertEqual(report["fields"]["recall"], 1)
        self.assertIsNone(report["fields"]["exact_match"])
        self.assertEqual(report["exclusions"]["field_cases_excluded_from_exact_match_due_to_unresolved_fields"], 1)
        gold["facts"][1]["adjudication"] = "adjudicated"
        self.assertEqual(compare_annotations(gold, observed)["fields"]["exact_match"], 1)

    def test_unresolved_case_status_does_not_discard_separately_adjudicated_fields(self):
        gold, observed = self.gold(), self.observed()
        gold["cases"][0]["adjudication"] = "unresolved"
        report = compare_annotations(gold, observed)
        self.assertEqual(report["fields"]["gold_count"], 2)
        self.assertEqual(report["fields"]["recall"], 1)
        self.assertIsNone(report["cases"]["status_accuracy"])

    def test_candidates_cannot_count_as_verified_even_if_they_claim_verification(self):
        gold, observed = self.gold(), self.observed()
        observed["facts"][0]["status"] = "candidate"
        observed["facts"][1].update(verified=False, validation_status="source_bound")
        report = compare_annotations(gold, observed)
        self.assertEqual(report["exclusions"]["observed_facts_unverified"], 2)
        self.assertIsNone(report["fields"]["precision"])
        self.assertEqual(report["fields"]["recall"], 0)
        self.assertEqual(report["fields"]["missing"], 2)
        self.assertEqual(report["evidence"]["location_accuracy"], 0)

    def test_false_no_pass_and_missed_problem_cases_are_measured_without_reclassifying(self):
        gold = {"adjudication": "consensus", "cases": [{"case_id": "ok", "status": "pass"}, {"case_id": "bad", "status": "no_pass"}]}
        observed = {"cases": [{"case_id": "ok", "status": "no_pass"}, {"case_id": "bad", "status": "pending"}]}
        report = compare_annotations(gold, observed)
        self.assertEqual(report["cases"]["false_no_pass"], 1)
        self.assertEqual(report["cases"]["false_no_pass_rate"], 1)
        self.assertEqual(report["cases"]["missed_problem_cases"], 1)
        self.assertEqual(report["cases"]["problem_recall"], 0)
        self.assertNotIn("status", report)

    def test_unknown_location_is_not_a_perfect_score_and_wrong_version_is_rejected(self):
        gold = {"adjudication": "resolved", "facts": [{"case_id": "c", "field": "f", "value": True}]}
        observed = {"facts": [{"case_id": "c", "field": "f", "value": 1, "verified": True}]}
        report = compare_annotations(gold, observed)
        self.assertEqual(report["fields"]["recall"], 0)
        self.assertIsNone(report["evidence"]["location_accuracy"])
        gold["facts"][0].update(evidence_refs=["e"], document_version="v1")
        observed["facts"][0].update(value=True, evidence_refs=["e"], document_version="v2")
        self.assertEqual(compare_annotations(gold, observed)["evidence"]["location_accuracy"], 0)
        with self.assertRaisesRegex(ValueError, "corpus_hash mismatch"):
            compare_annotations({**gold, "corpus_hash": "a"}, {**observed, "corpus_hash": "b"})

    def test_nested_reference_location_and_version_cannot_be_hidden_by_matching_id(self):
        gold = {"adjudication": "adjudicated", "facts": [{"case_id": "c", "field": "f", "value": 1,
                "evidence_refs": [{"evidence_id": "e", "content_hash": "h1", "locator": {"page": 1}}]}]}
        observed = {"facts": [{"case_id": "c", "field": "f", "value": 1, "verified": True,
                    "evidence_refs": [{"evidence_id": "e", "content_hash": "h1", "locator": {"page": 2}}]}]}
        self.assertEqual(compare_annotations(gold, observed)["evidence"]["location_accuracy"], 0)
        observed["facts"][0]["evidence_refs"][0]["locator"]["page"] = 1
        self.assertEqual(compare_annotations(gold, observed)["evidence"]["location_accuracy"], 1)
        observed["facts"][0]["evidence_refs"] = ["e"]
        self.assertEqual(compare_annotations(gold, observed)["evidence"]["location_accuracy"], 0)


class CorpusSplitTests(unittest.TestCase):
    def manifest(self):
        return [{"case_id": "case-" + str(i), "content_hash": "bytes-" + str(i)} for i in range(10)]

    def test_split_is_stable_disjoint_exhaustive_and_hash_bound(self):
        manifest = self.manifest()
        first = split_corpus(manifest, "sealed-seed")
        second = split_corpus(list(reversed(manifest)), "sealed-seed")
        self.assertEqual(first, second)
        partitions = [{row["case_id"] for row in first["partitions"][name]} for name in ("train", "validation", "heldout")]
        self.assertEqual([len(partition) for partition in partitions], [6, 2, 2])
        self.assertFalse(partitions[0] & partitions[1] or partitions[0] & partitions[2] or partitions[1] & partitions[2])
        self.assertEqual(set.union(*partitions), {row["case_id"] for row in manifest})
        different_seed = split_corpus(manifest, "other-seed")
        self.assertEqual(first["manifest_hash"], different_seed["manifest_hash"])
        self.assertNotEqual(first["split_hash"], different_seed["split_hash"])
        manifest[0]["content_hash"] = "changed-bytes"
        self.assertNotEqual(first["manifest_hash"], split_corpus(manifest, "sealed-seed")["manifest_hash"])

    def test_duplicate_case_and_shared_bytes_cannot_leak_into_blind_partitions(self):
        for manifest in ([{"case_id": "c", "content_hash": "h1"}, {"case_id": "c", "content_hash": "h2"}],
                         [{"case_id": "a", "content_hash": "h"}, {"case_id": "b", "content_hash": "h"}],
                         [{"case_id": "a", "content_hashes": ["h1", "h2"]}, {"case_id": "b", "content_hashes": ["h2", "h3"]}]):
            with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                split_corpus(manifest, "seed")

    def test_cli_help_and_local_json_scoring(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "score-corpus.py"
        help_result = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True, timeout=15)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("--gold", help_result.stdout)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            gold = {"adjudication": "adjudicated", "facts": [{"case_id": "c", "field": "f", "value": 1}]}
            observed = {"facts": [{"case_id": "c", "field": "f", "value": 1, "verified": True}]}
            (root / "gold.json").write_text(json.dumps(gold), encoding="utf-8")
            (root / "observed.json").write_text(json.dumps(observed), encoding="utf-8")
            (root / "manifest.json").write_text(json.dumps([{"case_id": "c", "content_hash": "fixed"}]), encoding="utf-8")
            command = [sys.executable, str(script), "--gold", str(root / "gold.json"), "--observed", str(root / "observed.json"),
                       "--manifest", str(root / "manifest.json"), "--seed", "sealed", "--output", str(root / "report.json")]
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((root / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["fields"]["exact_match"], 1)
            self.assertEqual(report["corpus_hash"], report["corpus_split"]["manifest_hash"])


if __name__ == "__main__":
    unittest.main()
