import unittest

from apps.control_api.store import Conflict, Store


class FinalAuditGateTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.store = Store(":memory:", clock=lambda: self.now)
        task = self.store.accept({"mineId": "m1", "checkCycle": "2026-10", "executionId": "audit-case",
                                  "items": [{"itemCode": "synthetic"}]}, {"synthetic": {"planRevision": "rev1"}})
        self.run_id = task["runs"][0]["run_id"]
        self.run = self.store.claim("owner", run_id=self.run_id)
        self.store.save_snapshot(self.run_id, "owner", self.run["fence"], {"snapshot_id": "snap1", "manifest_hash": "manifest1",
            "environment": "production", "source_complete": False, "evidence": [{"evidence_id": "ev1"}], "files": []})

    def tearDown(self):
        self.store.engine.dispose()

    def audit(self, aid, complete):
        value = {"audit_id": aid, "manifest_hash": "manifest1", "rule_revision": "rev1",
                 "source_complete": complete, "coverage_complete": complete}
        self.store.save_records(self.run_id, "owner", self.run["fence"], "coverage_audit", [value])
        return value

    def result(self, audit):
        return {"run_id": self.run_id, "status": "pass", "evidence_refs": ["ev1"], "coverage_gaps": [],
                "propositions": [{"state": "true", "evidence_refs": ["ev1"]}], "coverage_audit": audit}

    def test_older_complete_audit_cannot_hide_newer_gap(self):
        complete = self.audit("first", True)
        self.now += 1
        self.audit("second", False)
        with self.assertRaisesRegex(Conflict, "complete source"):
            self.store.finalize(self.run_id, "owner", self.run["fence"], self.result(complete))

    def test_latest_matching_complete_audit_permits_finalization(self):
        self.audit("first", False)
        self.now += 1
        complete = self.audit("second", True)
        self.assertEqual(self.store.finalize(self.run_id, "owner", self.run["fence"], self.result(complete))["status"], "pass")

    def test_result_cannot_substitute_unregistered_audit(self):
        self.audit("only", False)
        with self.assertRaisesRegex(Conflict, "complete source"):
            self.store.finalize(self.run_id, "owner", self.run["fence"], self.result({"audit_id": "invented", "source_complete": True, "coverage_complete": True}))


if __name__ == "__main__":
    unittest.main()
