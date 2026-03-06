from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from ..core.hub_core import _global_stats, _monitoring_snapshot, _now_utc_iso
from ..config.settings import TrainingHubSettings
from .public_utils import prometheus_metrics as _prometheus_metrics


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
        context = {
            "request": request,
            "current_user": request.state.user,
            "csrf_token": getattr(request.state, "csrf_token", ""),
            "stats": stats,
        }
        return app.state.templates.TemplateResponse(request, "landing.html", context)

    @app.get("/hub")
    async def hub_redirect(request: Request):
        if request.state.user:
            return RedirectResponse(url="/dashboard", status_code=303)
        return RedirectResponse(url="/login", status_code=303)


