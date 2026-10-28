import json
import tempfile
import unittest
from pathlib import Path

from apps.control_api.operators import GraphEvaluator
from apps.control_api.registry import CheckRegistry


def plan(operator="all_of"):
    return {"executionPlan": {"propositions": [
        {"code": "observed", "operator": "field_equals", "config": {"source": {"kind": "record", "source_role": "actual_record", "where": {"event_id": "E1", "step": "observed"}}, "field": "status", "value": "done"}},
        {"code": "ordered", "operator": "before_time", "dependsOn": ["observed"], "config": {"first": {"selector": {"kind": "record", "where": {"event_id": "E1", "step": "observed"}}, "field": "time"}, "second": {"selector": {"kind": "record", "where": {"event_id": "E1", "step": "release"}}, "field": "time"}, "allow_equal": True}},
    ]}}


def snapshot(status="done", candidate=False):
    refs = [{"evidence_id": "e1", "source_role": "actual_record", "text": "source"}, {"evidence_id": "e2", "source_role": "actual_record", "text": "source"}]
    fact1 = {"kind": "record", "source_role": "actual_record", "event_id": "E1", "evidence_refs": ["e1"], "fields": {"step": "observed", "status": status, "time": "2026-10-01T08:00:00"}}
    fact2 = {"kind": "record", "source_role": "actual_record", "event_id": "E1", "evidence_refs": ["e2"], "fields": {"step": "release", "time": "2026-10-01T09:00:00"}}
    return {"evidence": refs, "verified_facts": [] if candidate else [fact1, fact2], "source_complete": True, "coverage_complete": True}


class OperatorGraphTests(unittest.TestCase):
    def test_registered_synthetic_graph_executes_operators_in_dependency_order(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            asset = root / "synthetic.json"
            evaluator = GraphEvaluator()
            propositions = plan()["executionPlan"]["propositions"]
            # all_of is deliberately declared before its dependencies.
            graph = [{"code": "closed", "operator": "all_of", "dependsOn": ["observed", "ordered"]}, *propositions]
            asset.write_text(json.dumps({"itemCode": "synthetic_graph", "requiredPropositions": ["closed"],
                                         "propositionGraph": graph, "deterministicOperators": list(evaluator.operator_names)}), encoding="utf-8")
            registry = CheckRegistry(root=root)
            registry.register("synthetic_graph", asset, evaluator, operators=evaluator.operator_names)
            run = {"run_id": "r1", "item_code": "synthetic_graph", "plan": registry.compile("synthetic_graph")}
            result = registry.evaluate(run, snapshot())
            self.assertEqual(result["status"], "pass")
            self.assertEqual(next(p for p in result["propositions"] if p["code"] == "closed")["evidence_refs"], ["e1", "e2"])

    def test_graph_uses_only_verified_facts_and_is_order_stable(self):
        result = GraphEvaluator()({"run_id": "r1", "item_code": "synthetic", "plan": plan()}, snapshot())
        self.assertEqual(result["status"], "pass")
        shuffled = snapshot()
        shuffled["verified_facts"] = list(reversed(shuffled["verified_facts"]))
        self.assertEqual(GraphEvaluator()({"run_id": "r1", "item_code": "synthetic", "plan": plan()}, shuffled)["status"], "pass")

    def test_missing_data_stays_pending_and_candidate_is_not_a_fact(self):
        missing = snapshot()
        missing["verified_facts"][0]["fields"].pop("status")
        result = GraphEvaluator()({"run_id": "r1", "item_code": "synthetic", "plan": plan()}, missing)
        self.assertEqual(result["status"], "pending")
        candidate = snapshot(candidate=True)
        candidate["candidates"] = [{"verified": True, "facts": [{"kind": "record"}]}]
        self.assertEqual(GraphEvaluator()({"run_id": "r1", "item_code": "synthetic", "plan": plan()}, candidate)["status"], "pending")

    def test_explicit_false_has_registered_finding_and_unknown_operator_fails(self):
        result = GraphEvaluator()({"run_id": "r1", "item_code": "synthetic", "plan": plan()}, snapshot(status="not_done"))
        self.assertEqual(result["status"], "no_pass")
        self.assertEqual(result["findings"][0]["evidence_refs"], ["e1"])
        bad = {"executionPlan": {"propositions": [{"code": "x", "operator": "eval"}]}}
        with self.assertRaises(ValueError):
            GraphEvaluator()({"run_id": "r1", "item_code": "synthetic", "plan": bad}, snapshot())

    def test_candidates_conflicts_wrong_role_and_unreadable_evidence_cannot_be_facts(self):
        data = snapshot()
        data["candidates"] = [{"verified": True, "facts": data["verified_facts"]}]
        data["verified_facts"] = []
        data["evidence"][0]["quality"] = "model_candidate"
        self.assertEqual(GraphEvaluator()({"run_id": "r1", "item_code": "synthetic", "plan": plan()}, data)["status"], "pending")
        data = snapshot()
        data["evidence"][0]["source_role"] = "requirement"
        self.assertEqual(GraphEvaluator()({"run_id": "r1", "item_code": "synthetic", "plan": plan()}, data)["status"], "pending")
        data = snapshot()
        data["coverage"] = [{"file_id": "f1", "unit_id": "page:1", "state": "unreadable"}]
        self.assertEqual(GraphEvaluator()({"run_id": "r1", "item_code": "synthetic", "plan": plan()}, data)["status"], "unverifiable")
        data = snapshot()
        data["verified_facts"][0]["validation_status"] = "source_bound"
        self.assertEqual(GraphEvaluator()({"run_id": "r1", "item_code": "synthetic", "plan": plan()}, data)["status"], "pending")
        data = snapshot()
        data["evidence"][0]["mine_id"] = "other"
        self.assertEqual(GraphEvaluator()({"run_id": "r1", "mine_id": "mine-a", "item_code": "synthetic", "plan": plan()}, data)["status"], "pending")

    def test_numeric_operator_requires_real_finite_values_and_matching_units(self):
        graph = {"executionPlan": {"propositions": [{"code": "within_limit", "operator": "compare_number", "config": {
            "left": {"selector": {"kind": "measurement"}, "field": "value", "unit": "MPa"},
            "right": {"value": "10", "unit": "MPa"}, "comparison": "<="}}]}}
        data = {"source_complete": True, "coverage_complete": True,
                "evidence": [{"evidence_id": "e1", "source_role": "actual_record"}],
                "verified_facts": [{"kind": "measurement", "source_role": "actual_record", "evidence_refs": ["e1"],
                                    "fields": {"value": "11", "unit": "MPa"}}]}
        run = {"run_id": "r1", "item_code": "synthetic", "plan": graph}
        self.assertEqual(GraphEvaluator()(run, data)["status"], "no_pass")
        for changed in ({"value": "NaN", "unit": "MPa"}, {"value": "5", "unit": "kPa"}, {"value": True, "unit": "MPa"}, {"unit": "MPa"}):
            data["verified_facts"][0]["fields"] = changed
            self.assertEqual(GraphEvaluator()(run, data)["status"], "pending", changed)

    def test_time_operator_refuses_date_only_mixed_timezones_and_different_events(self):
        run = {"run_id": "r1", "item_code": "synthetic", "plan": plan()}
        for changed in ({"time": "2026-10-01"}, {"time": "2026-10-01T09:00:00+08:00"}, {"event_id": "E2"}):
            data = snapshot()
            data["verified_facts"][1]["fields"].update(changed)
            self.assertEqual(GraphEvaluator()(run, data)["status"], "pending", changed)

    def test_field_presence_empty_graph_and_missing_coverage_do_not_pass(self):
        graph = {"executionPlan": {"propositions": [{"code": "has_value", "operator": "field_present",
                 "config": {"source": {"kind": "record", "where": {"step": "observed"}}, "field": "status"}}]}}
        run = {"run_id": "r1", "item_code": "synthetic", "plan": graph}
        self.assertEqual(GraphEvaluator()(run, snapshot())["status"], "pass")
        data = snapshot()
        data["verified_facts"][0]["fields"].pop("status")
        self.assertEqual(GraphEvaluator()(run, data)["status"], "pending")
        data = snapshot()
        data["coverage_complete"] = False
        self.assertEqual(GraphEvaluator()(run, data)["status"], "pending")
        run["plan"] = {"executionPlan": {"propositions": []}}
        self.assertEqual(GraphEvaluator()(run, snapshot())["status"], "pending")

    def test_conflicted_or_unreadable_support_cannot_publish_a_false_finding(self):
        for state in ("unreadable", "conflicted"):
            data = snapshot(status="not_done")
            data["evidence"][0].update(file_id="f1", unit_id="page:1")
            data["coverage"] = [{"file_id": "f1", "unit_id": "page:1", "state": state}]
            result = GraphEvaluator()({"run_id": "r1", "item_code": "synthetic", "plan": plan()}, data)
            self.assertEqual(result["status"], "unverifiable")
            self.assertEqual(result["findings"], [])


if __name__ == "__main__":
    unittest.main()
