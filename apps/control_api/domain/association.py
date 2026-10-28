"""Conservative, deterministic cross-file fact association.

Association is deliberately evidence bounded.  It may attach an unscoped
execution fact to an event only when the face and the temporal partition prove
the relationship.  It never invents a time window or an event from an action.
"""

from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
import unicodedata


_EXECUTION_KINDS = {"action", "measurement"}
_REQUIREMENT_KINDS = {"threshold", "requirements", "requirement", "risk_assessment", "design"}


def _face(value):
    if value is None:
        return None
    return " ".join(unicodedata.normalize("NFKC", str(value)).split()) or None


def _time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _day(value):
    parsed = _time(value)
    return parsed.date() if isinstance(parsed, datetime) else parsed


def _cmp(a, b):
    """Return -1/0/1 or None for invalid/incomparable values."""
    a, b = _time(a), _time(b)
    if a is None or b is None:
        return None
    try:
        return (a > b) - (a < b)
    except TypeError:
        return None


def _refs(fact):
    """Stable, de-duplicated source references for public association links."""
    result = []
    for ref in fact.get("evidence_refs", []) or []:
        if ref not in result:
            result.append(ref)
    return result


def _stable_id(face, when, trigger):
    payload = json.dumps([face, when, trigger], ensure_ascii=False, separators=(",", ":"))
    return "internal-event-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _link(target, event, reason, *, partition=None, source_fact=None):
    source = source_fact or target
    entry = {
        "fact_id": target.get("fact_id"),
        "event_id": event.get("event_id"),
        "workface": event.get("workface"),
        "source_fact_id": source.get("fact_id"),
        "evidence_refs": list(dict.fromkeys(_refs(target) + _refs(event))),
        "reason": reason,
    }
    if partition:
        entry["temporal_partition"] = partition
        entry["policy"] = "same normalized workface and original local date; unique ordered event partition"
        entry["limitations"] = ["An omitted or late-received event may change this candidate association; rerun with the new snapshot.",
                                "Same-day association does not prove causation or permit cross-day borrowing."]
    return entry


def _validity(fact):
    fields = fact.get("fields") if isinstance(fact.get("fields"), dict) else {}
    start, end = fields.get("valid_from"), fields.get("valid_to")
    if not start or not end or _time(start) is None or _time(end) is None:
        return None
    if _day(start) > _day(end) or _cmp(start, end) == 1:
        return None
    return start, end


def _point_in(value, interval):
    if value is None or interval is None:
        return False
    start, end = interval
    point, lower, upper = _time(value), _time(start), _time(end)
    if point is None or lower is None or upper is None:
        return False
    # Date-only limits cover the whole named day.  A date-only point cannot
    # establish whether it occurred before/after a same-day datetime limit.
    if not isinstance(lower, datetime):
        begins = _day(value) >= lower
    elif not isinstance(point, datetime):
        begins = point > lower.date()
    else:
        comparison = _cmp(value, start)
        begins = comparison is not None and comparison >= 0
    if not isinstance(upper, datetime):
        ends = _day(value) <= upper
    elif not isinstance(point, datetime):
        ends = point < upper.date()
    else:
        comparison = _cmp(value, end)
        ends = comparison is not None and comparison <= 0
    return begins and ends


def associate_facts(facts, *, observation=None):
    """Associate same-run facts into conservatively identified event partitions.

    The returned facts are deep copies.  ``links`` are auditable relationships;
    ``gaps`` records every unresolved or ambiguous relationship so callers can
    gate a compliance result instead of silently dropping evidence.
    """
    if not isinstance(facts, list):
        raise TypeError("facts must be a list")
    copied = deepcopy(facts)
    links, gaps = [], []

    def gap(message):
        if message not in gaps:
            gaps.append(message)

    # Normalize only the matching key; preserve all meaningful workface text.
    for fact in copied:
        original_face = fact.get("workface")
        fact["workface"] = _face(original_face)
        if fact["workface"] != original_face:
            fact.setdefault("source_workface", original_face)
        fields = fact.get("fields", {})
        if fields.get("target_scope") and _face(fields["target_scope"]) == fact["workface"]:
            fields["target_scope"] = fact["workface"]

    observation_interval = None
    if observation is not None:
        if not isinstance(observation, dict) or _validity({"fields": {
                "valid_from": observation.get("start"), "valid_to": observation.get("end")}}) is None:
            gap("observation bounds are invalid")
        else:
            observation_interval = observation["start"], observation["end"]

    events = [f for f in copied if f.get("kind") == "event" and f.get("source_role") == "actual_record"]
    by_face = {}
    seen_event_ids = {}
    for event in events:
        face, when = event.get("workface"), event.get("time")
        if not face or not when or _time(when) is None or not _refs(event):
            gap(f"event cannot establish identity: {event.get('fact_id')}")
            continue
        source_id = event.get("event_id")
        if not source_id:
            trigger = (event.get("fields") or {}).get("triggerfact_id") or event.get("fact_id")
            source_id = None
            event["source_event_id"] = None
            event["event_id"] = _stable_id(face, when, trigger)
        else:
            event.setdefault("source_event_id", source_id)
        by_face.setdefault(face, []).append(event)
        seen_event_ids.setdefault(event["event_id"], []).append(event)
        links.append(_link(event, event, "source event_id preserved" if source_id else "stable internal id from workface/time/trigger fact"))
        if observation is not None and not _point_in(when, observation_interval):
            gap(f"event outside observation or observation invalid: {event.get('fact_id')}")

    # The same explicit id cannot identify two faces.  Keep facts unchanged and
    # expose the conflict for rule evaluation.
    for event_id, grouped in seen_event_ids.items():
        faces = {e.get("workface") for e in grouped}
        if len(faces) > 1:
            gap(f"event id spans multiple workfaces: {event_id}")

    # Attach execution facts to exactly one provable event partition.
    execution = [f for f in copied if f.get("kind") in _EXECUTION_KINDS and f.get("source_role") == "actual_record"]
    attached = set()
    for fact in execution:
        if not _refs(fact):
            gap(f"uncited execution fact: {fact.get('fact_id')}")
            continue
        if observation is not None and not _point_in(fact.get("time"), observation_interval):
            gap(f"fact outside observation or observation invalid: {fact.get('fact_id')}")
            if not fact.get("event_id"):
                continue
        if fact.get("event_id"):
            candidates = [e for e in events if e.get("event_id") == fact.get("event_id")]
            if not candidates:
                gap(f"event id has no event fact: {fact.get('fact_id')}")
            elif len({e.get("workface") for e in candidates}) > 1:
                gap(f"cross-workface event reference: {fact.get('fact_id')}")
            else:
                event = candidates[0]
                if not fact.get("workface") or _time(fact.get("time")) is None:
                    gap(f"explicit event reference lacks workface or valid time: {fact.get('fact_id')}")
                elif fact.get("workface") != event.get("workface"):
                    gap(f"workface conflicts with event id: {fact.get('fact_id')}")
                elif (_cmp(fact.get("time"), event.get("time")) == -1
                      or (_day(fact.get("time")) is not None and _day(event.get("time")) is not None
                          and _day(fact.get("time")) < _day(event.get("time")))):
                    gap(f"fact precedes event trigger: {fact.get('fact_id')}")
                else:
                    attached.add(fact.get("fact_id"))
                    links.append(_link(fact, event, "explicit event_id preserved"))
            continue

        face, when = fact.get("workface"), fact.get("time")
        # An explicit original-record reference is stronger than proximity. It
        # can span dates, but still needs a unique event, face and precise time.
        referenced_time = fact.get("fields", {}).get("event_time_reference")
        if referenced_time:
            referenced = [e for e in by_face.get(face, [])
                          if isinstance(_time(referenced_time), datetime)
                          and _cmp(referenced_time, e.get("time")) == 0]
            valid = (len(referenced) == 1 and _cmp(when, referenced[0].get("time")) in {0, 1})
            if valid:
                event = referenced[0]
                fact["event_id"] = event["event_id"]
                fact.setdefault("source_event_id", None)
                attached.add(fact["fact_id"])
                link = _link(fact, event, "explicit original event-time reference and workface")
                link["association_state"] = "verified_reference"
                link["event_time_reference"] = referenced_time
                links.append(link)
            else:
                gap(f"event-time reference is invalid or ambiguous: {fact.get('fact_id')}")
            continue
        candidates = []
        if face and when and _time(when) is not None:
            day = _day(when)
            same_day = [e for e in by_face.get(face, []) if _day(e.get("time")) == day]
            ordered = (isinstance(_time(when), datetime)
                       and all(isinstance(_time(e.get("time")), datetime) for e in same_day)
                       and all(_cmp(a.get("time"), b.get("time")) in {-1, 1}
                               for i, a in enumerate(same_day) for b in same_day[i + 1:]))
            if ordered:
                same_day.sort(key=lambda e: _time(e["time"]))
            for index, event in enumerate(same_day if ordered else []):
                next_event = same_day[index + 1] if index + 1 < len(same_day) else None
                # Boundaries require precise times.  Date-only rows cannot prove
                # ordering, and an event's remaining month is never its interval.
                after = _cmp(when, event.get("time"))
                before_next = _cmp(when, next_event.get("time")) if next_event else 0
                if after is not None and after >= 0 and (next_event is None or (before_next is not None and before_next < 0)):
                    if observation is None or _point_in(event.get("time"), observation_interval):
                        candidates.append(event)
        if len(candidates) == 1:
            event = candidates[0]
            fact["event_id"] = event.get("event_id")
            fact.setdefault("source_event_id", None)
            attached.add(fact.get("fact_id"))
            links.append(_link(fact, event, "same workface/date and unique temporal partition", partition="[event_time,next_event_time)"))
        elif not candidates:
            gap(f"unassociated execution fact: {fact.get('fact_id')}")
        else:
            gap(f"ambiguous execution event candidates: {fact.get('fact_id')}")

    # Explicitly scoped facts are still checked for a missing event, while facts
    # of unknown kinds remain visible and auditable.
    for fact in copied:
        if fact.get("kind") in _EXECUTION_KINDS and fact.get("fact_id") not in attached and fact.get("event_id"):
            gap(f"unresolved execution fact: {fact.get('fact_id')}")

    # Requirements and thresholds with a valid range may be reused for every
    # matching event.  Each derived copy is independently traceable.
    derived, consumed = [], set()
    targets = [f for f in copied if f.get("kind") == "measurement" and f.get("fact_id") in attached]
    for requirement in copied:
        if requirement.get("kind") not in _REQUIREMENT_KINDS or requirement.get("source_role") != "requirement" or requirement.get("event_id"):
            continue
        interval = _validity(requirement)
        if interval is None:
            gap(f"requirement has no explicit valid range: {requirement.get('fact_id')}")
            continue
        if not requirement.get("workface") or not _refs(requirement):
            gap(f"requirement lacks workface or evidence: {requirement.get('fact_id')}")
            continue
        matched_events = []
        seen = set()
        for event in events:
            if (event.get("workface") != requirement.get("workface") or not event.get("event_id")
                    or event.get("event_id") in seen):
                continue
            points = [event] + [f for f in targets if f.get("event_id") == event.get("event_id")
                                and f.get("workface") == event.get("workface")]
            applicability_points = [point for point in points if _point_in(point.get("time"), interval)]
            if applicability_points:
                matched_events.append((event, applicability_points))
                seen.add(event["event_id"])
        if not matched_events:
            gap(f"requirement not applicable in its valid range: {requirement.get('fact_id')}")
            continue
        consumed.add(requirement.get("fact_id"))
        for event, applicability_points in matched_events:
            suffix = hashlib.sha256(f"{requirement.get('fact_id')}:{event.get('event_id')}".encode()).hexdigest()[:16]
            copy = deepcopy(requirement)
            copy["fact_id"] = f"{requirement.get('fact_id')}:derived:{suffix}"
            copy["source_fact_id"] = requirement.get("fact_id")
            copy["event_id"] = event.get("event_id")
            copy["source_event_id"] = None
            derived.append(copy)
            link = _link(copy, event, "explicit validity range covers event or its measurement", source_fact=requirement)
            link["validity"] = {"valid_from": interval[0], "valid_to": interval[1]}
            link["applicability_points"] = [{"fact_id": p.get("fact_id"), "time": p.get("time"),
                                             "evidence_refs": _refs(p)} for p in applicability_points]
            link["evidence_refs"] = list(dict.fromkeys(link["evidence_refs"] + [
                ref for point in applicability_points for ref in _refs(point)]))
            links.append(link)

    for fact in copied:
        if (not fact.get("event_id") and fact.get("fact_id") not in consumed
                and not (fact.get("kind") in _EXECUTION_KINDS and fact.get("source_role") == "actual_record")
                and not (fact.get("kind") in _REQUIREMENT_KINDS and fact.get("source_role") == "requirement")
                and not (fact.get("kind") == "event" and fact.get("source_role") == "actual_record")):
            gap(f"unassociated fact of unsupported kind: {fact.get('fact_id')}")
    copied = [f for f in copied if f.get("fact_id") not in consumed] + derived
    return {"facts": copied, "links": links, "gaps": gaps}
