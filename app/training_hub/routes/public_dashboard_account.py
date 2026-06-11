from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from ..config.settings import SESSION_COOKIE_NAME, TrainingHubSettings
from ..core.external_auth import (
    create_external_auth_redirect,
    external_auth_provider_display_name,
    external_auth_provider_enabled,
    external_identities_for_user,
)
from ..core.hub_core import (
    _create_audit_log,
    _delete_user_account,
    _link_client_identity_to_user,
    _purge_user_uploads,
    _queue_user_data_export_request,
    _refresh_user,
    _render_account,
    _render_dashboard,
    _revoke_other_user_sessions,
    _revoke_user_session_by_id,
    _unlink_client_identity_from_user,
    _validate_csrf_token,
)
from ..core.mfa import (
    _consume_auth_flow_by_id,
    _create_auth_flow,
    _load_auth_flow,
)
from .public_utils import request_meta as _request_meta

ACCOUNT_CONFIRM_COOKIE_NAME = "training_hub_account_confirm"
ACCOUNT_CONFIRM_TTL_MINUTES = 10


def _clear_account_confirm_cookie(response: RedirectResponse, settings: TrainingHubSettings) -> None:
    response.delete_cookie(
        ACCOUNT_CONFIRM_COOKIE_NAME,
        httponly=True,
        samesite="strict",
        secure=settings.enforce_https,
        path="/",
    )


def _set_account_confirm_cookie(
    response: RedirectResponse,
    settings: TrainingHubSettings,
    token: str,
    ttl_minutes: int = ACCOUNT_CONFIRM_TTL_MINUTES,
) -> None:
    response.set_cookie(
        ACCOUNT_CONFIRM_COOKIE_NAME,
        str(token),
        httponly=True,
        samesite="strict",
        secure=settings.enforce_https,
        max_age=max(1, int(ttl_minutes)) * 60,
        path="/",
    )


def _account_action_metadata(action: str) -> dict[str, str]:
    metadata = {
        "client-link": {
            "page": "clients",
            "title": "Confirm client ID link",
            "description": "Confirm this action with one of your linked external identity providers before linking the client ID.",
        },
        "client-unlink": {
            "page": "clients",
            "title": "Confirm client ID unlink",
            "description": "Confirm this action with one of your linked external identity providers before unlinking the client ID.",
        },
        "data-export-request": {
            "page": "privacy",
            "title": "Confirm data export request",
            "description": "Confirm this action with one of your linked external identity providers before queueing a data export.",
        },
        "data-purge": {
            "page": "privacy",
            "title": "Confirm upload data purge",
            "description": "Confirm this action with one of your linked external identity providers before deleting uploads and case contributions.",
        },
        "account-delete": {
            "page": "privacy",
            "title": "Confirm account deletion",
            "description": "Confirm this action with one of your linked external identity providers before permanently deleting this account.",
        },
    }
    return dict(metadata.get(str(action), {}))


def _account_action_page(payload: dict[str, str]) -> str:
    base_action = str(payload.get("action", "")).split(":", 1)[0]
    metadata = _account_action_metadata(base_action)
    return metadata.get("page", str(payload.get("page", "security") or "security"))


def _account_action_summary(payload: dict[str, str]) -> str:
    base_action = str(payload.get("action", "")).split(":", 1)[0]
    values = dict(payload.get("values", {})) if isinstance(payload.get("values"), dict) else {}
    if base_action == "client-link" and values.get("client_id"):
        return f"Client ID: {values['client_id']}"
    if base_action == "client-unlink" and values.get("client_identity_id"):
        return f"Linked client entry #{values['client_identity_id']}"
    return ""


def _start_account_confirm_response(
    settings: TrainingHubSettings,
    *,
    user_id: int,
    page: str,
    action: str,
    values: dict[str, str] | None,
    source_ip: str,
    user_agent: str,
) -> RedirectResponse:
    flow = _create_auth_flow(
        settings,
        user_id=int(user_id),
        flow_type="account-confirm",
        payload={
            "page": str(page or "security"),
            "action": str(action or ""),
            "values": dict(values or {}),
        },
        ttl_minutes=ACCOUNT_CONFIRM_TTL_MINUTES,
        source_ip=source_ip,
        user_agent=user_agent,
    )
    response = RedirectResponse(url="/account/confirm", status_code=303)
    _set_account_confirm_cookie(response, settings, str(flow["token"]))
    return response


def _load_account_confirm_flow(
    request: Request,
    settings: TrainingHubSettings,
    *,
    source_ip: str,
    user_agent: str,
) -> dict:
    flow_token = str(request.cookies.get(ACCOUNT_CONFIRM_COOKIE_NAME, "")).strip()
    if not flow_token:
        return {"ok": False, "error": "Confirmation required.", "status_code": 400}
    return _load_auth_flow(
        settings,
        token=flow_token,
        flow_type="account-confirm",
        source_ip=source_ip,
        user_agent=user_agent,
        max_attempts=settings.admin_mfa_max_attempts,
    )


def _account_confirm_context(
    request: Request,
    *,
    settings: TrainingHubSettings,
    user: dict,
    flow: dict,
    notice: str = "",
    error: str = "",
) -> dict:
    payload = dict(flow.get("payload", {}))
    base_action = str(payload.get("action", "")).split(":", 1)[0]
    metadata = _account_action_metadata(base_action)
    linked_identities = external_identities_for_user(settings, int(user["id"]))
    provider_options: list[dict[str, str]] = []
    seen_providers: set[str] = set()
    for row in linked_identities:
        provider_name = str(row.get("provider", "")).strip().lower()
        if not provider_name or provider_name in seen_providers:
            continue
        if not external_auth_provider_enabled(settings, provider_name):
            continue
        seen_providers.add(provider_name)
        provider_options.append({"name": provider_name, "label": external_auth_provider_display_name(provider_name)})
    return {
        "request": request,
        "current_user": user,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "notice": notice,
        "error": error,
        "account_page": _account_action_page(payload),
        "confirm_title": metadata.get("title", "Confirm account action"),
        "confirm_description": metadata.get("description", "Verify this action before it is applied."),
        "confirm_summary": _account_action_summary(payload),
        "confirm_action": base_action,
        "confirm_flow_expires_at": str(flow.get("expires_at", "")),
        "confirm_provider_options": provider_options,
        "confirm_provider_count": len(provider_options),
    }


def _account_security_redirect() -> RedirectResponse:
    return RedirectResponse(url="/account/security", status_code=303)


def _action_values(**values: str) -> dict[str, str]:
    return {key: str(value) for key, value in values.items() if str(value or "")}


async def _render_account_action(
    *,
    app: FastAPI,
    request: Request,
    settings: TrainingHubSettings,
    user: dict,
    page: str,
    active_action: str,
    action_error: str,
    status_code: int = 400,
    action_values: dict[str, str] | None = None,
    pending_totp: dict | None = None,
):
    return await run_in_threadpool(
        _render_account,
        request=request,
        templates=app.state.templates,
        settings=settings,
        user=user,
        error="",
        status_code=status_code,
        page=page,
        pending_totp=pending_totp,
        active_action=active_action,
        action_values=action_values,
        action_error=action_error,
    )


async def _render_account_confirm(
    *,
    app: FastAPI,
    request: Request,
    settings: TrainingHubSettings,
    user: dict,
    flow: dict,
    notice: str = "",
    error: str = "",
    status_code: int = 200,
):
    context = _account_confirm_context(request, settings=settings, user=user, flow=flow, notice=notice, error=error)
    return app.state.templates.TemplateResponse(request, "account_confirm.html", context, status_code=status_code)


async def _execute_confirmed_account_action(
    *,
    app: FastAPI,
    request: Request,
    settings: TrainingHubSettings,
    user: dict,
    payload: dict[str, str],
    method: str,
):
    action = str(payload.get("action", "")).split(":", 1)[0]
    page = _account_action_page(payload)
    values = dict(payload.get("values", {})) if isinstance(payload.get("values"), dict) else {}
    source_ip, user_agent = _request_meta(request, settings)

    if action == "client-link":
        link_result = await run_in_threadpool(_link_client_identity_to_user, settings.database_path, int(user["id"]), str(values.get("client_id", "")))
        if not bool(link_result.get("ok")):
            return await run_in_threadpool(
                _render_account,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error=str(link_result.get("error", "Client ID could not be linked.")),
                status_code=int(link_result.get("status_code", 400)),
                page=page,
            )
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="account.client_identity.linked",
            target_type="client_identity",
            target_id=int(link_result["client_identity_id"]),
            details=(
                f"Linked client ID {link_result['normalized_client_id']} after step-up auth using {method}. "
                f"Historical uploads: {int(link_result['upload_count'])}, cases: {int(link_result['case_count'])}."
            ),
            source_ip=source_ip,
            user_agent=user_agent,
        )
        refreshed_user = await run_in_threadpool(_refresh_user, settings.database_path, int(user["id"])) or user
        notice = f"Client ID {link_result['normalized_client_id']} linked."
        if str(link_result.get('status', '')) == "already-linked":
            notice = f"Client ID {link_result['normalized_client_id']} is already linked to your account."
        return await run_in_threadpool(
            _render_account,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=refreshed_user,
            notice=notice,
            page=page,
        )

    if action == "client-unlink":
        unlink_result = await run_in_threadpool(
            _unlink_client_identity_from_user,
            settings.database_path,
            int(user["id"]),
            int(values.get("client_identity_id", "0") or 0),
        )
        if not bool(unlink_result.get("ok")):
            return await run_in_threadpool(
                _render_account,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error=str(unlink_result.get("error", "Client ID could not be unlinked.")),
                status_code=int(unlink_result.get("status_code", 400)),
                page=page,
            )
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="account.client_identity.unlinked",
            target_type="client_identity",
            target_id=int(unlink_result["client_identity_id"]),
            details=(
                f"Unlinked client ID {unlink_result['normalized_client_id']} after step-up auth using {method}. "
                f"Historical uploads detached: {int(unlink_result['upload_count'])}, cases: {int(unlink_result['case_count'])}."
            ),
            source_ip=source_ip,
            user_agent=user_agent,
        )
        refreshed_user = await run_in_threadpool(_refresh_user, settings.database_path, int(user["id"])) or user
        return await run_in_threadpool(
            _render_account,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=refreshed_user,
            notice=f"Client ID {unlink_result['normalized_client_id']} unlinked.",
            page=page,
        )

    if action == "data-export-request":
        queue_result = await run_in_threadpool(
            _queue_user_data_export_request,
            settings,
            int(user["id"]),
            source_ip,
            user_agent,
        )
        if not bool(queue_result.get("ok")):
            return await run_in_threadpool(
                _render_account,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error=str(queue_result.get("error", "Could not queue account data export.")),
                status_code=int(queue_result.get("status_code", 400)),
                page=page,
            )
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="account.data_export.requested",
            target_type="data_export_request",
            target_id=int(queue_result["request_id"]),
            details=f"Queued account data export request #{int(queue_result['request_id'])} after step-up auth using {method}.",
            source_ip=source_ip,
            user_agent=user_agent,
        )
        data_export_wake = getattr(app.state, "data_export_wake", None)
        if data_export_wake is not None:
            data_export_wake.set()
        refreshed_user = await run_in_threadpool(_refresh_user, settings.database_path, int(user["id"])) or user
        return await run_in_threadpool(
            _render_account,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=refreshed_user,
            notice=f"Account data export requested. It will be emailed to {queue_result['recipient_email']}.",
            status_code=202,
            page=page,
        )

    if action == "data-purge":
        purge_result = await run_in_threadpool(_purge_user_uploads, settings, int(user["id"]))
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="account.data.purged",
            target_type="user",
            target_id=int(user["id"]),
            details=(
                f"Purged own uploads and cases after step-up auth using {method}. "
                f"Uploads deleted: {int(purge_result['deleted_uploads'])}, "
                f"cases deleted: {int(purge_result['deleted_cases'])}, cases rebuilt: {int(purge_result['rebuilt_cases'])}."
            ),
            source_ip=source_ip,
            user_agent=user_agent,
        )
        refreshed_user = await run_in_threadpool(_refresh_user, settings.database_path, int(user["id"])) or user
        return await run_in_threadpool(
            _render_account,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=refreshed_user,
            notice=(
                f"Deleted {int(purge_result['deleted_uploads'])} uploads. "
                f"Cases removed: {int(purge_result['deleted_cases'])}, rebuilt from remaining uploads: {int(purge_result['rebuilt_cases'])}."
            ),
            page=page,
        )

    if action == "account-delete":
        delete_result = await run_in_threadpool(_delete_user_account, settings, int(user["id"]))
        if not bool(delete_result.get("ok")):
            return await run_in_threadpool(
                _render_account,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error=str(delete_result.get("error", "Account deletion failed.")),
                status_code=int(delete_result.get("status_code", 400)),
                page=page,
            )
        response = RedirectResponse(url="/login?notice=Account+deleted", status_code=303)
        response.delete_cookie(
            SESSION_COOKIE_NAME,
            httponly=True,
            samesite="strict",
            secure=settings.enforce_https,
            path="/",
        )
        _clear_account_confirm_cookie(response, settings)
        return response

    return await run_in_threadpool(
        _render_account,
        request=request,
        templates=app.state.templates,
        settings=settings,
        user=user,
        error="Unsupported account action.",
        status_code=400,
        page=page,
    )


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
            page="overview",
        )

    @app.get("/dashboard/uploads", response_class=HTMLResponse)
    async def dashboard_uploads(request: Request):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        return await run_in_threadpool(
            _render_dashboard,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=user,
            page="uploads",
        )

    @app.get("/dashboard/account", response_class=HTMLResponse)
    async def dashboard_account_redirect(request: Request):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        return _account_security_redirect()

    @app.get("/account/security", response_class=HTMLResponse)
    async def account_security(request: Request, notice: str = "", error: str = ""):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        return await run_in_threadpool(
            _render_account,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=user,
            notice=notice,
            error=error,
            page="security",
        )

    @app.get("/account/sessions", response_class=HTMLResponse)
    async def account_sessions(request: Request, notice: str = "", error: str = ""):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        return await run_in_threadpool(
            _render_account,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=user,
            notice=notice,
            error=error,
            page="sessions",
        )

    @app.get("/account/clients", response_class=HTMLResponse)
    async def account_clients(request: Request, notice: str = "", error: str = ""):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        return await run_in_threadpool(
            _render_account,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=user,
            notice=notice,
            error=error,
            page="clients",
        )

    @app.get("/account/privacy", response_class=HTMLResponse)
    async def account_privacy(request: Request, notice: str = "", error: str = ""):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        return await run_in_threadpool(
            _render_account,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=user,
            notice=notice,
            error=error,
            page="privacy",
        )

    @app.get("/account/confirm", response_class=HTMLResponse)
    async def account_confirm_page(request: Request, notice: str = "", error: str = ""):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        source_ip, user_agent = _request_meta(request, settings)
        flow = await run_in_threadpool(
            _load_account_confirm_flow,
            request,
            settings,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        if not bool(flow.get("ok")):
            message = quote_plus(str(flow.get("error", "Confirmation required.")))
            response = RedirectResponse(url=f"/account/{_account_action_page({})}?error={message}", status_code=303)
            _clear_account_confirm_cookie(response, settings)
            return response
        if int(flow["user_id"]) != int(user["id"]):
            response = RedirectResponse(url="/account/security?error=Confirmation+is+invalid+for+this+account", status_code=303)
            _clear_account_confirm_cookie(response, settings)
            return response
        context = _account_confirm_context(request, settings=settings, user=user, flow=flow, notice=notice, error=error)
        provider_options = list(context.get("confirm_provider_options", []))
        if not provider_options:
            response = RedirectResponse(
                url=f"/account/{_account_action_page(dict(flow.get('payload', {})))}?error=No+linked+external+provider+is+available+for+confirmation",
                status_code=303,
            )
            _clear_account_confirm_cookie(response, settings)
            return response
        if len(provider_options) == 1:
            provider_name = str(provider_options[0].get("name", "")).strip().lower()
            return RedirectResponse(url=f"/account/confirm/external/{provider_name}", status_code=303)
        return await _render_account_confirm(
            app=app,
            request=request,
            settings=settings,
            user=user,
            flow=flow,
            notice=notice,
            error=error,
        )

    @app.get("/account/confirm/external/{provider}")
    async def account_confirm_external_start(request: Request, provider: str):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        source_ip, user_agent = _request_meta(request, settings)
        flow = await run_in_threadpool(
            _load_account_confirm_flow,
            request,
            settings,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        if not bool(flow.get("ok")):
            response = RedirectResponse(url="/account/security?error=Confirmation+required", status_code=303)
            _clear_account_confirm_cookie(response, settings)
            return response
        if int(flow.get("user_id", 0)) != int(user["id"]):
            response = RedirectResponse(url="/account/security?error=Confirmation+is+invalid+for+this+account", status_code=303)
            _clear_account_confirm_cookie(response, settings)
            return response
        normalized_provider = str(provider or "").strip().lower()
        linked_providers = {
            str(row.get("provider", "")).strip().lower()
            for row in external_identities_for_user(settings, int(user["id"]))
            if external_auth_provider_enabled(settings, str(row.get("provider", "")).strip().lower())
        }
        if normalized_provider not in linked_providers:
            page = _account_action_page(dict(flow.get("payload", {})))
            return RedirectResponse(
                url=f"/account/{page}?error=This+provider+is+not+linked+to+your+account",
                status_code=303,
            )

        confirm_token = str(request.cookies.get(ACCOUNT_CONFIRM_COOKIE_NAME, "")).strip()
        if not confirm_token:
            response = RedirectResponse(url="/account/security?error=Confirmation+required", status_code=303)
            _clear_account_confirm_cookie(response, settings)
            return response
        try:
            redirect_result = await run_in_threadpool(
                create_external_auth_redirect,
                settings,
                provider=normalized_provider,
                next_path=f"/account/confirm/complete?confirm_token={quote_plus(confirm_token)}",
                reauth=True,
                source_ip=source_ip,
                user_agent=user_agent,
            )
        except Exception:
            return RedirectResponse(
                url=f"/account/{_account_action_page(dict(flow.get('payload', {})))}?error=External+confirmation+could+not+be+started",
                status_code=303,
            )
        if not bool(redirect_result.get("ok")):
            return RedirectResponse(
                url=f"/account/{_account_action_page(dict(flow.get('payload', {})))}?error={quote_plus(str(redirect_result.get('error', 'External confirmation could not be started.')))}",
                status_code=303,
            )
        return RedirectResponse(url=str(redirect_result["redirect_url"]), status_code=303)

    @app.get("/account/confirm/complete", response_class=HTMLResponse)
    async def account_confirm_complete(request: Request):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        source_ip, user_agent = _request_meta(request, settings)
        flow = await run_in_threadpool(
            _load_account_confirm_flow,
            request,
            settings,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        if not bool(flow.get("ok")) or int(flow.get("user_id", 0)) != int(user["id"]):
            response = RedirectResponse(url="/account/security?error=Confirmation+required", status_code=303)
            _clear_account_confirm_cookie(response, settings)
            return response
        payload = dict(flow["payload"])
        method = str(payload.get("confirmed_method", "") or "")
        if not method:
            response = RedirectResponse(url="/account/security?error=Confirmation+state+is+invalid", status_code=303)
            _clear_account_confirm_cookie(response, settings)
            return response
        await run_in_threadpool(_consume_auth_flow_by_id, settings, int(flow["id"]))
        response = await _execute_confirmed_account_action(
            app=app,
            request=request,
            settings=settings,
            user=user,
            payload=payload,
            method=method,
        )
        _clear_account_confirm_cookie(response, settings)
        return response

    @app.post("/account/sessions/revoke-others", response_class=HTMLResponse)
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
            _render_account,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=refreshed_user,
            notice=f"Revoked {revoked_count} other sessions.",
            page="sessions",
        )

    @app.post("/account/sessions/{session_id}/revoke", response_class=HTMLResponse)
    async def revoke_single_session(request: Request, session_id: int, csrf_token: str = Form(...)):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)

        current_session_id = getattr(request.state, "session_id", None)
        if current_session_id is not None and int(current_session_id) == int(session_id):
            return await run_in_threadpool(
                _render_account,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error="Use logout to end your current session.",
                status_code=400,
                page="sessions",
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
                _render_account,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error="Session not found or already revoked.",
                status_code=404,
                page="sessions",
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
            _render_account,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=refreshed_user,
            notice=f"Revoked session #{session_id}.",
            page="sessions",
        )

    @app.post("/account/clients/link", response_class=HTMLResponse)
    async def link_client_id(
        request: Request,
        client_id: str = Form(...),
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)
        source_ip, user_agent = _request_meta(request, settings)
        return _start_account_confirm_response(
            settings,
            user_id=int(user["id"]),
            page="clients",
            action="client-link",
            values=_action_values(client_id=client_id),
            source_ip=source_ip,
            user_agent=user_agent,
        )

    @app.post("/account/clients/{client_identity_id}/unlink", response_class=HTMLResponse)
    async def unlink_client_id(
        request: Request,
        client_identity_id: int,
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)
        source_ip, user_agent = _request_meta(request, settings)
        return _start_account_confirm_response(
            settings,
            user_id=int(user["id"]),
            page="clients",
            action="client-unlink",
            values={"client_identity_id": str(int(client_identity_id))},
            source_ip=source_ip,
            user_agent=user_agent,
        )

    @app.post("/account/privacy/data-export/request", response_class=HTMLResponse)
    async def request_account_data_export(
        request: Request,
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)
        source_ip, user_agent = _request_meta(request, settings)
        return _start_account_confirm_response(
            settings,
            user_id=int(user["id"]),
            page="privacy",
            action="data-export-request",
            values={},
            source_ip=source_ip,
            user_agent=user_agent,
        )

    @app.post("/account/privacy/data/purge", response_class=HTMLResponse)
    async def purge_own_uploads_and_cases(
        request: Request,
        confirmation: str = Form(...),
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)
        if (confirmation or "").strip() != "ERASE MY DATA":
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="privacy",
                active_action="data-purge",
                action_error="Type ERASE MY DATA exactly to confirm deleting your uploads and cases.",
                status_code=400,
                action_values=_action_values(confirmation=confirmation),
            )
        source_ip, user_agent = _request_meta(request, settings)
        return _start_account_confirm_response(
            settings,
            user_id=int(user["id"]),
            page="privacy",
            action="data-purge",
            values={},
            source_ip=source_ip,
            user_agent=user_agent,
        )

    @app.post("/account/privacy/account/delete", response_class=HTMLResponse)
    async def delete_own_account(
        request: Request,
        confirmation: str = Form(...),
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)
        if (confirmation or "").strip() != "DELETE MY ACCOUNT":
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="privacy",
                active_action="account-delete",
                action_error="Type DELETE MY ACCOUNT exactly to confirm permanent account deletion.",
                status_code=400,
                action_values=_action_values(confirmation=confirmation),
            )
        source_ip, user_agent = _request_meta(request, settings)
        return _start_account_confirm_response(
            settings,
            user_id=int(user["id"]),
            page="privacy",
            action="account-delete",
            values={},
            source_ip=source_ip,
            user_agent=user_agent,
        )
