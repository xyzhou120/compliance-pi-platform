"""Deployment configuration. Model servers are external; no model downloads."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _integer(name: str, default: int, minimum: int = 1, maximum: int = 1000) -> int:
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be in [{minimum},{maximum}]")
    return value


@dataclass(frozen=True)
class Settings:
    environment: str = "local"
    database_url: str = "sqlite:///.local/control.sqlite3"
    input_root: Path = Path(".local/inputs")
    storage_dir: Path = Path(".local/artifacts")
    source_config: Path | None = None
    internal_token: str = field(default="", repr=False)
    allowed_mines: tuple[str, ...] = ()
    pi_worker_url: str = "http://127.0.0.1:8093"
    pi_enabled: bool = False
    lease_seconds: int = 120
    task_timeout: int = 1800
    max_model_calls: int = 80
    max_work_attempts: int = 3
    provider_concurrency: int = 2
    provider_timeout: int = 120
    ocr_endpoint: str = ""
    ocr_provider: str = "http-json"
    ocr_authorization_env: str = "OCR_AUTHORIZATION"
    vision_endpoint: str = ""
    vision_model: str = ""
    vision_authorization_env: str = "VISION_AUTHORIZATION"
    callback_hosts: tuple[str, ...] = ()
    callback_allow_http: bool = False
    callback_allow_private: bool = True
    callback_allow_loopback: bool = False
    callback_timeout: int = 15
    callback_max_attempts: int = 3
    callback_auth: str = field(default="", repr=False)
    scope_proof_key: str = field(default="", repr=False)
    max_document_concurrency: int = 4
    max_total_pages: int = 5000
    max_provider_attempts: int = 2
    model_concurrency: int = 2
    evidence_review_key: str = field(default="", repr=False)
    evidence_review_receipts: Path | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        value = cls(
            environment=os.getenv("COMPLIANCE_ENVIRONMENT", "local"),
            database_url=os.getenv("COMPLIANCE_DATABASE_URL", os.getenv("COMPLIANCE_DB", "sqlite:///.local/control.sqlite3")),
            input_root=Path(os.getenv("COMPLIANCE_INPUT_ROOT", ".local/inputs")).resolve(),
            storage_dir=Path(os.getenv("COMPLIANCE_STORAGE_DIR", ".local/artifacts")).resolve(),
            source_config=Path(os.environ["COMPLIANCE_SOURCE_CONFIG"]).resolve() if os.getenv("COMPLIANCE_SOURCE_CONFIG") else None,
            internal_token=os.getenv("COMPLIANCE_INTERNAL_TOKEN", os.getenv("INTERNAL_TOKEN", "")),
            allowed_mines=tuple(x.strip() for x in os.getenv("COMPLIANCE_ALLOWED_MINES", "").split(",") if x.strip()),
            pi_worker_url=os.getenv("PI_WORKER_URL", "http://127.0.0.1:8093").rstrip("/"),
            pi_enabled=os.getenv("PI_ENABLED", "true" if os.getenv("COMPLIANCE_ENVIRONMENT") == "production" else "false").lower() == "true",
            lease_seconds=_integer("COMPLIANCE_LEASE_SECONDS", 120, maximum=3600),
            task_timeout=_integer("COMPLIANCE_TASK_TIMEOUT", 1800, maximum=86400),
            max_model_calls=_integer("PI_MAX_TOOL_CALLS", 80, maximum=10000),
            max_work_attempts=_integer("COMPLIANCE_MAX_WORK_ATTEMPTS", 3),
            provider_concurrency=_integer("MAX_OCR_CONCURRENCY", 2, maximum=64),
            provider_timeout=_integer("OCR_TIMEOUT_SECONDS", 120, maximum=1800),
            ocr_endpoint=os.getenv("OCR_ENDPOINT", ""),
            ocr_provider=os.getenv("OCR_PROVIDER", "http-json"),
            ocr_authorization_env=os.getenv("OCR_AUTHORIZATION_ENV", "OCR_AUTHORIZATION"),
            vision_endpoint=os.getenv("VISION_ENDPOINT", ""),
            vision_model=os.getenv("VISION_MODEL", ""),
            vision_authorization_env=os.getenv("VISION_AUTHORIZATION_ENV", "VISION_AUTHORIZATION"),
            callback_hosts=tuple(x.strip().lower() for x in os.getenv("CALLBACK_ALLOWED_HOSTS", "").split(",") if x.strip()),
            callback_allow_http=os.getenv("CALLBACK_ALLOW_HTTP", "false").lower() == "true",
            callback_allow_private=os.getenv("CALLBACK_ALLOW_PRIVATE", "true").lower() == "true",
            callback_allow_loopback=os.getenv("CALLBACK_ALLOW_LOOPBACK", "false").lower() == "true",
            callback_timeout=_integer("CALLBACK_TIMEOUT_SECONDS", 15, maximum=300),
            callback_max_attempts=_integer("CALLBACK_MAX_ATTEMPTS", 3, maximum=20),
            callback_auth=os.getenv("COMPLIANCE_CALLBACK_AUTHORIZATION", ""),
            scope_proof_key=os.getenv("COMPLIANCE_SCOPE_PROOF_KEY", ""),
            max_document_concurrency=_integer("MAX_DOCUMENT_CONCURRENCY", 4, maximum=32),
            max_total_pages=_integer("PI_MAX_TOTAL_PAGES", 5000, maximum=100000),
            max_provider_attempts=_integer("PROVIDER_MAX_ATTEMPTS", 2, maximum=5),
            model_concurrency=_integer("MAX_MODEL_CONCURRENCY", 2, maximum=64),
            evidence_review_key=os.getenv("COMPLIANCE_EVIDENCE_REVIEW_KEY", ""),
            evidence_review_receipts=Path(os.environ["COMPLIANCE_EVIDENCE_REVIEW_RECEIPTS"]).resolve() if os.getenv("COMPLIANCE_EVIDENCE_REVIEW_RECEIPTS") else None,
        )
        if value.environment not in {"local", "test", "production"}:
            raise ValueError("invalid COMPLIANCE_ENVIRONMENT")
        if value.environment == "production":
            if len(value.internal_token) < 16:
                raise ValueError("production requires internal token")
            if not value.database_url.startswith("postgresql"):
                raise ValueError("production requires PostgreSQL ledger")
        return value

    def input_config(self) -> dict:
        if not self.source_config:
            return {}
        data = json.loads(self.source_config.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("source configuration must be an object")
        return data

    def document_settings(self) -> dict:
        return {"input_root": str(self.input_root), "storage_dir": str(self.storage_dir), "max_file_bytes": 50 * 1024 * 1024}

    def provider_settings(self) -> dict:
        if self.ocr_endpoint:
            result = {"endpoint": self.ocr_endpoint, "provider": self.ocr_provider,
                    "authorization_env": self.ocr_authorization_env, "max_concurrency": self.provider_concurrency,
                    "timeout": self.provider_timeout, "max_attempts": self.max_provider_attempts,
                    "max_total_pages": self.max_total_pages}
            if self.vision_endpoint:
                endpoint = self.vision_endpoint.rstrip("/")
                result["fallback_provider"] = {"endpoint": endpoint if endpoint.endswith("/chat/completions") else endpoint + "/chat/completions",
                    "provider": "openai-compatible", "model": self.vision_model,
                    "authorization_env": self.vision_authorization_env, "timeout": self.provider_timeout}
            return result
        endpoint = self.vision_endpoint.rstrip("/")
        if endpoint and not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        return {"endpoint": endpoint, "provider": "openai-compatible", "model": self.vision_model,
                "authorization_env": self.vision_authorization_env, "max_concurrency": self.provider_concurrency,
                "timeout": self.provider_timeout, "max_attempts": self.max_provider_attempts, "max_total_pages": self.max_total_pages}
