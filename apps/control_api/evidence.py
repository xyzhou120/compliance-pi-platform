"""Frozen-run evidence navigation and lease-fenced Pi tool mutations."""
from __future__ import annotations

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import time
import threading
import json
from pathlib import Path
from uuid import uuid4
from pydantic import ValidationError

from packages.contracts.models import CandidateEnvelope
from .documents import get_coverage, provider_evidence, render_pages, search_evidence, parse_frozen_file
from .coverage import audit_coverage
from .providers import ProviderPool, recognize_pages
from .settings import Settings
from .store import NotFound, Conflict
from .validator import CandidateRejected, validate_candidate


def run_context(run: dict) -> dict:
    spec, request, plan = run.get("spec", {}), run.get("request", {}), run.get("plan", {})
    return {**run, "run_id": run.get("run_id", run.get("runId")),
            "mine_id": request.get("mineId", spec.get("mine_id", run.get("mine_id"))),
            "mine_code": request.get("mineId", spec.get("mine_code", run.get("mine_id"))),
            "item_code": run.get("item_code", run.get("itemCode")),
            "check_cycle": request.get("checkCycle", spec.get("check_cycle", run.get("check_cycle"))),
            "environment": request.get("environment", spec.get("environment", "local")),
            "source_system": request.get("sourceSystem", spec.get("source_system", "upstream")),
            "rule_revision": plan.get("planRevision", plan.get("revision", run.get("rule_revision"))),
            "scope": spec.get("scope", {})}


class EvidenceService:
    def __init__(self, store, settings: Settings | None = None, registry=None):
        from .registry import default_registry
        self.store, self.settings = store, settings or Settings()
        self.inspections = registry or default_registry()
        self.provider_pool = ProviderPool(max_concurrency=self.settings.provider_concurrency)
        self.mutation_lock = threading.RLock()

    def _cache_root(self, snapshot):
        allowed = self.settings.storage_dir.resolve()
        root = Path(snapshot.get("storage_root") or allowed).resolve()
        if root != allowed and allowed not in root.parents:
            raise ValueError("run cache outside storage root")
        return root

    def _run(self, run_id: str) -> dict:
        return run_context(self.store.get_run(run_id))

    @staticmethod
    def _public_record(record: dict) -> dict:
        return {key: record.get(key) for key in ("evidence_id", "file_id", "document_id", "document_version", "content_hash", "locator", "source_role", "material_group", "text", "quote", "original_name", "quality", "unit_id", "source_file_ids")}

    def snapshot(self, run: dict) -> dict:
        result = deepcopy(run.get("snapshot") or {})
        records = {item["evidence_id"]: item for item in result.get("evidence", [])}
        records.update({item["evidence_id"]: item for item in run.get("evidence", [])})
        result["evidence"] = list(records.values())
        result["candidates"] = run.get("candidates", [])
        parsed_files = set()
        coverage = {(item.get("file_id"), item.get("unit_id")): item for item in result.get("coverage", [])}
        parsed_gaps = []
        for unit in run.get("work_units", []):
            if unit.get("kind") == "parse_file" and unit.get("state") == "succeeded":
                output = unit.get("output") or {}
                fid = output.get("file_id")
                parsed_files.add(fid)
                coverage = {key: value for key, value in coverage.items() if key[0] != fid}
                coverage.update({(item.get("file_id", fid), item.get("unit_id")): {**item, "file_id": item.get("file_id", fid)} for item in output.get("coverage", [])})
                parsed_gaps.extend(output.get("coverage_gaps", []))
        result["coverage"] = list(coverage.values())
        result["coverage_gaps"] = [gap for gap in result.get("coverage_gaps", []) if not any(str(gap).startswith(str(fid) + "/") for fid in parsed_files)] + parsed_gaps
        for unit in run.get("work_units", []):
            if unit.get("state") == "failed":
                data = unit.get("input", {})
                fid, page = data.get("file_id"), data.get("page")
                result["coverage"] = [{**row, "state": "provider_failed"} if row.get("file_id") == fid and (page is None or row.get("unit_id") == f"page:{page}") else row for row in result["coverage"]]
            if unit.get("kind") != "review_transcription" or unit.get("state") != "succeeded":
                continue
            reviewed = unit.get("output") or {}
            updated = {(row["file_id"], row["unit_id"]): row for row in reviewed.get("coverage", [])}
            result["coverage"] = [{**row, **updated.get((row["file_id"], row["unit_id"]), {})} for row in result["coverage"]]
            closed = {f"{fid}/{uid}:" for (fid, uid), row in updated.items() if row.get("state") == "readable"}
            result["coverage_gaps"] = [gap for gap in result["coverage_gaps"] if not any(str(gap).startswith(prefix) for prefix in closed)] + reviewed.get("gaps", [])
        result.setdefault("coverage_gaps", []).extend(str(gap.get("reason", "unresolved_gap")) for gap in run.get("gaps", []))
        return result

    def plan(self, run_id: str) -> dict:
        run = self._run(run_id)
        return {"run_id": run_id, "rule_revision": run["rule_revision"], "manifest_hash": (run.get("snapshot") or {}).get("manifest_hash"), "plan": run["plan"],
                "capabilities": ["tool_trace", "parse_file", "inspect_documents", "coverage_audit", "version_comparison", "shared_model_permits"]}

    def search(self, run_id: str, query: str = "", *, file_id: str | None = None, offset: int = 0, limit: int = 20, worker_id=None, fence=None) -> dict:
        run = self._run(run_id)
        snapshot = self.snapshot(run)
        result = search_evidence(snapshot, query, file_id=file_id, offset=offset, limit=limit)
        if worker_id and fence is not None:
            # A receipt covers only an exhaustively returned query page range.
            selected = [unit for unit in snapshot.get("coverage", []) if (not file_id or unit.get("file_id") == file_id)]
            receipt = {"query_hash": self.store.digest(query), "file_id": file_id, "offset": offset, "limit": limit,
                       "total": result["total"], "hit_count": result["total"], "count": len(result["items"]),
                       "index_revision": self.store.digest(sorted(item["evidence_id"] for item in snapshot["evidence"])), "complete": False,
                       "units": [{"file_id": unit["file_id"], "unit_id": unit["unit_id"]} for unit in selected]}
            pages = [item for item in run.get("searches", []) if all(item.get(key) == receipt[key] for key in ("query_hash", "file_id", "index_revision", "total"))] + [receipt]
            waterline = 0
            for page in sorted(pages, key=lambda item: item["offset"]):
                if page["offset"] <= waterline:
                    waterline = max(waterline, page["offset"] + page.get("count", 0))
            receipt["complete"] = waterline >= receipt["total"]
            receipt["search_id"] = self.store.digest(receipt)
            self.store.save_records(run_id, worker_id, fence, "search", [receipt])
        result["unindexed_files"] = sorted({unit["file_id"] for unit in snapshot.get("coverage", []) if unit.get("state") == "not_indexed"})
        return result

    def _unit(self, run_id, worker_id, fence, kind, inputs, operation):
        run = self._run(run_id)
        inputs = {**inputs, "rule_revision": run["rule_revision"]}
        with self.mutation_lock:
            budget = run.get("plan", {}).get("budgets", {}).get("maxPages", self.settings.max_total_pages)
            unit = self.store.enqueue_work_unit(run_id, worker_id, fence, kind, inputs, page_limit=min(budget, self.settings.max_total_pages))
            if unit["state"] == "succeeded":
                self.store.checkpoint(run_id, worker_id, fence, {"stage": "work_unit", "event": "reused", "work_unit_id": unit["work_unit_id"]})
                return {**unit["output"], "work_unit_id": unit["work_unit_id"], "reused": True}
            claimed = self.store.claim_work_unit(run_id, worker_id, fence, unit["work_unit_id"], max_attempts=self.settings.max_work_attempts)
        if claimed is None:
            # Another identical parallel tool call owns this unit; wait for its durable result.
            deadline = time.monotonic() + self.settings.provider_timeout
            while time.monotonic() < deadline:
                self.store.checkpoint(run_id, worker_id, fence)
                latest = next(item for item in self.store.list_work_units(run_id) if item["work_unit_id"] == unit["work_unit_id"])
                if latest["state"] == "succeeded":
                    return {**latest["output"], "work_unit_id": unit["work_unit_id"], "reused": True}
                if latest["state"] == "failed":
                    raise Conflict("work_unit_failed:" + str(latest.get("error")))
                time.sleep(.05)
            raise Conflict("work_unit_busy")
        try:
            permit = None
            if kind == "parse_file":
                while permit is None:
                    permit = self.store.acquire_permit(run_id, worker_id, fence, "document", unit["work_unit_id"],
                        limit=self.settings.max_document_concurrency, ttl=self.settings.task_timeout + 30)
                    if permit is None:
                        time.sleep(.05)
            try:
                result = operation()
            finally:
                if permit:
                    self.store.release_permit(run_id, worker_id, fence, "document", permit_id=permit)
            with self.mutation_lock:
                if result.get("evidence"):
                    self.store.save_evidence(run_id, worker_id, fence, result["evidence"])
                self.store.finish_work_unit(run_id, worker_id, fence, unit["work_unit_id"], output=result, error="provider_failed" if result.get("unit_failed") else None)
            return {**result, "work_unit_id": unit["work_unit_id"], "reused": False}
        except Exception as exc:
            self.store.finish_work_unit(run_id, worker_id, fence, unit["work_unit_id"], error=type(exc).__name__)
            raise

    def parse(self, run_id, file_id, *, worker_id, fence):
        run = self._run(run_id)
        snapshot = self.snapshot(run)
        file = next((item for item in snapshot.get("files", []) if item["file_id"] == file_id), None)
        if file is None:
            raise NotFound("file outside frozen run")
        budget = run.get("plan", {}).get("budgets", {}).get("maxPages", self.settings.max_total_pages)
        processed = {unit.get("input", {}).get("file_id") for unit in run.get("work_units", []) if unit.get("kind") == "parse_file"}
        total_pages = sum(int(item.get("page_count") or 1) for item in snapshot.get("files", []) if item["file_id"] in processed | {file_id})
        if total_pages > min(budget, self.settings.max_total_pages):
            raise ValueError("page_budget_exhausted")
        return self._unit(run_id, worker_id, fence, "parse_file", {"file_id": file_id, "content_hash": file["content_hash"], "page_count": int(file.get("page_count") or 1), "parser_revision": snapshot.get("parser_revision")},
                          lambda: parse_frozen_file(snapshot, file_id, {**self.settings.document_settings(), "storage_dir": str(self._cache_root(snapshot))}))

    def inspect_documents(self, run_id, file_ids, *, worker_id, fence):
        if not isinstance(file_ids, list) or not 1 <= len(file_ids) <= 32 or any(not isinstance(fid, str) for fid in file_ids):
            raise ValueError("file_ids must contain 1 to 32 file identifiers")
        with ThreadPoolExecutor(max_workers=self.settings.max_document_concurrency) as pool:
            outputs = list(pool.map(lambda fid: self.parse(run_id, fid, worker_id=worker_id, fence=fence), list(dict.fromkeys(file_ids))))
        return {"items": outputs, "total": len(outputs)}

    def audit(self, run_id, *, propositions=(), frozen=True):
        run = self._run(run_id)
        if frozen and (run.get("result") or {}).get("coverage_audit"):
            return deepcopy(run["result"]["coverage_audit"])
        snapshot = self.snapshot(run)
        return audit_coverage(run, snapshot, run.get("plan", {}), searches=run.get("searches", []),
                              proof=snapshot.get("scope_proof"), proof_key=self.settings.scope_proof_key, propositions=propositions)

    def apply_reviews(self, run_id, *, worker_id, fence):
        from .quality import apply_transcription_reviews
        run = self._run(run_id)
        snapshot = self.snapshot(run)
        receipts = snapshot.get("transcription_review_receipts")
        if not receipts:
            return None
        def apply():
            reviewed = apply_transcription_reviews(snapshot, receipts, self.settings.evidence_review_key)
            return {"evidence": reviewed["evidence"], "coverage": reviewed["coverage_updates"], "gaps": reviewed["gaps"]}
        return self._unit(run_id, worker_id, fence, "review_transcription", {"receipt_hash": self.store.digest(receipts), "policy": "signed-independent-v1"}, apply)

    def preview_rule(self, run_id, registry):
        from .navigation import compare_rule_plans
        run = self._run(run_id)
        current = registry.compile(run["item_code"])
        snapshot = self.snapshot(run)
        audit = self.audit(run_id)
        snapshot.update(source_complete=audit["source_complete"] and audit["coverage_complete"], coverage_complete=audit["coverage_complete"], coverage_audit=audit)
        snapshot["coverage_gaps"] = list(dict.fromkeys(snapshot.get("coverage_gaps", []) + audit["gaps"]))
        preview_run = {**run, "plan": current, "rule_revision": current["planRevision"]}
        result = registry.evaluate(preview_run, snapshot)
        return {"run_id": run_id, "preview_only": True, "history_modified": False, "callback_created": False,
                "plan_comparison": compare_rule_plans(run["plan"], current), "result": result}

    def record_trace(self, run_id, body, *, worker_id, fence):
        allowed = {"trace_id", "tool", "phase", "status", "args_hash", "elapsed_ms", "error_code"}
        if not isinstance(body, dict) or set(body) - allowed or not body.get("trace_id") or body.get("phase") not in {"started", "finished"}:
            raise ValueError("invalid_tool_trace")
        self.store.save_records(run_id, worker_id, fence, "trace", [body])
        return {"saved": True}

    def submit_gaps(self, run_id, gaps, *, worker_id, fence):
        if not isinstance(gaps, list) or len(gaps) > 200:
            raise ValueError("invalid_gap_batch")
        records = []
        for gap in gaps:
            if not isinstance(gap, dict) or set(gap) - {"reason", "proposition_id", "file_id", "unit_id"} or not isinstance(gap.get("reason"), str):
                raise ValueError("invalid_gap")
            records.append({**gap, "gap_id": self.store.digest(gap)})
        return {"saved": self.store.save_records(run_id, worker_id, fence, "gap", records)}

    def expand_event(self, run_id, event_id):
        from .navigation import expand_event_context
        run = self._run(run_id)
        return expand_event_context(run, self.snapshot(run), event_id)

    def compare_versions(self, run_id, file_ids):
        from .navigation import compare_versions
        return compare_versions(self.snapshot(self._run(run_id)), file_ids)

    def answer(self, run_id, question, proposition_id=None):
        from .navigation import answer_question
        run = self._run(run_id)
        return answer_question(run, self.snapshot(run), question, proposition_id=proposition_id)

    def compare(self, before_id, after_id):
        from .navigation import compare_runs
        before, after = self._run(before_id), self._run(after_id)
        return compare_runs({**before, "snapshot": self.snapshot(before)}, {**after, "snapshot": self.snapshot(after)})

    def search_public(self, run_id: str, query: str = "", *, file_id: str | None = None, offset: int = 0, limit: int = 20) -> dict:
        snapshot = self.snapshot(self._run(run_id))
        files = {item["file_id"]: item for item in snapshot.get("files", [])}
        result = search_evidence(snapshot, query, file_id=file_id, offset=offset, limit=limit)
        bounded = []
        for item in result.get("items", []):
            text = str(item.get("text") or item.get("quote") or "")
            source = files.get(item.get("file_id"), {})
            bounded.append({**{key: item.get(key) for key in ("evidence_id", "file_id", "document_id", "document_version", "content_hash", "locator", "source_role", "material_group", "score")},
                            "original_name": source.get("original_name", item.get("file_id")), "quote": text[:1200], "text": text[:1200],
                            "truncated": len(text) > 1200, "full_text_available": True,
                            "quality": item.get("quality", item.get("metadata", {}).get("recognition_quality", "native"))})
        result["items"] = bounded
        return result

    def get(self, run_id: str, evidence_id: str) -> dict:
        snapshot = self.snapshot(self._run(run_id))
        for record in snapshot["evidence"]:
            if record["evidence_id"] == evidence_id:
                file = next((item for item in snapshot.get("files", []) if item["file_id"] == record.get("file_id")), {})
                return {**record, "original_name": file.get("original_name", record.get("file_id"))}
        raise NotFound("evidence outside frozen run")

    def get_public(self, run_id: str, evidence_id: str) -> dict:
        return self._public_record(self.get(run_id, evidence_id))

    def page(self, run_id: str, file_id: str, page: int) -> dict:
        snapshot = self.snapshot(self._run(run_id))
        if page < 1 or not any(file["file_id"] == file_id for file in snapshot.get("files", [])):
            raise NotFound("file/page outside frozen run")
        evidence = [record for record in snapshot["evidence"] if record.get("file_id") == file_id and record.get("locator", {}).get("page") == page]
        coverage = [record for record in snapshot.get("coverage", []) if record.get("file_id") == file_id and record.get("unit_id") == f"page:{page}"]
        if not evidence and not coverage:
            raise NotFound("page outside frozen run")
        return {"file_id": file_id, "page": page, "evidence": evidence, "coverage": coverage}

    def coverage(self, run_id: str) -> dict:
        run = self._run(run_id)
        snapshot = self.snapshot(run)
        return {**get_coverage(snapshot), **self.audit(run_id),
                "files": [{key: file.get(key) for key in ("file_id", "original_name", "format", "source_role", "material_group", "content_hash")} for file in snapshot.get("files", [])],
                "scope": "frozen_run_only", "rule_revision": run["rule_revision"]}

    def context(self, run_id: str) -> dict:
        run = self._run(run_id)
        return {"run_id": run_id, "mine_id": run["mine_id"], "item_code": run["item_code"], "state": run.get("state"), "plan": run.get("plan", {}), "coverage": self.coverage(run_id)}

    def recognize(self, run_id: str, body: dict, *, worker_id: str, fence: int) -> dict:
        run = self._run(run_id)
        self.store.checkpoint(run_id, worker_id, fence, {"stage": "recognition", "event": "requested"})
        pages = body.get("pages") if isinstance(body, dict) else None
        if not isinstance(pages, list) or not 1 <= len(pages) <= 32:
            raise ValueError("pages must be a list of 1 to 32 items")
        if any(not isinstance(page, dict) or not isinstance(page.get("file_id"), str) or type(page.get("page")) is not int or page["page"] < 1 for page in pages):
            raise ValueError("invalid page selection")
        snapshot = self.snapshot(run)
        provider_settings = self.settings.provider_settings()
        if not provider_settings.get("endpoint"):
            gap = {"reason": "provider_endpoint_missing", "pages": pages}
            self.store.save_records(run_id, worker_id, fence, "gap", [gap])
            return {"evidence": [], "attempts": [], "gaps": [gap]}
        def cancelled():
            try:
                self.store.checkpoint(run_id, worker_id, fence)
                return False
            except Exception:
                return True
        service = self
        class LedgerPool:
            max_concurrency = service.settings.provider_concurrency
            def call(self, *args, **kwargs):
                request_id = uuid4().hex
                queued = time.monotonic()
                permit = None
                while permit is None:
                    if cancelled():
                        raise RuntimeError("provider_cancelled")
                    permit = service.store.acquire_permit(run_id, worker_id, fence, "ocr", request_id,
                        limit=service.settings.provider_concurrency, ttl=service.settings.task_timeout + 30)
                    if permit is None:
                        time.sleep(.05)
                try:
                    value = service.provider_pool.call(*args, **kwargs)
                    value["queue_ms"] = value.get("queue_ms", 0) + round((time.monotonic() - queued) * 1000, 2) - value.get("elapsed_ms", 0)
                    return value
                finally:
                    service.store.release_permit(run_id, worker_id, fence, "ocr", permit_id=permit)
        def one(page):
            file = next((item for item in snapshot.get("files", []) if item["file_id"] == page["file_id"]), None)
            if file is None:
                raise NotFound("file outside frozen run")
            def perform():
                rendered = render_pages(snapshot, [page], max_pages=1)
                result = recognize_pages(rendered["images"], settings={**provider_settings, "cache_dir": str(self._cache_root(snapshot) / "recognition"), "cancelled": cancelled}, pool=LedgerPool())
                records = provider_evidence(snapshot, result["candidates"])
                for record in records:
                    record.update(mine_code=run["mine_id"], source_system=run["source_system"], snapshot_id=snapshot["snapshot_id"])
                gaps = rendered["errors"] + result["errors"]
                self.store.checkpoint(run_id, worker_id, fence, {"stage": "recognition", "event": "finished", "attempts": result["attempts"], "evidence_count": len(records)})
                return {"evidence": records, "attempts": result["attempts"], "gaps": gaps, "unit_failed": bool(gaps and not records)}
            profile = {key: value for key, value in provider_settings.items() if key not in {"authorization_env", "cancelled"}}
            return self._unit(run_id, worker_id, fence, "recognize_page", {**page, "content_hash": file["content_hash"], "profile_revision": self.store.digest(profile)}, perform)
        with ThreadPoolExecutor(max_workers=self.settings.provider_concurrency) as pool:
            outputs = list(pool.map(one, pages))
        return {"evidence": [item for output in outputs for item in output["evidence"]],
                "attempts": [item for output in outputs for item in output["attempts"]],
                "gaps": [item for output in outputs for item in output["gaps"]],
                "quality": "model_candidate", "source_complete": False}

    def submit(self, run_id: str, candidates: list[dict], *, worker_id: str, fence: int) -> dict:
        run = self._run(run_id)
        snapshot = self.snapshot(run)
        self.store.checkpoint(run_id, worker_id, fence, {"stage": "candidates", "event": "submitted"})
        if not isinstance(candidates, list) or len(candidates) > 200:
            raise ValueError("candidates must be a list of at most 200 objects")
        registry = {item["evidence_id"]: item for item in snapshot["evidence"]}
        versions, hashes = {item.get("document_version") for item in registry.values()}, {item.get("content_hash") for item in registry.values()}
        accepted, rejected = [], []
        for original in candidates:
            try:
                if not isinstance(original, dict) or any(key in original for key in ("verified", "verdict", "source_complete", "findings")):
                    raise CandidateRejected("model_claimed_verdict_or_verification")
                raw = deepcopy(original)
                if raw.get("run_id") != run_id or raw.get("rule_revision") != run["rule_revision"]:
                    raise CandidateRejected("candidate_owner_or_rule_mismatch")
                if raw.get("status", "candidate") != "candidate":
                    raise CandidateRejected("candidate_status_must_be_candidate")
                refs = []
                for reference in raw.get("evidence_refs", []):
                    ref_id = reference if isinstance(reference, str) else reference.get("evidence_id")
                    source = registry.get(ref_id)
                    if source is None:
                        raise CandidateRejected("unknown_evidence_id")
                    refs.append({key: source[key] for key in ("evidence_id", "document_id", "document_version", "content_hash", "locator", "source_role")} | {"quote": source.get("text", source.get("quote", ""))})
                raw["evidence_refs"] = refs
                candidate = CandidateEnvelope.model_validate(raw)
                bound = validate_candidate(candidate, evidence_by_id=registry, allowed_documents=versions, allowed_hashes=hashes, run=run,
                                           fact_contract=(lambda contract: contract.get("kinds", {}).get(candidate.kind, contract))(run.get("plan", {}).get("registry", {}).get("factContract", {})))
                if run.get("plan", {}).get("registry"):
                    bound = self.inspections.verify_candidate(run, bound, snapshot)
                bound.update(candidate_id=candidate.candidate_id, run_id=run_id, rule_revision=run["rule_revision"])
                accepted.append(bound)
            except (CandidateRejected, ValidationError, ValueError, KeyError, TypeError) as exc:
                rejected.append({"candidate_id": original.get("candidate_id") if isinstance(original, dict) else None, "reason": type(exc).__name__ + ":candidate_rejected"})
        if accepted:
            self.store.save_candidates(run_id, worker_id, fence, accepted)
        return {"run_id": run_id, "accepted": len(accepted), "rejected": rejected, "validation": [{"candidate_id": item["candidate_id"], "state": item["validation_status"], "reasons": item["reasons"]} for item in accepted]}

    def navigate(self, run_id: str, *, query: str = "", proposition_id: str | None = None, event_id: str | None = None) -> dict:
        run = self._run(run_id)
        result = run.get("result") or {}
        propositions = [p for p in result.get("propositions", []) if (event_id is None or p.get("event_id") == event_id) and (proposition_id is None or p.get("code") == proposition_id)]
        refs = {ref for prop in propositions for ref in prop.get("evidence_refs", [])}
        registry = {record["evidence_id"]: record for record in self.snapshot(run)["evidence"]}
        files = {item["file_id"]: item for item in (run.get("snapshot") or {}).get("files", [])}
        return {"run_id": run_id, "rule_revision": run["rule_revision"], "propositions": propositions, "association_links": result.get("association_links", []),
                "evidence": [{**self._public_record(registry[ref]), "original_name": files.get(registry[ref].get("file_id"), {}).get("original_name", registry[ref].get("file_id")), "text": str(registry[ref].get("text", ""))[:1200], "quote": str(registry[ref].get("quote", registry[ref].get("text", "")))[:1200], "truncated": len(str(registry[ref].get("text", ""))) > 1200} for ref in sorted(refs) if ref in registry], "search": self.search_public(run_id, query) if query else None,
                "candidates": [{"candidate_id": item.get("candidate_id"), "validation_status": item.get("validation_status"), "reasons": item.get("reasons", [])} for item in run.get("candidates", [])],
                "gaps": result.get("coverage_gaps", []), "status": result.get("status", "running")}
