from __future__ import annotations

from typing import Any
import hashlib
import json

from packages.contracts.models import CandidateEnvelope
from .domain.fact_extraction import extract_facts
from .quality import validate_field_candidate


class CandidateRejected(ValueError):
    pass


def validate_candidate(candidate: CandidateEnvelope | dict[str, Any], *, evidence_by_id: dict[str, dict[str, Any]] | None = None, allowed_documents: set[str] | None = None, allowed_hashes: set[str] | None = None, run: dict[str, Any] | None = None, fact_contract: dict[str, Any] | None = None) -> dict[str, Any]:
    """Bind model output to trusted source records; model status is ignored."""
    raw = candidate.model_dump() if isinstance(candidate, CandidateEnvelope) else dict(candidate)
    evidence_by_id = evidence_by_id or {}
    allowed_documents = set(allowed_documents) if allowed_documents is not None else {entry.get("document_version") for entry in evidence_by_id.values()}
    allowed_hashes = set(allowed_hashes) if allowed_hashes is not None else {entry.get("content_hash") for entry in evidence_by_id.values()}
    reasons: list[str] = []
    if run and raw.get("run_id") != run.get("run_id"):
        reasons.append("run_id_mismatch")
    if run and run.get("rule_revision") and raw.get("rule_revision") != run["rule_revision"]:
        reasons.append("rule_revision_mismatch")
    refs = raw.get("evidence_refs") or []
    ref_ids = [ref if isinstance(ref, str) else ref.get("evidence_id") if isinstance(ref, dict) else getattr(ref, "evidence_id", None) for ref in refs]
    if not ref_ids:
        reasons.append("candidate_has_no_evidence_refs")
    trusted = []
    for ref_id in ref_ids:
        source = evidence_by_id.get(ref_id)
        if source is None:
            reasons.append(f"unknown_evidence:{ref_id}")
            continue
        if source.get("document_version") not in allowed_documents:
            reasons.append(f"document_outside_scope:{ref_id}")
        if allowed_hashes and source.get("content_hash") not in allowed_hashes:
            reasons.append(f"hash_outside_scope:{ref_id}")
        if run and (source.get("mine_id") != run.get("mine_id") or source.get("run_id") != run.get("run_id")):
            reasons.append(f"source_owner_mismatch:{ref_id}")
        trusted.append(source)
    field_evidence = raw.get("field_evidence") or {}
    fields = raw.get("fields") or {}
    bound_fields: dict[str, list[str]] = {}
    for field_name in fields:
        entries = field_evidence.get(field_name, field_evidence.get(f"fields.{field_name}", [])) if isinstance(field_evidence, dict) else []
        entries = [entries] if isinstance(entries, (str, dict)) else list(entries) if isinstance(entries, list) else []
        valid = []
        for entry in entries:
            ref_id = entry.get("evidence_id") if isinstance(entry, dict) else getattr(entry, "evidence_id", entry)
            source = evidence_by_id.get(ref_id)
            source_text = str(source.get("text") or source.get("quote") or "") if source else ""
            if source is None or not source_text.strip():
                reasons.append(f"field_unbound:{field_name}:{ref_id}")
                continue
            if ref_id not in ref_ids:
                reasons.append(f"field_evidence_not_in_refs:{field_name}:{ref_id}")
                continue
            requested_quote = entry.get("quote") if isinstance(entry, dict) else getattr(entry, "quote", None)
            if requested_quote and requested_quote not in source_text:
                reasons.append(f"field_quote_mismatch:{field_name}:{ref_id}")
                continue
            valid.append(ref_id)
        if valid:
            bound_fields[field_name] = valid
        else:
            reasons.append(f"field_unbound:{field_name}")
    if raw.get("status") in {"verified", "accepted", "pass", "no_pass"}:
        reasons.append("model_status_ignored")
    anchored_fields = {}
    field_contract_revision = None
    verification_scope = None
    if fact_contract:
        encoded = json.dumps(fact_contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        field_contract_revision = hashlib.sha256(encoded).hexdigest()
        # Pydantic envelopes expand refs into objects; quality validation uses
        # the same authoritative ID set as the ownership checks above.
        quality = validate_field_candidate({**raw, "evidence_refs": ref_ids}, evidence_by_id, fact_contract)
        anchored_fields = quality.get("verified_fields", {})
        reasons.extend(quality.get("reasons", []))
        verification_scope = quality.get("verification_scope", "source_anchor_only")
        status = "source_bound" if trusted and not reasons and quality.get("validation_status") == "source_bound" else "candidate"
    else:
        status = "source_bound" if trusted and not reasons else "candidate"
    matched = []
    if status == "source_bound" and fact_contract:
        # A registered field contract proves only that values are anchored to
        # permitted source text. Domain extraction remains the authority for
        # semantic facts and is intentionally not promoted on this path.
        return {"candidate": raw, "validation_status": status, "verification_scope": verification_scope or "source_anchor_only",
                "verified": False, "facts": [], "field_bindings": bound_fields, "anchored_fields": anchored_fields,
                "field_contract_revision": field_contract_revision, "reasons": list(dict.fromkeys(reasons))}
    if status == "source_bound":
        # Do not equate quotation matching with semantic verification. Only
        # facts independently derived from the original source can be promoted.
        parsed = extract_facts(trusted, allow_unbound=False)
        for fact in parsed["facts"]:
            known = {**fact.get("fields", {}), **{k: fact.get(k) for k in ("event_id", "workface", "time")}}
            if fact.get("kind") == raw.get("kind") and fields and all(known.get(key) == value for key, value in fields.items()):
                matched.append(fact)
        if not matched:
            reasons.append("semantic_fields_not_independently_verified")
            status = "candidate"
    return {"candidate": raw, "validation_status": status, "verification_scope": verification_scope,
            "verified": bool(matched), "facts": matched, "field_bindings": bound_fields,
            "anchored_fields": anchored_fields, "field_contract_revision": field_contract_revision,
            "reasons": list(dict.fromkeys(reasons))}
