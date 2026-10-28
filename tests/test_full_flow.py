"""Real Pi SDK + HTTP API + control worker + callback, local fake model only."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import uvicorn

from apps.control_api.main import create_app
from apps.control_api.planner import compile_plan
from apps.control_api.settings import Settings
from apps.control_api.store import Store
from apps.control_api.worker import deliver_once, run_once

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "事件编号:E1;工作面:W1;时间:2026-10-01T08:00:00;动作:撤人;状态:已完成;执行人角色:operator;目标范围:W1;路线符合:是;立即:是"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(shutil.which("node") and (ROOT / "apps/pi-worker/node_modules/@earendil-works/pi-agent-core").exists(), "npm ci required for real Pi integration")
class FullFlowTests(unittest.TestCase):
    def test_real_pi_tools_source_validation_finalization_and_upstream_callback(self):
        callbacks, calls = [], []

        class ModelHandler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                if self.path == "/callback":
                    callbacks.append((self.headers.get("Idempotency-Key"), body))
                    content = b'{"code":200,"success":true}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                    return
                calls.append(body)
                tools = [m for m in body["messages"] if m.get("role") == "tool"]
                if not tools:
                    requested = [("inspect_documents", {"file_ids": ["f1"]})]
                elif not any("search_evidence" in str(m) for m in body["messages"] if m.get("role") == "assistant"):
                    requested = [("search_evidence", {"query": "", "limit": 5}), ("get_coverage", {})]
                elif not any("submit_candidates" in str(m) for m in body["messages"] if m.get("role") == "assistant"):
                    found = None
                    for tool in tools:
                        text = tool.get("content", "")
                        if isinstance(text, str):
                            try:
                                candidates = json.loads(text).get("items", [])
                                found = next((item for item in candidates if isinstance(item, dict) and item.get("evidence_id")), None) or found
                            except (ValueError, IndexError, AttributeError):
                                pass
                    if not found:
                        raise AssertionError("Pi never obtained registered evidence")
                    fields = {"event_id": "E1", "workface": "W1", "time": "2026-10-01T08:00:00", "action": "evacuate", "status": "done"}
                    candidate = {"candidate_id": "full-flow-c1", "proposition_id": "evacuation_executed", "kind": "action", "fields": fields,
                                 "evidence_refs": [found["evidence_id"]], "uncertainty": [],
                                 "field_evidence": {f"fields.{key}": [{"evidence_id": found["evidence_id"], "quote": SOURCE}] for key in fields}}
                    requested = [("submit_candidates", {"candidates": [candidate]})]
                else:
                    requested = []
                delta = {"role": "assistant", "tool_calls": [{"index": i, "id": f"call-{len(calls)}-{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}} for i, (name, args) in enumerate(requested)]} if requested else {"role": "assistant", "content": "Evidence collection complete; unresolved trigger remains a gap."}
                chunk = {"id": f"model-{len(calls)}", "object": "chat.completion.chunk", "created": 1, "model": "local-test-qwen", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                final = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if requested else "stop"}]}
                content = ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\ndata: " + json.dumps(final) + "\n\ndata: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, *_):
                pass

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "record.txt").write_text(SOURCE, encoding="utf-8")
            (root / "manifest.json").write_text(json.dumps({"mine_id": "m1", "source_complete": False, "files": [{"path": "record.txt", "file_id": "f1", "role": "actual_record"}]}), encoding="utf-8")
            config_path = root / "sources.json"
            config_path.write_text(json.dumps({"manifests": {"m1": {"2026-10": "manifest.json"}}}), encoding="utf-8")
            model = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
            model_thread = threading.Thread(target=model.serve_forever, daemon=True)
            model_thread.start()
            api_port, pi_port = free_port(), free_port()
            settings = Settings(input_root=root, storage_dir=root / "artifacts", source_config=config_path, internal_token="full-flow-internal", allowed_mines=("m1",),
                                pi_enabled=True, pi_worker_url=f"http://127.0.0.1:{pi_port}", task_timeout=15, callback_hosts=("127.0.0.1",), callback_allow_http=True, callback_allow_loopback=True)
            store = Store(root / "ledger.sqlite3")
            api = uvicorn.Server(uvicorn.Config(create_app(store=store, settings=settings), host="127.0.0.1", port=api_port, log_level="error"))
            api_thread = threading.Thread(target=api.run, daemon=True)
            api_thread.start()
            env = {**os.environ, "PI_PORT": str(pi_port), "PI_HOST": "127.0.0.1", "INTERNAL_TOKEN": settings.internal_token,
                   "EVIDENCE_API_URL": f"http://127.0.0.1:{api_port}", "PI_MODEL_BASE_URL": f"http://127.0.0.1:{model.server_port}/v1", "PI_MODEL_ID": "local-test-qwen", "PI_MODEL_API_KEY": "local-test"}
            node = subprocess.Popen([shutil.which("node"), str(ROOT / "apps/pi-worker/src/server.mjs")], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                with httpx.Client(trust_env=False, timeout=3) as client:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        try:
                            if client.get(settings.pi_worker_url + "/health").status_code == 200 and api.started:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(.05)
                    else:
                        self.fail("local test services did not start")
                    response = client.post(f"http://127.0.0.1:{api_port}/inspection/tasks", json={"mineId": "m1", "checkCycle": "2026-10", "executionId": "full-flow", "items": [{"itemCode": "stop_work_unload"}], "resultCallbackUrl": f"http://127.0.0.1:{model.server_port}/callback"})
                    self.assertEqual(response.status_code, 200, response.text)
                    task_id = response.headers["X-Inspection-Task"]
                    run_id = store.get_task(task_id)["runs"][0]["run_id"]
                    result = run_once(store, settings, run_id=run_id)
                    self.assertEqual(result["status"], "pending", result)
                    stored = store.get_run(run_id)
                    self.assertEqual(len(stored["candidates"]), 1, stored["gaps"])
                    self.assertEqual(stored["candidates"][0]["validation_status"], "source_bound")
                    self.assertEqual(len(calls), 4)
                    self.assertEqual(len(store.list_work_units(run_id)), 1)
                    self.assertEqual(store.list_work_units(run_id)[0]["state"], "succeeded")
                    self.assertTrue(any(event.get("stage") == "pi" and event.get("event") == "finished" for event in store.events(run_id)))
                    deliver_once(store, settings)
                    self.assertEqual(store.get_run(run_id)["delivery"]["state"], "delivered")
                    self.assertEqual(callbacks[0][1]["mineId"], "m1")
                    self.assertEqual(callbacks[0][1]["checkResult"], "pending")
                    self.assertTrue(callbacks[0][0].startswith("delivery-"))
            finally:
                node.terminate()
                try:
                    node.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    node.kill()
                    node.wait(timeout=3)
                if node.stderr:
                    node.stderr.close()
                api.should_exit = True
                api_thread.join(timeout=5)
                model.shutdown()
                model.server_close()
                model_thread.join(timeout=2)
                store.engine.dispose()
