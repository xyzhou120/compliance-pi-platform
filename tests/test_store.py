import unittest

from apps.control_api.store import Conflict, StaleLease, Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.request = {"mineId": "m1", "checkCycle": "2026-10", "items": [{"itemCode": "stop_work_unload"}], "executionId": "exec-1"}

    def test_identity_is_idempotent_and_scope_immutable(self):
        first = self.store.accept(self.request, {"stop_work_unload": {"revision": "r1"}})
        second = self.store.accept(self.request, {"stop_work_unload": {"revision": "r1"}})
        self.assertEqual(first["taskId"], second["taskId"])
        changed = {**self.request, "resultCallbackUrl": "https://callback.invalid"}
        with self.assertRaises(Conflict):
            self.store.accept(changed, {"stop_work_unload": {"revision": "r1"}})

    def test_lease_fence_rejects_old_worker(self):
        accepted = self.store.accept(self.request, {})
        run = accepted["runs"][0]
        claimed = self.store.claim("worker-a", run_id=run["runId"])
        with self.assertRaises(StaleLease):
            self.store.checkpoint(run["runId"], "worker-b", claimed["fence"], {"stage": "source"})

    def test_candidate_dedup_and_atomic_finalize_outbox(self):
        accepted = self.store.accept(self.request, {})
        run = accepted["runs"][0]
        claimed = self.store.claim("worker-a", run_id=run["runId"])
        candidate = {"candidate_id": "c1", "quote": "source"}
        self.assertEqual(self.store.save_candidates(run["runId"], "worker-a", claimed["fence"], [candidate, candidate])["savedCandidates"], 1)
        self.store.finalize(run["runId"], "worker-a", claimed["fence"], {"status": "pending", "description": "gap", "detail": "gap"}, {"checkResult": "pending"}, "https://callback.invalid")
        delivery = self.store.claim_delivery("delivery")
        self.assertEqual(delivery["payload"]["checkResult"], "pending")

    def test_implicit_identity_does_not_change_when_callback_changes(self):
        request = {key: value for key, value in self.request.items() if key != "executionId"}
        self.store.accept(request, {})
        with self.assertRaises(Conflict):
            self.store.accept({**request, "resultCallbackUrl": "https://other.invalid"}, {})

    def test_new_execution_is_a_new_run_and_stale_claim_cannot_publish(self):
        first = self.store.accept(self.request, {})
        second = self.store.accept({**self.request, "executionId": "exec-2"}, {})
        self.assertNotEqual(first["taskId"], second["taskId"])
        clock = [1000.0]
        store = Store(":memory:", clock=lambda: clock[0])
        accepted = store.accept(self.request, {})
        run_id = accepted["runs"][0]["runId"]
        first_claim = store.claim("a", lease_seconds=1, run_id=run_id)
        clock[0] += 2
        second_claim = store.claim("b", lease_seconds=10, run_id=run_id)
        self.assertEqual(second_claim["fence"], first_claim["fence"] + 1)
        with self.assertRaises(StaleLease):
            store.finalize(run_id, "a", first_claim["fence"], {"status": "pending"})

    def test_final_transaction_rejects_false_pass_and_unknown_evidence(self):
        task = self.store.accept(self.request, {})
        rid = task["runs"][0]["run_id"]
        run = self.store.claim("w", run_id=rid)
        for result in ({"status": "pass"}, {"status": "pending", "run_id": "other"}, {"status": "pending", "evidence_refs": ["unknown"]}, {"status": "no_pass", "findings": []}):
            with self.assertRaises(Conflict):
                self.store.finalize(rid, "w", run["fence"], result)
        self.assertIsNone(self.store.get_run(rid)["result"])

    def test_canonical_rule_revision_is_plan_revision(self):
        task = self.store.accept(self.request, {"stop_work_unload": {"revision": "asset", "planRevision": "frozen-plan"}})
        run = self.store.get_run(task["runs"][0]["run_id"])
        self.assertEqual(run["ruleRevision"], "frozen-plan")
        self.assertEqual(run["spec"]["rule_revision"], "frozen-plan")


if __name__ == "__main__":
    unittest.main()
