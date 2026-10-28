import json
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from apps.control_api.acquisition import AcquisitionError
from apps.control_api.sources import SourceRouteError, prepare_source


class SourceRouterTests(unittest.TestCase):
    def test_local_route_is_server_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "m.json"
            manifest.write_text("{}", encoding="utf-8")
            settings = {"input_root": str(root), "source_config": {"mode": "local_manifest", "manifests": {"mine": {"2026-10": "m.json"}}}}
            result = prepare_source({"run_id": "r", "mine_id": "mine", "check_cycle": "2026-10", "item_code": "stop_work_unload"}, settings)
            self.assertEqual(Path(result["scope"]["manifest"]), manifest.resolve())

    def test_remote_query_and_download_are_frozen_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = {"storage_dir": str(root), "source_config": {"mode": "oceanbase", "catalog": {"stop_work_unload": [{"file_name_id": "f", "source_role": "actual_record"}]}, "attachment_allowed_hosts": ["internal"]}}
            calls = {"query": 0, "download": 0}
            def query_fn(**_):
                calls["query"] += 1
                return {"rows": [{"attachment_id": "a1", "attachment_name": "a.pdf", "attachment_url": "https://internal/a.pdf", "file_name_id": "f", "report_date": "2026-10-01"}]}
            def download_fn(url, destination, **_):
                calls["download"] += 1
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"%PDF-test")
                return {"path": str(destination), "content_hash": sha256(b"%PDF-test").hexdigest(), "byte_size": 9}
            run = {"run_id": "r", "mine_id": "mine", "check_cycle": "2026-10", "item_code": "stop_work_unload", "environment": "production"}
            first = prepare_source(run, settings, query_fn=query_fn, download_fn=download_fn)
            second = prepare_source(run, settings, query_fn=query_fn, download_fn=download_fn)
            self.assertEqual(calls, {"query": 1, "download": 1})
            self.assertEqual(first["scope"], second["scope"])
            self.assertFalse(first["acquisition"]["source_complete"])

    def test_attachment_metadata_conflict_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = {"storage_dir": tmp, "source_config": {"mode": "oceanbase", "catalog": {"stop_work_unload": [{"file_name_id": "f", "source_role": "actual_record"}]}, "attachment_allowed_hosts": ["internal"]}}
            def query_fn(**_):
                return {"rows": [{"attachment_id": "same", "attachment_name": "a.pdf", "attachment_url": "https://internal/a", "file_name_id": "f"}, {"attachment_id": "same", "attachment_name": "b.pdf", "attachment_url": "https://internal/b", "file_name_id": "f"}]}
            with self.assertRaises(SourceRouteError):
                prepare_source({"run_id": "r", "mine_id": "m", "check_cycle": "2026-10", "item_code": "stop_work_unload"}, settings, query_fn=query_fn, download_fn=lambda *args, **kwargs: {})

    def test_sql_attachment_link_is_encoded_for_http_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = {"storage_dir": str(root), "source_config": {"mode": "oceanbase", "attachment_allowed_hosts": ["files.internal"], "catalog": {"stop_work_unload": [{"file_name_id": "f", "source_role": "actual_record", "material_group": "prevention_daily", "cadence": "daily"}]}}}
            def query_fn(**_):
                return {"rows": [{"attachment_id": "a1", "attachment_name": "资料.pdf", "attachment_link": "https://files.internal/bucket/资料 #1.pdf", "file_name_id": "f", "report_time": "2026-10-01"}]}
            def download(url, destination, **_):
                self.assertEqual(url, "https://files.internal/bucket/%E8%B5%84%E6%96%99%20%231.pdf")
                destination.parent.mkdir(parents=True, exist_ok=True); destination.write_bytes(b"%PDF-test")
                return {"path": str(destination), "content_hash": sha256(b"%PDF-test").hexdigest(), "byte_size": 9}
            prepare_source({"run_id": "r", "mine_id": "m", "check_cycle": "2026-10", "item_code": "stop_work_unload"}, settings, query_fn=query_fn, download_fn=download)

    def test_all_download_failures_are_recorded_and_tls_setting_is_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = {"storage_dir": tmp, "source_config": {"mode": "oceanbase", "attachment_tls_verify": False,
                        "attachment_allowed_hosts": ["files.internal"], "catalog": {"stop_work_unload": [{"file_name_id": "f"}]}}}
            def query_fn(**_):
                return {"rows": [{"attachment_id": "a1", "attachment_link": "https://files.internal/a.pdf", "file_name_id": "f"}]}
            def download(_url, _destination, **kwargs):
                self.assertFalse(kwargs["verify_tls"])
                raise AcquisitionError("attachment_tls_certificate_failed")
            with self.assertRaisesRegex(SourceRouteError, "source_attachments_unavailable"):
                prepare_source({"run_id": "r", "mine_id": "m", "check_cycle": "2026-10", "item_code": "stop_work_unload"}, settings, query_fn=query_fn, download_fn=download)
            manifest = json.loads((Path(tmp) / "acquisition" / "r" / "source" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["failures"][0]["reason"], "acquisition_failed:attachment_tls_certificate_failed")


if __name__ == "__main__":
    unittest.main()
