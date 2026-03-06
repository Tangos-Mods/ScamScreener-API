from __future__ import annotations

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
    return bool(client_host and client_host in trusted_proxies)


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

