"""Versioned business contracts for the control plane and Pi adapter."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Any, Literal
import re

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class ResultStatus(StrEnum):
    PASS = "pass"
    NO_PASS = "no_pass"
    PENDING = "pending"
    UNVERIFIABLE = "unverifiable"
    FAILED = "failed"


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str
    document_id: str
    document_version: str
    content_hash: str
    locator: dict[str, Any]
    quote: str = ""
    source_role: str


class FieldEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    evidence_id: str
    quote: str = ""


class CandidateEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "candidate-envelope-v1"
    candidate_id: str
    attempt_id: str
    run_id: str
    rule_revision: str
    proposition_id: str
    kind: str
    fields: dict[str, Any] = Field(default_factory=dict)
    field_evidence: dict[str, list[FieldEvidence] | list[str] | str] = Field(default_factory=dict)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    quote: str = ""
    uncertainty: list[str] = Field(default_factory=list)
    provider: str
    model: str
    prompt_revision: str
    status: str = "candidate"

    @field_validator("candidate_id", "attempt_id", "run_id", "rule_revision", "proposition_id")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("identifier must not be empty")
        return value


class SourceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    manifest_hash: str
    run_id: str
    mine_id: str
    check_cycle: str
    rule_revision: str
    files: list[dict[str, Any]] = Field(default_factory=list)
    coverage: list[dict[str, Any]] = Field(default_factory=list)
    source_complete: bool = False


class InspectionTask(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    mine_id: Annotated[str, Field(validation_alias=AliasChoices("mineId", "mine_id"), serialization_alias="mineId")]
    check_cycle: Annotated[str, Field(validation_alias=AliasChoices("checkCycle", "check_cycle"), serialization_alias="checkCycle")]
    items: list[dict[str, Any]]
    status: Literal["running", "pause", "resume"] = "running"
    task_id: Annotated[str | None, Field(default=None, validation_alias=AliasChoices("taskId", "task_id"), serialization_alias="taskId")]
    execution_id: Annotated[str | None, Field(default=None, validation_alias=AliasChoices("executionId", "execution_id"), serialization_alias="executionId")]
    callback_url: Annotated[str | None, Field(default=None, validation_alias=AliasChoices("resultCallbackUrl", "callback_url"), serialization_alias="resultCallbackUrl")]
    scope: dict[str, Any] | None = None

    @field_validator("mine_id", "check_cycle")
    @classmethod
    def required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("required text is empty")
        return value

    @field_validator("check_cycle")
    @classmethod
    def valid_cycle(cls, value: str) -> str:
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
            raise ValueError("checkCycle must use YYYY-MM")
        return value

    @field_validator("items")
    @classmethod
    def valid_items(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not value:
            raise ValueError("items must not be empty")
        codes = [item.get("itemCode") if isinstance(item, dict) else None for item in value]
        if any(not isinstance(code, str) or not code.strip() for code in codes):
            raise ValueError("each item requires itemCode")
        if len(codes) != len(set(codes)):
            raise ValueError("itemCode values must be unique")
        return value


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    item_code: str
    status: ResultStatus
    decision_class: str | None = None
    decision_summary: str = ""
    processing_state: str | None = None
    classification: str | None = None
    description: str
    detail: str
    evidence_refs: list[str] = Field(default_factory=list)
    coverage_gaps: list[str] = Field(default_factory=list)
    finalized_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


def stable_id(*parts: object) -> str:
    return sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()
