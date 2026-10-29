"""Crash- and timeout-isolated native document parser."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

_MAX_OUTPUT_BYTES = 50 * 1024 * 1024
_DEFAULT_TIMEOUT = 120
_PARSE_SLOTS = threading.BoundedSemaphore(4)


def _child(payload: dict) -> dict:
    try:
        # Import only in the child. The API process does not load parser native
        # or extension libraries when isolation is enabled.
        from .documents import parse_file
        parsed = parse_file(Path(payload["path"]), file_id=payload["file_id"],
                            content_hash=payload["content_hash"], source_role=payload["source_role"])
        if not isinstance(parsed, dict) or not isinstance(parsed.get("blocks"), list) or not isinstance(parsed.get("coverage"), list):
            return {"ok": False, "error": "native_parser_schema_invalid"}
        return {"ok": True, "parsed": parsed}
    except ImportError:
        return {"ok": False, "error": "native_parser_dependency_missing"}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}


def _main() -> None:
    status_path = None
    try:
        payload = json.loads(sys.stdin.buffer.read(32769))
        status_path = payload.pop("status_path")
        result = _child(payload)
    except Exception:
        result = {"ok": False, "error": "native_parser_request_invalid"}
    if status_path:
        encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
        if len(encoded) > _MAX_OUTPUT_BYTES:
            encoded = b'{"ok":false,"error":"native_parser_output_budget_exceeded"}'
        Path(status_path).write_bytes(encoded)


def _command():
    return [sys.executable, "-m", "apps.control_api.native_parser", "--child"]


def _parse_isolated(path: Path, *, file_id: str, content_hash: str, source_role: str,
                    timeout: float, max_output_bytes: int) -> dict:
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
        return {"ok": False, "error": "native_parser_timeout_invalid"}
    if type(max_output_bytes) is not int or not 1 <= max_output_bytes <= _MAX_OUTPUT_BYTES:
        return {"ok": False, "error": "native_parser_output_budget_invalid"}
    with tempfile.TemporaryDirectory(prefix="compliance-parse-") as temporary:
        status = Path(temporary) / "status.json"
        payload = {"path": str(path), "file_id": file_id, "content_hash": content_hash,
                   "source_role": source_role, "status_path": str(status)}
        try:
            completed = subprocess.run(_command(),
                                       input=json.dumps(payload).encode("utf-8"), stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, timeout=timeout, check=False,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "native_parser_timeout"}
        except OSError:
            return {"ok": False, "error": "native_parser_child_failed"}
        if completed.returncode != 0:
            return {"ok": False, "error": "native_parser_child_crashed"}
        try:
            if not status.is_file() or status.stat().st_size > max_output_bytes:
                return {"ok": False, "error": "native_parser_output_budget_exceeded"}
            result = json.loads(status.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return {"ok": False, "error": "native_parser_response_invalid"}
        if not isinstance(result, dict) or result.get("ok") is not True or not isinstance(result.get("parsed"), dict):
            code = str(result.get("error") or "native_parser_failed") if isinstance(result, dict) else "native_parser_response_invalid"
            return {"ok": False, "error": code if code.isidentifier() and len(code) <= 80 else "native_parser_failed"}
        parsed = result["parsed"]
        if not isinstance(parsed.get("blocks"), list) or not isinstance(parsed.get("coverage"), list):
            return {"ok": False, "error": "native_parser_schema_invalid"}
        return {"ok": True, "parsed": parsed}


def parse_isolated(path: Path, *, file_id: str, content_hash: str, source_role: str,
                   timeout: float = _DEFAULT_TIMEOUT, max_output_bytes: int = _MAX_OUTPUT_BYTES) -> dict:
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
        return {"ok": False, "error": "native_parser_timeout_invalid"}
    started = time.monotonic()
    if not _PARSE_SLOTS.acquire(timeout=timeout):
        return {"ok": False, "error": "native_parser_busy"}
    try:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            return {"ok": False, "error": "native_parser_timeout"}
        return _parse_isolated(path, file_id=file_id, content_hash=content_hash,
                               source_role=source_role, timeout=remaining, max_output_bytes=max_output_bytes)
    except OSError:
        return {"ok": False, "error": "native_parser_io_failed"}
    finally:
        _PARSE_SLOTS.release()


if __name__ == "__main__":
    _main()
