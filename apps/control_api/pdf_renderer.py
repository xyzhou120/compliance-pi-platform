"""Crash-isolated PDFium page rendering; the API never imports PDFium.

Every PDF page has a short-lived child, deadline and bounded PNG artifact. A
native crash is a page failure, not a termination of the API hosting the run.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time


_RENDER_TIMEOUT_SECONDS = 45
_MAX_PIXELS = 40_000_000
_MAX_PNG_BYTES = 50 * 1024 * 1024
_RENDER_SLOTS = threading.BoundedSemaphore(2)


def _child_render(path: str, output_path: str, page_number: int, dpi: int, max_pixels: int) -> dict:
    document = page = bitmap = None
    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(path)
        if page_number > len(document):
            return {"ok": False, "error": "page_out_of_range"}
        page = document[page_number - 1]
        width, height = page.get_size()
        if width <= 0 or height <= 0 or width * height * (dpi / 72) ** 2 > max_pixels:
            return {"ok": False, "error": "page_pixel_budget_exceeded"}
        bitmap = page.render(scale=dpi / 72)
        with bitmap.to_pil() as image:
            image.save(output_path, format="PNG")
        if Path(output_path).stat().st_size > _MAX_PNG_BYTES:
            return {"ok": False, "error": "page_output_budget_exceeded"}
        return {"ok": True}
    except ImportError:
        return {"ok": False, "error": "page_renderer_unavailable"}
    except Exception as exc:
        # Paths and native exception messages must not enter tool responses.
        return {"ok": False, "error": type(exc).__name__}
    finally:
        for resource in (bitmap, page, document):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass


def _command() -> list[str]:
    # Execute the module file directly: spawn must not re-import uvicorn,
    # worker entry points or a caller's unguarded __main__ (especially Windows).
    return [sys.executable, str(Path(__file__).resolve()), "--child"]


def render_pdf_page(path: str | Path, page_number: int, *, dpi: int = 120,
                    max_pixels: int = _MAX_PIXELS, timeout: float = _RENDER_TIMEOUT_SECONDS) -> dict:
    """Return PNG bytes or an isolated, safe failure for precisely one page."""
    if type(page_number) is not int or page_number < 1:
        return {"ok": False, "error": "page_not_renderable"}
    if type(dpi) is not int or not 1 <= dpi <= 600:
        return {"ok": False, "error": "invalid_render_dpi"}
    if type(max_pixels) is not int or not 1 <= max_pixels <= _MAX_PIXELS:
        return {"ok": False, "error": "invalid_pixel_budget"}
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
        return {"ok": False, "error": "invalid_render_timeout"}
    deadline = time.monotonic() + timeout
    if not _RENDER_SLOTS.acquire(timeout=timeout):
        return {"ok": False, "error": "pdf_renderer_busy"}
    try:
        with tempfile.TemporaryDirectory(prefix="compliance-pdf-") as temporary:
            output = Path(temporary) / "page.png"
            status = Path(temporary) / "status.json"
            payload = {"path": str(Path(path)), "output_path": str(output), "page_number": page_number,
                       "dpi": dpi, "max_pixels": max_pixels, "status_path": str(status)}
            completed = None
            for attempt in range(2):
                try:
                    # run() kills and waits on timeout; a crashed child is retried
                    # once because PDFium crashes have been observed as transient.
                    completed = subprocess.run(_command(), input=json.dumps(payload).encode("utf-8"),
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=max(0.01, deadline-time.monotonic()),
                                               check=False, close_fds=True,
                                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                except subprocess.TimeoutExpired:
                    return {"ok": False, "error": "pdf_renderer_timeout"}
                except OSError:
                    return {"ok": False, "error": "pdf_renderer_child_failed"}
                if completed.returncode == 0 or attempt == 1:
                    break
            if completed.returncode != 0:
                return {"ok": False, "error": "pdf_renderer_child_crashed"}
            try:
                result = json.loads(status.read_bytes()) if status.is_file() and status.stat().st_size <= 4096 else None
            except (OSError, ValueError, UnicodeDecodeError):
                result = None
            if not isinstance(result, dict):
                return {"ok": False, "error": "pdf_renderer_response_invalid"}
            if result.get("ok") is not True:
                code = str(result.get("error") or "pdf_renderer_failed")
                return {"ok": False, "error": code if code.isidentifier() and len(code) <= 80 else "pdf_renderer_failed"}
            if not output.is_file() or output.stat().st_size > _MAX_PNG_BYTES:
                return {"ok": False, "error": "page_output_budget_exceeded"}
            image_bytes = output.read_bytes()
            if not image_bytes.startswith(bytes([137,80,78,71,13,10,26,10])):
                return {"ok": False, "error": "page_output_invalid"}
            return {"ok": True, "image_bytes": image_bytes}
    except OSError:
        return {"ok": False, "error": "pdf_renderer_io_failed"}
    finally:
        _RENDER_SLOTS.release()


def _main() -> None:
    status_path = None
    try:
        payload = json.loads(sys.stdin.buffer.read(32769))
        status_path = payload.pop("status_path")
        result = _child_render(**payload)
    except Exception:
        result = {"ok": False, "error": "pdf_renderer_request_invalid"}
    if status_path:
        Path(status_path).write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    _main()
