"""Risk-grade mapping for grade-matrix thresholds (蓝/黄/橙/红)."""

from __future__ import annotations

GRADE_ORDER = {"蓝": 0, "黄": 1, "橙": 2, "红": 3}
#: Lettered monitoring grades seen on real 临界指标 sheets (a < b < c < d).
LETTER_ORDER = {"a": 0, "b": 1, "c": 2, "d": 3}


def grade_rank(grade) -> int | None:
    text = str(grade or "").strip().lower()
    if text in LETTER_ORDER:
        return LETTER_ORDER[text]
    if text in GRADE_ORDER:
        return GRADE_ORDER[text]
    return None


def at_or_above(grade, baseline) -> bool | None:
    """Whether a grade reaches the baseline (e.g. c means c and d are over limit)."""
    rank, base = grade_rank(grade), grade_rank(baseline)
    if rank is None or base is None:
        return None
    return rank >= base

#: Document wording for risk level, mapped onto the four grade columns.
RISK_TO_GRADE = {
    "无": "蓝", "无冲击危险": "蓝", "无危险": "蓝",
    "弱": "黄", "弱冲击危险": "黄",
    "中等": "橙", "中等冲击危险": "橙", "中等冲击危险区域": "橙",
    "强": "红", "强冲击危险": "红", "强冲击危险区域": "红",
}


def normalize_risk(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text in GRADE_ORDER:
        return text
    if text in RISK_TO_GRADE:
        return RISK_TO_GRADE[text]
    # Substring matching only for multi-character keys: "无" alone would make
    # every sentence containing 无 look like grade 蓝.
    for key, grade in RISK_TO_GRADE.items():
        if len(key) >= 2 and key in text:
            return grade
    return None


def better_grade(left: str | None, right: str | None) -> str | None:
    """Return the more severe of two grades (used when several grades apply)."""
    if left is None:
        return right
    if right is None:
        return left
    return left if GRADE_ORDER.get(left, -1) >= GRADE_ORDER.get(right, -1) else right


def select_for_risk(thresholds, risk_value) -> tuple:
    """Pick the threshold whose grade matches the risk level.

    Returns (chosen, diagnostics). No match is reported instead of guessed.
    """
    grade = normalize_risk(risk_value)
    graded = [item for item in thresholds or [] if str(item.get("grade") or "").strip()]
    if not graded:
        return list(thresholds or []), {"grade": None, "reason": "NO_GRADED_THRESHOLDS"}
    if grade is None:
        return [], {"grade": None, "reason": "RISK_GRADE_UNKNOWN"}
    chosen = [item for item in graded if str(item.get("grade")).strip() == grade]
    if not chosen:
        return [], {"grade": grade, "reason": "NO_THRESHOLD_FOR_GRADE"}
    return chosen, {"grade": grade, "reason": None}
