"""Minimal ownership checks for the migrated deterministic operators."""

import json
from hashlib import sha256


class InvalidRequest(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def assert_snapshot_owner(snapshot, run):
    spec = run["spec"]
    for key, expected in {"run_id": run["run_id"], "mine_code": spec["mine_code"],
                          "source_system": spec["source_system"], "environment": spec["environment"]}.items():
        if snapshot.get(key) != expected:
            raise InvalidRequest(f"snapshot ownership mismatch: {key}")
    evidence = snapshot.get("evidence", [])
    registered = set()
    for entry in evidence:
        if not isinstance(entry, dict) or not entry.get("evidence_id") or entry["evidence_id"] in registered:
            raise InvalidRequest("invalid or duplicate evidence")
        registered.add(entry["evidence_id"])
        if any(entry.get(key) != snapshot.get(key) for key in ("run_id", "mine_code", "source_system", "environment", "snapshot_id")):
            raise InvalidRequest("evidence ownership mismatch")
        if entry.get("content_hash") not in {f.get("content_hash") for f in snapshot.get("files", [])}:
            raise InvalidRequest("evidence content hash outside frozen files")
    by_id = {e["evidence_id"]: e for e in evidence}
    for fact in snapshot.get("facts", []):
        refs = fact.get("evidence_refs", [])
        if not refs or not set(refs) <= registered:
            raise InvalidRequest("fact references unregistered evidence")
        if any(by_id[r].get("source_role") != fact.get("source_role") for r in refs):
            raise InvalidRequest("fact role differs from source evidence")


def validate_result(result, run, snapshot):
    if result.get("status") not in {"pass", "no_pass", "pending", "unverifiable", "failed"}:
        raise InvalidRequest("invalid result code")
    if result.get("run_id") != run["run_id"]:
        raise InvalidRequest("result run mismatch")
    if snapshot is not None:
        assert_snapshot_owner(snapshot, run)
        if result.get("snapshot_id") != snapshot["snapshot_id"]:
            raise InvalidRequest("result snapshot mismatch")
        registered = {e["evidence_id"] for e in snapshot["evidence"]}
        if not set(result.get("evidence_refs", [])) <= registered:
            raise InvalidRequest("unregistered result evidence")
        if result.get("status") == "pass" and snapshot.get("source_complete") is not True:
            raise InvalidRequest("incomplete source cannot pass")
