"""Trusted inspection-item registry.

Inspection assets are selected by code, never imported from task input. The
registry is the extension boundary for future checks: each entry owns a plan
asset, deterministic evaluator and immutable source fingerprints.
"""
from __future__ import annotations

import inspect
import json
import marshal
import math
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

from .planner import _domain_bundle_hash, assert_plan_compatible, validate_plan


Evaluator = Callable[[dict, dict], dict]


def _portable_code(code):
    """Keep release fingerprints independent of installation paths/line maps."""
    return code.replace(co_filename="", co_firstlineno=0, co_linetable=b"",
                        co_consts=tuple(_portable_code(value) if inspect.iscode(value) else value
                                        for value in code.co_consts))


@dataclass(frozen=True)
class InspectionRegistration:
    item_code: str
    asset_path: Path
    evaluator: Evaluator
    operators: tuple[str, ...]
    fact_contract: dict[str, Any]
    fact_validator: Callable | None
    asset_revision: str
    evaluator_revision: str


def _revision_value(value, active=None):
    """Canonicalize trusted callable configuration without address-based reprs."""
    active = active if active is not None else set()
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("callable revision contains a non-finite value")
        return value
    identity = id(value)
    if identity in active:
        raise ValueError("callable revision contains cyclic state")
    active.add(identity)
    try:
        if callable(value):
            return {"callable": _evaluator_components(value, active)}
        if isinstance(value, Mapping):
            if any(not isinstance(key, str) for key in value):
                raise ValueError("callable revision mapping keys must be strings")
            return {key: _revision_value(item, active) for key, item in sorted(value.items())}
        if isinstance(value, (list, tuple)):
            return {"sequence_type": type(value).__name__, "items": [_revision_value(item, active) for item in value]}
        raise ValueError("callable revision state must be canonical data or trusted callables")
    finally:
        active.remove(identity)


def _evaluator_components(evaluator: Evaluator, active) -> dict:
    target = evaluator if inspect.isfunction(evaluator) else type(evaluator)
    if inspect.ismethod(evaluator):
        target = evaluator.__func__
    try:
        source = inspect.getsource(target)
    except (OSError, TypeError):
        code = getattr(evaluator, "__code__", None)
        if code is None:
            raise ValueError("evaluator must have inspectable source or Python bytecode")
        source = ""
    components = {"source": sha256(source.encode("utf-8")).hexdigest(),
                  "identity": target.__module__ + "." + target.__qualname__}
    code = getattr(evaluator, "__code__", None)
    if code is not None:
        components["code"] = sha256(marshal.dumps(_portable_code(code))).hexdigest()
        components["defaults"] = _revision_value(getattr(evaluator, "__defaults__", None), active)
        components["kwdefaults"] = _revision_value(getattr(evaluator, "__kwdefaults__", None), active)
        closure = []
        for cell in getattr(evaluator, "__closure__", None) or ():
            try:
                contents = cell.cell_contents
            except ValueError:
                closure.append({"empty_cell": True})
            else:
                closure.append(_revision_value(contents, active))
        components["closure"] = closure
    if inspect.ismethod(evaluator):
        components["bound_state"] = _revision_value(vars(evaluator.__self__), active)
    elif not inspect.isfunction(evaluator):
        revision = getattr(evaluator, "revision_components", None)
        if revision is not None:
            components["state"] = _revision_value(revision() if callable(revision) else revision, active)
        elif hasattr(evaluator, "__dict__"):
            components["state"] = _revision_value(vars(evaluator), active)
        else:
            raise ValueError("callable instance needs canonical state or revision_components")
    source_file = inspect.getsourcefile(target)
    if source_file and Path(source_file).is_file():
        components["source_file"] = sha256(Path(source_file).read_bytes()).hexdigest()
    return components


def _evaluator_fingerprint(evaluator: Evaluator) -> str:
    components = _revision_value(evaluator)
    return sha256(json.dumps(components, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


class CheckRegistry:
    def __init__(self, *, root: Path | None = None):
        self.root = (root or Path(__file__).parents[2]).resolve()
        self._entries: dict[str, InspectionRegistration] = {}

    def register(self, item_code: str, asset_path: str | Path, evaluator: Evaluator,
                 *, operators: tuple[str, ...] | list[str] = (), fact_contract: dict | None = None,
                 fact_validator: Callable | None = None) -> None:
        if not isinstance(item_code, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", item_code) or item_code in self._entries:
            raise ValueError("item_code must be non-empty and unique")
        if not callable(evaluator):
            raise TypeError("evaluator must be callable")
        path = Path(asset_path).resolve()
        if self.root not in path.parents or path.suffix.lower() != ".json" or not path.is_file():
            raise ValueError("inspection asset must be an existing JSON file inside registry root")
        operator_names = tuple(operators)
        if len(set(operator_names)) != len(operator_names) or any(not isinstance(value, str) or not value.strip() for value in operator_names):
            raise ValueError("operators must contain non-empty names")
        if fact_contract is not None and not isinstance(fact_contract, dict):
            raise ValueError("fact_contract must be an object")
        if fact_validator is not None and not callable(fact_validator):
            raise ValueError("fact_validator must be a trusted callable")
        if fact_validator is not None:
            _evaluator_fingerprint(fact_validator)
        self._entries[item_code] = InspectionRegistration(
            item_code=item_code, asset_path=path, evaluator=evaluator,
            operators=operator_names, fact_contract=deepcopy(fact_contract or {}),
            fact_validator=fact_validator,
            asset_revision=sha256(path.read_bytes()).hexdigest(),
            evaluator_revision=_evaluator_fingerprint(evaluator),
        )

    def supported_items(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    def _entry(self, item_code: str) -> InspectionRegistration:
        try:
            return self._entries[item_code]
        except KeyError as exc:
            raise ValueError(f"unsupported inspection item: {item_code}") from exc

    def compile(self, item_code: str) -> dict[str, Any]:
        entry = self._entry(item_code)
        raw = json.loads(entry.asset_path.read_text(encoding="utf-8"))
        if raw.get("itemCode") != item_code:
            raise ValueError("inspection asset itemCode mismatch")
        plan = validate_plan(raw, registered_operators=entry.operators)
        plan["assetRevision"] = sha256(entry.asset_path.read_bytes()).hexdigest()
        plan["evaluatorRevision"] = (_domain_bundle_hash() if item_code == "stop_work_unload"
                                      else _evaluator_fingerprint(entry.evaluator))
        plan["registry"] = {"itemCode": item_code, "assetRevision": plan["assetRevision"],
                             "evaluatorRevision": _evaluator_fingerprint(entry.evaluator),
                             "factValidatorRevision": _evaluator_fingerprint(entry.fact_validator) if entry.fact_validator else None,
                             "factContract": deepcopy(entry.fact_contract)}
        plan["revision"] = sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        plan["planRevision"] = sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        return plan

    def assert_compatible(self, run: dict) -> None:
        code = run.get("item_code") or run.get("itemCode")
        self._entry(code)
        accepted = run.get("plan") or {}
        if not accepted.get("registry"):
            if code == "stop_work_unload":
                assert_plan_compatible(run)
                return
            raise ValueError("inspection registry revision is missing")
        if accepted.get("planRevision"):
            content = {key: value for key, value in accepted.items() if key != "planRevision"}
            if accepted["planRevision"] != sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest():
                raise ValueError("accepted inspection plan fingerprint is invalid")
        registry = accepted.get("registry") or {}
        current = self.compile(code)
        if accepted.get("planRevision") != current["planRevision"]:
            raise ValueError("inspection registry revision changed; create a new execution")

    def evaluate(self, run: dict, snapshot: dict) -> dict:
        code = run.get("item_code") or run.get("itemCode")
        self.assert_compatible(run)
        return self._entry(code).evaluator(run, snapshot)

    def verify_candidate(self, run: dict, bound: dict, snapshot: dict) -> dict:
        """Promote only facts independently proven by the registered adapter.

        The general field contract establishes source anchoring; a trusted
        check-specific adapter must still independently parse the relevant
        source semantics. Neither candidates nor review flags choose it.
        """
        code = run.get("item_code") or run.get("itemCode")
        entry = self._entry(code)
        if entry.fact_validator is None or bound.get("validation_status") != "source_bound":
            return deepcopy(bound)
        self.assert_compatible(run)
        result = deepcopy(bound)
        raw = result.get("candidate") or {}
        mine = run.get("mine_id", run.get("mineId", run.get("request", {}).get("mineId")))
        run_id = run.get("run_id", run.get("runId"))
        revision = run.get("rule_revision", (run.get("plan") or {}).get("planRevision"))
        registry = {record["evidence_id"]: record for record in snapshot.get("evidence", [])
                    if isinstance(record, dict) and record.get("evidence_id")}
        references = raw.get("evidence_refs") or []
        ref_ids = [ref if isinstance(ref, str) else ref.get("evidence_id") for ref in references]
        fields = raw.get("fields") or {}
        bindings = result.get("field_bindings") or {}
        reasons = []
        if result.get("reasons"):
            reasons.append("fact_validator_candidate_has_binding_errors")
        if (raw.get("run_id") != run_id or raw.get("rule_revision") != revision
                or snapshot.get("run_id") not in {None, run_id}
                or snapshot.get("mine_id") not in {None, mine}):
            reasons.append("fact_validator_owner_mismatch")
        if not isinstance(fields, dict) or not fields or not ref_ids or not set(ref_ids) <= set(registry):
            reasons.append("fact_validator_source_unbound")
        for field in fields if isinstance(fields, dict) else ():
            refs = bindings.get(field, bindings.get("fields." + field, []))
            if not isinstance(refs, list) or not refs or not set(refs) <= set(ref_ids):
                reasons.append("fact_validator_field_unbound:" + str(field))
        if not reasons:
            try:
                verified = entry.fact_validator(deepcopy(raw), deepcopy(snapshot))
            except (ValueError, TypeError, KeyError):
                verified = []
            if not isinstance(verified, list):
                verified = []
                reasons.append("fact_validator_invalid_result")
            accepted = []
            for fact in verified:
                if not isinstance(fact, dict):
                    reasons.append("fact_validator_invalid_fact")
                    continue
                refs = fact.get("evidence_refs") or []
                role, fact_fields = fact.get("source_role"), fact.get("fields")
                if (not isinstance(fact.get("fact_id"), str) or not fact["fact_id"].strip()
                        or fact.get("kind") != raw.get("kind") or not isinstance(fact_fields, dict) or not fact_fields
                        or role not in {"actual_record", "requirement"} or fact.get("quality") in {"candidate", "model_candidate", "conflicted_review"}
                        or not isinstance(refs, list) or not refs or any(not isinstance(ref, str) for ref in refs)
                        or not set(refs) <= set(ref_ids)):
                    reasons.append("fact_validator_invalid_fact")
                    continue
                if (fact.get("run_id") not in {None, run_id} or fact.get("mine_id") not in {None, mine}
                        or any(registry[ref].get("source_role") != role or registry[ref].get("quality") in {"candidate", "model_candidate", "conflicted_review"}
                               or registry[ref].get("run_id") not in {None, run_id}
                               or registry[ref].get("mine_id") not in {None, mine} for ref in refs)):
                    reasons.append("fact_validator_source_role_or_owner_mismatch")
                    continue
                unbound = [field for field, value in fact_fields.items()
                           if field not in fields or type(value) is not type(fields[field]) or value != fields[field]
                           or not set(bindings.get(field, bindings.get("fields." + field, []))) & set(refs)]
                if unbound:
                    reasons.append("fact_validator_fact_fields_unbound")
                    continue
                accepted.append({**deepcopy(fact), "run_id": run_id, "mine_id": mine,
                                 "fact_validator_revision": _evaluator_fingerprint(entry.fact_validator)})
            covered_fields = {field for fact in accepted for field in fact["fields"]}
            if covered_fields != set(fields):
                reasons.append("fact_validator_candidate_fields_not_fully_verified")
            if not accepted:
                reasons.append("semantic_fields_not_independently_verified")
        else:
            accepted = []
        if reasons:
            result.update(verified=False, facts=[], verification_scope="source_anchor_only")
            result["reasons"] = list(dict.fromkeys(result.get("reasons", []) + reasons))
        else:
            result.update(verified=True, facts=accepted, verification_scope="independent_registered_validator",
                          fact_validator_revision=_evaluator_fingerprint(entry.fact_validator))
        return result


def default_registry() -> CheckRegistry:
    registry = CheckRegistry()
    from .domain.evaluator import evaluate_run
    versions = sorted((registry.root / "rule_assets").glob("stop_work_unload.v*.json"), key=lambda item: item.name)
    registry.register("stop_work_unload", versions[-1], evaluate_run)
    return registry
