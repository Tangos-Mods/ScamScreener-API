from __future__ import annotations

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from ..core.hub_core import _create_audit_log, _render_admin, _run_retention_cleanup, _run_training_pipeline, _validate_csrf_token
from ..config.settings import TrainingHubSettings
from .admin_utils import request_meta as _request_meta


def register_admin_overview_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    @app.get("/admin", response_class=HTMLResponse)
    async def admin_dashboard(request: Request, notice: str = "", error: str = ""):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        if int(user["is_admin"]) != 1:
            raise HTTPException(status_code=403, detail="Admin access required.")
        return await run_in_threadpool(
            _render_admin,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=user,
            notice=notice,
            error=error,
        )

    @app.post("/admin/train", response_class=HTMLResponse)
    async def admin_train(request: Request, csrf_token: str = Form(...)):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        if int(user["is_admin"]) != 1:
            raise HTTPException(status_code=403, detail="Admin access required.")
        _validate_csrf_token(request, csrf_token)

        result = await run_in_threadpool(_run_training_pipeline, settings, int(user["id"]))
        result_status = str(result.get("status", "unknown"))
        source_ip, user_agent = _request_meta(request, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action=f"training.bundle.{result_status}",
            target_type="training_run",
            target_id=result.get("run_id"),
            details=str(result.get("message", "Training bundle created.")),
            source_ip=source_ip,
            user_agent=user_agent,
        )

        if result_status == "failed":
            return await run_in_threadpool(
                _render_admin,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error=str(result.get("message", "Pipeline failed.")),
            )
        return await run_in_threadpool(
            _render_admin,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=user,
            notice=str(result.get("message", "Pipeline completed.")),
        )

    @app.post("/admin/retention/run", response_class=HTMLResponse)
    async def admin_run_retention(request: Request, csrf_token: str = Form(...)):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        if int(user["is_admin"]) != 1:
            raise HTTPException(status_code=403, detail="Admin access required.")
        _validate_csrf_token(request, csrf_token)

        cleanup = await run_in_threadpool(_run_retention_cleanup, settings)
        summary = (
            f"Retention cleanup completed. Sessions: {cleanup['sessions']}, "
            f"Reset tokens: {cleanup['password_reset_tokens']}, "
            f"MFA challenges: {cleanup['admin_mfa_challenges']}, "
            f"Audit logs: {cleanup['audit_logs']}, "
            f"Uploads: {cleanup['uploads']}, Bundles: {cleanup['bundles']}, "
            f"Rate-limit rows: {cleanup['rate_limit_hits']}."
        )
        source_ip, user_agent = _request_meta(request, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="retention.cleanup.completed",
            target_type="system",
            target_id=None,
            details=summary,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        return await run_in_threadpool(
            _render_admin,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=user,
            notice=summary,
        )


