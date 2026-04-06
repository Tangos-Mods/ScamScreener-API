from __future__ import annotations

from datetime import datetime, timedelta, timezone
from json import JSONDecodeError
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from ..config.settings import TrainingHubSettings
from ..core.common import _authorization_bearer_token
from ..core.hub_core import (
    _consume_login_attempt,
    _create_audit_log,
    _create_session,
    _maybe_raise_security_alert,
    _refresh_user,
    _revoke_session_by_token,
)
from ..core.upload_workflow import _accept_training_upload
from .public_utils import logger, read_request_bytes as _read_request_bytes, request_meta as _request_meta


def _bearer_token_or_401(request: Request) -> str:
    token = _authorization_bearer_token(str(request.headers.get("authorization", "")))
    if token:
        return token
    raise HTTPException(
        status_code=401,
        detail="Bearer session token required.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _current_api_user_or_401(request: Request) -> dict[str, Any]:
    user = getattr(request.state, "user", None)
    if isinstance(user, dict):
        return user
    raise HTTPException(
        status_code=401,
        detail="Invalid or expired API session.",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _json_body(request: Request) -> dict[str, Any]:
    content_type = str(request.headers.get("content-type", "")).split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(status_code=415, detail="Content-Type must be application/json.")
    try:
        payload = await request.json()
    except (JSONDecodeError, UnicodeDecodeError) as exception:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body. {exception}") from exception
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object.")
    return payload


def register_public_api_client_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    @app.post("/api/v1/client/auth/login")
    async def api_client_login(request: Request):
        if request.state.user:
            current_user = request.state.user
            return JSONResponse(
                {
                    "status": "already-authenticated",
                    "user": {
                        "id": int(current_user["id"]),
                        "username": str(current_user["username"]),
                        "isAdmin": int(current_user["is_admin"]) == 1,
                    },
                },
                status_code=200,
            )

        payload = await _json_body(request)
        username_or_email = str(payload.get("username_or_email", payload.get("usernameOrEmail", ""))).strip()
        password = str(payload.get("password", ""))
        if not username_or_email or len(username_or_email) > 320:
            raise HTTPException(status_code=400, detail="usernameOrEmail is required and must be <= 320 characters.")
        if not password or len(password) > 1024:
            raise HTTPException(status_code=400, detail="password is required and must be <= 1024 characters.")

        login_result = await run_in_threadpool(
            _consume_login_attempt,
            settings.database_path,
            username_or_email,
            password,
        )
        status = str(login_result.get("status", "invalid"))
        source_ip, user_agent = _request_meta(request, settings)
        actor_user_id = int(login_result["user_id"]) if "user_id" in login_result else 0
        if status == "locked":
            if actor_user_id:
                await run_in_threadpool(
                    _create_audit_log,
                    settings.database_path,
                    actor_user_id=actor_user_id,
                    action="auth.login.locked",
                    target_type="user",
                    target_id=actor_user_id,
                    details=f"API login blocked due to lockout. Retry after {int(login_result.get('retry_after', 60))}s.",
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
                        "Security alert: API login lockout spike for ip=%s (count=%s).",
                        source_ip,
                        alert_result.get("count"),
                    )
            return JSONResponse(
                {"status": "locked", "retryAfter": int(login_result.get("retry_after", 60))},
                status_code=429,
            )

        if status != "ok":
            if actor_user_id:
                await run_in_threadpool(
                    _create_audit_log,
                    settings.database_path,
                    actor_user_id=actor_user_id,
                    action="auth.login.failed",
                    target_type="user",
                    target_id=actor_user_id,
                    details="Invalid API password attempt.",
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
                        "Security alert: API login failure spike for ip=%s (count=%s).",
                        source_ip,
                        alert_result.get("count"),
                    )
            raise HTTPException(status_code=401, detail="Invalid credentials.")

        user_row = await run_in_threadpool(_refresh_user, settings.database_path, actor_user_id)
        if user_row is None:
            raise HTTPException(status_code=404, detail="Account not found.")
        if settings.admin_mfa_required and int(user_row["is_admin"]) == 1:
            await run_in_threadpool(
                _create_audit_log,
                settings.database_path,
                actor_user_id=actor_user_id,
                action="auth.api.login.blocked",
                target_type="user",
                target_id=actor_user_id,
                details="API login blocked for admin account because web MFA is required.",
                source_ip=source_ip,
                user_agent=user_agent,
            )
            raise HTTPException(
                status_code=403,
                detail="Admin accounts must use the web login flow with MFA. Use a non-admin account for client uploads.",
            )

        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=settings.session_ttl_minutes)).isoformat().replace(
            "+00:00",
            "Z",
        )
        session_token = await run_in_threadpool(
            _create_session,
            settings.database_path,
            actor_user_id,
            settings.session_ttl_minutes,
            source_ip,
            user_agent,
            settings.secret_key,
        )
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=actor_user_id,
            action="auth.api.login.success",
            target_type="user",
            target_id=actor_user_id,
            details="API login successful.",
            source_ip=source_ip,
            user_agent=user_agent,
        )
        return JSONResponse(
            {
                "status": "ok",
                "sessionToken": session_token,
                "expiresAt": expires_at,
                "user": {
                    "id": int(user_row["id"]),
                    "username": str(user_row["username"]),
                    "isAdmin": int(user_row["is_admin"]) == 1,
                },
            },
            status_code=200,
        )

    @app.post("/api/v1/client/uploads")
    async def api_client_upload(request: Request):
        _bearer_token_or_401(request)
        user = _current_api_user_or_401(request)
        payload = await _read_request_bytes(request, settings.max_upload_bytes)
        source_ip, user_agent = _request_meta(request, settings)
        original_name = str(request.headers.get("x-scamscreener-filename", "")).strip() or "training-cases-v2.jsonl"

        upload_result = await run_in_threadpool(
            _accept_training_upload,
            settings,
            user_id=int(user["id"]),
            payload=payload,
            original_name=original_name,
            source_ip=source_ip,
            user_agent=user_agent,
            audit_details_suffix=" via API client",
        )
        if str(upload_result.get("status", "")) == "quota-exceeded":
            return JSONResponse(
                {
                    "status": "quota-exceeded",
                    "detail": str(upload_result["error"]),
                    "caseCount": int(upload_result.get("case_count", 0)),
                    "sha256": str(upload_result.get("payload_sha256", "")),
                },
                status_code=429,
            )
        if str(upload_result.get("status", "")) == "duplicate":
            return JSONResponse(
                {
                    "status": "duplicate",
                    "uploadId": int(upload_result["upload_id"]),
                    "caseCount": int(upload_result["case_count"]),
                    "sha256": str(upload_result.get("payload_sha256", "")),
                },
                status_code=200,
            )

        return JSONResponse(
            {
                "status": "accepted",
                "uploadId": int(upload_result["upload_id"]),
                "caseCount": int(upload_result["case_count"]),
                "insertedCases": int(upload_result["inserted_cases"]),
                "updatedCases": int(upload_result["updated_cases"]),
                "sha256": str(upload_result.get("payload_sha256", "")),
            },
            status_code=201,
        )

    @app.post("/api/v1/client/auth/logout")
    async def api_client_logout(request: Request):
        session_token = _bearer_token_or_401(request)
        user = _current_api_user_or_401(request)
        source_ip, user_agent = _request_meta(request, settings)
        await run_in_threadpool(
            _revoke_session_by_token,
            settings.database_path,
            session_token,
            "api-logout",
            settings.secret_key,
        )
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="auth.api.logout",
            target_type="session",
            target_id=getattr(request.state, "session_id", None),
            details="API session logged out.",
            source_ip=source_ip,
            user_agent=user_agent,
        )
        return JSONResponse({"status": "ok"}, status_code=200)
