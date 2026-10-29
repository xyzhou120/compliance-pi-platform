from __future__ import annotations

import json
import re
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any


_TOOLS = {
    "search_evidence", "get_evidence", "open_page", "get_inspection_plan", "get_coverage",
    "parse_file", "inspect_documents", "recognize_pages", "expand_event_context", "compare_versions",
    "submit_candidates", "submit_gaps",
}
_BUDGETS = {"maxToolCalls": 10000, "maxModelTurns": 1000, "maxPages": 10000,
            "maxInputTokens": 10000000, "maxOutputTokens": 10000000,
            "deadlineMs": 86400000, "maxModelRetries": 5}
_BUDGET_ALIASES = {"max_tool_calls": "maxToolCalls", "max_model_turns": "maxModelTurns",
                   "max_pages": "maxPages", "max_input_tokens": "maxInputTokens",
                   "max_output_tokens": "maxOutputTokens", "deadline_ms": "deadlineMs",
                   "maxTokens": "maxInputTokens", "max_tokens": "maxInputTokens",
                   "maxRetries": "maxModelRetries", "max_retries": "maxModelRetries",
                   "max_model_retries": "maxModelRetries", "model_max_retries": "maxModelRetries"}
_FIELD_TYPES = {"string", "number", "integer", "boolean", "date", "datetime", "enum", "array", "object"}


def _names(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} must be a list of non-empty names")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} contains duplicate names")
    return value


def _conditional_materials(plan: dict, proposition_codes: set[str], material_groups: set[str]) -> list[dict]:
    conditions = plan.get("conditionalMaterials", [])
    if not isinstance(conditions, list):
        raise ValueError("conditionalMaterials must be a list")
    normalized, seen = [], set()
    for condition in conditions:
        if not isinstance(condition, dict) or not isinstance(condition.get("when"), dict):
            raise ValueError("conditional material needs when")
        when = condition["when"]
        if set(when) != {"proposition", "state"}:
            raise ValueError("material condition needs proposition and state")
        if not isinstance(when["proposition"], str) or when["proposition"] not in proposition_codes:
            raise ValueError("material condition references unknown proposition")
        if not isinstance(when["state"], str) or when["state"] not in {"true", "false"}:
            raise ValueError("material condition state must be true or false")
        groups = _names(condition.get("materialGroups"), "conditional materialGroups")
        if not groups or set(groups) - material_groups:
            raise ValueError("material condition references unknown or empty material groups")
        for group in groups:
            identity = (when["proposition"], when["state"], group)
            if identity in seen:
                raise ValueError("duplicate conditional material obligation")
            seen.add(identity)
        normalized.append({"when": dict(when), "materialGroups": list(groups)})
    return normalized


def resolve_material_requirements(plan: dict, propositions=()) -> dict:
    """Resolve declared material conditions from trusted deterministic states.

    Unknown or conflicting proposition states defer requirements; they are
    never treated as a negative condition. Existing conditionalRequirements
    remains legacy domain documentation and is not reinterpreted here.
    """
    execution = plan.get("executionPlan") or {}
    materials = deepcopy(plan.get("materialRequirements", execution.get("materials", [])))
    groups = {row["materialGroup"] for row in materials}
    proposition_codes = set(plan.get("requiredPropositions", []))
    proposition_codes.update(row["code"] for row in plan.get("propositionGraph", execution.get("propositions", [])))
    conditions = _conditional_materials({"conditionalMaterials": plan.get("conditionalMaterials", execution.get("conditionalMaterials", []))},
                                       proposition_codes, groups)
    contract = plan.get("coverageContract", execution.get("coverageContract", {})) or {}
    required = set(contract.get("materialGroups", []))
    if required - groups:
        raise ValueError("coverage references an unknown material group")
    required.update(row["materialGroup"] for row in materials if row.get("required") is True)
    states = {}
    for proposition in propositions:
        if not isinstance(proposition, dict) or proposition.get("code") not in proposition_codes:
            continue
        # Multiple events may activate a material independently. Conflicting
        # states for the same event remain unresolved rather than using a vote.
        context = (proposition.get("event_id"), proposition.get("workface"))
        state = proposition.get("state")
        state = state if isinstance(state, str) and state in {"true", "false"} else "unknown"
        if proposition.get("applicable") is False and state == "true":
            state = "not_applicable"
        states.setdefault(proposition["code"], {}).setdefault(context, set()).add(state)
    active, deferred, conditional_groups, assessed = set(), set(), set(), []
    for condition in conditions:
        when, targets = condition["when"], set(condition["materialGroups"])
        contexts = states.get(when["proposition"], {})
        known = [next(iter(values)) for values in contexts.values() if len(values) == 1 and "unknown" not in values]
        unresolved = not contexts or any(len(values) != 1 or "unknown" in values for values in contexts.values())
        status = "active" if when["state"] in known else "deferred" if unresolved else "inactive"
        conditional_groups.update(targets)
        if status == "active":
            active.update(targets)
        elif status == "deferred":
            deferred.update(targets)
        assessed.append({**condition, "status": status})
    required.update(active)
    deferred.difference_update(required)
    inactive = conditional_groups - required - deferred
    supporting = groups - required - deferred - inactive
    result = {"conditions": assessed, "legacy_conditions_interpreted": False}
    for status, selected in (("required", required), ("supporting", supporting), ("deferred", deferred), ("inactive", inactive)):
        result[status] = [row for row in materials if row["materialGroup"] in selected]
        result[status + "_material_groups"] = sorted(selected)
    return result


def validate_plan(plan: dict, *, registered_operators=()) -> dict:
    """Compile data-only execution contracts; operators are host registrations.

    Legacy material lists remain descriptive unless they explicitly declare a
    required group. Merely listing a source must not invent a new business gate.
    """
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object")
    compiled = deepcopy(plan)
    propositions = _names(compiled.get("requiredPropositions", []), "requiredPropositions")
    materials = compiled.get("materialRequirements", [])
    if not isinstance(materials, list):
        raise ValueError("materialRequirements must be a list")
    groups = []
    for item in materials:
        if not isinstance(item, dict) or not isinstance(item.get("materialGroup"), str) or not item["materialGroup"].strip():
            raise ValueError("material requirement needs materialGroup")
        groups.append(item["materialGroup"])
        if item.get("role", "statement") not in {"actual_record", "requirement", "statement", "regulation"}:
            raise ValueError("unknown material role")
        if item.get("cadence", "persistent") not in {"daily", "weekly", "monthly", "annual", "half_year", "persistent"}:
            raise ValueError("unknown material cadence")
        if "required" in item and type(item["required"]) is not bool:
            raise ValueError("material required must be boolean")
    _names(groups, "materialRequirements")
    conditions = compiled.get("conditionalRequirements", [])
    if not isinstance(conditions, list):
        raise ValueError("conditionalRequirements must be a list")
    for condition in conditions:
        if not isinstance(condition, dict) or not isinstance(condition.get("condition"), str) or not condition["condition"].strip():
            raise ValueError("conditional requirement needs condition")
        _names(condition.get("requires", []), "conditional requires")
    graph = compiled.get("propositionGraph", [])
    if not isinstance(graph, list):
        raise ValueError("propositionGraph must be a list")
    nodes = {}
    for node in graph:
        if not isinstance(node, dict) or not isinstance(node.get("code"), str) or not node["code"].strip() or node["code"] in nodes:
            raise ValueError("propositionGraph needs unique codes")
        nodes[node["code"]] = deepcopy(node)
    for code in propositions:
        nodes.setdefault(code, {"code": code})
    conditional_materials = _conditional_materials(compiled, set(nodes), set(groups))
    dependencies = compiled.get("propositionDependencies", {})
    if not isinstance(dependencies, dict):
        raise ValueError("propositionDependencies must be an object")
    if any(code not in nodes for code in dependencies):
        raise ValueError("dependency target is not a proposition")
    declared = compiled.get("deterministicOperators", [])
    if not isinstance(declared, list):
        raise ValueError("deterministicOperators must be a list")
    operator_ids = [item.get("id") if isinstance(item, dict) else item for item in declared]
    _names(operator_ids, "deterministicOperators")
    known = set(registered_operators)
    if set(operator_ids) - known:
        raise ValueError("unregistered deterministic operator")
    for code, node in nodes.items():
        deps = dependencies.get(code, node.get("dependsOn", []))
        _names(deps, f"dependencies for {code}")
        if any(dependency not in nodes for dependency in deps):
            raise ValueError("unknown proposition dependency")
        node["dependsOn"] = deps
        if node.get("operator") is not None and (not isinstance(node["operator"], str) or node["operator"] not in known):
            raise ValueError("unregistered proposition operator")
    order, active, done = [], set(), set()

    def visit(code):
        if code in active:
            raise ValueError("cyclic proposition dependencies")
        if code in done:
            return
        active.add(code)
        for dependency in nodes[code]["dependsOn"]:
            visit(dependency)
        active.remove(code)
        done.add(code)
        order.append(code)

    for code in nodes:
        visit(code)
    retrieval = compiled.get("retrievalQueries", {})
    if not isinstance(retrieval, dict) or set(retrieval) - set(nodes):
        raise ValueError("retrieval queries reference unknown propositions")
    for code, queries in retrieval.items():
        if (not isinstance(queries, list) or not queries or len(queries) > 12
                or any(not isinstance(query, str) or not query.strip() or len(query) > 2000 for query in queries)
                or len(set(queries)) != len(queries)):
            raise ValueError(f"invalid retrieval queries for {code}")
    facts = compiled.get("factRequirements", [])
    if not isinstance(facts, list):
        raise ValueError("factRequirements must be a list")
    fact_kinds = []
    for fact in facts:
        if not isinstance(fact, dict) or not isinstance(fact.get("kind"), str) or not fact["kind"].strip():
            raise ValueError("fact requirement needs kind")
        fact_kinds.append(fact["kind"])
        fields = fact.get("fields", {})
        if not isinstance(fields, dict) or any(not isinstance(name, str) or not name.strip() for name in fields):
            raise ValueError("fact fields must be named contracts")
        for contract in fields.values():
            if not isinstance(contract, dict) or contract.get("type") not in _FIELD_TYPES:
                raise ValueError("unknown fact field type")
            if "required" in contract and type(contract["required"]) is not bool:
                raise ValueError("field required must be boolean")
            if contract["type"] == "enum" and (not isinstance(contract.get("values"), list) or not contract["values"]):
                raise ValueError("enum field needs values")
    _names(fact_kinds, "factRequirements")
    tools = _names(compiled.get("allowedTools", sorted(_TOOLS)), "allowedTools")
    if set(tools) - _TOOLS:
        raise ValueError("unknown evidence tool")
    raw_budgets = compiled.get("budgets", {})
    if not isinstance(raw_budgets, dict) or set(raw_budgets) - (set(_BUDGETS) | set(_BUDGET_ALIASES)):
        raise ValueError("unknown execution budget")
    budgets = {}
    for raw_name, value in raw_budgets.items():
        name = _BUDGET_ALIASES.get(raw_name, raw_name)
        if type(value) is not int or not (0 if name == "maxModelRetries" else 1) <= value <= _BUDGETS[name]:
            raise ValueError("execution budgets must be bounded positive integers")
        if name in budgets and budgets[name] != value:
            raise ValueError("conflicting execution budget aliases")
        budgets[name] = value
    compiled["budgets"] = budgets
    coverage = compiled.get("coverageContract", {})
    if not isinstance(coverage, dict):
        raise ValueError("coverageContract must be an object")
    if "materialGroups" in coverage:
        _names(coverage["materialGroups"], "coverage materialGroups")
        if set(coverage["materialGroups"]) - set(groups):
            raise ValueError("coverage references an unknown material group")
    if "units" in coverage:
        units = coverage["units"]
        if not isinstance(units, list) or any(not isinstance(unit, dict) or not unit.get("file_id") or not unit.get("unit_id") for unit in units):
            raise ValueError("coverage units need file_id and unit_id")
        if len({(unit["file_id"], unit["unit_id"]) for unit in units}) != len(units):
            raise ValueError("duplicate coverage unit")
    compiled["executionPlan"] = {
        "propositionOrder": order, "propositions": [nodes[code] for code in order],
        "materials": materials, "factRequirements": facts, "coverageContract": coverage,
        "allowedTools": tools, "budgets": budgets, "conditionalMaterials": conditional_materials,
        "retrievalQueries": retrieval,
    }
    return compiled


def _domain_bundle_hash() -> str:
    root = Path(__file__).parents[2]
    paths = [Path(__file__)] + [root / "apps" / "control_api" / name for name in
        ("validator.py", "review_plan.py", "collection.py", "file_outcomes.py", "pdf_renderer.py", "native_parser.py",
         "documents.py", "evidence.py", "providers.py", "worker.py")]
    paths.extend(sorted((root / "apps" / "control_api" / "domain").rglob("*.py")))
    paths.extend(sorted((root / "apps" / "control_api" / "domain" / "data").glob("*.json")))
    digest = sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def load_plan(item_code: str) -> dict:
    if not isinstance(item_code, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,99}", item_code):
        raise ValueError("invalid inspection item code")
    root = Path(__file__).parents[2] / "rule_assets"
    versions = sorted(root.glob(f"{item_code}.v*.json"), key=lambda item: item.name)
    path = versions[-1] if versions else root / f"{item_code}.v1.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["assetRevision"] = sha256(path.read_bytes()).hexdigest()
    data["evaluatorRevision"] = _domain_bundle_hash()
    data["revision"] = sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return data


def compile_plan(item_code: str, *, evaluator_files: tuple[str, ...] = (), registered_operators=()) -> dict[str, Any]:
    plan = validate_plan(load_plan(item_code), registered_operators=registered_operators)
    root = Path(__file__).parents[2]
    digest = sha256()
    for name in evaluator_files:
        path = root / name
        digest.update(name.encode("utf-8"))
        digest.update(path.read_bytes())
    plan["additionalEvaluatorRevision"] = digest.hexdigest()
    plan["planRevision"] = sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    return plan


def assert_plan_compatible(run: dict) -> None:
    """Refuse to resume a frozen plan with a changed rule/evaluator bundle."""
    accepted = run.get("plan") or {}
    if not accepted.get("evaluatorRevision"):
        return  # Pure domain unit tests do not represent admitted jobs.
    current = compile_plan(run["item_code"])
    if accepted.get("planRevision") != current["planRevision"]:
        raise ValueError("accepted_rule_revision_unavailable: create a new execution or deploy the matching release")


def evaluate_coverage(*, item_code: str, source_complete: bool, proven: set[str], false: set[str], gaps: list[str], evidence_refs: list[str]) -> dict:
    """Coverage diagnostic only; boolean claims cannot determine compliance.

    Call evaluation.evaluate_run with trusted source evidence for business
    outcomes. This compatibility helper intentionally never returns pass or
    no_pass from caller supplied proposition names.
    """
    plan = load_plan(item_code)
    required = set(plan["requiredPropositions"])
    missing = sorted(required - proven - false)
    status = "unverifiable" if any("unreadable" in gap or "conflict" in gap for gap in gaps) else "pending"
    description = "覆盖诊断不能替代确定性规则判定"
    reasons = list(gaps) + [f"missing proposition: {code}" for code in missing] + ["deterministic_evaluation_required"]
    return {"status": status, "description": description, "detail": "; ".join(reasons), "evidence_refs": sorted(set(evidence_refs)), "coverage_gaps": reasons, "rule_revision": plan["revision"]}
