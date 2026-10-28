"""Small, trusted deterministic operator graph for future inspection items.

Operators only consume host-published verified facts. Model candidates are
kept out of this path even when they contain plausible fields.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Callable


def _time(value):
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    value = value.strip().replace("Z", "+00:00")
    if "T" not in value and " " not in value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _facts(snapshot: dict, run: dict) -> tuple[list[dict], set[str]]:
    evidence = {item.get("evidence_id"): item for item in snapshot.get("evidence", []) if item.get("evidence_id")}
    conflicted_files = {file.get("file_id") for file in snapshot.get("files", []) if file.get("conflicted") is True}
    unusable_units = {(unit.get("file_id"), unit.get("unit_id")) for unit in snapshot.get("coverage", [])
                      if unit.get("state") in {"conflicted", "unreadable", "provider_failed"}}

    def usable(ref, role):
        source = evidence[ref]
        unit_id = source.get("unit_id")
        if not unit_id and source.get("locator", {}).get("page"):
            unit_id = "page:" + str(source["locator"]["page"])
        run_id = run.get("run_id", run.get("runId"))
        mine_id = run.get("mine_id", run.get("mineId", run.get("request", {}).get("mineId")))
        return (source.get("source_role") == role and source.get("quality") != "model_candidate"
                and source.get("file_id") not in conflicted_files
                and source.get("state") not in {"conflicted", "unreadable"}
                and source.get("run_id") in {None, run_id}
                and source.get("mine_id") in {None, mine_id}
                and (source.get("file_id"), unit_id) not in unusable_units)
    trusted = []
    for fact in snapshot.get("verified_facts", snapshot.get("facts", [])):
        if (not isinstance(fact, dict) or fact.get("validation_status") in {"candidate", "source_bound"}
                or fact.get("status") == "candidate" or fact.get("quality") == "model_candidate"):
            continue
        role = fact.get("source_role") or fact.get("fields", {}).get("source_role")
        refs = fact.get("evidence_refs", [])
        if not isinstance(refs, list) or any(not isinstance(ref, (str, dict)) for ref in refs):
            continue
        refs = [ref if isinstance(ref, str) else ref.get("evidence_id") for ref in refs]
        if (role not in {"actual_record", "requirement"} or not refs or not set(refs) <= set(evidence)
                or fact.get("verified") is False
                or any(not usable(ref, role) for ref in refs)):
            continue
        copied = dict(fact)
        copied["_refs"] = sorted(set(refs))
        trusted.append(copied)
    return trusted, set(evidence)


def _value(fact: dict, field: str):
    return (fact.get("fields") or {}).get(field, fact.get(field))


def _select(facts: list[dict], selector: dict | None) -> list[dict]:
    selector = selector or {}
    if not isinstance(selector, dict):
        return []
    result = []
    for fact in facts:
        if selector.get("kind") and fact.get("kind") != selector["kind"]:
            continue
        if selector.get("source_role") and fact.get("source_role") != selector["source_role"]:
            continue
        fields = fact.get("fields") or {}
        where = selector.get("where", {})
        if not isinstance(where, dict):
            raise ValueError("fact selector where must be an object")
        if any((fields.get(key, fact.get(key)) != value) for key, value in where.items()):
            continue
        result.append(fact)
    return result


class GraphEvaluator:
    def __init__(self, operators: dict[str, Callable] | None = None):
        self._operators = MappingProxyType({**self._builtins(), **(operators or {})})
        if any(not isinstance(name, str) or not callable(operator) for name, operator in self.operators.items()):
            raise ValueError("graph operators must be named trusted callables")

    @property
    def operators(self):
        return self._operators

    @property
    def operator_names(self):
        return tuple(sorted(self.operators))

    @staticmethod
    def _builtins():
        return {
            "field_present": lambda ev, node, facts: ev._field_present(node, facts),
            "field_equals": lambda ev, node, facts: ev._field_equals(node, facts),
            "compare_number": lambda ev, node, facts: ev._compare_number(node, facts),
            "before_time": lambda ev, node, facts: ev._before_time(node, facts),
            "all_of": lambda ev, node, facts: ev._all_of(node, facts),
        }

    def _result(self, state, refs=(), detail=""):
        return {"state": state, "evidence_refs": sorted(set(refs)), "detail": detail}

    def _single(self, selector, facts):
        rows = _select(facts, selector)
        if len(rows) != 1:
            return None, rows
        return rows[0], rows

    @staticmethod
    def _same_context(refs, facts):
        selected = [fact for fact in facts if set(fact["_refs"]) & set(refs)]
        for field in ("run_id", "mine_id", "workface", "event_id"):
            values = [_value(fact, field) for fact in selected if _value(fact, field) is not None]
            if any(not isinstance(value, (str, int, float)) for value in values) or len(set(values)) > 1:
                return False
        return True

    def _field_present(self, node, facts):
        config = node.get("config", {})
        fact, rows = self._single(config.get("source"), facts)
        if fact is None:
            return self._result("missing", [ref for row in rows for ref in row["_refs"]], "source is missing or ambiguous")
        field = config.get("field", (config.get("source") or {}).get("field"))
        value = _value(fact, field)
        return self._result("true" if value not in (None, "") else "missing", fact["_refs"], "field is present" if value not in (None, "") else "field is empty")

    def _field_equals(self, node, facts):
        config = node.get("config", {})
        fact, rows = self._single(config.get("source"), facts)
        if fact is None:
            return self._result("missing", [ref for row in rows for ref in row["_refs"]], "source is missing or ambiguous")
        value = _value(fact, config.get("field", (config.get("source") or {}).get("field")))
        if value is None or "value" not in config:
            return self._result("missing", fact["_refs"], "field is missing")
        expected = config["value"]
        if isinstance(value, bool) != isinstance(expected, bool):
            return self._result("missing", fact["_refs"], "boolean and non-boolean values cannot be compared")
        if isinstance(value, (dict, list)) or isinstance(expected, (dict, list)):
            return self._result("missing", fact["_refs"], "field equality requires scalar values")
        if type(value) is not type(expected):
            return self._result("missing", fact["_refs"], "field equality types differ")
        return self._result("true" if value == expected else "false", fact["_refs"], "field equality")

    def _operand(self, operand, facts):
        if isinstance(operand, dict) and ("selector" in operand or "kind" in operand):
            selector = operand.get("selector", operand)
            fact, rows = self._single(selector, facts)
            if fact is None:
                return None, [ref for row in rows for ref in row["_refs"]], None
            fields = fact.get("fields") or {}
            unit = fields.get("unit", fact.get("unit"))
            expected_unit = operand.get("unit", selector.get("unit"))
            if expected_unit is not None and unit != expected_unit:
                return None, fact["_refs"], unit
            return _value(fact, operand.get("field", selector.get("field"))), fact["_refs"], unit
        if isinstance(operand, dict):
            return operand.get("value"), [], operand.get("unit")
        return operand, [], None

    def _compare_number(self, node, facts):
        config = node.get("config", {})
        left, left_refs, left_unit = self._operand(config.get("left"), facts)
        right, right_refs, right_unit = self._operand(config.get("right"), facts)
        a, b = _number(left), _number(right)
        refs = left_refs + right_refs
        if a is None or b is None or left_unit != right_unit or not refs or not self._same_context(refs, facts):
            return self._result("missing", refs, "numeric value or compatible unit is missing")
        operator = config.get("comparison", config.get("operator"))
        states = {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b, "=": a == b, "==": a == b}
        if operator not in states:
            raise ValueError("unknown numeric comparison")
        return self._result("true" if states[operator] else "false", refs, "numeric comparison")

    def _before_time(self, node, facts):
        config = node.get("config", {})
        first, first_refs, _ = self._operand(config.get("first"), facts)
        second, second_refs, _ = self._operand(config.get("second", config.get("last")), facts)
        a, b = _time(first), _time(second)
        refs = first_refs + second_refs
        if (a is None or b is None or not refs or not self._same_context(refs, facts)
                or (a.utcoffset() is None) != (b.utcoffset() is None)):
            return self._result("missing", refs, "time precision is missing")
        allowed_equal = config.get("allow_equal", False)
        if type(allowed_equal) is not bool:
            raise ValueError("allow_equal must be boolean")
        return self._result("true" if (a <= b if allowed_equal else a < b) else "false", refs, "time ordering")

    def _all_of(self, node, facts):
        states = [node["_states"].get(dep) for dep in node.get("dependsOn", [])]
        refs = [ref for dep in node.get("dependsOn", []) for ref in node["_refs"].get(dep, [])]
        if not states:
            return self._result("missing", refs, "all_of needs dependencies")
        if any(state == "false" for state in states):
            return self._result("false", refs, "a required proposition is false")
        if any(state != "true" for state in states):
            return self._result("missing", refs, "a required proposition is unresolved")
        return self._result("true", refs, "all dependencies are true")

    def __call__(self, run: dict, snapshot: dict) -> dict:
        expected_mine = run.get("mine_id", run.get("mineId", run.get("request", {}).get("mineId")))
        if (snapshot.get("run_id") not in {None, run.get("run_id", run.get("runId"))}
                or snapshot.get("mine_id") not in {None, expected_mine}):
            raise ValueError("operator snapshot owner mismatch")
        plan = run.get("plan") or {}
        execution = plan.get("executionPlan", {})
        nodes = execution.get("propositions", [])
        if not isinstance(nodes, list):
            raise ValueError("graph propositions must be a list")
        by_code = {}
        for node in nodes:
            if not isinstance(node, dict) or not isinstance(node.get("code"), str) or node["code"] in by_code:
                raise ValueError("graph requires unique proposition codes")
            by_code[node["code"]] = node
        ordered, active, done = [], set(), set()
        def visit(code):
            if code not in by_code or code in active:
                raise ValueError("unknown or cyclic proposition dependency")
            if code in done:
                return
            active.add(code)
            for dependency in by_code[code].get("dependsOn", []):
                visit(dependency)
            active.remove(code)
            done.add(code)
            ordered.append(by_code[code])
        for code in sorted(by_code):
            visit(code)
        facts, registered = _facts(snapshot, run)
        states, refs, propositions = {}, {}, []
        for node in ordered:
            code = node.get("code")
            operator = node.get("operator") or ("all_of" if node.get("dependsOn") else None)
            if not code or not operator:
                raise ValueError("graph proposition requires code and operator")
            if operator not in self.operators:
                raise ValueError(f"unknown graph operator: {operator}")
            working = {**node, "_states": states, "_refs": refs}
            result = self.operators[operator](self, working, facts)
            state = result["state"]
            if state not in {"true", "false", "missing", "unreadable", "conflicted"}:
                raise ValueError("operator returned invalid proposition state")
            if state in {"true", "false"} and not result["evidence_refs"]:
                result = self._result("missing", (), "operator result has no registered source")
                state = "missing"
            states[code], refs[code] = state, result["evidence_refs"]
            propositions.append({"code": code, "state": state, "evidence_refs": result["evidence_refs"], "detail": result["detail"]})
        invalid_refs = any(ref not in registered for ref_list in refs.values() for ref in ref_list)
        if invalid_refs:
            raise ValueError("graph result contains unregistered evidence")
        gaps = list(snapshot.get("coverage_gaps", []))
        gaps.extend("conflicted:" + str(file.get("file_id")) for file in snapshot.get("files", []) if file.get("conflicted") is True)
        gaps.extend(str(unit["state"]) + ":" + str(unit.get("file_id")) + "/" + str(unit.get("unit_id"))
                    for unit in snapshot.get("coverage", []) if unit.get("state") in {"conflicted", "unreadable", "provider_failed", "not_indexed"})
        gaps = list(dict.fromkeys(gaps))
        coverage_complete = snapshot.get("coverage_complete") is True or (snapshot.get("coverage_audit") or {}).get("coverage_complete") is True
        source_complete = snapshot.get("source_complete") is True
        false_props = [p for p in propositions if p["state"] == "false"]
        unresolved = [p for p in propositions if p["state"] != "true"]
        gaps.extend(f"{p['state']}:{p['code']}:{p['detail']}" for p in unresolved if p["state"] != "false")
        if not source_complete:
            gaps.append("source_scope_incomplete")
        if not coverage_complete:
            gaps.append("processing_coverage_incomplete")
        if false_props:
            status = "no_pass"
            findings = [{**p, "status": "no_pass", "major_hazard_classification": "not_assessed"} for p in false_props]
            description = "可信事实证明图中条件不满足"
        elif any("unreadable" in str(g).lower() or "conflict" in str(g).lower() for g in gaps):
            status, findings, description = "unverifiable", [], "可信证据不可读或存在冲突"
        elif unresolved or not propositions or not source_complete or not coverage_complete or gaps:
            status, findings, description = "pending", [], "图条件或覆盖范围尚未闭合"
        else:
            status, findings, description = "pass", [], "图中所有条件均有可信证据"
        return {"run_id": run.get("run_id", run.get("runId")), "item_code": run.get("item_code", run.get("itemCode")),
                "status": status, "description": description, "detail": "; ".join(gaps),
                "propositions": propositions, "findings": findings,
                "evidence_refs": sorted({ref for refs_list in refs.values() for ref in refs_list}),
                "coverage_gaps": gaps}
