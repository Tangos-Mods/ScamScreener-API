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

from .admin_ops import _admin_audit_logs, _admin_cases, _admin_runs, _admin_users
from .data_exports import _user_data_export_requests
from .recovery import _monitoring_snapshot
from .session_auth import _user_active_sessions
from .training_data import _user_uploads


def _render_auth(
    request: Request,
    templates: Jinja2Templates,
    mode: str,
    notice: str = "",
    error: str = "",
    registration_mode: str = "open",
    status_code: int = 200,
):
    is_register = mode == "register"
    normalized_registration_mode = (
        registration_mode
        if registration_mode in {"open", "invite", "closed"}
        else "open"
    )
    context = {
        "request": request,
        "mode": mode,
        "title": "Register" if is_register else "Login",
        "form_action": "/register" if is_register else "/login",
        "notice": notice,
        "error": error,
        "current_user": request.state.user,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "registration_mode": normalized_registration_mode,
        "registration_invite_required": normalized_registration_mode == "invite",
        "registration_closed": normalized_registration_mode == "closed",
    }
    return templates.TemplateResponse(request, "auth.html", context, status_code=status_code)


def _render_dashboard(
    request: Request,
    templates: Jinja2Templates,
    settings: TrainingHubSettings,
    user: dict[str, Any],
    notice: str = "",
    error: str = "",
    status_code: int = 200,
):
    uploads = [dict(row) for row in _user_uploads(settings.database_path, int(user["id"]))]
    total_cases = sum(int(row["case_count"]) for row in uploads)
    current_session_id = getattr(request.state, "session_id", None)
    sessions = _user_active_sessions(settings.database_path, int(user["id"]), current_session_id)
    context = {
        "request": request,
        "notice": notice,
        "error": error,
        "current_user": user,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "uploads": uploads,
        "sessions": sessions,
        "data_export_requests": _user_data_export_requests(settings.database_path, int(user["id"])),
        "total_cases": total_cases,
        "max_mb": settings.max_upload_bytes // (1024 * 1024),
        "email_exports_enabled": settings.outbound_email_enabled,
        "data_export_cooldown_minutes": settings.data_export_cooldown_minutes,
    }
    return templates.TemplateResponse(request, "dashboard.html", context, status_code=status_code)


def _render_admin(
    request: Request,
    templates: Jinja2Templates,
    settings: TrainingHubSettings,
    user: dict[str, Any],
    notice: str = "",
    error: str = "",
    status_code: int = 200,
):
    context = {
        "request": request,
        "notice": notice,
        "error": error,
        "current_user": user,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "monitoring": _monitoring_snapshot(settings),
        "users": [dict(row) for row in _admin_users(settings.database_path)],
        "cases": [dict(row) for row in _admin_cases(settings.database_path)],
        "runs": [dict(row) for row in _admin_runs(settings.database_path)],
        "audit_logs": [dict(row) for row in _admin_audit_logs(settings.database_path)],
    }
    return templates.TemplateResponse(request, "admin.html", context, status_code=status_code)

