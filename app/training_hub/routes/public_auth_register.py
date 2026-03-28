from __future__ import annotations

import hmac
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from ..infra import db as sqlite3
from ..core.hub_core import (
    _hash_password,
    _normalize_email,
    _normalize_username,
    _now_utc_iso,
    _render_auth,
    _set_session_cookie,
    _validate_csrf_token,
    _validate_password,
)
from ..config.settings import TrainingHubSettings


def register_public_auth_register_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    @app.get("/register", response_class=HTMLResponse)
    async def register_page(request: Request):
        if request.state.user:
            return RedirectResponse(url="/dashboard", status_code=303)
        if settings.registration_mode == "closed":
            return _render_auth(
                request=request,
                templates=app.state.templates,
                mode="register",
                registration_mode=settings.registration_mode,
                error="Registration is currently disabled.",
                status_code=403,
            )
        return _render_auth(
            request=request,
            templates=app.state.templates,
            mode="register",
            registration_mode=settings.registration_mode,
        )

    @app.post("/register", response_class=HTMLResponse)
    async def register_user(
        request: Request,
        username: str = Form(...),
        email: str = Form(...),
        password: str = Form(...),
        invite_code: str = Form(default=""),
        csrf_token: str = Form(...),
    ):
        if request.state.user:
            return RedirectResponse(url="/dashboard", status_code=303)
        _validate_csrf_token(request, csrf_token)

        if settings.registration_mode == "closed":
            return _render_auth(
                request=request,
                templates=app.state.templates,
                mode="register",
                registration_mode=settings.registration_mode,
                error="Registration is currently disabled.",
                status_code=403,
            )
        if settings.registration_mode == "invite":
            submitted_invite = (invite_code or "").strip()
            if not submitted_invite or not hmac.compare_digest(submitted_invite, settings.registration_invite_code):
                return _render_auth(
                    request=request,
                    templates=app.state.templates,
                    mode="register",
                    registration_mode=settings.registration_mode,
                    error="Invalid invite code.",
                    status_code=403,
                )

        normalized_username = _normalize_username(username)
        normalized_email = _normalize_email(email)
        password_error = _validate_password(password)

        if not normalized_username:
            return _render_auth(
                request=request,
                templates=app.state.templates,
                mode="register",
                registration_mode=settings.registration_mode,
                error="Username must be 3-32 chars: letters, numbers, _ or -.",
                status_code=400,
            )
        if not normalized_email:
            return _render_auth(
                request=request,
                templates=app.state.templates,
                mode="register",
                registration_mode=settings.registration_mode,
                error="Enter a valid email address.",
                status_code=400,
            )
        if password_error:
            return _render_auth(
                request=request,
                templates=app.state.templates,
                mode="register",
                registration_mode=settings.registration_mode,
                error=password_error,
                status_code=400,
            )

        def _register_sync() -> dict[str, Any]:
            with sqlite3.connect(settings.database_path) as connection:
                connection.row_factory = sqlite3.Row
                exists = connection.execute(
                    "SELECT 1 FROM users WHERE username = ? OR email = ?",
                    (normalized_username, normalized_email),
                ).fetchone()
                if exists is not None:
                    return {
                        "error": "Username or email already exists.",
                        "status_code": 409,
                    }

                user_count = int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])
                if user_count == 0:
                    if not settings.admin_usernames:
                        return {
                            "error": (
                                "Initial admin bootstrap is locked. "
                                "Set TRAINING_HUB_ADMIN_USERNAMES before first registration."
                            ),
                            "status_code": 503,
                        }
                    if normalized_username not in settings.admin_usernames:
                        return {
                            "error": "First account must match TRAINING_HUB_ADMIN_USERNAMES bootstrap allowlist.",
                            "status_code": 403,
                        }
                    is_admin = 1
                else:
                    is_admin = 0

                cursor = connection.execute(
                    """
                    INSERT INTO users (created_at, username, email, password_hash, is_admin)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (_now_utc_iso(), normalized_username, normalized_email, _hash_password(password), is_admin),
                )
                connection.commit()
                return {"user_id": int(cursor.lastrowid)}

        register_result = await run_in_threadpool(_register_sync)
        if "error" in register_result:
            return _render_auth(
                request=request,
                templates=app.state.templates,
                mode="register",
                registration_mode=settings.registration_mode,
                error=str(register_result["error"]),
                status_code=int(register_result.get("status_code", 400)),
            )

        response = RedirectResponse(url="/dashboard", status_code=303)
        _set_session_cookie(response, settings, int(register_result["user_id"]), request)
        return response

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, notice: str | None = None):
        if request.state.user:
            return RedirectResponse(url="/dashboard", status_code=303)
        return _render_auth(
            request=request,
            templates=app.state.templates,
            mode="login",
            notice=notice or "",
            registration_mode=settings.registration_mode,
        )


