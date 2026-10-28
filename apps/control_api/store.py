from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from sqlalchemy import Float, Integer, JSON, String, Text, create_engine, func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool


class Conflict(ValueError):
    pass


class NotFound(LookupError):
    pass


class StaleLease(RuntimeError):
    pass


class RunPaused(RuntimeError):
    pass


class Base(DeclarativeBase):
    pass


class Task(Base):
    __tablename__ = "tasks"
    task_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    execution_id: Mapped[str] = mapped_column(String(160), index=True)
    identity_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    scope_hash: Mapped[str] = mapped_column(String(64))
    mine_id: Mapped[str] = mapped_column(String(160), index=True)
    check_cycle: Mapped[str] = mapped_column(String(20))
    request_json: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(30), default="running")
    created_at: Mapped[float] = mapped_column(Float)


class Run(Base):
    __tablename__ = "runs"
    run_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(80), index=True)
    item_code: Mapped[str] = mapped_column(String(100))
    plan_json: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(30), default="queued")
    control_state: Mapped[str] = mapped_column(String(30), default="running")
    worker_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    fence: Mapped[int] = mapped_column(Integer, default=0)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    snapshot_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[float] = mapped_column(Float)


class Record(Base):
    __tablename__ = "records"
    record_id: Mapped[str] = mapped_column(String(240), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)


class Event(Base):
    __tablename__ = "events"
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    event_json: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[float] = mapped_column(Float)


class Outbox(Base):
    __tablename__ = "outbox"
    delivery_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    payload_json: Mapped[dict] = mapped_column(JSON)
    callback_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    state: Mapped[str] = mapped_column(String(30), default="waiting")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    worker_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    fence: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[float] = mapped_column(Float)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class WorkUnit(Base):
    __tablename__ = "work_units"
    record_id: Mapped[str] = mapped_column(String(240), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    unit_id: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(40))
    fingerprint: Mapped[str] = mapped_column(String(64))
    input_json: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(20), default="queued")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    run_fence: Mapped[int] = mapped_column(Integer, default=0)
    output_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float)


class ResourceGate(Base):
    __tablename__ = "resource_gates"
    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class SchedulingTurn(Base):
    __tablename__ = "scheduling_turns"
    mine_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    last_claimed_at: Mapped[float] = mapped_column(Float, default=0)


class ProviderPermit(Base):
    __tablename__ = "provider_permits"
    permit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    resource: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[str] = mapped_column(String(80), index=True)
    owner: Mapped[str] = mapped_column(String(160))
    fence: Mapped[int] = mapped_column(Integer)
    lease_until: Mapped[float] = mapped_column(Float)


class Store:
    """Business ledger with compare-and-swap leases; SQLite local, PostgreSQL target."""

    def __init__(self, path: str | Path = ".local/control.sqlite3", *, database_url: str | None = None, clock=time.time):
        if database_url:
            url = database_url
        elif str(path).startswith(("sqlite:", "postgresql:", "postgresql+psycopg:")):
            url = str(path)
        elif str(path) == ":memory:":
            url = "sqlite://"
        else:
            file = Path(path)
            file.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite:///{file.resolve().as_posix()}"
        kwargs = {"pool_pre_ping": True}
        if url.startswith("sqlite:"):
            database = make_url(url).database
            if database and database != ":memory:":
                Path(database).parent.mkdir(parents=True, exist_ok=True)
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if url == "sqlite://":
                kwargs["poolclass"] = StaticPool
        self.engine = create_engine(url, **kwargs)
        self.clock = clock
        Base.metadata.create_all(self.engine)
        for resource in ("model", "ocr", "document"):
            try:
                with Session(self.engine) as db, db.begin():
                    if db.get(ResourceGate, resource) is None:
                        db.add(ResourceGate(name=resource, revision=0))
            except IntegrityError:
                pass  # Another instance initialized the same shared gate.

    @staticmethod
    def canonical(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def digest(cls, value: Any) -> str:
        return hashlib.sha256(cls.canonical(value).encode()).hexdigest()

    def accept(self, request: dict, plans: dict[str, dict] | None = None) -> dict:
        request = json.loads(self.canonical(request))
        codes = [item["itemCode"] for item in request["items"]]
        if not codes or len(codes) != len(set(codes)):
            raise Conflict("items must contain unique itemCode values")
        scope = {"mineId": request["mineId"], "checkCycle": request["checkCycle"], "items": sorted(codes),
                 "resultCallbackUrl": request.get("resultCallbackUrl"), "scope": request.get("scope"),
                 "sourceSystem": request.get("sourceSystem", "upstream"), "environment": request.get("environment", "local")}
        scope_hash = self.digest(scope)
        implicit_scope = {key: scope[key] for key in ("mineId", "checkCycle", "items")}
        external = request.get("executionId") or request.get("taskId") or "fingerprint-" + self.digest(implicit_scope)
        # Execution identity is tenant/source scoped; same scope with a new executionId is a new run.
        identity_hash = self.digest([scope["sourceSystem"], scope["environment"], request["mineId"], request.get("taskId"), external])
        task_id = "task-" + identity_hash[:40]
        now = self.clock()
        try:
            with Session(self.engine) as db, db.begin():
                prior = db.get(Task, task_id)
                if prior:
                    if prior.scope_hash != scope_hash:
                        raise Conflict("accepted task scope or callback changed; use a new executionId")
                else:
                    if request.get("status") in {"pause", "resume"}:
                        raise NotFound("control request has no accepted task")
                    db.add(Task(task_id=task_id, execution_id=str(external), identity_hash=identity_hash,
                                scope_hash=scope_hash, mine_id=request["mineId"], check_cycle=request["checkCycle"],
                                request_json=request, state="running", created_at=now))
                    for code in codes:
                        run_id = "run-" + self.digest([task_id, code])[:40]
                        db.add(Run(run_id=run_id, task_id=task_id, item_code=code, plan_json=(plans or {}).get(code, {}), state="queued", control_state="running", created_at=now))
                        db.add(Event(run_id=run_id, event_json={"event": "accepted", "task_id": task_id}, created_at=now))
        except IntegrityError:
            with Session(self.engine) as db:
                prior = db.get(Task, task_id)
                if prior is None or prior.scope_hash != scope_hash:
                    raise Conflict("conflicting task admission")
        return self.get_task(task_id)

    create_task = accept

    @staticmethod
    def _summary_select():
        """Select metadata only: polling must never deserialize document snapshots."""
        registered = select(func.count()).where(Record.run_id == Run.run_id, Record.kind == "evidence").correlate(Run).scalar_subquery()
        return select(
            Run.run_id.label("runId"), Run.task_id.label("taskId"), Run.item_code.label("itemCode"),
            Task.mine_id.label("mineId"), Task.check_cycle.label("checkCycle"),
            Run.state, Run.control_state, Run.created_at,
            Run.plan_json["name"].as_string().label("name"),
            Run.plan_json["planRevision"].as_string().label("plan_revision"),
            Run.plan_json["revision"].as_string().label("asset_revision"),
            Run.result_json["status"].as_string().label("result_status"),
            Run.result_json["description"].as_string().label("description"),
            func.coalesce(func.json_array_length(Run.snapshot_json["files"]), 0).label("file_count"),
            (func.coalesce(func.json_array_length(Run.snapshot_json["evidence"]), 0) + registered).label("evidence_count"),
            func.coalesce(func.json_array_length(Run.result_json["coverage_gaps"]), 0).label("coverage_gap_count"),
            Run.snapshot_json["source_complete"].as_boolean().label("source_complete"),
            Outbox.state.label("delivery_state"), Outbox.attempt.label("delivery_attempt"),
        ).join(Task, Task.task_id == Run.task_id).outerjoin(Outbox, Outbox.run_id == Run.run_id)

    @staticmethod
    def _summary_row(row) -> dict:
        item = dict(row)
        item["run_id"], item["item_code"] = item["runId"], item["itemCode"]
        item["ruleRevision"] = item.pop("plan_revision") or item.pop("asset_revision")
        item.pop("asset_revision", None)
        item["description"] = item["description"] or ""
        item["delivery"] = {"state": item.pop("delivery_state") or "none", "attempt": item.pop("delivery_attempt") or 0}
        return item

    def run_summary(self, run_id: str) -> dict:
        with Session(self.engine) as db:
            row = db.execute(self._summary_select().where(Run.run_id == run_id)).mappings().first()
            if row is None:
                raise NotFound(run_id)
            return self._summary_row(row)

    def task_summary(self, task_id: str) -> dict:
        with Session(self.engine) as db:
            row = db.execute(select(Task.task_id, Task.execution_id, Task.mine_id, Task.check_cycle, Task.state, Task.created_at).where(Task.task_id == task_id)).mappings().first()
            if row is None:
                raise NotFound(task_id)
            runs = db.execute(self._summary_select().where(Run.task_id == task_id).order_by(Run.created_at, Run.run_id)).mappings().all()
            return {"taskId": row["task_id"], "executionId": row["execution_id"], "mineId": row["mine_id"], "checkCycle": row["check_cycle"],
                    "state": row["state"], "created_at": row["created_at"], "runs": [self._summary_row(run) for run in runs]}

    def list_tasks(self, *, offset: int = 0, limit: int = 20, mine_id: str | None = None,
                   check_cycle: str | None = None, allowed_mines: tuple[str, ...] | None = None) -> dict:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid pagination")
        clauses = []
        if allowed_mines is not None:
            clauses.append(Task.mine_id.in_(allowed_mines))
        if mine_id is not None:
            clauses.append(Task.mine_id == mine_id)
        if check_cycle is not None:
            clauses.append(Task.check_cycle == check_cycle)
        with Session(self.engine) as db:
            total = db.scalar(select(func.count()).select_from(Task).where(*clauses)) or 0
            rows = db.execute(select(Task.task_id, Task.execution_id, Task.mine_id, Task.check_cycle, Task.state, Task.created_at)
                              .where(*clauses).order_by(Task.created_at.desc(), Task.task_id).offset(offset).limit(limit)).mappings().all()
            task_ids = [row["task_id"] for row in rows]
            grouped = {task_id: [] for task_id in task_ids}
            if task_ids:
                for run in db.execute(self._summary_select().where(Run.task_id.in_(task_ids)).order_by(Run.created_at, Run.run_id)).mappings():
                    grouped[run["taskId"]].append(self._summary_row(run))
            items = [{"taskId": row["task_id"], "executionId": row["execution_id"], "mineId": row["mine_id"], "checkCycle": row["check_cycle"],
                      "state": row["state"], "created_at": row["created_at"], "runs": grouped[row["task_id"]]} for row in rows]
            return {"items": items, "total": total, "offset": offset, "limit": limit, "has_more": offset + limit < total}

    def get_task(self, task_id: str) -> dict:
        with Session(self.engine) as db:
            task = db.get(Task, task_id)
            if not task:
                raise NotFound(task_id)
            runs = db.scalars(select(Run).where(Run.task_id == task_id).order_by(Run.created_at, Run.run_id)).all()
            return {"taskId": task.task_id, "task_id": task.task_id, "executionId": task.execution_id,
                    "mineId": task.mine_id, "checkCycle": task.check_cycle, "state": task.state,
                    "request": task.request_json, "runs": [self._run_dict(run, task) for run in runs]}

    @staticmethod
    def _run_dict(run: Run, task: Task | None = None) -> dict:
        request = task.request_json if task else {}
        rule_revision = run.plan_json.get("planRevision", run.plan_json.get("revision"))
        result = {"runId": run.run_id, "run_id": run.run_id, "taskId": run.task_id, "task_id": run.task_id,
                  "itemCode": run.item_code, "item_code": run.item_code, "ruleRevision": rule_revision,
                  "plan": run.plan_json, "state": run.state, "execution_state": run.state,
                  "control_state": run.control_state, "fence": run.fence, "attempt": run.attempt,
                  "worker_id": run.worker_id, "lease_until": run.lease_until,
                  "snapshot": run.snapshot_json, "result": run.result_json,
                  "request": request, "mine_id": request.get("mineId"), "mineId": request.get("mineId"),
                  "check_cycle": request.get("checkCycle"), "environment": request.get("environment", "local"),
                  "rule_revision": rule_revision}
        result["spec"] = {"mine_code": request.get("mineId"), "mine_id": request.get("mineId"),
                          "check_cycle": request.get("checkCycle"), "item_code": run.item_code,
                          "rule_revision": rule_revision,
                          "source_system": request.get("sourceSystem", "upstream"),
                          "environment": request.get("environment", "local"),
                          "scope": request.get("scope", {}), "callback_url": request.get("resultCallbackUrl")}
        return result

    def get_run(self, run_id: str) -> dict:
        with Session(self.engine) as db:
            run = db.get(Run, run_id)
            if not run:
                raise NotFound(run_id)
            task = db.get(Task, run.task_id)
            result = self._run_dict(run, task)
            outbox = db.scalar(select(Outbox).where(Outbox.run_id == run_id))
            result["delivery"] = {"state": outbox.state, "attempt": outbox.attempt, "last_error": outbox.last_error} if outbox else {"state": "none", "attempt": 0}
            records = db.scalars(select(Record).where(Record.run_id == run_id)).all()
            for kind in ("candidate", "gap", "evidence", "work_unit", "search", "trace", "review", "coverage_audit"):
                result[{"candidate": "candidates", "gap": "gaps", "evidence": "evidence", "work_unit": "work_units", "search": "searches", "trace": "traces", "review": "reviews", "coverage_audit": "coverage_audits"}[kind]] = [r.payload for r in records if r.kind == kind]
            result["work_units"].extend(self._unit_dict(unit) for unit in db.scalars(select(WorkUnit).where(WorkUnit.run_id == run_id)).all())
            return result

    def run_report(self, run_id: str) -> dict:
        with Session(self.engine) as db:
            row = db.execute(select(Run.run_id, Run.result_json).where(Run.run_id == run_id)).first()
            if row is None:
                raise NotFound(run_id)
            return {"runId": run_id, "result": row.result_json}

    def run_plan(self, run_id: str) -> dict:
        with Session(self.engine) as db:
            row = db.execute(select(Run.plan_json, Run.snapshot_json["manifest_hash"].as_string().label("manifest_hash")).where(Run.run_id == run_id)).first()
            if row is None:
                raise NotFound(run_id)
            return {"run_id": run_id, "rule_revision": row.plan_json.get("planRevision", row.plan_json.get("revision")),
                    "manifest_hash": row.manifest_hash, "plan": row.plan_json}

    def run_files(self, run_id: str, *, offset: int = 0, limit: int = 20) -> dict:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid pagination")
        with Session(self.engine) as db:
            row = db.execute(select(Run.run_id, Run.snapshot_json["files"].label("files")).where(Run.run_id == run_id)).first()
            if row is None:
                raise NotFound(run_id)
        files = []
        for file in row.files or []:
            files.append({key: file.get(key) for key in ("file_id", "original_name", "format", "source_role", "material_group", "content_hash", "byte_size", "report_date", "attachment_id")})
        return {"items": files[offset:offset + limit], "total": len(files), "offset": offset, "limit": limit, "has_more": offset + limit < len(files)}

    def run_candidates(self, run_id: str, *, offset: int = 0, limit: int = 20) -> dict:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid pagination")
        with Session(self.engine) as db:
            if not db.scalar(select(Run.run_id).where(Run.run_id == run_id)):
                raise NotFound(run_id)
            clauses = (Record.run_id == run_id, Record.kind == "candidate")
            total = db.scalar(select(func.count()).select_from(Record).where(*clauses)) or 0
            records = db.scalars(select(Record).where(*clauses).order_by(Record.created_at, Record.record_id).offset(offset).limit(limit)).all()
            items = []
            for record in records:
                item = record.payload
                raw = item.get("candidate", item)
                items.append({"candidate_id": item.get("candidate_id", raw.get("candidate_id")), "kind": raw.get("kind"), "proposition_id": raw.get("proposition_id"),
                              "fields": raw.get("fields", {}), "validation_status": item.get("validation_status"), "reasons": item.get("reasons", []),
                              "verified": bool(item.get("verified")), "evidence_refs": raw.get("evidence_refs", []),
                              "field_evidence": raw.get("field_evidence", {}), "field_bindings": item.get("field_bindings", {}),
                              "verification_scope": item.get("verification_scope"), "anchored_fields": item.get("anchored_fields", {})})
        return {"items": items, "total": total, "offset": offset, "limit": limit, "has_more": offset + limit < total}

    def run_coverage(self, run_id: str, *, offset: int = 0, limit: int = 50) -> dict:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("invalid pagination")
        with Session(self.engine) as db:
            row = db.execute(select(Run.run_id, Run.snapshot_json["coverage"].label("coverage"), Run.snapshot_json["coverage_gaps"].label("gaps"),
                                    Run.result_json["coverage_gaps"].label("result_gaps"),
                                    Run.snapshot_json["source_complete"].as_boolean().label("source_complete")).where(Run.run_id == run_id)).first()
            if row is None:
                raise NotFound(run_id)
            units, gaps = row.coverage or [], row.result_gaps if isinstance(row.result_gaps, list) else list(row.gaps or [])
            if not isinstance(row.result_gaps, list):
                gaps.extend(record.payload.get("reason", "unresolved_gap") for record in db.scalars(select(Record).where(Record.run_id == run_id, Record.kind == "gap")).all())
        return {"total": len(units), "readable": sum(item.get("state") in {"readable", "empty"} for item in units),
                "source_complete": bool(row.source_complete), "items": units[offset:offset + limit],
                "gaps": gaps[offset:offset + limit], "gap_total": len(gaps), "offset": offset, "limit": limit, "has_more": offset + limit < max(len(units), len(gaps))}

    def claim(self, worker_id: str, lease_seconds: float = 60, *, run_id: str | None = None) -> dict | None:
        if lease_seconds <= 0 or not worker_id:
            raise ValueError("worker and positive lease required")
        now = self.clock()
        with Session(self.engine) as db, db.begin():
            eligible = (Run.control_state == "running") & ((Run.state == "queued") | ((Run.state == "running") & (Run.lease_until <= now)))
            stmt = select(Run).join(Task, Run.task_id == Task.task_id).outerjoin(SchedulingTurn, SchedulingTurn.mine_id == Task.mine_id).where(eligible)
            if run_id:
                stmt = stmt.where(Run.run_id == run_id)
            prior = db.scalars(stmt.order_by(func.coalesce(SchedulingTurn.last_claimed_at, 0), Run.created_at, Run.run_id)).first()
            if not prior:
                return None
            old_fence = prior.fence
            claimed = db.execute(update(Run).where(Run.run_id == prior.run_id, Run.fence == old_fence, eligible)
                                 .values(state="running", worker_id=worker_id, lease_until=now + lease_seconds, fence=old_fence + 1, attempt=Run.attempt + 1))
            if claimed.rowcount != 1:
                return None
            db.flush()
            db.refresh(prior)
            task = db.get(Task, prior.task_id)
            turn = db.get(SchedulingTurn, task.mine_id)
            if turn is None:
                db.add(SchedulingTurn(mine_id=task.mine_id, last_claimed_at=now))
            else:
                turn.last_claimed_at = now
            db.add(Event(run_id=prior.run_id, event_json={"event": "claimed", "worker_id": worker_id, "fence": prior.fence}, created_at=now))
            return self._run_dict(prior, db.get(Task, prior.task_id))

    def _owned(self, db: Session, run_id: str, worker_id: str, fence: int, *, allow_paused: bool = False) -> Run:
        # Row lock serializes mutations on PostgreSQL; fence rejects prior owners.
        # A no-op CAS also obtains a write lock on SQLite, where FOR UPDATE is ignored.
        db.execute(update(Run).where(Run.run_id == run_id, Run.state == "running", Run.worker_id == worker_id,
                   Run.fence == fence, Run.lease_until > self.clock()).values(fence=Run.fence))
        run = db.scalar(select(Run).where(Run.run_id == run_id).with_for_update())
        if not run or run.state != "running" or run.worker_id != worker_id or run.fence != fence or not run.lease_until or run.lease_until <= self.clock():
            raise StaleLease(run_id)
        if run.control_state != "running" and not allow_paused:
            raise RunPaused(run_id)
        return run

    def renew(self, run_id: str, worker_id: str, fence: int, lease_seconds: float = 60) -> dict:
        with Session(self.engine) as db, db.begin():
            run = self._owned(db, run_id, worker_id, fence, allow_paused=True)
            run.lease_until = self.clock() + lease_seconds
            return {"runId": run_id, "fence": fence, "leaseUntil": run.lease_until}

    def checkpoint(self, run_id: str, worker_id: str, fence: int, event: dict | None = None) -> dict:
        with Session(self.engine) as db, db.begin():
            self._owned(db, run_id, worker_id, fence)
            payload = {"event": "checkpoint", **(event or {}), "fence": fence}
            db.add(Event(run_id=run_id, event_json=payload, created_at=self.clock()))
            return payload

    def control(self, task_id: str, action: str) -> dict:
        if action not in {"pause", "resume"}:
            raise ValueError("unknown control action")
        with Session(self.engine) as db, db.begin():
            task = db.get(Task, task_id)
            if not task:
                raise NotFound(task_id)
            runs = db.scalars(select(Run).where(Run.task_id == task_id)).all()
            for run in runs:
                if run.state in {"completed", "failed"}:
                    continue
                run.control_state = "paused" if action == "pause" else "running"
                if action == "pause" and run.state == "queued":
                    run.state = "paused"
                elif action == "resume" and run.state == "paused":
                    run.state = "queued"
            if any(run.state not in {"completed", "failed"} for run in runs):
                task.state = "paused" if action == "pause" else "running"
        return self.get_task(task_id)

    def pause_run(self, run_id: str, worker_id: str, fence: int) -> None:
        with Session(self.engine) as db, db.begin():
            run = self._owned(db, run_id, worker_id, fence, allow_paused=True)
            run.state = "paused"
            run.worker_id = None
            run.lease_until = None

    def save_snapshot(self, run_id: str, worker_id: str, fence: int, snapshot: dict) -> None:
        with Session(self.engine) as db, db.begin():
            run = self._owned(db, run_id, worker_id, fence)
            if run.snapshot_json is not None and self.canonical(run.snapshot_json) != self.canonical(snapshot):
                raise Conflict("run snapshot is immutable")
            run.snapshot_json = snapshot

    def save_records(self, run_id: str, worker_id: str, fence: int, kind: str, payloads: list[dict]) -> int:
        keys = {"candidate": "candidate_id", "gap": "gap_id", "evidence": "evidence_id", "work_unit": "work_unit_id", "search": "search_id", "trace": "trace_id", "review": "review_id", "coverage_audit": "audit_id"}
        if kind not in keys:
            raise ValueError("unknown record kind")
        with Session(self.engine) as db, db.begin():
            self._owned(db, run_id, worker_id, fence)
            saved = 0
            for payload in payloads:
                rid = f"{run_id}:{kind}:{payload.get(keys[kind]) or self.digest(payload)}"
                prior = db.get(Record, rid)
                if prior:
                    if self.canonical(prior.payload) != self.canonical(payload):
                        raise Conflict("record identity reused with different content")
                    continue
                db.add(Record(record_id=rid, run_id=run_id, kind=kind, payload=payload, created_at=self.clock()))
                saved += 1
            return saved

    def save_candidates(self, run_id: str, worker_id: str, fence: int, candidates: list[dict], gaps: list[dict] | None = None) -> dict:
        saved = self.save_records(run_id, worker_id, fence, "candidate", candidates)
        if gaps:
            self.save_records(run_id, worker_id, fence, "gap", gaps)
        return {"savedCandidates": saved}

    def save_evidence(self, run_id: str, worker_id: str, fence: int, evidence: list[dict]) -> int:
        return self.save_records(run_id, worker_id, fence, "evidence", evidence)

    def save_work_units(self, run_id: str, worker_id: str, fence: int, units: list[dict]) -> int:
        return self.save_records(run_id, worker_id, fence, "work_unit", units)

    @staticmethod
    def _unit_dict(unit: WorkUnit) -> dict:
        return {"work_unit_id": unit.unit_id, "run_id": unit.run_id, "kind": unit.kind,
                "fingerprint": unit.fingerprint, "input": unit.input_json, "state": unit.state,
                "attempt": unit.attempt, "fence": unit.run_fence, "output": unit.output_json,
                "error": unit.error, "created_at": unit.created_at, "updated_at": unit.updated_at}

    def enqueue_work_unit(self, run_id: str, worker_id: str, fence: int, kind: str, inputs: dict, *, page_limit: int | None = None) -> dict:
        fingerprint = self.digest({"kind": kind, "input": inputs})
        unit_id = self.digest([run_id, fingerprint])
        with Session(self.engine) as db, db.begin():
            self._owned(db, run_id, worker_id, fence)
            key = f"{run_id}:{unit_id}"
            unit = db.get(WorkUnit, key)
            if unit is None:
                if page_limit is not None:
                    existing = db.scalars(select(WorkUnit).where(WorkUnit.run_id == run_id, WorkUnit.kind == kind)).all()
                    if sum(int(item.input_json.get("page_count") or 1) for item in existing) + int(inputs.get("page_count") or 1) > page_limit:
                        raise Conflict("page_budget_exhausted")
                unit = WorkUnit(record_id=key, unit_id=unit_id, run_id=run_id, kind=kind,
                                fingerprint=fingerprint, input_json=inputs, state="queued", attempt=0,
                                run_fence=0, created_at=self.clock(), updated_at=self.clock())
                db.add(unit)
                db.flush()
            return self._unit_dict(unit)

    def claim_work_unit(self, run_id: str, worker_id: str, fence: int, unit_id: str, *, max_attempts: int = 3) -> dict | None:
        with Session(self.engine) as db, db.begin():
            self._owned(db, run_id, worker_id, fence)
            unit = db.scalar(select(WorkUnit).where(WorkUnit.record_id == f"{run_id}:{unit_id}").with_for_update())
            if unit is None:
                raise NotFound(unit_id)
            if unit.state == "succeeded" or unit.attempt >= max_attempts:
                return None
            if unit.state == "running" and unit.run_fence == fence:
                return None
            previous_state, previous_attempt, previous_fence = unit.state, unit.attempt, unit.run_fence
            changed = db.execute(update(WorkUnit).where(WorkUnit.record_id == unit.record_id,
                WorkUnit.state == previous_state, WorkUnit.attempt == previous_attempt, WorkUnit.run_fence == previous_fence)
                .values(state="running", run_fence=fence, error=None, attempt=previous_attempt + 1, updated_at=self.clock()))
            if changed.rowcount != 1:
                return None
            db.refresh(unit)
            return self._unit_dict(unit)

    def finish_work_unit(self, run_id: str, worker_id: str, fence: int, unit_id: str, *, output: dict | None = None, error: str | None = None) -> dict:
        with Session(self.engine) as db, db.begin():
            self._owned(db, run_id, worker_id, fence)
            unit = db.scalar(select(WorkUnit).where(WorkUnit.record_id == f"{run_id}:{unit_id}").with_for_update())
            if unit is None or unit.state != "running" or unit.run_fence != fence:
                raise StaleLease(unit_id)
            unit.state = "failed" if error else "succeeded"
            unit.output_json, unit.error, unit.updated_at = output, error, self.clock()
            db.flush()
            return self._unit_dict(unit)

    def list_work_units(self, run_id: str) -> list[dict]:
        with Session(self.engine) as db:
            return [self._unit_dict(unit) for unit in db.scalars(select(WorkUnit).where(WorkUnit.run_id == run_id).order_by(WorkUnit.created_at, WorkUnit.unit_id)).all()]

    def acquire_permit(self, run_id, worker_id, fence, resource, request_id, *, limit, ttl=3600):
        if resource not in {"model", "ocr", "document"} or not isinstance(request_id, str) or not request_id or limit < 1:
            raise ValueError("invalid_provider_permit")
        permit_id = self.digest([run_id, worker_id, fence, resource, request_id])
        with Session(self.engine) as db, db.begin():
            self._owned(db, run_id, worker_id, fence)
            # UPDATE obtains a real SQLite write lock and a PostgreSQL row lock.
            db.execute(update(ResourceGate).where(ResourceGate.name == resource).values(revision=ResourceGate.revision + 1))
            prior = db.get(ProviderPermit, permit_id)
            if prior and prior.lease_until > self.clock():
                return permit_id
            active = db.scalar(select(func.count()).select_from(ProviderPermit).where(ProviderPermit.resource == resource, ProviderPermit.lease_until > self.clock())) or 0
            if active >= limit:
                return None
            if prior is None:
                db.add(ProviderPermit(permit_id=permit_id, resource=resource, run_id=run_id, owner=worker_id, fence=fence, lease_until=self.clock() + ttl))
            else:
                prior.lease_until = self.clock() + ttl
            return permit_id

    def release_permit(self, run_id, worker_id, fence, resource, *, permit_id=None, request_id=None):
        identity = permit_id or self.digest([run_id, worker_id, fence, resource, request_id])
        with Session(self.engine) as db, db.begin():
            row = db.get(ProviderPermit, identity)
            if row is not None:
                if (row.run_id, row.owner, row.fence, row.resource) != (run_id, worker_id, fence, resource):
                    raise StaleLease("provider permit owner mismatch")
                db.delete(row)
        return True

    def append_review(self, run_id: str, payload: dict) -> dict:
        from .quality import validate_review_evidence
        run = self.get_run(run_id)
        if payload.get("candidate_id") not in {item.get("candidate_id", item.get("candidate", {}).get("candidate_id")) for item in run.get("candidates", [])}:
            raise ValueError("review candidate outside frozen run")
        evidence = {item["evidence_id"]: item for item in (run.get("snapshot") or {}).get("evidence", []) + run.get("evidence", [])}
        review = validate_review_evidence(payload, evidence_by_id=evidence)
        # Reviews are append-only notes. They cannot mutate a frozen result or silently promote a model fact.
        review["run_id"] = run_id
        review["created_at"] = self.clock()
        key = f"{run_id}:review:{review['review_id']}"
        with Session(self.engine) as db, db.begin():
            prior = db.get(Record, key)
            if prior:
                stable = {key: value for key, value in prior.payload.items() if key != "created_at"}
                if self.canonical(stable) != self.canonical({key: value for key, value in review.items() if key != "created_at"}):
                    raise Conflict("review is immutable")
                return prior.payload
            db.add(Record(record_id=key, run_id=run_id, kind="review", payload=review, created_at=self.clock()))
        return review

    def finalize(self, run_id: str, worker_id: str, fence: int, result: dict, delivery_payload: dict | None = None, callback_url: str | None = None) -> dict:
        if result.get("status") not in {"pass", "no_pass", "pending", "unverifiable", "failed"}:
            raise Conflict("invalid result status")
        with Session(self.engine) as db, db.begin():
            run = self._owned(db, run_id, worker_id, fence)
            if run.result_json is not None:
                raise Conflict("run result is immutable")
            if result.get("run_id", run_id) != run_id:
                raise Conflict("result run identity mismatch")
            snapshot = run.snapshot_json
            registered = {entry["evidence_id"] for entry in (snapshot or {}).get("evidence", [])}
            registered.update(record.payload["evidence_id"] for record in db.scalars(select(Record).where(Record.run_id == run_id, Record.kind == "evidence")).all())
            if not set(result.get("evidence_refs", [])) <= registered:
                raise Conflict("result contains unregistered evidence")
            if snapshot and result.get("snapshot_id", snapshot.get("snapshot_id")) != snapshot.get("snapshot_id"):
                raise Conflict("result snapshot identity mismatch")
            if result["status"] == "pass":
                propositions = result.get("propositions", [])
                latest = db.scalar(select(Record).where(Record.run_id == run_id, Record.kind == "coverage_audit").order_by(Record.created_at.desc(), Record.record_id.desc()))
                audit = latest.payload if latest else {}
                coverage_proven = bool(audit.get("source_complete") is True and audit.get("coverage_complete") is True
                                      and audit.get("manifest_hash") == (snapshot or {}).get("manifest_hash")
                                      and audit.get("rule_revision") == run.plan_json.get("planRevision", run.plan_json.get("revision"))
                                      and result.get("coverage_audit", {}).get("audit_id") == audit.get("audit_id"))
                if (not snapshot or not (coverage_proven or (snapshot.get("source_complete") is True and not latest and (snapshot.get("environment") or "local") != "production")) or not propositions
                        or result.get("coverage_gaps") or any(prop.get("state") != "true" or not prop.get("evidence_refs") for prop in propositions)):
                    raise Conflict("pass requires complete source and proven propositions")
            if result["status"] == "no_pass":
                findings = result.get("findings", [])
                if not findings or any(not finding.get("evidence_refs") or not set(finding["evidence_refs"]) <= registered for finding in findings):
                    raise Conflict("no_pass requires registered finding evidence")
            run.result_json = result
            run.state = "failed" if result["status"] == "failed" else "completed"
            run.worker_id = None
            run.lease_until = None
            db.add(Event(run_id=run_id, event_json={"event": "finished", "status": result["status"], "fence": fence}, created_at=self.clock()))
            if delivery_payload is not None:
                delivery_id = "delivery-" + self.digest(run_id)
                db.add(Outbox(delivery_id=delivery_id, run_id=run_id, payload_json=delivery_payload,
                              callback_url=callback_url, state="waiting", available_at=self.clock()))
            db.flush()
            task = db.get(Task, run.task_id)
            remaining = db.scalars(select(Run).where(Run.task_id == run.task_id)).all()
            if all(r.state in {"completed", "failed"} for r in remaining):
                task.state = "failed" if any(r.state == "failed" for r in remaining) else "completed"
            return {"runId": run_id, "state": run.state, "status": result["status"]}

    def claim_delivery(self, worker_id: str, lease_seconds: float = 60) -> dict | None:
        now = self.clock()
        with Session(self.engine) as db, db.begin():
            eligible = ((Outbox.state == "waiting") & (Outbox.available_at <= now)) | ((Outbox.state == "sending") & (Outbox.lease_until <= now))
            outbox = db.scalars(select(Outbox).where(eligible).order_by(Outbox.available_at)).first()
            if not outbox:
                return None
            old_fence = outbox.fence
            claimed = db.execute(update(Outbox).where(Outbox.delivery_id == outbox.delivery_id, Outbox.fence == old_fence, eligible)
                                 .values(state="sending", worker_id=worker_id, lease_until=now + lease_seconds, fence=old_fence + 1, attempt=Outbox.attempt + 1))
            if claimed.rowcount != 1:
                return None
            db.refresh(outbox)
            return {"deliveryId": outbox.delivery_id, "delivery_id": outbox.delivery_id, "runId": outbox.run_id, "run_id": outbox.run_id,
                    "payload": outbox.payload_json, "callbackUrl": outbox.callback_url, "callback_url": outbox.callback_url,
                    "attempt": outbox.attempt, "fence": outbox.fence}

    def complete_delivery(self, delivery_id: str, worker_id: str, fence: int, *, error: str | None = None, retry: bool = True) -> None:
        with Session(self.engine) as db, db.begin():
            outbox = db.scalar(select(Outbox).where(Outbox.delivery_id == delivery_id).with_for_update())
            if not outbox or outbox.state != "sending" or outbox.worker_id != worker_id or outbox.fence != fence or not outbox.lease_until or outbox.lease_until <= self.clock():
                raise StaleLease(delivery_id)
            outbox.state = "delivered" if error is None else "waiting" if retry else "failed"
            outbox.last_error = error
            outbox.worker_id = None
            outbox.lease_until = None
            if error is not None and retry:
                outbox.available_at = self.clock() + min(300, 2 ** min(outbox.attempt, 8))

    def events(self, run_id: str, *, after: int = 0, limit: int = 100) -> list[dict]:
        with Session(self.engine) as db:
            return [{"sequence": item.sequence, "created_at": item.created_at, **item.event_json} for item in db.scalars(select(Event).where(Event.run_id == run_id, Event.sequence > after).order_by(Event.sequence).limit(min(limit, 500))).all()]
