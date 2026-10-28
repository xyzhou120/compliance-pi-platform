"""Public deterministic evaluation boundary.

Only registered source records are parsed into facts. Client/model supplied
facts, true/false flags and business status are never evaluation inputs.
"""

from .registry import default_registry


def evaluate_run(run: dict, snapshot: dict) -> dict:
    registry = default_registry()
    if run.get("item_code") not in registry.supported_items():
        return {"status": "failed", "description": "检查项尚未实现", "detail": "unsupported item_code",
                "propositions": [], "findings": [], "evidence_refs": [], "coverage_gaps": ["unsupported_item_code"]}
    return registry.evaluate(run, snapshot)
