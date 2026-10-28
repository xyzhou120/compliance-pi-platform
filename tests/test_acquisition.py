import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from apps.control_api.acquisition import AcquisitionError, download_attachment, query_oceanbase


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"%PDF-test")

    def log_message(self, *args):
        pass


class AcquisitionTests(unittest.TestCase):
    def test_download_validates_hash_magic_and_refuses_redirect(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                url = f"http://127.0.0.1:{server.server_port}"
                target = Path(tmp) / "source.pdf"
                result = download_attachment(url + "/ok", target, allowed_hosts=("127.0.0.1",), allow_http=True, allow_loopback=True)
                self.assertEqual(result["byte_size"], 9)
                with self.assertRaises(AcquisitionError):
                    download_attachment(url + "/redirect", target, allowed_hosts=("127.0.0.1",), allow_http=True, allow_loopback=True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_query_uses_placeholders_and_keeps_completeness_false(self):
        seen = {}
        class Cursor:
            description = [(name,) for name in ("mine_code", "mine_name", "report_time", "update_rate", "cycle", "file_name_id", "file_name", "attachment_id", "attachment_name", "attachment_link", "suffix")]
            def execute(self, sql, params): seen.update(sql=sql, params=params)
            def fetchall(self): return [("m1", "矿井", "2026-01-01", "annual", "annual", "annual", "年度资料", "a1", "design.pdf", "https://172.26.10.67:8082/bucket/design.pdf", "pdf")]
            def close(self): pass
        class Connection:
            def cursor(self): return Cursor()
            def close(self): pass
        result = query_oceanbase(mine_id="m1' OR 1=1", check_cycle="2026-10", item_code="stop_work_unload", config={"table":"readonly_files", "catalog":{"stop_work_unload":[{"file_name_id":"annual", "cadence":"annual"}]}}, connection_factory=Connection)
        self.assertNotIn("m1'", seen["sql"])
        self.assertEqual(seen["params"][1], "m1' OR 1=1")
        self.assertIn("CONCAT(%s,local_bucket_name,'/',local_attachment_address) AS attachment_link", seen["sql"])
        self.assertIn("SUBSTRING_INDEX(local_attachment_address,'.',-1) AS suffix", seen["sql"])
        self.assertFalse(result["source_complete"])

    def test_monthly_records_are_bounded_in_sql_and_requirements_keep_versions(self):
        seen = {}
        class Cursor:
            description = [(name,) for name in ("mine_code", "mine_name", "report_time", "update_rate", "cycle", "file_name_id", "file_name", "attachment_id", "attachment_name", "attachment_link", "suffix")]
            def execute(self, sql, params): seen.update(sql=sql, params=params)
            def fetchall(self): return []
            def close(self): pass
        class Connection:
            def cursor(self): return Cursor()
            def close(self): pass
        config = {"table":"readonly_files", "catalog":{"stop_work_unload":[{"file_name_id":"daily", "cadence":"daily", "role":"actual_record"}, {"file_name_id":"design", "cadence":"annual", "role":"requirement"}]}, "max_query_rows":100}
        query_oceanbase(mine_id="m1", check_cycle="2026-10", item_code="stop_work_unload", config=config, connection_factory=Connection)
        self.assertIn("report_time_year = %s", seen["sql"])
        self.assertIn("report_time_month = %s", seen["sql"])
        self.assertIn("LIMIT 101", seen["sql"])
        self.assertEqual(seen["params"], ["https://172.26.10.67:8082/", "m1", "daily", "design", "daily", 2026, "10", "design", 2026])

    def test_explicit_attachment_projection_is_mapped(self):
        class Cursor:
            description = [(name,) for name in ("mine_code", "mine_name", "report_time", "update_rate", "cycle", "file_name_id", "file_name", "attachment_id", "attachment_name", "attachment_link", "suffix")]
            def execute(self, sql, params): self.sql, self.params = sql, params
            def fetchall(self): return [("m1", "矿井", "2026-10-01", "daily", "daily", "daily", "日报", "a1", "daily.pdf", "https://172.26.10.67:8082/bucket/daily.pdf", "pdf")]
            def close(self): pass
        class Connection:
            def cursor(self): return Cursor()
            def close(self): pass
        result = query_oceanbase(mine_id="m1", check_cycle="2026-10", item_code="stop_work_unload", config={"table":"mine_remote_disaster_file", "catalog":{"stop_work_unload":[{"file_name_id":"daily", "cadence":"daily", "source_role":"actual_record"}]}}, connection_factory=Connection)
        self.assertEqual(result["rows"][0]["attachment_link"], "https://172.26.10.67:8082/bucket/daily.pdf")
        self.assertEqual(result["rows"][0]["suffix"], "pdf")


if __name__ == "__main__":
    unittest.main()
