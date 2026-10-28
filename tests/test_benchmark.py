from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from apps.control_api.settings import Settings
from scripts.benchmark import cache_settings, safe_profile, summarize_run, summarize_series


class FakeStore:
    def __init__(self, run, events):
        self.run, self.recorded = run, events
    def get_run(self, _):
        return deepcopy(self.run)
    def events(self, _, *, after=0, limit=500):
        return deepcopy([e for e in self.recorded if e["sequence"] > after][:limit])


def fixture():
    run = {"run_id": "r1", "mine_id": "m1", "item_code": "sample", "check_cycle": "2026-10",
           "plan": {"planRevision": "p1"}, "request": {"executionId": "e1"},
           "snapshot": {"files": [{"file_id": "f1", "content_hash": "h1", "path": "private-path"}],
                        "evidence": [{"evidence_id": "ev1"}], "source_complete": False, "coverage": [{"state": "not_searched"}]},
           "result": {"status": "pending", "coverage_gaps": ["missing"]}, "candidates": [],
           "work_units": [{"state": "succeeded", "attempt": 2}], "traces": [{"phase": "finished", "elapsed_ms": 12}]}
    events = [{"sequence": 1, "stage": "pi", "event": "finished", "tool_calls": 2, "model_turns": 2,
               "usage": {"input": 100, "output": 20, "cacheRead": 5}, "modelQueueMs": 8,
               "modelAttempts": [{"attempt": 1}, {"attempt": 2}], "toolAttempts": [{"status": "ok", "elapsed_ms": 10}]},
              {"sequence": 2, "stage": "recognition", "attempts": [
                  {"status": "retry", "attempt": 1, "elapsed_ms": 2}, {"status": "ok", "attempt": 2, "elapsed_ms": 3, "queue_ms": 1}, {"status": "cached", "attempt": 0, "queue_ms": 0}]},
              {"sequence": 3, "stage": "work_unit", "event": "reused"}]
    return run, events


class BenchmarkTests(unittest.TestCase):
    def test_persisted_model_provider_and_work_unit_metrics_are_summarized(self):
        run, events = fixture()
        report = summarize_run(FakeStore(run, events), "r1", 1.23456, 123456)
        self.assertEqual(report["model"]["input_tokens"], 100)
        self.assertEqual(report["model"]["cache_read_tokens"], 5)
        self.assertEqual(report["model"]["retries"], 1)
        self.assertEqual(report["providers"]["requests"], 2)
        self.assertEqual(report["providers"]["cache_hits"], 1)
        self.assertEqual(report["work_units"]["reused"], 1)
        self.assertEqual(report["peak_memory_bytes"], 123456)
        self.assertNotIn("private-path", str(report))

    def test_unavailable_telemetry_stays_null(self):
        report = summarize_run(FakeStore({"snapshot": {}, "result": {}}, []), "r1", 1)
        self.assertIsNone(report["model"]["input_tokens"])
        self.assertIsNone(report["model"]["calls"])
        self.assertIsNone(report["providers"]["requests"])
        self.assertIsNone(report["work_units"])
        self.assertIsNone(report["peak_memory_bytes"])
        self.assertIsNone(report["source_complete"])

    def test_event_pagination_collects_all_measured_attempts(self):
        run, _ = fixture()
        events = [{"sequence": i + 1, "stage": "pi", "event": "finished", "usage": {"input": 1}} for i in range(501)]
        self.assertEqual(summarize_run(FakeStore(run, events), "r1", 2)["model"]["input_tokens"], 501)

    def test_configuration_never_exports_credentials_queries_or_full_paths(self):
        settings = Settings(ocr_endpoint="http://user:secret@ocr.local:123/private/api-key?token=secret#secret")
        profile = safe_profile(settings, environment={"PI_MODEL_BASE_URL": "http://secret:secret@model.local/v1?key=secret",
                                                    "PI_MODEL_ID": "test-model", "PI_MODEL_API_KEY": "secret"})
        self.assertEqual(profile["ocr_endpoint"]["host"], "ocr.local")
        self.assertNotIn("secret", str(profile))
        self.assertNotIn("private", str(profile))

    def test_cold_cache_requires_explicit_root_and_never_deletes_existing_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            existing = root / "existing"
            existing.mkdir()
            marker = existing / "keep.txt"
            marker.write_text("keep")
            settings = Settings(storage_dir=existing)
            with self.assertRaises(ValueError):
                cache_settings(settings, "cold", None, "batch", 0)
            first = cache_settings(settings, "cold", existing / "benchmark", "batch", 0)
            second = cache_settings(settings, "cold", existing / "benchmark", "batch", 0)
            self.assertNotEqual(first.storage_dir, second.storage_dir)
            self.assertEqual(marker.read_text(), "keep")
            self.assertEqual(settings.storage_dir, existing)

    def test_warm_cache_is_explicitly_reused_in_one_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(storage_dir=Path(temp))
            first = cache_settings(settings, "warm", Path(temp), "batch", 0)
            second = cache_settings(settings, "warm", Path(temp), "batch", 1)
            self.assertEqual(first.storage_dir, second.storage_dir)

    def test_cache_root_cannot_escape_the_api_storage_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(storage_dir=Path(temp) / "storage")
            with self.assertRaises(ValueError):
                cache_settings(settings, "cold", Path(temp) / "elsewhere", "batch", 0)

    def test_partial_usage_counter_is_not_reported_as_complete_total(self):
        run, events = fixture()
        events.append({"sequence": 4, "stage": "pi", "event": "finished", "usage": {"output": 1}})
        report = summarize_run(FakeStore(run, events), "r1", 2)
        self.assertIsNone(report["model"]["input_tokens"])
        self.assertEqual(report["model"]["output_tokens"], 21)

    def test_series_percentiles_require_explicit_comparable_inputs(self):
        run, events = fixture()
        report = summarize_run(FakeStore(run, events), "r1", 10)
        report["configuration_fingerprint"] = "cfg"
        other = {**report, "elapsed_seconds": 30}
        series = summarize_series([report, other])
        self.assertTrue(series["comparable"])
        self.assertEqual(series["elapsed_p50_seconds"], 20)
        self.assertEqual(series["elapsed_p95_seconds"], 29)
        other["rule_revision"] = "p2"
        self.assertFalse(summarize_series([report, other])["comparable"])

    def test_missing_hash_does_not_claim_comparability(self):
        run, events = fixture()
        run["snapshot"]["files"][0].pop("content_hash")
        report = summarize_run(FakeStore(run, events), "r1", 10)
        report["configuration_fingerprint"] = "cfg"
        self.assertFalse(summarize_series([report])["comparable"])


if __name__ == "__main__":
    unittest.main()
