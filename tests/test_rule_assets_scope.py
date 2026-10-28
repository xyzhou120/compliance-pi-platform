import json
import unittest
from pathlib import Path
from apps.control_api.planner import validate_plan


ROOT = Path(__file__).resolve().parents[1]


class StopWorkUnloadAssetScopeTests(unittest.TestCase):
    def setUp(self):
        self.asset = json.loads((ROOT / "rule_assets" / "stop_work_unload.v2.json").read_text(encoding="utf-8"))
        self.sources = json.loads((ROOT / "config" / "sources.example.json").read_text(encoding="utf-8"))

    def test_new_policy_contains_exact_six_material_groups(self):
        plan = validate_plan(self.asset)
        rows = self.asset["materialRequirements"]
        self.assertEqual({row["materialGroup"] for row in rows}, {"prevention_daily", "threshold", "dispatch_daily", "release", "relief_acceptance", "design"})
        self.assertEqual(len(rows), 6)
        conditions = {row["when"]["proposition"]: row for row in self.asset["conditionalMaterials"]}
        self.assertEqual(conditions["warning_danger_confirmed"]["materialGroups"], ["relief_acceptance", "design"])
        self.assertEqual(conditions["warning_danger_confirmed"]["when"], {"proposition": "warning_danger_confirmed", "state": "true"})
        self.assertEqual(conditions["trigger"]["materialGroups"], ["dispatch_daily"])
        self.assertNotIn("warning_danger_confirmed", self.asset["requiredPropositions"])
        self.assertIn("warning_danger_confirmed", [node["code"] for node in self.asset["propositionGraph"]])
        self.assertIn("warning_danger_confirmed", plan["executionPlan"]["propositionOrder"])
        self.assertEqual(self.asset["requiredPropositions"], ["trigger", "danger_assessment", "stop", "power_off", "evacuate", "report"])

    def test_instruction_layer_cannot_be_execution_source(self):
        release = next(row for row in self.asset["materialRequirements"] if row["materialGroup"] == "release")
        self.assertFalse(release["required"])
        self.assertIn("指令层", release["purpose"])
        config_release = next(row for row in self.sources["catalog"]["stop_work_unload"] if row["material_group"] == "release")
        self.assertTrue(config_release["instruction_layer"])

    def test_source_catalog_matches_exact_groups_cadence_and_roles(self):
        asset = {row["materialGroup"]: row for row in self.asset["materialRequirements"]}
        rows = self.sources["catalog"]["stop_work_unload"]
        self.assertEqual(len(rows), 6)
        self.assertEqual({row["material_group"] for row in rows}, set(asset))
        for row in rows:
            with self.subTest(group=row["material_group"]):
                expected = asset[row["material_group"]]
                self.assertEqual(row["source_role"], expected["role"])
                self.assertEqual(row["cadence"], expected["cadence"])
                self.assertEqual(row["display_name"], expected["displayName"])

    def test_frontend_labels_match_six_registered_material_names(self):
        labels = (ROOT / "frontend" / "labels.js").read_text(encoding="utf-8")
        for row in self.asset["materialRequirements"]:
            with self.subTest(group=row["materialGroup"]):
                self.assertIn(row["materialGroup"] + ":'" + row["displayName"] + "'", labels)

    def test_new_policy_has_revision_and_does_not_mutate_v1(self):
        old = json.loads((ROOT / "rule_assets" / "stop_work_unload.v1.json").read_text(encoding="utf-8"))
        self.assertNotEqual(self.asset["version"], old["version"])
        self.assertNotEqual(self.asset["policyRevision"], old.get("policyRevision"))
        self.assertNotEqual(self.asset["materialRequirements"], old["materialRequirements"])
        self.assertEqual(self.asset["legacySource"]["sha256"], old["source"]["sha256"])
        self.assertEqual(self.asset["source"]["type"], "user_confirmed_policy")
        self.assertNotIn("sha256", self.asset["source"])
        self.assertNotIn("sourceCells", self.asset)

    def test_finding_policy_requires_human_confirmation_for_major_class(self):
        self.assertTrue(self.asset["policies"]["majorFindingRequiresHumanConfirmation"])
        self.assertIn("suspectedMajorHazard", self.asset["findingPolicy"])
        self.assertIn("humanConfirmationRequiredFor", self.asset["findingPolicy"])


if __name__ == "__main__":
    unittest.main()
