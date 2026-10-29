import json
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from apps.control_api.documents import parse_frozen_file
from apps.control_api import native_parser
from apps.control_api.native_parser import parse_isolated


class NativeParserTests(unittest.TestCase):
    def test_child_crash_is_file_scoped_and_safe(self):
        with patch.object(native_parser, "_command", return_value=[sys.executable, "-c", "import os; os._exit(18)"]):
            result = parse_isolated(Path("sensitive-internal-path"), file_id="f", content_hash="h", source_role="actual_record", timeout=5)
        self.assertEqual(result, {"ok": False, "error": "native_parser_child_crashed"})
        self.assertNotIn("sensitive-internal-path", json.dumps(result))

    def test_parse_isolated_timeout_and_native_parser_process_exit_are_safe(self):
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        launched = []
        popen = subprocess.Popen
        def capture(*args, **kwargs):
            process = popen(*args, **kwargs)
            launched.append(process)
            return process
        with patch.object(native_parser, "_command", return_value=command), patch.object(native_parser.subprocess, "Popen", side_effect=capture):
            result = parse_isolated(Path("unused"), file_id="f", content_hash="h", source_role="actual_record", timeout=.15)
        self.assertEqual(result, {"ok": False, "error": "native_parser_timeout"})
        self.assertEqual(len(launched), 1)
        self.assertIsNotNone(launched[0].poll(), "parser child must be killed and reaped before slot release")

    def test_failed_isolated_parse_is_not_cached_and_parse_returns_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "source.docx"
            path.write_bytes(b"document")
            digest = sha256(path.read_bytes()).hexdigest()
            snapshot = {"run_id": "r", "mine_id": "m", "environment": "test", "rule_revision": "rev",
                        "snapshot_id": "snap", "files": [{"file_id": "f", "path": str(path), "content_hash": digest,
                        "source_role": "actual_record", "format": "docx"}]}
            with patch("apps.control_api.native_parser.parse_isolated", return_value={"ok": False, "error": "native_parser_child_crashed"}):
                result = parse_frozen_file(snapshot, "f", {"storage_dir": str(root), "isolate_native_parse": True, "parse_timeout": 1})
            self.assertEqual(result["evidence"], [])
            self.assertEqual(result["coverage"][0]["state"], "failed")
            self.assertIn("native_parser_child_crashed", result["coverage_gaps"][0])
            self.assertFalse(list((root / "native-cache").glob("*.json")) if (root / "native-cache").exists() else False)

    def test_successful_isolated_text_parse_returns_normal_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "source.txt"
            path.write_text("事件：已撤人", encoding="utf-8")
            digest = sha256(path.read_bytes()).hexdigest()
            result = parse_isolated(path, file_id="f", content_hash=digest, source_role="actual_record", timeout=20)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["parsed"]["coverage"][0]["state"], "readable")
            self.assertTrue(result["parsed"]["blocks"])

    def test_parser_child_concurrency_never_exceeds_four(self):
        lock = threading.Lock()
        active = {"current": 0, "peak": 0}
        def transport(command, **kwargs):
            payload = json.loads(kwargs["input"])
            with lock:
                active["current"] += 1
                active["peak"] = max(active["peak"], active["current"])
            try:
                time.sleep(.02)
                Path(payload["status_path"]).write_text(json.dumps({"ok": True, "parsed": {"blocks": [], "coverage": []}}), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0)
            finally:
                with lock:
                    active["current"] -= 1
        with patch.object(native_parser.subprocess, "run", side_effect=transport):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: parse_isolated(Path("unused"), file_id="f", content_hash="h", source_role="actual_record", timeout=5), range(16)))
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(active, {"current": 0, "peak": 4})


if __name__ == "__main__":
    unittest.main()
