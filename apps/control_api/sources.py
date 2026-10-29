"""Server-owned source routing and immutable acquisition receipts."""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
from typing import Any, Callable

from .acquisition import AcquisitionError, download_attachment, query_oceanbase


class SourceRouteError(ValueError):
    pass


_ROLES = {"actual_record", "requirement", "statement", "regulation"}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
                      default=lambda item: item.isoformat() if isinstance(item, (date, datetime)) else str(item)).encode("utf-8")


def _hash(value: Any) -> str:
    return sha256(_canonical(value)).hexdigest()


def _setting(settings: Any, name: str, default=None):
    if isinstance(settings, dict):
        return settings.get(name, default)
    return getattr(settings, name, default)


def _config(settings: Any) -> dict:
    if isinstance(settings, dict):
        value = settings.get("source_config") or settings.get("config") or {}
    else:
        value = settings.input_config() if hasattr(settings, "input_config") else {}
    return value if isinstance(value, dict) else {}


def _root(settings: Any) -> Path:
    value = _setting(settings, "input_root", ".local/inputs")
    return Path(value).resolve()


def _storage(settings: Any) -> Path:
    value = _setting(settings, "storage_dir", ".local/artifacts")
    return Path(value).resolve()


def _resolve_under(root: Path, value: str) -> Path:
    target = (root / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
    if root != target and root not in target.parents:
        raise SourceRouteError("manifest_outside_input_root")
    return target


def _local_manifest(run: dict, settings: Any, config: dict) -> dict:
    manifests = config.get("manifests") or {}
    selected = (manifests.get(run["mine_id"], {}) if isinstance(manifests, dict) else {})
    if isinstance(selected, dict):
        selected = selected.get(run["check_cycle"], selected.get(run["item_code"]))
    if isinstance(selected, dict):
        selected = selected.get("manifest") or selected.get("path")
    if not isinstance(selected, str) or not selected.strip():
        raise SourceRouteError("server_manifest_not_configured")
    manifest = _resolve_under(_root(settings), selected)
    if not manifest.is_file():
        raise SourceRouteError("server_manifest_missing")
    return {**run, "scope": {"manifest": str(manifest)}, "source_input_root": str(_root(settings))}


def _safe_name(value: str, fallback: str) -> str:
    name = Path(value or fallback).name
    name = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", name).strip("._")
    return name[:180] or fallback


def _attachment_url(row: dict, config: dict) -> str | None:
    for key in ("attachment_link", "attachment_url", "local_attachment_address", "attachment_address", "minio_address", "url"):
        value = row.get(key)
        if isinstance(value, str) and value.startswith(("https://", "http://")):
            parsed = urlsplit(value)
            if key == "attachment_link":
                # The SQL concatenates a raw object key, so '?' and '#' are
                # filename characters rather than URL query/fragment syntax.
                raw_path = parsed.path + ("?" + parsed.query if parsed.query else "") + ("#" + parsed.fragment if parsed.fragment else "")
                return urlunsplit((parsed.scheme, parsed.netloc, quote(raw_path, safe="/"), "", ""))
            return urlunsplit((parsed.scheme, parsed.netloc, quote(parsed.path, safe="/%"), parsed.query, parsed.fragment))
    base = config.get("attachment_base_url") or config.get("attachmentBaseUrl")
    bucket = row.get("local_bucket_name") or row.get("bucket_name")
    key = row.get("local_attachment_address") or row.get("attachment_address")
    if not (isinstance(base, str) and isinstance(bucket, str) and isinstance(key, str) and bucket and key):
        return None
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise SourceRouteError("attachment_base_url_invalid")
    if any(part in {"", ".", ".."} for part in bucket.replace("\\", "/").split("/")) or "/" in bucket or "\\" in key or any(part in {"", ".", ".."} for part in key.split("/")):
        raise SourceRouteError("attachment_object_path_invalid")
    return base.rstrip("/") + "/" + quote(bucket, safe="") + "/" + quote(key.lstrip("/"), safe="/")


def _receipt(path: Path, payload: dict) -> dict:
    encoded = _canonical(payload)
    digest = sha256(encoded).hexdigest()
    if path.exists():
        current = json.loads(path.read_text(encoding="utf-8"))
        if current.get("receipt_hash") != digest or current.get("payload") != payload:
            raise SourceRouteError("frozen_receipt_conflict")
        return current
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"receipt_hash": digest, "payload": payload}, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"receipt_hash": digest, "payload": payload}


def _catalog_entry(config: dict, item_code: str, file_name_id: str) -> dict:
    for entry in config.get("catalog", {}).get(item_code, []):
        if str(entry.get("file_name_id")) == str(file_name_id):
            return entry
    return {}


def _remote_manifest(run: dict, settings: Any, config: dict, *, query_fn: Callable[..., dict], download_fn: Callable[..., dict]) -> dict:
    if not re.fullmatch(r"[\w.-]+", str(run["run_id"])):
        raise SourceRouteError("invalid_run_id")
    root = _storage(settings) / "acquisition" / run["run_id"]
    source_root = root / "source"
    source_root.mkdir(parents=True, exist_ok=True)
    query_path = root / "query.json"
    if query_path.exists():
        receipt = json.loads(query_path.read_text(encoding="utf-8"))
        if receipt.get("receipt_hash") != _hash(receipt.get("payload")):
            raise SourceRouteError("query_receipt_hash_mismatch")
        query = receipt.get("payload", {}).get("query")
        if not isinstance(query, dict):
            raise SourceRouteError("invalid_frozen_query_receipt")
    else:
        query_config = {**config, **config.get("oceanbase", {})}
        query = query_fn(mine_id=run["mine_id"], check_cycle=run["check_cycle"], item_code=run["item_code"], config=query_config)
        query = json.loads(_canonical(query))
        if not isinstance(query, dict) or not isinstance(query.get("rows"), list):
            raise SourceRouteError("invalid_source_query")
        _receipt(query_path, {"run_id": run["run_id"], "mine_id": run["mine_id"], "check_cycle": run["check_cycle"], "item_code": run["item_code"], "query": query})
    rows = query.get("rows", [])
    if not rows:
        raise SourceRouteError("no_source_files_for_inspection")
    files, failures, excluded, seen_ids, seen_meta = [], [], [], set(), {}
    file_dir = source_root / "files"
    for row in rows:
        aid = str(row.get("attachment_id") or "")
        metadata_key = _hash({key: row.get(key) for key in ("attachment_name", "attachment_url", "attachment_address", "local_attachment_address", "local_bucket_name", "bucket_name", "file_name_id", "report_date", "report_time")})
        if aid and aid in seen_meta and seen_meta[aid] != metadata_key:
            raise SourceRouteError("attachment_id_metadata_conflict")
        seen_meta[aid] = metadata_key
    for row_index, row in enumerate(rows):
        aid = str(row.get("attachment_id") or "")
        if not aid:
            failures.append({"row": row_index, "reason": "attachment_id_missing"})
            continue
        metadata_key = _hash({key: row.get(key) for key in ("attachment_name", "attachment_url", "attachment_address", "local_attachment_address", "local_bucket_name", "bucket_name", "file_name_id", "report_date", "report_time")})
        if aid in seen_ids:
            continue
        seen_ids.add(aid)
        url = _attachment_url(row, config)
        if not isinstance(url, str) or not url:
            failures.append({"attachment_id": aid, "reason": "attachment_url_missing"})
            continue
        merged_config = {**config, **config.get("oceanbase", {})}
        catalog = _catalog_entry(merged_config, run["item_code"], str(row.get("file_name_id")))
        cadence = catalog.get("cadence", "unknown")
        report_date = str(row.get("report_date") or row.get("report_time") or "").replace("/", "-")
        try:
            observed_month = date.fromisoformat(report_date[:10]).strftime("%Y-%m")
        except ValueError:
            observed_month = None
        if cadence in {"daily", "monthly", "weekly"} and observed_month and observed_month != run["check_cycle"]:
            excluded.append({"attachment_id": aid, "reason": "outside_check_cycle", "report_date": row.get("report_date")})
            continue
        role = catalog.get("source_role", catalog.get("role", "statement"))
        if role not in _ROLES:
            raise SourceRouteError("unknown_source_role")
        name = _safe_name(str(row.get("attachment_name") or aid), aid)
        stable_aid = sha256(aid.encode()).hexdigest()[:24]
        target = file_dir / f"{stable_aid}-{name}"
        file_receipt_path = root / "receipts" / f"{stable_aid}.json"
        try:
            if file_receipt_path.exists():
                saved = json.loads(file_receipt_path.read_text(encoding="utf-8"))
                receipt = saved["payload"]
                if saved.get("receipt_hash") != _hash(receipt) or receipt.get("url") != url or not target.is_file():
                    raise SourceRouteError("attachment_receipt_mismatch")
                if sha256(target.read_bytes()).hexdigest() != receipt["content_hash"]:
                    raise SourceRouteError("frozen_attachment_hash_mismatch")
            else:
                receipt = download_fn(url, target, allowed_hosts=tuple(config.get("attachment_allowed_hosts", config.get("allowed_hosts", []))),
                                      max_bytes=int(config.get("max_attachment_bytes", 50 * 1024 * 1024)), allow_http=bool(config.get("allow_http", False)),
                                      allow_private=bool(config.get("allow_private", True)), allow_loopback=bool(config.get("allow_loopback", False)),
                                      expected_hash=row.get("content_hash"),
                                      verify_tls=bool(config.get("attachment_tls_verify", True)))
                receipt["url"] = url
                _receipt(file_receipt_path, receipt)
            files.append({"path": str(Path(receipt["path"]).relative_to(source_root)), "file_id": aid,
                          "attachment_id": aid, "attachment_url": url, "original_url": url,
                          "original_name": row.get("attachment_name") or name, "content_hash": receipt["content_hash"],
                          "byte_size": receipt["byte_size"], "source_role": role, "material_group": catalog.get("material_group", row.get("material_group")),
                          "report_date": row.get("report_date") or row.get("report_time"),
                          "report_time_year": row.get("report_time_year"), "report_time_month": row.get("report_time_month"),
                          "source_metadata": {"row_index": row_index, "file_name_id": row.get("file_name_id"),
                              **{key: row[key] for key in ("logical_source_key", "coverage_period", "scope_key") if key in row}}})
        except (AcquisitionError, OSError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, AcquisitionError) else type(exc).__name__
            if not re.fullmatch(r"[A-Za-z0-9_:.-]{1,120}", reason):
                reason = type(exc).__name__
            failures.append({"attachment_id": aid, "reason": f"acquisition_failed:{reason}"})
    manifest = {"run_id": run["run_id"], "mine_id": run["mine_id"], "mine_code": run["mine_id"], "environment": run.get("environment", "production"),
                "period": {"start": f"{run['check_cycle']}-01", "label": run["check_cycle"]}, "source_complete": False,
                "files": files, "failures": failures, "excluded": excluded, "scope_proof": False}
    manifest_path = source_root / "manifest.json"
    # Acquisition checkpoints may gain successful formerly-failed attachments;
    # query and individual successful file receipts remain immutable.
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if not files:
        raise SourceRouteError("source_attachments_unavailable" if failures else "no_source_files_for_inspection")
    return {**run, "scope": {"manifest": str(manifest_path)}, "source_input_root": str(source_root),
            "acquisition": {"query_receipt": str(query_path), "failures": failures, "source_complete": False}}


def prepare_source(run: dict, settings: Any, checkpoint=None, *, query_fn=query_oceanbase, download_fn=download_attachment) -> dict:
    """Resolve operator-owned source mode without accepting upstream paths."""
    config = _config(settings)
    mode = config.get("mode", config.get("source_mode", "local_manifest"))
    if mode == "local_manifest":
        selected = _local_manifest(run, settings, config)
    elif mode == "oceanbase":
        selected = _remote_manifest(run, settings, config, query_fn=query_fn, download_fn=download_fn)
    else:
        raise SourceRouteError("unsupported_source_mode")
    # The receipt is operator configuration, never a field accepted from task
    # scope. Keep it separate from manifest bytes so its signed manifest hash
    # does not recursively include the signature itself. Coverage verifies it.
    selected.pop("scope_proof_receipt", None)
    receipt_path = config.get("authority_receipt_file")
    if receipt_path:
        if not isinstance(receipt_path, str):
            raise SourceRouteError("invalid_authority_receipt_file")
        path = _resolve_under(_root(settings), receipt_path)
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise SourceRouteError("authority_receipt_unavailable")
        try:
            proof = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise SourceRouteError("authority_receipt_invalid") from exc
        if not isinstance(proof, dict):
            raise SourceRouteError("authority_receipt_invalid")
        selected["scope_proof_receipt"] = proof
    return selected
