from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from ..config.settings import TrainingHubSettings
from ..core.rendering import _admin_context
from ..http.live_api_metrics import live_api_metrics_snapshot, merge_live_api_metrics_snapshots
from ..http.persistent_api_metrics import combine_live_and_persistent_api_metrics


async def _combined_live_api_metrics_snapshot(app: FastAPI, settings: TrainingHubSettings) -> dict[str, Any]:
    local_snapshot = live_api_metrics_snapshot(app.state)
    internal_api_metrics_url = settings.internal_api_metrics_url.strip()
    remote_snapshot: dict[str, Any] | None = None

    if internal_api_metrics_url:
        try:
            async with httpx.AsyncClient(
                follow_redirects=False,
                timeout=httpx.Timeout(1.0, connect=0.5),
            ) as client:
                response = await client.get(
                    internal_api_metrics_url,
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()
                remote_snapshot = response.json()
        except (httpx.HTTPError, ValueError, TypeError):
            remote_snapshot = None

    merged_live_snapshot = merge_live_api_metrics_snapshots(
        local_snapshot,
        remote_snapshot or {},
    )
    persistent_snapshot = None
    persistent_metrics = getattr(app.state, "persistent_api_metrics", None)
    if persistent_metrics is not None:
        await run_in_threadpool(persistent_metrics.flush)
        try:
            persistent_snapshot = await run_in_threadpool(
                persistent_metrics.snapshot,
                focus_entries=[
                    (str(entry.get("endpoint", "")), str(entry.get("agent", "")))
                    for entry in (merged_live_snapshot.get("entries", []) or [])
                ],
            )
        except Exception:
            persistent_snapshot = None

    return combine_live_and_persistent_api_metrics(merged_live_snapshot, persistent_snapshot)


def register_admin_analytics_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    def _require_admin(request: Request):
        user = request.state.user
        if user is None:
            return None, RedirectResponse(url="/login", status_code=303)
        if int(user["is_admin"]) != 1:
            raise HTTPException(status_code=403, detail="Admin access required.")
        return user, None

    @app.get("/admin/analytics", response_class=HTMLResponse)
    async def admin_analytics_statistics_alias(request: Request, notice: str = "", error: str = ""):
        user, redirect = _require_admin(request)
        if redirect is not None:
            return redirect
        context = await run_in_threadpool(
            _admin_context,
            request=request,
            settings=settings,
            user=user,
            notice=notice,
            error=error,
        )
        context["admin_page"] = "analytics_statistics"
        return app.state.templates.TemplateResponse(request, "admin_analytics_statistics.html", context)

    @app.get("/admin/analytics/statistics", response_class=HTMLResponse)
    async def admin_analytics_statistics(request: Request, notice: str = "", error: str = ""):
        user, redirect = _require_admin(request)
        if redirect is not None:
            return redirect
        context = await run_in_threadpool(
            _admin_context,
            request=request,
            settings=settings,
            user=user,
            notice=notice,
            error=error,
        )
        context["admin_page"] = "analytics_statistics"
        return app.state.templates.TemplateResponse(request, "admin_analytics_statistics.html", context)

    @app.get("/admin/analytics/metrics", response_class=HTMLResponse)
    async def admin_analytics_metrics(request: Request, notice: str = "", error: str = ""):
        user, redirect = _require_admin(request)
        if redirect is not None:
            return redirect
        context = await run_in_threadpool(
            _admin_context,
            request=request,
            settings=settings,
            user=user,
            notice=notice,
            error=error,
        )
        context["admin_page"] = "analytics_metrics"
        context["live_api_metrics"] = await _combined_live_api_metrics_snapshot(request.app, settings)
        return app.state.templates.TemplateResponse(request, "admin_analytics_metrics.html", context)

    @app.get("/admin/analytics/metrics/stream")
    async def admin_analytics_metrics_stream(request: Request):
        user, redirect = _require_admin(request)
        if redirect is not None:
            return redirect

        async def event_stream():
            while True:
                payload = json.dumps(
                    await _combined_live_api_metrics_snapshot(request.app, settings),
                    separators=(",", ":"),
                )
                yield f"event: snapshot\ndata: {payload}\n\n"
                await asyncio.sleep(1)
                if await request.is_disconnected():
                    break

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
