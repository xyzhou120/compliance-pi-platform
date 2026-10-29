"""Per-file processing outcomes, kept distinct from compliance verdicts."""
from collections import Counter, defaultdict


def file_outcomes(run, snapshot, result):
    files = snapshot.get("files", [])
    evidence = {item["evidence_id"]: item for item in snapshot.get("evidence", []) if item.get("evidence_id")}
    audit = result.get("coverage_audit") or snapshot.get("coverage_audit") or {}
    coverage = defaultdict(list)
    for unit in audit.get("units") or snapshot.get("coverage", []):
        coverage[unit.get("file_id")].append(unit)
    work = defaultdict(list)
    for unit in run.get("work_units", []):
        work[unit.get("input", {}).get("file_id")].append(unit)
    batch_errors = defaultdict(list)
    for gap in run.get("gaps", []):
        if gap.get("reason") in {"parse_file_failed", "recognize_page_failed"} and gap.get("file_id"):
            batch_errors[gap["file_id"]].append(gap)
    refs_by_file = defaultdict(list)
    for item in evidence.values():
        for file_id in set([item.get("file_id")] + item.get("source_file_ids", [])):
            refs_by_file[file_id].append(item)
    findings_by_file = defaultdict(list)
    for finding in result.get("findings", []):
        for ref in finding.get("evidence_refs", []):
            item = evidence.get(ref, {})
            if item.get("file_id"):
                findings_by_file[item["file_id"]].append({key: finding.get(key) for key in ("code", "event_id", "workface", "detail", "evidence_refs")})
    rows = []
    for file in files:
        fid = file["file_id"]
        failures = [unit for unit in work[fid] if unit.get("state") == "failed"]
        active = [unit for unit in work[fid] if unit.get("state") in {"queued", "running"}]
        native = [unit for unit in work[fid] if unit.get("kind") == "parse_file" and unit.get("state") == "succeeded"]
        states = Counter(unit.get("state") for unit in coverage[fid])
        candidate_count = sum(item.get("quality") == "model_candidate" for item in refs_by_file[fid])
        reasons = []
        for unit in failures:
            data, output = unit.get("input", {}), unit.get("output") or {}
            reasons.append({"stage": unit.get("kind"), "page": data.get("page"), "reason": unit.get("error") or "processing_failed",
                            "details": [{key: gap.get(key) for key in ("reason", "error", "http_status") if gap.get(key) is not None}
                                        for gap in output.get("gaps", []) if isinstance(gap, dict)]})
        reasons.extend({"stage": gap.get("reason"), "page": gap.get("page"), "reason": gap.get("error") or gap["reason"]}
                       for gap in batch_errors[fid])
        if findings_by_file[fid]:
            status, description = "business_issue", "存在有引用的问题线索"
        elif failures or batch_errors[fid] or states.get("provider_failed") or states.get("failed"):
            status, description = "processing_failed", "部分处理失败，已有有效证据继续保留" if refs_by_file[fid] else "处理失败，未取得可用证据"
        elif active:
            status, description = "unfinished", "处理未完成，未标记为已核查"
        elif candidate_count:
            status, description = "candidate_review", "识别文本为候选，尚不能直接作为业务事实"
        elif states.get("unreadable") or states.get("partial") or states.get("unsupported"):
            status, description = "needs_recognition", "有页面尚未完整识别"
        elif states.get("not_searched"):
            status, description = "needs_search", "原文已解析，尚未完成本项命题检索"
        elif states.get("not_indexed"):
            status, description = "unprocessed", "仍有单元未解析，不能视为文件已核查"
        elif native or refs_by_file[fid]:
            status, description = "processed", "已解析；未记录该文件支持的问题线索，不等于整项合规"
        else:
            status, description = "unprocessed", "本轮未处理，不能视为已检查"
        problem_units = [unit["unit_id"] for unit in coverage[fid] if unit.get("state") not in {"readable", "empty", "searched_hit", "searched_no_hit"} and unit.get("unit_id")]
        rows.append({"file_id": fid, "file_name": file.get("original_name") or fid, "format": file.get("format"),
                     "status": status, "description": description, "evidence_count": len(refs_by_file[fid]),
                     "candidate_evidence_count": candidate_count, "unit_states": dict(states), "problem_units": problem_units,
                     "failures": reasons, "findings": findings_by_file[fid], "notable": status != "processed"})
    return {"counts": dict(Counter(row["status"] for row in rows)), "items": rows,
            "total": len(rows), "notable_count": sum(row["notable"] for row in rows),
            "processed_does_not_mean_compliant": True}
