"""Offline, deterministic checks; only cited execution facts prove execution.

No result editing, OCR guessing, model call, or cross-event evidence borrowing is
performed here. Insufficient evidence closes the current run as pending.
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .contracts import InvalidRequest, assert_snapshot_owner
from .fixtures import result_base
from .rulebook import DEFAULT_RULEBOOK_VERSION
from .grades import normalize_risk
from .units import quantity as shared_quantity
from .scope_checks import _parameter_matches


DETAILS_EFFECTIVE = date(2026, 9, 23)
STANDARD_EFFECTIVE = date(2026, 7, 1)
LAW_SOURCE = "《防治煤矿冲击地压细则》（矿安〔2026〕82号）"
LAW_URL = "https://www.chinamine-safety.gov.cn/zfxxgk/fdzdgknr/tzgg/202607/t20260724_677906.shtml"


def normalize_method(value):
    """Known spelling aliases only; unfamiliar method names stay distinct."""
    text = "".join(str(value or "").split()).lower()
    return {"应力监测": "应力监测法", "微震监测": "微震监测法",
            "钻屑": "钻屑法", "电磁辐射": "电磁辐射法"}.get(text, text)


def _fields(fact):
    return fact.get("fields", {})


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _day(value):
    value = _time(value)
    return value.date() if isinstance(value, datetime) else value


def _before(a, b):
    """Whether a strictly precedes b; equal/overlapping timestamps are unknown."""
    a, b = _time(a), _time(b)
    if a is None or b is None:
        return None
    if isinstance(a, datetime) and isinstance(b, datetime):
        try:
            return a < b if a != b else None
        except TypeError:
            return None  # Mixing explicit and unspecified time zones is ambiguous.
    ad = a.date() if isinstance(a, datetime) else a
    bd = b.date() if isinstance(b, datetime) else b
    return (ad < bd) if ad != bd else None


def _inclusive_before(a, b):
    """Inclusive boundary comparison, separate from a proven action sequence."""
    first, second = _time(a), _time(b)
    if isinstance(first, datetime) and isinstance(second, datetime):
        try:
            return first <= second
        except TypeError:
            return None
    return _before(a, b)


def _inside_observation(value, start, end):
    """Date-only observation bounds include the entire named local date."""
    value_time, lower, upper = _time(value), _time(start), _time(end)
    if value_time is None or lower is None or upper is None:
        return False
    day = value_time.date() if isinstance(value_time, datetime) else value_time
    begins = day >= lower if not isinstance(lower, datetime) else _inclusive_before(start, value) is True
    ends = day <= upper if not isinstance(upper, datetime) else _inclusive_before(value, end) is True
    return begins and ends


def _effective_at(fields, when):
    """A stated validity boundary is binding; date-only endpoints are inclusive."""
    value, day = _time(when), _day(when)
    if value is None:
        return False
    for name, lower in (("valid_from", True), ("valid_to", False)):
        if fields.get(name) in (None, ""):
            continue
        bound = _time(fields[name])
        if bound is None:
            return False
        if isinstance(bound, datetime):
            comparison = _inclusive_before(fields[name], when) if lower else _inclusive_before(when, fields[name])
            if comparison is not True:
                return False
        elif (day < bound if lower else day > bound):
            return False
    return True


def _denial_covers(denial, event):
    """A point-in-time negative is not presumed to remain true at another time."""
    denied_time, event_time = _time(denial.get("time")), _time(event.get("time"))
    if isinstance(denied_time, datetime) and isinstance(event_time, datetime) and denied_time == event_time:
        return True
    fields = _fields(denial)
    start, end = fields.get("status_valid_from"), fields.get("status_valid_to")
    return bool(start and end and _inside_observation(event.get("time"), start, end))


def _number(value):
    try:
        value = Decimal(str(value))
        return value if value.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _strict_before(a, b):
    return _before(a, b) is True and _time(a) != _time(b)


def _quantity(fields):
    """Dimension, converted value and scale family from the shared unit table."""
    value = _number(fields.get("value"))
    if value is None:
        return None
    converted = shared_quantity(value, fields.get("unit", ""))
    if converted is None:
        return None
    # A shared numeric unit must never make a drill-powder index into pressure.
    method = normalize_method(fields.get("method"))
    metric = str(fields.get("metric", ""))
    if (method == "钻屑法" or "钻粉" in metric or "钻屑" in metric) and converted[0] == "pressure":
        return None
    return converted


def _point(fields):
    return normalize_method(fields.get("method")), fields.get("metric"), fields.get("point")


def _law(article):
    return {"id": f"impact-details-2026-{article}", "source": LAW_SOURCE,
            "article": f"第{article}条", "effective_date": "2026-09-23", "url": LAW_URL}


class _Check:
    def __init__(self, run, snapshot):
        self.run, self.snapshot = run, snapshot
        self.item = (run["spec"].get("inspection_item") or {}).get("handler", run["spec"]["item_code"])
        self.rule_version = (snapshot.get("rule_baseline") or {}).get("version", "impact-1023-draft-v2")
        self.article = 57 if self.item == "stop_work_unload" else 78
        self.gaps = list(snapshot.get("coverage_gaps", []))
        self.propositions, self.findings, self.events = [], [], []
        self.candidate_findings = []
        self.limitations = []
        self.rejected_action_context = set()
        self.used = set()
        self.facts = []
        self.version_conflicts = _conflicted_evidence(snapshot)
        registered = {e["evidence_id"] for e in snapshot["evidence"]}
        seen = set()
        if not isinstance(snapshot.get("facts", []), list):
            raise InvalidRequest("facts must be a list")
        for f in snapshot.get("facts", []):
            if (not isinstance(f, dict) or not isinstance(f.get("fact_id"), str)
                    or f["fact_id"] in seen or not isinstance(f.get("fields", {}), dict)):
                raise InvalidRequest("invalid or duplicate fact")
            seen.add(f["fact_id"])
            refs = f.get("evidence_refs", [])
            if not isinstance(refs, list) or any(not isinstance(r, str) for r in refs) or not set(refs) <= registered:
                raise InvalidRequest("fact references unregistered evidence")
            if f.get("source_role") not in {"actual_record", "requirement", "statement", "regulation"}:
                raise InvalidRequest("invalid fact source role")
            if not refs:
                self.gap(f"uncited fact: {f['fact_id']}")
                continue
            if not f.get("event_id") or not f.get("workface"):
                self.gap(f"unscoped fact: {f['fact_id']}")
                continue
            self.facts.append(f)
        if snapshot.get("source_complete") is not True:
            self.gap("source coverage is incomplete")

    def _risk_grade(self, event, fs):
        """Grade of this event, taken only from explicit risk wording."""
        for source in [event] + list(fs):
            fields = _fields(source)
            if not fields.get("risk_grade") and not fields.get("hazard_grade"):
                continue
            if source is not event and fields.get("hazard_confirmed") is None:
                continue
            grade = normalize_risk(fields.get("risk_grade") or fields.get("hazard_grade"))
            if grade:
                return grade
        return None

    def gap(self, reason):
        if reason not in self.gaps:
            self.gaps.append(reason)

    def prop(self, event, code, state, facts=(), detail="", article=None, rule_refs=None, classification=None, human_confirmation_required=False):
        refs = sorted({r for f in facts if f for r in f.get("evidence_refs", [])})
        original_state = state
        if state in {"true", "false"} and set(refs) & self.version_conflicts:
            state = "missing"
            detail = (detail + "; " if detail else "") + "同一资料日期和类别存在不同内容版本，相关结论暂缓确定性判断"
        self.used.update(refs)
        p = {"event_id": event["event_id"], "workface": event["workface"],
             "code": code, "state": state, "evidence_refs": refs, "detail": detail,
             "law_refs": [] if (rule_refs or self.rule_version == "stop-work-unload-scope-v3") else [_law(article or self.article)]}
        if self.rule_version == "stop-work-unload-scope-v3":
            p["rule_refs"] = [{"version": self.rule_version, "revision": (self.snapshot.get("rule_baseline") or {}).get("revision"),
                               "source": (self.snapshot.get("rule_baseline") or {}).get("source"), "status": "development-draft"}]
        if classification:
            p["finding_classification"] = classification
        if human_confirmation_required:
            p["human_confirmation_required"] = True
        if rule_refs:
            p["rule_refs"] = rule_refs
        self.propositions.append(p)
        if original_state == "false" and state == "missing":
            self.candidate_findings.append({**p, "status": "candidate", "major_hazard_classification": "not_assessed"})
        if state == "false":
            if self.rule_version == "stop-work-unload-scope-v3":
                inferred = "suspected_major_hazard" if code in {"stop", "evacuate", "relief_violation"} else "suspected_violation" if code in {"power_off", "report"} else "review_required"
                p.setdefault("finding_classification", classification or inferred)
                if p["finding_classification"] == "suspected_major_hazard":
                    p["human_confirmation_required"] = True
                finding = {**p, "status": "no_pass", "major_hazard_classification": p["finding_classification"]}
            else:
                finding = {**p, "status": "no_pass", "major_hazard_classification": "not_assessed"}
            self.findings.append(finding)
        elif state != "true":
            self.gap(f"{event['event_id']}/{event['workface']}: {code}: {detail}")

    def order(self, event, code, first, second):
        known = first and second and _before(first.get("time"), second.get("time"))
        self.prop(event, code, "true" if known is True else "missing", [first, second],
                  "time chain is incomplete or precision is insufficient" if known is not True else "")
        return known

    def actions(self, fs, name):
        return [f for f in fs if f.get("kind") == "action" and f.get("source_role") == "actual_record"
                and _fields(f).get("action") == name
                and (name == "dispatch_release" or _fields(f).get("action_layer", "execution") != "instruction")]

    def context_actions(self, event, fs, name, role=None):
        result = []
        for f in self.actions(fs, name):
            fields = _fields(f)
            valid = (fields.get("actor_role") and fields.get("target_scope") == event["workface"]
                     and (not role or fields.get("actor_role") == role)
                     and _inclusive_before(event.get("time"), f.get("time")) is True)
            if valid:
                result.append(f)
            elif f["fact_id"] not in self.rejected_action_context:
                self.rejected_action_context.add(f["fact_id"])
                self.prop(event, "action_context:" + f["fact_id"], "missing", [f],
                          "action scope, responsible role or event-time relationship is not established")
        return result

    def action(self, event, fs, name, role=None, immediate=False, trigger=None, violation_context=True, violations=()):
        found = self.context_actions(event, fs, name, role)
        done = [f for f in found if _fields(f).get("status") == "done"]
        denied = [f for f in found if _fields(f).get("status") == "not_done"]
        if denied and done:
            self.prop(event, name, "missing", found, "conflicting execution records")
            return None
        if denied:
            instants = list(violations) or (denied if self.rule_version == "stop-work-unload-scope-v3" else [trigger] if trigger else [])
            linked = [f for f in denied if any(_denial_covers(f, instant) for instant in instants)]
            self.prop(event, name, "false" if violation_context and linked else "missing", denied + instants,
                      "negative execution record is only conclusive at its own instant or explicitly covered status interval")
            return None
        if len(done) != 1:
            self.prop(event, name, "missing", found, "missing or ambiguous actual execution record")
            return None
        f, fields = done[0], _fields(done[0])
        if name == "evacuate" and fields.get("route_conforms") is not True:
            self.prop(event, name, "false" if fields.get("route_conforms") is False and violation_context else "missing", done,
                      "evacuation route conformity must be explicitly evidenced")
            return None
        if immediate:
            # No invented number of minutes is a legal definition of immediately.
            if fields.get("immediate") is not True:
                self.prop(event, name + "_immediate", "false" if fields.get("immediate") is False else "missing", done,
                          "immediate response requires explicit actual-record evidence")
                return None
            self.order(event, name + "_after_trigger", trigger, f)
        self.prop(event, name, "true", done)
        return f

    def compare(self, event, measurement, fs):
        mf = _fields(measurement)
        when = _day(measurement.get("time"))
        q = _quantity(mf)
        candidates = []
        graded = []
        scale_mismatch = []
        for f in fs:
            fields = _fields(f)
            if (f.get("kind") != "threshold" or f.get("source_role") != "requirement"
                    or fields.get("approved") is not True or _point(fields)[:2] != _point(mf)[:2]
                    or fields.get("point") not in (None, "", mf.get("point"))):
                continue
            start, end = _day(fields.get("valid_from")), _day(fields.get("valid_to"))
            if not when or not start or when < start or (fields.get("valid_to") and (not end or when > end)):
                continue
            if isinstance(_time(fields.get("valid_from")), datetime) and _inclusive_before(fields["valid_from"], measurement.get("time")) is not True:
                continue
            if isinstance(_time(fields.get("valid_to")), datetime) and _inclusive_before(measurement.get("time"), fields["valid_to"]) is not True:
                continue
            tq = _quantity(fields)
            if tq and q and tq[0] == q[0]:
                if tq[2] != q[2]:
                    # Same dimension, different scale (percent vs ratio). Comparing
                    # the raw numbers would fabricate a direction, so refuse.
                    scale_mismatch.append(f)
                    continue
                grade = normalize_risk(fields.get("grade")) if fields.get("grade") else None
                if grade:
                    graded.append((tq[1], f, grade))
                else:
                    candidates.append((tq[1], f))
        # Grade-matrix thresholds (蓝/黄/橙/红) are only usable with the grade of
        # this very event; without it no comparison may happen.
        graded_note = None
        if graded:
            event_grade = self._risk_grade(event, fs)
            if event_grade is None:
                graded_note = "阈值按色阶（蓝/黄/橙/红）给出，但事件危险等级缺失，已拒绝比较"
                candidates = []
            else:
                candidates.extend((value, f) for value, f, grade in graded if grade == event_grade)
                if not candidates:
                    graded_note = "色阶阈值中没有与事件危险等级匹配的档位"
        # Even equal-valued overlapping approvals need explicit supersession.
        if len(candidates) != 1:
            detail = "missing, expired, unsupported-unit or conflicting approved threshold"
            if graded_note:
                detail = graded_note
                if graded_note not in self.limitations:
                    self.limitations.append(graded_note)
            if scale_mismatch and not candidates and not graded_note:
                detail = "threshold unit scale differs from the measurement (percent vs ratio); comparison refused"
                note = "阈值与实测值的单位刻度不一致（百分比 vs 无量纲），已拒绝换算比较"
                if note not in self.limitations:
                    self.limitations.append(note)
            # No hard-coded statute here: the rulebook registers which articles
            # cover this condition. Inventing 第27条 would be a fabricated citation.
            self.prop(event, "applicable_threshold", "missing",
                      [measurement] + [f for _, f in candidates] + scale_mismatch, detail)
            return None
        value, threshold = candidates[0]
        self.used.update(measurement["evidence_refs"] + threshold["evidence_refs"])
        return (q[1] > value) - (q[1] < value), threshold

    def check57_v3(self, event, fs):
        # v3 separates trigger recognition from the danger assessment. A
        # dynamic record alone is not enough to enter the response branch.
        trigger = None
        dynamic = _fields(event).get("trigger_type") == "dynamic"
        warning_measurements = []
        for m in fs:
            if m.get("kind") != "measurement" or m.get("source_role") != "actual_record" or _fields(m).get("stage") != "warning":
                continue
            comparison = self.compare(event, m, fs)
            if comparison and comparison[0] > 0:
                trigger = m
                warning_measurements.append(m)
                self.prop(event, "threshold_trigger", "true", [m, comparison[1]])
        if trigger is None and not dynamic:
            self.prop(event, "trigger", "missing", [event], "warning alone does not prove a statutory trigger")
            return
        trigger_fact = trigger or event

        analyses = [f for f in self.context_actions(event, fs, "hazard_analysis")
                    if _fields(f).get("status") == "done"]
        confirmed = _fields(event).get("hazard_confirmed")
        confirmed = confirmed if isinstance(confirmed, bool) else None
        hazard_anchor = trigger_fact
        if len(analyses) == 1 and isinstance(_fields(analyses[0]).get("hazard_confirmed"), bool):
            analysis_value = _fields(analyses[0])["hazard_confirmed"]
            if _before(trigger_fact.get("time"), analyses[0].get("time")) is not True:
                confirmed = None
                self.prop(event, "hazard_analysis", "missing", analyses + [event], "危险研判与触发时间先后无法确定")
            elif confirmed is not None and confirmed != analysis_value:
                confirmed = None
                self.prop(event, "hazard_analysis", "missing", analyses + [event], "hazard conclusions conflict")
            else:
                confirmed = analysis_value
                hazard_anchor = analyses[0]
                self.order(event, "analysis_after_trigger", trigger_fact, hazard_anchor)
        elif analyses:
            confirmed = None
            self.prop(event, "hazard_analysis", "missing", analyses, "danger assessment is ambiguous")
        else:
            confirmed = None

        if dynamic and trigger is None and confirmed is not True:
            self.prop(event, "trigger", "missing", [event], "动力现象缺少同一事件明确的冲击地压危险研判")
            self.prop(event, "hazard_analysis", "missing", [event], "动力现象未形成危险确认，未进入处置核验")
            return
        if not dynamic:
            self.prop(event, "trigger", "true", [trigger_fact])
        else:
            self.prop(event, "trigger", "true", [trigger_fact, hazard_anchor])
        self.prop(event, "danger_assessment", "true" if confirmed is not None else "missing", [hazard_anchor] + analyses,
                  "危险研判缺失或冲突" if confirmed is None else "")
        self.propositions[-1]["applicable"] = confirmed is True
        self.prop(event, "danger_confirmed", "true" if confirmed is not None else "missing",
                  analyses + [event] if confirmed is not None else [event],
                  "明确危险研判" if confirmed is True else "明确无危险研判" if confirmed is False else "危险研判缺证")
        self.propositions[-1]["applicable"] = confirmed is True
        warning_danger = "true" if warning_measurements and confirmed is True else "missing" if warning_measurements and confirmed is None else "false"
        self.prop(event, "warning_danger_confirmed", "true" if warning_danger in {"true", "false"} else "missing", [hazard_anchor] + warning_measurements,
                  "预警超阈值后危险研判缺证" if warning_danger == "missing" else "")
        self.propositions[-1]["applicable"] = warning_danger == "true"
        immediate_actions = {a: self.action(event, fs, a, trigger=trigger_fact)
                             for a in ("stop", "power_off", "evacuate", "report")}
        ordered = [("stop", immediate_actions["stop"]), ("power_off", immediate_actions["power_off"]),
                   ("evacuate", immediate_actions["evacuate"]), ("report", immediate_actions["report"])]
        for (first_name, first), (second_name, second) in zip(ordered, ordered[1:]):
            first_time, second_time = _time(first.get("time")) if first else None, _time(second.get("time")) if second else None
            relation = (_inclusive_before(first["time"], second["time"])
                        if isinstance(first_time, datetime) and isinstance(second_time, datetime) else None)
            self.prop(event, f"sequence_{first_name}_before_{second_name}",
                      "true" if relation is True else "false" if relation is False else "missing", [first, second],
                      "必须使用精确时间证明处置顺序；同一精确时间允许，日期级记录不证明先后")
        # The v3 plan only classifies explicit response failures. Absence stays
        # pending and is never converted into a negative action.
        if self.rule_version == "stop-work-unload-scope-v3":
            relief_required = "missing"
            if warning_danger == "true":
                relief_required = "true"
            elif warning_danger == "false":
                relief_required = "false"
            self.prop(event, "relief_required", "true" if relief_required in {"true", "false"} else "missing", analyses + ([trigger] if trigger else []),
                      "危险确认时需要核对针对性卸压措施" if relief_required == "true" else "")
            self.propositions[-1]["applicable"] = relief_required == "true"
            if relief_required == "true":
                self.check_relief_v3(event, fs, hazard_anchor)
            return

    def check_relief_v3(self, event, fs, hazard_anchor):
        designs = [f for f in fs if f.get("kind") == "design" and f.get("source_role") == "requirement"
                   and _fields(f).get("approved") is True and _fields(f).get("target_scope") == event["workface"]
                   and _effective_at(_fields(f), event.get("time"))]
        design = designs[0] if len(designs) == 1 else None
        if design is None:
            self.prop(event, "applicable_design", "missing", [event] + designs,
                      "缺少同工作面、唯一、有效且已批准的防冲设计")
        else:
            self.prop(event, "applicable_design", "true", [design])
        records = self.context_actions(event, fs, "relief")
        completed = [r for r in records if _fields(r).get("status") == "done"]
        denied = [r for r in records if _fields(r).get("status") == "not_done"]
        if len(completed) != 1 or denied:
            self.prop(event, "relief_measures", "missing", records,
                      "缺少唯一实际施工及验收记录，或存在相互冲突的执行状态")
            return
        relief = completed[0]
        df, lf = _fields(design) if design else {}, _fields(relief)
        checks = []
        checks.append(True if _before(hazard_anchor.get("time"), relief.get("time")) is True else None)
        methods = df.get("relief_methods")
        checks.append(lf["method"] in methods if lf.get("method") and isinstance(methods, list) and methods else None)
        checks.append(lf["relief_area"] == df["relief_area"] if lf.get("relief_area") and df.get("relief_area") else None)
        checks.append(lf.get("acceptance_passed") if isinstance(lf.get("acceptance_passed"), bool) else None)
        checks.append(True if lf.get("acceptance_signer") else None)
        checks.append(_inclusive_before(relief.get("time"), lf.get("acceptance_time"))
                      if isinstance(_time(relief.get("time")), datetime) and isinstance(_time(lf.get("acceptance_time")), datetime) else None)
        requirements, actual = df.get("relief_parameters"), lf.get("parameters")
        if isinstance(requirements, list) and requirements:
            for requirement in requirements:
                found = [item for item in actual or [] if isinstance(item, dict) and isinstance(requirement, dict)
                         and item.get("name") == requirement.get("name")] if isinstance(actual, list) else []
                checks.append(_parameter_matches(found[0], requirement) if len(found) == 1 else None)
        else:
            checks.append(None)
        status = "false" if False in checks else "missing" if None in checks else "true"
        self.prop(event, "relief_measures", status, [event, design, relief],
                  "施工类型、参数、范围、时间和验收结果须与有效防冲设计逐项一致")

    def check57(self, event, fs):
        if self.rule_version == "stop-work-unload-scope-v3":
            return self.check57_v3(event, fs)
        trigger = event if _fields(event).get("trigger_type") == "dynamic" else None
        for m in fs:
            if m.get("kind") != "measurement" or m.get("source_role") != "actual_record" or _fields(m).get("stage") != "warning":
                continue
            comparison = self.compare(event, m, fs)
            drill_index = normalize_method(_fields(m).get("method")) == "钻屑法" and _fields(m).get("metric") in {"钻粉率指数", "drill_powder_index"}
            if comparison and (comparison[0] > 0 or (drill_index and comparison[0] == 0)):
                trigger = m
                self.prop(event, "threshold_trigger", "true", [m, comparison[1]])
                if drill_index:
                    self.propositions[-1]["law_refs"] = []
                    note = "钻粉率指数触发条件未在规则表登记法条，本条不附法规出处"
                    if note not in self.limitations:
                        self.limitations.append(note)
        if trigger is None:
            self.prop(event, "trigger", "missing", [event], "warning alone does not prove a statutory trigger")
            return
        self.prop(event, "trigger", "true", [trigger])
        immediate_actions = {a: self.action(event, fs, a, immediate=True, trigger=trigger)
                             for a in ("stop", "power_off", "evacuate", "report")}

        baseline = self.snapshot.get("rule_baseline") or {}
        if baseline.get("version") in {"impact-1023-draft-v1", "impact-1023-draft-v2"}:
            source = baseline.get("source") or {}
            reference = {"version": baseline["version"], "revision": baseline.get("revision"),
                         "source": source,
                         "source_cells": [f"{source.get('sheet', 'AI深度监察')}!{cell}" for cell in ("E2", "F2")],
                         "status": baseline.get("status", "development-draft")}
            relief_records = self.context_actions(event, fs, "relief")
            relief = (relief_records[0] if len(relief_records) == 1
                      and _fields(relief_records[0]).get("status") == "done" else None)
            later_actions = [("evacuate", immediate_actions["evacuate"])]
            if relief_records:
                later_actions.append(("relief", relief))
            for later_name, later in later_actions:
                for first_name in ("stop", "power_off"):
                    first = immediate_actions[first_name]
                    times = [_time(f.get("time")) if f else None for f in (first, later)]
                    known = (_inclusive_before(first["time"], later["time"])
                             if all(isinstance(t, datetime) for t in times) else None)
                    self.prop(event, f"draft_{first_name}_not_after_{later_name}",
                              "true" if known is True else "false" if known is False else "missing",
                              [first, later],
                              "Draft workbook E2/F2 requires stop and power-off no later than evacuation and relief; "
                              "equal precise timestamps are allowed, date-only timing is unresolved; no minute limit is inferred.",
                              rule_refs=[reference])

        # A separately recorded analysis may be in another file. Resolve it
        # before continued work, without delaying the immediate responses above.
        analyses = [f for f in self.context_actions(event, fs, "hazard_analysis")
                    if _fields(f).get("status") == "done"]
        confirmed = _fields(event).get("hazard_confirmed")
        confirmed = confirmed if isinstance(confirmed, bool) else None
        hazard_anchor = trigger
        if analyses:
            if len(analyses) != 1 or not isinstance(_fields(analyses[0]).get("hazard_confirmed"), bool):
                self.prop(event, "hazard_analysis", "missing", analyses, "danger assessment is ambiguous")
                confirmed = None
            elif confirmed is not None and confirmed != _fields(analyses[0])["hazard_confirmed"]:
                self.prop(event, "hazard_analysis", "missing", analyses + [event], "hazard conclusions conflict")
                confirmed = None
            else:
                confirmed = _fields(analyses[0])["hazard_confirmed"]
                hazard_anchor = analyses[0]
                self.order(event, "analysis_after_trigger", trigger, hazard_anchor)
        # A later return to work may be authorized; it is not automatically a 57 violation.
        for f in self.context_actions(event, fs, "continue_work"):
            if _fields(f).get("status") != "done" or not _strict_before(trigger.get("time"), f.get("time")):
                continue
            stop = immediate_actions["stop"]
            reliefs = [r for r in self.context_actions(event, fs, "relief") if _fields(r).get("status") == "done"]
            before_stop = stop and _strict_before(f.get("time"), stop.get("time"))
            before_relief = (confirmed is True and _strict_before(hazard_anchor.get("time"), f.get("time"))
                             and len(reliefs) == 1 and _strict_before(f.get("time"), reliefs[0].get("time")))
            explicit_failure = [r for r in self.context_actions(event, fs, "stop") if _fields(r).get("status") == "not_done" and _denial_covers(r, f)]
            if before_stop or before_relief or explicit_failure:
                self.prop(event, "continued_after_trigger", "false", [trigger, f, stop] + reliefs + explicit_failure + ([event, hazard_anchor] if before_relief else []),
                          "positive records prove continued work before stopping or before confirmed-danger relief")
            else:
                self.prop(event, "continuation_context", "missing", [trigger, f], "later work requires a separately evidenced lawful release; continuation alone is not proof of a violation")

        if confirmed is None:
            self.prop(event, "hazard_analysis", "missing", [event], "danger assessment is missing; relief cannot be inferred")
        elif confirmed is True:
            relief = self.action(event, fs, "relief")
            self.order(event, "relief_after_hazard", hazard_anchor, relief)
        else:
            if not analyses:
                self.prop(event, "hazard_analysis", "missing", [event], "negative danger assessment needs its actual analysis record")
            else:
                self.prop(event, "relief_not_required_by_assessment", "true", analyses)

    def check78(self, event, fs):
        resumes = [f for f in self.context_actions(event, fs, "resume") + self.context_actions(event, fs, "continue_work") if _fields(f).get("status") == "done"]
        relief = self.action(event, fs, "relief", violation_context=bool(resumes), violations=resumes)
        inspection = self.action(event, fs, "inspection", violation_context=bool(resumes), violations=resumes)
        self.order(event, "inspection_after_relief", relief, inspection)
        requirements = [f for f in fs if f.get("kind") == "requirements" and f.get("source_role") == "requirement"
                        and _fields(f).get("approved") is True and _fields(f).get("required_points")]
        required_points = []
        if len(requirements) != 1:
            self.prop(event, "required_points", "missing", requirements, "one applicable approved full method/point specification is required")
        else:
            req_fields = _fields(requirements[0])
            if not req_fields.get("valid_from") and not req_fields.get("valid_to"):
                self.limitations.append("Approved required-point list has no validity dates; applicability is limited to its explicit event/workface binding, with no cross-period assurance.")
            raw_points = _fields(requirements[0])["required_points"]
            if isinstance(raw_points, list) and all(isinstance(p, dict) and all(_point(p)) for p in raw_points):
                required_points = [_point(p) for p in raw_points]
                if len(set(required_points)) != len(required_points):
                    required_points = []
            if not required_points:
                self.prop(event, "required_points", "missing", requirements, "invalid or duplicated required points")
            else:
                self.prop(event, "required_points", "true", requirements)
        # Resume is useful positive evidence; absence of it alone is not a violation.
        measurements = []
        for point in required_points:
            found = [f for f in fs if f.get("kind") == "measurement" and f.get("source_role") == "actual_record"
                     and _fields(f).get("stage") == "effect" and _point(_fields(f)) == point]
            if len(found) != 1:
                self.prop(event, "measurement:" + repr(point), "missing", found, "all required raw measurements must be uniquely established")
                continue
            m = found[0]
            measurements.append(m)
            self.order(event, "measurement_after_relief:" + m["fact_id"], relief, m)
            self.order(event, "inspection_measurement_link:" + m["fact_id"], inspection, m)
            if not _effective_at(_fields(requirements[0]), m.get("time")):
                self.prop(event, "required_points_effective:" + m["fact_id"], "missing", requirements + [m],
                          "approved method/point specification is not effective at measurement time")
                continue
            comparison = self.compare(event, m, fs)
            if comparison is None:
                continue
            cmp, threshold = comparison
            if cmp < 0:
                self.prop(event, "below_threshold:" + m["fact_id"], "true", [m, threshold])
            else:
                after = [r for r in resumes if _strict_before(m.get("time"), r.get("time"))]
                self.prop(event, "below_threshold:" + m["fact_id"], "false" if after else "missing", [m, threshold] + after,
                          "effect must be strictly below threshold; equality is not the major-hazard standard's 'exceeds'")
        technical = self.action(event, fs, "technical_confirm", role="technical_head", violation_context=bool(resumes), violations=resumes)
        dispatch = self.action(event, fs, "dispatch_release", role="dispatch", violation_context=bool(resumes), violations=resumes)
        for m in measurements:
            self.order(event, "confirmation_after_measurement:" + m["fact_id"], m, technical)
        self.order(event, "confirmation_after_inspection", inspection, technical)
        self.order(event, "release_after_confirmation", technical, dispatch)
        for r in resumes:
            for name, prerequisite in (("technical_confirm", technical), ("dispatch_release", dispatch)):
                if prerequisite and _before(prerequisite.get("time"), r.get("time")) is False:
                    self.prop(event, "resume_before_" + name, "false", [prerequisite, r], "positive timestamps prove premature resumption")
                else:
                    self.order(event, "resume_after_" + name, prerequisite, r)
        if not resumes:
            self.prop(event, "resumption_observation", "missing", [], "current closed-work state may be safe; no completed resumption chain was observed")

    def finish(self):
        events = [f for f in self.facts if f.get("kind") == "event" and f.get("source_role") == "actual_record"]
        keys = {(e["event_id"], e["workface"]) for e in events}
        for key in sorted(keys):
            es = [e for e in events if (e["event_id"], e["workface"]) == key]
            e = es[0]
            self.events.append({"event_id": key[0], "workface": key[1], "evidence_refs": e["evidence_refs"]})
            if len(es) != 1 or _time(e.get("time")) is None:
                self.prop(e, "event_identity", "missing", es, "event identity or time is ambiguous")
                continue
            if _day(e["time"]) < DETAILS_EFFECTIVE:
                self.prop(e, "historical_rule", "missing", [e], "historical rule required: event predates 2026-09-23")
                continue
            fs = [f for f in self.facts if (f["event_id"], f["workface"]) == key]
            self.used.update(e["evidence_refs"])
            observation = self.snapshot.get("observation", {})
            start, end = observation.get("start"), observation.get("end")
            if any(not _inside_observation(f.get("time"), start, end)
                   for f in fs if f.get("kind") in {"event", "action", "measurement"}):
                self.prop(e, "observation_window", "missing", [e], "event/chain is not fully bounded by the input observation")
            (self.check57 if self.item == "stop_work_unload" else self.check78)(e, fs)
        if not keys:
            self.gap("no actual event observed; absence of clues cannot establish compliance")
        if any((f["event_id"], f["workface"]) not in keys for f in self.facts):
            self.gap("orphan event/workface facts could not be evaluated")
        if self.findings:
            return self.result("no_pass", "实际证据证明至少一项必要条件不满足")
        if self.version_conflicts:
            return self.result("unverifiable", "存在内容版本冲突，相关结论无法确定")
        unreadable = any(f.get("state") == "unreadable" for f in self.snapshot.get("coverage", []))
        unreadable = unreadable or any(str(g).lower().find("unreadable") >= 0 or "requires_ocr" in str(g) for g in self.gaps)
        if unreadable:
            return self.result("unverifiable", "必要内容不可读，本轮无法判定")
        if self.gaps or not self.propositions or any(p["state"] != "true" for p in self.propositions):
            return self.result("pending", "证据或检查范围不完整，本轮结束")
        return self.result("pass", "全部必要条件形成完整证据链")

    def result(self, status, description):
        r = result_base(self.run, status, description,
                        "本地确定性规则；仅使用实际记录证明执行，不以报告自述代替原始指标；不进行人工改判或重大隐患升级定性。")
        r.update(profile="local-documents", snapshot_id=self.snapshot["snapshot_id"],
                 evidence_refs=sorted(self.used), coverage_gaps=self.gaps,
                 limitations=list(dict.fromkeys(self.limitations)),
                 events=self.events, propositions=self.propositions, findings=self.findings,
                 candidate_findings=self.candidate_findings,
                 version_conflicts=sorted(self.version_conflicts),
                 law_refs=(list({law["id"]: law for p in self.propositions for law in p["law_refs"]}.values())
                           if self.rule_version == "stop-work-unload-scope-v3" else list({law["id"]: law for p in self.propositions for law in p["law_refs"]}.values()) or [_law(self.article)]),
                 attachments=[{"file_id": f["file_id"]} for f in self.snapshot["files"]])
        gated = gate_conflicted_evidence(r, self.snapshot)
        return classify_stop_findings(gated, self.snapshot) if self.item == "stop_work_unload" and self.rule_version == "stop-work-unload-scope-v3" else gated


def classify_stop_findings(result, snapshot):
    """Keep statutory-risk candidates separate from the five technical statuses."""
    by_event = {}
    for prop in result.get("propositions", []):
        by_event.setdefault((prop.get("event_id"), prop.get("workface")), {}).setdefault(prop.get("code"), []).append(prop)
    facts = snapshot.get("facts", [])
    weak_facts = {link.get("fact_id") for link in snapshot.get("association_links", []) if link.get("temporal_partition")}
    weak_refs = {ref for fact in facts if fact.get("fact_id") in weak_facts for ref in fact.get("evidence_refs", [])}
    findings, uncertain = [], []
    for (event_id, workface), grouped in by_event.items():
        def proved(code, *, applicable=None):
            return next((p for p in grouped.get(code, []) if p.get("state") == "true"
                         and (applicable is None or p.get("applicable") is applicable)), None)
        trigger = proved("trigger")
        hazard = proved("danger_confirmed", applicable=True)
        warning = proved("warning_danger_confirmed", applicable=True)
        design = proved("applicable_design")
        if not trigger:
            continue
        def add(prop, classification, extra=()):
            refs = sorted(set(prop.get("evidence_refs", [])) | {ref for item in extra if item for ref in item.get("evidence_refs", [])})
            finding = {**prop, "evidence_refs": refs, "status": "no_pass", "finding_classification": classification,
                       "major_hazard_classification": classification,
                       "human_confirmation_required": classification == "suspected_major_hazard"}
            if not refs or set(refs) & weak_refs:
                uncertain.append({**finding, "status": "candidate", "finding_classification": "review_required"})
            else:
                findings.append(finding)
        if hazard:
            for code in ("stop", "evacuate"):
                for prop in grouped.get(code, []):
                    if prop.get("state") == "false":
                        add(prop, "suspected_major_hazard", (trigger, hazard))
            stopped = [f for f in facts if f.get("kind") == "action" and f.get("source_role") == "actual_record"
                       and (f.get("event_id"), f.get("workface")) == (event_id, workface)
                       and _fields(f).get("action") == "stop" and _fields(f).get("status") == "done"
                       and _fields(f).get("target_scope") == workface]
            continued = [f for f in facts if f.get("kind") == "action" and f.get("source_role") == "actual_record"
                         and (f.get("event_id"), f.get("workface")) == (event_id, workface)
                         and _fields(f).get("action") == "continue_work" and _fields(f).get("status") == "done"
                         and _fields(f).get("target_scope") == workface]
            for work in continued:
                late_stop = [f for f in stopped if _strict_before(work.get("time"), f.get("time"))
                             and _strict_before(trigger.get("time") or next((e.get("time") for e in facts if e.get("event_id") == event_id and e.get("kind") == "event"), None), work.get("time"))]
                if late_stop:
                    prop = {"event_id": event_id, "workface": workface, "code": "continued_before_stop",
                            "state": "false", "evidence_refs": sorted(set(work["evidence_refs"] + late_stop[0]["evidence_refs"])),
                            "detail": "实际继续采掘记录早于实际停工记录", "law_refs": []}
                    result["propositions"].append(prop)
                    add(prop, "suspected_major_hazard", (trigger, hazard))
        if warning and hazard:
            continuation = [f for f in facts if f.get("kind") == "action" and f.get("source_role") == "actual_record"
                            and (f.get("event_id"), f.get("workface")) == (event_id, workface)
                            and _fields(f).get("action") == "continue_work" and _fields(f).get("status") == "done"
                            and _fields(f).get("actor_role") and _fields(f).get("target_scope") == workface]
            reliefs = [f for f in facts if f.get("kind") == "action" and f.get("source_role") == "actual_record"
                       and (f.get("event_id"), f.get("workface")) == (event_id, workface)
                       and _fields(f).get("action") == "relief"]
            bad_relief = [p for p in grouped.get("relief_measures", []) if p.get("state") == "false"]
            for work in continuation:
                if not any(_strict_before(f.get("time"), work.get("time")) for f in facts if f.get("kind") == "action"
                           and (f.get("event_id"), f.get("workface")) == (event_id, workface)
                           and _fields(f).get("action") == "hazard_analysis" and _fields(f).get("hazard_confirmed") is True):
                    continue
                denied = [f for f in reliefs if _fields(f).get("status") == "not_done" and _denial_covers(f, work)]
                mismatched = [p for p in bad_relief if design and any(_strict_before(f.get("time"), work.get("time")) for f in reliefs
                                                     if _fields(f).get("status") == "done" and set(f.get("evidence_refs", [])) & set(p.get("evidence_refs", [])))]
                if denied or mismatched:
                    refs = sorted(set(trigger["evidence_refs"] + hazard["evidence_refs"] + work["evidence_refs"]
                                      + [ref for f in denied for ref in f["evidence_refs"]]
                                      + [ref for p in mismatched for ref in p["evidence_refs"]]))
                    prop = {"event_id": event_id, "workface": workface, "code": "relief_violation", "state": "false",
                            "evidence_refs": refs, "detail": "预警后危险研判明确、针对性卸压有确证不符且仍采掘",
                            "law_refs": [_law(57)]}
                    if not any(p.get("code") == "relief_violation" and p.get("evidence_refs") == refs for p in result["propositions"]):
                        result["propositions"].append(prop)
                    add(prop, "suspected_major_hazard")
        for code in ("power_off", "report"):
            for prop in grouped.get(code, []):
                if prop.get("state") == "false":
                    add(prop, "suspected_violation", (trigger,))
    result["findings"] = list({(f["event_id"], f["workface"], f["code"], tuple(f["evidence_refs"])): f for f in findings}.values())
    result["candidate_findings"] = list({(f.get("event_id"), f.get("workface"), f.get("code"), tuple(f.get("evidence_refs", []))): f
                                         for f in result.get("candidate_findings", []) + uncertain}.values())
    accepted = {(f.get("event_id"), f.get("workface"), f.get("code")) for f in result["findings"]}
    for p in result["propositions"]:
        if p.get("state") == "false" and (p.get("event_id"), p.get("workface"), p.get("code")) not in accepted:
            gap = f"{p.get('event_id')}/{p.get('workface')}: {p.get('code')}: 需人工复核明确反证"
            if gap not in result["coverage_gaps"]:
                result["coverage_gaps"].append(gap)
    grades = {f["finding_classification"] for f in result["findings"]}
    if "suspected_major_hazard" in grades:
        result.update(status="no_pass", description="疑似重大事故隐患，停工与撤人分支须人工确认",
                      classification="suspected_major_hazard", human_confirmation_required=True)
    elif "suspected_violation" in grades:
        result.update(status="no_pass", description="疑似违规，不单独套用本项重大隐患条款",
                      classification="suspected_violation", human_confirmation_required=False)
    elif result.get("version_conflicts"):
        result.update(status="unverifiable", description="存在来源版本冲突，待人工复核", classification="review_required")
    elif any(row.get("state") == "unreadable" for row in snapshot.get("coverage", [])):
        result.update(status="unverifiable", description="必要内容不可读，待人工复核", classification="review_required")
    elif result.get("coverage_gaps") or not result.get("propositions") or any(p.get("state") != "true" for p in result["propositions"]):
        result.update(status="pending", description="待补证/人工复核", classification="review_required")
    else:
        result.update(status="pass", description="本次事件未发现相应问题", classification="no_issue_observed")
    result["detail"] = "按用户确认的三步规则核对；疑似重大事故隐患仅为需人工确认的候选，缺证不作未实施判断。"
    return result


def evaluate_documents(run, snapshot):
    """Evaluate registered local facts; schema/ownership errors are technical errors."""
    assert_snapshot_owner(snapshot, run)
    handler = (run["spec"].get("inspection_item") or {}).get("handler", run["spec"]["item_code"])
    if handler not in {"stop_work_unload", "relief_effects"}:
        raise InvalidRequest("unsupported statutory rule")
    return _Check(run, snapshot).finish()


def _conflicted_evidence(snapshot):
    """Identify evidence tied to differing bytes for one report date/category."""
    files = {f.get("file_id"): f for f in snapshot.get("files", []) if f.get("file_id")}
    groups = {}
    for file_id, file in files.items():
        day = file.get("report_date") or (file.get("source_metadata") or {}).get("report_date")
        category = file.get("material_group") or (file.get("source_metadata") or {}).get("material_group")
        digest = file.get("content_hash")
        if day and category and digest:
            groups.setdefault((day, category), []).append((file_id, digest))
    conflicted_files = set()
    for members in groups.values():
        if len({digest for _, digest in members}) > 1:
            conflicted_files.update(file_id for file_id, _ in members)
    evidence = {e.get("evidence_id"): e for e in snapshot.get("evidence", [])}
    return {eid for eid, entry in evidence.items() if entry.get("file_id") in conflicted_files}


def gate_conflicted_evidence(result, snapshot):
    """Quarantine only findings backed by conflicting source bytes.

    This helper is idempotent and does not mutate ``snapshot``.  It is exposed
    for the service merge path where scope-check propositions are combined with
    this evaluator's result.
    """
    import copy
    gated = copy.deepcopy(result)
    conflicted = _conflicted_evidence(snapshot)
    if not conflicted:
        return gated
    gated["version_conflicts"] = sorted(set(gated.get("version_conflicts", [])) | conflicted)
    dependent_events = {(fact.get("event_id"), fact.get("workface"))
                        for fact in snapshot.get("facts", [])
                        if fact.get("kind") == "event" and set(fact.get("evidence_refs", [])) & conflicted}
    dependent_events.update((p.get("event_id"), p.get("workface"))
                            for p in gated.get("propositions", [])
                            if p.get("code") in {"trigger", "threshold_trigger"}
                            and set(p.get("evidence_refs", [])) & conflicted)
    findings, kept = [], []
    for finding in gated.get("findings", []):
        if (set(finding.get("evidence_refs", [])) & conflicted
                or (finding.get("event_id"), finding.get("workface")) in dependent_events):
            candidate = {**finding, "status": "candidate", "state": "missing"}
            kept.append(candidate)
        else:
            findings.append(finding)
    gated["findings"] = findings
    gated["candidate_findings"] = gated.get("candidate_findings", []) + kept
    affected = bool(kept) or any(set(p.get("evidence_refs", [])) & conflicted
                               for p in gated.get("propositions", []))
    for proposition in gated.get("propositions", []):
        if (set(proposition.get("evidence_refs", [])) & conflicted
                or (proposition.get("event_id"), proposition.get("workface")) in dependent_events) and proposition.get("state") in {"true", "false"}:
            proposition["state"] = "missing"
            affected = True
            proposition["detail"] = (proposition.get("detail", "") + "; " + "同一资料日期和类别存在不同内容版本，相关结论暂缓确定性判断").strip("; ")
    if affected:
        gap = "同一资料日期和类别存在不同内容版本，相关证据已隔离，不能作确定判断"
        gated["coverage_gaps"] = list(dict.fromkeys(gated.get("coverage_gaps", []) + [gap]))
        gated["uncertain_evidence_refs"] = sorted(set(gated.get("uncertain_evidence_refs", [])) | conflicted)
        if "evidence_refs" in gated:
            gated["evidence_refs"] = sorted(set(gated["evidence_refs"]) - conflicted)
    if not findings and affected and gated.get("status") in {"no_pass", "pass"}:
        gated["status"] = "unverifiable"
        gated["description"] = "存在内容版本冲突，相关结论无法确定"
    return gated
