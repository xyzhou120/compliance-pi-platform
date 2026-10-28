"""Conservative field and review validation for model/OCR candidates."""
from __future__ import annotations

import re
import hashlib
import hmac
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any


def _review_canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sign_transcription_review(payload: dict[str, Any], key: str | bytes) -> dict[str, Any]:
    """Trusted review-service helper; never expose signing through the API."""
    if not isinstance(payload, dict) or not key:
        raise ValueError("review payload and signing key are required")
    secret = key.encode("utf-8") if isinstance(key, str) else key
    if not isinstance(secret, bytes):
        raise ValueError("invalid review signing key")
    encoded = _review_canonical(payload)
    # Decode the canonical bytes to detach caller-owned nested values.
    return {"payload": json.loads(encoded), "signature": hmac.new(secret, encoded, hashlib.sha256).hexdigest()}


def apply_transcription_reviews(snapshot: dict[str, Any], receipts: list[dict[str, Any]], key: str | bytes) -> dict[str, Any]:
    """Verify independent transcriptions without modifying native evidence.

    A signed reviewer attests only transcription of the specified frozen page.
    It cannot set compliance, completeness, or model verification flags.
    """
    evidence, updates, gaps = [], [], []
    secret = key.encode("utf-8") if isinstance(key, str) else key
    files = {item.get("file_id"): item for item in snapshot.get("files", []) if isinstance(item, dict)}
    native_coverage = [item for item in snapshot.get("coverage", []) if isinstance(item, dict)]
    originals = [item for item in snapshot.get("evidence", []) if isinstance(item, dict)]
    required = {"review_id", "authority", "reviewer", "policy_revision", "file_id", "content_hash", "page", "text", "text_hash", "source_role"}
    seen = {}
    page_texts = {}
    conflicting_review_ids = set()

    def fail(review_id, reason):
        gaps.append(f"transcription_review:{review_id}:{reason}")

    for receipt in receipts if isinstance(receipts, list) else []:
        review_id = "unknown"
        if not isinstance(receipt, dict) or set(receipt) != {"payload", "signature"} or not isinstance(receipt.get("payload"), dict):
            fail(review_id, "invalid_receipt")
            continue
        payload = receipt["payload"]
        review_id = payload.get("review_id", review_id)
        if not isinstance(secret, bytes) or not secret:
            fail(review_id, "verification_key_missing")
            continue
        if set(payload) != required or any(not isinstance(payload.get(name), str) or not payload[name].strip() for name in required - {"page"}):
            fail(review_id, "invalid_payload")
            continue
        try:
            encoded = _review_canonical(payload)
            receipt_hash = hashlib.sha256(_review_canonical(receipt)).hexdigest()
        except (TypeError, ValueError):
            fail(review_id, "invalid_payload")
            continue
        signature = receipt.get("signature")
        expected = hmac.new(secret, encoded, hashlib.sha256).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
            fail(review_id, "signature_mismatch")
            continue
        if review_id in seen:
            if seen[review_id] != receipt_hash:
                fail(review_id, "review_identity_conflict")
                conflicting_review_ids.add(review_id)
            continue
        seen[review_id] = receipt_hash
        file = files.get(payload["file_id"])
        if file is None or payload["content_hash"] != file.get("content_hash"):
            fail(review_id, "frozen_file_hash_mismatch")
            continue
        role = payload["source_role"]
        if role not in {"actual_record", "requirement"} or role != file.get("source_role", file.get("role")):
            fail(review_id, "source_role_mismatch")
            continue
        page = payload["page"]
        count = file.get("page_count")
        if type(page) is not int or page < 1 or (type(count) is int and page > count):
            fail(review_id, "page_outside_frozen_coverage")
            continue
        units = [unit for unit in native_coverage if unit.get("file_id") == payload["file_id"]
                 and (unit.get("unit_id") == f"page:{page}"
                      or (page == 1 and file.get("format") in {"png", "jpg", "jpeg", "bmp", "tif", "tiff", "webp"} and unit.get("unit_id") == "document"))]
        if not units:
            fail(review_id, "page_outside_frozen_coverage")
            continue
        text = payload["text"]
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != payload["text_hash"]:
            fail(review_id, "text_hash_mismatch")
            continue
        derived = [record for record in originals
                   if record.get("quality") == "model_candidate"
                   and record.get("file_id") == payload["file_id"]
                   and record.get("content_hash") == payload["content_hash"]
                   and (record.get("locator") or {}).get("page") == page
                   and record.get("evidence_id")
                   and all(not snapshot.get(name) or record.get(name, snapshot[name]) == snapshot[name] for name in ("run_id", "mine_id"))]
        if not derived:
            fail(review_id, "model_candidate_source_missing")
            continue
        unit_id = units[0]["unit_id"]
        record_id = "reviewed-" + hashlib.sha256(encoded).hexdigest()
        record = {name: snapshot[name] for name in ("run_id", "mine_id", "mine_code", "source_system", "environment", "snapshot_id", "rule_revision") if name in snapshot}
        record.update(evidence_id=record_id, file_id=payload["file_id"], document_id=payload["file_id"], document_version=payload["content_hash"],
                      content_hash=payload["content_hash"], source_role=role, locator={"kind": "reviewed_transcription", "page": page},
                      unit_id=unit_id, text=text, quote=text, quality="independently_reviewed", material_group=file.get("material_group"),
                      origin_document_version=payload["content_hash"], derived_from=sorted({item["evidence_id"] for item in derived}),
                      review_receipt_hash=receipt_hash, review_id=review_id, authority=payload["authority"], reviewer=payload["reviewer"], policy_revision=payload["policy_revision"])
        evidence.append(record)
        updates.append({"file_id": payload["file_id"], "unit_id": unit_id, "state": "readable", "reason": "independent_review",
                        "evidence_refs": [record_id], "review_receipt_hash": receipt_hash, "native_state": units[0].get("state")})
        page_texts.setdefault((payload["file_id"], page), set()).add(payload["text_hash"])

    for (file_id, page), hashes in page_texts.items():
        identity_conflict = any(record["review_id"] in conflicting_review_ids and record["file_id"] == file_id and record["locator"]["page"] == page for record in evidence)
        if len(hashes) > 1 or identity_conflict:
            gaps.append(f"transcription_review:{file_id}/page:{page}:reviewed_transcriptions_conflict")
            for record in evidence:
                if record["file_id"] == file_id and record["locator"]["page"] == page:
                    # Conflicting reviewer attestations remain available as
                    # evidence, but cannot enter deterministic source facts.
                    record.update(origin_source_role=record["source_role"], source_role="statement", quality="conflicted_review", conflicted=True)
            for unit in updates:
                if unit["file_id"] == file_id and unit["unit_id"] in {f"page:{page}", "document"}:
                    unit.update(state="conflicted", reason="independent_review_conflict")
    return {"evidence": evidence, "coverage_updates": updates, "gaps": list(dict.fromkeys(gaps))}


def _source_text(source: dict) -> str:
    return str(source.get("text") or source.get("quote") or "")


def _number(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _typed_value(value: Any, text: str, spec: dict) -> bool:
    kind = spec.get("type", "text")
    label = spec.get("source_label")
    if label and str(label) not in text:
        return False
    if kind == "text":
        return isinstance(value, str) and bool(value.strip()) and value in text
    if kind == "number":
        if isinstance(value, bool):
            return False
        number = _number(value)
        unit = spec.get("unit")
        return number is not None and (not unit or unit in text) and any(_number(token) == number for token in re.findall(r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?![\w.])", text))
    if kind == "bool":
        if type(value) is not bool:
            return False
        positive = spec.get("true_values", [])
        negative = spec.get("false_values", [])
        # The host contract must name the actual source phrases. A stray 已 or
        # 是 cannot prove an unrelated boolean field.
        if not isinstance(positive, list) or not isinstance(negative, list) or not positive or not negative:
            return False
        yes = any(isinstance(token, str) and token and token in text for token in positive)
        no = any(isinstance(token, str) and token and token in text for token in negative)
        return yes and not no if value else no and not yes
    if kind == "date":
        try:
            expected = date.fromisoformat(str(value))
            for token in re.findall(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{4}年\d{1,2}月\d{1,2}日", text):
                values = [int(number) for number in re.findall(r"\d+", token)]
                if date(*values) == expected:
                    return True
            return False
        except ValueError:
            return False
    return False


def validate_field_candidate(candidate: dict[str, Any], evidence_by_id: dict[str, dict[str, Any]], fact_contract: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate source anchors only, not inferred meaning or business truth."""
    contract = fact_contract or {}
    fields = candidate.get("fields") if isinstance(candidate.get("fields"), dict) else {}
    refs = candidate.get("evidence_refs") or []
    field_evidence = candidate.get("field_evidence") or {}
    reasons, verified = [], {}
    allowed_roles = set(contract.get("source_roles", ["actual_record", "requirement", "regulation"]))
    if candidate.get("status", "candidate") != "candidate" or any(name in candidate for name in ("verified", "verdict", "source_complete", "findings")):
        reasons.append("model_verification_or_status_claim_ignored")
    for name, value in fields.items():
        spec = contract.get("fields", {}).get(name) if isinstance(contract.get("fields"), dict) else None
        if not isinstance(spec, dict) or spec.get("type") not in {"text", "number", "date", "bool"}:
            reasons.append(f"field_contract_missing:{name}")
            continue
        entries = field_evidence.get(name, field_evidence.get(f"fields.{name}", []))
        entries = [entries] if isinstance(entries, (str, dict)) else entries if isinstance(entries, list) else []
        valid = []
        for entry in entries:
            ref = entry if isinstance(entry, str) else entry.get("evidence_id") if isinstance(entry, dict) else None
            source = evidence_by_id.get(ref)
            quote = entry.get("quote", "") if isinstance(entry, dict) else ""
            if not ref or ref not in refs or source is None:
                reasons.append(f"field_unbound:{name}"); continue
            if allowed_roles and source.get("source_role") not in allowed_roles:
                reasons.append(f"source_role_not_allowed:{name}:{ref}"); continue
            if any(candidate.get(key) and source.get(key) != candidate[key] for key in ("run_id", "mine_id")):
                reasons.append(f"source_owner_mismatch:{name}:{ref}"); continue
            if source.get("quality") in {"candidate", "model_candidate"}:
                reasons.append(f"unverified_source_quality:{name}:{ref}"); continue
            text = _source_text(source)
            if quote and quote not in text:
                reasons.append(f"field_quote_mismatch:{name}:{ref}"); continue
            if spec.get("type") == "bool":
                yes = any(token in text for token in spec.get("true_values", []) if isinstance(token, str) and token)
                no = any(token in text for token in spec.get("false_values", []) if isinstance(token, str) and token)
                if yes and no:
                    reasons.append(f"field_conflicted:{name}:{ref}"); continue
            if not _typed_value(value, quote or text, spec):
                reasons.append(f"field_value_not_anchored:{name}:{ref}"); continue
            if value is True and any(token in (quote or text) for token in spec.get("negation_tokens", ("未", "没有", "否"))):
                reasons.append(f"field_negated:{name}:{ref}"); continue
            valid.append(ref)
        if valid: verified[name] = sorted(set(valid))
        else: reasons.append(f"field_unbound:{name}")
    for name, spec in contract.get("fields", {}).items():
        if isinstance(spec, dict) and spec.get("required") is True and name not in fields:
            reasons.append(f"required_field_missing:{name}")
    return {"candidate": candidate, "validation_status": "source_bound" if fields and not reasons and len(verified) == len(fields) else "candidate", "verification_scope": "source_anchor_only", "verified_fields": verified, "reasons": list(dict.fromkeys(reasons))}


def validate_review_evidence(payload: dict[str, Any], evidence_by_id: dict[str, dict] | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload.get("review_id") or not payload.get("candidate_id"):
        raise ValueError("review identity required")
    if any(key in payload for key in ("status", "result", "verdict", "final_status", "verified", "source_complete", "findings")):
        raise ValueError("review cannot change business status")
    fields = payload.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("review fields with evidence references required")
    for name, refs in fields.items():
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or not ref for ref in refs):
            raise ValueError(f"review field lacks evidence: {name}")
        if evidence_by_id is not None and any(ref not in evidence_by_id for ref in refs):
            raise ValueError(f"review field references unknown evidence: {name}")
    return {"review_id": payload["review_id"], "candidate_id": payload["candidate_id"], "fields": fields, "comment": str(payload.get("comment", "")), "state": "recorded"}
