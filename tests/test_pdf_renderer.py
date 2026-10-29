from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from apps.control_api import pdf_renderer


class PdfRendererTests(unittest.TestCase):
    def test_abrupt_child_exit_is_a_safe_page_error_and_parent_survives(self):
        command = [sys.executable, "-c", "import os; os._exit(17)"]
        with patch.object(pdf_renderer, "_command", return_value=command):
            result = pdf_renderer.render_pdf_page("unused-sensitive-path", 1, timeout=5)
        self.assertEqual(result, {"ok": False, "error": "pdf_renderer_child_crashed"})
        self.assertNotIn("unused-sensitive-path", json.dumps(result))

    def test_timeout_kills_and_reaps_child_and_releases_slot(self):
        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        processes = []
        launch = subprocess.Popen
        def tracked_launch(*args, **kwargs):
            process = launch(*args, **kwargs)
            processes.append(process)
            return process
        with patch.object(pdf_renderer, "_command", return_value=command), patch.object(pdf_renderer.subprocess, "Popen", side_effect=tracked_launch):
            result = pdf_renderer.render_pdf_page("unused", 1, timeout=.15)
        self.assertEqual(result, {"ok": False, "error": "pdf_renderer_timeout"})
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll(), "timed-out renderer must be reaped before releasing its slot")
        with patch.object(pdf_renderer, "_command", return_value=[sys.executable, "-c", "import os; os._exit(17)"]):
            self.assertEqual(pdf_renderer.render_pdf_page("unused", 1, timeout=5)["error"], "pdf_renderer_child_crashed")

    def test_render_concurrency_is_bounded_at_two_and_output_is_validated(self):
        lock = threading.Lock()
        counts = {"active": 0, "peak": 0}
        def transport(command, **kwargs):
            payload = json.loads(kwargs["input"])
            with lock:
                counts["active"] += 1
                counts["peak"] = max(counts["peak"], counts["active"])
            try:
                time.sleep(.02)
                Path(payload["output_path"]).write_bytes(b"\x89PNG\r\n\x1a\nsynthetic")
                Path(payload["status_path"]).write_text('{"ok":true}', encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, b'{"ok":true}', b'')
            finally:
                with lock:
                    counts["active"] -= 1
        with patch.object(pdf_renderer.subprocess, "run", side_effect=transport):
            with ThreadPoolExecutor(max_workers=6) as executor:
                results = list(executor.map(lambda _: pdf_renderer.render_pdf_page("unused", 1, timeout=5), range(16)))
        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(counts, {"active": 0, "peak": 2})

    def test_real_synthetic_pdf_renders_without_importing_pdfium_in_parent(self):
        from pypdf import PdfWriter
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=100, height=100)
            writer.write(path)
            prior_modules = set(sys.modules)
            rendered = pdf_renderer.render_pdf_page(path, 1)
            self.assertTrue(rendered["ok"], rendered)
            self.assertTrue(rendered["image_bytes"].startswith(b"\x89PNG"))
            self.assertFalse(any(name.startswith("pypdfium2") for name in set(sys.modules) - prior_modules))
            invalid = pdf_renderer.render_pdf_page(path, 2)
            self.assertEqual(invalid["error"], "page_out_of_range")
            budget = pdf_renderer.render_pdf_page(path, 1, max_pixels=1)
            self.assertEqual(budget["error"], "page_pixel_budget_exceeded")


if __name__ == "__main__":
    unittest.main()
