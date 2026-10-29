from __future__ import annotations

import calendar
from copy import deepcopy

from .association import associate_facts
from .fact_extraction import extract_facts
from .rulebook import DEFAULT_RULEBOOK_VERSION, load_rulebook
from .rules import evaluate_documents
from .scope_checks import evaluate_scope
from .source_relations import analyze_source_relations, relation_explanation
from .threshold_profiles import extract_threshold_profiles, compare_threshold_profiles
from .monitoring_candidates import extract_monitoring_candidates, adjacent_probe_groups
from ..review_plan import build_review_plan


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
    adapted["monitoring_policy_mode"] = (run.get("plan") or {}).get("monitoringPolicy", {}).get("mode")
    # Native deterministic extraction is the only input to statutory rules.
    # Model candidates are stored separately by the control plane and do not
    # become facts merely because they contain a plausible JSON object.
    extracted = extract_facts(adapted.get("evidence", []), allow_unbound=True)
    associated = associate_facts(extracted["facts"], observation=adapted["observation"])
    adapted["facts"] = associated["facts"]
    adapted["association_links"] = associated["links"]
    adapted["coverage_gaps"] = list(dict.fromkeys(adapted["coverage_gaps"] + extracted["gaps"] + associated["gaps"]))
    adapted["source_relations"] = analyze_source_relations(adapted)
    threshold_files = [file for file in adapted.get("files", []) if file.get("material_group") == "threshold"]
    threshold_ids = {file["file_id"] for file in threshold_files}
    threshold_profile = extract_threshold_profiles(threshold_files,
        [entry for entry in adapted.get("evidence", []) if entry.get("file_id") in threshold_ids])
    threshold_comparison = compare_threshold_profiles(threshold_profile)
    monitoring_ids = {file["file_id"] for file in adapted.get("files", []) if file.get("material_group") == "prevention_daily"}
    monitoring_candidates = extract_monitoring_candidates(
        [entry for entry in adapted.get("evidence", []) if entry.get("file_id") in monitoring_ids],
        coverage=[unit for unit in adapted.get("coverage", []) if unit.get("file_id") in monitoring_ids])
    adapted["trigger_review"] = {"threshold_profiles": threshold_profile, "threshold_comparison": threshold_comparison,
                                  "monitoring_candidates": monitoring_candidates,
                                  "adjacent_probe_groups": adjacent_probe_groups(monitoring_candidates.get("candidates", [])),
                                  "trigger": None, "verified": False,
                                  "policy": "trigger-evidence-v1"}
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
    result["trigger_review"] = adapted["trigger_review"]
    result["review_plan"] = build_review_plan(run, adapted, result)
    result["decision_class"] = result["review_plan"]["decision_class"]
    result["decision_summary"] = result["review_plan"]["decision_summary"]
    result["processing_state"] = ("incomplete" if any(action["code"] in {"provider_failed", "agent_interrupted", "processing_incomplete", "not_indexed", "not_searched"}
                                                      for action in result["review_plan"]["actions"]) else "complete")
    if result.get("status") in {"pass", "pending", "unverifiable"}:
        if result["decision_class"] == "missing_required_data":
            result["status"] = "unverifiable"
            result["classification"] = "missing_required_data"
            result["description"] = "缺少必要资料或范围证明，当前无法核验"
        elif result["decision_class"] == "system_processing_incomplete":
            result["status"] = "failed"
            result["classification"] = "system_processing_incomplete"
            result["description"] = "系统处理失败，未形成业务判定"
        elif result["decision_class"] == "review_required":
            result["status"] = "pending"
            result["classification"] = "review_required"
            result["description"] = "待监察人员核验具体命题与原文"
    if (not result.get("propositions")
            and result["review_plan"]["blocking_layer"] == "evidence_collection"
            and result.get("status") in {"pending", "unverifiable"}):
        result["description"] = "取证未闭合，无法进入业务判定"
    explanation = relation_explanation(result)
    if result.get("status") in {"pending", "unverifiable"}:
        proved = sum(p.get("state") == "true" for p in result.get("propositions", []))
        result["detail"] = (f"本轮 {len(adapted['files'])} 份冻结文件，{len(adapted['evidence'])} 条证据片段，"
                            f"已证明 {proved} 项命题。" +
                            ("尚未建立可判定的事件与命题。" if not result.get("propositions") else "仍有命题未闭合。") +
                            ("资料范围完整性尚未确认。" if not adapted.get("source_complete") else "") +
                            "详见命题与缺口；缺证不代表未实施。")
        result["detail"] = " ".join(filter(None, (result.get("detail"), result["review_plan"]["summary"])))
    if explanation:
        result["detail"] = " ".join(filter(None, (result.get("detail"), explanation)))
    return result
