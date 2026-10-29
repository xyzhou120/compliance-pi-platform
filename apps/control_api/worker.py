"""Lease-fenced control worker. Pi collects evidence; business code finalizes."""
from __future__ import annotations

import argparse
import http.client
import json
import re
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .callbacks import CallbackPolicy, DeliveryError, HttpCallbackClient, callback_payload
from .documents import acquire_run
from .evaluation import evaluate_run
from .registry import default_registry
from .settings import Settings
from .sources import prepare_source
from .store import Conflict, RunPaused, StaleLease, Store
from .collection import collect_native_files, collect_remaining_pages, collect_exhaustive_search


class LeaseHeartbeat:
    def __init__(self, store, run, worker_id, lease_seconds):
        self.store, self.run, self.worker_id = store, run, worker_id
        self.lease_seconds = lease_seconds
        self.interval = min(5.0, max(0.1, lease_seconds / 3))
        self.stop = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._run, name=f"lease-{run['run_id'][:12]}", daemon=True)

    def _run(self):
        while not self.stop.wait(self.interval):
            try:
                renewed = self.store.renew(self.run["run_id"], self.worker_id, self.run["fence"], self.lease_seconds)
                if renewed.get("control_state") == "paused":
                    raise RunPaused(self.run["run_id"])
                self.store.checkpoint(self.run["run_id"], self.worker_id, self.run["fence"], {"stage": "lease", "event": "heartbeat"})
            except Exception as exc:
                self.error = exc
                return

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=self.interval + 1)

    def check(self):
        if self.error:
            raise self.error


def normalize_run(run: dict) -> dict:
    spec, req, plan = run.get("spec", {}), run.get("request", {}), run.get("plan", {})
    return {**run, "run_id": run.get("run_id", run.get("runId")),
            "mine_id": req.get("mineId", run.get("mine_id", spec.get("mine_id"))),
            "check_cycle": req.get("checkCycle", run.get("check_cycle", spec.get("check_cycle"))),
            "item_code": run.get("item_code", run.get("itemCode", spec.get("item_code"))),
            "environment": req.get("environment", run.get("environment", spec.get("environment", "local"))),
            "source_system": req.get("sourceSystem", spec.get("source_system", "upstream")),
            "rule_revision": plan.get("planRevision", plan.get("revision", run.get("rule_revision"))),
            "scope": spec.get("scope") or run.get("scope") or {}}


def resolve_manifest(run: dict, settings: Settings) -> dict:
    """Resolve operator-owned source routing; never accepts an upstream path."""
    return prepare_source(run, settings)


def working_snapshot(store, run: dict) -> dict:
    from .evidence import EvidenceService
    fresh = normalize_run(store.get_run(run["run_id"]))
    return EvidenceService(store).snapshot(fresh)


def _run_pi(run: dict, settings: Settings, owner: str, fence: int, *, check=lambda: None) -> dict:
    payload = {"run_id": run["run_id"], "rule_revision": run["rule_revision"],
               "manifest_hash": run["snapshot"]["manifest_hash"], "plan": run.get("plan", {}),
               "work_owner": owner, "work_fence": fence, "max_tool_calls": settings.max_model_calls}
    payload["budgets"] = run.get("plan", {}).get("executionPlan", {}).get("budgets", run.get("plan", {}).get("budgets", {}))
    parsed = urlsplit(settings.pi_worker_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("invalid_pi_worker_url")
    cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = cls(parsed.hostname, parsed.port, timeout=settings.provider_timeout)
    done, output = threading.Event(), {}

    def request():
        try:
            connection.request("POST", parsed.path.rstrip("/") + "/v1/inspect",
                               json.dumps(payload, ensure_ascii=False).encode(),
                               {"Content-Type": "application/json", "X-Internal-Token": settings.internal_token})
            # The response spans the entire Pi session; only connection/send
            # failures use a timeout. Model requests have their own local guard.
            if connection.sock:
                connection.sock.settimeout(None)
            response = connection.getresponse()
            body = response.read(16 * 1024 * 1024 + 1)
            if len(body) > 16 * 1024 * 1024 or response.status != 200:
                detail = "http_error"
                try:
                    parsed_body = json.loads(body.decode("utf-8", errors="replace"))
                    if isinstance(parsed_body, dict) and isinstance(parsed_body.get("detail"), str):
                        detail = parsed_body["detail"]
                except (ValueError, UnicodeError):
                    pass
                safe_detail = re.sub(r"[^a-zA-Z0-9_]+", "_", detail).strip("_")[:80] or "http_error"
                raise RuntimeError(f"pi_worker_http_{response.status}_{safe_detail}")
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("invalid_pi_response")
            output["value"] = data
        except Exception as exc:
            output["error"] = exc
        finally:
            done.set()

    thread = threading.Thread(target=request, name="pi-http", daemon=True)
    thread.start()
    try:
        while not done.wait(0.1):
            check()
        check()
        if "error" in output:
            raise output["error"]
        return output["value"]
    finally:
        if connection.sock:
            try:
                connection.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        connection.close()
        thread.join(timeout=1)


def run_once(store: Store, settings: Settings, *, worker_id="control-worker", run_id=None,
             source_loader=None, pi_client=None, evaluator=None, registry=None) -> dict | None:
    claimed = store.claim(worker_id, lease_seconds=settings.lease_seconds, run_id=run_id)
    if claimed is None:
        return None
    run = normalize_run(claimed)
    rid, fence = run["run_id"], run["fence"]
    inspections = registry or default_registry()
    source_loader, evaluator = source_loader or acquire_run, evaluator or inspections.evaluate
    try:
        with LeaseHeartbeat(store, run, worker_id, settings.lease_seconds) as lease:
            inspections.assert_compatible(run)
            if run.get("attempt", 1) > settings.max_work_attempts:
                raise RuntimeError("execution_recovery_attempt_limit")
            if run.get("snapshot") is None:
                source_started = time.perf_counter()
                selected = resolve_manifest(run, settings)
                store.checkpoint(rid, worker_id, fence, {"stage": "source", "event": "started"})
                document_settings = settings.document_settings()
                document_settings["inventory_only"] = settings.pi_enabled or settings.host_collection_sweep
                if selected.get("source_input_root"):
                    document_settings["input_root"] = selected["source_input_root"]
                snapshot = source_loader(selected, document_settings)
                if settings.evidence_review_receipts:
                    receipt_path = settings.evidence_review_receipts.resolve()
                    if settings.input_root.resolve() not in receipt_path.parents or not receipt_path.is_file() or receipt_path.stat().st_size > 8 * 1024 * 1024:
                        raise RuntimeError("evidence_review_receipts_unavailable")
                    snapshot["transcription_review_receipts"] = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
                snapshot.update(run_id=rid, mine_id=run["mine_id"], mine_code=run["mine_id"], source_system=run["source_system"],
                                environment=run["environment"], rule_revision=run["rule_revision"])
                for entry in snapshot.get("evidence", []):
                    entry.update({k: snapshot[k] for k in ("run_id", "mine_id", "mine_code", "source_system", "environment", "rule_revision", "snapshot_id")})
                lease.check()
                store.save_snapshot(rid, worker_id, fence, snapshot)
                store.checkpoint(rid, worker_id, fence, {"stage": "source", "event": "timing", "elapsed_ms": round((time.perf_counter() - source_started) * 1000, 3)})
            run = normalize_run(store.get_run(rid))
            store.checkpoint(rid, worker_id, fence, {"stage": "source", "event": "finished", "manifest_hash": run["snapshot"].get("manifest_hash")})
            if settings.host_collection_sweep:
                sweep_started = time.monotonic()
                from .evidence import EvidenceService
                store_service = EvidenceService(store, settings, registry=inspections)
                store.checkpoint(rid, worker_id, fence, {"stage": "collection", "event": "started"})
                try:
                    collect_native_files(store_service, rid, worker_id, fence,
                                         deadline=None, check=lease.check)
                    run = normalize_run(store.get_run(rid))
                except (StaleLease, RunPaused):
                    raise
                except Exception as exc:
                    store.save_records(rid, worker_id, fence, "gap", [{"reason": "host_collection_failed", "error": type(exc).__name__}])
                store.checkpoint(rid, worker_id, fence, {"stage": "collection", "event": "finished",
                                   "elapsed_ms": round((time.monotonic() - sweep_started) * 1000, 3)})
            if settings.pi_enabled:
                pi_started = time.perf_counter()
                store.checkpoint(rid, worker_id, fence, {"stage": "pi", "event": "started"})
                try:
                    outcome = pi_client(run, settings, worker_id, fence) if pi_client else _run_pi(run, settings, worker_id, fence, check=lease.check)
                    for gap in outcome.get("gaps", []):
                        if isinstance(gap, dict):
                            store.save_records(rid, worker_id, fence, "gap", [gap])
                    store.checkpoint(rid, worker_id, fence, {"stage": "pi", "event": "finished",
                                      "tool_calls": outcome.get("toolCalls"), "model_turns": outcome.get("modelTurns"),
                                      "elapsed_ms": round((time.perf_counter() - pi_started) * 1000, 3),
                                      "usage": outcome.get("usage", {}), "modelAttempts": outcome.get("modelAttempts", []),
                                      "toolAttempts": outcome.get("toolAttempts", []), "modelQueueMs": outcome.get("modelQueueMs")})
                except (StaleLease, RunPaused):
                    raise
                except Exception as exc:
                    detail = re.sub(r"[^a-zA-Z0-9_:-]+", "_", str(exc)).strip("_")[:120]
                    store.save_records(rid, worker_id, fence, "gap", [{"gap_id": f"pi:{fence}", "reason": f"pi_worker_failed:{detail or type(exc).__name__}"}])
                if settings.host_collection_sweep:
                    try:
                        collect_remaining_pages(store_service, rid, worker_id, fence,
                                                deadline=None, check=lease.check)
                    except (StaleLease, RunPaused):
                        raise
                    except Exception as recovery_exc:
                        store.save_records(rid, worker_id, fence, "gap", [{"reason": "host_page_recovery_failed", "error": type(recovery_exc).__name__}])
            else:
                store.save_records(rid, worker_id, fence, "gap", [{"gap_id": "pi:disabled", "reason": "pi_execution_disabled"}])
            if settings.host_collection_sweep:
                # Settle calls left behind by a failed Pi session within their
                # individual operation limits, independent of task age.
                drain_deadline = time.monotonic() + max(settings.provider_timeout, 120) + 5
                while any(unit.get("state") == "running" for unit in store.list_work_units(rid)) and time.monotonic() < drain_deadline:
                    lease.check()
                    time.sleep(0.2)
                closed = store.close_unfinished_units(rid, worker_id, fence)
                if closed:
                    store.checkpoint(rid, worker_id, fence, {"stage": "collection", "event": "finished", "unsettled_units_failed": closed})
            lease.check()
            store.checkpoint(rid, worker_id, fence, {"stage": "evaluation", "event": "started"})
            run = normalize_run(store.get_run(rid))
            evaluation_started = time.perf_counter()
            from .evidence import EvidenceService
            service = EvidenceService(store, settings, registry=inspections)
            service.apply_reviews(rid, worker_id=worker_id, fence=fence)
            run = normalize_run(store.get_run(rid))
            snapshot = service.snapshot(run)
            persisted_ids = {item.get("evidence_id") for item in (run.get("snapshot") or {}).get("evidence", [])}
            reviewed = [item for item in snapshot.get("evidence", []) if item.get("evidence_id") not in persisted_ids]
            if reviewed:
                store.save_evidence(rid, worker_id, fence, reviewed)
                run = normalize_run(store.get_run(rid))
                snapshot = service.snapshot(run)
            if settings.host_collection_sweep:
                try:
                    searched = collect_exhaustive_search(service, rid, worker_id, fence,
                                                         deadline=None, check=lease.check)
                    store.checkpoint(rid, worker_id, fence, {"stage": "search", "event": "finished", **searched})
                except (StaleLease, RunPaused):
                    raise
                except Exception as search_exc:
                    store.save_records(rid, worker_id, fence, "gap", [{"reason": "exhaustive_search_failed", "error": type(search_exc).__name__}])
            audit = service.audit(rid)
            base_gaps = list(snapshot.get("coverage_gaps", []))
            snapshot["source_complete"] = audit["source_complete"] and audit["coverage_complete"]
            snapshot["coverage_complete"] = audit["coverage_complete"]
            snapshot["coverage_audit"] = audit
            snapshot["coverage_gaps"] = list(dict.fromkeys(snapshot.get("coverage_gaps", []) + audit["gaps"]))
            snapshot["verified_facts"] = [fact for candidate in run.get("candidates", []) if candidate.get("verified") for fact in candidate.get("facts", [])]
            result = evaluator(run, snapshot)
            # Conditional materials are activated only by deterministic propositions.
            conditional_audit = service.audit(rid, propositions=result.get("propositions", []), frozen=False)
            if conditional_audit != audit:
                audit = conditional_audit
                snapshot.update(source_complete=audit["source_complete"] and audit["coverage_complete"], coverage_complete=audit["coverage_complete"], coverage_audit=audit)
                snapshot["coverage_gaps"] = list(dict.fromkeys(base_gaps + audit["gaps"]))
                result = evaluator(run, snapshot)
            audit.update(run_id=rid, manifest_hash=snapshot.get("manifest_hash"), rule_revision=run["rule_revision"])
            audit["audit_id"] = store.digest(audit)
            store.save_records(rid, worker_id, fence, "coverage_audit", [audit])
            result["coverage_audit"] = audit
            store.checkpoint(rid, worker_id, fence, {"stage": "evaluation", "event": "finished", "elapsed_ms": round((time.perf_counter() - evaluation_started) * 1000, 3)})
            result.update(run_id=rid, item_code=run["item_code"], finalized_at=datetime.now(timezone.utc).isoformat())
            callback_url = run.get("request", {}).get("resultCallbackUrl")
            payload = callback_payload(run, result) if callback_url else None
            store.finalize(rid, worker_id, fence, result, delivery_payload=payload, callback_url=callback_url)
            return result
    except RunPaused:
        store.pause_run(rid, worker_id, fence)
        return {"run_id": rid, "execution_state": "paused"}
    except StaleLease:
        raise
    except Exception as exc:
        no_files = str(exc) == "no_source_files_for_inspection"
        unavailable = str(exc) == "source_attachments_unavailable"
        result = {"run_id": rid, "item_code": run["item_code"], "status": "failed", "description": "没有可推送检查的资料文件" if no_files else "资料附件下载失败" if unavailable else "检查执行技术失败",
                  "detail": "OceanBase清单查询没有返回匹配的资料文件" if no_files else "OceanBase存在目录记录，但没有成功下载的附件" if unavailable else type(exc).__name__,
                  "coverage_gaps": ["no_source_files_for_inspection" if no_files else "source_attachments_unavailable" if unavailable else "technical_execution_failed"],
                  "finalized_at": datetime.now(timezone.utc).isoformat()}
        callback_url = run.get("request", {}).get("resultCallbackUrl")
        store.finalize(rid, worker_id, fence, result,
                       delivery_payload=callback_payload(run, result) if callback_url else None, callback_url=callback_url)
        return result


def deliver_once(store: Store, settings: Settings, *, worker_id="delivery-worker", receiver=None) -> dict | None:
    delivery = store.claim_delivery(worker_id, lease_seconds=max(settings.lease_seconds, settings.callback_timeout + 10))
    if not delivery:
        return None
    try:
        if receiver:
            receiver(delivery)
        else:
            policy = CallbackPolicy(settings.callback_hosts, allow_http=settings.callback_allow_http,
                                    allow_private=settings.callback_allow_private, allow_loopback=settings.callback_allow_loopback)
            HttpCallbackClient(policy, timeout=settings.callback_timeout, authorization=settings.callback_auth).send(
                delivery["callback_url"], delivery["payload"], delivery["delivery_id"])
        store.complete_delivery(delivery["delivery_id"], worker_id, delivery["fence"])
    except DeliveryError as exc:
        store.complete_delivery(delivery["delivery_id"], worker_id, delivery["fence"], error=str(exc),
                                retry=exc.retryable and delivery["attempt"] < settings.callback_max_attempts)
    return delivery


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env()
    store = Store(database_url=settings.database_url)
    while True:
        result = run_once(store, settings)
        delivered = deliver_once(store, settings)
        if args.once:
            return
        if result is None and delivered is None:
            time.sleep(1)


if __name__ == "__main__":
    main()
