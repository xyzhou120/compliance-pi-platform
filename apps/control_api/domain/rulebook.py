"""Immutable, source-traceable draft rulebook versions (not an arbitrary DSL)."""

from hashlib import sha256
from pathlib import Path
import json


DEFAULT_RULEBOOK_VERSION = "stop-work-unload-scope-v3"
_VERSIONS = {
    "impact-1023-draft-v1": "impact_1023_draft_v1.json",
    "impact-1023-draft-v2": "impact_1023_draft_v2.json",
    DEFAULT_RULEBOOK_VERSION: "stop_work_unload_scope_v3.json",
}

# Keep draft-wide completeness separate from the conditions already evaluated.
# These cannot be satisfied by merely finding a done/role label in a table.
_UNCOVERED = {
    "stop_work_unload": [
        {"id": "risk_and_design_scope", "cells": ["D2", "F2"], "detail": "危险性评价与防冲设计适用范围尚未完整核验"},
        {"id": "relief_parameters_acceptance", "cells": ["E2", "F2"], "detail": "卸压类型、参数、施工范围和验收实质尚未完整核验"},
        {"id": "personnel_and_equipment", "cells": ["E2"], "detail": "解危人员限制、撤离覆盖及无关设备停运尚未完整核验"},
        {"id": "signatures_and_notification", "cells": ["E2"], "detail": "研判签字和告知记录尚未完整核验"},
    ],
    "relief_effects": [
        {"id": "risk_and_design_scope", "cells": ["D3", "F3", "G3"], "detail": "真实设计和检验任务的适用范围尚未完整核验"},
        {"id": "signature_evidence", "cells": ["E3", "F3"], "detail": "技术确认签字或电子签章证据尚未核验，角色名称不等于已签署"},
    ],
}


def load_rulebook(version=DEFAULT_RULEBOOK_VERSION):
    if version not in _VERSIONS:
        raise ValueError("unsupported rulebook version")
    raw = (Path(__file__).parent / "data" / _VERSIONS[version]).read_bytes()
    document = json.loads(raw)
    if document.get("version") != version or document.get("status") != "development-draft":
        raise ValueError("invalid registered draft rulebook")
    document["revision"] = sha256(raw).hexdigest()
    return document


def rulebook_reference(rulebook, item_code):
    checks = [check for check in rulebook["checks"] if check["item_code"] == item_code]
    if len(checks) != 1:
        raise ValueError("rulebook has no unique definition for item")
    check = checks[0]
    return {
        "version": rulebook["version"], "revision": rulebook["revision"], "status": rulebook["status"],
        "source": rulebook["source"], "item_code": item_code, "name": check["name"],
        "source_cells": [f"{rulebook['source']['sheet']}!{cell}" for cell in check["cells"]],
        "provisional_execution_notes": rulebook["execution_notes"],
    }


def apply_draft_scope(result, snapshot, item_code, *, implemented_ids=()):
    """Expose incomplete capabilities and candidate joins without overstating PASS."""
    from copy import deepcopy
    result["implemented_condition_status"] = result["status"]
    implemented = set(implemented_ids)
    uncovered = [] if (item_code == "stop_work_unload" and (snapshot.get("rule_baseline") or {}).get("version") == "stop-work-unload-scope-v3") else [entry for entry in deepcopy(_UNCOVERED[item_code]) if entry["id"] not in implemented]
    result["rule_coverage"] = {"full_rule_check_complete": False,
                               "all_required_checks_implemented": not uncovered,
                               "uncovered_conditions": uncovered, "implemented_checks": sorted(implemented)}
    candidates = [link for link in snapshot.get("association_links", []) if link.get("temporal_partition")]
    facts = {fact["fact_id"]: fact for fact in snapshot.get("facts", [])}
    candidate_refs = {ref for link in candidates for ref in facts.get(link["fact_id"], {}).get("evidence_refs", [])}
    result["association_assessment"] = {
        "candidate_association_used": bool(candidates),
        "candidate_link_count": len(candidates),
        "limitations": list(dict.fromkeys(text for link in candidates for text in link.get("limitations", []))),
    }
    candidate_findings = [f for f in result.get("findings", []) if set(f["evidence_refs"]) & candidate_refs]
    if candidate_findings:
        result["candidate_findings"] = candidate_findings
        result["findings"] = [f for f in result["findings"] if f not in candidate_findings]
    gaps = result.setdefault("coverage_gaps", [])
    gaps.extend("未覆盖规则条件: " + entry["detail"] for entry in uncovered)
    if candidates:
        gaps.append("跨文件时间关联仍是候选，尚无独立事件归属证明")
    result["rule_coverage"]["full_rule_check_complete"] = (
        not uncovered and not candidates and not gaps
        and bool(result.get("propositions"))
        and all(p.get("state") in {"true", "false"} for p in result["propositions"])
    )
    if (result["status"] == "pass" and (uncovered or candidates)) or (candidate_findings and not result["findings"]):
        result["status"] = "pending"
        result["description"] = "已实现条件已执行，整项草案仍有未覆盖条件或关联缺口"
    result["detail"] += " 原表整项覆盖、已实现条件结果和候选关联分别记录；未覆盖条件不作为整项合规证明。"
    return result
