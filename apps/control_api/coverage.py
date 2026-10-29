"""Pure frozen-scope coverage audit, independent from business verdicts."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import hmac
import json

from .planner import resolve_material_requirements


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _work_rank(work):
    def number(value):
        return value if type(value) in {int, float} else 0
    return (number(work.get("fence", work.get("run_fence"))),
            number(work.get("updated_at", work.get("created_at"))), number(work.get("attempt")))


def _work_input(work):
    value = work.get("input", work.get("input_json", work.get("payload", {}))) or {}
    return value if isinstance(value, dict) else {}


def _latest_work_units(work_units):
    """Order-independent projection of persistent work-unit identities."""
    latest = {}
    state_priority = {"succeeded": 0, "queued": 1, "running": 2, "failed": 3}
    for work in work_units:
        identity = work.get("work_unit_id", work.get("unit_id")) or _canonical([work.get("kind"), _work_input(work)])
        previous = latest.get(identity)
        rank = (*_work_rank(work), state_priority.get(work.get("state"), 4), _canonical(work))
        if previous is None or rank > previous[0]:
            latest[identity] = (rank, work)
    # Deterministic order also makes overlapping coverage outputs reproducible.
    return [entry[1] for entry in sorted(latest.values(), key=lambda entry: entry[0])]


def _work_scopes(work):
    data = _work_input(work)
    rows = data.get("pages")
    rows = rows if isinstance(rows, list) else [data]
    scopes = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        file_id = row.get("file_id", work.get("file_id"))
        if not file_id:
            continue
        unit_id = row.get("unit_id", work.get("unit_id"))
        if type(row.get("page")) is int:
            unit_id = f"page:{row['page']}"
        scopes.append((file_id, unit_id or "*"))
    return scopes


def sign_scope_proof(payload: dict, key: str | bytes) -> dict:
    """Authority-side helper. Do not expose signing keys or this operation to Pi."""
    if not isinstance(payload, dict) or not key:
        raise ValueError("scope proof requires a payload and server authority key")
    secret = key.encode() if isinstance(key, str) else key
    return {"payload": deepcopy(payload), "signature": hmac.new(secret, _canonical(payload).encode(), hashlib.sha256).hexdigest()}


def _proof_valid(run, snapshot, proof, key):
    if not isinstance(proof, dict) or not isinstance(proof.get("payload"), dict) or not key:
        return False, "scope_proof_missing"
    payload = proof["payload"]
    try:
        expected = sign_scope_proof(payload, key)["signature"]
    except (TypeError, ValueError):
        return False, "scope_proof_invalid"
    signature = proof.get("signature")
    if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
        return False, "scope_proof_signature_invalid"
    mine = run.get("mine_id", run.get("mineId", run.get("request", {}).get("mineId")))
    cycle = run.get("check_cycle", run.get("checkCycle", run.get("request", {}).get("checkCycle")))
    if (not mine or not cycle or payload.get("mine_id") != mine or payload.get("check_cycle") != cycle
            or not snapshot.get("manifest_hash") or payload.get("manifest_hash") != snapshot["manifest_hash"]
            or payload.get("scope_complete") is not True or not isinstance(payload.get("authority"), str)
            or not payload["authority"].strip()):
        return False, "scope_proof_binding_invalid"
    if payload.get("run_id") is not None and payload["run_id"] != run.get("run_id", run.get("runId")):
        return False, "scope_proof_run_mismatch"

    def files(rows):
        if not isinstance(rows, list) or any(not isinstance(row, dict) or not row.get("file_id") or not row.get("content_hash") for row in rows):
            raise ValueError("invalid file inventory")
        values = [(row["file_id"], row["content_hash"]) for row in rows]
        if len({entry[0] for entry in values}) != len(values):
            raise ValueError("duplicate file identity")
        return sorted(values)

    try:
        if files(payload.get("files")) != files(snapshot.get("files")):
            return False, "scope_proof_files_mismatch"
    except (TypeError, ValueError):
        return False, "scope_proof_files_invalid"
    return True, None


def audit_coverage(run, snapshot, plan, *, work_units=(), searches=(), proof=None, proof_key="", propositions=()):
    """Audit actual frozen units and host-recorded exhaustive reads/searches.

    ``source_complete`` describes authority-proven inventory, while
    ``coverage_complete`` describes work performed on that inventory. Both
    must hold before a business evaluator may consider a pass. A top-k search
    without explicit host-recorded completion does not close any unit.
    """
    snapshot = deepcopy(snapshot)
    files = snapshot.get("files", [])
    coverage = {(row.get("file_id"), row.get("unit_id", "document")): dict(row) for row in snapshot.get("coverage", [])}
    failed_units, reviewed_units, reviewed_coverage = {}, {}, {}
    for work in _latest_work_units(work_units):
        data = _work_input(work)
        file_id = work.get("file_id", data.get("file_id"))
        if work.get("state") == "failed":
            for scope in _work_scopes(work):
                failed_units[scope] = max(failed_units.get(scope, _work_rank(work)), _work_rank(work))
        if work.get("state") != "succeeded":
            continue
        output = work.get("output", work.get("output_json", work.get("result", {}))) or {}
        if not isinstance(output, dict):
            continue
        for row in output.get("coverage", []):
            fid = row.get("file_id", file_id)
            scope = (fid, row.get("unit_id", "document"))
            coverage[scope] = {**row, "file_id": fid}
            if work.get("kind") == "review_transcription" and row.get("state") in {"readable", "empty", "indexed", "parsed"}:
                reviewed_units[scope] = max(reviewed_units.get(scope, _work_rank(work)), _work_rank(work))
                reviewed_coverage[scope] = {**row, "file_id": fid}
    for scope, reviewed_rank in reviewed_units.items():
        if coverage.get(scope, {}).get("state") != "conflicted":
            coverage[scope] = reviewed_coverage[scope]
        if scope in failed_units and reviewed_rank >= failed_units[scope]:
            failed_units.pop(scope)
    completed_searches = {}
    evidence_ids = sorted(item["evidence_id"] for item in snapshot.get("evidence", []) if item.get("evidence_id"))
    index_revision = hashlib.sha256(_canonical(evidence_ids).encode()).hexdigest()
    for search in searches:
        if search.get("complete") is not True and search.get("exhaustive") is not True:
            continue
        if search.get("index_revision") and search["index_revision"] != index_revision:
            continue
        keys = search.get("units") or [{"file_id": search.get("file_id"), "unit_id": search.get("unit_id", "document")}]
        for unit in keys:
            if isinstance(unit, dict) and unit.get("file_id"):
                completed_searches.setdefault((unit["file_id"], unit.get("unit_id", "document")), []).append(search)
    required_queries = [""] + list(dict.fromkeys(query for queries in plan.get("retrievalQueries", {}).values() for query in queries)) if plan.get("retrievalQueries") else []
    required_hashes = {hashlib.sha256(_canonical(query).encode()).hexdigest() for query in required_queries}
    units, gaps = [], []
    contract = plan.get("coverageContract", plan.get("executionPlan", {}).get("coverageContract", {})) or {}
    for unit in contract.get("units", []):
        if isinstance(unit, dict) and unit.get("file_id") and unit.get("unit_id"):
            coverage.setdefault((unit["file_id"], unit["unit_id"]), {**unit, "state": "not_indexed"})
    materials = resolve_material_requirements(plan, propositions)
    required_groups = set(materials["required_material_groups"])
    deferred_groups = materials["deferred_material_groups"]
    gaps.extend("material_condition_unresolved:" + group for group in deferred_groups)
    present_groups = {file.get("material_group") for file in files}
    for group in sorted(required_groups - present_groups):
        units.append({"file_id": None, "unit_id": "material:" + group, "material_group": group, "state": "source_absent"})
        gaps.append("source_absent:" + group)
    for file in files:
        file_id = file.get("file_id")
        page_count = file.get("page_count", file.get("pageCount"))
        if type(page_count) is int and 0 < page_count <= 1000000:
            for page in range(1, page_count + 1):
                unit_id = f"page:{page}"
                coverage.setdefault((file_id, unit_id), {"file_id": file_id, "unit_id": unit_id, "state": "not_indexed"})
        known = [row for (fid, _), row in coverage.items() if fid == file_id]
        if not known:
            known = [{"file_id": file_id, "unit_id": "document", "state": "not_indexed"}]
        for record in sorted(known, key=lambda row: str(row.get("unit_id", "document"))):
            unit_id, native = record.get("unit_id", "document"), record.get("state", "not_indexed")
            if file.get("conflicted") is True or native == "conflicted":
                state = "conflicted"
            elif native in {"failed", "provider_failed"} or (file_id, unit_id) in failed_units or (file_id, "*") in failed_units:
                state = "provider_failed"
            elif native in {"unreadable", "partial", "unsupported"}:
                state = "unreadable"
            elif native not in {"readable", "empty", "indexed", "parsed"}:
                state = "not_indexed"
            elif native == "empty":
                state = "empty"
            else:
                receipts = completed_searches.get((file_id, unit_id), [])
                if required_hashes:
                    receipts = [search for search in receipts if search.get("index_revision") == index_revision]
                if not receipts or (required_hashes and not required_hashes <= {search.get("query_hash") for search in receipts}):
                    state = "not_searched"
                elif any(search.get("state") == "provider_failed" for search in receipts):
                    state = "provider_failed"
                elif any(search.get("state") == "conflicted" for search in receipts):
                    state = "conflicted"
                else:
                    count = max((search.get("unit_hit_counts", {}).get(f"{file_id}/{unit_id}", 0)
                                 if "unit_hit_counts" in search else search.get("hit_count", search.get("total", 0))
                                 for search in receipts), default=0)
                    state = "not_searched" if type(count) is not int or count < 0 else "searched_hit" if count else "searched_no_hit"
            units.append({**record, "file_id": file_id, "unit_id": unit_id, "parse_state": native, "state": state})
            if state not in {"searched_hit", "searched_no_hit", "empty"}:
                gaps.append(f"{state}:{file_id}/{unit_id}")
    valid, proof_gap = _proof_valid(run, snapshot, proof, proof_key)
    expected_owner = {"run_id": run.get("run_id", run.get("runId")),
                      "mine_id": run.get("mine_id", run.get("mineId", run.get("request", {}).get("mineId")))}
    if any(snapshot.get(field) not in {None, value} for field, value in expected_owner.items()):
        valid, proof_gap = False, "scope_snapshot_owner_mismatch"
    file_ids = {file.get("file_id") for file in files}
    for unit in contract.get("units", []):
        if unit.get("file_id") not in file_ids:
            units.append({**unit, "state": "source_absent"})
            gaps.append(f"source_absent:{unit['file_id']}/{unit['unit_id']}")
    if proof_gap:
        gaps.append(proof_gap)
    source_gaps = any(row["state"] == "source_absent" for row in units)
    complete = bool(units) and not deferred_groups and all(row["state"] in {"searched_hit", "searched_no_hit", "empty"} for row in units)
    return {"source_complete": bool(valid and not source_gaps), "coverage_complete": complete,
            "units": units, "gaps": list(dict.fromkeys(gaps)), "scope": "frozen_run_only",
            "authority": proof["payload"].get("authority") if valid else None,
            "absence_is_not_negative": True, "material_requirements": materials,
            "required_material_groups": materials["required_material_groups"],
            "deferred_material_groups": deferred_groups}
