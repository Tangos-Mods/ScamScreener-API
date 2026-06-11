from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from ..config.settings import TrainingHubSettings
from ..core.common import _authorization_bearer_token
from ..core.hub_core import (
    _revoke_session_by_token,
)
from ..core.upload_workflow import _accept_training_upload
from ..core.upload_workflow import _validate_anonymous_upload_headers
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

def _require_ndjson_content_type(request: Request) -> None:
    content_type = str(request.headers.get("content-type", "")).split(";", 1)[0].strip().lower()
    if content_type != "application/x-ndjson":
        raise HTTPException(status_code=415, detail="Content-Type must be application/x-ndjson.")


def register_public_api_client_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    @app.post("/api/v1/client/uploads")
    async def api_client_upload(request: Request):
        _bearer_token_or_401(request)
        user = _current_api_user_or_401(request)
        _require_ndjson_content_type(request)
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
                "skippedRejectedCases": int(upload_result.get("skipped_rejected_cases", 0)),
                "sha256": str(upload_result.get("payload_sha256", "")),
            },
            status_code=201,
        )

    @app.post("/api/v1/client/uploads/anonymous")
    async def api_client_upload_anonymous(request: Request):
        _require_ndjson_content_type(request)
        payload = await _read_request_bytes(request, settings.max_upload_bytes)
        source_ip, user_agent = _request_meta(request, settings)
        original_name = str(request.headers.get("x-scamscreener-filename", "")).strip() or "training-cases-v2.jsonl"
        normalized_client_id, payload_sha = _validate_anonymous_upload_headers(
            client_id=str(request.headers.get("x-scamscreener-client-id", "")),
            payload=payload,
            payload_sha_header=str(request.headers.get("x-scamscreener-payload-sha256", "")),
            handshake_sha_header=str(request.headers.get("x-scamscreener-handshake-sha256", "")),
        )

        upload_result = await run_in_threadpool(
            _accept_training_upload,
            settings,
            client_id=normalized_client_id,
            payload=payload,
            original_name=original_name,
            source_ip=source_ip,
            user_agent=user_agent,
            audit_details_suffix=" via anonymous API client",
        )
        if str(upload_result.get("status", "")) == "quota-exceeded":
            return JSONResponse(
                {
                    "status": "quota-exceeded",
                    "detail": str(upload_result["error"]),
                    "caseCount": int(upload_result.get("case_count", 0)),
                    "sha256": payload_sha,
                },
                status_code=429,
            )
        if str(upload_result.get("status", "")) == "duplicate":
            return JSONResponse(
                {
                    "status": "duplicate",
                    "uploadId": int(upload_result["upload_id"]),
                    "caseCount": int(upload_result["case_count"]),
                    "sha256": payload_sha,
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
                "skippedRejectedCases": int(upload_result.get("skipped_rejected_cases", 0)),
                "sha256": payload_sha,
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
