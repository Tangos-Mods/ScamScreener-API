from __future__ import annotations

import ipaddress
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import tarfile
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ..infra import db as sqlite3
from ..config.settings import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, TRAINING_FORMAT, TRAINING_SCHEMA_VERSION, TrainingHubSettings

_INTERNAL_IPV4_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_INTERNAL_IPV6_NETWORK = ipaddress.ip_network("fc00::/7")


def _is_request_from_trusted_proxy(request: Request, trusted_proxies: set[str]) -> bool:
    if "*" in trusted_proxies:
        return True
    client_host = request.client.host.strip().lower() if request.client and request.client.host else ""
    if not client_host:
        return False
    if client_host in trusted_proxies:
        return True
    try:
        client_ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    for candidate in trusted_proxies:
        normalized = str(candidate or "").strip().lower()
        if "/" not in normalized:
            continue
        try:
            if client_ip in ipaddress.ip_network(normalized, strict=False):
                return True
        except ValueError:
            continue
    return False


def _request_client_ip(request: Request, settings: TrainingHubSettings) -> str:
    source_ip = request.client.host if request.client and request.client.host else ""
    if _is_request_from_trusted_proxy(request, settings.trusted_proxies):
        forwarded_for = str(request.headers.get("x-forwarded-for", "")).strip()
        if forwarded_for:
            first_ip = forwarded_for.split(",")[0].strip()
            if first_ip:
                source_ip = first_ip
    return source_ip


def _parse_ip_address(value: str) -> ipaddress._BaseAddress | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        return ipaddress.ip_address(normalized)
    except ValueError:
        return None


def _is_internal_network_ip(value: str) -> bool:
    address = _parse_ip_address(value)
    if address is None:
        return False
    if address.is_loopback or address.is_link_local:
        return True
    if isinstance(address, ipaddress.IPv4Address):
        return any(address in network for network in _INTERNAL_IPV4_NETWORKS)
    return address in _INTERNAL_IPV6_NETWORK


def _request_originates_from_internal_network(request: Request, trusted_proxies: set[str]) -> bool:
    source_ip = request.client.host if request.client and request.client.host else ""
    if _is_request_from_trusted_proxy(request, trusted_proxies):
        forwarded_for = str(request.headers.get("x-forwarded-for", "")).strip()
        if forwarded_for:
            first_ip = forwarded_for.split(",")[0].strip()
            if first_ip:
                source_ip = first_ip
    return _is_internal_network_ip(source_ip)


def _normalize_user_agent_for_binding(value: str) -> str:
    return (value or "").strip().lower()[:180]


def _authorization_bearer_token(value: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        return ""
    scheme, separator, token = normalized.partition(" ")
    if separator != " " or scheme.strip().lower() != "bearer":
        return ""
    return token.strip()


def _is_path_within(base_dir: Path, candidate: Path) -> bool:
    base_resolved = base_dir.resolve(strict=False)
    candidate_resolved = candidate.resolve(strict=False)
    try:
        return candidate_resolved.is_relative_to(base_resolved)
    except AttributeError:
        base_text = str(base_resolved)
        candidate_text = str(candidate_resolved)
        return candidate_text == base_text or candidate_text.startswith(base_text + os.sep)


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _format_utc_timestamp(value: Any) -> str:
    if value is None:
        return ""

    candidate = value
    if isinstance(candidate, datetime):
        parsed = candidate
    else:
        raw = str(candidate or "").strip()
        if not raw:
            return ""
        normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return raw

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)

    if parsed.second or parsed.microsecond:
        return parsed.strftime("%Y-%m-%d %H:%M:%S UTC")
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


def _format_integer_grouped(value: Any) -> str:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return str(value or "")

    sign = "-" if numeric < 0 else ""
    digits = str(abs(numeric))
    grouped_parts: list[str] = []
    while digits:
        grouped_parts.append(digits[-3:])
        digits = digits[:-3]
    return sign + ".".join(reversed(grouped_parts or ["0"]))

