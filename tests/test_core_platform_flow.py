"""Platform integration without coal-rule assumptions or a real model endpoint."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from apps.control_api.coverage import sign_scope_proof
from apps.control_api.documents import acquire_run
from apps.control_api.evidence import EvidenceService
from apps.control_api.main import create_app
from apps.control_api.registry import CheckRegistry
from apps.control_api.settings import Settings
from apps.control_api.store import Conflict, StaleLease, Store
from apps.control_api.worker import normalize_run, resolve_manifest, run_once


ITEM = "synthetic_core_observation"
PROOF_KEY = "isolated-core-authority-key"


def evaluate_observation(run, snapshot):
    """Test-only check: exact markers in every declared original document."""
    originals = [record for record in snapshot.get("evidence", [])
                 if record.get("source_role") == "actual_record"
                 and str(record.get("text", "")).startswith("observed:")]
    present = {record["file_id"] for record in originals}
    expected = {file["file_id"] for file in snapshot.get("files", [])}
    refs = sorted(record["evidence_id"] for record in originals)
    satisfied = bool(expected and present == expected and refs)
    complete = snapshot.get("source_complete") is True
    gaps = list(snapshot.get("coverage_gaps", []))
    if not satisfied:
        gaps.append("observation_originals_missing")
    if not complete:
        gaps.append("scope_or_processing_incomplete")
    return {"status": "pass" if satisfied and complete and not gaps else "pending",
            "description": "合成平台观察项", "detail": "Exact original markers, independent of coal-mine rules",
            "snapshot_id": snapshot.get("snapshot_id"), "evidence_refs": refs,
            "propositions": [{"code": "originals_observed", "state": "true" if satisfied else "missing",
                              "evidence_refs": refs}], "findings": [], "coverage_gaps": gaps}


class CorePlatformFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sources = {"f1": b"observed: alpha\nsupporting: alpha\n", "f2": b"observed: beta\nsupporting: beta\n"}
        files = []
        for fid, data in self.sources.items():
            (self.root / (fid + ".txt")).write_bytes(data)
            files.append({"path": fid + ".txt", "file_id": fid,
                          "source_role": "actual_record", "material_group": "observations"})
        self.manifest = self.root / "manifest.json"
        self.manifest.write_text(json.dumps({"mine_id": "m1", "environment": "production", "files": files}), encoding="utf-8")
        self.proof_payload = {"authority": "isolated-test-source-authority", "mine_id": "m1", "check_cycle": "2026-10",
                              "manifest_hash": hashlib.sha256(self.manifest.read_bytes()).hexdigest(), "scope_complete": True,
                              "files": [{"file_id": fid, "content_hash": hashlib.sha256(data).hexdigest()}
                                        for fid, data in self.sources.items()]}
        self.receipt = self.root / "scope-proof.json"
        self.write_proof()
        config = self.root / "sources.json"
        config.write_text(json.dumps({"mode": "local_manifest", "manifests": {"m1": {"2026-10": "manifest.json"}},
                                      "authority_receipt_file": "scope-proof.json"}), encoding="utf-8")
        asset = self.root / "synthetic.v1.json"
        asset.write_text(json.dumps({"itemCode": ITEM, "name": "Synthetic core observation", "status": "test-only",
                                     "requiredPropositions": ["originals_observed"],
                                     "materialRequirements": [{"materialGroup": "observations", "role": "actual_record",
                                                               "cadence": "monthly", "required": True}],
                                     "coverageContract": {"materialGroups": ["observations"]}}), encoding="utf-8")
        self.registry = CheckRegistry(root=self.root)
        self.registry.register(ITEM, asset, evaluate_observation)
        # Production semantics are exercised against an isolated SQLite ledger;
        # this is deliberately not a PostgreSQL deployment acceptance test.
        self.settings = Settings(environment="production", input_root=self.root, storage_dir=self.root / "artifacts",
                                 source_config=config, allowed_mines=("m1",), internal_token="isolated-core-internal-token",
                                 scope_proof_key=PROOF_KEY, pi_enabled=True)
        self.clock = [100.0]
        self.store = Store(self.root / "ledger.sqlite3", clock=lambda: self.clock[0])
        self.client = TestClient(create_app(store=self.store, settings=self.settings, registry=self.registry))

    def tearDown(self):
        self.client.close()
        self.store.engine.dispose()
        self.temp.cleanup()

    def write_proof(self, payload=None):
        proof = sign_scope_proof(payload or self.proof_payload, PROOF_KEY)
        self.receipt.write_text(json.dumps(proof), encoding="utf-8")

    def accept(self, execution="core-flow"):
        response = self.client.post("/inspection/tasks", json={"mineId": "m1", "checkCycle": "2026-10",
                                    "executionId": execution, "items": [{"itemCode": ITEM}]})
        self.assertEqual(response.status_code, 200, response.text)
        task = self.client.get(f"/inspection/tasks/{response.headers['X-Inspection-Task']}/summary").json()
        return task["runs"][0]["runId"]

    def collect(self, run, settings, owner, fence):
        service = EvidenceService(self.store, settings)
        before = service.snapshot(service._run(run["run_id"]))
        file_ids = [file["file_id"] for file in before["files"]]
        parsed = service.inspect_documents(run["run_id"], file_ids, worker_id=owner, fence=fence)
        for fid in file_ids:
            # Pagination is intentionally smaller than the document inventory;
            # receipts must close only the fully returned frozen query range.
            page = service.search(run["run_id"], "", file_id=fid, offset=0, limit=1, worker_id=owner, fence=fence)
            for offset in range(1, page["total"]):
                service.search(run["run_id"], "", file_id=fid, offset=offset, limit=1, worker_id=owner, fence=fence)
        return {"gaps": [], "toolCalls": len(file_ids) + 1, "parsed": parsed}

    def test_registered_item_lazy_parse_exhaustive_search_and_proof_reach_pass(self):
        rid = self.accept()
        runtime = self.client.get("/v1/runtime").json()
        self.assertEqual([item["itemCode"] for item in runtime["supported_items"]], [ITEM])
        self.assertEqual(self.client.get(f"/v1/rules/{ITEM}/plan").status_code, 200)
        started = []

        def pi(run, settings, owner, fence):
            started.append(run["snapshot"])
            self.assertEqual(run["snapshot"]["evidence"], [])
            self.assertTrue(all(unit["state"] == "not_indexed" for unit in run["snapshot"]["coverage"]))
            return self.collect(run, settings, owner, fence)

        result = run_once(self.store, self.settings, run_id=rid, pi_client=pi, registry=self.registry)
        self.assertEqual(result["status"], "pass", result)
        self.assertTrue(result["coverage_audit"]["source_complete"])
        self.assertTrue(result["coverage_audit"]["coverage_complete"])
        self.assertFalse(self.store.get_run(rid)["snapshot"]["source_complete"], "Frozen inventory is not overwritten by processing")
        self.assertEqual(len(self.store.get_run(rid)["evidence"]), 4)
        self.assertEqual(len(self.store.list_work_units(rid)), 2)
        self.assertTrue(all(unit["state"] == "succeeded" and unit["attempt"] == 1 for unit in self.store.list_work_units(rid)))
        self.assertEqual(self.client.get(f"/v1/runs/{rid}/report").json()["result"]["status"], "pass")
        self.assertTrue(started)

    def test_tampered_or_missing_file_scope_receipt_cannot_pass(self):
        for mode in ("tampered_signature", "omitted_file"):
            with self.subTest(mode=mode):
                if mode == "tampered_signature":
                    self.write_proof()
                    receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
                    receipt["payload"]["authority"] = "tampered-without-signing"
                    self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
                else:
                    payload = {**self.proof_payload, "files": self.proof_payload["files"][:1]}
                    self.write_proof(payload)
                rid = self.accept(mode)
                result = run_once(self.store, self.settings, run_id=rid, pi_client=self.collect, registry=self.registry)
                self.assertEqual(result["status"], "pending", result)
                self.assertFalse(result["coverage_audit"]["source_complete"])
                self.assertTrue(result["coverage_audit"]["coverage_complete"])
                self.assertTrue(any("scope_proof" in gap for gap in result["coverage_gaps"]), result)

    def test_signed_inventory_without_parsing_or_searching_is_not_complete(self):
        rid = self.accept()
        result = run_once(self.store, self.settings, run_id=rid,
                          pi_client=lambda *_: {"gaps": [], "toolCalls": 0}, registry=self.registry)
        self.assertEqual(result["status"], "pending", result)
        self.assertTrue(result["coverage_audit"]["source_complete"])
        self.assertFalse(result["coverage_audit"]["coverage_complete"])
        self.assertTrue(all(unit["state"] == "not_indexed" for unit in result["coverage_audit"]["units"]))

    def test_first_search_page_does_not_close_the_unreturned_query_range(self):
        rid = self.accept()
        run = normalize_run(self.store.claim("owner", run_id=rid))
        selected = resolve_manifest(run, self.settings)
        snapshot = acquire_run(selected, {**self.settings.document_settings(), "inventory_only": True})
        self.store.save_snapshot(rid, "owner", run["fence"], snapshot)
        service = EvidenceService(self.store, self.settings)
        service.inspect_documents(rid, list(self.sources), worker_id="owner", fence=run["fence"])
        first = service.search(rid, "", limit=1, worker_id="owner", fence=run["fence"])
        self.assertEqual(first["total"], 4)
        self.assertTrue(service.audit(rid)["source_complete"])
        self.assertFalse(service.audit(rid)["coverage_complete"])
        for offset in range(1, first["total"]):
            service.search(rid, "", offset=offset, limit=1, worker_id="owner", fence=run["fence"])
        self.assertTrue(service.audit(rid)["coverage_complete"])

    def test_untrusted_pass_cannot_bypass_audit_at_finalization(self):
        rid = self.accept()
        run = normalize_run(self.store.claim("owner", run_id=rid))
        selected = resolve_manifest(run, self.settings)
        snapshot = acquire_run(selected, {**self.settings.document_settings(), "inventory_only": True})
        self.store.save_snapshot(rid, "owner", run["fence"], snapshot)
        forged = {"status": "pass", "coverage_gaps": [], "evidence_refs": [],
                  "coverage_audit": {"source_complete": True, "coverage_complete": True},
                  "propositions": [{"code": "originals_observed", "state": "true", "evidence_refs": ["invented"]}]}
        with self.assertRaises(Conflict):
            self.store.finalize(rid, "owner", run["fence"], forged)
        self.assertIsNone(self.store.get_run(rid)["result"])

    def test_recovery_reuses_success_and_retries_only_failed_unit_under_new_fence(self):
        rid = self.accept()
        first = normalize_run(self.store.claim("owner-a", lease_seconds=1, run_id=rid))
        selected = resolve_manifest(first, self.settings)
        snapshot = acquire_run(selected, {**self.settings.document_settings(), "inventory_only": True})
        self.store.save_snapshot(rid, "owner-a", first["fence"], snapshot)
        service = EvidenceService(self.store, self.settings)
        success = service.parse(rid, "f1", worker_id="owner-a", fence=first["fence"])
        frozen = Path(next(file for file in snapshot["files"] if file["file_id"] == "f2")["path"])
        frozen.write_bytes(b"changed frozen bytes")
        with self.assertRaises(ValueError):
            service.parse(rid, "f2", worker_id="owner-a", fence=first["fence"])
        self.assertFalse(service.audit(rid)["coverage_complete"])
        frozen.write_bytes(self.sources["f2"])
        self.clock[0] += 2
        second = normalize_run(self.store.claim("owner-b", lease_seconds=10, run_id=rid))
        self.assertGreater(second["fence"], first["fence"])
        restored_service = EvidenceService(self.store, self.settings)
        reused = restored_service.parse(rid, "f1", worker_id="owner-b", fence=second["fence"])
        retried = restored_service.parse(rid, "f2", worker_id="owner-b", fence=second["fence"])
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["work_unit_id"], success["work_unit_id"])
        self.assertFalse(retried["reused"])
        attempts = {unit["input"]["file_id"]: unit["attempt"] for unit in self.store.list_work_units(rid)}
        self.assertEqual(attempts, {"f1": 1, "f2": 2})
        with self.assertRaises(StaleLease):
            restored_service.parse(rid, "f1", worker_id="owner-a", fence=first["fence"])
        self.clock[0] += 11
        result = run_once(self.store, self.settings, run_id=rid, pi_client=self.collect, registry=self.registry)
        self.assertEqual(result["status"], "pass", result)
        self.assertEqual({unit["input"]["file_id"]: unit["attempt"] for unit in self.store.list_work_units(rid)}, attempts)


if __name__ == "__main__":
    unittest.main()
