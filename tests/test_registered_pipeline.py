"""A new check's candidate contract, independent fact adapter and graph compose."""
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from apps.control_api.documents import acquire_run
from apps.control_api.evidence import EvidenceService
from apps.control_api.main import create_app
from apps.control_api.operators import GraphEvaluator
from apps.control_api.registry import CheckRegistry
from apps.control_api.settings import Settings
from apps.control_api.store import Store
from apps.control_api.validator import validate_candidate
from apps.control_api.worker import normalize_run, run_once


CONTRACT = {"source_roles": ["actual_record"], "fields": {"value": {"type": "number", "required": True, "source_label": "温度", "unit": "℃"}}}


def verify_temperature(candidate, snapshot):
    """Test adapter: exact labelled grammar, not a value anywhere on the page."""
    facts = []
    for record in snapshot["evidence"]:
        if record["evidence_id"] not in {ref if isinstance(ref, str) else ref["evidence_id"] for ref in candidate["evidence_refs"]}:
            continue
        match = re.fullmatch(r"温度\s*[:：]\s*(-?\d+(?:\.\d+)?)\s*℃", record["text"])
        if match and float(match.group(1)) == candidate["fields"].get("value"):
            facts.append({"fact_id": "temperature-" + record["evidence_id"], "kind": "temperature",
                          "source_role": "actual_record", "evidence_refs": [record["evidence_id"]],
                          "fields": {"value": float(match.group(1))}})
    return facts


class RegisteredPipelineTests(unittest.TestCase):
    def make_registry(self, root, *, validator=verify_temperature):
        asset = root / "temperature.json"
        asset.write_text(json.dumps({"itemCode": "temperature_check", "requiredPropositions": ["temperature_ok"],
                                     "propositionGraph": [{"code": "temperature_ok", "operator": "compare_number", "config": {
                                         "left": {"selector": {"kind": "temperature"}, "field": "value"},
                                         "right": {"value": 25}, "comparison": "<="}}]}), encoding="utf-8")
        graph = GraphEvaluator()
        registry = CheckRegistry(root=root)
        registry.register("temperature_check", asset, graph, operators=graph.operator_names,
                          fact_contract=CONTRACT, fact_validator=validator)
        plan = registry.compile("temperature_check")
        run = {"run_id": "r1", "mine_id": "m1", "item_code": "temperature_check", "plan": plan,
               "rule_revision": plan["planRevision"]}
        return registry, run

    def snapshot(self, text="温度:20℃"):
        digest = hashlib.sha256(text.encode()).hexdigest()
        return {"run_id": "r1", "mine_id": "m1", "source_complete": True, "coverage_complete": True,
                "evidence": [{"evidence_id": "e1", "run_id": "r1", "mine_id": "m1", "file_id": "f1", "document_id": "f1",
                              "document_version": digest, "content_hash": digest, "source_role": "actual_record",
                              "locator": {"line": 1}, "text": text}], "verified_facts": []}

    def bind(self, run, snapshot, value=20.0):
        candidate = {"run_id": "r1", "rule_revision": run["rule_revision"], "kind": "temperature", "status": "candidate",
                     "fields": {"value": value}, "evidence_refs": ["e1"],
                     "field_evidence": {"value": [{"evidence_id": "e1", "quote": snapshot["evidence"][0]["text"]}]}}
        return validate_candidate(candidate, run=run, evidence_by_id={"e1": snapshot["evidence"][0]}, fact_contract=CONTRACT)

    def test_registered_fact_adapter_is_required_between_source_anchor_and_graph(self):
        with tempfile.TemporaryDirectory() as temp:
            registry, run = self.make_registry(Path(temp))
            snapshot = self.snapshot()
            bound = self.bind(run, snapshot)
            self.assertEqual(bound["validation_status"], "source_bound")
            self.assertFalse(bound["verified"])
            self.assertEqual(registry.evaluate(run, snapshot)["status"], "pending")
            verified = registry.verify_candidate(run, bound, snapshot)
            self.assertTrue(verified["verified"], verified)
            self.assertEqual(verified["verification_scope"], "independent_registered_validator")
            snapshot["verified_facts"] = verified["facts"]
            result = registry.evaluate(run, snapshot)
            self.assertEqual(result["status"], "pass", result)
            self.assertEqual(result["propositions"][0]["evidence_refs"], ["e1"])

    def test_unrelated_number_or_inverted_value_cannot_be_promoted(self):
        with tempfile.TemporaryDirectory() as temp:
            registry, run = self.make_registry(Path(temp))
            # 20 exists in the quote, but belongs to ambient temperature.
            # Anchoring alone is deliberately insufficient for semantics.
            snapshot = self.snapshot("温度:21℃;环境温度:20℃")
            bound = self.bind(run, snapshot, 20.0)
            self.assertEqual(bound["validation_status"], "source_bound")
            verified = registry.verify_candidate(run, bound, snapshot)
            self.assertFalse(verified["verified"], verified)
            self.assertEqual(verified["facts"], [])
            self.assertEqual(registry.evaluate(run, snapshot)["status"], "pending")
            snapshot = self.snapshot()
            self.assertEqual(self.bind(run, snapshot, 21.0)["validation_status"], "candidate")

    def test_unregistered_semantic_adapter_does_not_turn_anchors_into_facts(self):
        with tempfile.TemporaryDirectory() as temp:
            registry, run = self.make_registry(Path(temp), validator=None)
            snapshot = self.snapshot()
            bound = self.bind(run, snapshot)
            verified = registry.verify_candidate(run, bound, snapshot)
            self.assertEqual(verified, bound)
            self.assertFalse(verified["verified"])
            self.assertEqual(verified["facts"], [])

    def test_api_service_submission_and_worker_consume_registered_facts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            registry, _ = self.make_registry(root)
            (root / "record.txt").write_text("温度:20℃", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"files": [{"path": "record.txt", "file_id": "f1", "source_role": "actual_record"}]}), encoding="utf-8")
            now = [100.0]
            store = Store(root / "ledger.sqlite3", clock=lambda: now[0])
            settings = Settings(environment="test", input_root=root, storage_dir=root / "artifacts", pi_enabled=True)
            try:
                with TestClient(create_app(store=store, settings=settings, registry=registry)) as client:
                    accepted = client.post("/inspection/tasks", json={"mineId": "m1", "checkCycle": "2026-10", "executionId": "registered-flow",
                                                                     "items": [{"itemCode": "temperature_check"}]})
                    self.assertEqual(accepted.status_code, 200, accepted.text)
                    task = store.get_task(accepted.headers["X-Inspection-Task"])
                    rid = task["runs"][0]["run_id"]
                    claimed = normalize_run(store.claim("owner-a", lease_seconds=1, run_id=rid))
                    snapshot = acquire_run({**claimed, "scope": {"manifest": str(manifest)}}, settings.document_settings())
                    store.save_snapshot(rid, "owner-a", claimed["fence"], snapshot)
                    evidence = snapshot["evidence"][0]
                    candidate = {"candidate_id": "temperature-c1", "attempt_id": "attempt-1", "run_id": rid,
                                 "rule_revision": claimed["rule_revision"], "proposition_id": "temperature_ok", "kind": "temperature",
                                 "fields": {"value": 20.0}, "status": "candidate", "provider": "test-provider", "model": "test-model",
                                 "prompt_revision": "test-prompt-v1", "evidence_refs": [evidence["evidence_id"]],
                                 "field_evidence": {"value": [{"evidence_id": evidence["evidence_id"], "quote": "温度:20℃"}]}}
                    service = EvidenceService(store, settings, registry=registry)
                    submitted = service.submit(rid, [candidate], worker_id="owner-a", fence=claimed["fence"])
                    self.assertEqual(submitted["accepted"], 1, submitted)
                    stored = store.get_run(rid)["candidates"][0]
                    self.assertTrue(stored["verified"], stored)
                    self.assertEqual(stored["facts"][0]["fields"], {"value": 20.0})
                    self.assertEqual(stored["verification_scope"], "independent_registered_validator")
                    now[0] += 2
                    outcome = run_once(store, settings, run_id=rid, worker_id="owner-b", registry=registry,
                                       pi_client=lambda *_: {"gaps": [], "toolCalls": 0})
                    self.assertEqual(outcome["status"], "pending", outcome)
                    proposition = next(p for p in outcome["propositions"] if p["code"] == "temperature_ok")
                    self.assertEqual(proposition["state"], "true", outcome)
                    self.assertEqual(proposition["evidence_refs"], [evidence["evidence_id"]])
                    self.assertFalse(outcome["coverage_audit"]["source_complete"], "No authority receipt was configured")
                    self.assertEqual(store.get_run(rid)["result"]["propositions"][0]["state"], "true")
            finally:
                store.engine.dispose()


if __name__ == "__main__":
    unittest.main()
