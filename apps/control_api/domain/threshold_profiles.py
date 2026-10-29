"""Conservative extraction and comparison of mine-specific threshold documents.

This module deliberately keeps the source's grade labels and wording. It does
not map C/D to blue/yellow/orange/red, infer validity from upload dates, or
choose a latest document. The result is a rule profile for review and later
deterministic adapters.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import re
from typing import Any


POLICY_REVISION = "threshold-profile-v1"
_TECHNOLOGIES = (
    ("ground_sound", re.compile(r"地音(?:监测|预警)")),
    ("microseismic", re.compile(r"微震(?:监测|预警)")),
    ("stress", re.compile(r"(?:钻孔)?应力(?:监测|预警|分析)")),
    ("drill_cuttings", re.compile(r"钻屑|钻粉")),
    ("electromagnetic", re.compile(r"电磁辐射")),
    ("multi_parameter", re.compile(r"多参量综合")),
)
_LEVEL_START = re.compile(
    r"(?<![A-Za-z\u4e00-\u9fff、])(?P<label>[a-dA-D]|蓝|黄|橙|红|无|弱|中|强)\s*(?:(?:色)?级\s*[.．、:：]?|[.．、:：])\s*"
)
_RANGE = re.compile(
    r"(?P<low>\d+(?:\.\d+)?)\s*%?\s*(?P<low_op>[≤<])\s*dev\s*(?P<high_op><|≤)\s*(?P<high>\d+(?:\.\d+)?)\s*%?",
    re.I,
)
_LOWER = re.compile(r"dev\s*(?P<op>≥|>=|>)\s*(?P<value>\d+(?:\.\d+)?)\s*%?", re.I)
_UPPER = re.compile(r"dev\s*(?P<op>≤|<=|<)\s*(?P<value>\d+(?:\.\d+)?)\s*%?", re.I)
_SECTION = re.compile(r"(?<![\u4e00-\u9fffA-Za-z0-9])(?:[一二三四五六七八九十]+|\d+)[、.．]\s*(?P<title>[^\n。；;]{2,40})")


def _native(entry: dict) -> bool:
    """Only native/independently reviewed text may define a threshold profile."""
    if entry.get("quality") in {"model_candidate", "candidate", "conflicted_review"}:
        return False
    if entry.get("source_role") in {"statement"}:
        return False
    return bool(str(entry.get("text") or entry.get("quote") or "").strip())


def _file_info(file: dict) -> dict:
    return {key: file.get(key) for key in ("file_id", "original_name", "content_hash", "report_date", "source_role")}


def _technology(text: str) -> str | None:
    for name, pattern in _TECHNOLOGIES:
        if pattern.search(text):
            return name
    return None


def _technology_sections(text: str, previous: str | None = None) -> list[tuple[str, str, str]]:
    """Split a page by explicit section headings; return tech, body, heading."""
    matches = []
    for match in _SECTION.finditer(text):
        technology = _technology(match.group("title"))
        if technology:
            matches.append((match.start(), match.end(), technology, match.group("title")))
    if not matches:
        mentioned = {name for name, pattern in _TECHNOLOGIES if pattern.search(text)}
        technology = previous or (next(iter(mentioned)) if len(mentioned) == 1 else None)
        return [(technology, text, "") ] if technology else []
    sections = []
    if previous and matches[0][0] > 0:
        sections.append((previous, text[:matches[0][0]], "continued_previous_section"))
    for index, (_, end, technology, title) in enumerate(matches):
        stop = matches[index + 1][0] if index + 1 < len(matches) else len(text)
        sections.append((technology, text[end:stop], title))
    return sections


def _condition_flags(body: str) -> list[str]:
    flags = []
    if re.search(r"单个探头.{0,40}连续\s*(?:两个|2个?)\s*班", body):
        flags.append("same_probe_consecutive_shifts")
    if re.search(r"同一顺槽.{0,30}相邻.{0,12}(?:两个|2个?)\s*探头", body):
        flags.append("adjacent_probes_same_location")
    elif re.search(r"相邻.{0,12}(?:两个|2个?)\s*探头", body):
        flags.append("adjacent_probes")
    if re.search(r"(?:两个|2个?)\s*班", body):
        flags.append("two_shifts")
    if re.search(r"生产班|检修班", body):
        flags.append("shift_type_must_be_preserved")
    return list(dict.fromkeys(flags))


def _numeric_bounds(body: str) -> list[dict]:
    body = body.replace("＜", "<").replace("＞", ">")
    bounds = []
    for match in _RANGE.finditer(body):
        bounds.append({"kind": "range", "low": float(match.group("low")), "low_operator": match.group("low_op"),
                       "high": float(match.group("high")), "high_operator": match.group("high_op"), "unit": "%"})
    for match in _LOWER.finditer(body):
        bounds.append({"kind": "lower_bound", "value": float(match.group("value")),
                       "operator": match.group("op"), "unit": "%"})
    for match in _UPPER.finditer(body):
        if not any(item.get("kind") == "range" and item.get("high") == float(match.group("value")) for item in bounds):
            bounds.append({"kind": "upper_bound", "value": float(match.group("value")),
                           "operator": match.group("op"), "unit": "%"})
    return bounds


def _incomplete_condition(body: str) -> bool:
    """A split value or unmatched branch cannot be an executable threshold."""
    normalized = body.replace("＜", "<").replace("＞", ">")
    return bool(re.search(r"(?:dev\s*[<≤≥>]\s*(?!\d)|\d+(?:\.\d+)?\s*%?\s*[<≤≥>]\s*dev\s*<\s*(?!\d))", normalized, re.I))


def _branches(body: str) -> list[dict]:
    branches = []
    for segment in re.split(r"\s*(?:；|;|或(?!者))\s*", body):
        segment = segment.strip(" ，,。")
        if not segment:
            continue
        flags = _condition_flags(segment)
        if "same_probe_consecutive_shifts" in flags:
            branch_type = "same_probe_consecutive_shifts"
        elif "adjacent_probes_same_location" in flags or "adjacent_probes" in flags:
            branch_type = "adjacent_probes_same_location"
        elif _numeric_bounds(segment):
            branch_type = "simple_numeric"
        else:
            branch_type = "unknown"
        numeric = _numeric_bounds(segment)
        mixed = bool(re.search(r"其中一个(?:班|探头)|另一个(?:班|探头)", segment))
        executable = branch_type != "unknown" and bool(numeric) and not _incomplete_condition(segment)
        if mixed and len(numeric) < 2:
            executable = False
        branches.append({"branch_type": branch_type, "condition_text": segment,
                         "numeric_bounds": numeric, "executable": executable})
    return branches or [{"branch_type": "unknown", "condition_text": body, "numeric_bounds": [], "executable": False}]


def _rows_for_text(file: dict, entry: dict, text: str, technology: str | None) -> list[dict]:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not technology or not normalized:
        return []
    starts = list(_LEVEL_START.finditer(normalized))
    rows = []
    for index, match in enumerate(starts):
        body_end = starts[index + 1].start() if index + 1 < len(starts) else len(normalized)
        body = normalized[match.end():body_end].strip(" ;；。")
        if not re.search(r"dev|探头|预警|冲击|能量|频次", body, re.I):
            continue
        label = match.group("label")
        locator = entry.get("locator") or {}
        rows.append({
            "document": _file_info(file), "technology": technology, "grade_label": label,
            "condition_text": body, "condition_flags": _condition_flags(body),
            "numeric_bounds": _numeric_bounds(body), "branches": _branches(body),
            "evidence_refs": [entry.get("evidence_id")] if entry.get("evidence_id") else [],
            "locator": deepcopy(locator), "quote": normalized[max(0, match.start() - 80):min(len(normalized), body_end + 80)],
            "source_kind": "native_text", "status": "incomplete_condition" if _incomplete_condition(body) else "candidate_rule_text",
            "response_mentions": [name for name in ("停止作业", "停电", "人员撤离", "解危", "卸压", "恢复生产") if name in body],
            "response_verified": False,
        })
    return rows


def extract_threshold_profiles(files: list[dict], evidence: list[dict]) -> dict:
    """Extract per-document, per-technology threshold rows without choosing authority.

    Returns ``{documents, rows, by_technology, gaps, status, authority_selected}``.
    ``rows`` are source-bound rule candidates, not executable business facts.
    """
    files_by_id = {file.get("file_id"): file for file in files if file.get("file_id")}
    grouped: dict[str, list[dict]] = defaultdict(list)
    gaps = []
    for entry in evidence:
        file_id = entry.get("file_id")
        if file_id not in files_by_id or not _native(entry):
            continue
        grouped[file_id].append(entry)
    documents = []
    rows = []
    for file_id, file in files_by_id.items():
        entries = sorted(grouped.get(file_id, []), key=lambda item: (int((item.get("locator") or {}).get("page", 0) or 0), item.get("evidence_id", "")))
        active = None
        document_rows = []
        for entry in entries:
            text = str(entry.get("text") or entry.get("quote") or "")
            sections = _technology_sections(text, active)
            if sections:
                active = sections[-1][0]
            for technology, body, _title in sections:
                section_entry = {**entry, "text": body, "quote": body}
                document_rows.extend(_rows_for_text(file, section_entry, body, technology))
        document = {"document": _file_info(file), "row_count": len(document_rows),
                    "status": "parsed" if document_rows else "no_threshold_rows",
                    "evidence_count": len(entries)}
        if not entries:
            document["status"] = "no_native_text"
            gaps.append({"file_id": file_id, "reason": "threshold_document_no_native_text"})
        elif not document_rows:
            gaps.append({"file_id": file_id, "reason": "threshold_levels_not_structured"})
        documents.append(document)
        rows.extend(document_rows)
        for row in document_rows:
            if row["status"] == "incomplete_condition":
                gaps.append({"file_id": file_id, "technology": row["technology"], "grade_label": row["grade_label"],
                             "reason": "threshold_condition_truncated", "evidence_refs": row["evidence_refs"]})
    if not documents:
        gaps.append({"reason": "threshold_documents_missing"})
    by_technology = defaultdict(list)
    for row in rows:
        by_technology[row["technology"]].append(row)
    return {"policy_revision": POLICY_REVISION, "documents": documents, "rows": rows,
            "by_technology": dict(by_technology), "gaps": gaps,
            "status": "parsed" if rows else "unresolved", "authority_selected": False}


def _row_signature(row: dict) -> tuple:
    bounds = tuple(tuple(sorted(item.items())) for item in row.get("numeric_bounds", []))
    branches = tuple(sorted((item.get("branch_type"), item.get("condition_text", ""),
                             tuple(tuple(sorted(bound.items())) for bound in item.get("numeric_bounds", [])))
                            for item in row.get("branches", [])))
    return (row.get("technology"), row.get("grade_label"), branches, bounds,
            re.sub(r"\s+", "", row.get("condition_text", "")))


def compare_threshold_profiles(profile: dict) -> dict:
    """Compare repeated reports; never select the latest by date alone."""
    rows = profile.get("rows", []) if isinstance(profile, dict) else []
    by_technology = defaultdict(list)
    for row in rows:
        by_technology[row.get("technology")].append(row)
    comparisons = []
    for technology, tech_rows in sorted(by_technology.items()):
        by_document = defaultdict(list)
        for row in tech_rows:
            by_document[row.get("document", {}).get("file_id")].append(row)
        docs = sorted(by_document)
        hashes = sorted({row.get("document", {}).get("content_hash") for row in tech_rows if row.get("document", {}).get("content_hash")})
        document_signatures = {doc: tuple(sorted(_row_signature(row) for row in doc_rows)) for doc, doc_rows in by_document.items()}
        if len(docs) <= 1:
            classification = "single"
            reason = "只有一份文档有可结构化的该技术等级条件。"
        elif len(hashes) <= 1:
            classification = "duplicate"
            reason = "多份来源内容哈希相同，作为重复出处保留。"
        elif any(row.get("status") == "incomplete_condition" for row in tech_rows):
            classification = "unresolved_extraction"
            reason = "至少一份报告的等级条件跨页或被截断，需人工复核完整原文。"
        elif len(set(document_signatures.values())) == 1:
            classification = "equivalent_versions"
            reason = "不同来源的结构化条件一致，仍保留全部来源，不自动选权威版本。"
        else:
            classification = "conflict_or_parallel"
            reason = "不同来源的等级条件或数值边界不同；未根据日期自动选择版本。"
        comparisons.append({"technology": technology, "document_ids": docs, "content_hashes": hashes,
                            "classification": classification, "reason": reason,
                            "authority_selected": False, "rows": deepcopy(tech_rows)})
    return {"policy_revision": POLICY_REVISION, "comparisons": comparisons,
            "authority_selected": False,
            "has_unresolved_conflict": any(item["classification"] == "conflict_or_parallel" for item in comparisons)}
