"""Conservative monitoring candidates for the first trigger step.

The adapter preserves what OCR/VLM/table extraction found. It deliberately
does not infer chart time, mine shifts, grade semantics, or compliance facts.
Those meanings belong to the applicable threshold profile and a later trusted
validator.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
import unicodedata


_ALIASES = {
    "probe_id": ("探头", "探头编号", "传感器", "传感器编号", "测点", "测点编号", "点号"),
    "workface": ("工作面", "工作面名称", "作业地点", "作业位置"),
    "location": ("地点", "具体地点", "位置", "巷道", "顺槽"),
    "technology": ("监测技术", "监测方法", "方法", "类型", "监测项目"),
    "dev_value": ("dev", "偏差值", "偏差", "dev值", "dev值(%)", "dev（%）"),
    "value": ("监测值", "实测值", "数值", "读数", "值", "结果"),
    "metric": ("指标", "指标名称", "监测指标"),
    "unit": ("单位", "计量单位"),
    "sample_time": ("时间", "采样时间", "记录时间", "日期", "监测日期", "班次时间"),
    "shift_id": ("班次", "班次编号", "班别", "班次标识"),
    "shift_type": ("班次类型", "生产班", "检修班", "作业班"),
    "window_start": ("统计开始", "统计起始时间", "时段开始"),
    "window_end": ("统计结束", "统计截止时间", "时段结束"),
    "grade": ("等级", "预警等级", "危险等级", "级别"),
}
_TECHNOLOGY_HINTS = ("地音", "微震", "应力", "钻屑", "电磁辐射", "矿压", "综合预警")
_MAX_CANDIDATES = 2000


def _norm(value) -> str:
    return re.sub(r"[\s　:_：；;,，/（）()]+", "", unicodedata.normalize("NFKC", str(value or ""))).lower()


def _header_field(value):
    key = _norm(value)
    for field, aliases in _ALIASES.items():
        if key in {_norm(alias) for alias in aliases} or any(_norm(alias) in key for alias in aliases if len(_norm(alias)) >= 2):
            return field
    return None


def _number(value):
    text = str(value if value is not None else "").strip().replace(",", "")
    text = text.replace("％", "%").rstrip("%")
    try:
        number = Decimal(text)
        return str(number), "%" if "%" in str(value) or "％" in str(value) else None
    except (InvalidOperation, ValueError, TypeError):
        return None, None


def _time_precision(value, shift_id=None, window_start=None, window_end=None):
    text = str(value or "").strip()
    if window_start and window_end and _valid_datetime(window_start) and _valid_datetime(window_end):
        return "explicit_interval"
    if shift_id and _valid_date(text):
        return "shift_labeled"
    if _valid_datetime(text):
        return "exact_time"
    if _valid_date(text):
        return "date_only"
    return "unknown"


def _valid_date(text):
    raw = str(text or "").strip().replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-").replace(".", "-")
    try:
        return bool(re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", raw)) and bool(datetime.fromisoformat(raw))
    except ValueError:
        return False


def _valid_datetime(text):
    raw = str(text or "").strip().replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-").replace(".", "-")
    if not re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?", raw):
        return False
    try:
        datetime.fromisoformat(raw)
        return True
    except ValueError:
        return False


def _technology(text):
    text = str(text or "")
    return next((hint for hint in _TECHNOLOGY_HINTS if hint in text), None)


def _source(entry, method):
    locator = entry.get("locator") or {}
    return {"evidence_id": entry.get("evidence_id"), "file_id": entry.get("file_id"),
            "locator": locator, "quote": str(entry.get("text") or entry.get("quote") or "")[:2000],
            "method": method, "quality": entry.get("quality") or "native"}


def _candidate(entry, values, *, method, raw_text=None, bbox=None):
    probe = str(values.get("probe_id") or "").strip() or None
    workface = str(values.get("workface") or "").strip() or None
    location = str(values.get("location") or "").strip() or None
    technology = str(values.get("technology") or "").strip() or _technology(raw_text or entry.get("text"))
    dev, implied_unit = _number(values.get("dev_value"))
    value, value_unit = _number(values.get("value"))
    unit = str(values.get("unit") or implied_unit or value_unit or "").strip() or None
    sample_time = str(values.get("sample_time") or "").strip() or None
    shift_id = str(values.get("shift_id") or "").strip() or None
    shift_type = str(values.get("shift_type") or "").strip() or None
    window_start = str(values.get("window_start") or "").strip() or None
    window_end = str(values.get("window_end") or "").strip() or None
    grade = str(values.get("grade") or "").strip() or None
    number = dev if dev is not None else value
    if number is None:
        return None, "monitoring_value_missing"
    if not probe and not workface and not location:
        return None, "monitoring_identity_missing"
    identity = json.dumps([entry.get("evidence_id"), entry.get("file_id"), entry.get("locator"), method,
                           probe, workface, location, sample_time, shift_id, window_start, window_end, number, bbox],
                          ensure_ascii=False, sort_keys=True, default=str)
    return {"candidate_id": "monitoring-" + sha256(identity.encode("utf-8")).hexdigest()[:24],
            "technology": technology, "probe_id": probe, "workface": workface, "location": location,
            "metric": str(values.get("metric") or "dev" if dev is not None else values.get("metric") or "").strip() or None,
            "dev_value": dev, "value": value, "unit": unit, "sample_time": sample_time,
            "shift_id": shift_id, "shift_type": shift_type, "window_start": window_start,
            "window_end": window_end, "grade_label": grade,
            "time_precision": _time_precision(sample_time, shift_id, window_start, window_end), "shift_derived": False,
            "raw_text": str(raw_text or entry.get("text") or entry.get("quote") or "")[:500],
            "bbox": bbox, "source": _source(entry, method), "validated": False,
            "fact_status": "candidate", "trigger": None}, None


def _table_candidates(entry):
    meta = entry.get("metadata") or {}
    cells, headers = meta.get("cells"), meta.get("headers")
    if not isinstance(cells, list) or not isinstance(headers, list):
        return [], []
    fields = {_header_field(header): index for index, header in enumerate(headers) if _header_field(header)}
    if not fields:
        return [], ["monitoring_table_headers_unrecognized"]
    values = {field: cells[index] for field, index in fields.items() if index < len(cells)}
    candidate, gap = _candidate(entry, values, method="table_cells")
    return ([candidate] if candidate else []), ([gap] if gap else [])


def _structured_chart_candidates(entry):
    meta = entry.get("metadata") or {}
    points = meta.get("chart_points") or meta.get("monitoring_points")
    if not isinstance(points, list):
        return [], []
    candidates, gaps = [], []
    for point in points:
        if not isinstance(point, dict):
            gaps.append("chart_point_not_object")
            continue
        candidate, gap = _candidate(entry, point, method="structured_chart", raw_text=point.get("raw_text"), bbox=point.get("bbox"))
        if candidate:
            # Explicit metadata may say time is inferred; never turn it into a shift.
            candidate["shift_derived"] = bool(point.get("shift_derived", False))
            candidates.append(candidate)
        elif gap:
            gaps.append(gap)
    return candidates, gaps


def _text_candidate(entry):
    text = str(entry.get("text") or entry.get("quote") or "")
    if not text or not _technology(text) or not re.search(r"(?:dev|偏差|%|％)", text, re.I):
        return [], []
    probe = re.search(r"(?:探头|传感器|测点)(?:编号)?\s*[-#：:]?\s*([A-Za-z]?\d{1,4})|(?<![A-Za-z0-9])(D\d{1,4})(?!\d)", text, re.I)
    values = {"technology": _technology(text), "probe_id": (probe.group(1) or probe.group(2)) if probe else None}
    dev = re.search(r"(?:dev|偏差(?:值)?)\s*(?:[=:：])?\s*(-?\d+(?:\.\d+)?)\s*[%％]?", text, re.I)
    if dev:
        values["dev_value"] = dev.group(1) + ("%" if "%" in dev.group(0) or "％" in dev.group(0) else "")
    candidate, gap = _candidate(entry, values, method="text_candidate", raw_text=text)
    return ([candidate] if candidate else []), ([gap] if gap else ["monitoring_text_identity_or_value_unresolved"])


def extract_monitoring_candidates(evidence: list[dict], *, coverage: list[dict] | None = None) -> dict:
    """Extract auditable monitoring candidates without inferring trigger facts."""
    candidates, gaps = [], []
    for entry in evidence or []:
        found, local = _structured_chart_candidates(entry)
        if not found:
            table_found, table_gaps = _table_candidates(entry)
            found.extend(table_found); local.extend(table_gaps)
        if not found:
            text_found, text_gaps = _text_candidate(entry)
            found.extend(text_found); local.extend(text_gaps)
        candidates.extend(found)
        for gap in local:
            gaps.append({"evidence_id": entry.get("evidence_id"), "file_id": entry.get("file_id"),
                         "locator": entry.get("locator") or {}, "reason": gap})
    observed_units = {(entry.get("file_id"), entry.get("unit_id") or
                       ("page:" + str((entry.get("locator") or {})["page"]) if (entry.get("locator") or {}).get("page") else None))
                      for entry in evidence or []}
    for unit in coverage or []:
        scope = (unit.get("file_id"), unit.get("unit_id"))
        if scope not in observed_units and unit.get("state") in {"unreadable", "partial", "not_indexed", "provider_failed", "failed"}:
            gaps.append({"file_id": scope[0], "unit_id": scope[1], "reason": "monitoring_source_unavailable:" + str(unit["state"])})
    truncated = max(0, len(candidates) - _MAX_CANDIDATES)
    if truncated:
        gaps.append({"reason": "monitoring_candidate_budget", "omitted_count": truncated})
        candidates = candidates[:_MAX_CANDIDATES]
    return {"candidates": candidates, "gaps": gaps, "trigger": None, "verified": False,
            "truncated_count": truncated, "policy": "candidate_only_v1"}


def adjacent_probe_groups(candidates: list[dict]) -> dict:
    """Group only explicit different-probe same-location/time candidates.

    This implements the confirmed business convention. It does not claim a
    physical topology and never asserts a C/D or color grade.
    """
    groups = defaultdict(list)
    gaps = []
    for candidate in candidates or []:
        probe = candidate.get("probe_id")
        sample_time = candidate.get("sample_time")
        shift_id = candidate.get("shift_id")
        precision = candidate.get("time_precision")
        comparable = (((candidate.get("window_start"), candidate.get("window_end"))
                       if precision == "explicit_interval" else
                       (sample_time, shift_id) if precision == "shift_labeled" and sample_time and shift_id else
                       (sample_time, shift_id) if precision == "exact_time" else None))
        key = (candidate.get("technology"), candidate.get("workface"), candidate.get("location"),
               candidate.get("metric"), candidate.get("unit"), comparable)
        if not probe or not key[0] or not key[1] or not key[2] or not key[3] or not key[4] or not key[5] or candidate.get("shift_derived"):
            continue
        groups[key].append(candidate)
    output = []
    for key, rows in groups.items():
        by_probe = {row.get("probe_id"): row for row in rows if row.get("probe_id")}
        if len(by_probe) >= 2:
            output.append({"technology": key[0], "workface": key[1], "location": key[2], "metric": key[3],
                           "unit": key[4], "time_key": key[5],
                           "probe_ids": sorted(by_probe), "candidates": list(by_probe.values()),
                           "relationship": "same_location_distinct_probe_convention", "adjacent_proven": False,
                           "trigger": None})
    return {"groups": output, "gaps": gaps, "verified": False}
