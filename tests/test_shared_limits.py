"""Shared provider permits and mine scheduling exercised against real SQLite."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.control_api.store import ProviderPermit, StaleLease, Store


class SharedLimitsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.database = Path(self.temp.name) / "shared.sqlite3"
        self.first = Store(self.database, clock=lambda: self.now)
        self.second = Store(self.database, clock=lambda: self.now)
        self.counter = 0

    def tearDown(self):
        self.first.engine.dispose()
        self.second.engine.dispose()
        self.temp.cleanup()

    def accept(self, mine="m1"):
        self.counter += 1
        task = self.first.accept({"mineId": mine, "checkCycle": "2026-10", "executionId": f"execution-{self.counter}",
                                  "items": [{"itemCode": "synthetic-check"}]}, {})
        return task

    def claim(self, mine="m1", owner="worker", lease=100):
        task = self.accept(mine)
        run = self.first.claim(owner, lease_seconds=lease, run_id=task["runs"][0]["run_id"])
        return task, run

    def active_permits(self, resource="model"):
        with Session(self.first.engine) as db:
            return db.scalar(select(func.count()).select_from(ProviderPermit).where(
                ProviderPermit.resource == resource, ProviderPermit.lease_until > self.now))

    def test_two_store_instances_cannot_exceed_one_shared_limit(self):
        runs = [self.claim(mine=f"mine-{i}", owner=f"worker-{i}")[1] for i in range(6)]
        barrier = threading.Barrier(len(runs))

        def acquire(index):
            barrier.wait(timeout=5)
            store = self.first if index % 2 else self.second
            run = runs[index]
            return store.acquire_permit(run["run_id"], f"worker-{index}", run["fence"], "model", f"request-{index}", limit=2)

        with ThreadPoolExecutor(max_workers=len(runs)) as executor:
            permits = list(executor.map(acquire, range(len(runs))))
        self.assertEqual(sum(permit is not None for permit in permits), 2)
        self.assertEqual(self.active_permits(), 2)
        for index, permit in enumerate(permits):
            if permit:
                self.second.release_permit(runs[index]["run_id"], f"worker-{index}", runs[index]["fence"], "model", permit_id=permit)
        self.assertEqual(self.active_permits(), 0)

    def test_duplicate_request_reuses_capacity_and_release_by_request_is_idempotent(self):
        _, first = self.claim(owner="a")
        _, second = self.claim(owner="b")
        permit = self.first.acquire_permit(first["run_id"], "a", first["fence"], "model", "same-request", limit=1)
        repeated = self.second.acquire_permit(first["run_id"], "a", first["fence"], "model", "same-request", limit=1)
        self.assertEqual(permit, repeated)
        self.assertEqual(self.active_permits(), 1)
        self.assertIsNone(self.second.acquire_permit(second["run_id"], "b", second["fence"], "model", "other-request", limit=1))
        self.second.release_permit(first["run_id"], "a", first["fence"], "model", request_id="same-request")
        self.first.release_permit(first["run_id"], "a", first["fence"], "model", request_id="same-request")
        self.assertEqual(self.active_permits(), 0)
        self.assertIsNotNone(self.second.acquire_permit(second["run_id"], "b", second["fence"], "model", "other-request", limit=1))

    def test_paused_run_and_expired_run_lease_can_still_release_their_permit(self):
        task, run = self.claim(owner="owner", lease=1)
        permit = self.first.acquire_permit(run["run_id"], "owner", run["fence"], "model", "cancelled-call", limit=1, ttl=30)
        self.first.control(task["taskId"], "pause")
        self.now += 2
        with self.assertRaises(StaleLease):
            self.second.checkpoint(run["run_id"], "owner", run["fence"])
        self.second.release_permit(run["run_id"], "owner", run["fence"], "model", permit_id=permit)
        self.assertEqual(self.active_permits(), 0)

    def test_stale_owner_cannot_release_the_new_owners_permit(self):
        _, original = self.claim(owner="old", lease=1)
        old_permit = self.first.acquire_permit(original["run_id"], "old", original["fence"], "model", "call", limit=2, ttl=30)
        self.now += 2
        current = self.second.claim("new", lease_seconds=100, run_id=original["run_id"])
        self.assertGreater(current["fence"], original["fence"])
        new_permit = self.second.acquire_permit(current["run_id"], "new", current["fence"], "model", "call", limit=2, ttl=30)
        self.assertNotEqual(old_permit, new_permit)
        with self.assertRaises(StaleLease):
            self.first.release_permit(current["run_id"], "old", original["fence"], "model", permit_id=new_permit)
        self.assertEqual(self.active_permits(), 2)
        self.first.release_permit(original["run_id"], "old", original["fence"], "model", permit_id=old_permit)
        self.assertEqual(self.active_permits(), 1)
        self.second.release_permit(current["run_id"], "new", current["fence"], "model", permit_id=new_permit)
        self.assertEqual(self.active_permits(), 0)

    def test_expired_provider_ttl_reclaims_capacity_without_expiring_the_run(self):
        _, old = self.claim(owner="a")
        _, waiting = self.claim(owner="b")
        self.first.acquire_permit(old["run_id"], "a", old["fence"], "ocr", "old-call", limit=1, ttl=1)
        self.assertIsNone(self.second.acquire_permit(waiting["run_id"], "b", waiting["fence"], "ocr", "new-call", limit=1))
        self.now += 2
        self.assertIsNotNone(self.second.acquire_permit(waiting["run_id"], "b", waiting["fence"], "ocr", "new-call", limit=1))
        self.assertEqual(self.active_permits("ocr"), 1)

    def test_heartbeat_keeps_provider_capacity_held_during_long_running_task(self):
        _, active = self.claim(owner="active", lease=120)
        _, waiting = self.claim(owner="waiting", lease=120)
        self.first.acquire_permit(active["run_id"], "active", active["fence"], "model", "long-call", limit=1, ttl=45)
        for _ in range(70):
            self.now += 30
            self.first.renew(active["run_id"], "active", active["fence"], lease_seconds=120)
            self.second.renew(waiting["run_id"], "waiting", waiting["fence"], lease_seconds=120)
            self.assertIsNone(self.second.acquire_permit(waiting["run_id"], "waiting", waiting["fence"], "model", "waiting-call", limit=1))
        self.first.release_permit(active["run_id"], "active", active["fence"], "model", request_id="long-call")
        self.assertIsNotNone(self.second.acquire_permit(waiting["run_id"], "waiting", waiting["fence"], "model", "waiting-call", limit=1))

    def test_new_mine_gets_a_turn_before_an_old_mines_backlog(self):
        for _ in range(8):
            self.accept("busy-mine")
            self.now += 1
        later = self.accept("other-mine")
        first = self.first.claim("worker-a", lease_seconds=100)
        self.assertEqual(first["request"]["mineId"], "busy-mine")
        self.now += 1
        second = self.second.claim("worker-b", lease_seconds=100)
        self.assertEqual(second["run_id"], later["runs"][0]["run_id"])
        self.assertEqual(second["request"]["mineId"], "other-mine")
        self.now += 1
        third = self.first.claim("worker-c", lease_seconds=100)
        self.assertEqual(third["request"]["mineId"], "busy-mine")


if __name__ == "__main__":
    unittest.main()
