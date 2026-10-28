"""Result envelope helper only; no synthetic source or mock evaluator."""


def result_base(run, status, description, detail):
    spec = run["spec"]
    return {"run_id": run["run_id"], "mine_code": spec["mine_code"], "itemCode": spec["item_code"],
            "checkCycle": spec["check_cycle"], "definition_version": spec.get("definition_version", "statutory-2026-v1"), "status": status,
            "description": description, "detail": detail, "evidence_refs": []}
