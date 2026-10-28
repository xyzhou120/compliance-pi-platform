import unittest

from fastapi.testclient import TestClient

from apps.control_api.main import ApiSettings, create_app
from apps.control_api.store import Store


class ApiTests(unittest.TestCase):
    def test_upstream_envelope_and_query(self):
        client = TestClient(create_app(store=Store(":memory:"), settings=ApiSettings()))
        response = client.post("/inspection/tasks", json={"mineId": "m1", "checkCycle": "2026-10", "executionId": "e1", "items": [{"itemCode": "stop_work_unload"}]})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        task_id = response.headers["X-Inspection-Task"]
        task = client.get(f"/inspection/tasks/{task_id}")
        self.assertEqual(task.status_code, 200)

    def test_public_task_accepts_request_without_token(self):
        client = TestClient(create_app(store=Store(":memory:"), settings=ApiSettings()))
        response = client.post("/inspection/tasks", json={"mineId": "m1", "checkCycle": "2026-10", "items": [{"itemCode": "stop_work_unload"}]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(client.get("/v1/runtime").status_code, 200)

    def test_submitted_mine_is_not_rejected_by_legacy_allowlist(self):
        settings = ApiSettings(allowed_mines=("allowed",))
        client = TestClient(create_app(store=Store(":memory:"), settings=settings))
        response = client.post("/inspection/tasks", json={"mineId": "other", "checkCycle": "2026-10", "items": [{"itemCode": "stop_work_unload"}]})
        self.assertEqual(response.status_code, 200)

    def test_upstream_rejects_client_source_scope(self):
        client = TestClient(create_app(store=Store(":memory:"), settings=ApiSettings()))
        response = client.post("/inspection/tasks", json={"mineId": "m1", "checkCycle": "2026-10", "scope": {"manifest": "/etc/passwd"}, "items": [{"itemCode": "stop_work_unload"}]})
        self.assertEqual(response.status_code, 400)

    def test_mutation_requires_current_fence(self):
        store = Store(":memory:")
        accepted = store.accept({"mineId": "m1", "checkCycle": "2026-10", "items": [{"itemCode": "stop_work_unload"}]}, {})
        run_id = accepted["runs"][0]["runId"]
        claim = store.claim("worker", run_id=run_id)
        client = TestClient(create_app(store=store, settings=ApiSettings(internal_token="internal")))
        self.assertEqual(client.get(f"/internal/runs/{run_id}/plan").status_code, 401)
        response = client.post(f"/internal/runs/{run_id}/candidates", headers={"X-Internal-Token": "internal", "X-Work-Owner": "worker", "X-Work-Fence": str(claim["fence"] - 1)}, json={"candidates": []})
        self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main()
