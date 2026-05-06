from __future__ import annotations

import json

from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..config.settings import SESSION_COOKIE_NAME, TrainingHubSettings
from ..core.hub_core import (
    _consume_login_attempt,
    _create_audit_log,
    _maybe_raise_security_alert,
    _refresh_user,
    _render_auth,
    _revoke_session_by_token,
    _set_session_cookie,
    _validate_csrf_token,
)
from ..core.mfa import (
    LOGIN_CHALLENGE_COOKIE_NAME,
    _create_login_challenge,
    _generate_passkey_auth_options,
    _mfa_state,
    _resolve_user_by_identifier,
    _verify_passkey_authentication,
)
from .public_utils import logger, request_meta as _request_meta


def _clear_login_challenge_cookie(response: RedirectResponse | JSONResponse, settings: TrainingHubSettings) -> None:
    response.delete_cookie(
        LOGIN_CHALLENGE_COOKIE_NAME,
        httponly=True,
        samesite="strict",
        secure=settings.enforce_https,
        path="/",
    )


def _clear_session_cookie(response: RedirectResponse | JSONResponse, settings: TrainingHubSettings) -> None:
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        httponly=True,
        samesite="strict",
        secure=settings.enforce_https,
        path="/",
    )


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
                    details=f"Login blocked due to lockout. Retry after {int(login_result.get('retry_after', 60))}s.",
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

        mfa_state = await run_in_threadpool(_mfa_state, settings, actor_user_id)
        if bool(mfa_state.get("mfa_required_for_login")):
            challenge = await run_in_threadpool(
                _create_login_challenge,
                settings,
                user_id=actor_user_id,
                source_ip=source_ip,
                user_agent=user_agent,
            )
            if bool(challenge.get("allow_email_bridge")):
                try:
                    await run_in_threadpool(
                        __import__("app.training_hub.routes.public", fromlist=["send_admin_mfa_email"]).send_admin_mfa_email,
                        settings,
                        str(user_row["email"]),
                        str(challenge["email_code"]),
                        str(challenge["expires_at"]),
                    )
                except Exception as exception:
                    logger.exception(
                        "Admin MFA bridge email delivery failed for user_id=%s via smtp_host=%s smtp_port=%s.",
                        actor_user_id,
                        settings.smtp_host,
                        settings.smtp_port,
                    )
                    await run_in_threadpool(
                        _create_audit_log,
                        settings.database_path,
                        actor_user_id=actor_user_id,
                        action="auth.mfa.challenge.email.failed",
                        target_type="user",
                        target_id=actor_user_id,
                        details=f"Admin MFA bridge delivery failed: {exception}",
                        source_ip=source_ip,
                        user_agent=user_agent,
                    )
                    return _render_auth(
                        request=request,
                        templates=app.state.templates,
                        mode="login",
                        error="Verification code could not be delivered.",
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
                details="MFA challenge issued after password login.",
                source_ip=source_ip,
                user_agent=user_agent,
            )
            response = RedirectResponse(url="/mfa", status_code=303)
            response.set_cookie(
                LOGIN_CHALLENGE_COOKIE_NAME,
                str(challenge["token"]),
                httponly=True,
                samesite="strict",
                secure=settings.enforce_https,
                max_age=settings.admin_mfa_ttl_minutes * 60,
                path="/",
            )
            return response

        response = RedirectResponse(url="/dashboard", status_code=303)
        _set_session_cookie(response, settings, actor_user_id, request)
        _clear_login_challenge_cookie(response, settings)
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

    @app.post("/login/passkey/options")
    async def login_passkey_options(request: Request):
        if request.state.user:
            return JSONResponse({"ok": True, "redirectUrl": "/dashboard"}, status_code=200)
        payload = await _json_body(request)
        identifier = str(payload.get("identifier", "")).strip()
        source_ip, user_agent = _request_meta(request, settings)
        rp_id, origin = _webauthn_request_context(request)
        if identifier and len(identifier) > 320:
            return JSONResponse({"detail": "identifier must be <= 320 characters."}, status_code=400)

        if identifier:
            user_row = await run_in_threadpool(_resolve_user_by_identifier, settings.database_path, identifier)
            if user_row is None:
                return JSONResponse({"detail": "No passkeys are registered for this account."}, status_code=404)
            options_result = await run_in_threadpool(
                _generate_passkey_auth_options,
                settings,
                user_id=int(user_row["id"]),
                purpose="passwordless-login",
                rp_id=rp_id,
                expected_origin=origin,
                source_ip=source_ip,
                user_agent=user_agent,
            )
        else:
            options_result = await run_in_threadpool(
                _generate_passkey_auth_options,
                settings,
                user_id=1,
                purpose="passwordless-login",
                discoverable=True,
                rp_id=rp_id,
                expected_origin=origin,
                source_ip=source_ip,
                user_agent=user_agent,
            )
        if not bool(options_result.get("ok")):
            return JSONResponse({"detail": str(options_result.get("error", "Passkey login is unavailable."))}, status_code=int(options_result.get("status_code", 400)))

        return JSONResponse(
            {
                "flowToken": str(options_result["flow_token"]),
                "publicKey": json.loads(str(options_result["options_json"])),
            },
            status_code=200,
        )

    @app.post("/login/passkey/verify")
    async def login_passkey_verify(request: Request):
        if request.state.user:
            return JSONResponse({"ok": True, "redirectUrl": "/dashboard"}, status_code=200)
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
                    action="auth.passkey.login.failed",
                    target_type="user",
                    target_id=actor_user_id,
                    details=str(verify_result.get("error", "Passkey login failed.")),
                    source_ip=source_ip,
                    user_agent=user_agent,
                )
            return JSONResponse({"detail": str(verify_result.get("error", "Passkey login failed."))}, status_code=int(verify_result.get("status_code", 400)))

        actor_user_id = int(verify_result["user_id"])
        response = JSONResponse({"ok": True, "redirectUrl": "/dashboard"}, status_code=200)
        _set_session_cookie(response, settings, actor_user_id, request)
        _clear_login_challenge_cookie(response, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=actor_user_id,
            action="auth.passkey.login.success",
            target_type="user",
            target_id=actor_user_id,
            details="Passkey login successful.",
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

    @app.post("/logout")
    async def logout_user(request: Request, csrf_token: str = Form(...)):
        _validate_csrf_token(request, csrf_token)
        session_token = str(request.cookies.get(SESSION_COOKIE_NAME, "")).strip()
        source_ip, user_agent = _request_meta(request, settings)
        current_user = request.state.user
        if session_token:
            await run_in_threadpool(
                _revoke_session_by_token,
                settings.database_path,
                session_token,
                "logout",
                settings.secret_key,
            )
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
        _clear_session_cookie(response, settings)
        _clear_login_challenge_cookie(response, settings)
        return response
