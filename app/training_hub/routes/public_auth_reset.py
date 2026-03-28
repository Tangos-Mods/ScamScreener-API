from __future__ import annotations

from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from ..core.hub_core import (
    _create_audit_log,
    _create_password_reset_request,
    _maybe_raise_security_alert,
    _reset_password_with_token,
    _validate_csrf_token,
    _validate_password_reset_token,
)
from ..config.settings import TrainingHubSettings
from .public_utils import logger, request_meta as _request_meta


def register_public_auth_password_reset_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    @app.get("/forgot-password", response_class=HTMLResponse)
    async def forgot_password_page(request: Request):
        if request.state.user:
            return RedirectResponse(url="/dashboard", status_code=303)
        context = {
            "request": request,
            "current_user": request.state.user,
            "csrf_token": getattr(request.state, "csrf_token", ""),
            "notice": "",
            "error": "",
            "reset_link": "",
        }
        return app.state.templates.TemplateResponse(request, "forgot_password.html", context)

    @app.post("/forgot-password", response_class=HTMLResponse)
    async def forgot_password_submit(
        request: Request,
        username_or_email: str = Form(...),
        csrf_token: str = Form(...),
    ):
        if request.state.user:
            return RedirectResponse(url="/dashboard", status_code=303)
        _validate_csrf_token(request, csrf_token)

        source_ip, user_agent = _request_meta(request, settings)
        reset_result = await run_in_threadpool(
            _create_password_reset_request,
            settings.database_path,
            username_or_email,
            settings.password_reset_ttl_minutes,
            source_ip,
            user_agent,
            settings.secret_key,
        )

        reset_link = ""
        if bool(reset_result.get("issued")) and "token" in reset_result:
            base_url = settings.public_base_url or str(request.base_url).rstrip("/")
            reset_link = f"{base_url}/reset-password?token={reset_result['token']}"

        if bool(reset_result.get("issued")) and "user_id" in reset_result:
            actor_user_id = int(reset_result["user_id"])
            await run_in_threadpool(
                _create_audit_log,
                settings.database_path,
                actor_user_id=actor_user_id,
                action="auth.password.reset.requested",
                target_type="user",
                target_id=actor_user_id,
                details="Password reset requested.",
                source_ip=source_ip,
                user_agent=user_agent,
            )
            alert_result = await run_in_threadpool(
                _maybe_raise_security_alert,
                settings,
                actor_user_id,
                source_ip,
                "auth.password.reset.requested",
                settings.security_alert_password_reset_threshold,
            )
            if bool(alert_result.get("triggered")):
                logger.warning(
                    "Security alert: password reset spike for ip=%s (count=%s).",
                    source_ip,
                    alert_result.get("count"),
                )
            if settings.password_reset_send_email and "user_email" in reset_result and reset_link:
                try:
                    await run_in_threadpool(
                        __import__("app.training_hub.routes.public", fromlist=["send_password_reset_email"]).send_password_reset_email,
                        settings,
                        str(reset_result["user_email"]),
                        reset_link,
                        str(reset_result.get("expires_at", "")),
                    )
                    await run_in_threadpool(
                        _create_audit_log,
                        settings.database_path,
                        actor_user_id=int(reset_result["user_id"]),
                        action="auth.password.reset.email.sent",
                        target_type="user",
                        target_id=int(reset_result["user_id"]),
                        details="Password reset email sent.",
                        source_ip=source_ip,
                        user_agent=user_agent,
                    )
                except Exception as exception:
                    await run_in_threadpool(
                        _create_audit_log,
                        settings.database_path,
                        actor_user_id=int(reset_result["user_id"]),
                        action="auth.password.reset.email.failed",
                        target_type="user",
                        target_id=int(reset_result["user_id"]),
                        details=f"Password reset email failed: {exception}",
                        source_ip=source_ip,
                        user_agent=user_agent,
                    )

        reset_link_display = ""
        if settings.password_reset_show_token and reset_link:
            reset_link_display = reset_link

        context = {
            "request": request,
            "current_user": request.state.user,
            "csrf_token": getattr(request.state, "csrf_token", ""),
            "notice": (
                "If an account exists for that identifier, a reset link has been generated. "
                "Check your secure delivery channel."
            ),
            "error": "",
            "reset_link": reset_link_display,
        }
        return app.state.templates.TemplateResponse(request, "forgot_password.html", context)

    @app.get("/reset-password", response_class=HTMLResponse)
    async def reset_password_page(request: Request, token: str = ""):
        if request.state.user:
            return RedirectResponse(url="/dashboard", status_code=303)

        token_state = await run_in_threadpool(_validate_password_reset_token, settings.database_path, token, settings.secret_key)
        context = {
            "request": request,
            "current_user": request.state.user,
            "csrf_token": getattr(request.state, "csrf_token", ""),
            "token": token,
            "token_valid": bool(token_state.get("ok")),
            "notice": "",
            "error": "" if bool(token_state.get("ok")) else str(token_state.get("error", "Invalid reset token.")),
        }
        return app.state.templates.TemplateResponse(request, "reset_password.html", context)

    @app.post("/reset-password", response_class=HTMLResponse)
    async def reset_password_submit(
        request: Request,
        token: str = Form(...),
        new_password: str = Form(...),
        new_password_confirm: str = Form(...),
        csrf_token: str = Form(...),
    ):
        if request.state.user:
            return RedirectResponse(url="/dashboard", status_code=303)
        _validate_csrf_token(request, csrf_token)

        if (new_password or "") != (new_password_confirm or ""):
            context = {
                "request": request,
                "current_user": request.state.user,
                "csrf_token": getattr(request.state, "csrf_token", ""),
                "token": token,
                "token_valid": True,
                "notice": "",
                "error": "New password confirmation does not match.",
            }
            return app.state.templates.TemplateResponse(request, "reset_password.html", context, status_code=400)

        reset_result = await run_in_threadpool(
            _reset_password_with_token,
            settings.database_path,
            token,
            new_password,
            settings.secret_key,
        )
        if not bool(reset_result.get("ok")):
            context = {
                "request": request,
                "current_user": request.state.user,
                "csrf_token": getattr(request.state, "csrf_token", ""),
                "token": token,
                "token_valid": False,
                "notice": "",
                "error": str(reset_result.get("error", "Password reset failed.")),
            }
            return app.state.templates.TemplateResponse(
                request,
                "reset_password.html",
                context,
                status_code=int(reset_result.get("status_code", 400)),
            )

        source_ip, user_agent = _request_meta(request, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(reset_result["user_id"]),
            action="auth.password.reset",
            target_type="user",
            target_id=int(reset_result["user_id"]),
            details=f"Password reset completed. Revoked {int(reset_result.get('revoked_sessions', 0))} sessions.",
            source_ip=source_ip,
            user_agent=user_agent,
        )
        return RedirectResponse(url="/login?notice=Password+reset+successful", status_code=303)


