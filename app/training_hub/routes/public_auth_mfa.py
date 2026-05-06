from __future__ import annotations

import json
from urllib.parse import quote_plus

from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..config.settings import TrainingHubSettings
from ..core.hub_core import (
    _create_audit_log,
    _maybe_raise_security_alert,
    _refresh_user,
    _set_session_cookie,
    _validate_csrf_token,
)
from ..core.mfa import (
    LOGIN_CHALLENGE_COOKIE_NAME,
    _complete_login_challenge_with_code,
    _generate_passkey_auth_options,
    _mfa_state,
    _validate_login_challenge,
    _verify_passkey_authentication,
)
from .public_utils import logger, mask_email as _mask_email, request_meta as _request_meta


def _clear_login_cookie(response: RedirectResponse | JSONResponse, settings: TrainingHubSettings) -> None:
    response.delete_cookie(
        LOGIN_CHALLENGE_COOKIE_NAME,
        httponly=True,
        samesite="strict",
        secure=settings.enforce_https,
        path="/",
    )


def _mfa_template_context(
    request: Request,
    *,
    notice: str,
    error: str,
    delivery_hint: str,
    expires_at: str,
    mfa_state: dict,
) -> dict:
    return {
        "request": request,
        "current_user": None,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "notice": notice,
        "error": error,
        "delivery_hint": delivery_hint,
        "expires_at": expires_at,
        "mfa_state": mfa_state,
    }


async def _json_body(request: Request) -> dict:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _webauthn_request_context(request: Request) -> tuple[str, str]:
    rp_id = (request.url.hostname or "").strip().lower()
    origin = (request.headers.get("origin") or "").strip().rstrip("/")
    if not origin:
        origin = str(request.base_url).rstrip("/")
    return rp_id, origin


def register_public_auth_mfa_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    @app.get("/mfa", response_class=HTMLResponse)
    @app.get("/admin/mfa", response_class=HTMLResponse)
    async def mfa_page(request: Request, notice: str = "", error: str = ""):
        current_user = request.state.user
        if current_user is not None:
            if int(current_user["is_admin"]) == 1:
                return RedirectResponse(url="/admin", status_code=303)
            return RedirectResponse(url="/dashboard", status_code=303)

        challenge_token = str(request.cookies.get(LOGIN_CHALLENGE_COOKIE_NAME, "")).strip()
        if not challenge_token:
            return RedirectResponse(url="/login?notice=Verification+required", status_code=303)

        source_ip, user_agent = _request_meta(request, settings)
        challenge_state = await run_in_threadpool(
            _validate_login_challenge,
            settings,
            token=challenge_token,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        if not bool(challenge_state.get("ok")):
            message = quote_plus(str(challenge_state.get("error", "Authentication flow is invalid or expired.")))
            response = RedirectResponse(url=f"/login?notice={message}", status_code=303)
            _clear_login_cookie(response, settings)
            return response

        challenge_user = await run_in_threadpool(_refresh_user, settings.database_path, int(challenge_state["user_id"]))
        delivery_hint = ""
        if bool(challenge_state["state"].get("email_bridge_allowed")) and challenge_user is not None:
            delivery_hint = _mask_email(str(challenge_user["email"]))
        context = _mfa_template_context(
            request,
            notice=notice,
            error=error,
            delivery_hint=delivery_hint,
            expires_at=str(challenge_state.get("expires_at", "")),
            mfa_state=dict(challenge_state["state"]),
        )
        return app.state.templates.TemplateResponse(request, "mfa.html", context)

    @app.post("/mfa", response_class=HTMLResponse)
    @app.post("/admin/mfa", response_class=HTMLResponse)
    async def mfa_submit(
        request: Request,
        code: str = Form(...),
        csrf_token: str = Form(...),
    ):
        current_user = request.state.user
        if current_user is not None:
            if int(current_user["is_admin"]) == 1:
                return RedirectResponse(url="/admin", status_code=303)
            return RedirectResponse(url="/dashboard", status_code=303)
        _validate_csrf_token(request, csrf_token)

        challenge_token = str(request.cookies.get(LOGIN_CHALLENGE_COOKIE_NAME, "")).strip()
        if not challenge_token:
            return RedirectResponse(url="/login?notice=Verification+required", status_code=303)

        source_ip, user_agent = _request_meta(request, settings)
        consume_result = await run_in_threadpool(
            _complete_login_challenge_with_code,
            settings,
            token=challenge_token,
            code=code,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        if not bool(consume_result.get("ok")):
            challenge_state = await run_in_threadpool(
                _validate_login_challenge,
                settings,
                token=challenge_token,
                source_ip=source_ip,
                user_agent=user_agent,
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
                    details=str(consume_result.get("error", "MFA verification failed.")),
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
                delivery_hint = ""
                if bool(challenge_state["state"].get("email_bridge_allowed")) and challenge_user is not None:
                    delivery_hint = _mask_email(str(challenge_user["email"]))
                context = _mfa_template_context(
                    request,
                    notice="",
                    error=str(consume_result.get("error", "MFA verification failed.")),
                    delivery_hint=delivery_hint,
                    expires_at=str(challenge_state.get("expires_at", "")),
                    mfa_state=dict(challenge_state["state"]),
                )
                return app.state.templates.TemplateResponse(
                    request,
                    "mfa.html",
                    context,
                    status_code=int(consume_result.get("status_code", 400)),
                )

            message = quote_plus(str(consume_result.get("error", "Verification required.")))
            response = RedirectResponse(url=f"/login?notice={message}", status_code=303)
            _clear_login_cookie(response, settings)
            return response

        actor_user_id = int(consume_result["user_id"])
        state = await run_in_threadpool(_mfa_state, settings, actor_user_id)
        redirect_url = "/admin" if bool(state.get("is_admin")) else "/dashboard"
        if bool(consume_result.get("admin_setup_required")) or bool(state.get("admin_setup_required")):
            redirect_url = "/account/security?notice=Complete+MFA+setup"
        response = RedirectResponse(url=redirect_url, status_code=303)
        _set_session_cookie(response, settings, actor_user_id, request)
        _clear_login_cookie(response, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=actor_user_id,
            action="auth.mfa.verified",
            target_type="user",
            target_id=actor_user_id,
            details=f"MFA verified using {str(consume_result.get('method', 'unknown'))}.",
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

    @app.post("/mfa/passkey/options")
    async def mfa_passkey_options(request: Request):
        challenge_token = str(request.cookies.get(LOGIN_CHALLENGE_COOKIE_NAME, "")).strip()
        if not challenge_token:
            return JSONResponse({"detail": "Verification required."}, status_code=401)

        source_ip, user_agent = _request_meta(request, settings)
        rp_id, origin = _webauthn_request_context(request)
        challenge_state = await run_in_threadpool(
            _validate_login_challenge,
            settings,
            token=challenge_token,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        if not bool(challenge_state.get("ok")):
            return JSONResponse({"detail": str(challenge_state.get("error", "Authentication flow is invalid or expired."))}, status_code=int(challenge_state.get("status_code", 400)))

        options_result = await run_in_threadpool(
            _generate_passkey_auth_options,
            settings,
            user_id=int(challenge_state["user_id"]),
            purpose="mfa",
            rp_id=rp_id,
            expected_origin=origin,
            source_ip=source_ip,
            user_agent=user_agent,
            login_flow_id=int(challenge_state["id"]),
        )
        if not bool(options_result.get("ok")):
            return JSONResponse({"detail": str(options_result.get("error", "Passkey verification is unavailable."))}, status_code=int(options_result.get("status_code", 400)))

        return JSONResponse(
            {
                "flowToken": str(options_result["flow_token"]),
                "publicKey": json.loads(str(options_result["options_json"])),
            },
            status_code=200,
        )

    @app.post("/mfa/passkey/verify")
    async def mfa_passkey_verify(request: Request):
        challenge_token = str(request.cookies.get(LOGIN_CHALLENGE_COOKIE_NAME, "")).strip()
        if not challenge_token:
            return JSONResponse({"detail": "Verification required."}, status_code=401)

        payload = await _json_body(request)
        flow_token = str(payload.get("flowToken", "")).strip()
        credential = payload.get("credential")
        if not flow_token or not isinstance(credential, dict):
            return JSONResponse({"detail": "flowToken and credential are required."}, status_code=400)

        source_ip, user_agent = _request_meta(request, settings)
        verify_result = await run_in_threadpool(
            _verify_passkey_authentication,
            settings,
            flow_token=flow_token,
            credential=credential,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        if not bool(verify_result.get("ok")):
            actor_user_id = int(verify_result["user_id"]) if "user_id" in verify_result else 0
            if actor_user_id:
                await run_in_threadpool(
                    _create_audit_log,
                    settings.database_path,
                    actor_user_id=actor_user_id,
                    action="auth.mfa.failed",
                    target_type="user",
                    target_id=actor_user_id,
                    details=str(verify_result.get("error", "Passkey verification failed.")),
                    source_ip=source_ip,
                    user_agent=user_agent,
                )
            return JSONResponse({"detail": str(verify_result.get("error", "Passkey verification failed."))}, status_code=int(verify_result.get("status_code", 400)))

        actor_user_id = int(verify_result["user_id"])
        state = await run_in_threadpool(_mfa_state, settings, actor_user_id)
        redirect_url = "/admin" if bool(state.get("is_admin")) else "/dashboard"
        if bool(state.get("admin_setup_required")):
            redirect_url = "/account/security?notice=Complete+MFA+setup"
        response = JSONResponse({"ok": True, "redirectUrl": redirect_url}, status_code=200)
        _set_session_cookie(response, settings, actor_user_id, request)
        _clear_login_cookie(response, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=actor_user_id,
            action="auth.mfa.verified",
            target_type="user",
            target_id=actor_user_id,
            details="MFA verified using passkey.",
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
