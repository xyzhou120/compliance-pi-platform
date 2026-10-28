import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from apps.control_api.callbacks import DeliveryError
from apps.control_api.settings import Settings
from apps.control_api.store import StaleLease, Store
from apps.control_api.worker import _run_pi, deliver_once, resolve_manifest, run_once


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "ledger.sqlite3")
        (self.root / "record.txt").write_text("事件编号:ev1 工作面:face 时间:2026-10-12\n", encoding="utf-8")
        (self.root / "manifest.json").write_text(json.dumps({"mine_id": "m1", "files": [{"path": "record.txt", "file_id": "d1", "role": "actual_record"}]}), encoding="utf-8")
        config = self.root / "sources.json"
        config.write_text(json.dumps({"manifests": {"m1": {"2026-10": "manifest.json"}}}), encoding="utf-8")
        self.settings = Settings(input_root=self.root, storage_dir=self.root / "artifacts", source_config=config, internal_token="test-internal-token", pi_enabled=True)
        accepted = self.store.accept({"mineId": "m1", "checkCycle": "2026-10", "executionId": "ex1", "items": [{"itemCode": "stop_work_unload"}]}, {"stop_work_unload": {"revision": "asset", "planRevision": "plan"}})
        self.task_id, self.run_id = accepted["taskId"], accepted["runs"][0]["runId"]

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    @staticmethod
    def pending(_run, snapshot):
        return {"status": "pending", "description": "缺证", "detail": "必需材料未闭合", "evidence_refs": [], "coverage_gaps": snapshot.get("coverage_gaps", [])}

    def test_worker_freezes_input_calls_pi_merges_records_and_finalizes(self):
        captured = {}
        def pi(run, settings, owner, fence):
            captured.update(run)
            self.assertTrue(run["snapshot"]["manifest_hash"])
            self.assertEqual(run["rule_revision"], "plan")
            from apps.control_api.evidence import EvidenceService
            self.assertEqual(run["snapshot"]["evidence"], [])
            EvidenceService(self.store, settings).parse(run["run_id"], "d1", worker_id=owner, fence=fence)
            self.store.save_records(run["run_id"], owner, fence, "gap", [{"gap_id": "new", "reason": "model_gap"}])
            return {"toolCalls": 2, "gaps": []}
        result = run_once(self.store, self.settings, pi_client=pi, evaluator=self.pending)
        self.assertEqual(result["status"], "pending")
        self.assertIn("model_gap", result["coverage_gaps"])
        stored = self.store.get_run(self.run_id)
        self.assertEqual(stored["state"], "completed")
        evidence = stored["evidence"][0]
        self.assertEqual(evidence["snapshot_id"], stored["snapshot"]["snapshot_id"])
        self.assertEqual(evidence["mine_code"], "m1")

    def test_pi_failure_is_persisted_as_gap(self):
        def fail(*_):
            raise TimeoutError()
        result = run_once(self.store, self.settings, pi_client=fail, evaluator=self.pending)
        self.assertEqual(result["status"], "pending")
        self.assertIn("pi_worker_failed:TimeoutError", result["coverage_gaps"])

    def test_pause_does_not_publish_result(self):
        def pause(*_):
            self.store.control(self.task_id, "pause")
            return {"gaps": []}
        result = run_once(self.store, self.settings, pi_client=pause, evaluator=self.pending)
        self.assertEqual(result["execution_state"], "paused")
        self.assertIsNone(self.store.get_run(self.run_id)["result"])

    def test_stale_source_owner_cannot_publish(self):
        clock = [1000.0]
        self.store.clock = lambda: clock[0]
        def source(run, _settings):
            clock[0] += 121
            self.store.claim("replacement", run_id=self.run_id)
            return {"snapshot_id": "s1", "manifest_hash": "h1", "evidence": [], "files": []}
        with self.assertRaises(StaleLease):
            run_once(self.store, self.settings, source_loader=source, evaluator=self.pending)
        self.assertIsNone(self.store.get_run(self.run_id)["result"])

    def test_server_mapping_cannot_escape_input_root(self):
        config = self.settings.source_config
        config.write_text(json.dumps({"manifests": {"m1": {"2026-10": "../outside.json"}}}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outside_input_root"):
            resolve_manifest({"mine_id": "m1", "check_cycle": "2026-10", "item_code": "stop_work_unload"}, self.settings)

    def test_source_failure_creates_callback_outbox(self):
        accepted = self.store.accept({"mineId": "m1", "checkCycle": "2026-10", "executionId": "ex2", "items": [{"itemCode": "stop_work_unload"}], "resultCallbackUrl": "https://callback.invalid/result"}, {})
        rid = accepted["runs"][0]["runId"]
        def bad_source(*_):
            raise OSError("source not accessible")
        result = run_once(self.store, self.settings, run_id=rid, source_loader=bad_source)
        self.assertEqual(result["status"], "failed")
        delivery = self.store.claim_delivery("sender")
        self.assertEqual(delivery["payload"]["checkResult"], "failed")
        self.assertEqual(delivery["payload"]["mineId"], "m1")

    def test_heartbeat_preserves_long_source_lease(self):
        settings = replace(self.settings, lease_seconds=0.3, pi_enabled=False)
        def source(run, _settings):
            time.sleep(0.45)
            return {"snapshot_id": "s1", "manifest_hash": "h1", "evidence": [], "files": [], "coverage_gaps": []}
        result = run_once(self.store, settings, source_loader=source, evaluator=self.pending)
        self.assertEqual(result["status"], "pending")

    def test_callback_retry_keeps_payload_identity_and_stops_at_budget(self):
        run_once(self.store, self.settings, evaluator=self.pending, pi_client=lambda *_: {})
        accepted = self.store.accept({"mineId": "m1", "checkCycle": "2026-10", "executionId": "callback", "items": [{"itemCode": "stop_work_unload"}], "resultCallbackUrl": "https://callback.invalid/result"}, {})
        rid = accepted["runs"][0]["runId"]
        claimed = self.store.claim("owner", run_id=rid)
        payload = {"checkResult": "pending", "checkTime": "2026-10-28T00:00:00+00:00"}
        self.store.finalize(rid, "owner", claimed["fence"], {"status": "pending"}, payload, "https://callback.invalid/result")
        requests = []
        def fail(delivery):
            requests.append((delivery["delivery_id"], delivery["payload"]))
            raise DeliveryError("temporary", retryable=True)
        settings = replace(self.settings, callback_max_attempts=2)
        deliver_once(self.store, settings, receiver=fail)
        self.assertEqual(self.store.get_run(rid)["delivery"]["state"], "waiting")
        initial = self.store.clock()
        self.store.clock = lambda: initial + 10
        deliver_once(self.store, settings, receiver=fail)
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(self.store.get_run(rid)["delivery"]["state"], "failed")
        self.assertEqual(self.store.get_run(rid)["result"]["status"], "pending")

    def test_settings_database_alias_and_production_pi_default(self):
        env = {"COMPLIANCE_DATABASE_URL": "postgresql+psycopg://test:test@db/test", "COMPLIANCE_ENVIRONMENT": "production",
               "COMPLIANCE_INTERNAL_TOKEN": "internal-token-16-characters", "COMPLIANCE_ALLOWED_MINES": "m1"}
        with patch.dict("os.environ", env, clear=True):
            settings = Settings.from_env()
        self.assertTrue(settings.pi_enabled)
        self.assertEqual(settings.max_model_calls, 80)
        self.assertEqual(settings.database_url, env["COMPLIANCE_DATABASE_URL"])


class PiHttpTests(unittest.TestCase):
    def test_pi_http_contract_has_frozen_scope_and_owner(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, self.headers.get("X-Internal-Token"), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                body = b'{"gaps":[],"toolCalls":2}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            settings = Settings(pi_worker_url=f"http://127.0.0.1:{server.server_port}", internal_token="secret", task_timeout=2)
            result = _run_pi({"run_id": "r1", "rule_revision": "revision", "snapshot": {"manifest_hash": "mh"}, "plan": {}}, settings, "owner", 3)
            self.assertEqual(result["toolCalls"], 2)
            self.assertEqual(seen[0][0], "/v1/inspect")
            self.assertEqual(seen[0][1], "secret")
            self.assertEqual(seen[0][2]["work_owner"], "owner")
            self.assertEqual(seen[0][2]["work_fence"], 3)
            self.assertEqual(seen[0][2]["manifest_hash"], "mh")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
