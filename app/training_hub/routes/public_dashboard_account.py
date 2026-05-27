from __future__ import annotations

import json
from urllib.parse import quote_plus

from fastapi import FastAPI, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..config.settings import SESSION_COOKIE_NAME, TrainingHubSettings
from ..core.hub_core import (
    _change_user_password,
    _change_user_password_after_reauth,
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
    _verify_user_action_password,
)
from ..core.mfa import (
    WEBAUTHN_FLOW_TTL_MINUTES,
    _consume_auth_flow_by_id,
    _create_auth_flow,
    _create_totp_enrollment,
    _decrypt_value,
    _disable_user_mfa,
    _encrypt_value,
    _generate_passkey_auth_options,
    _generate_passkey_registration_options,
    _load_auth_flow,
    _mfa_state,
    _pending_totp_enrollment,
    _regenerate_backup_codes,
    _remove_passkey,
    _remove_totp_factor,
    _verify_passkey_authentication,
    _verify_passkey_registration,
    _verify_totp_enrollment,
    _verify_user_step_up_code,
)
from .public_utils import request_meta as _request_meta, webauthn_request_context as _webauthn_request_context

ACCOUNT_CONFIRM_COOKIE_NAME = "training_hub_account_confirm"
ACCOUNT_CONFIRM_TTL_MINUTES = 10


def _clear_account_confirm_cookie(response: RedirectResponse | JSONResponse, settings: TrainingHubSettings) -> None:
    response.delete_cookie(
        ACCOUNT_CONFIRM_COOKIE_NAME,
        httponly=True,
        samesite="strict",
        secure=settings.enforce_https,
        path="/",
    )


def _set_account_confirm_cookie(
    response: RedirectResponse | JSONResponse,
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
        "password-change": {
            "page": "security",
            "title": "Confirm password change",
            "description": "Authenticate once more before replacing your account password and revoking other sessions.",
        },
        "totp-enroll": {
            "page": "security",
            "title": "Confirm authenticator setup",
            "description": "Authenticate once more before creating a new TOTP enrollment.",
        },
        "totp-remove": {
            "page": "security",
            "title": "Confirm authenticator removal",
            "description": "Authenticate once more before removing this authenticator app.",
        },
        "passkey-register": {
            "page": "security",
            "title": "Confirm passkey registration",
            "description": "Authenticate once more before registering a new passkey.",
        },
        "passkey-remove": {
            "page": "security",
            "title": "Confirm passkey removal",
            "description": "Authenticate once more before removing this passkey.",
        },
        "backup-codes-regenerate": {
            "page": "security",
            "title": "Confirm backup code regeneration",
            "description": "Authenticate once more before replacing all stored backup codes.",
        },
        "mfa-disable": {
            "page": "security",
            "title": "Confirm MFA disable",
            "description": "Authenticate once more before deleting all MFA methods and recovery codes from this account.",
        },
        "client-link": {
            "page": "clients",
            "title": "Confirm client ID link",
            "description": "Authenticate once more before linking this ScamScreener mod client ID to your account.",
        },
        "client-unlink": {
            "page": "clients",
            "title": "Confirm client ID unlink",
            "description": "Authenticate once more before removing this client ID from your account.",
        },
        "data-export-request": {
            "page": "privacy",
            "title": "Confirm data export request",
            "description": "Authenticate once more before queueing an email data export.",
        },
        "data-purge": {
            "page": "privacy",
            "title": "Confirm upload data purge",
            "description": "Authenticate once more before deleting your uploads and related case contributions.",
        },
        "account-delete": {
            "page": "privacy",
            "title": "Confirm account deletion",
            "description": "Authenticate once more before permanently deleting this account.",
        },
    }
    return dict(metadata.get(str(action), {}))


def _account_action_label(payload: dict[str, str]) -> str:
    base_action = str(payload.get("action", "")).split(":", 1)[0]
    metadata = _account_action_metadata(base_action)
    return metadata.get("title", "Confirm account action")


def _account_action_page(payload: dict[str, str]) -> str:
    base_action = str(payload.get("action", "")).split(":", 1)[0]
    metadata = _account_action_metadata(base_action)
    return metadata.get("page", str(payload.get("page", "security") or "security"))


def _account_action_summary(payload: dict[str, str]) -> str:
    base_action = str(payload.get("action", "")).split(":", 1)[0]
    values = dict(payload.get("values", {})) if isinstance(payload.get("values"), dict) else {}
    if base_action == "totp-enroll" and values.get("label"):
        return f"Authenticator label: {values['label']}"
    if base_action == "passkey-register" and values.get("label"):
        return f"Passkey label: {values['label']}"
    if base_action == "client-link" and values.get("client_id"):
        return f"Client ID: {values['client_id']}"
    if base_action == "client-unlink" and values.get("client_identity_id"):
        return f"Linked client entry #{values['client_identity_id']}"
    if base_action == "totp-remove" and values.get("factor_id"):
        return f"Authenticator entry #{values['factor_id']}"
    if base_action == "passkey-remove" and values.get("passkey_id"):
        return f"Passkey entry #{values['passkey_id']}"
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


def _decode_account_action_values(settings: TrainingHubSettings, values: dict[str, str]) -> dict[str, str]:
    decoded = dict(values or {})
    encrypted_password = str(decoded.pop("new_password_encrypted", "") or "")
    if encrypted_password:
        try:
            decoded["new_password"] = _decrypt_value(encrypted_password, settings.secret_key)
        except ValueError:
            decoded["new_password"] = ""
    return decoded


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
    mfa_state = _mfa_state(settings, int(user["id"]))
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
        "confirm_ready_for_passkey_registration": bool(payload.get("passkey_registration_ready")),
        "confirm_flow_expires_at": str(flow.get("expires_at", "")),
        "mfa_state": mfa_state,
    }


async def _json_body(request: Request) -> dict:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


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


def _passkey_register_ready_response(
    settings: TrainingHubSettings,
    *,
    user_id: int,
    payload: dict[str, str],
    source_ip: str,
    user_agent: str,
) -> RedirectResponse:
    ready_flow = _create_auth_flow(
        settings,
        user_id=int(user_id),
        flow_type="account-confirm",
        payload={**payload, "passkey_registration_ready": True},
        ttl_minutes=WEBAUTHN_FLOW_TTL_MINUTES,
        source_ip=source_ip,
        user_agent=user_agent,
    )
    response = RedirectResponse(url="/account/confirm?notice=Authentication+confirmed", status_code=303)
    _set_account_confirm_cookie(response, settings, str(ready_flow["token"]), WEBAUTHN_FLOW_TTL_MINUTES)
    return response


def _account_confirm_completion_response(
    settings: TrainingHubSettings,
    *,
    user_id: int,
    payload: dict[str, str],
    method: str,
    source_ip: str,
    user_agent: str,
) -> RedirectResponse:
    completed_flow = _create_auth_flow(
        settings,
        user_id=int(user_id),
        flow_type="account-confirm",
        payload={**payload, "confirmed_method": str(method or "passkey")},
        ttl_minutes=ACCOUNT_CONFIRM_TTL_MINUTES,
        source_ip=source_ip,
        user_agent=user_agent,
    )
    response = RedirectResponse(url="/account/confirm/complete", status_code=303)
    _set_account_confirm_cookie(response, settings, str(completed_flow["token"]))
    return response


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
    values = _decode_account_action_values(settings, dict(payload.get("values", {})) if isinstance(payload.get("values"), dict) else {})
    source_ip, user_agent = _request_meta(request, settings)

    if action == "password-change":
        change_result = await run_in_threadpool(
            _change_user_password_after_reauth,
            settings.database_path,
            int(user["id"]),
            str(values.get("new_password", "")),
        )
        if not bool(change_result.get("ok")):
            return await run_in_threadpool(
                _render_account,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error=str(change_result.get("error", "Password update failed.")),
                status_code=int(change_result.get("status_code", 400)),
                page=page,
            )
        current_session_id = getattr(request.state, "session_id", None)
        revoked_count = await run_in_threadpool(
            _revoke_other_user_sessions,
            settings.database_path,
            int(user["id"]),
            int(current_session_id) if current_session_id is not None else None,
            "password-change",
        )
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="auth.password.changed",
            target_type="user",
            target_id=int(user["id"]),
            details=f"Password changed after step-up auth using {method}. Revoked {revoked_count} other sessions.",
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
            notice="Password updated successfully.",
            page=page,
        )

    if action == "totp-enroll":
        enrollment = await run_in_threadpool(
            _create_totp_enrollment,
            settings,
            user_id=int(user["id"]),
            label=str(values.get("label", "Authenticator App")),
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
            notice="Authenticator setup created. Verify one code to activate it.",
            page=page,
            pending_totp=enrollment,
        )

    if action == "totp-remove":
        remove_result = await run_in_threadpool(_remove_totp_factor, settings, int(user["id"]), int(values.get("factor_id", "0") or 0))
        if not bool(remove_result.get("ok")):
            return await run_in_threadpool(
                _render_account,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error=str(remove_result.get("error", "Authenticator app could not be removed.")),
                status_code=int(remove_result.get("status_code", 400)),
                page=page,
            )
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="auth.mfa.totp.removed",
            target_type="user",
            target_id=int(user["id"]),
            details=f"Removed authenticator app factor #{int(values.get('factor_id', '0') or 0)} after step-up auth using {method}.",
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
            notice="Authenticator app removed.",
            page=page,
        )

    if action == "passkey-remove":
        remove_result = await run_in_threadpool(_remove_passkey, settings, int(user["id"]), int(values.get("passkey_id", "0") or 0))
        if not bool(remove_result.get("ok")):
            return await run_in_threadpool(
                _render_account,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error=str(remove_result.get("error", "Passkey could not be removed.")),
                status_code=int(remove_result.get("status_code", 400)),
                page=page,
            )
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="auth.mfa.passkey.removed",
            target_type="user",
            target_id=int(user["id"]),
            details=f"Removed passkey #{int(values.get('passkey_id', '0') or 0)} after step-up auth using {method}.",
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
            notice="Passkey removed.",
            page=page,
        )

    if action == "backup-codes-regenerate":
        regenerate_result = await run_in_threadpool(_regenerate_backup_codes, settings, int(user["id"]))
        if not bool(regenerate_result.get("ok")):
            return await run_in_threadpool(
                _render_account,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error=str(regenerate_result.get("error", "Backup codes could not be generated.")),
                status_code=int(regenerate_result.get("status_code", 400)),
                page=page,
            )
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="auth.mfa.backup_codes.regenerated",
            target_type="user",
            target_id=int(user["id"]),
            details=f"Regenerated backup codes after step-up auth using {method}.",
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
            notice="Backup codes regenerated. Save them now; they will not be shown again.",
            page=page,
            backup_codes=list(regenerate_result["codes"]),
        )

    if action == "mfa-disable":
        disable_result = await run_in_threadpool(_disable_user_mfa, settings, int(user["id"]))
        if not bool(disable_result.get("ok")):
            return await run_in_threadpool(
                _render_account,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error=str(disable_result.get("error", "MFA could not be disabled.")),
                status_code=int(disable_result.get("status_code", 400)),
                page=page,
            )
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="auth.mfa.disabled",
            target_type="user",
            target_id=int(user["id"]),
            details=f"Disabled MFA after step-up auth using {method}.",
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
            notice="MFA disabled.",
            page=page,
        )

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
        return await _render_account_confirm(
            app=app,
            request=request,
            settings=settings,
            user=user,
            flow=flow,
            notice=notice,
            error=error,
        )

    @app.post("/account/confirm/password", response_class=HTMLResponse)
    async def account_confirm_password(
        request: Request,
        current_password: str = Form(...),
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)
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
        password_result = await run_in_threadpool(
            _verify_user_action_password,
            settings.database_path,
            int(user["id"]),
            current_password,
        )
        if not bool(password_result.get("ok")):
            return await _render_account_confirm(
                app=app,
                request=request,
                settings=settings,
                user=user,
                flow=flow,
                error=str(password_result.get("error", "Current password is incorrect.")),
                status_code=int(password_result.get("status_code", 400)),
            )
        await run_in_threadpool(_consume_auth_flow_by_id, settings, int(flow["id"]))
        payload = dict(flow["payload"])
        if str(payload.get("action", "")).split(":", 1)[0] == "passkey-register":
            return _passkey_register_ready_response(
                settings,
                user_id=int(user["id"]),
                payload=payload,
                source_ip=source_ip,
                user_agent=user_agent,
            )
        response = await _execute_confirmed_account_action(
            app=app,
            request=request,
            settings=settings,
            user=user,
            payload=payload,
            method="password",
        )
        _clear_account_confirm_cookie(response, settings)
        return response

    @app.post("/account/confirm/code", response_class=HTMLResponse)
    async def account_confirm_code(
        request: Request,
        code: str = Form(...),
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)
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
        verify_result = await run_in_threadpool(_verify_user_step_up_code, settings, user_id=int(user["id"]), code=code)
        if not bool(verify_result.get("ok")):
            return await _render_account_confirm(
                app=app,
                request=request,
                settings=settings,
                user=user,
                flow=flow,
                error=str(verify_result.get("error", "Verification failed.")),
                status_code=int(verify_result.get("status_code", 400)),
            )
        await run_in_threadpool(_consume_auth_flow_by_id, settings, int(flow["id"]))
        payload = dict(flow["payload"])
        if str(payload.get("action", "")).split(":", 1)[0] == "passkey-register":
            return _passkey_register_ready_response(
                settings,
                user_id=int(user["id"]),
                payload=payload,
                source_ip=source_ip,
                user_agent=user_agent,
            )
        response = await _execute_confirmed_account_action(
            app=app,
            request=request,
            settings=settings,
            user=user,
            payload=payload,
            method=str(verify_result.get("method", "code")),
        )
        _clear_account_confirm_cookie(response, settings)
        return response

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

    @app.post("/account/confirm/passkey/options")
    async def account_confirm_passkey_options(request: Request):
        user = request.state.user
        if user is None:
            return JSONResponse({"detail": "Login required."}, status_code=401)
        source_ip, user_agent = _request_meta(request, settings)
        flow = await run_in_threadpool(
            _load_account_confirm_flow,
            request,
            settings,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        if not bool(flow.get("ok")) or int(flow.get("user_id", 0)) != int(user["id"]):
            return JSONResponse({"detail": "Confirmation required."}, status_code=401)
        rp_id, origin = _webauthn_request_context(request, settings)
        options_result = await run_in_threadpool(
            _generate_passkey_auth_options,
            settings,
            user_id=int(user["id"]),
            purpose="account-confirm",
            rp_id=rp_id,
            expected_origin=origin,
            source_ip=source_ip,
            user_agent=user_agent,
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

    @app.post("/account/confirm/passkey/verify")
    async def account_confirm_passkey_verify(request: Request):
        user = request.state.user
        if user is None:
            return JSONResponse({"detail": "Login required."}, status_code=401)
        source_ip, user_agent = _request_meta(request, settings)
        flow = await run_in_threadpool(
            _load_account_confirm_flow,
            request,
            settings,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        if not bool(flow.get("ok")) or int(flow.get("user_id", 0)) != int(user["id"]):
            return JSONResponse({"detail": "Confirmation required."}, status_code=401)
        payload = await _json_body(request)
        flow_token = str(payload.get("flowToken", "")).strip()
        credential = payload.get("credential")
        if not flow_token or not isinstance(credential, dict):
            return JSONResponse({"detail": "flowToken and credential are required."}, status_code=400)
        verify_result = await run_in_threadpool(
            _verify_passkey_authentication,
            settings,
            flow_token=flow_token,
            credential=credential,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        if not bool(verify_result.get("ok")) or int(verify_result.get("user_id", 0)) != int(user["id"]):
            return JSONResponse({"detail": str(verify_result.get("error", "Passkey verification failed."))}, status_code=int(verify_result.get("status_code", 400)))
        await run_in_threadpool(_consume_auth_flow_by_id, settings, int(flow["id"]))
        confirm_payload = dict(flow["payload"])
        if str(confirm_payload.get("action", "")).split(":", 1)[0] == "passkey-register":
            response = _passkey_register_ready_response(
                settings,
                user_id=int(user["id"]),
                payload=confirm_payload,
                source_ip=source_ip,
                user_agent=user_agent,
            )
            return JSONResponse({"ok": True, "redirectUrl": str(response.headers.get("location", "/account/confirm"))}, status_code=200, headers=response.headers)
        response = _account_confirm_completion_response(
            settings,
            user_id=int(user["id"]),
            payload=confirm_payload,
            method="passkey",
            source_ip=source_ip,
            user_agent=user_agent,
        )
        return JSONResponse({"ok": True, "redirectUrl": str(response.headers.get("location", "/account/confirm/complete"))}, status_code=200, headers=response.headers)

    @app.post("/account/confirm/passkey/register/options")
    async def account_confirm_register_passkey_options(request: Request):
        user = request.state.user
        if user is None:
            return JSONResponse({"detail": "Login required."}, status_code=401)
        source_ip, user_agent = _request_meta(request, settings)
        flow = await run_in_threadpool(
            _load_account_confirm_flow,
            request,
            settings,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        if not bool(flow.get("ok")) or int(flow.get("user_id", 0)) != int(user["id"]):
            return JSONResponse({"detail": "Confirmation required."}, status_code=401)
        confirm_payload = dict(flow["payload"])
        if str(confirm_payload.get("action", "")).split(":", 1)[0] != "passkey-register" or not bool(confirm_payload.get("passkey_registration_ready")):
            return JSONResponse({"detail": "Passkey registration is not ready."}, status_code=409)
        label = str(dict(confirm_payload.get("values", {})).get("label", "Passkey"))
        rp_id, origin = _webauthn_request_context(request, settings)
        options_result = await run_in_threadpool(
            _generate_passkey_registration_options,
            settings,
            user_id=int(user["id"]),
            label=label,
            rp_id=rp_id,
            expected_origin=origin,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        return JSONResponse({"flowToken": str(options_result["token"]), "publicKey": json.loads(str(options_result["options_json"]))}, status_code=200)

    @app.post("/account/confirm/passkey/register/verify")
    async def account_confirm_register_passkey_verify(request: Request):
        user = request.state.user
        if user is None:
            return JSONResponse({"detail": "Login required."}, status_code=401)
        source_ip, user_agent = _request_meta(request, settings)
        flow = await run_in_threadpool(
            _load_account_confirm_flow,
            request,
            settings,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        if not bool(flow.get("ok")) or int(flow.get("user_id", 0)) != int(user["id"]):
            return JSONResponse({"detail": "Confirmation required."}, status_code=401)
        confirm_payload = dict(flow["payload"])
        if str(confirm_payload.get("action", "")).split(":", 1)[0] != "passkey-register" or not bool(confirm_payload.get("passkey_registration_ready")):
            return JSONResponse({"detail": "Passkey registration is not ready."}, status_code=409)
        payload = await _json_body(request)
        flow_token = str(payload.get("flowToken", "")).strip()
        credential = payload.get("credential")
        if not flow_token or not isinstance(credential, dict):
            return JSONResponse({"detail": "flowToken and credential are required."}, status_code=400)
        verify_result = await run_in_threadpool(
            _verify_passkey_registration,
            settings,
            user_id=int(user["id"]),
            flow_token=flow_token,
            credential=credential,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        if not bool(verify_result.get("ok")):
            return JSONResponse({"detail": str(verify_result.get("error", "Passkey registration failed."))}, status_code=int(verify_result.get("status_code", 400)))
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="auth.mfa.passkey.enrolled",
            target_type="user",
            target_id=int(user["id"]),
            details=f"Added passkey {verify_result['label']} after step-up auth.",
            source_ip=source_ip,
            user_agent=user_agent,
        )
        await run_in_threadpool(_consume_auth_flow_by_id, settings, int(flow["id"]))
        response = JSONResponse({"ok": True, "redirectUrl": "/account/security?notice=Passkey+registered"}, status_code=200)
        _clear_account_confirm_cookie(response, settings)
        return response

    @app.post("/account/security/password", response_class=HTMLResponse)
    @app.post("/dashboard/password", response_class=HTMLResponse)
    async def change_password(
        request: Request,
        current_password: str = Form(""),
        new_password: str = Form(...),
        new_password_confirm: str = Form(...),
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)

        if (new_password or "") != (new_password_confirm or ""):
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="security",
                active_action="password-change",
                action_error="New password confirmation does not match.",
                status_code=400,
            )
        if not (current_password or "").strip():
            source_ip, user_agent = _request_meta(request, settings)
            return _start_account_confirm_response(
                settings,
                user_id=int(user["id"]),
                page="security",
                action="password-change",
                values={"new_password_encrypted": _encrypt_value(new_password, settings.secret_key)},
                source_ip=source_ip,
                user_agent=user_agent,
            )

        change_result = await run_in_threadpool(
            _change_user_password,
            settings.database_path,
            int(user["id"]),
            current_password,
            new_password,
        )
        if not bool(change_result.get("ok")):
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="security",
                active_action="password-change",
                action_error=str(change_result.get("error", "Password update failed.")),
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
            _render_account,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=refreshed_user,
            notice="Password updated successfully.",
            page="security",
        )

    @app.post("/account/security/totp/enroll", response_class=HTMLResponse)
    async def start_totp_enrollment(
        request: Request,
        current_password: str = Form(""),
        label: str = Form("Authenticator App"),
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)
        if not (current_password or "").strip():
            source_ip, user_agent = _request_meta(request, settings)
            return _start_account_confirm_response(
                settings,
                user_id=int(user["id"]),
                page="security",
                action="totp-enroll",
                values=_action_values(label=label or "Authenticator App"),
                source_ip=source_ip,
                user_agent=user_agent,
            )
        password_result = await run_in_threadpool(
            _verify_user_action_password,
            settings.database_path,
            int(user["id"]),
            current_password,
        )
        if not bool(password_result.get("ok")):
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="security",
                active_action="totp-enroll",
                action_error=str(password_result.get("error", "Current password is incorrect.")),
                status_code=int(password_result.get("status_code", 400)),
                action_values=_action_values(label=label),
            )
        source_ip, user_agent = _request_meta(request, settings)
        enrollment = await run_in_threadpool(
            _create_totp_enrollment,
            settings,
            user_id=int(user["id"]),
            label=label,
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
            notice="Authenticator setup created. Verify one code to activate it.",
            page="security",
            pending_totp=enrollment,
        )

    @app.post("/account/security/totp/verify", response_class=HTMLResponse)
    async def verify_totp_enrollment(
        request: Request,
        enrollment_token: str = Form(...),
        code: str = Form(...),
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)
        source_ip, user_agent = _request_meta(request, settings)
        verify_result = await run_in_threadpool(
            _verify_totp_enrollment,
            settings,
            user_id=int(user["id"]),
            enrollment_token=enrollment_token,
            code=code,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        if not bool(verify_result.get("ok")):
            pending_totp = await run_in_threadpool(
                _pending_totp_enrollment,
                settings,
                user_id=int(user["id"]),
                enrollment_token=enrollment_token,
                source_ip=source_ip,
                user_agent=user_agent,
            )
            return await run_in_threadpool(
                _render_account,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error=str(verify_result.get("error", "Authenticator setup could not be verified.")),
                status_code=int(verify_result.get("status_code", 400)),
                page="security",
                pending_totp=pending_totp if bool(pending_totp.get("ok")) else None,
            )

        source_ip, user_agent = _request_meta(request, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="auth.mfa.totp.enrolled",
            target_type="user",
            target_id=int(user["id"]),
            details=f"Added authenticator app factor {verify_result['label']}.",
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
            notice="Authenticator app verified and activated.",
            page="security",
        )

    @app.post("/account/security/totp/{factor_id}/remove", response_class=HTMLResponse)
    async def remove_totp_factor(
        request: Request,
        factor_id: int,
        current_password: str = Form(""),
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)
        if not (current_password or "").strip():
            source_ip, user_agent = _request_meta(request, settings)
            return _start_account_confirm_response(
                settings,
                user_id=int(user["id"]),
                page="security",
                action="totp-remove",
                values={"factor_id": str(int(factor_id))},
                source_ip=source_ip,
                user_agent=user_agent,
            )
        password_result = await run_in_threadpool(
            _verify_user_action_password,
            settings.database_path,
            int(user["id"]),
            current_password,
        )
        if not bool(password_result.get("ok")):
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="security",
                active_action=f"totp-remove-{int(factor_id)}",
                action_error=str(password_result.get("error", "Current password is incorrect.")),
                status_code=int(password_result.get("status_code", 400)),
            )
        remove_result = await run_in_threadpool(_remove_totp_factor, settings, int(user["id"]), int(factor_id))
        if not bool(remove_result.get("ok")):
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="security",
                active_action=f"totp-remove-{int(factor_id)}",
                action_error=str(remove_result.get("error", "Authenticator app could not be removed.")),
                status_code=int(remove_result.get("status_code", 400)),
            )
        source_ip, user_agent = _request_meta(request, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="auth.mfa.totp.removed",
            target_type="user",
            target_id=int(user["id"]),
            details=f"Removed authenticator app factor #{int(factor_id)}.",
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
            notice="Authenticator app removed.",
            page="security",
        )

    @app.post("/account/security/passkeys/register", response_class=HTMLResponse)
    async def passkey_register_start(
        request: Request,
        label: str = Form("Passkey"),
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
            page="security",
            action="passkey-register",
            values=_action_values(label=label or "Passkey"),
            source_ip=source_ip,
            user_agent=user_agent,
        )

    @app.post("/account/security/passkeys/register/options")
    async def passkey_registration_options(request: Request):
        user = request.state.user
        if user is None:
            return JSONResponse({"detail": "Login required."}, status_code=401)
        payload = await _json_body(request)
        label = str(payload.get("label", "Passkey")).strip()
        current_password = str(payload.get("currentPassword", ""))
        password_result = await run_in_threadpool(
            _verify_user_action_password,
            settings.database_path,
            int(user["id"]),
            current_password,
        )
        if not bool(password_result.get("ok")):
            return JSONResponse({"detail": str(password_result.get("error", "Current password is incorrect."))}, status_code=int(password_result.get("status_code", 400)))
        source_ip, user_agent = _request_meta(request, settings)
        rp_id, origin = _webauthn_request_context(request, settings)
        options_result = await run_in_threadpool(
            _generate_passkey_registration_options,
            settings,
            user_id=int(user["id"]),
            label=label,
            rp_id=rp_id,
            expected_origin=origin,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        return JSONResponse(
            {
                "flowToken": str(options_result["token"]),
                "publicKey": json.loads(str(options_result["options_json"])),
            },
            status_code=200,
        )

    @app.post("/account/security/passkeys/register/verify")
    async def passkey_registration_verify(request: Request):
        user = request.state.user
        if user is None:
            return JSONResponse({"detail": "Login required."}, status_code=401)
        payload = await _json_body(request)
        flow_token = str(payload.get("flowToken", "")).strip()
        credential = payload.get("credential")
        if not flow_token or not isinstance(credential, dict):
            return JSONResponse({"detail": "flowToken and credential are required."}, status_code=400)
        source_ip, user_agent = _request_meta(request, settings)
        verify_result = await run_in_threadpool(
            _verify_passkey_registration,
            settings,
            user_id=int(user["id"]),
            flow_token=flow_token,
            credential=credential,
            source_ip=source_ip,
            user_agent=user_agent,
        )
        if not bool(verify_result.get("ok")):
            return JSONResponse({"detail": str(verify_result.get("error", "Passkey registration failed."))}, status_code=int(verify_result.get("status_code", 400)))
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="auth.mfa.passkey.enrolled",
            target_type="user",
            target_id=int(user["id"]),
            details=f"Added passkey {verify_result['label']}.",
            source_ip=source_ip,
            user_agent=user_agent,
        )
        return JSONResponse({"ok": True, "notice": "Passkey registered."}, status_code=200)

    @app.post("/account/security/passkeys/{passkey_id}/remove", response_class=HTMLResponse)
    async def remove_passkey(
        request: Request,
        passkey_id: int,
        current_password: str = Form(""),
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)
        if not (current_password or "").strip():
            source_ip, user_agent = _request_meta(request, settings)
            return _start_account_confirm_response(
                settings,
                user_id=int(user["id"]),
                page="security",
                action="passkey-remove",
                values={"passkey_id": str(int(passkey_id))},
                source_ip=source_ip,
                user_agent=user_agent,
            )
        password_result = await run_in_threadpool(
            _verify_user_action_password,
            settings.database_path,
            int(user["id"]),
            current_password,
        )
        if not bool(password_result.get("ok")):
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="security",
                active_action=f"passkey-remove-{int(passkey_id)}",
                action_error=str(password_result.get("error", "Current password is incorrect.")),
                status_code=int(password_result.get("status_code", 400)),
            )
        remove_result = await run_in_threadpool(_remove_passkey, settings, int(user["id"]), int(passkey_id))
        if not bool(remove_result.get("ok")):
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="security",
                active_action=f"passkey-remove-{int(passkey_id)}",
                action_error=str(remove_result.get("error", "Passkey could not be removed.")),
                status_code=int(remove_result.get("status_code", 400)),
            )
        source_ip, user_agent = _request_meta(request, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="auth.mfa.passkey.removed",
            target_type="user",
            target_id=int(user["id"]),
            details=f"Removed passkey #{int(passkey_id)}.",
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
            notice="Passkey removed.",
            page="security",
        )

    @app.post("/account/security/backup-codes/regenerate", response_class=HTMLResponse)
    async def regenerate_backup_codes(
        request: Request,
        current_password: str = Form(""),
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)
        if not (current_password or "").strip():
            source_ip, user_agent = _request_meta(request, settings)
            return _start_account_confirm_response(
                settings,
                user_id=int(user["id"]),
                page="security",
                action="backup-codes-regenerate",
                values={},
                source_ip=source_ip,
                user_agent=user_agent,
            )
        password_result = await run_in_threadpool(
            _verify_user_action_password,
            settings.database_path,
            int(user["id"]),
            current_password,
        )
        if not bool(password_result.get("ok")):
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="security",
                active_action="backup-codes-regenerate",
                action_error=str(password_result.get("error", "Current password is incorrect.")),
                status_code=int(password_result.get("status_code", 400)),
            )
        regenerate_result = await run_in_threadpool(_regenerate_backup_codes, settings, int(user["id"]))
        if not bool(regenerate_result.get("ok")):
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="security",
                active_action="backup-codes-regenerate",
                action_error=str(regenerate_result.get("error", "Backup codes could not be generated.")),
                status_code=int(regenerate_result.get("status_code", 400)),
            )
        source_ip, user_agent = _request_meta(request, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="auth.mfa.backup_codes.regenerated",
            target_type="user",
            target_id=int(user["id"]),
            details="Regenerated backup codes.",
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
            notice="Backup codes regenerated. Save them now; they will not be shown again.",
            page="security",
            backup_codes=list(regenerate_result["codes"]),
        )

    @app.post("/account/security/mfa/disable", response_class=HTMLResponse)
    async def disable_mfa(
        request: Request,
        current_password: str = Form(""),
        confirmation: str = Form(...),
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)
        if (confirmation or "").strip() != "DISABLE MFA":
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="security",
                active_action="mfa-disable",
                action_error="Type DISABLE MFA exactly to confirm.",
                status_code=400,
                action_values=_action_values(confirmation=confirmation),
            )
        if not (current_password or "").strip():
            source_ip, user_agent = _request_meta(request, settings)
            return _start_account_confirm_response(
                settings,
                user_id=int(user["id"]),
                page="security",
                action="mfa-disable",
                values={},
                source_ip=source_ip,
                user_agent=user_agent,
            )
        password_result = await run_in_threadpool(
            _verify_user_action_password,
            settings.database_path,
            int(user["id"]),
            current_password,
        )
        if not bool(password_result.get("ok")):
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="security",
                active_action="mfa-disable",
                action_error=str(password_result.get("error", "Current password is incorrect.")),
                status_code=int(password_result.get("status_code", 400)),
                action_values=_action_values(confirmation=confirmation),
            )
        disable_result = await run_in_threadpool(_disable_user_mfa, settings, int(user["id"]))
        if not bool(disable_result.get("ok")):
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="security",
                active_action="mfa-disable",
                action_error=str(disable_result.get("error", "MFA could not be disabled.")),
                status_code=int(disable_result.get("status_code", 400)),
                action_values=_action_values(confirmation=confirmation),
            )
        source_ip, user_agent = _request_meta(request, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="auth.mfa.disabled",
            target_type="user",
            target_id=int(user["id"]),
            details="Disabled MFA and removed all registered MFA methods.",
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
            notice="MFA disabled.",
            page="security",
        )

    @app.post("/account/sessions/revoke-others", response_class=HTMLResponse)
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
            _render_account,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=refreshed_user,
            notice=f"Revoked {revoked_count} other sessions.",
            page="sessions",
        )

    @app.post("/account/sessions/{session_id}/revoke", response_class=HTMLResponse)
    @app.post("/dashboard/sessions/{session_id}/revoke", response_class=HTMLResponse)
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
    @app.post("/dashboard/account/client-ids/link", response_class=HTMLResponse)
    async def link_client_id(
        request: Request,
        client_id: str = Form(...),
        current_password: str = Form(""),
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)
        if not (current_password or "").strip():
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
        password_result = await run_in_threadpool(
            _verify_user_action_password,
            settings.database_path,
            int(user["id"]),
            current_password,
        )
        if not bool(password_result.get("ok")):
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="clients",
                active_action="client-link",
                action_error=str(password_result.get("error", "Current password is incorrect.")),
                status_code=int(password_result.get("status_code", 400)),
                action_values=_action_values(client_id=client_id),
            )
        link_result = await run_in_threadpool(
            _link_client_identity_to_user,
            settings.database_path,
            int(user["id"]),
            client_id,
        )
        if not bool(link_result.get("ok")):
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="clients",
                active_action="client-link",
                action_error=str(link_result.get("error", "Client ID could not be linked.")),
                status_code=int(link_result.get("status_code", 400)),
                action_values=_action_values(client_id=client_id),
            )
        source_ip, user_agent = _request_meta(request, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="account.client_identity.linked",
            target_type="client_identity",
            target_id=int(link_result["client_identity_id"]),
            details=(
                f"Linked client ID {link_result['normalized_client_id']} to account. "
                f"Historical uploads: {int(link_result['upload_count'])}, cases: {int(link_result['case_count'])}."
            ),
            source_ip=source_ip,
            user_agent=user_agent,
        )
        refreshed_user = await run_in_threadpool(_refresh_user, settings.database_path, int(user["id"])) or user
        notice = f"Client ID {link_result['normalized_client_id']} linked."
        if str(link_result.get("status", "")) == "already-linked":
            notice = f"Client ID {link_result['normalized_client_id']} is already linked to your account."
        return await run_in_threadpool(
            _render_account,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=refreshed_user,
            notice=notice,
            page="clients",
        )

    @app.post("/account/clients/{client_identity_id}/unlink", response_class=HTMLResponse)
    @app.post("/dashboard/account/client-ids/{client_identity_id}/unlink", response_class=HTMLResponse)
    async def unlink_client_id(
        request: Request,
        client_identity_id: int,
        current_password: str = Form(""),
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)
        if not (current_password or "").strip():
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
        password_result = await run_in_threadpool(
            _verify_user_action_password,
            settings.database_path,
            int(user["id"]),
            current_password,
        )
        if not bool(password_result.get("ok")):
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="clients",
                active_action=f"client-unlink-{int(client_identity_id)}",
                action_error=str(password_result.get("error", "Current password is incorrect.")),
                status_code=int(password_result.get("status_code", 400)),
            )
        unlink_result = await run_in_threadpool(
            _unlink_client_identity_from_user,
            settings.database_path,
            int(user["id"]),
            int(client_identity_id),
        )
        if not bool(unlink_result.get("ok")):
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="clients",
                active_action=f"client-unlink-{int(client_identity_id)}",
                action_error=str(unlink_result.get("error", "Client ID could not be unlinked.")),
                status_code=int(unlink_result.get("status_code", 400)),
            )
        source_ip, user_agent = _request_meta(request, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="account.client_identity.unlinked",
            target_type="client_identity",
            target_id=int(unlink_result["client_identity_id"]),
            details=(
                f"Unlinked client ID {unlink_result['normalized_client_id']} from account. "
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
            page="clients",
        )

    @app.post("/account/privacy/data-export/request", response_class=HTMLResponse)
    @app.post("/dashboard/data-export/request", response_class=HTMLResponse)
    async def request_account_data_export(
        request: Request,
        current_password: str = Form(""),
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)
        if not (current_password or "").strip():
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
        password_result = await run_in_threadpool(
            _verify_user_action_password,
            settings.database_path,
            int(user["id"]),
            current_password,
        )
        if not bool(password_result.get("ok")):
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="privacy",
                active_action="data-export-request",
                action_error=str(password_result.get("error", "Current password is incorrect.")),
                status_code=int(password_result.get("status_code", 400)),
            )

        source_ip, user_agent = _request_meta(request, settings)
        queue_result = await run_in_threadpool(
            _queue_user_data_export_request,
            settings,
            int(user["id"]),
            source_ip,
            user_agent,
        )
        if not bool(queue_result.get("ok")):
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="privacy",
                active_action="data-export-request",
                action_error=str(queue_result.get("error", "Could not queue account data export.")),
                status_code=int(queue_result.get("status_code", 400)),
            )
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="account.data_export.requested",
            target_type="data_export_request",
            target_id=int(queue_result["request_id"]),
            details=f"Queued account data export request #{int(queue_result['request_id'])}.",
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
            page="privacy",
        )

    @app.post("/account/privacy/data/purge", response_class=HTMLResponse)
    @app.post("/dashboard/data/purge", response_class=HTMLResponse)
    async def purge_own_uploads_and_cases(
        request: Request,
        current_password: str = Form(""),
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
        if not (current_password or "").strip():
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
        password_result = await run_in_threadpool(
            _verify_user_action_password,
            settings.database_path,
            int(user["id"]),
            current_password,
        )
        if not bool(password_result.get("ok")):
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="privacy",
                active_action="data-purge",
                action_error=str(password_result.get("error", "Current password is incorrect.")),
                status_code=int(password_result.get("status_code", 400)),
                action_values=_action_values(confirmation=confirmation),
            )
        purge_result = await run_in_threadpool(_purge_user_uploads, settings, int(user["id"]))
        source_ip, user_agent = _request_meta(request, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="account.data.purged",
            target_type="user",
            target_id=int(user["id"]),
            details=(
                f"Purged own uploads and cases. Uploads deleted: {int(purge_result['deleted_uploads'])}, "
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
            page="privacy",
        )

    @app.post("/account/privacy/account/delete", response_class=HTMLResponse)
    @app.post("/dashboard/account/delete", response_class=HTMLResponse)
    async def delete_own_account(
        request: Request,
        current_password: str = Form(""),
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
        if not (current_password or "").strip():
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
        password_result = await run_in_threadpool(
            _verify_user_action_password,
            settings.database_path,
            int(user["id"]),
            current_password,
        )
        if not bool(password_result.get("ok")):
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="privacy",
                active_action="account-delete",
                action_error=str(password_result.get("error", "Current password is incorrect.")),
                status_code=int(password_result.get("status_code", 400)),
                action_values=_action_values(confirmation=confirmation),
            )
        delete_result = await run_in_threadpool(_delete_user_account, settings, int(user["id"]))
        if not bool(delete_result.get("ok")):
            return await _render_account_action(
                app=app,
                request=request,
                settings=settings,
                user=user,
                page="privacy",
                active_action="account-delete",
                action_error=str(delete_result.get("error", "Account deletion failed.")),
                status_code=int(delete_result.get("status_code", 400)),
                action_values=_action_values(confirmation=confirmation),
            )
        response = RedirectResponse(url="/login?notice=Account+deleted", status_code=303)
        response.delete_cookie(
            SESSION_COOKIE_NAME,
            httponly=True,
            samesite="strict",
            secure=settings.enforce_https,
            path="/",
        )
        return response
