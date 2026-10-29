import copy
import hashlib
import json
from itertools import permutations
import unittest

from apps.control_api.coverage import audit_coverage, sign_scope_proof
from apps.control_api.planner import compile_plan, resolve_material_requirements, validate_plan


class PlanContractTests(unittest.TestCase):
    def plan(self):
        return {"itemCode": "test-only", "requiredPropositions": ["action", "trigger"],
                "materialRequirements": [{"materialGroup": "record", "role": "actual_record", "cadence": "daily"}],
                "propositionDependencies": {"action": ["trigger"]},
                "factRequirements": [{"kind": "action", "fields": {"time": {"type": "datetime", "required": True}}}]}

    def test_current_plan_compiles_and_contract_order_is_deterministic(self):
        original = self.plan()
        result = validate_plan(original)
        self.assertEqual(result["executionPlan"]["propositionOrder"], ["trigger", "action"])
        self.assertNotIn("executionPlan", original)
        plan = compile_plan("stop_work_unload")
        self.assertEqual(len(plan["executionPlan"]["materials"]), 6)
        self.assertEqual(len(plan["planRevision"]), 64)

    def test_retrieval_queries_reference_frozen_propositions(self):
        plan = self.plan()
        plan["retrievalQueries"] = {"trigger": ["预警", "超限"]}
        self.assertEqual(validate_plan(plan)["executionPlan"]["retrievalQueries"], plan["retrievalQueries"])
        for value in ({"outside": ["预警"]}, {"trigger": []}, {"trigger": ["预警", "预警"]}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_plan({**self.plan(), "retrievalQueries": value})

    def test_unknown_dependency_and_cycle_are_rejected_before_execution(self):
        for dependencies in ({"action": ["outside"]}, {"action": ["trigger"], "trigger": ["action"]}):
            plan = self.plan()
            plan["propositionDependencies"] = dependencies
            with self.assertRaises(ValueError):
                validate_plan(plan)

    def test_operators_are_host_registered_not_model_supplied(self):
        plan = self.plan()
        plan["deterministicOperators"] = ["test.operator"]
        plan["propositionGraph"] = [{"code": "action", "operator": "test.operator"}]
        with self.assertRaisesRegex(ValueError, "unregistered"):
            validate_plan(plan)
        result = validate_plan(plan, registered_operators={"test.operator"})
        self.assertEqual(result["executionPlan"]["propositions"][-1]["operator"], "test.operator")

    def test_duplicate_materials_invalid_field_tool_and_budget_are_rejected(self):
        variants = []
        plan = self.plan()
        plan["materialRequirements"] *= 2
        variants.append(plan)
        plan = self.plan()
        plan["factRequirements"][0]["fields"]["time"]["type"] = "shell"
        variants.append(plan)
        variants.extend([{**self.plan(), "allowedTools": ["shell"]}, {**self.plan(), "budgets": {"maxPages": True}},
                         {**self.plan(), "budgets": {"maxModelTurns": 0}}, {**self.plan(), "budgets": {"deadlineMs": 10**20}}])
        for variant in variants:
            with self.subTest(plan=variant), self.assertRaises(ValueError):
                validate_plan(variant)

    def test_item_code_cannot_escape_registered_asset_directory(self):
        for code in ("../other", "C:/secret", "", "nested/item"):
            with self.subTest(code=code), self.assertRaises(ValueError):
                compile_plan(code)

    def test_conditional_material_contract_is_compiled_and_validated(self):
        plan = self.plan()
        plan["conditionalMaterials"] = [{"when": {"proposition": "trigger", "state": "true"}, "materialGroups": ["record"]}]
        compiled = validate_plan(plan)
        self.assertEqual(compiled["executionPlan"]["conditionalMaterials"], plan["conditionalMaterials"])
        for condition in (
                {"when": {"proposition": "outside", "state": "true"}, "materialGroups": ["record"]},
                {"when": {"proposition": "trigger", "state": "missing"}, "materialGroups": ["record"]},
                {"when": {"proposition": "trigger", "state": "true"}, "materialGroups": ["outside"]},
                {"when": {"proposition": "trigger", "state": "true"}, "materialGroups": ["record", "record"]},
                {"when": {"proposition": "trigger", "state": "true"}, "materialGroups": []},
        ):
            with self.subTest(condition=condition), self.assertRaises(ValueError):
                validate_plan({**plan, "conditionalMaterials": [condition]})
        with self.assertRaisesRegex(ValueError, "duplicate conditional"):
            validate_plan({**plan, "conditionalMaterials": plan["conditionalMaterials"] * 2})

    def test_material_resolution_preserves_supporting_and_legacy_domain_notes(self):
        plan = self.plan()
        plan["conditionalRequirements"] = [{"condition": "unresolved-domain-trigger", "requires": ["record"]}]
        result = resolve_material_requirements(validate_plan(plan))
        self.assertEqual(result["supporting_material_groups"], ["record"])
        self.assertEqual(result["required_material_groups"], [])
        self.assertEqual(result["deferred_material_groups"], [])
        self.assertFalse(result["legacy_conditions_interpreted"])

    def test_budget_aliases_compile_to_runtime_keys_and_legacy_tokens_are_enforced(self):
        plan = {**self.plan(), "budgets": {"max_tool_calls": 30, "max_model_turns": 20, "max_pages": 40,
                                          "maxTokens": 1000, "max_output_tokens": 500, "model_max_retries": 0,
                                          "deadline_ms": 5000}}
        compiled = validate_plan(plan)
        expected = {"maxToolCalls": 30, "maxModelTurns": 20, "maxPages": 40, "maxInputTokens": 1000,
                    "maxOutputTokens": 500, "maxModelRetries": 0, "deadlineMs": 5000}
        self.assertEqual(compiled["budgets"], expected)
        self.assertEqual(compiled["executionPlan"]["budgets"], expected)
        for budgets in ({"maxInputTokens": 1000, "max_input_tokens": 999},
                        {"maxRetries": 6}, {"maxPages": 10001}, {"maxOutputTokens": 10000001}):
            with self.subTest(budgets=budgets), self.assertRaises(ValueError):
                validate_plan({**self.plan(), "budgets": budgets})


class CoverageAuditTests(unittest.TestCase):
    def setUp(self):
        self.run = {"run_id": "r1", "mine_id": "m1", "check_cycle": "2026-10", "environment": "production"}
        self.snapshot = {"run_id": "r1", "mine_id": "m1", "manifest_hash": "manifest-hash",
                         "files": [{"file_id": "f1", "content_hash": "file-hash", "material_group": "record"}],
                         "coverage": [{"file_id": "f1", "unit_id": "page:1", "state": "readable"}]}
        self.plan = {"materialRequirements": [{"materialGroup": "record", "required": True}]}
        self.key = "test-authority-secret"
        self.payload = {"authority": "test-source-catalog", "mine_id": "m1", "check_cycle": "2026-10",
                        "run_id": "r1", "manifest_hash": "manifest-hash", "scope_complete": True,
                        "files": [{"file_id": "f1", "content_hash": "file-hash"}]}
        self.searches = [{"file_id": "f1", "unit_id": "page:1", "complete": True, "hit_count": 0}]

    def audit(self, **kwargs):
        return audit_coverage(self.run, self.snapshot, self.plan, **kwargs)

    def test_signed_production_scope_and_exhaustive_no_hit_are_distinct_from_absence(self):
        proof = sign_scope_proof(self.payload, self.key)
        before = copy.deepcopy(self.snapshot)
        result = self.audit(proof=proof, proof_key=self.key, searches=self.searches)
        self.assertTrue(result["source_complete"])
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(result["units"][0]["state"], "searched_no_hit")
        self.assertTrue(result["absence_is_not_negative"])
        self.assertEqual(self.snapshot, before)

    def test_parsing_and_top_k_search_do_not_prove_scope_or_exhaustive_search(self):
        self.snapshot.update(source_complete=True, scope_proof=True)
        result = self.audit(searches=[{"file_id": "f1", "unit_id": "page:1", "total": 20}])
        self.assertFalse(result["source_complete"])
        self.assertFalse(result["coverage_complete"])
        self.assertEqual(result["units"][0]["state"], "not_searched")
        self.assertIn("scope_proof_missing", result["gaps"])

    def test_stale_exhaustive_receipt_does_not_close_current_index(self):
        self.snapshot["evidence"] = [{"evidence_id": "ev1", "file_id": "f1", "text": "原文"}]
        result = self.audit(searches=[{"file_id": "f1", "unit_id": "page:1", "complete": True,
                                      "hit_count": 0, "query_hash": "x", "index_revision": "old"}])
        self.assertEqual(result["units"][0]["state"], "not_searched")

    def test_plan_queries_must_all_close_on_current_index(self):
        self.snapshot["evidence"] = [{"evidence_id": "ev1", "file_id": "f1", "text": "预警 停工"}]
        self.plan["retrievalQueries"] = {"trigger": ["预警"], "stop": ["停工"]}
        revision = hashlib.sha256(json.dumps(["ev1"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        def receipt(query, hits):
            return {"file_id": "f1", "unit_id": "page:1", "complete": True,
                    "query_hash": hashlib.sha256(json.dumps(query, ensure_ascii=False).encode()).hexdigest(),
                    "index_revision": revision, "unit_hit_counts": {"f1/page:1": hits}}
        first = [receipt("", 1), receipt("预警", 1)]
        self.assertEqual(self.audit(searches=first)["units"][0]["state"], "not_searched")
        self.assertEqual(self.audit(searches=first + [receipt("停工", 0)])["units"][0]["state"], "searched_hit")
        self.assertEqual(self.audit(searches=[receipt("", 0), receipt("预警", 0), receipt("停工", 0)])["units"][0]["state"], "searched_no_hit")

    def test_tamper_wrong_owner_cycle_manifest_and_incomplete_inventory_fail(self):
        proof = sign_scope_proof(self.payload, self.key)
        proof["payload"]["authority"] = "attacker"
        variants = [proof]
        for field, value in (("mine_id", "m2"), ("check_cycle", "2026-09"), ("manifest_hash", "other"),
                             ("run_id", "r2"), ("scope_complete", False), ("files", [])):
            variants.append(sign_scope_proof({**self.payload, field: value}, self.key))
        for variant in variants:
            with self.subTest(proof=variant):
                self.assertFalse(self.audit(proof=variant, proof_key=self.key, searches=self.searches)["source_complete"])

    def test_required_missing_material_is_source_absent_but_legacy_list_is_descriptive(self):
        self.plan["materialRequirements"].append({"materialGroup": "missing", "required": True})
        result = self.audit(proof=sign_scope_proof(self.payload, self.key), proof_key=self.key)
        self.assertIn("source_absent:missing", result["gaps"])
        self.assertFalse(result["source_complete"])
        self.plan["materialRequirements"][-1].pop("required")
        self.assertNotIn("source_absent:missing", self.audit()["gaps"])

    def test_successful_work_units_supply_parse_coverage_without_overwriting_inventory(self):
        self.snapshot["coverage"] = [{"file_id": "f1", "unit_id": "page:1", "state": "not_indexed"}]
        work = {"state": "succeeded", "input_json": {"file_id": "f1"},
                "output_json": {"coverage": [{"file_id": "f1", "unit_id": "page:1", "state": "readable"}]}}
        result = self.audit(work_units=[work], searches=self.searches)
        self.assertEqual(result["units"][0]["state"], "searched_no_hit")
        self.assertEqual(self.snapshot["coverage"][0]["state"], "not_indexed")

    def test_unreadable_failed_conflicted_and_not_indexed_remain_distinct(self):
        for state, expected in (("unreadable", "unreadable"), ("failed", "provider_failed"),
                                ("conflicted", "conflicted"), ("not_indexed", "not_indexed")):
            self.snapshot["coverage"][0]["state"] = state
            with self.subTest(state=state):
                result = self.audit(searches=self.searches)
                self.assertEqual(result["units"][0]["state"], expected)
                self.assertFalse(result["coverage_complete"])

    def test_unparsed_inventory_page_and_missing_contract_file_cannot_close(self):
        self.snapshot["files"][0]["page_count"] = 2
        result = self.audit(searches=self.searches)
        self.assertEqual([unit["state"] for unit in result["units"]], ["searched_no_hit", "not_indexed"])
        self.assertFalse(result["coverage_complete"])
        self.plan["coverageContract"] = {"units": [{"file_id": "missing", "unit_id": "page:1"}]}
        self.assertTrue(any(unit["state"] == "source_absent" for unit in self.audit()["units"]))

    def test_other_mine_snapshot_cannot_use_valid_run_scope_proof(self):
        self.snapshot["mine_id"] = "other"
        result = self.audit(proof=sign_scope_proof(self.payload, self.key), proof_key=self.key, searches=self.searches)
        self.assertFalse(result["source_complete"])
        self.assertIn("scope_snapshot_owner_mismatch", result["gaps"])

    def conditional_plan(self):
        self.plan["requiredPropositions"] = ["trigger"]
        self.plan["materialRequirements"].append({"materialGroup": "followup"})
        self.plan["conditionalMaterials"] = [{"when": {"proposition": "trigger", "state": "true"}, "materialGroups": ["followup"]}]
        self.plan = validate_plan(self.plan)
        return sign_scope_proof(self.payload, self.key)

    def test_true_condition_activates_required_material_and_absence_gap(self):
        proof = self.conditional_plan()
        result = self.audit(proof=proof, proof_key=self.key, searches=self.searches,
                            propositions=[{"code": "trigger", "state": "true"}])
        self.assertEqual(result["required_material_groups"], ["followup", "record"])
        self.assertIn("source_absent:followup", result["gaps"])
        self.assertFalse(result["source_complete"])
        self.assertFalse(result["coverage_complete"])

    def test_false_condition_is_inactive_and_does_not_require_followup(self):
        proof = self.conditional_plan()
        result = self.audit(proof=proof, proof_key=self.key, searches=self.searches,
                            propositions=[{"code": "trigger", "state": "false"}])
        self.assertEqual(result["required_material_groups"], ["record"])
        self.assertEqual(result["material_requirements"]["inactive_material_groups"], ["followup"])
        self.assertNotIn("source_absent:followup", result["gaps"])
        self.assertTrue(result["source_complete"])
        self.assertTrue(result["coverage_complete"])

    def test_unknown_condition_is_deferred_not_assumed_false_or_absent(self):
        proof = self.conditional_plan()
        for propositions in ([], [{"code": "trigger", "state": "missing"}],
                             [{"code": "trigger", "state": "true"}, {"code": "trigger", "state": "false"}]):
            with self.subTest(propositions=propositions):
                result = self.audit(proof=proof, proof_key=self.key, searches=self.searches, propositions=propositions)
                self.assertEqual(result["deferred_material_groups"], ["followup"])
                self.assertIn("material_condition_unresolved:followup", result["gaps"])
                self.assertNotIn("source_absent:followup", result["gaps"])
                self.assertFalse(result["coverage_complete"])

    def test_any_triggered_event_activates_material_without_losing_all_file_audit(self):
        self.conditional_plan()
        resolved = resolve_material_requirements(self.plan, [{"event_id": "e1", "code": "trigger", "state": "true"},
                                                           {"event_id": "e2", "code": "trigger", "state": "false"}])
        self.assertEqual(resolved["required_material_groups"], ["followup", "record"])
        self.snapshot["files"].append({"file_id": "f2", "content_hash": "h2", "material_group": "followup"})
        result = self.audit(propositions=[{"code": "trigger", "state": "false"}])
        self.assertTrue(any(unit["file_id"] == "f2" and unit["state"] == "not_indexed" for unit in result["units"]))

    def test_explicit_false_condition_is_supported_without_inverting_unknown(self):
        self.conditional_plan()
        self.plan["conditionalMaterials"][0]["when"]["state"] = "false"
        resolved = resolve_material_requirements(self.plan, [{"code": "trigger", "state": "false"}])
        self.assertIn("followup", resolved["required_material_groups"])
        deferred = resolve_material_requirements(self.plan)
        self.assertIn("followup", deferred["deferred_material_groups"])

    def test_successful_parse_does_not_hide_separate_recognition_failure_in_any_order(self):
        failed = {"work_unit_id": "recognition", "kind": "recognize_page", "state": "failed",
                  "input": {"file_id": "f1", "page": 1}, "attempt": 1, "updated_at": 20}
        parsed = {"work_unit_id": "parse", "kind": "parse_file", "state": "succeeded",
                  "input": {"file_id": "f1"}, "output": {"coverage": self.snapshot["coverage"]},
                  "attempt": 1, "updated_at": 10}
        for ordered in permutations([failed, parsed]):
            result = self.audit(work_units=ordered, searches=self.searches)
            self.assertFalse(result["coverage_complete"])
            self.assertEqual(result["units"][0]["state"], "provider_failed")

    def test_latest_same_work_identity_supersedes_old_attempt_independent_of_order(self):
        failed = {"work_unit_id": "recognition", "kind": "recognize_page", "state": "failed",
                  "input": {"file_id": "f1", "page": 1}, "attempt": 1, "updated_at": 10}
        succeeded = {**failed, "state": "succeeded", "attempt": 2, "updated_at": 20,
                     "output": {"coverage": self.snapshot["coverage"]}}
        for ordered in permutations([failed, succeeded]):
            result = self.audit(work_units=ordered, searches=self.searches)
            self.assertTrue(result["coverage_complete"])
            self.assertEqual(result["units"][0]["state"], "searched_no_hit")

    def test_review_closes_only_corresponding_page_failure_and_not_a_newer_failure(self):
        self.snapshot["coverage"].append({"file_id": "f1", "unit_id": "page:2", "state": "readable"})
        searches = self.searches + [{"file_id": "f1", "unit_id": "page:2", "complete": True, "hit_count": 0}]
        failures = [{"work_unit_id": "ocr" + str(page), "kind": "recognize_page", "state": "failed",
                     "input": {"file_id": "f1", "page": page}, "updated_at": 10} for page in (1, 2)]
        reviewed = {"work_unit_id": "review", "kind": "review_transcription", "state": "succeeded", "updated_at": 20,
                    "output": {"coverage": [{"file_id": "f1", "unit_id": "page:1", "state": "readable"}]}}
        for ordered in permutations(failures + [reviewed]):
            result = self.audit(work_units=ordered, searches=searches)
            states = {row["unit_id"]: row["state"] for row in result["units"]}
            self.assertEqual(states, {"page:1": "searched_no_hit", "page:2": "provider_failed"})
            self.assertFalse(result["coverage_complete"])
        late_failure = {**failures[0], "updated_at": 30, "attempt": 2}
        result = self.audit(work_units=[reviewed, late_failure], searches=searches)
        self.assertEqual(result["units"][0]["state"], "provider_failed")

    def test_native_reparse_cannot_undo_independent_review_for_same_frozen_page(self):
        parsed = {"work_unit_id": "parse", "kind": "parse_file", "state": "succeeded", "updated_at": 30,
                  "input": {"file_id": "f1"}, "output": {"coverage": [{"file_id": "f1", "unit_id": "page:1", "state": "unreadable"}]}}
        reviewed = {"work_unit_id": "review", "kind": "review_transcription", "state": "succeeded", "updated_at": 20,
                    "output": {"coverage": [{"file_id": "f1", "unit_id": "page:1", "state": "readable"}]}}
        for ordered in permutations([parsed, reviewed]):
            result = self.audit(work_units=ordered, searches=self.searches)
            self.assertEqual(result["units"][0]["state"], "searched_no_hit")
            self.assertTrue(result["coverage_complete"])


if __name__ == "__main__":
    unittest.main()
