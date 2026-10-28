"""Conservative, provenance-preserving extraction of compliance facts.

This module deliberately does not try to understand arbitrary prose.  A fact is
emitted only when its source is an actual record (or, for a threshold/requirement,
an explicit requirement) and the identifying fields are present in the same
block.  Everything else is retained as a traceable candidate or a gap.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any


ROLES = {"actual_record", "requirement", "statement", "regulation"}
EVENT_TYPES = {
    "动力现象": "dynamic", "强烈震动": "dynamic", "巨响": "dynamic",
    "瞬间底鼓": "dynamic", "煤岩弹射": "dynamic", "动力": "dynamic",
    "预警": "warning", "预警事件": "warning",
}
ACTION_TYPES = {
    "停工": "stop", "停止作业": "stop", "停电": "power_off", "断电": "power_off",
    "撤人": "evacuate", "撤离": "evacuate", "报告": "report", "上报": "report",
    "解危": "relief", "卸压": "relief", "检验": "inspection", "检查": "inspection",
    "技术确认": "technical_confirm", "确认": "technical_confirm", "调度放行": "dispatch_release",
    "放行": "dispatch_release", "复工": "resume", "继续作业": "continue_work",
    "继续采掘": "continue_work", "实际采掘": "continue_work", "恢复采掘": "resume", "危险研判": "hazard_analysis",
}
FIELD_ALIASES = {
    "event_id": ("事件编号", "事件id", "事件 ID", "编号", "事件"),
    "workface": ("工作面", "地点", "作业地点", "位置"),
    "time": ("时间", "日期", "发生时间", "记录时间"),
    "method": ("方法", "监测方法", "检验方法", "解危方法"),
    "metric": ("指标", "监测指标", "指标名称"),
    "point": ("测点", "监测点", "测点名称"),
    "value": ("数值", "实测值", "值", "结果"),
    "unit": ("单位",),
    "valid_from": ("有效起始", "有效期起", "起始时间"),
    "valid_to": ("有效截止", "有效期止", "截止时间"),
    "approved": ("批复", "已批准", "审批", "批准"),
    "action": ("动作", "行动", "措施"),
    "status": ("状态", "执行状态", "是否完成"),
    "actor_role": ("执行人角色", "执行人", "责任人角色"),
    "target_scope": ("目标范围", "范围", "目标"),
    "route_conforms": ("路线符合", "路线是否符合"),
    "immediate": ("立即", "是否立即"),
    "hazard_confirmed": ("危险研判", "危险确认", "研判结果"),
    "trigger_type": ("记录类型", "类型", "事件类型"),
    "requirements": ("指标全集",),
    "stage": ("阶段", "测量阶段", "监测阶段"),
    "impact_risk": ("冲击危险",),
    "risk_grade": ("危险等级",),
    "technical_head": ("技术负责人",),
    "relief_methods": ("解危方法全集",),
    "relief_parameters": ("解危参数要求",),
    "relief_area": ("解危区域",),
    "withdrawal_area": ("撤离区域",),
    "max_personnel": ("解危限员",),
    "unrelated_equipment_stop_required": ("无关设备停运要求",),
    "parameters": ("施工参数",),
    "acceptance_passed": ("验收通过",),
    "acceptance_signer": ("验收人",),
    "acceptance_time": ("验收时间",),
    "personnel_count": ("解危人数",),
    "unrelated_equipment_stopped": ("无关设备已停",),
    "expected_personnel": ("应撤人数",),
    "withdrawn_personnel": ("实撤人数",),
    "attendance_count": ("出勤人数", "出勤人数（人）", "出勤人数(人)", "出勤"),
    "coal_cutting_count": ("当班割煤刀数", "当班割煤刀数（刀）", "割煤刀数", "刀数"),
    "planned_cutting_count": ("日计划割煤", "日计划割煤（刀）"),
    "cumulative_cutting_count": ("本月累计割煤", "本月累计割煤（刀）", "月累计割煤"),
    "work_content": ("作业内容", "产量（t）/进尺（m）/作业内容", "产量(t)/进尺(m)/作业内容"),
    "signer": ("签字人",),
    "signature_present": ("签名存在",),
    "signed_at": ("签署时间",),
    "notified": ("已告知",),
    "notification_time": ("告知时间",),
    "event_time_reference": ("关联事件时间",),
}
FIELD_ALIASES["target_scope"] += ("适用范围",)
FIELD_ALIASES["workface"] += ("工作面（作业点）名称", "工作面(作业点)名称", "工作面名称", "工作地点")
FIELD_ALIASES["work_content"] = ("作业内容", "产量（t）/进尺（m）/作业内容", "产量(t)/进尺(m)/作业内容")


def _norm(value: Any) -> str:
    return re.sub(r"[\s　:_：；;,，/]+", "", str(value or "")).lower()


def _role(entry: dict) -> str:
    role = entry.get("source_role") or entry.get("metadata", {}).get("source_role")
    return role if role in ROLES else "statement"


def _date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = re.sub(r"(\d{4})年(\d{1,2})月(\d{1,2})日", r"\1-\2-\3", text)
    text = text.replace("年", "-").replace("月", "-").replace("日", "")
    text = re.sub(r"[Tt]", " ", text)
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?))?", text)
    if not match:
        return None
    try:
        day = "-".join((match.group(1), match.group(2).zfill(2), match.group(3).zfill(2)))
        if match.group(4) is None:
            return datetime.fromisoformat(day).date().isoformat()
        raw_time = match.group(4)
        raw_time = re.sub(r"^(\d):", r"0\1:", raw_time)
        normalized = day + "T" + raw_time
        datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        return normalized
    except ValueError:
        return None


def _num(value: Any) -> Any:
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    try:
        Decimal(text.replace(",", ""))
        return text  # Preserve decimal spelling and zero; never infer unit.
    except (InvalidOperation, ValueError):
        return text


def _bool(value: Any) -> bool | None:
    text = str(value if value is not None else "").strip().lower()
    if not text:
        return None
    if text in {"否", "false", "假", "未批复", "未批准", "未通过", "未完成", "未执行", "不符合", "不立即", "无危险", "未确认危险", "不危险", "not_done", "0", "无", "不存在", "未签字", "未签名", "未告知", "未停运"}:
        return False
    if text in {"是", "true", "真", "已批复", "已批准", "已通过", "确认", "完成", "已完成", "已执行", "符合", "立即", "有危险", "已确认危险", "危险", "done", "1", "有", "存在", "已签字", "已签名", "已告知", "已停运"}:
        return True
    return None


def _actor(value: Any) -> Any:
    text = str(value or "").strip()
    if not text:
        return None
    if text in {"技术负责人", "煤矿技术负责人", "矿技术负责人", "总工程师", "矿总工程师", "technical_head"}:
        return "technical_head"
    if text in {"调度室", "调度", "矿调度室", "dispatch"}:
        return "dispatch"
    return text


def _fact_id(kind: str, event_id: Any, workface: Any, time: Any, fields: dict) -> str:
    payload = json.dumps([kind, event_id, workface, time, fields], ensure_ascii=False, sort_keys=True)
    return "fact_" + hashlib.sha256(payload.encode()).hexdigest()[:20]


def _pairs(text: str) -> dict[str, str]:
    """Parse explicit key:value pairs without allowing values to cross lines."""
    result: dict[str, str] = {}
    aliases = sorted({a for names in FIELD_ALIASES.values() for a in names}, key=len, reverse=True)
    # PDF extraction often drops table borders but retains explicit field labels.
    # Only labels followed by a colon start a field; ordinary prose is untouched.
    label = re.compile(r"(?:^|[\s；;，,])(" + "|".join(re.escape(a) for a in aliases) + r")\s*[:：]\s*")
    for line in re.split(r"[\r\n]+", text):
        matches = list(label.finditer(line))
        if matches:
            for i, match in enumerate(matches):
                key = match.group(1)
                value = line[match.end():matches[i + 1].start() if i + 1 < len(matches) else len(line)].strip(" ；;，,")
                if value:
                    if key in result and result[key] != value:
                        result["__conflict__" + key] = "conflict"
                    result[key] = value
            continue
        for part in re.split(r"[；;]+", line):
            match = re.match(r"\s*([^:：]{1,24})\s*[:：]\s*(.*?)\s*$", part)
            if match and match.group(2):
                key, value = match.group(1).strip(), match.group(2).strip()
                if key in result and result[key] != value:
                    result["__conflict__" + key] = "conflict"
                result[key] = value
    return result


def _table_records(evidence: list[dict]) -> list[dict]:
    """Select explicit headers inside each sheet, preserving raw cell provenance.

    A report date may scope sheet rows only when it is stated in a report title.
    Workfaces, event IDs and action times are never filled down from other rows.
    """
    states: dict[tuple, dict] = {}
    records = []
    known = {_norm(alias) for names in FIELD_ALIASES.values() for alias in names}
    for original in evidence or []:
        if not isinstance(original, dict):
            continue
        meta = original.get("metadata") or {}
        cells = meta.get("cells")
        locator = original.get("locator") or {}
        if not isinstance(cells, list) or "sheet" not in locator:
            records.append(original)
            continue
        key = (original.get("file_id"), locator["sheet"])
        state = states.setdefault(key, {})
        labels = {_norm(c) for c in cells if str(c).strip()}
        recognized = labels & known
        if len(recognized) >= 2:
            state["headers"] = cells
            state["header_source"] = original
        elif "headers" not in state and re.search(r"日报|调度台账", str(original.get("text") or "")):
            dates = set(re.findall(r"\d{4}年\d{1,2}月\d{1,2}日|\d{4}-\d{1,2}-\d{1,2}", str(original.get("text") or "")))
            if len(dates) == 1 and _date(next(iter(dates))):
                day = _date(next(iter(dates)))
                if "date" in state and state["date"] != day:
                    state["date_conflict"] = True
                state.update(date=day, date_source=original)
                records.append({**original, "metadata": {**meta, "fact_title": True}})
                continue
        if "headers" in state:
            context = [state["header_source"]]
            updated = {**meta, "headers": state["headers"]}
            if state.get("date") and not state.get("date_conflict"):
                updated["report_date"] = state["date"]
                context.append(state["date_source"])
            updated["fact_context"] = [{"evidence_id": e.get("evidence_id"), "file_id": e.get("file_id"),
                                        "locator": e.get("locator", {})} for e in context]
            records.append({**original, "metadata": updated})
        else:
            records.append(original)
    return records


def _mapped(pairs: dict[str, Any], gaps: list[str]) -> dict[str, Any]:
    mapped: dict[str, Any] = {}
    gaps.extend("conflicting_columns:" + key[len("__conflict__"):] for key in pairs if key.startswith("__conflict__"))
    for field, aliases in FIELD_ALIASES.items():
        hits = [value for key, value in pairs.items() if _norm(key) in {_norm(a) for a in aliases}]
        if len({str(x).strip() for x in hits}) > 1:
            gaps.append(f"conflicting_columns:{field}")
        if hits:
            mapped[field] = hits[-1]
    return mapped


def _source_values(entry: dict, gaps: list[str],
                   advisories: list[str] | None = None) -> tuple[dict[str, Any], bool]:
    meta = entry.get("metadata") or {}
    cells = meta.get("cells")
    headers = meta.get("headers")
    structured = isinstance(cells, list) and isinstance(headers, list) and bool(headers)
    if structured:
        if len(cells) != len(headers):
            gaps.append("table_row_length_mismatch")
        by_header = {}
        known = {_norm(alias) for aliases in FIELD_ALIASES.values() for alias in aliases}
        for h, value in zip(headers, cells):
            label = _norm(h)
            if not label or label not in known or value is None or not str(value).strip():
                continue
            if label in by_header and str(by_header[label]).strip() != str(value).strip():
                gaps.append("conflicting_columns:" + label)
            by_header[label] = value
        unknown = [str(h) for h in headers if str(h).strip() and _norm(h) not in known]
        if unknown:
            # Format information, not missing evidence: an unknown column must
            # never be the reason a document cannot be judged complete.
            target = advisories if advisories is not None else gaps
            target.append("unknown_headers:" + ",".join(unknown))
        pairs = {str(h): cells[i] for i, h in enumerate(headers)
                 if i < len(cells) and str(h).strip() and cells[i] is not None and str(cells[i]).strip()}
        values = _mapped(pairs, gaps)
        if not values.get("time") and meta.get("report_date"):
            values["time"] = meta["report_date"]
        content = str(values.get("work_content") or "").strip()
        # An explicit daily production entry is evidence of work on that day,
        # not proof of a trigger or an exact resumption time.
        if not values.get("action") and re.fullmatch(r"(?:\d+(?:\.\d+)?\s*(?:m|t|米|吨)\s*[/／]\s*)?正常(?:生产|掘进|采掘)", content):
            values.update(action="continue_work", status="done")
        return values, True
    text = str(entry.get("text") or "")
    values = _mapped(_pairs(text), gaps)
    # Accept narrow, explicit body records as well as colon-separated fields.
    # All identifying values must occur on this same source line.
    if not values:
        dates = re.findall(r"\d{4}年\d{1,2}月\d{1,2}日(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?|\d{4}-\d{1,2}-\d{1,2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?", text)
        faces = re.findall(r"(?<!\d)\d{4,}[A-Za-z-]*工作面", text)
        actions = re.findall(r"(?:已|尚未|未)(?:停止作业|停工|断电|撤人|撤离|复工)|正常(?:生产|掘进|采掘)", text)
        direct_record = (len(dates) == len(faces) == len(actions) == 1 and
                         re.fullmatch(r"\s*" + re.escape(dates[0]) + r"\s*" + re.escape(faces[0]) +
                                      r"\s*" + re.escape(actions[0]) + r"\s*[。；;]?\s*", text))
        if direct_record:
            values = {"time": dates[0], "workface": faces[0], "action": actions[0]}
            if actions[0].startswith("正常"):
                values.update(action="continue_work", status="done")
    return values, False


def _event_values(values: dict[str, Any], text: str) -> tuple[str | None, str | None, str | None, dict]:
    event_id, workface, when = values.get("event_id"), values.get("workface"), _date(values.get("time"))
    fields: dict[str, Any] = {}
    typ = str(values.get("trigger_type") or "")
    if typ in EVENT_TYPES:
        fields["trigger_type"] = EVENT_TYPES[typ]
    elif typ in {"dynamic", "warning"}:
        fields["trigger_type"] = typ
    hb = _bool(values.get("hazard_confirmed"))
    if hb is not None:
        fields["hazard_confirmed"] = hb
    return (str(event_id).strip() if event_id is not None and str(event_id).strip() else None,
            str(workface).strip() if workface is not None and str(workface).strip() else None,
            when, fields)


def _make_fact(kind: str, entry: dict, event_id: Any, workface: Any, when: Any, fields: dict) -> dict:
    role = _role(entry)
    sources = [{"evidence_id": entry.get("evidence_id"), "file_id": entry.get("file_id"),
                "locator": entry.get("locator", {})}]
    sources.extend((entry.get("metadata") or {}).get("fact_context", []))
    # Release notices and dispatch plans describe an instruction layer.  They
    # may prove that an order was issued, but cannot prove physical evacuation,
    # unloading, inspection, or resumption.  Dispatch release remains an order
    # by design and is handled separately by the deterministic rule.
    group = str(entry.get("material_group") or (entry.get("metadata") or {}).get("material_group") or "")
    if kind == "action":
        if group == "release":
            fields = {**fields, "action_layer": "instruction"}
    fact = {"fact_id": _fact_id(kind + ":" + role, event_id, workface, when, fields), "kind": kind,
            "event_id": event_id, "workface": workface, "time": when,
            "source_role": role, "evidence_refs": list(dict.fromkeys(s["evidence_id"] for s in sources)),
            "sources": sources, "fields": fields}
    return fact


def _validity(values: dict, gaps: list[str]) -> dict:
    """Keep an explicit invalid bound visible; it must not become an open interval."""
    fields = {}
    for field in ("valid_from", "valid_to"):
        value = values.get(field)
        if value is None or not str(value).strip():
            continue
        normalized = _date(value)
        if normalized is None:
            gaps.append(f"invalid_date:{field}")
            fields[field] = str(value).strip()
        else:
            fields[field] = normalized
    return fields


def _present(value: Any) -> bool:
    return value is not None and bool(str(value).strip())


def _json_list(value: Any) -> list | None:
    def distinct_object(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = item
        return result

    try:
        obj = json.loads(str(value), parse_float=Decimal, parse_int=Decimal,
                         parse_constant=lambda _: None, object_pairs_hook=distinct_object)
        return obj if isinstance(obj, list) and obj else None
    except (ValueError, TypeError):
        return None


def _parameter_list(value: Any, *, requirement: bool) -> list[dict] | None:
    """Parameter units and comparison semantics must be supplied by the source."""
    obj = _json_list(value)
    keys = {"name", "value", "unit"} | ({"operator"} if requirement else set())
    if obj is None:
        return None
    result = []
    for item in obj:
        if not isinstance(item, dict) or set(item) != keys:
            return None
        if not all(isinstance(item[k], str) and item[k].strip() for k in keys - {"value"}):
            return None
        if isinstance(item["value"], bool) or item["value"] is None:
            return None
        try:
            number = Decimal(str(item["value"]))
            if not number.is_finite():
                return None
        except (InvalidOperation, ValueError, TypeError):
            return None
        if requirement and item["operator"] not in {"<", "<=", "=", "==", ">=", ">"}:
            return None
        result.append({k: str(v).strip() for k, v in item.items()})
    return result


def _extra_fields(values: dict, gaps: list[str], *, strings=(), booleans=(), dates=(), counts=()) -> dict:
    fields = {}
    for field in strings:
        if _present(values.get(field)):
            fields[field] = str(values[field]).strip()
    for field in booleans:
        if _present(values.get(field)):
            parsed = _bool(values[field])
            if parsed is None:
                gaps.append(f"unresolved_boolean:{field}")
            else:
                fields[field] = parsed
    for field in dates:
        if _present(values.get(field)):
            parsed = _date(values[field])
            fields[field] = parsed if parsed is not None else str(values[field]).strip()
            if parsed is None:
                gaps.append(f"invalid_date:{field}")
    for field in counts:
        if _present(values.get(field)):
            raw = str(values[field]).strip()
            if re.fullmatch(r"\d+", raw):
                fields[field] = raw
            else:
                gaps.append(f"invalid_count:{field}")
    return fields


def _explicit_requirements(value: Any) -> list[dict] | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, list) and obj and all(isinstance(x, dict) and {"method", "metric", "point"} <= set(x)
                                                and all(isinstance(x[k], str) and x[k].strip() for k in ("method", "metric", "point")) for x in obj):
            return [{"method": str(x["method"]), "metric": str(x["metric"]), "point": str(x["point"])} for x in obj]
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    # A strict semicolon list is accepted only when every item has all three labels.
    items = [x.strip() for x in re.split(r"[；;\n]+", text) if x.strip()]
    result = []
    for item in items:
        p = _mapped(_pairs(item), [])
        if not all(p.get(k) for k in ("method", "metric", "point")):
            return None
        result.append({k: str(p[k]).strip() for k in ("method", "metric", "point")})
    return result or None


def extract_facts(evidence: list[dict], *, allow_unbound=False) -> dict:
    facts: list[dict] = []
    candidates: list[dict] = []
    gaps: list[str] = []
    advisories: list[str] = []
    records = []
    for original in _table_records(evidence):
        if not isinstance(original, dict):
            continue
        meta = original.get("metadata") or {}
        if isinstance(meta.get("cells"), list):
            records.append(original)
        else:
            # A new text line is a new record; identifiers are never carried forward.
            for line in str(original.get("text") or "").splitlines():
                if line.strip():
                    records.append({**original, "text": line})
    for entry in records:
        if not isinstance(entry, dict):
            continue
        eid = entry.get("evidence_id")
        text = str(entry.get("text") or "")
        meta = entry.get("metadata") or {}
        if meta.get("fact_title"):
            continue
        if meta.get("cells") and meta.get("cells") == meta.get("headers"):
            continue
        initial_fact_count = len(facts)
        local_gaps: list[str] = []
        values, structured = _source_values(entry, local_gaps, advisories)
        metadata_gaps = (entry.get("metadata") or {}).get("gaps") or []
        if metadata_gaps:
            local_gaps.extend("source_content_unresolved:" + str(x) for x in metadata_gaps)
        event_id, workface, when, event_fields = _event_values(values, text)
        role = _role(entry)
        # Requirement/threshold facts may be independent of an event, execution facts may not.
        requirement_items = _explicit_requirements(values.get("requirements"))
        conflict = any(x.startswith(("conflicting_columns:", "source_content_unresolved:")) or x == "table_row_length_mismatch" for x in local_gaps)
        if conflict:
            values = {}
            event_id = workface = when = None
            event_fields = {}
            requirement_items = None
        document_kind = {"危险性评价": "risk_assessment", "冲击危险性评价": "risk_assessment",
                         "防冲设计": "design", "防冲专项措施": "design"}.get(str(values.get("trigger_type") or ""))
        if document_kind and role == "requirement":
            fields = _extra_fields(values, local_gaps, strings=("target_scope",), booleans=("approved",))
            fields.update(_validity(values, local_gaps))
            if document_kind == "risk_assessment":
                fields.update(_extra_fields(values, local_gaps, strings=("risk_grade",), booleans=("impact_risk",)))
            else:
                fields.update(_extra_fields(values, local_gaps,
                    strings=("technical_head", "relief_area", "withdrawal_area"),
                    booleans=("unrelated_equipment_stop_required",), counts=("max_personnel",)))
                if _present(values.get("relief_methods")):
                    methods = _json_list(values["relief_methods"])
                    if methods is not None and all(isinstance(x, str) and x.strip() for x in methods):
                        fields["relief_methods"] = [x.strip() for x in methods]
                    else:
                        local_gaps.append("invalid_json:relief_methods")
                if _present(values.get("relief_parameters")):
                    parameters = _parameter_list(values["relief_parameters"], requirement=True)
                    if parameters is None:
                        local_gaps.append("invalid_json:relief_parameters")
                    else:
                        fields["relief_parameters"] = parameters
            facts.append(_make_fact(document_kind, entry, event_id, workface, when, fields))
        if requirement_items is not None and role in {"requirement", "regulation"}:
            approved = _bool(values.get("approved"))
            fields = {"required_points": requirement_items, "approved": approved}
            fields.update(_validity(values, local_gaps))
            facts.append(_make_fact("requirements", entry, event_id, workface, when, fields))
        # Explicit threshold records are requirement material, never execution results.
        if role in {"requirement", "regulation"} and values.get("value") is not None and values.get("metric"):
            fields = {k: (_num(values.get(k)) if k == "value" else values.get(k)) for k in
                      ("method", "metric", "point", "value", "unit", "valid_from", "valid_to")}
            fields = {k: (str(v).strip() if v is not None and k not in {"value", "valid_from", "valid_to"} else v)
                      for k, v in fields.items() if v is not None and str(v).strip()}
            fields.update(_validity(values, local_gaps))
            fields["approved"] = _bool(values.get("approved"))
            facts.append(_make_fact("threshold", entry, event_id, workface, when, fields))
        normative = bool(re.search(r"应当|必须|严禁|不得|计划|拟于|拟定|拟停|拟撤|建议|未检索到|未找到|未见记录|未发现记录", text))
        execution_allowed = role == "actual_record" and bool((event_id or allow_unbound) and workface and when) and not normative
        if role == "actual_record" and not execution_allowed and (event_id or workface or values.get("time")):
            local_gaps.append("execution_record_requires_event_id_workface_time")
        if execution_allowed:
            if "trigger_type" in event_fields:
                facts.append(_make_fact("event", entry, event_id, workface, when, event_fields))
            # Measurements need explicit metric, point and value; zero is valid.
            if values.get("metric") is not None and values.get("value") is not None and values.get("point") is not None:
                stage_text = str(values.get("stage") or values.get("trigger_type") or "")
                stage = "warning" if stage_text in {"预警", "预警监测", "warning"} else "effect" if stage_text in {"效果检验", "解危效果检验", "效果", "effect"} else None
                fields = {"stage": stage,
                          "method": values.get("method"), "metric": values.get("metric"),
                          "point": values.get("point"), "value": _num(values.get("value")), "unit": values.get("unit")}
                facts.append(_make_fact("measurement", entry, event_id, workface, when,
                                        {k: v for k, v in fields.items() if v is not None}))
            # Actions require an explicit action/status. Plans and regulations become candidates.
            action_text = str(values.get("action") or "")
            action_recognized = False
            for token, action_kind in ACTION_TYPES.items():
                if re.fullmatch(rf"(?:已|未|没有|尚未|未曾)?\s*{re.escape(token)}", action_text) or action_text == action_kind:
                    action_recognized = True
                    status = _bool(values.get("status"))
                    prefix = re.fullmatch(rf"(已|未|没有|尚未|未曾)\s*{re.escape(token)}", action_text)
                    if prefix:
                        action_status = False if re.search(r"未|没有", prefix.group(0)) else True
                        if status is not None and status != action_status:
                            local_gaps.append("conflicting_action_status")
                            status = None
                        else:
                            status = action_status
                    if status is None:
                        local_gaps.append(f"action_status_missing:{action_kind}")
                    else:
                        fields = {"status": "done" if status else "not_done"}
                        for field in ("actor_role", "target_scope"):
                            if values.get(field) is not None:
                                fields[field] = _actor(values[field]) if field == "actor_role" else str(values[field]).strip()
                        for field in ("route_conforms", "immediate"):
                            flag = _bool(values.get(field))
                            if flag is not None:
                                fields[field] = flag
                        if action_kind == "hazard_analysis":
                            hazard = _bool(values.get("hazard_confirmed"))
                            if hazard is not None:
                                fields["hazard_confirmed"] = hazard
                            fields.update(_extra_fields(values, local_gaps, strings=("signer",),
                                booleans=("signature_present", "notified"), dates=("notification_time",)))
                        elif action_kind == "technical_confirm":
                            fields.update(_extra_fields(values, local_gaps, strings=("signer",),
                                booleans=("signature_present",), dates=("signed_at",)))
                        elif action_kind == "evacuate":
                            fields.update(_extra_fields(values, local_gaps, strings=("withdrawal_area",),
                                counts=("expected_personnel", "withdrawn_personnel")))
                        elif action_kind == "relief":
                            fields.update(_extra_fields(values, local_gaps,
                                strings=("method", "relief_area", "acceptance_signer"),
                                booleans=("acceptance_passed", "unrelated_equipment_stopped"),
                                dates=("acceptance_time",), counts=("personnel_count",)))
                            if _present(values.get("parameters")):
                                parameters = _parameter_list(values["parameters"], requirement=False)
                                if parameters is None:
                                    local_gaps.append("invalid_json:parameters")
                                else:
                                    fields["parameters"] = parameters
                        facts.append(_make_fact("action", entry, event_id, workface, when, {"action": action_kind, **fields}))
                    break
            if action_text and not action_recognized:
                local_gaps.append("uninterpreted_action_not_proof_of_mining")
        # Every kind may explicitly point to a trigger time, without inventing an event id.
        reference = _extra_fields(values, local_gaps, dates=("event_time_reference",))
        for fact in facts[initial_fact_count:]:
            if reference:
                fact["fields"].update(reference)
                fact["fact_id"] = _fact_id(fact["kind"] + ":" + fact["source_role"], fact["event_id"],
                                           fact["workface"], fact["time"], fact["fields"])
        # Free prose is always a candidate, with exact quote and provenance.
        clue = any(k in text for k in ("动力", "震动", "巨响", "底鼓", "煤岩弹射", "预警", "停工", "撤人", "解危", "监测", "检验"))
        if (clue and len(facts) == initial_fact_count) or local_gaps or (values and role == "statement"):
            candidates.append({"quote": text, "evidence_refs": [eid], "kind": "possible_compliance_fact"})
        for gap in local_gaps:
            issue = f"{eid}: {gap}" if eid else gap
            if issue not in gaps:
                gaps.append(issue)
    # De-duplicate facts while preserving first evidence provenance.
    unique: dict[tuple, dict] = {}
    for fact in facts:
        key = (fact["kind"], fact.get("event_id"), fact.get("workface"), fact.get("time"), fact["source_role"], json.dumps(fact["fields"], sort_keys=True, ensure_ascii=False))
        if key in unique:
            for ref in fact["evidence_refs"]:
                if ref not in unique[key]["evidence_refs"]:
                    unique[key]["evidence_refs"].append(ref)
            for source in fact["sources"]:
                if source not in unique[key]["sources"]:
                    unique[key]["sources"].append(source)
        else:
            unique[key] = fact
    return {"facts": list(unique.values()), "candidates": candidates, "gaps": gaps,
            "advisories": list(dict.fromkeys(advisories))}
