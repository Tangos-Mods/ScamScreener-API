from __future__ import annotations

from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from ..core.hub_core import (
    _change_user_password,
    _create_audit_log,
    _refresh_user,
    _render_dashboard,
    _revoke_other_user_sessions,
    _revoke_user_session_by_id,
    _validate_csrf_token,
)
from ..config.settings import TrainingHubSettings
from .public_utils import request_meta as _request_meta


def register_public_dashboard_account_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        return await run_in_threadpool(
            _render_dashboard,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=user,
        )

    @app.post("/dashboard/password", response_class=HTMLResponse)
    async def change_password(
        request: Request,
        current_password: str = Form(...),
        new_password: str = Form(...),
        new_password_confirm: str = Form(...),
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)

        if (new_password or "") != (new_password_confirm or ""):
            return await run_in_threadpool(
                _render_dashboard,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error="New password confirmation does not match.",
                status_code=400,
            )

        change_result = await run_in_threadpool(
            _change_user_password,
            settings.database_path,
            int(user["id"]),
            current_password,
            new_password,
        )
        if not bool(change_result.get("ok")):
            return await run_in_threadpool(
                _render_dashboard,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error=str(change_result.get("error", "Password update failed.")),
                status_code=int(change_result.get("status_code", 400)),
            )

        current_session_id = getattr(request.state, "session_id", None)
        revoked_count = await run_in_threadpool(
            _revoke_other_user_sessions,
            settings.database_path,
            int(user["id"]),
            int(current_session_id) if current_session_id is not None else None,
            "password-change",
        )
        source_ip, user_agent = _request_meta(request, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="auth.password.changed",
            target_type="user",
            target_id=int(user["id"]),
            details=f"Password changed. Revoked {revoked_count} other sessions.",
            source_ip=source_ip,
            user_agent=user_agent,
        )
        refreshed_user = await run_in_threadpool(_refresh_user, settings.database_path, int(user["id"])) or user
        return await run_in_threadpool(
            _render_dashboard,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=refreshed_user,
            notice="Password updated successfully.",
        )

    @app.post("/dashboard/sessions/revoke-others", response_class=HTMLResponse)
    async def revoke_other_sessions(request: Request, csrf_token: str = Form(...)):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)

        current_session_id = getattr(request.state, "session_id", None)
        revoked_count = await run_in_threadpool(
            _revoke_other_user_sessions,
            settings.database_path,
            int(user["id"]),
            int(current_session_id) if current_session_id is not None else None,
            "user-revoke-others",
        )
        source_ip, user_agent = _request_meta(request, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="session.revoke.others",
            target_type="session",
            target_id=None,
            details=f"Revoked {revoked_count} other sessions.",
            source_ip=source_ip,
            user_agent=user_agent,
        )
        refreshed_user = await run_in_threadpool(_refresh_user, settings.database_path, int(user["id"])) or user
        return await run_in_threadpool(
            _render_dashboard,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=refreshed_user,
            notice=f"Revoked {revoked_count} other sessions.",
        )

    @app.post("/dashboard/sessions/{session_id}/revoke", response_class=HTMLResponse)
    async def revoke_single_session(request: Request, session_id: int, csrf_token: str = Form(...)):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)

        current_session_id = getattr(request.state, "session_id", None)
        if current_session_id is not None and int(current_session_id) == int(session_id):
            return await run_in_threadpool(
                _render_dashboard,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error="Use logout to end your current session.",
                status_code=400,
            )

        revoked = await run_in_threadpool(
            _revoke_user_session_by_id,
            settings.database_path,
            int(user["id"]),
            int(session_id),
            "user-revoke-session",
        )
        if not revoked:
            return await run_in_threadpool(
                _render_dashboard,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error="Session not found or already revoked.",
                status_code=404,
            )

        source_ip, user_agent = _request_meta(request, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="session.revoke.one",
            target_type="session",
            target_id=int(session_id),
            details=f"Revoked session #{session_id}.",
            source_ip=source_ip,
            user_agent=user_agent,
        )
        refreshed_user = await run_in_threadpool(_refresh_user, settings.database_path, int(user["id"])) or user
        return await run_in_threadpool(
            _render_dashboard,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=refreshed_user,
            notice=f"Revoked session #{session_id}.",
        )

