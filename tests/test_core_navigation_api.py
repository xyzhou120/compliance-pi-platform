import hashlib
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from apps.control_api.main import create_app
from apps.control_api.registry import default_registry
from apps.control_api.settings import Settings
from apps.control_api.store import Store


class CoreNavigationApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(storage_dir=self.root, internal_token="internal")
        self.store = Store(self.root / "ledger.db")
        self.client = TestClient(create_app(store=self.store, settings=self.settings))
        self.runs = [self.seed("m1", "first"), self.seed("m1", "second"), self.seed("m2", "other")]

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def seed(self, mine, execution):
        plan = default_registry().compile("stop_work_unload")
        task = self.store.accept({"mineId": mine, "checkCycle": "2026-10", "executionId": execution,
                                  "items": [{"itemCode": "stop_work_unload"}]}, {"stop_work_unload": plan})
        rid = task["runs"][0]["run_id"]
        run = self.store.claim("owner", run_id=rid)
        text = "原文：已撤人，引用仅供合成平台测试。"
        digest = hashlib.sha256(text.encode()).hexdigest()
        snapshot = {"run_id": rid, "mine_id": mine, "snapshot_id": rid + "-snapshot", "manifest_hash": "manifest",
                    "source_complete": False, "files": [{"file_id": "f1", "content_hash": digest}],
                    "evidence": [{"evidence_id": "ev1", "file_id": "f1", "run_id": rid, "mine_id": mine, "content_hash": digest,
                                  "source_role": "actual_record", "text": text, "locator": {"page": 1}}],
                    "coverage": [{"file_id": "f1", "unit_id": "page:1", "state": "readable"}], "coverage_gaps": []}
        self.store.save_snapshot(rid, "owner", run["fence"], snapshot)
        self.store.save_candidates(rid, "owner", run["fence"], [{"candidate_id": "c1", "candidate": {"candidate_id": "c1", "fields": {"action": "evacuate"}, "field_evidence": {"fields.action": [{"evidence_id": "ev1"}]}}}])
        result = {"run_id": rid, "status": "pending", "description": "合成平台测试", "detail": "尚缺覆盖证明",
                  "coverage_gaps": ["scope_proof_missing"], "evidence_refs": ["ev1"],
                  "propositions": [{"code": "evacuate", "state": "true", "event_id": "e1", "evidence_refs": ["ev1"]}]}
        self.store.finalize(rid, "owner", run["fence"], result)
        return rid

    def test_question_returns_registered_citations_without_changing_result(self):
        rid = self.runs[0]
        response = self.client.post(f"/v1/runs/{rid}/questions", json={"question": "撤人命题是什么", "proposition_id": "evacuate"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["evidence_refs"], ["ev1"])
        self.assertFalse(response.json()["business_status_generated"])
        self.assertEqual(self.store.get_run(rid)["result"]["status"], "pending")

    def test_compare_rejects_cross_mine(self):
        self.assertEqual(self.client.get(f"/v1/runs/{self.runs[0]}/compare/{self.runs[1]}").status_code, 200)
        self.assertEqual(self.client.get(f"/v1/runs/{self.runs[0]}/compare/{self.runs[2]}").status_code, 400)

    def test_review_is_immutable_and_cannot_change_verdict(self):
        url = f"/v1/runs/{self.runs[0]}/reviews"
        value = {"review_id": "review-1", "candidate_id": "c1", "fields": {"action": ["ev1"]}, "comment": "已核对原文"}
        self.assertEqual(self.client.post(url, json=value).status_code, 200)
        self.assertEqual(self.client.post(url, json=value).status_code, 200)
        self.assertEqual(len(self.client.get(url).json()["items"]), 1)
        self.assertEqual(self.client.post(url, json={**value, "comment": "覆盖历史"}).status_code, 400)
        self.assertEqual(self.client.post(url, json={**value, "status": "pass"}).status_code, 400)
        self.assertEqual(self.store.get_run(self.runs[0])["result"]["status"], "pending")

    def test_missing_question_audit_and_units(self):
        rid = self.runs[0]
        self.assertEqual(self.client.post(f"/v1/runs/{rid}/questions", json={"question": ""}).status_code, 400)
        self.assertFalse(self.client.get(f"/v1/runs/{rid}/audit").json()["source_complete"])
        self.assertEqual(self.client.get(f"/v1/runs/{rid}/work-units").json(), {"items": []})

    def test_rule_replay_only_previews_and_does_not_create_callback(self):
        rid = self.runs[0]
        before = self.store.get_run(rid)["result"]
        value = self.client.post("/v1/rules/stop_work_unload/replay", json={"run_ids": [rid]})
        self.assertEqual(value.status_code, 200, value.text)
        self.assertTrue(value.json()["preview_only"])
        self.assertFalse(value.json()["history_modified"])
        self.assertEqual(self.store.get_run(rid)["result"], before)
        self.assertEqual(self.store.get_run(rid)["delivery"]["state"], "none")
