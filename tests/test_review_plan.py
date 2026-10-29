from copy import deepcopy
import unittest

from fastapi.testclient import TestClient
from apps.control_api.main import create_app
from apps.control_api.review_plan import build_review_plan, decision_class, explain_frozen_run
from apps.control_api.settings import Settings
from apps.control_api.store import Store


class ReviewPlanTests(unittest.TestCase):
    def test_decision_class_keeps_business_review_separate_from_processing_and_missing_data(self):
        finding = {"status": "no_pass", "findings": [{"evidence_refs": ["ev1"]}]}
        system = [{"code": "provider_failed", "owner": "system"}]
        missing = [{"code": "source_absent", "owner": "source"}]
        review = [{"code": "proposition_review", "owner": "business", "evidence_refs": ["ev1"],
                   "targets": [{"file_id": "f1", "units": ["page:2"]}]}]
        self.assertEqual(decision_class(finding, system)[0], "issue_found")
        self.assertEqual(decision_class({"status": "pending"}, system)[0], "system_processing_incomplete")
        self.assertEqual(decision_class({"status": "pending"}, missing)[0], "missing_required_data")
        self.assertEqual(decision_class({"status": "pending"}, review)[0], "review_required")
        self.assertEqual(decision_class({"status": "pending"}, [])[0], "missing_required_data")

    def fixture(self):
        snapshot = {"run_id": "r", "manifest_hash": "m", "files": [{"file_id": "f", "original_name": "工作日报.pdf", "format": "pdf", "material_group": "prevention_daily"}],
                    "coverage": [], "evidence": []}
        result = {"status": "unverifiable", "description": "必要内容不可读", "propositions": [],
                  "coverage_audit": {"run_id": "r", "manifest_hash": "m", "rule_revision": "rev", "source_complete": False,
                                     "units": [{"file_id": "f", "unit_id": "page:3", "state": "provider_failed"}]}}
        return {"run_id": "r", "state": "completed", "rule_revision": "rev", "snapshot": snapshot, "result": result,
                "work_units": [{"kind": "recognize_page", "state": "running", "input": {"file_id": "f", "page": 2}}]}

    def test_historical_explanation_preserves_result_and_lists_exact_pages(self):
        run = self.fixture()
        original = deepcopy(run)
        plan = explain_frozen_run(run)
        self.assertEqual(run, original)
        self.assertFalse(plan["history_modified"])
        self.assertEqual(plan["based_on"], "stored_final_audit")
        failed = next(row for row in plan["actions"] if row["code"] == "provider_failed")
        self.assertEqual(failed["targets"][0]["units"], ["page:3"])
        self.assertEqual(failed["targets"][0]["file_name"], "工作日报.pdf")
        interrupted = next(row for row in plan["actions"] if row["code"] == "processing_incomplete")
        self.assertIn("新执行", interrupted["next_step"])
        self.assertNotIn("等待", interrupted["next_step"].split("；")[-1])

    def test_complete_audit_not_mislabeled_as_missing_scope(self):
        run = self.fixture()
        run["work_units"] = []
        run["result"].update(status="pass", propositions=[{"code": "stop", "state": "true"}])
        run["result"]["coverage_audit"].update(source_complete=True, units=[{"file_id": "f", "unit_id": "page:3", "state": "searched_hit"}])
        plan = explain_frozen_run(run)
        self.assertTrue(plan["source_complete"])
        self.assertEqual(plan["actions"], [])

    def test_foreign_audit_is_not_used(self):
        run = self.fixture()
        run["result"]["coverage_audit"].update(run_id="other", source_complete=True)
        plan = explain_frozen_run(run)
        self.assertFalse(plan["source_complete"])
        self.assertEqual(plan["based_on"], "frozen_inventory_and_ledger")

    def test_explanation_endpoint_is_read_only_and_mine_scoped(self):
        store = Store(":memory:")
        try:
            task = store.accept({"mineId": "m1", "checkCycle": "2026-10", "executionId": "review", "items": [{"itemCode": "stop_work_unload"}]}, {})
            rid = task["runs"][0]["run_id"]
            claimed = store.claim("owner", run_id=rid)
            run = self.fixture()
            snapshot = {**run["snapshot"], "run_id": rid, "mine_id": "m1", "snapshot_id": "snap"}
            result = {**run["result"], "run_id": rid}
            result["coverage_audit"] = {**result["coverage_audit"], "run_id": rid, "rule_revision": None}
            store.save_snapshot(rid, "owner", claimed["fence"], snapshot)
            store.finalize(rid, "owner", claimed["fence"], result)
            before = store.get_run(rid)["result"]
            client = TestClient(create_app(store=store, settings=Settings()))
            response = client.get(f"/v1/runs/{rid}/explanation")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("工作日报.pdf", response.text)
            self.assertEqual(store.get_run(rid)["result"], before)
            self.assertEqual(client.get(f"/v1/runs/{rid}/explanation?mine_id=other").status_code, 404)
        finally:
            store.engine.dispose()


if __name__ == "__main__":
    unittest.main()
