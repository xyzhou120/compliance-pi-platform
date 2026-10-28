"""Evidence-bound checks for the remaining workbook conditions.

These checks add no human verdict workflow. Missing facts remain gaps; only
explicit, applicable execution evidence can establish a failed condition.
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .contracts import InvalidRequest, assert_snapshot_owner


_CAPABILITIES = {
    "stop_work_unload": {
        "risk_and_design_scope": ("D2", "F2"),
        "relief_parameters_acceptance": ("E2", "F2"),
        "personnel_and_equipment": ("E2",),
        "signatures_and_notification": ("E2",),
    },
    "relief_effects": {
        "risk_and_design_scope": ("D3", "F3", "G3"),
        "signature_evidence": ("E3", "F3"),
    },
}


def _time(value):
    try:
        if not isinstance(value, str):
            return None
        return date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _as_datetime(value, end_of_day=False):
    """Normalize a date-only bound so it covers the whole named local day."""
    moment = _time(value)
    if moment is None:
        return None
    if isinstance(moment, datetime):
        return moment
    return datetime.combine(moment, datetime.max.time() if end_of_day else datetime.min.time())


def _inside_window(value, start, end):
    """True/False for the declared observation window; None when undecidable."""
    moment, begin, finish = _as_datetime(value), _as_datetime(start), _as_datetime(end, end_of_day=True)
    if moment is None or begin is None or finish is None:
        return None
    return begin <= moment <= finish


def _number(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _integer(value):
    number = _number(value)
    return number if number is not None and number >= 0 and number == number.to_integral_value() else None


def _in_validity(fields, when):
    instant = _time(when)
    start, end = _time(fields.get("valid_from")), _time(fields.get("valid_to"))
    if None in (instant, start, end):
        return False
    day = instant.date() if isinstance(instant, datetime) else instant
    try:
        lower = start <= instant if isinstance(start, datetime) and isinstance(instant, datetime) else start <= day if not isinstance(start, datetime) else False
        upper = instant <= end if isinstance(end, datetime) and isinstance(instant, datetime) else day <= end if not isinstance(end, datetime) else False
        return lower and upper
    except TypeError:
        return False


def _ordered(first, second):
    first, second = _time(first), _time(second)
    if first is None or second is None:
        return None
    try:
        if isinstance(first, datetime) and isinstance(second, datetime):
            return first <= second
        # Equal dates cannot prove the order of signing/notification.
        a = first.date() if isinstance(first, datetime) else first
        b = second.date() if isinstance(second, datetime) else second
        return True if a < b else False if a > b else None
    except TypeError:
        return None


_UNITS = {
    "m": ("length", "1"), "cm": ("length", ".01"), "mm": ("length", ".001"),
    "mpa": ("pressure", "1"), "kpa": ("pressure", ".001"), "pa": ("pressure", ".000001"),
    "j": ("energy", "1"), "kj": ("energy", "1000"),
    "1": ("scalar", "1"), "个": ("count", "1"), "孔": ("count", "1"),
}


def _parameter_matches(actual, requirement):
    av, rv = _number(actual.get("value")), _number(requirement.get("value"))
    au, ru = str(actual.get("unit", "")).strip().lower(), str(requirement.get("unit", "")).strip().lower()
    if av is None or rv is None or not au or not ru:
        return None
    if au != ru:
        if au not in _UNITS or ru not in _UNITS or _UNITS[au][0] != _UNITS[ru][0]:
            return None
        av, rv = av * Decimal(_UNITS[au][1]), rv * Decimal(_UNITS[ru][1])
    operator = requirement.get("operator")
    if operator in ("=", "=="):
        return av == rv
    if operator == ">=":
        return av >= rv
    if operator == "<=":
        return av <= rv
    if operator == ">":
        return av > rv
    if operator == "<":
        return av < rv
    return None


class _Scope:
    def __init__(self, run, snapshot):
        assert_snapshot_owner(snapshot, run)
        self.item = (run["spec"].get("inspection_item") or {}).get("handler", run["spec"]["item_code"])
        if self.item not in _CAPABILITIES:
            raise InvalidRequest("unsupported scope check")
        self.snapshot = snapshot
        self.facts = snapshot.get("facts", [])
        self.propositions, self.findings, self.gaps = [], [], []

    def emit(self, event, code, assertions, facts):
        states = [state for state, _ in assertions]
        state = "false" if False in states else "missing" if None in states or not states else "true"
        detail = "; ".join(message for value, message in assertions if value is not True) or "全部明确证据满足该项条件"
        baseline = self.snapshot.get("rule_baseline") or {}
        source = baseline.get("source") or {}
        prop = {"event_id": event["event_id"], "workface": event["workface"],
                "code": code, "state": state, "evidence_refs": sorted({r for f in facts if f for r in f["evidence_refs"]}),
                "detail": detail, "law_refs": [],
                "rule_refs": [{"version": baseline.get("version", "impact-1023-draft-v1"),
                               "revision": baseline.get("revision"), "source": source,
                               "source_cells": [source.get("sheet", "AI深度监察") + "!" + c for c in _CAPABILITIES[self.item][code]],
                               "status": "development-draft"}]}
        self.propositions.append(prop)
        if state == "false":
            self.findings.append({**prop, "status": "no_pass", "major_hazard_classification": "not_assessed"})
        if state != "true":
            self.gaps.extend(f"{event['event_id']}/{event['workface']}: {code}: {message}"
                             for value, message in assertions if value is None)

    def requirement(self, fs, event, kind):
        # Wrong-face and expired requirements are rejected, never borrowed.
        candidates = [f for f in fs if f.get("kind") == kind and f.get("source_role") == "requirement"
                      and f["fields"].get("approved") is True
                      and f["fields"].get("target_scope") == event["workface"]
                      and _in_validity(f["fields"], event.get("time"))]
        return candidates[0] if len(candidates) == 1 else None

    def action(self, fs, event, action):
        candidates = [f for f in fs if f.get("kind") == "action" and f.get("source_role") == "actual_record"
                      and f["fields"].get("action") == action
                      and (action == "dispatch_release" or f["fields"].get("action_layer", "execution") != "instruction")
                      and f["fields"].get("target_scope") == event["workface"]
                      and f["fields"].get("actor_role")
                      and f["fields"].get("status") == "done"
                      and _ordered(event.get("time"), f.get("time")) is True]
        return candidates[0] if len(candidates) == 1 else None

    def check_event(self, event, fs):
        risk, design = (self.requirement(fs, event, kind) for kind in ("risk_assessment", "design"))
        rf, df = (f["fields"] if f else {} for f in (risk, design))
        assertions = []
        for name, record, fields in (("危险性评价", risk, rf), ("防冲设计", design, df)):
            assertions.append((True if record else None, name + "缺少唯一、同工作面且有效期覆盖事件的原始要求"))
            assertions.append((fields.get("approved") if isinstance(fields.get("approved"), bool) else None,
                               name + "批准事实缺失或明确未批准"))
        assertions.append((True if isinstance(rf.get("impact_risk"), bool) and rf.get("risk_grade") else None,
                           "危险性评价缺少冲击危险结论或危险等级"))
        self.emit(event, "risk_and_design_scope", assertions, [event, risk, design])
        if self.item == "relief_effects":
            technical = self.action(fs, event, "technical_confirm")
            dispatch = self.action(fs, event, "dispatch_release")
            resume = self.action(fs, event, "resume") or self.action(fs, event, "continue_work")
            tf = technical["fields"] if technical else {}
            assertions = [(True if technical else None, "缺少唯一的实际技术确认记录"),
                          (tf.get("signature_present") if isinstance(tf.get("signature_present"), bool) else None, "技术确认签名存在事实缺失或明确无签名"),
                          (True if tf.get("signer") and df.get("technical_head") else None, "签字人或适用设计技术负责人缺失"),
                          ((tf["signer"] == df["technical_head"]) if tf.get("signer") and df.get("technical_head") else None, "签字人与适用设计技术负责人不一致"),
                          (_ordered(technical.get("time"), tf.get("signed_at")) if technical else None, "签署时间缺失、精度不足或早于确认记录"),
                          (True if technical and _in_validity(df, technical.get("time")) else None, "设计有效期未覆盖技术确认时间")]
            if dispatch:
                assertions.append((_ordered(tf.get("signed_at"), dispatch.get("time")),
                                   "技术确认实际签署时间缺失、精度不足或晚于调度解除"))
            if resume:
                assertions.append((_ordered(tf.get("signed_at"), resume.get("time")),
                                   "技术确认实际签署时间缺失、精度不足或晚于实际复工"))
            released = technical and any(_ordered(technical.get("time"), record.get("time")) is True
                                         for record in (dispatch, resume) if record)
            if not released:
                # Work may legitimately remain closed while confirmation is
                # being prepared. A signature deficiency alone is not an
                # evidenced premature release or resumption.
                assertions = [(None if value is False else value, message) for value, message in assertions]
                assertions.append((None, "尚无时序可比的调度解除或实际复工，签署问题暂不能构成违规结论"))
            self.emit(event, "signature_evidence", assertions, [event, design, technical, dispatch, resume])
            return
        analysis = self.action(fs, event, "hazard_analysis")
        af = analysis["fields"] if analysis else {}
        self.emit(event, "signatures_and_notification", [
            (True if analysis else None, "缺少唯一的实际危险研判记录"),
            (af.get("signature_present") if isinstance(af.get("signature_present"), bool) else None, "研判签名存在事实缺失或明确无签名"),
            (True if af.get("signer") else None, "研判签字人缺失"),
            (af.get("notified") if isinstance(af.get("notified"), bool) else None, "告知事实缺失或明确未告知"),
            (_ordered(analysis.get("time"), af.get("notification_time")) if analysis else None, "告知时间缺失、精度不足或早于研判")], [event, analysis])
        # A documented negative assessment removes the relief obligation, but
        # never removes the immediate evacuation obligation.
        negative = analysis and af.get("hazard_confirmed") is False and event["fields"].get("hazard_confirmed") is not True
        relief = self.action(fs, event, "relief")
        lf = relief["fields"] if relief else {}
        assertions = []
        if negative:
            self.emit(event, "relief_parameters_acceptance", [(True, "实际研判明确无危险，无本次卸压义务")], [event, analysis])
        else:
            assertions.extend([(True if relief else None, "缺少唯一实际卸压完成记录"),
                               (True if design and relief and _in_validity(df, relief.get("time")) else None, "缺少施工时有效的适用设计"),
                               (True if isinstance(df.get("relief_methods"), list) and df["relief_methods"] else None, "设计缺少解危方法全集"),
                               ((lf["method"] in df["relief_methods"]) if lf.get("method") and isinstance(df.get("relief_methods"), list) and df["relief_methods"] else None, "施工方法缺失或不符合设计"),
                               ((lf["relief_area"] == df["relief_area"]) if lf.get("relief_area") and df.get("relief_area") else None, "实际卸压区域或设计区域缺失、不一致"),
                               (lf.get("acceptance_passed") if isinstance(lf.get("acceptance_passed"), bool) else None, "验收结论缺失或明确未通过"),
                               (True if lf.get("acceptance_signer") else None, "验收人缺失"),
                               (_ordered(relief.get("time"), lf.get("acceptance_time")) if relief else None, "验收时间缺失、精度不足或早于施工完成")])
            requirements, parameters = df.get("relief_parameters"), lf.get("parameters")
            assertions.append((True if isinstance(requirements, list) and requirements and all(isinstance(p, dict) and p.get("name") for p in requirements) else None, "设计参数要求缺失或格式无效"))
            if isinstance(requirements, list):
                for requirement in requirements:
                    if not isinstance(requirement, dict) or not requirement.get("name"):
                        continue
                    found = [p for p in parameters if isinstance(p, dict) and p.get("name") == requirement["name"]] if isinstance(parameters, list) else []
                    assertions.append((_parameter_matches(found[0], requirement) if len(found) == 1 else None,
                                       "参数" + str(requirement["name"]) + "缺失、重复、不可比较或不符合设计"))
            self.emit(event, "relief_parameters_acceptance", assertions, [event, design, relief])
        evacuation = self.action(fs, event, "evacuate")
        ef = evacuation["fields"] if evacuation else {}
        expected, withdrawn = _integer(ef.get("expected_personnel")), _integer(ef.get("withdrawn_personnel"))
        assertions = [(True if evacuation else None, "缺少唯一实际撤人记录"),
                      ((ef["withdrawal_area"] == df["withdrawal_area"]) if ef.get("withdrawal_area") and df.get("withdrawal_area") else None, "撤离区域或设计要求缺失、不一致"),
                      ((withdrawn == expected) if expected is not None and withdrawn is not None else None, "应撤人数与实撤人数缺失或不一致"),
                      (ef.get("route_conforms") if isinstance(ef.get("route_conforms"), bool) else None, "撤离路线符合性缺失或明确不符合")]
        if not negative:
            maximum, count = _integer(df.get("max_personnel")), _integer(lf.get("personnel_count"))
            assertions.extend([((count <= maximum) if count is not None and maximum is not None else None, "解危实际人数或限员缺失、人数超限"),
                               (True if isinstance(df.get("unrelated_equipment_stop_required"), bool) else None, "设计缺少无关设备停运要求"),
                               (True if df.get("unrelated_equipment_stop_required") is False else lf.get("unrelated_equipment_stopped") if df.get("unrelated_equipment_stop_required") is True and isinstance(lf.get("unrelated_equipment_stopped"), bool) else None, "无关设备停运事实缺失或明确未停运")])
        self.emit(event, "personnel_and_equipment", assertions, [event, design, evacuation, relief, analysis])

    def evaluate(self):
        events = [f for f in self.facts if f.get("kind") == "event" and f.get("source_role") == "actual_record"]
        keys = {(f.get("event_id"), f.get("workface")) for f in events if f.get("event_id") and f.get("workface")}
        for event_id, workface in sorted(keys):
            matching = [f for f in events if (f.get("event_id"), f.get("workface")) == (event_id, workface)]
            if len(matching) != 1:
                self.gaps.append(f"{event_id}/{workface}: duplicate event identity prevents scope evaluation")
                continue
            event = matching[0]
            fs = [f for f in self.facts if (f.get("event_id"), f.get("workface")) == (event_id, workface)]
            observation = self.snapshot.get("observation") or {}
            start, end = observation.get("start"), observation.get("end")
            # Evidence outside the declared observation window can neither
            # support nor refute anything: an older report re-attached to this
            # cycle must never become a deterministic finding (INV-2).
            outside = [f for f in fs if f.get("kind") in {"event", "action", "measurement"}
                       and _inside_window(f.get("time"), start, end) is not True]
            if outside:
                self.propositions.append({
                    "event_id": event.get("event_id"), "workface": event.get("workface"),
                    "code": "observation_window", "state": "missing",
                    "evidence_refs": sorted({ref for f in outside for ref in f.get("evidence_refs", [])}),
                    "detail": "事件或执行证据不在本次观测窗口内，本轮不形成确定性结论",
                    "law_refs": [], "rule_refs": []})
                self.gaps.append(f"{event_id}/{workface}: {len(outside)} 条证据不在观测窗口 {start}~{end} 内，未参与判定")
                continue
            self.check_event(event, fs)
        if not keys:
            self.gaps.append("no scoped actual event for full-rule conditions")
        return {"propositions": self.propositions, "findings": self.findings,
                "gaps": list(dict.fromkeys(self.gaps)), "implemented_ids": list(_CAPABILITIES[self.item])}


def evaluate_scope(run, snapshot):
    """Evaluate registered facts without mutating the immutable input snapshot."""
    if ((run.get("spec", {}).get("inspection_item") or {}).get("handler", run.get("spec", {}).get("item_code")) == "stop_work_unload"
            and (snapshot.get("rule_baseline") or {}).get("version") == "stop-work-unload-scope-v3"):
        return {"propositions": [], "findings": [], "gaps": [], "implemented_ids": []}
    return _Scope(run, snapshot).evaluate()
