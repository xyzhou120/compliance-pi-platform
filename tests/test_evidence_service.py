import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from apps.control_api.documents import acquire_run, render_pages
from apps.control_api.evidence import EvidenceService
from apps.control_api.planner import compile_plan
from apps.control_api.settings import Settings
from apps.control_api.store import StaleLease, Store
from apps.control_api.worker import normalize_run


class EvidenceServiceTests(unittest.TestCase):
    def test_pdf_render_external_ocr_stays_candidate_and_stale_fence_cannot_write(self):
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            writer.write(root / "scan.pdf")
            (root / "manifest.json").write_text(json.dumps({"files": [{"path": "scan.pdf", "file_id": "f1", "role": "actual_record"}]}), encoding="utf-8")
            class Handler(BaseHTTPRequestHandler):
                def do_POST(self):
                    data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    self.server.calls.append(data)
                    response = json.dumps({"text": "模型转录候选", "blocks": []}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)
                def log_message(self, *_):
                    pass
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            server.calls = []
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            store = Store(root / "ledger.db")
            try:
                plan = compile_plan("stop_work_unload")
                task = store.accept({"mineId": "m1", "checkCycle": "2026-10", "items": [{"itemCode": "stop_work_unload"}]}, {"stop_work_unload": plan})
                run = normalize_run(store.claim("worker", run_id=task["runs"][0]["run_id"]))
                run["scope"] = {"manifest": str(root / "manifest.json")}
                snapshot = acquire_run(run, {"input_root": str(root), "storage_dir": str(root / "artifacts")})
                for entry in snapshot["evidence"]:
                    entry.update(snapshot_id=snapshot["snapshot_id"], source_system=run["source_system"], mine_code=run["mine_id"])
                store.save_snapshot(run["run_id"], "worker", run["fence"], snapshot)
                images = render_pages(snapshot, [{"file_id": "f1", "page": 1}])
                self.assertEqual(images["errors"], [])
                self.assertTrue(images["images"][0]["image_bytes"].startswith(b"\x89PNG"))
                settings = Settings(input_root=root, storage_dir=root / "artifacts", ocr_endpoint=f"http://127.0.0.1:{server.server_port}/ocr")
                service = EvidenceService(store, settings)
                result = service.recognize(run["run_id"], {"pages": [{"file_id": "f1", "page": 1}]}, worker_id="worker", fence=run["fence"])
                self.assertEqual(result["quality"], "model_candidate")
                record = result["evidence"][0]
                self.assertEqual(record["source_role"], "statement")
                self.assertFalse(service.coverage(run["run_id"])["source_complete"])
                self.assertEqual(service.search(run["run_id"], "转录")["total"], 1)
                service.recognize(run["run_id"], {"pages": [{"file_id": "f1", "page": 1}]}, worker_id="worker", fence=run["fence"])
                self.assertEqual(len(server.calls), 1, "successful OCR must be cached")
                with self.assertRaises(StaleLease):
                    service.recognize(run["run_id"], {"pages": [{"file_id": "f1", "page": 1}]}, worker_id="other", fence=run["fence"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                store.engine.dispose()

    def test_same_byte_attachments_do_not_create_duplicate_independent_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "one.txt").write_text("原文事实", encoding="utf-8")
            (root / "two.txt").write_text("原文事实", encoding="utf-8")
            (root / "m.json").write_text(json.dumps({"files": [{"path": name, "file_id": fid, "role": "actual_record", "report_date": "2026-10-01", "material_group": "prevention_daily"} for name, fid in [("one.txt", "a"), ("two.txt", "b")]]}), encoding="utf-8")
            result = acquire_run({"run_id": "r1", "mine_id": "m1", "rule_revision": "v1", "scope": {"manifest": str(root / "m.json")}}, {"input_root": str(root), "storage_dir": str(root / "store")})
            self.assertEqual(len(result["files"]), 2)
            self.assertEqual(len(result["evidence"]), 1)
            self.assertEqual(result["evidence"][0]["source_file_ids"], ["a", "b"])
            self.assertEqual(result["files"][0]["report_date"], "2026-10-01")
