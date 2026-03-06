from __future__ import annotations

from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from ..core.hub_core import (
    _consume_login_attempt,
    _create_admin_mfa_challenge,
    _create_audit_log,
    _maybe_raise_security_alert,
    _refresh_user,
    _render_auth,
    _revoke_session_by_token,
    _set_session_cookie,
    _validate_csrf_token,
)
from ..config.settings import SESSION_COOKIE_NAME, TrainingHubSettings
from .public_utils import ADMIN_MFA_COOKIE_NAME, logger, request_meta as _request_meta


def register_public_auth_login_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    @app.post("/login", response_class=HTMLResponse)
    async def login_user(
        request: Request,
        username_or_email: str = Form(...),
        password: str = Form(...),
        csrf_token: str = Form(...),
    ):
        if request.state.user:
            return RedirectResponse(url="/dashboard", status_code=303)
        _validate_csrf_token(request, csrf_token)

        login_result = await run_in_threadpool(
            _consume_login_attempt,
            settings.database_path,
            username_or_email,
            password,
        )
        status = str(login_result.get("status", "invalid"))
        source_ip, user_agent = _request_meta(request, settings)
        if status == "locked":
            if "user_id" in login_result:
                actor_user_id = int(login_result["user_id"])
                await run_in_threadpool(
                    _create_audit_log,
                    settings.database_path,
                    actor_user_id=actor_user_id,
                    action="auth.login.locked",
                    target_type="user",
                    target_id=actor_user_id,
                    details=(
                        f"Login blocked due to lockout. Retry after "
                        f"{int(login_result.get('retry_after', 60))}s."
                    ),
                    source_ip=source_ip,
                    user_agent=user_agent,
                )
                alert_result = await run_in_threadpool(
                    _maybe_raise_security_alert,
                    settings,
                    actor_user_id,
                    source_ip,
                    "auth.login.locked",
                    settings.security_alert_failed_login_threshold,
                )
                if bool(alert_result.get("triggered")):
                    logger.warning(
                        "Security alert: login lockout spike for ip=%s (count=%s).",
                        source_ip,
                        alert_result.get("count"),
                    )
            return _render_auth(
                request=request,
                templates=app.state.templates,
                mode="login",
                error=f"Too many failed attempts. Please wait {int(login_result.get('retry_after', 60))}s.",
                registration_mode=settings.registration_mode,
                status_code=429,
            )
        if status != "ok":
            if "user_id" in login_result:
                actor_user_id = int(login_result["user_id"])
                await run_in_threadpool(
                    _create_audit_log,
                    settings.database_path,
                    actor_user_id=actor_user_id,
                    action="auth.login.failed",
                    target_type="user",
                    target_id=actor_user_id,
                    details="Invalid password attempt.",
                    source_ip=source_ip,
                    user_agent=user_agent,
                )
                alert_result = await run_in_threadpool(
                    _maybe_raise_security_alert,
                    settings,
                    actor_user_id,
                    source_ip,
                    "auth.login.failed",
                    settings.security_alert_failed_login_threshold,
                )
                if bool(alert_result.get("triggered")):
                    logger.warning(
                        "Security alert: login failure spike for ip=%s (count=%s).",
                        source_ip,
                        alert_result.get("count"),
                    )
            return _render_auth(
                request=request,
                templates=app.state.templates,
                mode="login",
                error="Invalid credentials.",
                registration_mode=settings.registration_mode,
                status_code=401,
            )

        actor_user_id = int(login_result["user_id"])
        user_row = await run_in_threadpool(_refresh_user, settings.database_path, actor_user_id)
        if user_row is None:
            return _render_auth(
                request=request,
                templates=app.state.templates,
                mode="login",
                error="Account not found.",
                registration_mode=settings.registration_mode,
                status_code=404,
            )

        if settings.admin_mfa_required and int(user_row["is_admin"]) == 1:
            challenge = await run_in_threadpool(
                _create_admin_mfa_challenge,
                settings.database_path,
                actor_user_id,
                settings.admin_mfa_ttl_minutes,
                source_ip,
                user_agent,
            )
            if not bool(challenge.get("issued")):
                return _render_auth(
                    request=request,
                    templates=app.state.templates,
                    mode="login",
                    error="Could not create admin verification challenge.",
                    registration_mode=settings.registration_mode,
                    status_code=500,
                )
            try:
                await run_in_threadpool(
                    __import__("app.routes.public", fromlist=["send_admin_mfa_email"]).send_admin_mfa_email,
                    settings,
                    str(challenge["user_email"]),
                    str(challenge["code"]),
                    str(challenge["expires_at"]),
                )
            except Exception:
                await run_in_threadpool(
                    _create_audit_log,
                    settings.database_path,
                    actor_user_id=actor_user_id,
                    action="auth.mfa.challenge.email.failed",
                    target_type="user",
                    target_id=actor_user_id,
                    details="Admin MFA code delivery failed.",
                    source_ip=source_ip,
                    user_agent=user_agent,
                )
                return _render_auth(
                    request=request,
                    templates=app.state.templates,
                    mode="login",
                    error="Admin verification code could not be delivered.",
                    registration_mode=settings.registration_mode,
                    status_code=503,
                )

            await run_in_threadpool(
                _create_audit_log,
                settings.database_path,
                actor_user_id=actor_user_id,
                action="auth.mfa.challenge.issued",
                target_type="user",
                target_id=actor_user_id,
                details="Admin MFA challenge issued.",
                source_ip=source_ip,
                user_agent=user_agent,
            )

            response = RedirectResponse(url="/admin/mfa", status_code=303)
            response.set_cookie(
                ADMIN_MFA_COOKIE_NAME,
                str(challenge["token"]),
                httponly=True,
                samesite="lax",
                secure=settings.enforce_https,
                max_age=settings.admin_mfa_ttl_minutes * 60,
            )
            return response

        response = RedirectResponse(url="/dashboard", status_code=303)
        _set_session_cookie(response, settings, actor_user_id, request)
        response.delete_cookie(
            ADMIN_MFA_COOKIE_NAME,
            httponly=True,
            samesite="lax",
            secure=settings.enforce_https,
        )
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=actor_user_id,
            action="auth.login.success",
            target_type="user",
            target_id=actor_user_id,
            details="Login successful.",
            source_ip=source_ip,
            user_agent=user_agent,
        )
        return response

    @app.post("/logout")
    async def logout_user(request: Request, csrf_token: str = Form(...)):
        _validate_csrf_token(request, csrf_token)
        session_token = str(request.cookies.get(SESSION_COOKIE_NAME, "")).strip()
        source_ip, user_agent = _request_meta(request, settings)
        current_user = request.state.user
        if session_token:
            await run_in_threadpool(_revoke_session_by_token, settings.database_path, session_token, "logout")
        if current_user is not None:
            await run_in_threadpool(
                _create_audit_log,
                settings.database_path,
                actor_user_id=int(current_user["id"]),
                action="auth.logout",
                target_type="session",
                target_id=getattr(request.state, "session_id", None),
                details="User logged out.",
                source_ip=source_ip,
                user_agent=user_agent,
            )
        response = RedirectResponse(url="/login?notice=Signed+out", status_code=303)
        response.delete_cookie(
            SESSION_COOKIE_NAME,
            httponly=True,
            samesite="lax",
            secure=settings.enforce_https,
        )
        response.delete_cookie(
            ADMIN_MFA_COOKIE_NAME,
            httponly=True,
            samesite="lax",
            secure=settings.enforce_https,
        )
        return response


