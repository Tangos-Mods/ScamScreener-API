from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from ..core.hub_core import _global_stats, _monitoring_snapshot, _now_utc_iso
from ..core.rendering import _legal_context
from ..config.settings import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, TrainingHubSettings
from .public_utils import prometheus_metrics as _prometheus_metrics


def _base_context(request: Request) -> dict[str, Any]:
    return {
        "request": request,
        "current_user": request.state.user,
        "csrf_token": getattr(request.state, "csrf_token", ""),
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
        return RedirectResponse(url="/dashboard", status_code=303)

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


