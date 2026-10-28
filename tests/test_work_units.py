"""Persistent work-unit ownership, duplicate claims and crash recovery."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
from threading import Barrier
import unittest

from apps.control_api.store import RunPaused, StaleLease, Store


class WorkUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = [1000.0]
        self.store = Store(Path(self.temp.name) / "ledger.sqlite3", clock=lambda: self.clock[0])
        self.request = {"mineId": "test-mine", "checkCycle": "2026-10", "executionId": "work-unit-test",
                        "items": [{"itemCode": "test-only"}]}
        self.task = self.store.accept(self.request, {"test-only": {"planRevision": "frozen-plan"}})
        self.run_id = self.task["runs"][0]["run_id"]
        self.claim = self.store.claim("worker-a", lease_seconds=10, run_id=self.run_id)
        self.fence = self.claim["fence"]
        self.inputs = {"file_id": "f1", "content_hash": "hash-1", "parser_revision": "parser-v1"}

    def tearDown(self):
        self.store.engine.dispose()
        self.temp.cleanup()

    def enqueue(self, inputs=None):
        return self.store.enqueue_work_unit(self.run_id, "worker-a", self.fence, "parse_file", inputs or self.inputs)

    def test_success_is_persisted_and_reused_without_reclaim(self):
        unit = self.enqueue()
        claimed = self.store.claim_work_unit(self.run_id, "worker-a", self.fence, unit["work_unit_id"])
        self.assertEqual((claimed["state"], claimed["attempt"]), ("running", 1))
        output = {"file_id": "f1", "evidence": [{"evidence_id": "ev1"}], "coverage": [{"unit_id": "page:1", "state": "readable"}]}
        completed = self.store.finish_work_unit(self.run_id, "worker-a", self.fence, unit["work_unit_id"], output=output)
        self.assertEqual(completed["state"], "succeeded")
        reused = self.enqueue()
        self.assertEqual(reused["work_unit_id"], unit["work_unit_id"])
        self.assertEqual(reused["output"], output)
        self.assertIsNone(self.store.claim_work_unit(self.run_id, "worker-a", self.fence, unit["work_unit_id"]))
        self.assertEqual(len(self.store.list_work_units(self.run_id)), 1)
        # A separate engine reads the committed result, not an in-memory cache.
        reopened = Store(Path(self.temp.name) / "ledger.sqlite3", clock=lambda: self.clock[0])
        try:
            self.assertEqual(reopened.list_work_units(self.run_id)[0]["output"], output)
        finally:
            reopened.engine.dispose()

    def test_parallel_duplicate_claim_has_exactly_one_winner(self):
        # Exercise real independent sessions against a file-backed SQLite DB.
        for page in range(3):
            unit = self.enqueue({**self.inputs, "page": page + 1})
            barrier = Barrier(6)

            def claim(_):
                barrier.wait(timeout=5)
                return self.store.claim_work_unit(self.run_id, "worker-a", self.fence, unit["work_unit_id"])

            with ThreadPoolExecutor(max_workers=6) as executor:
                results = list(executor.map(claim, range(6)))
            self.assertEqual(sum(result is not None for result in results), 1, results)
            current = next(row for row in self.store.list_work_units(self.run_id) if row["work_unit_id"] == unit["work_unit_id"])
            self.assertEqual(current["attempt"], 1)

    def test_failed_work_retries_only_until_attempt_limit(self):
        unit = self.enqueue()
        for attempt in range(1, 4):
            claimed = self.store.claim_work_unit(self.run_id, "worker-a", self.fence, unit["work_unit_id"], max_attempts=3)
            self.assertEqual(claimed["attempt"], attempt)
            self.assertIsNone(claimed["error"])
            failed = self.store.finish_work_unit(self.run_id, "worker-a", self.fence, unit["work_unit_id"], error="temporary_provider_failure")
            self.assertEqual((failed["state"], failed["error"]), ("failed", "temporary_provider_failure"))
        self.assertIsNone(self.store.claim_work_unit(self.run_id, "worker-a", self.fence, unit["work_unit_id"], max_attempts=3))
        self.assertEqual(self.store.list_work_units(self.run_id)[0]["attempt"], 3)

    def test_new_run_fence_recovers_old_running_work_and_rejects_old_commit(self):
        unit = self.enqueue()
        self.store.claim_work_unit(self.run_id, "worker-a", self.fence, unit["work_unit_id"])
        self.clock[0] += 11
        recovered = self.store.claim("worker-b", lease_seconds=20, run_id=self.run_id)
        self.assertEqual(recovered["fence"], self.fence + 1)
        work = self.store.claim_work_unit(self.run_id, "worker-b", recovered["fence"], unit["work_unit_id"])
        self.assertEqual((work["state"], work["attempt"], work["fence"]), ("running", 2, recovered["fence"]))
        with self.assertRaises(StaleLease):
            self.store.finish_work_unit(self.run_id, "worker-a", self.fence, unit["work_unit_id"], output={"late": True})
        result = self.store.finish_work_unit(self.run_id, "worker-b", recovered["fence"], unit["work_unit_id"], output={"recovered": True})
        self.assertEqual(result["output"], {"recovered": True})

    def test_pause_rejects_enqueue_claim_and_commit(self):
        queued = self.enqueue()
        running = self.enqueue({**self.inputs, "page": 2})
        self.store.claim_work_unit(self.run_id, "worker-a", self.fence, running["work_unit_id"])
        self.store.control(self.task["taskId"], "pause")
        with self.assertRaises(RunPaused):
            self.enqueue({**self.inputs, "page": 3})
        with self.assertRaises(RunPaused):
            self.store.claim_work_unit(self.run_id, "worker-a", self.fence, queued["work_unit_id"])
        with self.assertRaises(RunPaused):
            self.store.finish_work_unit(self.run_id, "worker-a", self.fence, running["work_unit_id"], output={"late": True})
        self.store.pause_run(self.run_id, "worker-a", self.fence)
        self.store.control(self.task["taskId"], "resume")
        recovered = self.store.claim("worker-b", lease_seconds=20, run_id=self.run_id)
        reclaimed = self.store.claim_work_unit(self.run_id, "worker-b", recovered["fence"], running["work_unit_id"])
        self.assertEqual(reclaimed["attempt"], 2)

    def test_changed_input_and_operation_receive_different_stable_ids(self):
        first = self.enqueue()
        same = self.enqueue(dict(reversed(list(self.inputs.items()))))
        changed = self.enqueue({**self.inputs, "content_hash": "hash-2"})
        other_kind = self.store.enqueue_work_unit(self.run_id, "worker-a", self.fence, "recognize_page", self.inputs)
        self.assertEqual(first["work_unit_id"], same["work_unit_id"])
        self.assertEqual(len({first["work_unit_id"], changed["work_unit_id"], other_kind["work_unit_id"]}), 3)
        self.assertEqual(len(self.store.list_work_units(self.run_id)), 3)


if __name__ == "__main__":
    unittest.main()
