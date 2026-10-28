import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from apps.control_api.main import create_app
from apps.control_api.settings import Settings
from apps.control_api.store import Store


class FrontendApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.storage = self.root / "artifacts"
        self.storage.mkdir()
        self.store = Store(":memory:")
        self.settings = Settings(internal_token="internal-token", allowed_mines=("m1",), input_root=self.root, storage_dir=self.storage)
        self.client = TestClient(create_app(store=self.store, settings=self.settings))
        self.headers = {}

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def freeze(self, *, mine="m1", path=None, data=None, format="txt"):
        request = {"mineId": mine, "checkCycle": "2026-10", "executionId": "fixture-" + mine, "items": [{"itemCode": "stop_work_unload"}]}
        task = self.store.accept(request, {"stop_work_unload": {"name": "Test plan", "revision": "r1"}})
        rid = task["runs"][0]["runId"]
        run = self.store.claim("worker", run_id=rid)
        text = "工作面 W1 原文证据。" * 400
        data = data if data is not None else text.encode("utf-8")
        path = path or self.storage / ("source." + format)
        path.write_bytes(data)
        snapshot = {"run_id": rid, "mine_id": mine, "snapshot_id": "snapshot-1", "manifest_hash": "manifest-1", "source_complete": False,
                    "files": [{"file_id": "f1", "original_name": "原始资料." + format, "format": format, "content_hash": hashlib.sha256(data).hexdigest(), "path": str(path), "byte_size": len(data), "source_role": "actual_record"}],
                    "evidence": [{"evidence_id": "ev1", "file_id": "f1", "document_id": "f1", "document_version": "v1", "content_hash": hashlib.sha256(data).hexdigest(), "locator": {"page": 1}, "text": text, "quote": text, "source_role": "actual_record"}],
                    "coverage": [{"unit_id": "page:1", "file_id": "f1", "state": "partial"}], "coverage_gaps": ["source_incomplete"]}
        self.store.save_snapshot(rid, "worker", run["fence"], snapshot)
        return rid, path, data

    def test_static_workbench_and_runtime_info(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("connectionDialog", page.text)
        self.assertNotIn("accessToken", page.text)
        self.assertNotIn("X-Inspection-Token", self.client.get("/frontend/app.js").text)
        self.assertIn("Compliance Pi", self.client.get("/workbench").text)
        runtime = self.client.get("/v1/runtime", headers=self.headers)
        self.assertEqual(runtime.status_code, 200)
        self.assertNotIn("authorized_mines", runtime.json())

    def test_task_list_summary_and_plan_are_compact(self):
        response = self.client.post("/inspection/tasks", headers=self.headers, json={"mineId": "m1", "checkCycle": "2026-10", "executionId": "ui-1", "items": [{"itemCode": "stop_work_unload"}]})
        self.assertEqual(response.status_code, 200)
        task_id = response.headers["X-Inspection-Task"]
        listed = self.client.get("/inspection/tasks?offset=0&limit=10", headers=self.headers)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["items"][0]["taskId"], task_id)
        self.assertNotIn("snapshot", listed.text)
        summary = self.client.get(f"/inspection/tasks/{task_id}/summary", headers=self.headers).json()
        run_id = summary["runs"][0]["runId"]
        run = self.client.get(f"/v1/runs/{run_id}/summary", headers=self.headers).json()
        self.assertNotIn("snapshot", run)
        self.assertEqual(self.client.get(f"/v1/runs/{run_id}/plan", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get(f"/v1/runs/{run_id}/coverage", headers=self.headers).status_code, 200)

    def test_mine_isolation_and_control(self):
        response = self.client.post("/inspection/tasks", headers=self.headers, json={"mineId": "m1", "checkCycle": "2026-10", "executionId": "ui-2", "items": [{"itemCode": "stop_work_unload"}]})
        task_id = response.headers["X-Inspection-Task"]
        self.assertEqual(self.client.get(f"/inspection/tasks/{task_id}?mine_id=other", headers=self.headers).status_code, 404)
        control = self.client.post(f"/inspection/tasks/{task_id}/control", headers=self.headers, json={"action": "pause"})
        self.assertEqual(control.status_code, 200)
        self.assertEqual(control.json()["state"], "paused")

    def test_task_filtering_and_pagination_support_any_mine(self):
        for index, mine in enumerate(["m1", "m1", "m2"]):
            self.store.accept({"mineId": mine, "checkCycle": "2026-10", "executionId": f"page-{index}", "items": [{"itemCode": "stop_work_unload"}]}, {})
        first = self.client.get("/inspection/tasks?limit=1", headers=self.headers).json()
        second = self.client.get("/inspection/tasks?limit=1&offset=1", headers=self.headers).json()
        self.assertEqual(first["total"], 3)
        self.assertTrue(first["has_more"])
        self.assertNotEqual(first["items"][0]["taskId"], second["items"][0]["taskId"])
        filtered = self.client.get("/inspection/tasks?mine_id=m2", headers=self.headers)
        self.assertEqual(filtered.status_code, 200)
        self.assertEqual(filtered.json()["total"], 1)
        self.assertEqual(filtered.json()["items"][0]["mineId"], "m2")
        self.assertEqual(self.client.get("/inspection/tasks?limit=101", headers=self.headers).status_code, 422)

    def test_summary_and_file_metadata_do_not_include_source_body_or_local_path(self):
        rid, path, _ = self.freeze()
        for suffix in ("summary", "files"):
            response = self.client.get(f"/v1/runs/{rid}/{suffix}", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(str(path), response.text)
            self.assertNotIn("工作面 W1 原文证据", response.text)
            self.assertNotIn('"snapshot"', response.text)
        summary = self.client.get(f"/v1/runs/{rid}/summary", headers=self.headers).json()
        self.assertEqual(summary["file_count"], 1)
        self.assertEqual(summary["evidence_count"], 1)

    def test_public_evidence_is_bounded_but_full_record_is_available(self):
        rid, _, _ = self.freeze()
        response = self.client.get(f"/v1/runs/{rid}/evidence?query=W1", headers=self.headers)
        item = response.json()["items"][0]
        self.assertTrue(item["truncated"])
        self.assertEqual(len(item["quote"]), 1200)
        self.assertEqual(item["original_name"], "原始资料.txt")
        full = self.client.get(f"/v1/runs/{rid}/evidence/{item['evidence_id']}", headers=self.headers)
        self.assertGreater(len(full.json()["text"]), 1200)
        self.assertEqual(self.client.get(f"/v1/runs/{rid}/evidence/outside", headers=self.headers).status_code, 404)

    def test_original_file_is_public_and_rejects_changed_bytes(self):
        rid, path, data = self.freeze()
        url = f"/v1/runs/{rid}/files/f1/content"
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.get(url, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, data)
        self.assertIn("text/plain", response.headers["content-type"])
        path.write_bytes(b"tampered")
        self.assertEqual(self.client.get(url, headers=self.headers).status_code, 409)

    def test_original_file_outside_frozen_storage_is_hidden(self):
        rid, _, _ = self.freeze(path=self.root / "outside.txt")
        self.assertEqual(self.client.get(f"/v1/runs/{rid}/files/f1/content", headers=self.headers).status_code, 404)
        other, _, _ = self.freeze(mine="m2", path=self.storage / "other.txt")
        self.assertEqual(self.client.get(f"/v1/runs/{other}/summary", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get(f"/v1/runs/{other}/files/f1/content", headers=self.headers).status_code, 200)

    @unittest.skipUnless(importlib.util.find_spec("pypdfium2") and importlib.util.find_spec("PIL"), "PDF rendering extra required")
    def test_pdf_page_preview_is_png_and_hash_checked(self):
        from io import BytesIO
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        output = BytesIO()
        writer.write(output)
        rid, path, _ = self.freeze(data=output.getvalue(), format="pdf")
        url = f"/v1/runs/{rid}/files/f1/pages/1/image"
        response = self.client.get(url, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"\x89PNG"))
        path.write_bytes(b"changed")
        self.assertEqual(self.client.get(url, headers=self.headers).status_code, 409)


if __name__ == "__main__":
    unittest.main()
