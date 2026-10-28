"""Offline corpus scoring and deterministic splits; never creates a verdict."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math


_RESOLVED = {"adjudicated", "consensus", "resolved"}
_UNRESOLVED = {"unresolved", "disputed", "disagreement", "pending", "unadjudicated"}
_CASE_STATUSES = {"pass", "no_pass", "pending", "unverifiable", "failed"}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _document(value):
    if isinstance(value, list):
        value = {"facts": value}
    if not isinstance(value, dict):
        raise ValueError("annotations must be an object or a fact list")
    result = deepcopy(value)
    for name in ("facts", "cases"):
        result.setdefault(name, [])
        if not isinstance(result[name], list):
            raise ValueError(f"{name} must be a list")
    return result


def _identifier(row, name):
    value = row.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"annotation needs non-empty {name}")
    return value


def _index(rows, *, facts):
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("annotation must be an object")
        case_id = _identifier(row, "case_id")
        key = (case_id, _identifier(row, "field")) if facts else case_id
        if key in result:
            raise ValueError("duplicate case/field annotation" if facts else "duplicate case status annotation")
        if facts and "value" not in row:
            raise ValueError("fact annotation needs value")
        if not facts and row.get("status") is not None and (not isinstance(row["status"], str) or row["status"] not in _CASE_STATUSES):
            raise ValueError("unknown case status")
        if "issues" in row:
            issues = row["issues"]
            if not isinstance(issues, list) or any(not isinstance(issue, str) or not issue.strip() for issue in issues) or len(set(issues)) != len(issues):
                raise ValueError("issues must be unique non-empty issue identifiers")
        _canonical(row)  # Reject NaN/non-JSON annotations before counting.
        result[key] = row
    return result


def _adjudication(row, document):
    explicit = row.get("adjudication")
    if isinstance(explicit, dict):
        explicit = explicit.get("state")
    if isinstance(explicit, str):
        if explicit in _RESOLVED:
            return True
        if explicit in _UNRESOLVED:
            return False
        raise ValueError("unknown adjudication state")
    if "resolved" in row or "adjudicated" in row:
        return row.get("resolved") is True or row.get("adjudicated") is True
    if row.get("annotator_disagreement") is True:
        return False
    metadata = document.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("annotation metadata must be an object")
    inherited = document.get("adjudication", metadata.get("adjudication"))
    if isinstance(inherited, dict):
        inherited = inherited.get("state")
    if inherited is not None and inherited not in _RESOLVED | _UNRESOLVED:
        raise ValueError("unknown document adjudication state")
    if inherited is not None:
        return inherited in _RESOLVED
    return document.get("resolved") is True or document.get("adjudicated") is True


def _verified(row):
    states = [row.get(name) for name in ("status", "verification_status", "validation_status", "quality")]
    if any(isinstance(state, str) and state in {"candidate", "model_candidate", "unverified", "rejected"} for state in states):
        return False
    return row.get("verified") is True or any(state == "verified" for state in states)


def _references(row):
    refs = row.get("evidence_refs") or []
    if not isinstance(refs, list):
        raise ValueError("evidence_refs must be a list")
    output = []
    for ref in refs:
        if isinstance(ref, str) and ref.strip():
            output.append({"evidence_id": ref})
        elif isinstance(ref, dict) and ref:
            descriptor = {key: value for key, value in ref.items() if key in {"evidence_id", "file_id", "document_id", "document_version", "content_hash", "locator"}}
            if not descriptor or ("evidence_id" in descriptor and (not isinstance(descriptor["evidence_id"], str) or not descriptor["evidence_id"].strip())):
                raise ValueError("invalid evidence reference identity")
            if "locator" in descriptor and not isinstance(descriptor["locator"], dict):
                raise ValueError("evidence reference locator must be an object")
            output.append(descriptor)
        else:
            raise ValueError("invalid evidence reference")
    return output


def _located(gold, observed):
    expected_refs = _references(gold)
    expected_locator = gold.get("locator")
    if expected_locator is not None and not isinstance(expected_locator, dict):
        raise ValueError("locator must be an object")
    if not expected_refs and not expected_locator:
        return None
    if observed is None:
        return False
    if expected_locator and _canonical(observed.get("locator")) != _canonical(expected_locator):
        return False
    for name in ("file_id", "content_hash", "document_version"):
        if gold.get(name) is not None and observed.get(name) != gold[name]:
            return False
    if expected_refs:
        actual_refs = _references(observed)
        if not actual_refs or any(not any(all(_canonical(actual.get(key)) == _canonical(value) for key, value in expected.items())
                                          for expected in expected_refs) for actual in actual_refs):
            return False
    return True


def compare_annotations(gold, observed):
    """Compare sealed adjudicated labels with independently verified outputs.

    Missing adjudication is unresolved; it does not become ground truth by
    filename. Gold may declare adjudication at the document level. A field
    marked disputed remains excluded until that row is explicitly adjudicated.
    Field precision/recall count wrong values as both FP and FN. Exact match
    is the fraction of eligible cases whose complete field maps match.
    """
    gold, observed = _document(gold), _document(observed)
    for name in ("corpus_hash", "split_hash", "rule_revision"):
        if gold.get(name) is not None and observed.get(name) is not None and gold[name] != observed[name]:
            raise ValueError(f"annotation {name} mismatch")
    all_gold_facts, all_observed_facts = _index(gold["facts"], facts=True), _index(observed["facts"], facts=True)
    all_gold_cases, observed_cases = _index(gold["cases"], facts=False), _index(observed["cases"], facts=False)
    gold_cases = {key: row for key, row in all_gold_cases.items() if row.get("status") is not None and _adjudication(row, gold)}
    unresolved_cases = set(all_gold_cases) - set(gold_cases)
    gold_facts = {key: row for key, row in all_gold_facts.items() if _adjudication(row, gold)}
    eligible_cases = {key[0] for key in gold_facts}
    verified_facts = {key: row for key, row in all_observed_facts.items() if _verified(row)}
    predicted = {key: row for key, row in verified_facts.items()
                 if key[0] in eligible_cases and (key not in all_gold_facts or key in gold_facts)}
    matching = {key for key, row in predicted.items() if key in gold_facts and _canonical(row["value"]) == _canonical(gold_facts[key]["value"])}
    wrong = set(predicted) & set(gold_facts) - matching
    missing = set(gold_facts) - set(predicted)
    extra = set(predicted) - set(gold_facts)
    unresolved_fact_cases = {key[0] for key in all_gold_facts if key not in gold_facts}
    exact_eligible_cases = eligible_cases - unresolved_fact_cases
    exact_cases = 0
    for case in exact_eligible_cases:
        expected = {key[1]: row["value"] for key, row in gold_facts.items() if key[0] == case}
        actual = {key[1]: row["value"] for key, row in predicted.items() if key[0] == case}
        exact_cases += _canonical(expected) == _canonical(actual)
    locations = [_located(row, predicted.get(key)) for key, row in gold_facts.items()]
    located = [value for value in locations if value is not None]
    status_correct, false_no_pass, missed_cases, issue_tp, issue_fp, issue_fn = 0, [], [], 0, 0, 0
    issue_cases = 0
    for case_id, row in gold_cases.items():
        actual = observed_cases.get(case_id, {})
        status_correct += row["status"] == actual.get("status")
        if actual.get("status") == "no_pass" and row["status"] != "no_pass":
            false_no_pass.append(case_id)
        if row["status"] == "no_pass" and actual.get("status") != "no_pass":
            missed_cases.append(case_id)
        if "issues" in row:
            issue_cases += 1
            expected, detected = set(row["issues"]), set(actual.get("issues", []))
            issue_tp += len(expected & detected)
            issue_fp += len(detected - expected)
            issue_fn += len(expected - detected)
    observed_negatives = sum(observed_cases.get(case, {}).get("status") == "no_pass" for case in gold_cases)
    expected_negatives = sum(row["status"] == "no_pass" for row in gold_cases.values())
    exclusions = {
        "gold_facts_unresolved": len(all_gold_facts) - len(gold_facts),
        "gold_cases_unresolved": len(unresolved_cases),
        "observed_facts_unverified": len(all_observed_facts) - len(verified_facts),
        "verified_observed_facts_outside_adjudicated_scope": len(verified_facts) - len(predicted),
        "observed_cases_without_adjudicated_gold": len(set(observed_cases) - set(gold_cases)),
        "gold_fields_without_location_annotations": len(gold_facts) - len(located),
        "field_cases_excluded_from_exact_match_due_to_unresolved_fields": len(eligible_cases & unresolved_fact_cases),
    }
    return {
        "schema_version": "corpus-score-v1", "corpus_hash": gold.get("corpus_hash"), "split_hash": gold.get("split_hash"),
        "fields": {"precision": _ratio(len(matching), len(predicted)), "recall": _ratio(len(matching), len(gold_facts)),
                   "exact_match": _ratio(exact_cases, len(exact_eligible_cases)), "gold_count": len(gold_facts),
                   "verified_observed_count": len(predicted), "correct": len(matching), "wrong_values": len(wrong),
                   "missing": len(missing), "extra": len(extra), "exact_case_count": exact_cases,
                   "case_count": len(exact_eligible_cases), "field_case_count": len(eligible_cases)},
        "evidence": {"location_accuracy": _ratio(sum(located), len(located)), "correct": sum(located), "count": len(located)},
        "cases": {"status_accuracy": _ratio(status_correct, len(gold_cases)), "correct": status_correct, "count": len(gold_cases),
                  "false_no_pass": len(false_no_pass) if gold_cases else None,
                  "false_no_pass_rate": _ratio(len(false_no_pass), observed_negatives),
                  "missed_problem_cases": len(missed_cases) if gold_cases else None,
                  "problem_recall": _ratio(expected_negatives - len(missed_cases), expected_negatives),
                  "false_no_pass_case_ids": sorted(false_no_pass), "missed_problem_case_ids": sorted(missed_cases)},
        "issues": {"precision": _ratio(issue_tp, issue_tp + issue_fp), "recall": _ratio(issue_tp, issue_tp + issue_fn),
                   "correct": issue_tp if issue_cases else None, "missed": issue_fn if issue_cases else None,
                   "extra": issue_fp if issue_cases else None, "case_count": issue_cases},
        "exclusions": exclusions,
        "definitions": {"gold_policy": "explicit adjudication or declared consensus required; disagreements excluded until adjudicated",
                        "verified_policy": "explicit verification required; candidates excluded even when marked verified",
                        "exact_match": "complete field map equality within adjudicated field scope, including extra verified fields",
                        "location_accuracy": "gold-locatable fields with matching locator/version and nonempty allowed citations",
                        "unknown_denominators": "null", "thresholds": None},
    }


def split_corpus(manifest, seed, *, ratios=(0.6, 0.2, 0.2)):
    """Deterministically seal disjoint corpus partitions with byte provenance."""
    rows = manifest.get("cases") if isinstance(manifest, dict) else manifest
    if not isinstance(rows, list) or not isinstance(seed, (str, int)) or isinstance(seed, bool):
        raise ValueError("split needs a case manifest and explicit string/integer seed")
    if (len(ratios) != 3 or any(not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not math.isfinite(ratio) or ratio < 0 for ratio in ratios)
            or not math.isclose(sum(ratios), 1.0, rel_tol=0, abs_tol=1e-9)):
        raise ValueError("split ratios must contain three nonnegative values summing to one")
    cases, ids, hashes = [], set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("manifest case must be an object")
        case_id = _identifier(row, "case_id")
        if case_id in ids:
            raise ValueError("duplicate manifest case_id")
        content_hashes = row.get("content_hashes", [row.get("content_hash")])
        if not isinstance(content_hashes, list) or not content_hashes or any(not isinstance(value, str) or not value.strip() for value in content_hashes):
            raise ValueError("manifest case needs content_hash or content_hashes")
        if len(set(content_hashes)) != len(content_hashes) or set(content_hashes) & hashes:
            raise ValueError("shared source bytes across cases would leak between corpus partitions")
        ids.add(case_id)
        hashes.update(content_hashes)
        cases.append({"case_id": case_id, "content_hashes": sorted(content_hashes)})
    manifest_hash = _digest(sorted(cases, key=lambda row: row["case_id"]))
    ranked = sorted(cases, key=lambda row: (_digest([str(seed), row]), row["case_id"]))
    counts = [math.floor(len(cases) * ratio) for ratio in ratios]
    remainders = sorted(range(3), key=lambda index: (-(len(cases) * ratios[index] - counts[index]), index))
    for index in remainders[:len(cases) - sum(counts)]:
        counts[index] += 1
    output, offset = {}, 0
    for partition, count in zip(("train", "validation", "heldout"), counts):
        output[partition] = sorted(ranked[offset:offset + count], key=lambda row: row["case_id"])
        offset += count
    return {"schema_version": "corpus-split-v1", "manifest_hash": manifest_hash, "seed": str(seed),
            "ratios": dict(zip(("train", "validation", "heldout"), ratios)), "partitions": output,
            "partition_hashes": {name: _digest(rows) for name, rows in output.items()},
            "split_hash": _digest([manifest_hash, str(seed), ratios, output]), "case_count": len(cases)}
