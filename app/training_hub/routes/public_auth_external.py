from __future__ import annotations

from urllib.parse import parse_qs, quote_plus, urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..config.settings import SESSION_COOKIE_NAME, TrainingHubSettings
from ..core.access_policies import ACCESS_POLICY_DISABLE_LOGIN_NON_ADMIN, _access_policies
from ..core.external_auth import (
    create_external_auth_redirect,
    complete_external_auth_exchange,
    external_auth_provider_display_name,
    external_auth_provider_enabled,
    external_auth_provider_options,
    lookup_external_identity_user,
    _upsert_external_identity_user,
)
from ..core.hub_core import (
    _create_audit_log,
    _refresh_user,
    _revoke_session_by_token,
    _set_session_cookie,
    _validate_csrf_token,
)
from ..core.mfa import _consume_auth_flow_by_id, _create_auth_flow, _load_auth_flow
from .public_utils import logger, request_meta as _request_meta

_ACCOUNT_CONFIRM_COOKIE_NAME = "training_hub_account_confirm"
_ACCOUNT_CONFIRM_TTL_MINUTES = 10


def _set_account_confirm_cookie(response: RedirectResponse, settings: TrainingHubSettings, token: str) -> None:
    response.set_cookie(
        _ACCOUNT_CONFIRM_COOKIE_NAME,
        str(token),
        httponly=True,
        samesite="strict",
        secure=settings.enforce_https,
        max_age=_ACCOUNT_CONFIRM_TTL_MINUTES * 60,
        path="/",
    )


def _step_up_confirm_token(redirect_path: str) -> str:
    parsed = urlsplit(str(redirect_path or ""))
    if parsed.path != "/account/confirm/complete":
        return ""
    values = parse_qs(parsed.query).get("confirm_token", [])
    return str(values[0]).strip() if values else ""


def _load_account_confirm_flow_by_token(
    settings: TrainingHubSettings,
    token: str,
    *,
    source_ip: str,
    user_agent: str,
) -> dict:
    return _load_auth_flow(
        settings,
        token=str(token or "").strip(),
        flow_type="account-confirm",
        source_ip=source_ip,
        user_agent=user_agent,
        max_attempts=settings.admin_mfa_max_attempts,
    )


def _account_confirm_completion_response(
    settings: TrainingHubSettings,
    *,
    user_id: int,
    payload: dict,
    method: str,
    source_ip: str,
    user_agent: str,
) -> RedirectResponse:
    completed_flow = _create_auth_flow(
        settings,
        user_id=int(user_id),
        flow_type="account-confirm",
        payload={**payload, "confirmed_method": str(method)},
        ttl_minutes=_ACCOUNT_CONFIRM_TTL_MINUTES,
        source_ip=source_ip,
        user_agent=user_agent,
    )
    response = RedirectResponse(url="/account/confirm/complete", status_code=303)
    _set_account_confirm_cookie(response, settings, str(completed_flow["token"]))
    return response


def _clear_session_cookie(response: RedirectResponse | JSONResponse, settings: TrainingHubSettings) -> None:
    response.delete_cookie(
        SESSION_COOKIE_NAME,
        httponly=True,
        samesite="strict",
        secure=settings.enforce_https,
        path="/",
    )


def _external_auth_context(request: Request, settings: TrainingHubSettings, notice: str = "", error: str = "") -> dict:
    return {
        "request": request,
        "current_user": request.state.user,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "notice": notice,
        "error": error,
        "providers": external_auth_provider_options(settings),
    }


def register_public_auth_external_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request, notice: str | None = None, error: str | None = None):
        if request.state.user:
            return RedirectResponse(url="/dashboard", status_code=303)
        context = _external_auth_context(request, settings, notice=notice or "", error=error or "")
        if not settings.external_auth_enabled:
            context["error"] = context["error"] or "No external authentication provider is configured."
        return app.state.templates.TemplateResponse(request, "external_login.html", context)

    @app.get("/auth/external/{provider}")
    async def external_auth_start(request: Request, provider: str, next: str = "/dashboard"):
        if request.state.user:
            return RedirectResponse(url="/dashboard", status_code=303)
        if not settings.external_auth_enabled:
            return RedirectResponse(url="/login?error=External+authentication+is+not+configured", status_code=303)
        if not external_auth_provider_enabled(settings, provider):
            return RedirectResponse(url="/login?error=Requested+provider+is+not+available", status_code=303)
        source_ip, user_agent = _request_meta(request, settings)
        try:
            redirect_result = await run_in_threadpool(
                create_external_auth_redirect,
                settings,
                provider=provider,
                next_path=next,
                reauth=False,
                source_ip=source_ip,
                user_agent=user_agent,
            )
        except Exception:
            logger.exception("External auth start failed for provider=%s.", provider)
            return RedirectResponse(url="/login?error=External+sign-in+could+not+be+started", status_code=303)
        if not bool(redirect_result.get("ok")):
            return RedirectResponse(
                url=f"/login?error={quote_plus(str(redirect_result.get('error', 'External sign-in could not be started.')))}",
                status_code=303,
            )
        return RedirectResponse(url=str(redirect_result["redirect_url"]), status_code=303)

    @app.get("/auth/external/{provider}/callback")
    async def external_auth_callback(request: Request, provider: str, code: str = "", state: str = ""):
        if not settings.external_auth_enabled:
            return RedirectResponse(url="/login?error=External+authentication+is+not+configured", status_code=303)
        source_ip, user_agent = _request_meta(request, settings)
        try:
            callback_result = await run_in_threadpool(
                complete_external_auth_exchange,
                settings,
                provider=provider,
                state=state,
                code=code,
                source_ip=source_ip,
                user_agent=user_agent,
            )
        except Exception:
            logger.exception("External auth callback failed for provider=%s.", provider)
            return RedirectResponse(url="/login?error=External+sign-in+failed", status_code=303)
        if not bool(callback_result.get("ok")):
            return RedirectResponse(
                url=f"/login?error={quote_plus(str(callback_result.get('error', 'External sign-in failed.')))}",
                status_code=303,
            )

        redirect_path = str(callback_result.get("redirect_path", "/dashboard"))
        confirm_token = _step_up_confirm_token(redirect_path)
        if confirm_token:
            flow = await run_in_threadpool(
                _load_account_confirm_flow_by_token,
                settings,
                confirm_token,
                source_ip=source_ip,
                user_agent=user_agent,
            )
            if not bool(flow.get("ok")):
                return RedirectResponse(url="/account/security?error=External+confirmation+expired", status_code=303)

            profile = callback_result["profile"]
            linked_result = await run_in_threadpool(lookup_external_identity_user, settings, profile)
            target_page = str(dict(flow.get("payload", {})).get("page", "security") or "security")
            target_user_id = int(flow["user_id"])
            provider_label = external_auth_provider_display_name(provider)
            if not bool(linked_result.get("ok")):
                await run_in_threadpool(
                    _create_audit_log,
                    settings.database_path,
                    actor_user_id=target_user_id,
                    action="auth.external.step_up.blocked",
                    target_type="user",
                    target_id=target_user_id,
                    details=f"Rejected {provider_label} step-up because the callback identity is not linked to this account.",
                    source_ip=source_ip,
                    user_agent=user_agent,
                )
                return RedirectResponse(
                    url=f"/account/{target_page}?error=External+confirmation+did+not+match+this+account",
                    status_code=303,
                )
            if int(linked_result["user_id"]) != target_user_id:
                await run_in_threadpool(
                    _create_audit_log,
                    settings.database_path,
                    actor_user_id=target_user_id,
                    action="auth.external.step_up.blocked",
                    target_type="user",
                    target_id=target_user_id,
                    details=(
                        f"Rejected {provider_label} step-up because the callback identity belongs to user "
                        f"#{int(linked_result['user_id'])}, not the current account."
                    ),
                    source_ip=source_ip,
                    user_agent=user_agent,
                )
                return RedirectResponse(
                    url=f"/account/{target_page}?error=External+confirmation+did+not+match+this+account",
                    status_code=303,
                )

            await run_in_threadpool(_consume_auth_flow_by_id, settings, int(flow["id"]))
            response = _account_confirm_completion_response(
                settings,
                user_id=target_user_id,
                payload=dict(flow["payload"]),
                method=f"external:{provider}",
                source_ip=source_ip,
                user_agent=user_agent,
            )
            _set_session_cookie(response, settings, target_user_id, request)
            await run_in_threadpool(
                _create_audit_log,
                settings.database_path,
                actor_user_id=target_user_id,
                action="auth.external.step_up.success",
                target_type="user",
                target_id=target_user_id,
                details=f"Confirmed a sensitive account action with {provider_label}.",
                source_ip=source_ip,
                user_agent=user_agent,
            )
            return response

        profile = callback_result["profile"]
        user_result = await run_in_threadpool(_upsert_external_identity_user, settings, profile)
        if not bool(user_result.get("ok")):
            return RedirectResponse(
                url=f"/login?error={quote_plus(str(user_result.get('error', 'External sign-in failed.')))}",
                status_code=303,
            )

        user_id = int(user_result["user_id"])
        user_row = await run_in_threadpool(_refresh_user, settings.database_path, user_id)
        if user_row is None:
            return RedirectResponse(url="/login?error=Account+could+not+be+loaded", status_code=303)

        policies = await run_in_threadpool(_access_policies, settings.database_path)
        if bool(policies.get(ACCESS_POLICY_DISABLE_LOGIN_NON_ADMIN)) and int(user_row["is_admin"]) != 1:
            await run_in_threadpool(
                _create_audit_log,
                settings.database_path,
                actor_user_id=user_id,
                action="auth.external.login.blocked_by_policy",
                target_type="user",
                target_id=user_id,
                details=f"External {provider} login blocked by Disable Login policy.",
                source_ip=source_ip,
                user_agent=user_agent,
            )
            return RedirectResponse(url="/login?error=Login+is+currently+disabled+for+non-admin+accounts", status_code=303)

        response = RedirectResponse(url=redirect_path, status_code=303)
        _set_session_cookie(response, settings, user_id, request)
        label = external_auth_provider_display_name(provider)
        details = f"Signed in with {label}."
        if profile is not None:
            details = f"Signed in with {label} subject={getattr(profile, 'subject', '')}."
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=user_id,
            action="auth.external.login.success",
            target_type="user",
            target_id=user_id,
            details=details,
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
        return response
