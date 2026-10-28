"""Read-only navigation over frozen runs; no model-generated business decisions."""
from __future__ import annotations

from copy import deepcopy
from difflib import unified_diff


_RESULT_LABELS = {"pass": "通过", "no_pass": "不通过", "pending": "待核验",
                  "unverifiable": "无法核验", "failed": "技术失败"}
_PROPOSITION_LABELS = {"true": "已证明", "false": "明确反证", "missing": "缺证",
                       "unreadable": "不可读", "candidate": "候选待验证"}
_FILE_KEYS = ("file_id", "original_name", "content_hash", "document_version", "attachment_id",
              "material_group", "source_role", "report_date", "supersedes", "valid_from", "valid_to")


def _identity(run):
    request, spec = run.get("request") or {}, run.get("spec") or {}
    return {"run_id": run.get("run_id", run.get("runId")),
            "mine_id": run.get("mine_id") or run.get("mineId") or request.get("mineId") or spec.get("mine_code") or spec.get("mine_id"),
            "item_code": run.get("item_code") or run.get("itemCode") or spec.get("item_code")}


def _revision(run):
    plan = run.get("plan") or {}
    return plan.get("planRevision") or run.get("rule_revision") or run.get("ruleRevision") or plan.get("revision")


def _snapshot(run, snapshot=None):
    identity = _identity(run)
    source = deepcopy(snapshot if snapshot is not None else run.get("snapshot") or {})
    for key in ("run_id", "mine_id"):
        if identity[key] and source.get(key) not in (None, identity[key]):
            raise ValueError("snapshot owner mismatch")
    if identity["mine_id"] and source.get("mine_code") not in (None, identity["mine_id"]):
        raise ValueError("snapshot owner mismatch")
    files = {file.get("file_id"): file for file in source.get("files", [])}
    records = {}
    for record in [*source.get("evidence", []), *run.get("evidence", [])]:
        for key in ("run_id", "mine_id"):
            if identity[key] and record.get(key) not in (None, identity[key]):
                raise ValueError("evidence owner mismatch")
        if identity["mine_id"] and record.get("mine_code") not in (None, identity["mine_id"]):
            raise ValueError("evidence owner mismatch")
        if record.get("file_id") not in files:
            raise ValueError("evidence outside frozen files")
        expected = files[record["file_id"]].get("content_hash")
        if expected and record.get("content_hash") != expected:
            raise ValueError("evidence version mismatch")
        evidence_id = record.get("evidence_id")
        if not evidence_id:
            continue
        previous = records.get(evidence_id)
        if previous is not None and (previous.get("content_hash"), previous.get("locator"),
                                     previous.get("text", previous.get("quote"))) != (
                record.get("content_hash"), record.get("locator"), record.get("text", record.get("quote"))):
            raise ValueError("evidence identity conflict")
        records[evidence_id] = deepcopy(record)
    source["evidence"] = list(records.values())
    return source


def _refs(records):
    refs = set()
    for record in records:
        for ref in record.get("evidence_refs", []):
            value = ref if isinstance(ref, str) else ref.get("evidence_id")
            if value:
                refs.add(value)
    return refs


def _public_evidence(record, files):
    text = str(record.get("quote") or record.get("text") or "")
    file = files.get(record.get("file_id"), {})
    return {key: deepcopy(record.get(key)) for key in (
        "evidence_id", "file_id", "document_id", "document_version", "content_hash", "locator",
        "source_role", "quality", "origin_document_version")} | {
            "original_name": file.get("original_name", record.get("file_id")),
            "quote": text[:1200], "truncated": len(text) > 1200}


def _citations(snapshot, refs):
    registry = {record["evidence_id"]: record for record in snapshot.get("evidence", [])}
    files = {file.get("file_id"): file for file in snapshot.get("files", [])}
    valid = sorted(set(refs) & registry.keys())
    return valid, [_public_evidence(registry[ref], files) for ref in valid], sorted(set(refs) - registry.keys())


def expand_event_context(run: dict, snapshot: dict, event_id: str) -> dict:
    """Expand only an explicit event in this frozen run, with registered references."""
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("event_id is required")
    source = _snapshot(run, snapshot)
    result = run.get("result") or {}
    propositions = [deepcopy(p) for p in result.get("propositions", []) if p.get("event_id") == event_id]
    facts = [deepcopy(f) for f in source.get("facts", []) if f.get("event_id") == event_id]
    links = [deepcopy(link) for link in result.get("association_links", source.get("association_links", []))
             if link.get("event_id") == event_id]
    evidence_refs = _refs([*propositions, *facts])
    candidates = []
    for envelope in run.get("candidates", source.get("candidates", [])):
        candidate = envelope.get("candidate", envelope)
        if (candidate.get("event_id") or candidate.get("fields", {}).get("event_id")) == event_id:
            candidates.append(deepcopy(envelope))
            evidence_refs.update(_refs([candidate]))
    refs, citations, missing = _citations(source, evidence_refs)
    gaps = [deepcopy(gap) for gap in result.get("coverage_gaps", [])
            if (isinstance(gap, dict) and gap.get("event_id") == event_id)
            or (isinstance(gap, str) and (gap.startswith(event_id + "/") or gap.startswith(event_id + ":")))]
    return {**_identity(run), "rule_revision": _revision(run), "event_id": event_id,
            "found": bool(propositions or facts or links or candidates), "propositions": propositions,
            "facts": facts, "association_links": links, "candidates": candidates, "gaps": gaps,
            "evidence_refs": refs, "evidence": citations,
            "limitations": (["存在未登记引用，未作为可定位证据返回"] if missing else [])}


def compare_versions(snapshot: dict, file_ids: list[str]) -> dict:
    """Compare frozen versions without choosing an authoritative version."""
    if not isinstance(file_ids, list) or not 2 <= len(file_ids) <= 32 or any(not isinstance(i, str) for i in file_ids):
        raise ValueError("file_ids must contain 2 to 32 identifiers")
    if len(set(file_ids)) != len(file_ids):
        raise ValueError("duplicate file identifiers")
    files = {file.get("file_id"): file for file in snapshot.get("files", [])}
    if any(file_id not in files for file_id in file_ids):
        raise ValueError("file outside frozen snapshot")
    selected = [files[file_id] for file_id in file_ids]
    texts = {file_id: "\n".join(str(r.get("text") or r.get("quote") or "") for r in snapshot.get("evidence", [])
                                if r.get("file_id") == file_id or file_id in r.get("source_file_ids", []))
             for file_id in file_ids}
    origin = selected[0]
    comparisons = []
    for other in selected[1:]:
        same = bool(origin.get("content_hash") and origin.get("content_hash") == other.get("content_hash"))
        diff = list(unified_diff(texts[origin["file_id"]].splitlines(), texts[other["file_id"]].splitlines(),
                                 fromfile=origin["file_id"], tofile=other["file_id"], lineterm="", n=2))
        comparisons.append({"before_file_id": origin["file_id"], "after_file_id": other["file_id"],
                            "same_content": same, "independent_corroboration": False if same else None,
                            "text_compared": bool(texts[origin["file_id"]] and texts[other["file_id"]]),
                            "text_difference": diff[:200], "difference_truncated": len(diff) > 200})
    refs = set(record["evidence_id"] for record in snapshot.get("evidence", [])
                if record.get("evidence_id") and (record.get("file_id") in file_ids
                    or set(record.get("source_file_ids", [])) & set(file_ids)))
    return {"files": [{key: deepcopy(file.get(key)) for key in _FILE_KEYS if key in file} for file in selected],
            "comparisons": comparisons, "evidence_refs": sorted(refs), "authority_selected": False,
            "limitations": ["相同字节及其OCR、摘要是同源派生物，不能算独立佐证",
                            "不同字节或时间先后不能单独决定权威版本；需来源作废、替代或有效期证据",
                            "文本差异只覆盖已登记证据；未解析页面不因没有差异而视为相同"]}


def _indexed_changes(before, after):
    return {"added": [deepcopy(after[key]) for key in sorted(after.keys() - before.keys())],
            "removed": [deepcopy(before[key]) for key in sorted(before.keys() - after.keys())],
            "changed": [{"key": list(key) if isinstance(key, tuple) else key,
                         "before": deepcopy(before[key]), "after": deepcopy(after[key])}
                        for key in sorted(before.keys() & after.keys()) if before[key] != after[key]]}


def compare_runs(before: dict, after: dict) -> dict:
    """Describe changes; comparison never mutates or reruns either inspection."""
    left, right = _identity(before), _identity(after)
    if any(not left[key] or left[key] != right[key] for key in ("mine_id", "item_code")):
        raise ValueError("run comparison requires the same mine and inspection item")
    old, new = _snapshot(before), _snapshot(after)
    def file_index(snapshot):
        return {file["file_id"]: {key: deepcopy(file.get(key)) for key in _FILE_KEYS if key in file}
                for file in snapshot.get("files", []) if file.get("file_id")}
    def proposition_index(run):
        return {(str(p.get("event_id") or ""), str(p.get("workface") or ""), str(p.get("code") or "")):
                {"state": p.get("state"), "evidence_refs": sorted(_refs([p])), "detail": p.get("detail", "")}
                for p in (run.get("result") or {}).get("propositions", [])}
    return {"before_run_id": left["run_id"], "after_run_id": right["run_id"],
            "mine_id": left["mine_id"], "item_code": left["item_code"],
            "rule_changed": _revision(before) != _revision(after),
            "before_rule_revision": _revision(before), "after_rule_revision": _revision(after),
            "files": _indexed_changes(file_index(old), file_index(new)),
            "propositions": _indexed_changes(proposition_index(before), proposition_index(after)),
            "before_status": (before.get("result") or {}).get("status"),
            "after_status": (after.get("result") or {}).get("status"),
            "history_modified": False, "limitations": ["状态差异不自动归因于规则或资料变化，需逐项核对引用"]}


def answer_question(run: dict, snapshot: dict, question: str, *, proposition_id: str | None = None) -> dict:
    """Answer supported inspection questions from existing records only."""
    if not isinstance(question, str) or not question.strip() or len(question) > 2000:
        raise ValueError("question must contain 1 to 2000 characters")
    source = _snapshot(run, snapshot)
    result = run.get("result") or {}
    refs, limitations, propositions = set(), [], []
    kind = "inspection_record"
    if any(term in question for term in ("新资料", "新材料", "后来上传", "最新上传")):
        answer = "当前回答仅覆盖该运行的冻结资料，无法据此确认冻结后是否新增材料。纳入新材料需要创建新的执行。"
        limitations.append("未查询全局来源目录，不声明新材料存在或不存在")
        kind = "scope_notice"
    else:
        all_props = result.get("propositions", [])
        propositions = [p for p in all_props if p.get("code") == proposition_id] if proposition_id else []
        if not proposition_id:
            propositions = [p for p in all_props if str(p.get("code") or "") and str(p["code"]) in question]
        if proposition_id and not propositions:
            answer = "本次冻结运行没有该命题的判定记录，不能推断命题成立或不成立。"
            limitations.append("未登记命题")
        elif propositions:
            answer = "；".join(f"命题 {p.get('code')}（{p.get('event_id') or '事件未绑定'}）："
                               f"{_PROPOSITION_LABELS.get(p.get('state'), p.get('state') or '尚未判定')}。"
                               f"{p.get('detail') or ''}" for p in propositions)
            refs = _refs(propositions)
            kind = "proposition_record"
        elif any(term in question for term in ("版本", "来源", "原文", "文件")):
            files = source.get("files", [])
            answer = "本轮冻结来源：" + "；".join(f"{f.get('original_name') or f.get('file_id')}，"
                         f"SHA256 {f.get('content_hash') or '未登记'}" for f in files[:20]) if files else "本轮尚无冻结文件记录。"
            refs = {r["evidence_id"] for r in source.get("evidence", []) if r.get("evidence_id")}
            limitations.append("来源版本绑定只能证明引用出处，不单独证明业务事实为真")
            kind = "source_record"
        elif any(term in question.lower() for term in ("为什么", "缺口", "缺证", "待核验", "pending", "状态", "结果")):
            status = result.get("status")
            gaps = result.get("coverage_gaps") or source.get("coverage_gaps") or []
            answer = f"本轮记录状态：{_RESULT_LABELS.get(status, status or '尚未定版')}。"
            if result.get("description"):
                answer += str(result["description"]) + "。"
            answer += "未闭合项：" + "；".join(str(g) for g in gaps[:20]) if gaps else "当前记录未列出缺口；这不构成额外的完整性证明。"
            refs = _refs([result, *all_props])
        else:
            answer = "目前支持查询运行状态与缺口、具体命题、冻结来源版本和新资料范围。该问题尚无可直接引用的记录答案。"
            limitations.append("没有对任意自然语言问题进行业务推断")
            kind = "unsupported_question"
    valid, citations, missing = _citations(source, refs)
    if missing:
        limitations.append("记录包含未登记引用，未作为可定位证据返回")
    if not valid:
        limitations.append("本回答没有可定位的原文引用，不应作为新增业务事实")
    return {**_identity(run), "rule_revision": _revision(run), "question": question,
            "answer": answer, "knowledge_kind": kind, "evidence_refs": valid, "citations": citations,
            "limitations": list(dict.fromkeys(limitations)), "business_status_generated": False}


def compare_rule_plans(before: dict, after: dict) -> dict:
    """Prepare rule-change review without applying it to any historical result."""
    if not before.get("itemCode") or before.get("itemCode") != after.get("itemCode"):
        raise ValueError("plan comparison requires the same inspection item")
    ignored = {"revision", "planRevision", "assetRevision", "evaluatorRevision", "additionalEvaluatorRevision"}
    keys = sorted((before.keys() | after.keys()) - ignored)
    changed = [{"field": key, "before": deepcopy(before.get(key)), "after": deepcopy(after.get(key))}
               for key in keys if before.get(key) != after.get(key)]
    revision_changed = before.get("planRevision", before.get("revision")) != after.get("planRevision", after.get("revision"))
    return {"item_code": before["itemCode"], "before_revision": before.get("planRevision", before.get("revision")),
            "after_revision": after.get("planRevision", after.get("revision")), "changes": changed,
            "evaluator_changed": before.get("evaluatorRevision") != after.get("evaluatorRevision"),
            "requires_new_execution": bool(changed or revision_changed or before.get("evaluatorRevision") != after.get("evaluatorRevision")),
            "replay": {"mode": "new_execution_only", "history_modified": False,
                       "instruction": "使用新executionId对冻结样本回放，再用compare_runs核对命题与引用差异；不覆盖历史结果"}}
