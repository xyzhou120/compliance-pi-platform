"""Turn execution and evidence gaps into a bounded, actionable review plan."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from .file_outcomes import file_outcomes

_MATERIAL_PROPOSITIONS = {"prevention_daily": ["trigger"], "threshold": ["threshold_trigger"],
                         "dispatch_daily": ["stop", "power_off", "evacuate", "report"],
                         "release": ["stop", "evacuate"], "design": ["relief_measures"],
                         "relief_acceptance": ["relief_measures"]}

_SYSTEM_ACTIONS = {"provider_failed", "agent_interrupted", "processing_incomplete", "not_indexed", "not_searched"}
_MISSING_ACTIONS = {"source_absent", "scope_incomplete", "threshold_profile_unresolved", "unreadable"}


def decision_class(result: dict, actions: list[dict]) -> tuple[str, str]:
    """Separate system/data gaps from a reviewable business conclusion."""
    if result.get("status") == "no_pass" and result.get("findings"):
        return "issue_found", "已发现有证据支持的问题，监察人员可结合引用确认并上报。"
    if result.get("status") == "pass" and not result.get("coverage_gaps"):
        return "no_issue_observed", "必要证据已闭合，本轮未发现问题。"
    codes = {action.get("code") for action in actions}
    if codes & _SYSTEM_ACTIONS:
        return "system_processing_incomplete", "系统处理未完成，当前不是监察待核验；请先补齐处理链路。"
    if codes & _MISSING_ACTIONS:
        return "missing_required_data", "缺少必要资料或范围证明，当前无法核验。"
    if any(action.get("owner") == "business" and action.get("evidence_refs") and action.get("targets") for action in actions):
        return "review_required", "已有具体命题和原文定位，需监察人员复核。"
    return "missing_required_data", "尚无可定位的事件级复核依据，当前无法核验。"


def _sample_targets(rows, files):
    by_id = {file.get("file_id"): file for file in files}
    grouped = defaultdict(list)
    for row in rows:
        if row.get("file_id") in by_id:
            grouped[row["file_id"]].append(row.get("unit_id") or row.get("page"))
    targets = []
    for file_id, units in grouped.items():
        file = by_id.get(file_id, {})
        units = list(dict.fromkeys(str(unit) for unit in units))
        targets.append({"file_id": file_id, "file_name": file.get("original_name") or file_id,
                        "units": units, "unit_count": len(units), "format": file.get("format"),
                        "material_group": file.get("material_group"),
                        "related_propositions": _MATERIAL_PROPOSITIONS.get(file.get("material_group"), [])})
    return targets


def build_review_plan(run: dict, snapshot: dict, result: dict) -> dict:
    files = snapshot.get("files", [])
    audit = result.get("coverage_audit") or snapshot.get("coverage_audit") or {}
    units = audit.get("units") or snapshot.get("coverage", [])
    state_rows = defaultdict(list)
    for unit in units:
        state_rows[str(unit.get("state", "not_indexed"))].append(unit)
    work_units = run.get("work_units", [])
    failed_units = [unit for unit in work_units if unit.get("state") == "failed"]
    running_units = [unit for unit in work_units if unit.get("state") == "running"]
    finalized = bool(run.get("result") or result.get("finalized_at") or run.get("state") in {"completed", "failed"})
    actions = []

    def add(code, title, reason, next_step, rows=(), *, priority="high", owner="system", evidence_refs=(), related_propositions=()):
        rows = list(rows)
        actions.append({"code": code, "priority": priority, "title": title, "reason": reason,
                        "next_step": next_step, "unit_count": len(rows),
                        "owner": owner, "evidence_refs": list(evidence_refs), "related_propositions": list(related_propositions),
                        "targets": _sample_targets(rows, files) if rows else []})

    def work_rows(work):
        return [{"file_id": unit.get("input", {}).get("file_id"),
                 "unit_id": f"page:{unit['input']['page']}" if unit.get("input", {}).get("page") else "document"} for unit in work]
    provider_rows = state_rows.get("provider_failed", [])
    batch_failures = [gap for gap in run.get("gaps", []) if gap.get("reason") in {"parse_file_failed", "recognize_page_failed"}]
    provider_gap = any("provider_failed" in str(gap) for gap in result.get("coverage_gaps", []))
    if provider_rows or failed_units or provider_gap or batch_failures:
        add("provider_failed", "页面处理失败，先修复取证链路",
            "至少一个处理单元未成功生成原文。失败可能发生在解析、渲染或识别阶段，不能据此认定文件内容不存在。",
            "技术人员先检查该页面的处理日志，再创建新执行验证；业务人员无需把系统失败当成矿井未实施。",
            provider_rows or work_rows(failed_units) or [{"file_id": gap.get("file_id"), "unit_id": f"page:{gap['page']}" if gap.get("page") else "document"} for gap in batch_failures])
        actions[-1]["processing_errors"] = [
            {"kind": unit.get("kind"), "file_id": unit.get("input", {}).get("file_id"),
             "page": unit.get("input", {}).get("page"),
             "error_code": unit.get("error"),
             "recorded_reasons": [str(gap.get("reason") or gap.get("error") or "未登记")
                                  for gap in (unit.get("output") or {}).get("gaps", []) if isinstance(gap, dict)]}
            for unit in failed_units]
    pi_failures = [str(gap.get("reason")) for gap in run.get("gaps", [])
                   if str(gap.get("reason", "")).startswith("pi_worker_failed")]
    if pi_failures:
        add("agent_interrupted", "Pi 取证未正常结束",
            f"本轮取证执行器返回技术异常：{pi_failures[0]}。此错误不能作为业务事实。",
            "先按错误码检查模型请求、上下文或证据接口，再创建新执行；本轮结果无法说明全部资料已核验。")
    if running_units:
        add("processing_incomplete", "存在未收敛的页面处理记录" if finalized else "仍有资料正在处理",
            f"账本中 {len(running_units)} 个处理单元仍标记为处理中，不能视为读取成功。",
            "本轮已定版，不会通过等待补齐；恢复技术服务后创建新执行。" if finalized else "等待当前执行结束，再查看其最终处理状态。",
            work_rows(running_units))
    if state_rows.get("unreadable"):
        add("unreadable", "图像或部分可读页面尚未完整转录",
            f"覆盖审计中 {len(state_rows['unreadable'])} 个单元未达到完整可读要求；其中可能已有部分文字，不代表整份文件不可读。",
            "先完成这些页面的识别；识别仍不清晰时，再请业务人员核对具体原文并记录可靠转录。",
            state_rows["unreadable"])
    if state_rows.get("not_indexed"):
        add("not_indexed", "完成尚未解析的资料单元",
            f"{len(state_rows['not_indexed'])} 个资料单元尚未建立索引，系统无法从中检索事件、监测值或处置动作。",
            "由系统完成这些文件/页面的解析与检索；已定版运行需新建执行，不应要求业务人员替代系统逐页阅读。",
            state_rows["not_indexed"], priority="medium")
    if state_rows.get("not_searched"):
        add("not_searched", "已解析内容尚未完成检索",
            f"{len(state_rows['not_searched'])} 个单元已有可读内容，但本轮没有完成所需命题检索。",
            "先完成事件与处置线索检索；没有检索不能证明没有触发或处置记录。", state_rows["not_searched"])
    if state_rows.get("source_absent"):
        add("source_absent", "核对本轮缺少的资料来源",
            "冻结范围中有要求的资料组或文件未提供；这只证明缺少资料，不能证明处置未实施。",
            "按资料清单核对来源，补充后创建新执行。", state_rows["source_absent"], owner="source")
    if state_rows.get("conflicted"):
        add("conflicted", "核对已登记的证据冲突",
            "本轮覆盖审计中存在冲突单元；需要结合资料关系中的双方原文核对具体差异。",
            "打开目标原文并查看资料关系与冲突依据，只复核受影响的命题。", state_rows["conflicted"], owner="business")
    source_complete = (audit.get("source_complete") is True) if audit else snapshot.get("source_complete") is True
    if not source_complete:
        add("scope_incomplete", "确认资料范围完整性",
            "本轮没有形成完整范围证明，当前结果不能代表清单范围内全部资料。",
            "由资料提供方核对矿井、周期和清单范围，按既有策略补齐范围证明；不能据此直接认定业务违规。", priority="medium", owner="source")

    proposition_count = len(result.get("propositions", []))
    trigger_review = result.get("trigger_review") or {}
    comparison = trigger_review.get("threshold_comparison") or {}
    if comparison.get("has_unresolved_conflict"):
        add("threshold_profile_conflict", "适用预警指标版本未唯一确定",
            "多份指标报告对同一监测技术的等级条件或边界存在差异，不能按上传时间自动选一份。",
            "核对批准、生效、修订或替代关系；确认后再执行该监测技术的触发比较。", owner="source")
    if (trigger_review.get("threshold_profiles") or {}).get("status") == "unresolved":
        add("threshold_profile_unresolved", "预警等级定义未形成可执行档案",
            "阈值报告缺少可读取的原文等级条件，或等级条件被截断。",
            "补充清晰指标报告或人工核对原文页；不把 C/D、蓝黄橙红互相映射。", owner="source")
    monitoring = trigger_review.get("monitoring_candidates") or {}
    if monitoring.get("gaps"):
        add("monitoring_candidate_gap", "日报监测值仍是图表/表格候选",
            f"发现 {len(monitoring['candidates'])} 个监测候选，但仍有 {len(monitoring['gaps'])} 个字段或页面缺口；候选不等于触发事实。",
            "核对原文页、探头、地点、数值、时间和班次；没有明确班次不要判连续两个班。", owner="business")
    if proposition_count == 0:
        add("business_rule_not_started", "尚未形成事件级判定依据",
            "本轮没有形成可判定的事件或命题，因此没有产生‘已停工/未停工、已撤人/未撤人、是否卸压’结论。",
            "完成上述取证和证据绑定后，重新核对触发、危险研判、停工断电、撤人报告和卸压命题。")
    evidence = {e.get("evidence_id"): e for e in snapshot.get("evidence", [])}
    for prop in result.get("propositions", []):
        if prop.get("state") not in {"missing", "unreadable", "conflicted", "false"}:
            continue
        refs = [ref for ref in prop.get("evidence_refs", []) if isinstance(ref, str) and ref in evidence]
        rows = [{"file_id": evidence[ref].get("file_id"), "unit_id": "page:" + str(evidence[ref]["locator"]["page"])
                 if (evidence[ref].get("locator") or {}).get("page") else "document"} for ref in refs]
        add("proposition_review", "核对具体命题的证据与适用条件",
            prop.get("detail") or "该命题尚无足够证据或存在明确反证，需逐条核对引用。",
            "核对下方引用的事件、工作面、时间、适用范围及原文；无引用时先补取证，不推断未实施。",
            rows, owner="business", evidence_refs=refs, related_propositions=[prop.get("code")])
        actions[-1]["context"] = {key: prop.get(key) for key in ("event_id", "workface", "state")}

    blocking = "business_rule" if proposition_count else ("acquisition" if actions else "business_review")
    if any(item["owner"] == "system" for item in actions):
        blocking = "evidence_collection"
    counts = {key: len(value) for key, value in state_rows.items()}
    summary = ("本轮取证未闭合，尚未形成事件级判定依据；请先完成系统处理，再判断是否需要业务复核。" if proposition_count == 0
               else "本轮已有部分命题记录，但仍有证据缺口，不能据缺口推断未实施。")
    if result.get("status") == "pass" and not actions:
        summary = "本轮必要命题和证据覆盖已闭合，未记录待处理事项。"
    elif result.get("status") == "no_pass":
        summary = "本轮已记录问题线索；请按问题线索的命题、引用和原文核对具体事实。其他证据缺口独立保留。"
    first_system = next((action for action in actions if action["owner"] == "system" and action["targets"]), None)
    if first_system:
        first = first_system["targets"][0]
        position = "、".join(("第" + unit[5:] + "页") if unit.startswith("page:") else "整份文档" if unit == "document" else unit
                             for unit in first["units"][:3])
        summary += f" 首要处理：{first_system['title']}；{first['file_name']}，{position}。"
    if finalized and running_units:
        summary += " 本轮已定版，未完成的处理记录不会自行补齐，需恢复服务后新建执行。"
    parsed = {unit.get("input", {}).get("file_id") for unit in work_units if unit.get("kind") == "parse_file" and unit.get("state") == "succeeded"}
    decision, decision_summary = decision_class(result, actions)
    return {"blocking_layer": blocking, "summary": summary, "coverage_counts": counts, "file_outcomes": file_outcomes(run, snapshot, result),
            "source_complete": source_complete,
            "decision_class": decision, "decision_summary": decision_summary,
            "actions": actions, "has_propositions": proposition_count > 0, "rule_started": proposition_count > 0,
            "stats": {"frozen_files": len(files), "parsed_files": len(parsed), "evidence_count": len(evidence),
                      "proposition_count": proposition_count, "processing_unfinished": len(running_units)},
            "status_unchanged": result.get("status"), "history_modified": False,
            "limitations": ["资料定位来自本轮冻结文件；处理问题不构成业务违规事实。",
                            "覆盖缺口显示的是未闭合记录数，可能包含同一单元的不同检查记录，不能当作缺少文件数。"]}


def explain_frozen_run(run):
    """Use the stored final audit; never recompute a historical business result."""
    result = run.get("result") or {}
    snapshot = deepcopy(run.get("snapshot") or {})
    stored = result.get("coverage_audit") or {}
    expected = {"run_id": run.get("run_id", run.get("runId")), "manifest_hash": snapshot.get("manifest_hash"),
                "rule_revision": run.get("rule_revision")}
    if stored and any(stored.get(key) not in {None, value} for key, value in expected.items()):
        stored = {}
    snapshot["coverage_audit"] = stored
    snapshot["evidence"] = list({e["evidence_id"]: e for e in snapshot.get("evidence", []) + run.get("evidence", [])}.values())
    safe_result = {**result, "coverage_audit": stored}
    plan = build_review_plan(run, snapshot, safe_result)
    plan["based_on"] = "stored_final_audit" if stored else "frozen_inventory_and_ledger"
    return plan
