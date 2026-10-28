from __future__ import annotations

import calendar
from copy import deepcopy

from .association import associate_facts
from .fact_extraction import extract_facts
from .rulebook import DEFAULT_RULEBOOK_VERSION, load_rulebook
from .rules import evaluate_documents
from .scope_checks import evaluate_scope


def evaluate_run(run: dict, snapshot: dict) -> dict:
    """Run the migrated deterministic evaluator behind the new contract."""
    if snapshot.get("mine_id") not in {None, run["mine_id"]} or snapshot.get("run_id") not in {None, run["run_id"]}:
        raise ValueError("snapshot owner mismatch")
    spec = {
        "source_system": run.get("source_system", "pi-platform"),
        "mine_code": run["mine_id"],
        "environment": run.get("environment", "production"),
        "item_code": run["item_code"],
        "check_cycle": run["check_cycle"],
        "definition_version": "statutory-2026-v1",
        "inspection_item": {"handler": run["item_code"]},
    }
    adapted_run = {"run_id": run["run_id"], "spec": spec}
    adapted = deepcopy(snapshot)
    adapted.setdefault("source_system", spec["source_system"])
    adapted.setdefault("mine_code", run["mine_id"])
    adapted.setdefault("environment", spec["environment"])
    adapted.setdefault("profile", "local-documents")
    adapted.setdefault("snapshot_id", run.get("snapshot_id", "snapshot-unset"))
    adapted.setdefault("files", [])
    adapted.setdefault("evidence", [])
    adapted.setdefault("coverage", [])
    adapted.setdefault("coverage_gaps", [])
    for evidence in adapted["evidence"]:
        if evidence.get("mine_id") not in {None, run["mine_id"]}:
            raise ValueError("evidence owner mismatch")
        for key in ("run_id", "mine_code", "source_system", "environment", "snapshot_id"):
            evidence.setdefault(key, adapted[key])
    observation = adapted.get("observation") or {}
    if not observation.get("start") or not observation.get("end"):
        year, month = map(int, run["check_cycle"].split("-"))
        observation = {**observation, "start": f"{year:04d}-{month:02d}-01", "end": f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"}
    adapted["observation"] = observation
    baseline = load_rulebook(DEFAULT_RULEBOOK_VERSION if run["item_code"] == "stop_work_unload" else "impact-1023-draft-v2")
    adapted["rule_baseline"] = baseline
    # Native deterministic extraction is the only input to statutory rules.
    # Model candidates are stored separately by the control plane and do not
    # become facts merely because they contain a plausible JSON object.
    extracted = extract_facts(adapted.get("evidence", []), allow_unbound=True)
    associated = associate_facts(extracted["facts"], observation=adapted["observation"])
    adapted["facts"] = associated["facts"]
    adapted["association_links"] = associated["links"]
    adapted["coverage_gaps"] = list(dict.fromkeys(adapted["coverage_gaps"] + extracted["gaps"] + associated["gaps"]))
    result = evaluate_documents(adapted_run, adapted)
    scope_snapshot = deepcopy(adapted)
    if run["item_code"] == "stop_work_unload":
        triggered = {(p.get("event_id"), p.get("workface")) for p in result["propositions"]
                     if p.get("code") == "trigger" and p.get("state") == "true"}
        # Scope conditions cannot manufacture a violation when the statutory
        # trigger was not independently established.
        scope_snapshot["facts"] = [f for f in adapted["facts"] if (f.get("event_id"), f.get("workface")) in triggered]
    scoped = evaluate_scope(adapted_run, scope_snapshot)
    result["propositions"].extend(scoped["propositions"])
    result["findings"].extend(scoped["findings"])
    result["coverage_gaps"] = list(dict.fromkeys(result.get("coverage_gaps", []) + scoped["gaps"]))
    result["evidence_refs"] = sorted(set(result.get("evidence_refs", [])) | {ref for p in scoped["propositions"] for ref in p["evidence_refs"]})
    if scoped["findings"]:
        result["status"] = "no_pass"
        result["description"] = "实际证据证明检查条件不满足"
    elif scoped["gaps"] and result["status"] == "pass":
        result["status"] = "pending"
        result["description"] = "必要证据未闭合，本轮结束"
    from .rulebook import apply_draft_scope
    from .rules import gate_conflicted_evidence, classify_stop_findings
    result = apply_draft_scope(result, adapted, run["item_code"], implemented_ids=scoped["implemented_ids"])
    result = gate_conflicted_evidence(result, adapted)
    if run["item_code"] == "stop_work_unload" and baseline.get("version") == "stop-work-unload-scope-v3":
        result = classify_stop_findings(result, adapted)
    result["association_links"] = associated["links"]
    result["candidate_count"] = len(extracted["candidates"])
    result["extraction_gaps"] = extracted["gaps"]
    return result
