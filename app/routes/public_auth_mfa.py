from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from ..core.hub_core import (
    _consume_admin_mfa_challenge,
    _create_admin_mfa_challenge,
    _create_audit_log,
    _maybe_raise_security_alert,
    _refresh_user,
    _render_auth,
    _set_session_cookie,
    _validate_admin_mfa_challenge,
    _validate_csrf_token,
)
from ..config.settings import TrainingHubSettings
from .public_utils import (
    ADMIN_MFA_COOKIE_NAME,
    logger,
    mask_email as _mask_email,
    request_meta as _request_meta,
)


def register_public_auth_mfa_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    @app.get("/admin/mfa", response_class=HTMLResponse)
    async def admin_mfa_page(request: Request, notice: str = "", error: str = ""):
        current_user = request.state.user
        if current_user is not None:
            if int(current_user["is_admin"]) == 1:
                return RedirectResponse(url="/admin", status_code=303)
            return RedirectResponse(url="/dashboard", status_code=303)
        if not settings.admin_mfa_required:
            return RedirectResponse(url="/login", status_code=303)

        challenge_token = str(request.cookies.get(ADMIN_MFA_COOKIE_NAME, "")).strip()
        if not challenge_token:
            return RedirectResponse(url="/login?notice=Admin+verification+required", status_code=303)

        source_ip, user_agent = _request_meta(request, settings)
        challenge_state = await run_in_threadpool(
            _validate_admin_mfa_challenge,
            settings.database_path,
            challenge_token,
            source_ip,
            user_agent,
            settings.admin_mfa_max_attempts,
        )
        if not bool(challenge_state.get("ok")):
            message = quote_plus(str(challenge_state.get("error", "Verification challenge is invalid or expired.")))
            response = RedirectResponse(url=f"/login?notice={message}", status_code=303)
            response.delete_cookie(
                ADMIN_MFA_COOKIE_NAME,
                httponly=True,
                samesite="lax",
                secure=settings.enforce_https,
            )
            return response

        challenge_user = await run_in_threadpool(_refresh_user, settings.database_path, int(challenge_state["user_id"]))
        delivery_hint = _mask_email(str(challenge_user["email"])) if challenge_user is not None else "your email"
        context = {
            "request": request,
            "current_user": None,
            "csrf_token": getattr(request.state, "csrf_token", ""),
            "notice": notice,
            "error": error,
            "delivery_hint": delivery_hint,
            "expires_at": str(challenge_state.get("expires_at", "")),
        }
        return app.state.templates.TemplateResponse(request, "admin_mfa.html", context)

    @app.post("/admin/mfa", response_class=HTMLResponse)
    async def admin_mfa_submit(
        request: Request,
        code: str = Form(...),
        csrf_token: str = Form(...),
    ):
        current_user = request.state.user
        if current_user is not None:
            if int(current_user["is_admin"]) == 1:
                return RedirectResponse(url="/admin", status_code=303)
            return RedirectResponse(url="/dashboard", status_code=303)
        if not settings.admin_mfa_required:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)

        challenge_token = str(request.cookies.get(ADMIN_MFA_COOKIE_NAME, "")).strip()
        if not challenge_token:
            return RedirectResponse(url="/login?notice=Admin+verification+required", status_code=303)

        source_ip, user_agent = _request_meta(request, settings)
        consume_result = await run_in_threadpool(
            _consume_admin_mfa_challenge,
            settings.database_path,
            challenge_token,
            code,
            source_ip,
            user_agent,
            settings.admin_mfa_max_attempts,
        )
        if not bool(consume_result.get("ok")):
            status_code = int(consume_result.get("status_code", 400))
            challenge_state = await run_in_threadpool(
                _validate_admin_mfa_challenge,
                settings.database_path,
                challenge_token,
                source_ip,
                user_agent,
                settings.admin_mfa_max_attempts,
            )
            if bool(challenge_state.get("ok")):
                actor_user_id = int(challenge_state["user_id"])
                await run_in_threadpool(
                    _create_audit_log,
                    settings.database_path,
                    actor_user_id=actor_user_id,
                    action="auth.mfa.failed",
                    target_type="user",
                    target_id=actor_user_id,
                    details=str(consume_result.get("error", "Admin MFA failed.")),
                    source_ip=source_ip,
                    user_agent=user_agent,
                )
                alert_result = await run_in_threadpool(
                    _maybe_raise_security_alert,
                    settings,
                    actor_user_id,
                    source_ip,
                    "auth.mfa.failed",
                    settings.security_alert_mfa_failed_threshold,
                )
                if bool(alert_result.get("triggered")):
                    logger.warning(
                        "Security alert: MFA failure spike for ip=%s (count=%s).",
                        source_ip,
                        alert_result.get("count"),
                    )

                challenge_user = await run_in_threadpool(_refresh_user, settings.database_path, actor_user_id)
                delivery_hint = _mask_email(str(challenge_user["email"])) if challenge_user is not None else "your email"
                context = {
                    "request": request,
                    "current_user": None,
                    "csrf_token": getattr(request.state, "csrf_token", ""),
                    "notice": "",
                    "error": str(consume_result.get("error", "Admin verification failed.")),
                    "delivery_hint": delivery_hint,
                    "expires_at": str(challenge_state.get("expires_at", "")),
                }
                return app.state.templates.TemplateResponse(
                    request,
                    "admin_mfa.html",
                    context,
                    status_code=status_code,
                )

            message = quote_plus(str(consume_result.get("error", "Admin verification required.")))
            response = RedirectResponse(url=f"/login?notice={message}", status_code=303)
            response.delete_cookie(
                ADMIN_MFA_COOKIE_NAME,
                httponly=True,
                samesite="lax",
                secure=settings.enforce_https,
            )
            return response

        actor_user_id = int(consume_result["user_id"])
        response = RedirectResponse(url="/admin", status_code=303)
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
            action="auth.mfa.verified",
            target_type="user",
            target_id=actor_user_id,
            details="Admin MFA verified.",
            source_ip=source_ip,
            user_agent=user_agent,
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


