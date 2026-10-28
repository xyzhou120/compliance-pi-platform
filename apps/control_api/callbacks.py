"""Stable upstream callbacks with an explicitly allowlisted, pinned target."""

from __future__ import annotations

import http.client
import ipaddress
import json
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit


class DeliveryError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool):
        super().__init__(code)
        self.retryable = retryable


@dataclass(frozen=True)
class CallbackPolicy:
    hosts: tuple[str, ...]
    allow_http: bool = False
    allow_private: bool = True
    allow_loopback: bool = False

    def validate(self, url: str) -> None:
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in ({"http", "https"} if self.allow_http else {"https"}):
                raise ValueError
            if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                raise ValueError
            if parsed.hostname.lower() not in self.hosts:
                raise ValueError
            _ = parsed.port
        except (ValueError, TypeError) as exc:
            raise DeliveryError("callback_target_rejected", retryable=False) from exc

    def addresses(self, url: str) -> tuple[str, int]:
        self.validate(url)
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise DeliveryError("callback_dns_failed", retryable=True) from exc
        for _, _, _, _, address in addresses:
            ip = ipaddress.ip_address(address[0])
            if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
                raise DeliveryError("callback_address_rejected", retryable=False)
            if ip.is_loopback and not self.allow_loopback:
                raise DeliveryError("callback_loopback_rejected", retryable=False)
            if ip.is_private and not ip.is_loopback and not self.allow_private:
                raise DeliveryError("callback_private_rejected", retryable=False)
        if not addresses:
            raise DeliveryError("callback_dns_empty", retryable=True)
        return addresses[0][4][0], port


def callback_payload(run: dict, result: dict) -> dict:
    request = run.get("request", run.get("request_json", {}))
    spec = run.get("spec", {})
    mine = run.get("mine_id", spec.get("mine_code", request.get("mineId")))
    cycle = run.get("check_cycle", spec.get("check_cycle", request.get("checkCycle")))
    snapshot = run.get("snapshot") or {}
    by_file = {}
    for evidence in snapshot.get("evidence", []) + run.get("evidence", []):
        by_file.setdefault(evidence.get("file_id"), set()).add(evidence.get("evidence_id"))
    attachments = []
    for file in snapshot.get("files", []):
        url = file.get("original_url") or file.get("attachment_url")
        parsed = urlsplit(url) if isinstance(url, str) else None
        if not parsed or parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            continue
        attachment = {"attachmentName": file.get("original_name", file["file_id"]), "attachmentUrl": url,
                      "attachmentCycle": str(file.get("report_date") or cycle)[:7]}
        details = [finding.get("detail", "") for finding in result.get("findings", [])
                   if finding.get("certainty") not in {"candidate", "unverifiable"}
                   and set(finding.get("evidence_refs", [])) & by_file.get(file["file_id"], set())]
        if any(details):
            attachment["attachmentContent"] = "; ".join(dict.fromkeys(filter(None, details)))
        attachments.append(attachment)
    return {"mineId": mine, "itemCode": run.get("item_code", spec.get("item_code")), "checkCycle": cycle,
            "checkTime": result.get("finalized_at", datetime.now(timezone.utc).isoformat()),
            "checkResult": result["status"], "description": result["description"],
            "detail": result.get("detail") or result["description"], "attachments": attachments}


class HttpCallbackClient:
    def __init__(self, policy: CallbackPolicy, *, timeout: int = 15, authorization: str = ""):
        self.policy = policy
        self.timeout = timeout
        self.authorization = authorization

    def send(self, url: str, payload: dict, delivery_id: str) -> dict:
        ip, port = self.policy.addresses(url)
        parsed = urlsplit(url)
        host = parsed.hostname
        connection = http.client.HTTPConnection(host, port, timeout=self.timeout)
        # Connect to validated IP while preserving Host and TLS hostname.
        raw = None
        try:
            raw = socket.create_connection((ip, port), timeout=self.timeout)
            if parsed.scheme == "https":
                raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
            connection.sock = raw
            headers = {"Content-Type": "application/json", "Accept": "application/json", "Idempotency-Key": delivery_id}
            if self.authorization:
                headers["Authorization"] = self.authorization
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            connection.request("POST", parsed.path + ("?" + parsed.query if parsed.query else ""), body=body, headers=headers)
            response = connection.getresponse()
            content = response.read(65537)
            if len(content) > 65536:
                raise DeliveryError("callback_response_too_large", retryable=False)
            if response.status >= 500 or response.status in {408, 429}:
                raise DeliveryError("callback_temporary_http_error", retryable=True)
            if not 200 <= response.status < 300:
                raise DeliveryError("callback_http_rejected", retryable=False)
            try:
                data = json.loads(content)
            except ValueError as exc:
                raise DeliveryError("callback_response_invalid", retryable=False) from exc
            if not isinstance(data, dict) or type(data.get("code")) is not int or data.get("code") != 200 or data.get("success") is not True:
                raise DeliveryError("callback_business_rejected", retryable=False)
            return {"code": 200, "success": True}
        except DeliveryError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise DeliveryError("callback_transport_failed", retryable=True) from exc
        finally:
            connection.close()
            if raw:
                raw.close()
