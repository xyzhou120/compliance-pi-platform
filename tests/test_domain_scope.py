"""Independent full-rule conditions reject invented or borrowed evidence."""
from copy import deepcopy
import unittest

from apps.control_api.domain.contracts import InvalidRequest
from apps.control_api.domain.scope_checks import evaluate_scope
try:
    from test_domain_rules import action, effect_chain, fact, run_snapshot, stop_chain
except ModuleNotFoundError:
    from tests.test_domain_rules import action, effect_chain, fact, run_snapshot, stop_chain


def full_scope_chain(item="stop_work_unload"):
    chain = stop_chain() if item == "stop_work_unload" else effect_chain()
    validity = {"approved": True, "valid_from": "2026-09-23", "valid_to": "2026-12-31", "target_scope": "W1"}
    chain.extend([
        fact("risk_assessment", "risk", {**validity, "impact_risk": True, "risk_grade": "high"}, role="requirement"),
        fact("design", "design", {**validity, "technical_head": "张工", "relief_methods": ["钻孔卸压"],
             "relief_parameters": [{"name": "孔深", "operator": ">=", "value": "20", "unit": "m"}],
             "relief_area": "W1危险区", "withdrawal_area": "W1危险区外", "max_personnel": "5",
             "unrelated_equipment_stop_required": True}, role="requirement"),
    ])
    relief = next(f for f in chain if f["fact_id"] == "relief")
    relief["fields"].update(method="钻孔卸压", parameters=[{"name": "孔深", "value": "2000", "unit": "cm"}],
                           relief_area="W1危险区", acceptance_passed=True, acceptance_signer="李工",
                           acceptance_time="2026-10-01T08:10:00", personnel_count="4", unrelated_equipment_stopped=True)
    if item == "stop_work_unload":
        evacuate = next(f for f in chain if f["fact_id"] == "evacuate")
        evacuate["fields"].update(withdrawal_area="W1危险区外", expected_personnel="12", withdrawn_personnel="12")
        chain.append(action("hazard_analysis", 2, hazard_confirmed=True, signer="王工", signature_present=True,
                            notified=True, notification_time="2026-10-01T08:03:00"))
    else:
        technical = next(f for f in chain if f["fact_id"] == "technical_confirm")
        technical["fields"].update(signer="张工", signature_present=True, signed_at="2026-10-01T08:04:30")
    return chain


class ScopeChecksTests(unittest.TestCase):
    def evaluate(self, item, chain):
        run, snapshot = run_snapshot(item, chain)
        before = deepcopy(snapshot)
        result = evaluate_scope(run, snapshot)
        self.assertEqual(before, snapshot)
        registered = {e["evidence_id"] for e in snapshot["evidence"]}
        self.assertTrue(all(set(p["evidence_refs"]) <= registered for p in result["propositions"]))
        return result

    def test_evidence_outside_the_observation_window_cannot_become_a_finding(self):
        chain = full_scope_chain("stop_work_unload")
        relief = next(f for f in chain if f["fact_id"] == "relief")
        relief["fields"]["acceptance_passed"] = False      # 窗口内本会形成 finding
        run, snapshot = run_snapshot("stop_work_unload", chain)
        snapshot["observation"] = {"start": "2026-09-30T00:00:00", "end": "2026-09-30T23:59:59"}
        result = evaluate_scope(run, snapshot)
        self.assertEqual([], result["findings"])
        self.assertTrue(any(p["code"] == "observation_window" and p["state"] == "missing"
                            for p in result["propositions"]))
        self.assertTrue(any("观测窗口" in gap for gap in result["gaps"]), result["gaps"])

    def test_in_window_evidence_still_produces_the_finding(self):
        chain = full_scope_chain("stop_work_unload")
        relief = next(f for f in chain if f["fact_id"] == "relief")
        relief["fields"]["acceptance_passed"] = False
        result = self.evaluate("stop_work_unload", chain)
        self.assertTrue(result["findings"])
        self.assertFalse(any(p["code"] == "observation_window" for p in result["propositions"]))

    def test_missing_observation_window_cannot_be_treated_as_inside(self):
        chain = full_scope_chain("stop_work_unload")
        run, snapshot = run_snapshot("stop_work_unload", chain)
        snapshot.pop("observation")
        result = evaluate_scope(run, snapshot)
        self.assertEqual([], result["findings"])
        self.assertTrue(any("观测窗口" in gap for gap in result["gaps"]), result["gaps"])

    def test_complete_multi_document_actual_evidence_satisfies_full_scope(self):
        for item in ("stop_work_unload", "relief_effects"):
            with self.subTest(item=item):
                chain = full_scope_chain(item)
                run, snapshot = run_snapshot(item, chain)
                # Each fact has its own registered source file: no same-file shortcut.
                snapshot["files"] = [{"file_id": f"file-{i}", "content_hash": f"hash-{i}"} for i in range(len(chain))]
                for i, evidence in enumerate(snapshot["evidence"]):
                    evidence["content_hash"] = f"hash-{i}"
                result = evaluate_scope(run, snapshot)
                self.assertFalse(result["gaps"], result)
                self.assertFalse(result["findings"], result)
                self.assertTrue(all(p["state"] == "true" for p in result["propositions"]))
                self.assertEqual(set(result["implemented_ids"]), {p["code"] for p in result["propositions"]})
                self.assertTrue(all(p["rule_refs"][0]["version"] == "impact-1023-draft-v1" for p in result["propositions"]))
                self.assertTrue(all(not p["law_refs"] for p in result["propositions"]))

    def test_missing_signature_is_gap_never_fabricated_failure(self):
        for item, action_name, code in (("stop_work_unload", "hazard_analysis", "signatures_and_notification"),
                                        ("relief_effects", "technical_confirm", "signature_evidence")):
            chain = full_scope_chain(item)
            next(f for f in chain if f["fact_id"] == action_name)["fields"].pop("signature_present")
            result = self.evaluate(item, chain)
            self.assertEqual("missing", next(p for p in result["propositions"] if p["code"] == code)["state"])
            self.assertTrue(result["gaps"])
            self.assertFalse(result["findings"])

    def test_explicit_execution_parameter_personnel_and_equipment_failures(self):
        for field, value, code in (("parameters", [{"name": "孔深", "value": "19", "unit": "m"}], "relief_parameters_acceptance"),
                                   ("personnel_count", "6", "personnel_and_equipment"),
                                   ("unrelated_equipment_stopped", False, "personnel_and_equipment"),
                                   ("acceptance_passed", False, "relief_parameters_acceptance")):
            with self.subTest(field=field):
                chain = full_scope_chain()
                next(f for f in chain if f["fact_id"] == "relief")["fields"][field] = value
                result = self.evaluate("stop_work_unload", chain)
                finding = next(f for f in result["findings"] if f["code"] == code)
                self.assertEqual("no_pass", finding["status"])
                self.assertEqual("not_assessed", finding["major_hazard_classification"])
                self.assertTrue({"relief", "design"} <= set(finding["evidence_refs"]))

    def test_wrong_workface_design_and_actions_are_never_borrowed(self):
        for field in ("workface", "target_scope"):
            chain = full_scope_chain()
            design = next(f for f in chain if f["fact_id"] == "design")
            (design if field == "workface" else design["fields"])[field] = "W2"
            result = self.evaluate("stop_work_unload", chain)
            prop = next(p for p in result["propositions"] if p["code"] == "risk_and_design_scope")
            self.assertEqual("missing", prop["state"])
            self.assertNotIn("design", prop["evidence_refs"])
            self.assertFalse(result["findings"])

    def test_statement_cannot_establish_actual_execution_or_design(self):
        for identifier in ("hazard_analysis", "relief", "design"):
            chain = full_scope_chain()
            next(f for f in chain if f["fact_id"] == identifier)["source_role"] = "statement"
            result = self.evaluate("stop_work_unload", chain)
            self.assertTrue(result["gaps"])
            self.assertFalse(result["findings"])

    def test_signature_identity_mismatch_is_explicit_failure(self):
        chain = full_scope_chain("relief_effects")
        next(f for f in chain if f["fact_id"] == "technical_confirm")["fields"]["signer"] = "其他人"
        result = self.evaluate("relief_effects", chain)
        self.assertEqual("signature_evidence", result["findings"][0]["code"])

    def test_signature_after_dispatch_is_not_authorization_before_release(self):
        chain = full_scope_chain("relief_effects")
        next(f for f in chain if f["fact_id"] == "technical_confirm")["fields"]["signed_at"] = "2026-10-01T08:07:00"
        result = self.evaluate("relief_effects", chain)
        finding = next(f for f in result["findings"] if f["code"] == "signature_evidence")
        self.assertTrue({"technical_confirm", "dispatch_release"} <= set(finding["evidence_refs"]))

    def test_unapproved_requirements_cannot_establish_applicability_or_failure(self):
        for item in ("stop_work_unload", "relief_effects"):
            chain = full_scope_chain(item)
            for record in chain:
                if record["kind"] in {"design", "risk_assessment"}:
                    record["fields"]["approved"] = False
                    if record["kind"] == "design":
                        record["fields"].update(technical_head="不适用负责人", max_personnel="0",
                                               relief_parameters=[{"name": "孔深", "operator": ">=", "value": "999", "unit": "m"}])
            result = self.evaluate(item, chain)
            self.assertTrue(result["gaps"])
            self.assertFalse(result["findings"], result)
            self.assertFalse(any("design" in p["evidence_refs"] or "risk" in p["evidence_refs"]
                                 for p in result["propositions"]))

    def test_unsigned_confirmation_while_work_remains_closed_is_unresolved(self):
        chain = full_scope_chain("relief_effects")
        chain = [f for f in chain if f["fact_id"] not in {"dispatch_release", "resume"}]
        next(f for f in chain if f["fact_id"] == "technical_confirm")["fields"]["signature_present"] = False
        result = self.evaluate("relief_effects", chain)
        self.assertFalse(result["findings"], result)
        self.assertTrue(result["gaps"])
        self.assertEqual("missing", next(p for p in result["propositions"] if p["code"] == "signature_evidence")["state"])

    def test_unsigned_confirmation_with_proven_release_can_fail(self):
        chain = full_scope_chain("relief_effects")
        next(f for f in chain if f["fact_id"] == "technical_confirm")["fields"]["signature_present"] = False
        result = self.evaluate("relief_effects", chain)
        self.assertTrue(any(f["code"] == "signature_evidence" for f in result["findings"]))

    def test_expired_or_overlapping_design_is_unresolved(self):
        for mode in ("expired", "duplicate"):
            chain = full_scope_chain()
            design = next(f for f in chain if f["fact_id"] == "design")
            if mode == "expired":
                design["fields"]["valid_to"] = "2026-09-30"
            else:
                duplicate = deepcopy(design)
                duplicate.update(fact_id="design-copy", evidence_refs=["design-copy"])
                chain.append(duplicate)
            result = self.evaluate("stop_work_unload", chain)
            self.assertTrue(result["gaps"])
            self.assertFalse(result["findings"])

    def test_malformed_parameter_and_missing_count_are_gaps(self):
        chain = full_scope_chain()
        relief = next(f for f in chain if f["fact_id"] == "relief")["fields"]
        relief["parameters"] = [{"name": "孔深", "value": "NaN", "unit": "m"}]
        relief["personnel_count"] = "4.5"
        result = self.evaluate("stop_work_unload", chain)
        self.assertTrue(result["gaps"])
        self.assertFalse(result["findings"])

    def test_uncited_and_cross_run_inputs_are_rejected(self):
        run, snapshot = run_snapshot("stop_work_unload", full_scope_chain())
        snapshot["facts"][0]["evidence_refs"] = ["unregistered"]
        with self.assertRaises(InvalidRequest):
            evaluate_scope(run, snapshot)
        run, snapshot = run_snapshot("stop_work_unload", full_scope_chain())
        snapshot["run_id"] = "another-run"
        with self.assertRaises(InvalidRequest):
            evaluate_scope(run, snapshot)

    def test_no_hazard_analysis_does_not_skip_evacuation(self):
        chain = full_scope_chain()
        chain[0]["fields"]["hazard_confirmed"] = False
        analysis = next(f for f in chain if f["fact_id"] == "hazard_analysis")
        analysis["fields"]["hazard_confirmed"] = False
        chain = [f for f in chain if f["fact_id"] != "relief"]
        result = self.evaluate("stop_work_unload", chain)
        self.assertFalse(result["gaps"], result)
        chain = [f for f in chain if f["fact_id"] != "evacuate"]
        result = self.evaluate("stop_work_unload", chain)
        self.assertTrue(any("撤人" in gap for gap in result["gaps"]))


if __name__ == "__main__":
    unittest.main()
