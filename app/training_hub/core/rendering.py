from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from ..config.settings import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, TrainingHubSettings
from .admin_ops import _admin_audit_logs, _admin_cases, _admin_runs, _admin_users
from .data_exports import _user_data_export_requests
from .recovery import _monitoring_snapshot
from .session_auth import _user_active_sessions
from .training_data import _user_uploads


def _legal_context(settings: TrainingHubSettings) -> dict[str, Any]:
    compliance_warnings: list[str] = []
    if not settings.site_operator_name:
        compliance_warnings.append("No operator name is configured.")
    if not settings.site_postal_address:
        compliance_warnings.append("No serviceable postal address is configured.")
    elif settings.site_postal_address.lstrip().startswith("@"):
        compliance_warnings.append(
            "The configured address looks like a handle and not like a serviceable postal address."
        )
    if not settings.site_contact_channel:
        compliance_warnings.append("No public contact channel is configured.")

    return {
        "site_project_classification": settings.site_project_classification,
        "site_operator_name": settings.site_operator_name or "Not configured",
        "site_postal_address": settings.site_postal_address or "Not configured",
        "site_contact_channel": settings.site_contact_channel or "Not configured",
        "site_privacy_contact": settings.site_privacy_contact_display or "Not configured",
        "site_hosting_location": settings.site_hosting_location,
        "public_base_url": settings.public_base_url or "Not configured",
        "site_operator_identity_complete": settings.site_operator_identity_complete,
        "compliance_warnings": compliance_warnings,
        "email_features_enabled": (
            settings.password_reset_send_email or settings.admin_mfa_required or settings.outbound_email_enabled
        ),
        "account_data_export_email_enabled": settings.outbound_email_enabled,
        "smtp_host": settings.smtp_host or "Not configured",
        "session_cookie_name": SESSION_COOKIE_NAME,
        "csrf_cookie_name": CSRF_COOKIE_NAME,
        "retention_sessions_days": settings.retention_sessions_days,
        "retention_password_reset_days": settings.retention_password_reset_days,
        "retention_audit_logs_days": settings.retention_audit_logs_days,
        "retention_uploads_days": settings.retention_uploads_days,
        "retention_bundles_days": settings.retention_bundles_days,
        "retention_backups_days": settings.retention_backups_days,
        "retention_rate_limit_days": settings.retention_rate_limit_days,
    }


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
        registration_mode if registration_mode in {"open", "invite", "closed"} else "open"
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


def _dashboard_context(
    request: Request,
    settings: TrainingHubSettings,
    user: dict[str, Any],
    notice: str,
    error: str,
) -> dict[str, Any]:
    uploads = [dict(row) for row in _user_uploads(settings.database_path, int(user["id"]))]
    total_cases = sum(int(row["case_count"]) for row in uploads)
    current_session_id = getattr(request.state, "session_id", None)
    sessions = _user_active_sessions(settings.database_path, int(user["id"]), current_session_id)
    data_export_requests = _user_data_export_requests(settings.database_path, int(user["id"]))
    return {
        "request": request,
        "notice": notice,
        "error": error,
        "current_user": user,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "uploads": uploads,
        "recent_uploads": uploads[:5],
        "sessions": sessions,
        "data_export_requests": data_export_requests,
        "total_cases": total_cases,
        "max_mb": settings.max_upload_bytes // (1024 * 1024),
        "email_exports_enabled": settings.outbound_email_enabled,
        "data_export_cooldown_minutes": settings.data_export_cooldown_minutes,
        "active_session_count": len(sessions),
        "pending_export_count": len(
            [
                row
                for row in data_export_requests
                if str(row.get("status", "")).lower() in {"queued", "pending", "processing"}
            ]
        ),
    }


def _render_dashboard(
    request: Request,
    templates: Jinja2Templates,
    settings: TrainingHubSettings,
    user: dict[str, Any],
    notice: str = "",
    error: str = "",
    status_code: int = 200,
    page: str = "overview",
):
    template_map = {
        "overview": "dashboard.html",
        "uploads": "dashboard_uploads.html",
        "account": "dashboard_account.html",
    }
    template_name = template_map.get(page)
    if template_name is None:
        raise ValueError(f"Unsupported dashboard page: {page}")

    context = _dashboard_context(request, settings, user, notice, error)
    context["dashboard_page"] = page
    return templates.TemplateResponse(request, template_name, context, status_code=status_code)


def _admin_context(
    request: Request,
    settings: TrainingHubSettings,
    user: dict[str, Any],
    notice: str,
    error: str,
) -> dict[str, Any]:
    users = [dict(row) for row in _admin_users(settings.database_path)]
    cases = [dict(row) for row in _admin_cases(settings.database_path)]
    runs = [dict(row) for row in _admin_runs(settings.database_path)]
    audit_logs = [dict(row) for row in _admin_audit_logs(settings.database_path)]
    return {
        "request": request,
        "notice": notice,
        "error": error,
        "current_user": user,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "monitoring": _monitoring_snapshot(settings),
        "users": users,
        "cases": cases,
        "runs": runs,
        "audit_logs": audit_logs,
        "recent_users": users[:6],
        "recent_cases": cases[:8],
        "recent_runs": runs[:6],
        "recent_audit_logs": audit_logs[:8],
    }


def _render_admin(
    request: Request,
    templates: Jinja2Templates,
    settings: TrainingHubSettings,
    user: dict[str, Any],
    notice: str = "",
    error: str = "",
    status_code: int = 200,
    page: str = "overview",
):
    template_map = {
        "overview": "admin.html",
        "users": "admin_users.html",
        "cases": "admin_cases.html",
        "runs": "admin_runs.html",
        "system": "admin_system.html",
    }
    template_name = template_map.get(page)
    if template_name is None:
        raise ValueError(f"Unsupported admin page: {page}")

    context = _admin_context(request, settings, user, notice, error)
    context["admin_page"] = page
    return templates.TemplateResponse(request, template_name, context, status_code=status_code)
