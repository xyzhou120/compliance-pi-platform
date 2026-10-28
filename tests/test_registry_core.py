import json
import re
import tempfile
import unittest
from pathlib import Path

from apps.control_api.planner import compile_plan
from apps.control_api.registry import CheckRegistry, default_registry
from apps.control_api.operators import GraphEvaluator


def synthetic_evaluator(run, snapshot):
    return {"status": "pending", "item_code": run["item_code"],
            "evidence_refs": [record["evidence_id"] for record in snapshot.get("evidence", [])]}


class RegistryCoreTests(unittest.TestCase):
    def test_default_registry_contains_only_the_currently_supported_item(self):
        registry = default_registry()
        self.assertEqual(registry.supported_items(), ("stop_work_unload",))
        self.assertEqual(registry.compile("stop_work_unload")["itemCode"], "stop_work_unload")
        registry.assert_compatible({"item_code": "stop_work_unload", "plan": compile_plan("stop_work_unload")})

    def test_registration_is_code_owned_and_duplicate_safe(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            asset = root / "synthetic.v1.json"
            asset.write_text(json.dumps({"itemCode": "synthetic"}), encoding="utf-8")
            registry = CheckRegistry(root=root)
            registry.register("synthetic", asset, lambda run, snapshot: {"status": "pending"})
            with self.assertRaises(ValueError):
                registry.register("synthetic", asset, lambda run, snapshot: {"status": "pending"})
            with self.assertRaises(ValueError):
                registry.compile("unknown")

    def test_asset_or_evaluator_revision_change_blocks_frozen_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            asset = root / "synthetic.v1.json"
            asset.write_text(json.dumps({"itemCode": "synthetic"}), encoding="utf-8")
            registry = CheckRegistry(root=root)
            registry.register("synthetic", asset, lambda run, snapshot: {"status": "pending"})
            plan = registry.compile("synthetic")
            run = {"item_code": "synthetic", "plan": plan}
            registry.assert_compatible(run)
            asset.write_text(json.dumps({"itemCode": "synthetic", "revision": 2}), encoding="utf-8")
            with self.assertRaises(ValueError):
                registry.assert_compatible(run)

    def test_new_item_evaluates_through_the_same_registry_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            asset = root / "synthetic.v1.json"
            asset.write_text(json.dumps({"itemCode": "synthetic", "requiredPropositions": ["observed"],
                                        "propositionGraph": [{"code": "observed", "operator": "has_source"}],
                                        "deterministicOperators": ["has_source"]}), encoding="utf-8")
            registry = CheckRegistry(root=root)
            registry.register("synthetic", asset, synthetic_evaluator, operators=("has_source",),
                              fact_contract={"observation": {"value": {"type": "string"}}})
            plan = registry.compile("synthetic")
            run = {"item_code": "synthetic", "plan": plan}
            result = registry.evaluate(run, {"evidence": [{"evidence_id": "source-1"}]})
            self.assertEqual(result, {"status": "pending", "item_code": "synthetic", "evidence_refs": ["source-1"]})
            self.assertEqual(plan["executionPlan"]["propositionOrder"], ["observed"])
            plan["registry"]["factContract"]["observation"] = {}
            self.assertIn("value", registry.compile("synthetic")["registry"]["factContract"]["observation"])

    def test_changed_evaluator_code_and_plan_tampering_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            asset = root / "synthetic.v1.json"
            asset.write_text(json.dumps({"itemCode": "synthetic"}), encoding="utf-8")
            registry = CheckRegistry(root=root)
            def evaluator(run, snapshot):
                return {"status": "pending"}
            def replacement(run, snapshot):
                return {"status": "failed"}
            registry.register("synthetic", asset, evaluator)
            run = {"item_code": "synthetic", "plan": registry.compile("synthetic")}
            evaluator.__code__ = replacement.__code__
            with self.assertRaises(ValueError):
                registry.evaluate(run, {})
            run["plan"] = registry.compile("synthetic")
            run["plan"]["registry"]["factContract"] = {"injected": True}
            # Revision must describe the whole accepted plan, not just an unchanged stamp.
            with self.assertRaises(ValueError):
                registry.assert_compatible(run)

    def test_unknown_item_and_outside_asset_never_import_task_input(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            asset = root / "wrong.json"
            asset.write_text(json.dumps({"itemCode": "wrong"}), encoding="utf-8")
            registry = CheckRegistry(root=root / "owned")
            with self.assertRaises(ValueError):
                registry.register("synthetic", asset, synthetic_evaluator)
            registry = CheckRegistry(root=root)
            registry.register("synthetic", asset, synthetic_evaluator)
            with self.assertRaises(ValueError):
                registry.compile("synthetic")
            with self.assertRaises(ValueError):
                registry.evaluate({"item_code": "module:arbitrary", "plan": {}}, {})

    def test_graph_operator_callables_and_closure_configuration_change_revision(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            asset = root / "graph.json"
            asset.write_text(json.dumps({"itemCode": "synthetic_graph", "propositionGraph": [{"code": "custom", "operator": "custom"}]}), encoding="utf-8")
            def operator_with_value(value):
                return lambda evaluator, node, facts: {"state": value, "evidence_refs": [], "detail": "configured"}
            first = CheckRegistry(root=root)
            evaluator = GraphEvaluator({"custom": operator_with_value("true")})
            first.register("synthetic_graph", asset, evaluator, operators=evaluator.operator_names)
            accepted = {"item_code": "synthetic_graph", "plan": first.compile("synthetic_graph")}
            second = CheckRegistry(root=root)
            replacement = GraphEvaluator({"custom": operator_with_value("false")})
            second.register("synthetic_graph", asset, replacement, operators=replacement.operator_names)
            self.assertNotEqual(accepted["plan"]["planRevision"], second.compile("synthetic_graph")["planRevision"])
            with self.assertRaises(ValueError):
                second.assert_compatible(accepted)

    def test_operator_default_values_and_instance_configuration_are_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            asset = root / "graph.json"
            asset.write_text(json.dumps({"itemCode": "synthetic_graph"}), encoding="utf-8")
            def custom(evaluator, node, facts, expected=True):
                return {"state": "true" if expected else "false", "evidence_refs": [], "detail": "default"}
            evaluator = GraphEvaluator({"custom": custom})
            registry = CheckRegistry(root=root)
            registry.register("synthetic_graph", asset, evaluator, operators=evaluator.operator_names)
            run = {"item_code": "synthetic_graph", "plan": registry.compile("synthetic_graph")}
            custom.__defaults__ = (False,)
            with self.assertRaises(ValueError):
                registry.assert_compatible(run)
            class Configured:
                def __init__(self, threshold):
                    self.threshold = threshold
                def __call__(self, run, snapshot):
                    return {"status": "pending"}
            configured = Configured(10)
            registry = CheckRegistry(root=root)
            registry.register("synthetic_graph", asset, configured)
            run["plan"] = registry.compile("synthetic_graph")
            configured.threshold = 11
            with self.assertRaises(ValueError):
                registry.assert_compatible(run)
            configured.threshold = object()
            with self.assertRaises(ValueError):
                registry.compile("synthetic_graph")

    def test_registered_fact_validator_promotes_only_independent_source_facts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            asset = root / "temperature.json"
            asset.write_text(json.dumps({"itemCode": "temperature_check", "requiredPropositions": ["temperature"]}), encoding="utf-8")
            def verify(candidate, snapshot):
                source = snapshot["evidence"][0]["text"]
                match = re.search(r"温度\s*[:：]\s*(\d+(?:\.\d+)?)\s*℃", source)
                if not match or candidate["fields"].get("value") != float(match.group(1)):
                    return []
                return [{"fact_id": "temperature-f1", "kind": "measurement", "source_role": "actual_record",
                         "fields": {"value": float(match.group(1))}, "evidence_refs": ["e1"]}]
            registry = CheckRegistry(root=root)
            registry.register("temperature_check", asset, synthetic_evaluator, fact_contract={"value": {"type": "number"}}, fact_validator=verify)
            plan = registry.compile("temperature_check")
            run = {"run_id": "r1", "mine_id": "m1", "item_code": "temperature_check", "rule_revision": plan["planRevision"], "plan": plan}
            snapshot = {"run_id": "r1", "mine_id": "m1", "evidence": [{"evidence_id": "e1", "run_id": "r1", "mine_id": "m1", "source_role": "actual_record", "text": "温度:20℃"}]}
            bound = {"validation_status": "source_bound", "candidate": {"run_id": "r1", "rule_revision": plan["planRevision"], "kind": "measurement", "fields": {"value": 20.0}, "evidence_refs": ["e1"]}, "field_bindings": {"value": ["e1"]}, "reasons": []}
            promoted = registry.verify_candidate(run, bound, snapshot)
            self.assertTrue(promoted["verified"])
            self.assertEqual(promoted["facts"][0]["source_role"], "actual_record")
            bound["candidate"]["fields"]["value"] = 21.0
            rejected = registry.verify_candidate(run, bound, snapshot)
            self.assertFalse(rejected["verified"])
            self.assertEqual(rejected["facts"], [])
            changed = CheckRegistry(root=root)
            def other_verify(candidate, snapshot):
                return []
            changed.register("temperature_check", asset, synthetic_evaluator, fact_contract={"value": {"type": "number"}}, fact_validator=other_verify)
            with self.assertRaises(ValueError):
                changed.assert_compatible(run)


if __name__ == "__main__":
    unittest.main()
