"""Bounded attachment download and explicit read-only catalog adapter."""

from __future__ import annotations

import hashlib
import http.client
import importlib
import json
import os
import re
import socket
import ssl
import tempfile
from pathlib import Path
from datetime import date
from urllib.parse import urlsplit

from .callbacks import CallbackPolicy, DeliveryError


class AcquisitionError(ValueError):
    pass


def download_attachment(url: str, destination: Path, *, allowed_hosts: tuple[str, ...], max_bytes: int = 50 * 1024 * 1024,
                        timeout: float = 30, allow_http: bool = False, allow_private: bool = False,
                        allow_loopback: bool = False, expected_hash: str | None = None,
                        verify_tls: bool = True) -> dict:
    """Connect only to a resolved allowlisted address and never follow redirects."""
    policy = CallbackPolicy(allowed_hosts, allow_http=allow_http, allow_private=allow_private, allow_loopback=allow_loopback)
    try:
        address, port = policy.addresses(url)
    except DeliveryError as exc:
        raise AcquisitionError(str(exc)) from exc
    parsed = urlsplit(url)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    raw = None
    connection = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)
    try:
        raw = socket.create_connection((address, port), timeout=timeout)
        if parsed.scheme == "https":
            # Internal object stores may expose a private certificate whose SAN
            # does not contain the allowlisted service IP. Host allowlisting is
            # still mandatory; TLS verification is independently configurable.
            context = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()
            raw = context.wrap_socket(raw, server_hostname=parsed.hostname)
        connection.sock = raw
        connection.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""), headers={"Accept": "application/octet-stream"})
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            raise AcquisitionError("attachment_http_rejected")
        if response.getheader("Content-Length") and int(response.getheader("Content-Length")) > max_bytes:
            raise AcquisitionError("attachment_too_large")
        digest, size = hashlib.sha256(), 0
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as stream:
            temporary = Path(stream.name)
            while chunk := response.read(min(1024 * 1024, max_bytes + 1 - size)):
                size += len(chunk)
                if size > max_bytes:
                    raise AcquisitionError("attachment_too_large")
                digest.update(chunk)
                stream.write(chunk)
        content_hash = digest.hexdigest()
        if expected_hash and expected_hash != content_hash:
            raise AcquisitionError("attachment_hash_mismatch")
        magic = {".pdf": b"%PDF-", ".docx": b"PK", ".xlsx": b"PK", ".xlsm": b"PK"}.get(destination.suffix.lower())
        if magic and not temporary.read_bytes()[:len(magic)].startswith(magic):
            raise AcquisitionError("attachment_magic_mismatch")
        os.replace(temporary, destination)
        temporary = None
        return {"url": url, "content_hash": content_hash, "byte_size": size, "path": str(destination)}
    except ssl.SSLCertVerificationError as exc:
        raise AcquisitionError("attachment_tls_certificate_failed") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise AcquisitionError(f"attachment_transport_failed:{type(exc).__name__}") from exc
    finally:
        connection.close()
        if raw:
            raw.close()
        if temporary:
            temporary.unlink(missing_ok=True)


def query_oceanbase(*, mine_id: str, check_cycle: str, item_code: str, config: dict, connection_factory=None) -> dict:
    """Only parameterized SELECT against an operator-configured read-only view.

    The required view schema is explicit. Actual production DDL/driver remains
    a deployment integration gate; this does not discover or guess columns.
    """
    catalog = config.get("catalog", {}).get(item_code)
    if not isinstance(catalog, list) or not catalog:
        raise AcquisitionError("inspection_catalog_missing")
    if any(entry.get("cadence") not in {"daily", "monthly", "weekly", "annual", "semiannual", "half_year", "persistent"} for entry in catalog):
        raise AcquisitionError("catalog_cadence_policy_missing")
    table = config.get("table", "")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", table):
        raise AcquisitionError("catalog_view_rejected")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", check_cycle):
        raise AcquisitionError("check_cycle_invalid")
    ids = [str(entry["file_name_id"]) for entry in catalog]
    if connection_factory is None:
        if config.get("compatibility_mode") != "mysql":
            raise AcquisitionError("oceanbase_mysql_mode_required")
        driver = importlib.import_module(config["driver_module"])
        env = config.get("env", {})
        connection_factory = lambda: driver.connect(host=os.environ[env["host"]], port=int(os.environ[env["port"]]),
            database=os.environ[env["database"]], user=os.environ[env["user"]], password=os.environ[env["password"]],
            connect_timeout=int(config.get("connect_timeout", 10)))
    year, month = map(int, check_cycle.split("-"))
    start = date(year, month, 1).isoformat()
    end = date(year + (month == 12), month % 12 + 1, 1).isoformat()
    by_cadence = {}
    for entry in catalog:
        cadence = "semiannual" if entry.get("cadence") == "half_year" else entry.get("cadence", "monthly")
        by_cadence.setdefault(cadence, []).append(str(entry["file_name_id"]))
    clauses, parameters = [], [mine_id, *ids]
    for cadence, cadence_ids in by_cadence.items():
        marks = ",".join(["%s"] * len(cadence_ids))
        if cadence == "annual":
            clauses.append(f"(file_name_id IN ({marks}) AND report_time_year = %s)")
            parameters.extend([*cadence_ids, year])
        elif cadence == "semiannual":
            clauses.append(f"(file_name_id IN ({marks}) AND report_time_year = %s AND report_time_month IN (%s, %s))")
            parameters.extend([*cadence_ids, year, "01", "07"])
        else:
            clauses.append(f"(file_name_id IN ({marks}) AND report_time_year = %s AND report_time_month = %s)")
            parameters.extend([*cadence_ids, year, f"{month:02d}"])
    max_rows = config.get("max_query_rows", 5000)
    if type(max_rows) is not int or not 1 <= max_rows <= 100000:
        raise AcquisitionError("invalid_query_row_budget")
    # Requirements keep their historical versions for validity matching.
    # Actual monthly/daily records use the confirmed comparable date column.
    attachment_base = str(config.get("attachment_base_url", "https://172.26.10.67:8082/")).rstrip("/") + "/"
    sql = (f"SELECT mine_code,mine_name,report_time,update_rate,cycle,file_name_id,file_name,attachment_id,attachment_name,"
           f"CONCAT(%s,local_bucket_name,'/',local_attachment_address) AS attachment_link,"
           f"SUBSTRING_INDEX(local_attachment_address,'.',-1) AS suffix FROM {table} "
           f"WHERE mine_code=%s AND file_name_id IN ({','.join(['%s'] * len(ids))}) AND ({' OR '.join(clauses)}) ORDER BY report_time_year, attachment_id LIMIT {max_rows + 1}")
    parameters = [attachment_base, *parameters]
    connection = connection_factory()
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(sql, parameters)
            names = [column[0] for column in cursor.description]
            rows = [dict(row) if isinstance(row, dict) else dict(zip(names, row)) for row in cursor.fetchall()]
            if len(rows) > max_rows:
                raise AcquisitionError("source_query_row_budget_exhausted")
        finally:
            cursor.close()
    finally:
        connection.close()
    return {"mine_id": mine_id, "check_cycle": check_cycle, "item_code": item_code, "sql": sql,
            "parameters": parameters, "rows": rows, "source_complete": False,
            "coverage": {"catalog_index_only": True, "max_query_rows": max_rows, "cadence_groups": by_cadence}}
