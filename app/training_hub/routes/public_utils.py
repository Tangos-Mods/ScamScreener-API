from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, UploadFile

from ..config.settings import TrainingHubSettings
from ..core.common import _is_request_from_trusted_proxy, _request_client_ip

ADMIN_MFA_COOKIE_NAME = "training_hub_admin_mfa"
logger = logging.getLogger(__name__)


def is_path_within(base_dir: Path, candidate: Path) -> bool:
    base_resolved = base_dir.resolve(strict=False)
    candidate_resolved = candidate.resolve(strict=False)
    try:
        return candidate_resolved.is_relative_to(base_resolved)
    except AttributeError:
        base_text = str(base_resolved)
        candidate_text = str(candidate_resolved)
        return candidate_text == base_text or candidate_text.startswith(base_text + os.sep)


def request_meta(request: Request, settings: TrainingHubSettings) -> tuple[str, str]:
    source_ip = _request_client_ip(request, settings)
    user_agent = str(request.headers.get("user-agent", ""))
    return source_ip, user_agent


def mask_email(value: str) -> str:
    normalized = (value or "").strip()
    if "@" not in normalized:
        return "your email"
    local, domain = normalized.split("@", 1)
    if not local:
        return f"***@{domain}"
    if len(local) == 1:
        return f"{local}***@{domain}"
    return f"{local[0]}***{local[-1]}@{domain}"


def prometheus_metrics(snapshot: dict[str, Any]) -> str:
    totals = snapshot.get("totals", {})
    events = snapshot.get("events", {})
    alerts = snapshot.get("alerts", {})
    window_minutes = int(snapshot.get("window_minutes", 15))
    lines = [
        "# HELP scamscreener_users_total Total users.",
        "# TYPE scamscreener_users_total gauge",
        f"scamscreener_users_total {int(totals.get('users', 0))}",
        "# HELP scamscreener_uploads_total Total uploads.",
        "# TYPE scamscreener_uploads_total gauge",
        f"scamscreener_uploads_total {int(totals.get('uploads', 0))}",
        "# HELP scamscreener_training_cases_total Total training cases.",
        "# TYPE scamscreener_training_cases_total gauge",
        f"scamscreener_training_cases_total {int(totals.get('training_cases', 0))}",
        "# HELP scamscreener_training_runs_total Total training runs.",
        "# TYPE scamscreener_training_runs_total gauge",
        f"scamscreener_training_runs_total {int(totals.get('training_runs', 0))}",
        "# HELP scamscreener_audit_logs_total Total audit log entries.",
        "# TYPE scamscreener_audit_logs_total gauge",
        f"scamscreener_audit_logs_total {int(totals.get('audit_logs', 0))}",
        "# HELP scamscreener_login_failures_recent Login failures+lockouts in alert window.",
        "# TYPE scamscreener_login_failures_recent gauge",
        f"scamscreener_login_failures_recent {int(events.get('login_failures_total', 0))}",
        "# HELP scamscreener_mfa_failures_recent MFA failures in alert window.",
        "# TYPE scamscreener_mfa_failures_recent gauge",
        f"scamscreener_mfa_failures_recent {int(events.get('mfa_failed', 0))}",
        "# HELP scamscreener_password_reset_requests_recent Password reset requests in alert window.",
        "# TYPE scamscreener_password_reset_requests_recent gauge",
        f"scamscreener_password_reset_requests_recent {int(events.get('password_reset_requested', 0))}",
        "# HELP scamscreener_security_alert_failed_login_spike 1 when login failure alert threshold exceeded.",
        "# TYPE scamscreener_security_alert_failed_login_spike gauge",
        f"scamscreener_security_alert_failed_login_spike {int(alerts.get('failed_login_spike', 0))}",
        "# HELP scamscreener_security_alert_mfa_failed_spike 1 when MFA failure alert threshold exceeded.",
        "# TYPE scamscreener_security_alert_mfa_failed_spike gauge",
        f"scamscreener_security_alert_mfa_failed_spike {int(alerts.get('mfa_failed_spike', 0))}",
        "# HELP scamscreener_security_alert_password_reset_spike 1 when reset request alert threshold exceeded.",
        "# TYPE scamscreener_security_alert_password_reset_spike gauge",
        f"scamscreener_security_alert_password_reset_spike {int(alerts.get('password_reset_spike', 0))}",
        "# HELP scamscreener_security_alert_window_minutes Current security alert window size in minutes.",
        "# TYPE scamscreener_security_alert_window_minutes gauge",
        f"scamscreener_security_alert_window_minutes {window_minutes}",
        "",
    ]
    return "\n".join(lines)


async def read_upload_bytes(upload_file: UploadFile, max_bytes: int, chunk_size: int = 64 * 1024) -> bytes:
    payload = bytearray()
    while True:
        chunk = await upload_file.read(chunk_size)
        if not chunk:
            break
        payload.extend(chunk)
        if len(payload) > max_bytes:
            raise HTTPException(status_code=413, detail=f"File exceeds limit ({max_bytes} bytes).")
    return bytes(payload)


async def read_request_bytes(request: Request, max_bytes: int) -> bytes:
    payload = bytearray()
    async for chunk in request.stream():
        if not chunk:
            continue
        payload.extend(chunk)
        if len(payload) > max_bytes:
            raise HTTPException(status_code=413, detail=f"File exceeds limit ({max_bytes} bytes).")
    return bytes(payload)


