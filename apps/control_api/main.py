"""Task and Evidence API. Importing this module never opens a business database."""
from __future__ import annotations

import hmac
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from packages.contracts.models import InspectionTask
from .callbacks import CallbackPolicy, DeliveryError
from .registry import default_registry
from .settings import Settings
from .store import Conflict, NotFound, RunPaused, StaleLease, Store

ApiSettings = Settings


def create_app(*, store: Store | None = None, settings: Settings | None = None, evidence_service: Any | None = None, registry=None) -> FastAPI:
    inspections = registry or default_registry()
    @asynccontextmanager
    async def lifespan(app):
        dependencies()
        yield

    app = FastAPI(title="compliance-pi-platform", version="0.1.0", lifespan=lifespan)
    frontend_root = Path(__file__).parents[2] / "frontend"
    if frontend_root.is_dir():
        app.mount("/frontend", StaticFiles(directory=str(frontend_root)), name="frontend")
    app.state.store = store
    app.state.settings = settings
    app.state.evidence_service = evidence_service

    def dependencies():
        if app.state.settings is None:
            app.state.settings = Settings.from_env()
        config = app.state.settings
        if app.state.store is None:
            app.state.store = Store(database_url=config.database_url)
        if app.state.evidence_service is None:
            from .evidence import EvidenceService
            app.state.evidence_service = EvidenceService(app.state.store, config, registry=inspections)
        return app.state.store, config, app.state.evidence_service

    def require_internal_auth(x_internal_token: str | None = Header(default=None)):
        _, config, _ = dependencies()
        if not config.internal_token or not hmac.compare_digest(x_internal_token or "", config.internal_token):
            raise HTTPException(401, "invalid internal token")

    def allowed(mine: str, requested: str | None = None):
        if requested is not None and requested != mine:
            raise HTTPException(404, "task not found")

    def run_owned(run_id: str, requested_mine: str | None = None):
        ledger, _, _ = dependencies()
        try:
            run = ledger.get_run(run_id)
        except NotFound as exc:
            raise HTTPException(404, "run not found") from exc
        allowed(run["request"]["mineId"], requested_mine)
        return run

    def summary_owned(run_id: str, requested_mine: str | None = None):
        summary = dependencies()[0].run_summary(run_id)
        allowed(summary["mineId"], requested_mine)
        return summary

    def frozen_file(run: dict, file_id: str):
        storage = Path(dependencies()[1].storage_dir).resolve()
        file = next((item for item in (run.get("snapshot") or {}).get("files", []) if item.get("file_id") == file_id), None)
        if not file or not file.get("path"):
            raise HTTPException(404, "file not found")
        path = Path(file["path"]).resolve()
        if storage not in path.parents or not path.is_file() or path.stat().st_size > 50 * 1024 * 1024:
            raise HTTPException(404, "file not available")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != file.get("content_hash"):
            raise HTTPException(409, "frozen file checksum mismatch")
        return file, path

    def work(run_id: str, owner: str | None, fence: int | None):
        if not owner or fence is None:
            raise HTTPException(400, "X-Work-Owner and X-Work-Fence are required")
        ledger, _, _ = dependencies()
        try:
            ledger.checkpoint(run_id, owner, fence, {"event": "tool_request"})
        except (StaleLease, RunPaused) as exc:
            raise HTTPException(409, "work lease is stale or paused") from exc
        return owner, fence

    @app.exception_handler(NotFound)
    async def not_found(_request, _exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=404, content={"detail": "record not found"})

    @app.exception_handler(Conflict)
    async def conflict(_request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/health")
    def health():
        ledger, config, _ = dependencies()
        with ledger.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "service": "control-api", "environment": config.environment,
                "ledger": "available", "external_services": "not_checked", "production_validated": False}

    @app.get("/", include_in_schema=False)
    @app.get("/workbench", include_in_schema=False)
    def workbench():
        if not (frontend_root / "index.html").is_file():
            raise HTTPException(404, "frontend is not installed")
        return FileResponse(frontend_root / "index.html", media_type="text/html; charset=utf-8")

    @app.get("/v1/runtime")
    def runtime_info():
        ledger, config, _ = dependencies()
        return {"environment": config.environment, "pi_enabled": config.pi_enabled,
                "ledger": ledger.engine.dialect.name, "source_configured": bool(config.source_config),
                "supported_items": [{"itemCode": code, "name": inspections.compile(code).get("name", code), "status": "development"} for code in inspections.supported_items()]}

    @app.get("/v1/rules/stop_work_unload/plan")
    def rule_plan():
        return {"frozen": False, "plan": inspections.compile("stop_work_unload")}

    @app.get("/v1/rules/{item_code}/plan")
    def registered_rule_plan(item_code: str):
        if item_code not in inspections.supported_items():
            raise HTTPException(404, "inspection item is not registered")
        return {"frozen": False, "plan": inspections.compile(item_code)}

    @app.post("/v1/rules/{item_code}/replay")
    def replay_rule(item_code: str, body: dict):
        run_ids = body.get("run_ids")
        if item_code not in inspections.supported_items() or not isinstance(run_ids, list) or not 1 <= len(run_ids) <= 20:
            raise HTTPException(400, "replay needs a registered item and 1 to 20 frozen runs")
        outputs = []
        for run_id in run_ids:
            summary = summary_owned(run_id)
            if summary["itemCode"] != item_code:
                raise HTTPException(400, "run inspection item differs")
            outputs.append(dependencies()[2].preview_rule(run_id, inspections))
        return {"items": outputs, "preview_only": True, "history_modified": False}

    @app.post("/inspection/tasks")
    def accept_task(task: InspectionTask, response: Response):
        ledger, config, _ = dependencies()
        allowed(task.mine_id)
        if task.scope is not None:
            raise HTTPException(400, "source scope is server-configured; client scope is not accepted")
        plans = {}
        for item in task.items:
            code = item["itemCode"]
            if code not in inspections.supported_items():
                raise HTTPException(400, f"unsupported itemCode: {code}")
            plans[code] = inspections.compile(code)
        payload = task.model_dump(by_alias=True, exclude_none=True)
        payload["environment"] = config.environment
        payload["sourceSystem"] = "upstream"
        if task.callback_url:
            try:
                CallbackPolicy(config.callback_hosts, allow_http=config.callback_allow_http,
                               allow_private=config.callback_allow_private, allow_loopback=config.callback_allow_loopback).validate(task.callback_url)
            except DeliveryError as exc:
                raise HTTPException(400, str(exc)) from exc
        accepted = ledger.accept(payload, plans)
        if task.status in {"pause", "resume"}:
            accepted = ledger.control(accepted["taskId"], task.status)
        response.headers["X-Inspection-Task"] = accepted["taskId"]
        return {"code": 200, "success": True, "msg": "任务控制请求已生效"}

    @app.get("/inspection/tasks/{task_id}")
    def get_task(task_id: str, mine_id: str | None = None):
        ledger, _, _ = dependencies()
        task = ledger.get_task(task_id)
        allowed(task["mineId"], mine_id)
        return task

    @app.get("/inspection/tasks/{task_id}/summary")
    def task_summary(task_id: str, mine_id: str | None = None):
        task = dependencies()[0].task_summary(task_id)
        allowed(task["mineId"], mine_id)
        return task

    @app.post("/inspection/tasks/{task_id}/control")
    def control_task(task_id: str, body: dict, mine_id: str | None = None):
        ledger, _, _ = dependencies()
        task = ledger.task_summary(task_id)
        allowed(task["mineId"], mine_id)
        action = body.get("action") if isinstance(body, dict) else None
        if action not in {"pause", "resume"}:
            raise HTTPException(400, "action must be pause or resume")
        controlled = ledger.control(task_id, action)
        return {"taskId": task_id, "state": controlled["state"], "action": action}

    @app.get("/inspection/tasks")
    def list_tasks(mine_id: str | None = None, check_cycle: str | None = None,
                   offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
        ledger, _, _ = dependencies()
        return ledger.list_tasks(offset=offset, limit=limit, mine_id=mine_id, check_cycle=check_cycle)

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str, mine_id: str | None = None):
        return run_owned(run_id, mine_id)

    @app.get("/v1/runs/{run_id}/summary")
    def run_summary(run_id: str, mine_id: str | None = None):
        return summary_owned(run_id, mine_id)

    @app.get("/v1/runs/{run_id}/plan")
    def run_plan(run_id: str, mine_id: str | None = None):
        summary_owned(run_id, mine_id)
        return dependencies()[0].run_plan(run_id)

    @app.get("/v1/runs/{run_id}/report")
    def run_report(run_id: str, mine_id: str | None = None):
        summary_owned(run_id, mine_id)
        return dependencies()[0].run_report(run_id)

    @app.get("/v1/runs/{run_id}/files")
    def run_files(run_id: str, mine_id: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
        summary_owned(run_id, mine_id)
        return dependencies()[0].run_files(run_id, offset=offset, limit=limit)

    @app.get("/v1/runs/{run_id}/files/{file_id}/content")
    def file_content(run_id: str, file_id: str, mine_id: str | None = None):
        run = run_owned(run_id, mine_id)
        file, path = frozen_file(run, file_id)
        media_type = {"pdf": "application/pdf", "txt": "text/plain; charset=utf-8", "md": "text/plain; charset=utf-8", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                      "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}.get(file.get("format"), "application/octet-stream")
        return FileResponse(path, filename=file.get("original_name", file_id), media_type=media_type, content_disposition_type="inline",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/v1/runs/{run_id}/files/{file_id}/pages/{page}/image")
    def page_image(run_id: str, file_id: str, page: int, mine_id: str | None = None):
        run = run_owned(run_id, mine_id)
        frozen_file(run, file_id)
        if page < 1 or page > 10000:
            raise HTTPException(400, "page must be in 1..10000")
        snapshot = dependencies()[2].snapshot(run)
        from .documents import render_pages
        rendered = render_pages(snapshot, [{"file_id": file_id, "page": page}], max_pages=1)
        if not rendered["images"]:
            raise HTTPException(404, "page image unavailable")
        return Response(rendered["images"][0]["image_bytes"], media_type="image/png", headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    @app.get("/v1/runs/{run_id}/coverage")
    def run_coverage(run_id: str, mine_id: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        summary_owned(run_id, mine_id)
        return dependencies()[0].run_coverage(run_id, offset=offset, limit=limit)

    @app.get("/v1/runs/{run_id}/candidates")
    def run_candidates(run_id: str, mine_id: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
        summary_owned(run_id, mine_id)
        return dependencies()[0].run_candidates(run_id, offset=offset, limit=limit)

    @app.get("/v1/runs/{run_id}/progress")
    def progress(run_id: str, mine_id: str | None = None, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        summary_owned(run_id, mine_id)
        return {"runId": run_id, "events": dependencies()[0].events(run_id, after=after, limit=limit)}

    @app.get("/v1/runs/{run_id}/evidence")
    def public_evidence(run_id: str, mine_id: str | None = None, query: str = "", file_id: str | None = None,
                        offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
        summary_owned(run_id, mine_id)
        return dependencies()[2].search_public(run_id, query, file_id=file_id, offset=offset, limit=limit)

    @app.get("/v1/runs/{run_id}/evidence/{evidence_id}")
    def public_evidence_item(run_id: str, evidence_id: str, mine_id: str | None = None):
        summary_owned(run_id, mine_id)
        return dependencies()[2].get_public(run_id, evidence_id)

    @app.get("/v1/runs/{run_id}/navigation")
    def navigation(run_id: str, mine_id: str | None = None, query: str = "", proposition_id: str | None = None):
        summary_owned(run_id, mine_id)
        service = dependencies()[2]
        if not hasattr(service, "navigate"):
            raise HTTPException(503, "evidence navigation is not configured")
        return service.navigate(run_id, query=query, proposition_id=proposition_id)

    @app.post("/v1/runs/{run_id}/questions")
    def question(run_id: str, body: dict):
        summary_owned(run_id)
        value = body.get("question")
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 2000:
            raise HTTPException(400, "question must contain 1 to 2000 characters")
        return dependencies()[2].answer(run_id, value, body.get("proposition_id"))

    @app.get("/v1/runs/{run_id}/compare/{other_id}")
    def compare_run(run_id: str, other_id: str):
        summary_owned(run_id)
        summary_owned(other_id)
        try:
            return dependencies()[2].compare(run_id, other_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/v1/runs/{run_id}/audit")
    def coverage_audit(run_id: str):
        summary_owned(run_id)
        return dependencies()[2].audit(run_id)

    @app.get("/v1/runs/{run_id}/work-units")
    def units(run_id: str):
        summary_owned(run_id)
        rows = dependencies()[0].list_work_units(run_id)
        return {"items": [{key: row.get(key) for key in ("work_unit_id", "kind", "state", "attempt", "error", "updated_at")} for row in rows]}

    @app.get("/v1/runs/{run_id}/reviews")
    def reviews(run_id: str):
        summary_owned(run_id)
        return {"items": dependencies()[0].get_run(run_id).get("reviews", [])}

    @app.post("/v1/runs/{run_id}/reviews")
    def review(run_id: str, body: dict):
        summary_owned(run_id)
        try:
            return {"saved": True, "review": dependencies()[0].append_review(run_id, body)}
        except (ValueError, Conflict) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/internal/runs/{run_id}/plan", dependencies=[Depends(require_internal_auth)])
    def plan(run_id: str):
        return dependencies()[2].plan(run_id)

    @app.get("/internal/runs/{run_id}/evidence", dependencies=[Depends(require_internal_auth)])
    def evidence(run_id: str, query: str = "", q: str | None = None, file_id: str | None = None,
                 offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100),
                 x_work_owner: str | None = Header(default=None), x_work_fence: int | None = Header(default=None)):
        if x_work_owner or x_work_fence is not None:
            work(run_id, x_work_owner, x_work_fence)
        return dependencies()[2].search(run_id, q if q is not None else query, file_id=file_id, offset=offset, limit=limit, worker_id=x_work_owner, fence=x_work_fence)

    @app.post("/internal/runs/{run_id}/tools/{tool_name}", dependencies=[Depends(require_internal_auth)])
    def core_tool(run_id: str, tool_name: str, body: dict, x_work_owner: str | None = Header(default=None), x_work_fence: int | None = Header(default=None)):
        if tool_name == "permit" and body.get("action") == "release":
            dependencies()[0].release_permit(run_id, x_work_owner, x_work_fence, body.get("resource"), permit_id=body.get("permit_id"), request_id=body.get("request_id"))
            return {"released": True}
        owner, fence = work(run_id, x_work_owner, x_work_fence)
        service = dependencies()[2]
        try:
            if tool_name == "parse_file":
                return service.parse(run_id, body.get("file_id"), worker_id=owner, fence=fence)
            if tool_name == "inspect_documents":
                return service.inspect_documents(run_id, body.get("file_ids"), worker_id=owner, fence=fence)
            if tool_name == "expand_event_context":
                return service.expand_event(run_id, body.get("event_id"))
            if tool_name == "compare_versions":
                return service.compare_versions(run_id, body.get("file_ids"))
            if tool_name == "submit_gaps":
                return service.submit_gaps(run_id, body.get("gaps"), worker_id=owner, fence=fence)
            if tool_name == "trace":
                return service.record_trace(run_id, body, worker_id=owner, fence=fence)
            if tool_name == "recognize_pages":
                return service.recognize(run_id, body, worker_id=owner, fence=fence)
            if tool_name == "permit" and body.get("action") == "acquire" and body.get("resource") == "model":
                token = dependencies()[0].acquire_permit(run_id, owner, fence, "model", body.get("request_id"), limit=dependencies()[1].model_concurrency, ttl=dependencies()[1].task_timeout + 30)
                if token is None:
                    raise HTTPException(429, "model_capacity_exhausted")
                return {"permit_id": token}
            raise HTTPException(404, "unknown evidence tool")
        except (Conflict, StaleLease, RunPaused) as exc:
            raise HTTPException(409, type(exc).__name__) from exc
        except (ValueError, NotFound) as exc:
            raise HTTPException(400 if isinstance(exc, ValueError) else 404, str(exc)) from exc

    @app.get("/internal/runs/{run_id}/evidence/{evidence_id}", dependencies=[Depends(require_internal_auth)])
    def evidence_item(run_id: str, evidence_id: str):
        return dependencies()[2].get(run_id, evidence_id)

    @app.get("/internal/runs/{run_id}/pages/{file_id}/{page}", dependencies=[Depends(require_internal_auth)])
    def evidence_page(run_id: str, file_id: str, page: int):
        return dependencies()[2].page(run_id, file_id, page)

    @app.get("/internal/runs/{run_id}/coverage", dependencies=[Depends(require_internal_auth)])
    def coverage(run_id: str):
        return dependencies()[2].coverage(run_id)

    @app.get("/internal/runs/{run_id}/context", dependencies=[Depends(require_internal_auth)])
    def context(run_id: str):
        return dependencies()[2].context(run_id)

    @app.post("/internal/runs/{run_id}/tools/recognize_pages", dependencies=[Depends(require_internal_auth)])
    def recognize(run_id: str, body: dict, x_work_owner: str | None = Header(default=None), x_work_fence: int | None = Header(default=None)):
        owner, fence = work(run_id, x_work_owner, x_work_fence)
        try:
            return dependencies()[2].recognize(run_id, body, worker_id=owner, fence=fence)
        except (Conflict, StaleLease, RunPaused) as exc:
            raise HTTPException(409, str(exc)) from exc
        except (ValueError, NotFound) as exc:
            raise HTTPException(400 if isinstance(exc, ValueError) else 404, str(exc)) from exc

    @app.post("/internal/runs/{run_id}/candidates", dependencies=[Depends(require_internal_auth)])
    def submit(run_id: str, body: dict, x_work_owner: str | None = Header(default=None), x_work_fence: int | None = Header(default=None)):
        owner, fence = work(run_id, x_work_owner, x_work_fence)
        candidates = body.get("candidates")
        if not isinstance(candidates, list) or len(candidates) > 500:
            raise HTTPException(400, "candidates must be a list of at most 500 items")
        try:
            return dependencies()[2].submit(run_id, candidates, worker_id=owner, fence=fence)
        except (Conflict, StaleLease, RunPaused) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    return app


app = create_app()
