from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from ..infra import db as sqlite3
from ..core.hub_core import _create_audit_log, _render_admin, _revoke_all_user_sessions, _validate_csrf_token
from ..config.settings import TrainingHubSettings
from .admin_utils import request_meta as _request_meta


def register_admin_user_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
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
            )

        return await run_in_threadpool(
            _render_admin,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=user,
            notice=str(result.get("notice", "")),
            status_code=int(result.get("status_code", 200)),
        )


