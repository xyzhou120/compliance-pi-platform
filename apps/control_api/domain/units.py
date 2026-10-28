"""Shared unit table: one source of truth for quantity handling.

Rules refuse to compare across scale families (percent vs ratio); the field layer
uses the same table, so a value that cannot be compared is never presented as
comparable downstream.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

UNITS = {
    "mpa": ("pressure", "1", "pressure"), "kpa": ("pressure", "0.001", "pressure"),
    "pa": ("pressure", "0.000001", "pressure"),
    "j": ("energy", "1", "energy"), "kj": ("energy", "1000", "energy"),
    "kg/m": ("drill_mass", "1", "drill_mass"), "g/m": ("drill_mass", "0.001", "drill_mass"),
    "1": ("index", "1", "ratio"), "无量纲": ("index", "1", "ratio"),
    "%": ("index", "0.01", "percent"),
    "m": ("length", "1", "length"), "cm": ("length", "0.01", "length"),
    "mm": ("length", "0.001", "length"),
    "mv": ("voltage", "1", "voltage"), "v": ("voltage", "1000", "voltage"),
}


def number(value):
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def quantity(value, unit):
    """Return (dimension, converted value, scale family) or None."""
    parsed = number(value)
    entry = UNITS.get(str(unit or "").strip().lower())
    if parsed is None or entry is None:
        return None
    return entry[0], parsed * Decimal(entry[1]), entry[2]


def comparable(left, right) -> bool:
    """True only when two quantities share dimension and scale family."""
    if left is None or right is None:
        return False
    return left[0] == right[0] and left[2] == right[2]
