from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from ..core.hub_core import (
    ACCESS_POLICY_DISABLE_LOGIN_NON_ADMIN,
    ACCESS_POLICY_DISABLE_SIGNUP,
    _create_audit_log,
    _render_admin,
    _revoke_all_user_sessions,
    _set_access_policy,
    _validate_csrf_token,
)
from ..core.account_ops import _delete_user_account
from ..core.common import _now_utc_iso
from ..infra import db as sqlite3
from ..config.settings import TrainingHubSettings
from ..services.mailer import send_account_deletion_email
from .admin_utils import request_meta as _request_meta

logger = logging.getLogger(__name__)


def register_admin_user_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    def _policy_label(policy_key: str) -> str:
        if policy_key == ACCESS_POLICY_DISABLE_LOGIN_NON_ADMIN:
            return "Disable Login"
        if policy_key == ACCESS_POLICY_DISABLE_SIGNUP:
            return "Disable Signup"
        raise ValueError(f"Unsupported policy key: {policy_key}")

    @app.post("/admin/users/{target_user_id}/admin", response_class=HTMLResponse)
    async def admin_manage_user(
        request: Request,
        target_user_id: int,
        action: str = Form(...),
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        if int(user["is_admin"]) != 1:
            raise HTTPException(status_code=403, detail="Admin access required.")
        _validate_csrf_token(request, csrf_token)

        normalized_action = (action or "").strip().lower()
        if normalized_action not in {"grant", "revoke"}:
            return await run_in_threadpool(
                _render_admin,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error="Invalid user-management action.",
                status_code=400,
                page="users",
            )

        actor_user_id = int(user["id"])

        def _manage_user_role() -> dict[str, Any]:
            with sqlite3.connect(settings.database_path) as connection:
                connection.row_factory = sqlite3.Row
                target = connection.execute(
                    "SELECT id, username, is_admin FROM users WHERE id = ?",
                    (target_user_id,),
                ).fetchone()
                if target is None:
                    return {
                        "error": "Target user not found.",
                        "status_code": 404,
                    }

                target_id = int(target["id"])
                target_name = str(target["username"])
                target_is_admin = int(target["is_admin"]) == 1

                if target_id == actor_user_id:
                    return {
                        "error": "Manage your own admin role is disabled to prevent lockout.",
                        "status_code": 400,
                    }

                if normalized_action == "grant":
                    if target_is_admin:
                        return {"notice": f"User {target_name} is already admin."}

                    connection.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (target_id,))
                    connection.commit()
                    return {
                        "notice": f"Granted admin to {target_name}.",
                        "audit_action": "user.admin.grant",
                        "target_id": target_id,
                        "audit_details": f"Granted admin to {target_name}.",
                        "revoke_sessions_user_id": target_id,
                    }

                if not target_is_admin:
                    return {"notice": f"User {target_name} is already non-admin."}

                admin_count = int(connection.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0])
                if admin_count <= 1:
                    return {
                        "error": "Cannot revoke the last remaining admin.",
                        "status_code": 400,
                    }

                connection.execute("UPDATE users SET is_admin = 0 WHERE id = ?", (target_id,))
                connection.commit()
                return {
                    "notice": f"Revoked admin from {target_name}.",
                    "audit_action": "user.admin.revoke",
                    "target_id": target_id,
                    "audit_details": f"Revoked admin from {target_name}.",
                    "revoke_sessions_user_id": target_id,
                }

        result = await run_in_threadpool(_manage_user_role)
        source_ip, user_agent = _request_meta(request, settings)
        if "revoke_sessions_user_id" in result:
            await run_in_threadpool(
                _revoke_all_user_sessions,
                settings.database_path,
                int(result["revoke_sessions_user_id"]),
                "role-change",
            )
        if "audit_action" in result:
            await run_in_threadpool(
                _create_audit_log,
                settings.database_path,
                actor_user_id=actor_user_id,
                action=str(result["audit_action"]),
                target_type="user",
                target_id=int(result["target_id"]),
                details=str(result["audit_details"]),
                source_ip=source_ip,
                user_agent=user_agent,
            )

        if "error" in result:
            return await run_in_threadpool(
                _render_admin,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error=str(result["error"]),
                status_code=int(result.get("status_code", 400)),
                page="users",
            )

        return await run_in_threadpool(
            _render_admin,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=user,
            notice=str(result.get("notice", "")),
            status_code=int(result.get("status_code", 200)),
            page="users",
        )

    @app.post("/admin/users/access-policy", response_class=HTMLResponse)
    async def admin_update_access_policy(
        request: Request,
        policy_key: str = Form(...),
        enabled: str = Form(...),
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        if int(user["is_admin"]) != 1:
            raise HTTPException(status_code=403, detail="Admin access required.")
        _validate_csrf_token(request, csrf_token)

        normalized_enabled = str(enabled or "").strip().lower() in {"1", "true", "yes", "on"}
        try:
            result = await run_in_threadpool(
                _set_access_policy,
                settings.database_path,
                policy_key=policy_key,
                enabled=normalized_enabled,
                actor_user_id=int(user["id"]),
            )
        except ValueError:
            return await run_in_threadpool(
                _render_admin,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error="Invalid access-control policy.",
                status_code=400,
                page="users",
            )

        source_ip, user_agent = _request_meta(request, settings)
        policy_name = _policy_label(str(result["policy_key"]))
        enabled_now = bool(result["enabled"])
        audit_action = (
            "access.login.disable"
            if str(result["policy_key"]) == ACCESS_POLICY_DISABLE_LOGIN_NON_ADMIN and enabled_now
            else "access.login.enable"
            if str(result["policy_key"]) == ACCESS_POLICY_DISABLE_LOGIN_NON_ADMIN
            else "access.signup.disable"
            if enabled_now
            else "access.signup.enable"
        )
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action=audit_action,
            target_type="system",
            target_id=None,
            details=f"{policy_name} set to {'enabled' if enabled_now else 'disabled'}.",
            source_ip=source_ip,
            user_agent=user_agent,
        )
        return await run_in_threadpool(
            _render_admin,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=user,
            notice=f"{policy_name} {'enabled' if enabled_now else 'disabled'}.",
            page="users",
        )

    @app.post("/admin/users/{target_user_id}/delete", response_class=HTMLResponse)
    async def admin_delete_user(
        request: Request,
        target_user_id: int,
        confirm_delete: str = Form(default=""),
        notify_user_email: str = Form(default=""),
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        if int(user["is_admin"]) != 1:
            raise HTTPException(status_code=403, detail="Admin access required.")
        _validate_csrf_token(request, csrf_token)

        if str(confirm_delete or "").strip().lower() not in {"yes", "on", "true", "1"}:
            return await run_in_threadpool(
                _render_admin,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error="Confirm the deletion before removing the user account.",
                status_code=400,
                page="users",
            )

        notify_target_user = str(notify_user_email or "").strip().lower() in {"yes", "on", "true", "1"}
        actor_user_id = int(user["id"])

        def _delete_target_user() -> dict[str, Any]:
            with sqlite3.connect(settings.database_path) as connection:
                connection.row_factory = sqlite3.Row
                target = connection.execute(
                    "SELECT id, username, email FROM users WHERE id = ?",
                    (target_user_id,),
                ).fetchone()
                if target is None:
                    return {
                        "error": "Target user not found.",
                        "status_code": 404,
                    }

                target_id = int(target["id"])
                target_name = str(target["username"])

                if target_id == actor_user_id:
                    return {
                        "error": "Deleting your own account from the admin user list is disabled.",
                        "status_code": 400,
                    }

                if notify_target_user and not settings.outbound_email_enabled:
                    return {
                        "error": "Account deletion email delivery is unavailable until SMTP is configured.",
                        "status_code": 409,
                    }

                delete_result = _delete_user_account(settings, target_id)
                if not bool(delete_result.get("ok")):
                    return {
                        "error": str(delete_result.get("error", "User deletion failed.")),
                        "status_code": int(delete_result.get("status_code", 400)),
                    }

                return {
                    "notice": f"Deleted user {target_name}.",
                    "audit_action": "user.delete",
                    "target_id": target_id,
                    "audit_details": (
                        f"Deleted user {target_name}. "
                        f"Uploads deleted: {int(delete_result.get('deleted_uploads', 0))}, "
                        f"cases deleted: {int(delete_result.get('deleted_cases', 0))}, "
                        f"cases rebuilt: {int(delete_result.get('rebuilt_cases', 0))}."
                    ),
                    "deleted_user_name": str(delete_result.get("username", target_name)),
                    "deleted_user_email": str(delete_result.get("email", target["email"])),
                    "notify_target_user": notify_target_user,
                }

        result = await run_in_threadpool(_delete_target_user)
        source_ip, user_agent = _request_meta(request, settings)
        if "audit_action" in result:
            await run_in_threadpool(
                _create_audit_log,
                settings.database_path,
                actor_user_id=actor_user_id,
                action=str(result["audit_action"]),
                target_type="user",
                target_id=int(result["target_id"]),
                details=str(result["audit_details"]),
                source_ip=source_ip,
                user_agent=user_agent,
            )

        notice = str(result.get("notice", ""))
        error = str(result.get("error", ""))
        status_code = int(result.get("status_code", 200 if notice else 400))

        if bool(result.get("notify_target_user")):
            deleted_email = str(result.get("deleted_user_email", "") or "")
            deleted_name = str(result.get("deleted_user_name", "") or "")
            deleted_at = _now_utc_iso()
            try:
                await run_in_threadpool(
                    send_account_deletion_email,
                    settings,
                    recipient_email=deleted_email,
                    username=deleted_name,
                    deleted_at=deleted_at,
                )
                await run_in_threadpool(
                    _create_audit_log,
                    settings.database_path,
                    actor_user_id=actor_user_id,
                    action="user.delete.email.sent",
                    target_type="user",
                    target_id=int(result["target_id"]),
                    details=f"Sent account deletion notification email for deleted user {deleted_name}.",
                    source_ip=source_ip,
                    user_agent=user_agent,
                )
                notice = f"{notice} Notification email sent to {deleted_email}."
            except Exception as exc:
                logger.exception(
                    "Account deletion notification email failed for deleted user_id=%s recipient=%s.",
                    int(result["target_id"]),
                    deleted_email,
                )
                await run_in_threadpool(
                    _create_audit_log,
                    settings.database_path,
                    actor_user_id=actor_user_id,
                    action="user.delete.email.failed",
                    target_type="user",
                    target_id=int(result["target_id"]),
                    details=(
                        f"Failed to send account deletion notification email for deleted user {deleted_name}. "
                        f"Error type: {type(exc).__name__}."
                    ),
                    source_ip=source_ip,
                    user_agent=user_agent,
                )
                error = (
                    "The account was deleted, but the notification email could not be sent."
                )

        return await run_in_threadpool(
            _render_admin,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=user,
            notice=notice,
            error=error,
            status_code=status_code,
            page="users",
        )


