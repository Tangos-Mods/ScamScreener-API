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

