"""Frozen source relationships; byte differences never prove a contradiction."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from hashlib import sha256
import json
from pathlib import PurePath
import re
import unicodedata

from .units import quantity

POLICY_REVISION = "source-relations-v1"
_KINDS = ("duplicate", "parallel", "version_candidate", "fact_conflict")
_BOOL_FIELDS = {"immediate", "route_conforms", "hazard_confirmed", "acceptance_passed"}
_ACTION_CODES = {
    "stop": ["stop", "stop_immediate", "stop_and_power_off", "continued_before_stop"],
    "power_off": ["power_off", "power_off_immediate", "stop_and_power_off"],
    "evacuate": ["evacuate", "evacuation_executed"],
    "report": ["report", "dispatch_reported"],
    "relief": ["relief", "relief_measures", "relief_violation", "continued_after_trigger", "relief_completed", "relief_parameters_acceptance"],
    "hazard_analysis": ["hazard_analysis", "trigger", "danger_confirmed", "warning_danger_confirmed", "relief_required", "relief_violation", "continued_after_trigger"],
}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _meta(file, key):
    return file.get(key) or (file.get("source_metadata") or {}).get(key)


def source_identity(file):
    """Keep report time separate from filename coverage hints and trusted keys."""
    name = unicodedata.normalize("NFKC", str(file.get("original_name") or ""))
    stem = PurePath(name).stem
    # These are only hints; do not discard arbitrary numbers or workface names.
    title = re.sub(r"[（(](?:新|新版|最新版|修改版|修改最新版|修订版|修订|修改)[）)]", "", stem).strip()
    periods = re.findall(r"(?:\d{4}年)?\d{1,2}月\d{1,2}日(?:\s*[-—至~]\s*(?:\d{1,2}月)?\d{1,2}日)?", name)
    return {"file_id": file.get("file_id"), "original_name": file.get("original_name") or file.get("file_id"),
            "content_hash": file.get("content_hash"), "material_group": file.get("material_group"),
            "report_date": file.get("report_date"), "logical_source_key": _meta(file, "logical_source_key"),
            "coverage_period": _meta(file, "coverage_period"), "scope_key": _meta(file, "scope_key"),
            "filename_period_hints": periods, "filename_identity_hint": title,
            "identity_basis": "source_metadata" if _meta(file, "logical_source_key") else "filename_hint_only"}


def _relation(kind, files, reason, **extra):
    ordered = sorted(files, key=lambda f: str(f.get("file_id")))
    value = {"kind": kind, "file_ids": [f["file_id"] for f in ordered],
             "files": [source_identity(f) for f in ordered], "reason": reason,
             "evidence_refs": [], "affected_propositions": [], "blocks_verdict": False, **extra}
    value["relation_id"] = sha256(_canonical(value).encode()).hexdigest()[:24]
    return value


def _precise_time(value):
    if not isinstance(value, str) or not re.search(r"[T ]\d{2}:\d{2}", value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def _fact_slots(snapshot):
    """Comparable verified fields only; missing/unknown values are never denial."""
    evidence = {e["evidence_id"]: e for e in snapshot.get("evidence", []) if e.get("evidence_id")}
    for fact in snapshot.get("facts", []):
        refs = fact.get("evidence_refs") or []
        role = fact.get("source_role")
        if (role not in {"actual_record", "requirement"} or not refs
                or any(not isinstance(ref, str) or ref not in evidence for ref in refs)
                or fact.get("verified") is False or fact.get("status") == "candidate"
                or fact.get("validation_status") in {"candidate", "source_bound"}
                or fact.get("quality") in {"candidate", "model_candidate", "conflicted_review"}
                or any(evidence[ref].get("source_role") != role
                       or evidence[ref].get("quality") in {"candidate", "model_candidate", "conflicted_review"} for ref in refs)):
            continue
        event, face = fact.get("event_id"), fact.get("workface")
        if not event or not face:
            continue
        fields = fact.get("fields") or {}
        when = _precise_time(fact.get("time"))
        if fact.get("kind") == "action" and role == "actual_record" and when:
            action = fields.get("action")
            if action not in _ACTION_CODES or fields.get("target_scope") != face:
                continue
            base = (event, face, when, action, fields["target_scope"])
            if fields.get("status") in {"done", "not_done"}:
                yield base + ("status",), fields["status"], fact, _ACTION_CODES[action]
            for field in sorted(_BOOL_FIELDS):
                # Compare completion attributes only when both claim completion.
                if type(fields.get(field)) is bool and fields.get("status") == "done":
                    yield base + (field,), fields[field], fact, _ACTION_CODES[action]
        elif fact.get("kind") == "threshold" and role == "requirement" and fields.get("approved") is True:
            required = ("method", "metric", "point", "valid_from", "valid_to", "unit", "value")
            if any(fields.get(key) in (None, "") for key in required):
                continue
            try:
                start = datetime.fromisoformat(str(fields["valid_from"]).replace("Z", "+00:00"))
                end = datetime.fromisoformat(str(fields["valid_to"]).replace("Z", "+00:00"))
                if end < start:
                    continue
                converted = quantity(fields["value"], fields["unit"])
            except (ValueError, TypeError):
                continue
            if converted is None:
                continue
            dimension, value, scale = converted
            base = (event, face, start.isoformat(), end.isoformat(), fields["method"], fields["metric"], fields["point"],
                    fields.get("grade", fields.get("risk_grade")), dimension, scale, "value")
            yield base, str(value.normalize()), fact, ["trigger", "threshold_trigger", "applicable_threshold", "threshold_applicable"]


def analyze_source_relations(snapshot):
    """Pure audit: file relationships are advisory; contradictions cite facts."""
    files = {f["file_id"]: f for f in snapshot.get("files", []) if f.get("file_id")}
    relations = []
    groups = defaultdict(list)
    for file in files.values():
        if file.get("content_hash"):
            groups[file["content_hash"]].append(file)
    for grouped in groups.values():
        if len(grouped) > 1:
            relations.append(_relation("duplicate", grouped, "文件字节相同，保留所有附件出处，不作为独立佐证。"))
    groups = defaultdict(list)
    for file in files.values():
        identity = source_identity(file)
        logical = identity["logical_source_key"]
        title = identity["filename_identity_hint"]
        # Never use a common report date as logical identity.
        if logical or title:
            groups[_canonical((file.get("material_group"), "logical" if logical else "filename", logical or title))].append(file)
    for grouped in groups.values():
        if len({f.get("content_hash") for f in grouped}) > 1:
            relations.append(_relation("version_candidate", grouped,
                "逻辑资料标识或文件名线索相近，但字节不同；尚无替代或有效版本证明，全部保留，不自动选择最新版。"))
    groups = defaultdict(list)
    for file in files.values():
        if file.get("material_group"):
            groups[file["material_group"]].append(file)
    for grouped in groups.values():
        if len(grouped) > 1:
            relations.append(_relation("parallel", grouped,
                "同类别允许多份资料并存；报告日期相同、期间不同或字节不同均不单独构成冲突。"))

    slots = defaultdict(list)
    for key, value, fact, codes in _fact_slots(snapshot):
        slots[key].append((value, fact, codes))
    evidence = {e["evidence_id"]: e for e in snapshot.get("evidence", []) if e.get("evidence_id")}
    for key, records in slots.items():
        if len({_canonical(value) for value, _, _ in records}) < 2:
            continue
        refs = sorted({ref for _, fact, _ in records for ref in fact["evidence_refs"]})
        ids = sorted({evidence[ref].get("file_id") for ref in refs if evidence[ref].get("file_id") in files})
        if len(ids) < 2 or len({files[fid].get("content_hash") for fid in ids}) < 2:
            continue
        differences = [{"fact_id": fact.get("fact_id"), "value": (fact.get("fields") or {}).get(key[-1]),
                        "unit": (fact.get("fields") or {}).get("unit"), "evidence_refs": fact["evidence_refs"]}
                       for _, fact, _ in records]
        citations = [{"evidence_id": ref, "file_id": evidence[ref].get("file_id"),
                      "locator": evidence[ref].get("locator", {}),
                      "quote": str(evidence[ref].get("text") or evidence[ref].get("quote") or "")[:600]}
                     for ref in refs]
        context = {"event_id": key[0], "workface": key[1], "field": key[-1]}
        if key[-1] == "value":
            context.update({"valid_from": key[2], "valid_to": key[3], "method": key[4], "metric": key[5], "point": key[6], "grade": key[7]})
        else:
            context.update({"time": key[2], "action": key[3], "target_scope": key[4]})
        relations.append(_relation("fact_conflict", [files[fid] for fid in ids],
            "同一事件、工作面及相同时间或有效区间的同一字段存在不一致的已验证记录。",
            context=context,
            evidence_refs=refs, differences=differences, citations=citations,
            proposition_codes=sorted({code for _, _, codes in records for code in codes})))
    counts = Counter(row["kind"] for row in relations)
    return {"policy_revision": POLICY_REVISION, "counts": {kind: counts[kind] for kind in _KINDS},
            "relations": relations, "authority_selected": False}


def relation_explanation(result):
    """Describe stored relationships without changing any historical verdict."""
    audit = result.get("source_relations") or {}
    rows = audit.get("relations", [])
    if not audit:
        return ""
    conflicts = [row for row in rows if row.get("kind") == "fact_conflict" and row.get("blocks_verdict")]
    names = {"status": "执行状态", "value": "预警临界值", "hazard_confirmed": "危险确认",
             "immediate": "是否立即处置", "route_conforms": "撤人路线符合性", "acceptance_passed": "验收结果"}
    values = {"done": "已实施", "not_done": "明确未实施", "True": "是", "False": "否"}
    pieces = []
    for row in conflicts[:5]:
        context = row.get("context", {})
        sources = "、".join(str(file.get("original_name") or file.get("file_id")) for file in row.get("files", []))
        recorded = " / ".join(str(values.get(str(item.get("value")), item.get("value"))) +
                              (" " + str(item["unit"]) if item.get("unit") else "") for item in row.get("differences", []))
        scope = context.get("time") or (f"有效期 {context.get('valid_from')} 至 {context.get('valid_to')}"
                                         if context.get("valid_from") else "时间未登记")
        actions = {"stop": "停工", "power_off": "断电", "evacuate": "撤人", "report": "报告调度", "relief": "卸压", "hazard_analysis": "危险研判"}
        action = f"，动作 {actions.get(context['action'], context['action'])}" if context.get("action") else ""
        pieces.append(f"{context.get('workface')}，事件 {context.get('event_id')}，{scope}{action}，"
                      f"{names.get(context.get('field'), context.get('field'))}：{recorded}；来源：{sources}。")
    if conflicts:
        pieces.append("以上记录对应同一事实条件，需逐条核对原文；其他命题仍按各自证据判断。")
    candidates = [row for row in rows if row.get("kind") == "version_candidate"]
    if candidates:
        pieces.append(f"另有 {len(candidates)} 组可能的不同版本，仅作比对提示，尚未选择权威版本。")
    return " ".join(pieces)
