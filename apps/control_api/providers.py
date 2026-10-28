from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib import request
from urllib.parse import urlsplit
from uuid import uuid4


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("provider_redirect_rejected")


class ProviderPool:
    """External OCR/VLM calls only; no model downloads or local weights."""

    def __init__(self, *, max_concurrency: int = 2):
        if type(max_concurrency) is not int or max_concurrency < 1:
            raise ValueError("max_concurrency must be a positive integer")
        self._semaphore = threading.BoundedSemaphore(max_concurrency)
        self.max_concurrency = max_concurrency

    def call(self, url: str, payload: dict | bytes, *, timeout: float = 60, transport=None, authorization: str = "", content_type: str = "application/json", cancelled=None) -> dict:
        started = time.monotonic()
        while not self._semaphore.acquire(timeout=0.05):
            if callable(cancelled) and cancelled():
                raise RuntimeError("provider_cancelled")
        try:
            if callable(cancelled) and cancelled():
                raise RuntimeError("provider_cancelled")
            queue_ms = round((time.monotonic() - started) * 1000, 2)
            if transport:
                response = transport(url, payload, timeout)
            else:
                parsed = urlsplit(url)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                    raise ValueError("invalid_provider_endpoint")
                body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
                headers = {"Content-Type": content_type, "Accept": "application/json"}
                if authorization:
                    headers["Authorization"] = authorization
                req = request.Request(url, data=body, headers=headers, method="POST")
                opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())
                with opener.open(req, timeout=timeout) as raw:  # noqa: S310 - endpoint is deployment config
                    content = raw.read(8 * 1024 * 1024 + 1)
                    if len(content) > 8 * 1024 * 1024:
                        raise ValueError("provider_response_too_large")
                    response = json.loads(content.decode("utf-8"))
        finally:
            self._semaphore.release()
        if not isinstance(response, dict):
            raise ValueError("provider response must be an object")
        return {**response, "elapsed_ms": round((time.monotonic() - started) * 1000, 2), "queue_ms": queue_ms}


def _normalize(result: dict) -> dict:
    if "rec_texts" in result:
        texts = result["rec_texts"]
        if not isinstance(texts, list) or any(not isinstance(line, str) for line in texts):
            raise ValueError("ocr_lines_invalid")
        polygons = result.get("rec_polys", result.get("dt_polys", []))
        blocks = [{"text": text, **({"bbox": polygons[index]} if isinstance(polygons, list) and index < len(polygons) else {})} for index, text in enumerate(texts)]
        return {**result, "text": "\n".join(texts), "blocks": blocks}
    if "choices" in result:
        choices = result.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise ValueError("vision_choices_invalid")
        content = choices[0].get("message", {}).get("content", "")
        if isinstance(content, str):
            content = json.loads(content)
        if not isinstance(content, dict):
            raise ValueError("vision_content_invalid")
        return {**content, "queue_ms": result.get("queue_ms", 0)}
    return result


def _retryable(error: Exception) -> bool:
    from urllib.error import HTTPError, URLError
    if isinstance(error, HTTPError):
        return error.code in {408, 429} or error.code >= 500
    return isinstance(error, (TimeoutError, ConnectionError, OSError, URLError)) or str(error) == "provider_http_5xx"


def recognize_pages(pages: list[dict], *, settings: dict, pool: ProviderPool | None = None, transport=None) -> dict:
    """Retain independent transcriptions and attempts; never publish source facts.

    Cancellation prevents publication and cache writes. An already in-flight
    synchronous call retains its permit until the transport actually returns.
    """
    if not pages:
        return {"candidates": [], "attempts": [], "errors": []}
    pool = pool or ProviderPool(max_concurrency=int(settings.get("max_concurrency", 2)))
    if not settings.get("endpoint"):
        return {"candidates": [], "attempts": [], "errors": ["provider_endpoint_missing"]}
    max_attempts = int(settings.get("max_attempts", 2))
    max_pages = int(settings.get("max_total_pages", len(pages)))
    if not 1 <= max_attempts <= 10 or not 1 <= max_pages <= 10000:
        raise ValueError("invalid_provider_budget")
    selected, omitted = pages[:max_pages], pages[max_pages:]
    cancelled = settings.get("cancelled")
    def is_cancelled():
        return callable(cancelled) and bool(cancelled())

    def low_quality(result):
        confidence = result.get("confidence")
        return (not str(result.get("text", "")).strip()
                or bool(result.get("quality_flags") or result.get("qualityFlags"))
                or (type(confidence) in {int, float} and confidence < float(settings.get("min_confidence", 0.7))))

    def payload_for(page, profile):
        encoded = base64.b64encode(page["image_bytes"]).decode("ascii")
        payload = {"file_id": page["file_id"], "page": page.get("page"), "image_base64": encoded}
        provider, content_type = profile.get("provider"), "application/json"
        if provider == "multipart-http":
            boundary = "compliance-" + uuid4().hex
            prefix = f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="page.png"\r\nContent-Type: image/png\r\n\r\n'
            payload = prefix.encode() + page["image_bytes"] + f"\r\n--{boundary}--\r\n".encode()
            content_type = f"multipart/form-data; boundary={boundary}"
        elif provider == "openai-compatible":
            if not profile.get("model"):
                raise ValueError("vision_model_missing")
            payload = {"model": profile["model"], "messages": [{"role": "user", "content": [
                {"type": "text", "text": 'Transcribe visible source text only. Return JSON {"text":"...","blocks":[{"text":"...","bbox":[x1,y1,x2,y2]}]}. Leave unreadable text empty. Do not infer events, approvals, signatures, units or compliance.'},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}}]}],
                "response_format": {"type": "json_object"}, "temperature": 0, "max_tokens": int(profile.get("max_tokens", 8192))}
        return payload, content_type

    def read_cache(page, profile):
        identity = {"image_hash": hashlib.sha256(page["image_bytes"]).hexdigest(), "endpoint": profile.get("endpoint"),
                    "model": profile.get("model"), "provider": profile.get("provider", "http-json"),
                    "max_tokens": profile.get("max_tokens", 8192), "profile_revision": profile.get("profile_revision", "v1"), "contract": "external-ocr-v2"}
        directory = profile.get("cache_dir", settings.get("cache_dir"))
        path = Path(directory) / (hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest() + ".json") if directory else None
        if path:
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
                content = json.dumps(envelope["result"], ensure_ascii=False, sort_keys=True)
                if (envelope["hash"] == hashlib.sha256(content.encode()).hexdigest()
                        and isinstance(envelope["result"], dict)
                        and isinstance(envelope["result"].get("text", ""), str)
                        and isinstance(envelope["result"].get("blocks", []), list)):
                    return path, envelope["result"]
            except (OSError, ValueError, KeyError, TypeError):
                pass
        return path, None

    def one(page):
        own_attempts, results = [], []
        base = {"file_id": page.get("file_id"), "page": page.get("page")}
        if not page.get("image_bytes"):
            return [], [{**base, "status": "failed", "error": "image_bytes_missing"}]
        profiles = [(settings, "primary")]
        fallback = settings.get("fallback_provider")
        if isinstance(fallback, dict) and fallback.get("endpoint"):
            profiles.append((fallback, "fallback"))
        for profile, phase in profiles:
            if is_cancelled():
                own_attempts.append({**base, "status": "cancelled", "phase": phase})
                break
            if phase == "fallback" and results and not low_quality(results[-1][0]):
                break
            label = profile.get("profile_id") or profile.get("provider") or ("external" if phase == "primary" else "fallback")
            cache_path, result = read_cache(page, profile)
            if result is not None:
                own_attempts.append({**base, "provider": label, "phase": phase, "attempt": 0, "status": "cached", "queue_ms": 0})
            else:
                for number in range(1, max_attempts + 1):
                    if is_cancelled():
                        own_attempts.append({**base, "provider": label, "phase": phase, "attempt": number, "status": "cancelled"})
                        break
                    started = time.monotonic()
                    try:
                        payload, content_type = payload_for(page, profile)
                        token_env = profile.get("authorization_env", "")
                        authorization = os.environ.get(token_env, "") if token_env else ""
                        result = _normalize(pool.call(profile["endpoint"], payload, timeout=float(profile.get("timeout", 60)), transport=transport, authorization=authorization, content_type=content_type, cancelled=is_cancelled))
                        if not isinstance(result.get("text", ""), str) or not isinstance(result.get("blocks", []), list):
                            raise ValueError("provider_schema_invalid")
                        if type(result.get("status")) is int and result["status"] >= 500:
                            raise RuntimeError("provider_http_5xx")
                        own_attempts.append({**base, "provider": label, "phase": phase, "attempt": number, "status": "cancelled" if is_cancelled() else "ok",
                                             "elapsed_ms": round((time.monotonic() - started) * 1000, 2), "queue_ms": result.get("queue_ms", 0), "quality_review": low_quality(result)})
                        if not is_cancelled() and cache_path and not low_quality(result):
                            cache_path.parent.mkdir(parents=True, exist_ok=True)
                            content = json.dumps(result, ensure_ascii=False, sort_keys=True)
                            with tempfile.NamedTemporaryFile(dir=cache_path.parent, suffix=".part", mode="w", encoding="utf-8", delete=False) as stream:
                                json.dump({"result": result, "hash": hashlib.sha256(content.encode()).hexdigest()}, stream, ensure_ascii=False)
                                temporary = Path(stream.name)
                            os.replace(temporary, cache_path)
                        break
                    except Exception as exc:
                        result = None
                        retry = _retryable(exc) and number < max_attempts and not is_cancelled()
                        own_attempts.append({**base, "provider": label, "phase": phase, "attempt": number, "status": "retry" if retry else "failed", "error": type(exc).__name__, "retryable": _retryable(exc), "elapsed_ms": round((time.monotonic() - started) * 1000, 2)})
                        if not retry:
                            break
                        # Retry backoff is short, bounded, and cancellation aware.
                        deadline = time.monotonic() + min(0.1 * number, 0.5)
                        while time.monotonic() < deadline and not is_cancelled():
                            time.sleep(min(0.02, max(0, deadline - time.monotonic())))
            if result is not None and not is_cancelled():
                results.append((result, label))
        if is_cancelled():
            return [], own_attempts
        texts = {str(result.get("text", "")).strip() for result, _ in results if str(result.get("text", "")).strip()}
        conflict = len(texts) > 1
        candidates = [{**base, "text": result.get("text", ""), "blocks": result.get("blocks", []), "quality": "candidate", "provider": label,
                       "needs_review": low_quality(result) or conflict, "conflicted": conflict, "quality_flags": result.get("quality_flags", result.get("qualityFlags", []))}
                      for result, label in results]
        return candidates, own_attempts

    outcomes = []
    with ThreadPoolExecutor(max_workers=pool.max_concurrency) as executor:
        outcomes.extend(executor.map(one, selected))
    candidates = [candidate for outcome, _ in outcomes for candidate in outcome]
    attempts = [attempt for _, own_attempts in outcomes for attempt in own_attempts]
    # Failed attempts remain in the trace even if a later retry/provider
    # succeeds. Only unresolved pages become execution errors.
    errors = [attempt for own_candidates, own_attempts in outcomes if not own_candidates
              for attempt in own_attempts[-1:] if attempt["status"] in {"failed", "cancelled"}]
    errors.extend({"file_id": page.get("file_id"), "page": page.get("page"), "status": "skipped", "error": "page_budget_exhausted"} for page in omitted)
    return {"candidates": candidates, "attempts": attempts, "errors": errors}
