from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from ..core.hub_core import _global_stats, _monitoring_snapshot, _now_utc_iso
from ..config.settings import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, TrainingHubSettings
from .public_utils import prometheus_metrics as _prometheus_metrics


def _base_context(request: Request) -> dict[str, Any]:
    return {
        "request": request,
        "current_user": request.state.user,
        "csrf_token": getattr(request.state, "csrf_token", ""),
    }


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
        "email_features_enabled": settings.password_reset_send_email or settings.admin_mfa_required,
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


def register_public_site_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    @app.get("/api/v1/health")
    async def health() -> dict[str, Any]:
        stats = await run_in_threadpool(_global_stats, settings.database_path)
        return {
            "status": "ok",
            "timeUtc": _now_utc_iso(),
            "users": stats["users"],
            "uploads": stats["uploads"],
            "storageDir": str(settings.storage_dir),
            "maxUploadBytes": settings.max_upload_bytes,
        }

    @app.get("/api/v1/metrics")
    async def metrics() -> PlainTextResponse:
        snapshot = await run_in_threadpool(_monitoring_snapshot, settings)
        payload = _prometheus_metrics(snapshot)
        return PlainTextResponse(payload, media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        stats = await run_in_threadpool(_global_stats, settings.database_path)
        context = _base_context(request)
        context["stats"] = stats
        return app.state.templates.TemplateResponse(request, "landing.html", context)

    @app.get("/hub")
    async def hub_redirect(request: Request):
        if request.state.user:
            return RedirectResponse(url="/dashboard", status_code=303)
        return RedirectResponse(url="/login", status_code=303)

    @app.get("/legal-notice", response_class=HTMLResponse)
    async def legal_notice(request: Request):
        context = _base_context(request)
        context.update(_legal_context(settings))
        return app.state.templates.TemplateResponse(request, "legal_notice.html", context)

    @app.get("/privacy", response_class=HTMLResponse)
    async def privacy_notice(request: Request):
        context = _base_context(request)
        context.update(_legal_context(settings))
        return app.state.templates.TemplateResponse(request, "privacy.html", context)

    @app.get("/impressum")
    async def legal_notice_legacy_redirect() -> RedirectResponse:
        return RedirectResponse(url="/legal-notice", status_code=303)

    @app.get("/datenschutz")
    async def privacy_notice_legacy_redirect() -> RedirectResponse:
        return RedirectResponse(url="/privacy", status_code=303)

    @app.get("/legal")
    async def legal_redirect() -> RedirectResponse:
        return RedirectResponse(url="/legal-notice", status_code=303)


