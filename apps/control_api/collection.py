"""Host-owned coverage sweep; local failures never skip unrelated documents."""
from __future__ import annotations

import time


def collect_native_files(service, run_id, owner, fence, *, deadline=None, check):
    """Attempt every frozen file once, preserving Pi's evidence interpretation."""
    run = service._run(run_id)
    known = {unit.get("input", {}).get("file_id") for unit in run.get("work_units", [])
             if unit.get("kind") == "parse_file" and unit.get("state") in {"succeeded", "failed"}}
    file_ids = [file["file_id"] for file in (run.get("snapshot") or {}).get("files", []) if file["file_id"] not in known]
    outcomes = []
    for offset in range(0, len(file_ids), min(service.settings.max_document_concurrency, 32)):
        check()
        if deadline is not None and time.monotonic() >= deadline:
            service.store.save_records(run_id, owner, fence, "gap", [{"reason": "collection_deadline", "stage": "native_parse",
                "file_ids": file_ids[offset:]}])
            break
        batch = file_ids[offset:offset + min(service.settings.max_document_concurrency, 32)]
        result = service.inspect_documents(run_id, batch, worker_id=owner, fence=fence, deadline=deadline)
        outcomes.extend(result["items"])
    service.store.checkpoint(run_id, owner, fence, {"stage": "parse", "event": "finished",
        "attempted_files": len(outcomes), "failed_files": sum(bool(row.get("unit_failed")) for row in outcomes), "scope": "host_inventory_sweep"})
    return outcomes


def collect_remaining_pages(service, run_id, owner, fence, *, deadline=None, check):
    """Finish eligible OCR pages within existing budgets, without retry loops."""
    if not service.settings.provider_settings().get("endpoint"):
        return {"attempted_pages": 0, "reason": "provider_endpoint_missing"}
    run = service._run(run_id)
    snapshot = service.snapshot(run)
    files = {file["file_id"]: file for file in snapshot.get("files", [])}
    attempts = {(unit.get("input", {}).get("file_id"), unit.get("input", {}).get("page"))
                for unit in run.get("work_units", [])
                if unit.get("kind") == "recognize_page" and unit.get("state") in {"succeeded", "running"}}
    pages = []
    for unit in snapshot.get("coverage", []):
        file, uid = files.get(unit.get("file_id"), {}), str(unit.get("unit_id", ""))
        if unit.get("state") not in {"partial", "unreadable", "not_indexed", "unsupported", "provider_failed"}:
            continue
        page = int(uid[5:]) if uid.startswith("page:") and uid[5:].isdigit() else 1
        if file.get("format") not in {"pdf", "png", "jpg", "jpeg", "bmp", "tiff", "tif", "webp"}:
            continue
        if (file["file_id"], page) not in attempts:
            pages.append({"file_id": file["file_id"], "page": page})
            attempts.add((file["file_id"], page))
    order = {"prevention_daily": 0, "threshold": 1, "release": 2, "dispatch_daily": 3, "design": 4, "relief_acceptance": 5}
    pages.sort(key=lambda page: (order.get(files[page["file_id"]].get("material_group"), 6), page["file_id"], page["page"]))
    budgets = (run.get("plan") or {}).get("executionPlan", {}).get("budgets", (run.get("plan") or {}).get("budgets", {}))
    cap = min(service.settings.max_total_pages, budgets.get("maxPages", service.settings.max_total_pages))
    already = {(unit.get("input", {}).get("file_id"), unit.get("input", {}).get("page"))
               for unit in run.get("work_units", [])
               if unit.get("kind") == "recognize_page" and unit.get("state") in {"succeeded", "running"}}
    available = max(0, cap - len(already))
    if len(pages) > available:
        omitted = pages[available:]
        service.store.save_records(run_id, owner, fence, "gap", [{"reason": "collection_page_budget",
            "stage": "page_recognition", "remaining_pages": len(omitted), "file_ids": sorted({page['file_id'] for page in omitted})}])
        pages = pages[:available]
    processed = 0
    for offset in range(0, len(pages), service.settings.provider_concurrency):
        check()
        if deadline is not None and time.monotonic() >= deadline:
            service.store.save_records(run_id, owner, fence, "gap", [{"reason": "collection_deadline", "stage": "page_recognition",
                "remaining_pages": len(pages) - offset}])
            break
        selected = pages[offset:offset + service.settings.provider_concurrency]
        service.recognize(run_id, {"pages": selected}, worker_id=owner, fence=fence, deadline=deadline)
        processed += len(selected)
    service.store.checkpoint(run_id, owner, fence, {"stage": "recognition", "event": "finished", "attempted_pages": processed,
        "remaining_pages": len(pages) - processed, "scope": "host_coverage_sweep"})
    return {"attempted_pages": processed, "remaining_pages": len(pages) - processed}


def collect_exhaustive_search(service, run_id, owner, fence, *, deadline=None, check):
    """Close the frozen plan's retrieval queries after the evidence index settles."""
    run = service._run(run_id)
    queries = [""]
    for values in (run.get("plan") or {}).get("retrievalQueries", {}).values():
        queries.extend(values)
    check()
    return service.search_exhaustive(run_id, queries, worker_id=owner, fence=fence, deadline=deadline, limit=100)
