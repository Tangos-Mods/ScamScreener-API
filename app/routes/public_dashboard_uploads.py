from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from ..infra import db as sqlite3
from ..core.hub_core import (
    _create_audit_log,
    _ingest_cases_from_upload,
    _now_utc_iso,
    _parse_training_cases,
    _refresh_user,
    _render_dashboard,
    _safe_file_name,
    _upload_quota_violation,
    _validate_csrf_token,
    _write_payload,
)
from ..config.settings import TrainingHubSettings
from .public_utils import (
    is_path_within as _is_path_within,
    read_upload_bytes as _read_upload_bytes,
    request_meta as _request_meta,
)


def register_public_dashboard_upload_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    @app.post("/dashboard/upload", response_class=HTMLResponse)
    async def upload_training_file(
        request: Request,
        csrf_token: str = Form(...),
        training_file: UploadFile = File(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)

        if training_file is None:
            return await run_in_threadpool(
                _render_dashboard,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error="No file uploaded.",
                status_code=400,
            )

        try:
            payload = await _read_upload_bytes(training_file, settings.max_upload_bytes)
        except HTTPException as exception:
            return await run_in_threadpool(
                _render_dashboard,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error=str(exception.detail),
                status_code=exception.status_code,
            )
        if not payload:
            return await run_in_threadpool(
                _render_dashboard,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error="Uploaded file is empty.",
                status_code=400,
            )

        try:
            payload_text = payload.decode("utf-8")
        except UnicodeDecodeError as exception:
            return await run_in_threadpool(
                _render_dashboard,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error=f"File must be UTF-8 encoded. {exception}",
                status_code=400,
            )

        try:
            parsed_cases = await run_in_threadpool(_parse_training_cases, payload_text)
            case_count = len(parsed_cases)
        except HTTPException as exception:
            return await run_in_threadpool(
                _render_dashboard,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error=str(exception.detail),
                status_code=exception.status_code,
            )

        payload_sha = hashlib.sha256(payload).hexdigest()
        original_name = _safe_file_name(training_file.filename)
        user_id = int(user["id"])
        source_ip, user_agent = _request_meta(request, settings)

        def _store_upload_sync() -> dict[str, int | None]:
            with sqlite3.connect(settings.database_path) as connection:
                connection.row_factory = sqlite3.Row
                own_existing = connection.execute(
                    "SELECT id FROM uploads WHERE user_id = ? AND payload_sha256 = ?",
                    (user_id, payload_sha),
                ).fetchone()
                if own_existing is not None:
                    return {"existing_upload_id": int(own_existing["id"]), "upload_id": None}

                duplicate_row = connection.execute(
                    "SELECT id FROM uploads WHERE payload_sha256 = ? ORDER BY id ASC LIMIT 1",
                    (payload_sha,),
                ).fetchone()

                quota_error = _upload_quota_violation(
                    settings.database_path,
                    settings,
                    user_id,
                    source_ip,
                    len(payload),
                    case_count,
                )
                if quota_error:
                    return {"error": quota_error, "upload_id": None, "existing_upload_id": None}

                stored_path = settings.uploads_dir / f"{payload_sha}.jsonl"
                _write_payload(stored_path, payload)

                cursor = connection.execute(
                    """
                    INSERT INTO uploads (
                        created_at,
                        user_id,
                        original_file_name,
                        stored_path,
                        payload_sha256,
                        case_count,
                        size_bytes,
                        status,
                        duplicate_of_upload_id,
                        source_ip
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _now_utc_iso(),
                        user_id,
                        original_name,
                        str(stored_path),
                        payload_sha,
                        case_count,
                        len(payload),
                        "accepted",
                        int(duplicate_row["id"]) if duplicate_row is not None else None,
                        source_ip,
                    ),
                )
                connection.commit()
                return {"existing_upload_id": None, "upload_id": int(cursor.lastrowid)}

        store_result = await run_in_threadpool(_store_upload_sync)
        if "error" in store_result:
            return await run_in_threadpool(
                _render_dashboard,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error=str(store_result["error"]),
                status_code=429,
            )
        existing_upload_id = store_result.get("existing_upload_id")
        if existing_upload_id is not None:
            return await run_in_threadpool(
                _render_dashboard,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                notice=f"File already uploaded in your account (upload #{int(existing_upload_id)}).",
            )

        upload_id = int(store_result["upload_id"])
        inserted_cases, updated_cases = await run_in_threadpool(
            _ingest_cases_from_upload,
            settings.database_path,
            user_id,
            upload_id,
            parsed_cases,
        )
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=user_id,
            action="upload.accepted",
            target_type="upload",
            target_id=upload_id,
            details=f"Accepted upload {upload_id} ({case_count} cases).",
            source_ip=source_ip,
            user_agent=user_agent,
        )
        refreshed_user = await run_in_threadpool(_refresh_user, settings.database_path, user_id) or user
        return await run_in_threadpool(
            _render_dashboard,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=refreshed_user,
            notice=(
                f"Upload #{upload_id} accepted with {case_count} cases. "
                f"Cases inserted: {inserted_cases}, updated: {updated_cases}."
            ),
            status_code=201,
        )

    @app.get("/dashboard/uploads/{upload_id}/download")
    async def download_own_upload(request: Request, upload_id: int):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)

        def _load_upload_sync(target_upload_id: int) -> dict[str, Any] | None:
            with sqlite3.connect(settings.database_path) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    "SELECT user_id, original_file_name, stored_path FROM uploads WHERE id = ?",
                    (target_upload_id,),
                ).fetchone()
            if row is None:
                return None
            return {
                "user_id": int(row["user_id"]),
                "original_file_name": str(row["original_file_name"]),
                "stored_path": str(row["stored_path"]),
            }

        upload_row = await run_in_threadpool(_load_upload_sync, upload_id)
        if upload_row is None:
            raise HTTPException(status_code=404, detail="Upload not found.")
        if upload_row["user_id"] != int(user["id"]) and int(user["is_admin"]) != 1:
            raise HTTPException(status_code=403, detail="Not allowed.")

        file_path = Path(str(upload_row["stored_path"]))
        if not _is_path_within(settings.uploads_dir, file_path):
            raise HTTPException(status_code=403, detail="Upload path is outside allowed storage.")
        file_exists = await run_in_threadpool(file_path.exists)
        if not file_exists:
            raise HTTPException(status_code=404, detail="Upload file missing from disk.")

        source_ip, user_agent = _request_meta(request, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="upload.download",
            target_type="upload",
            target_id=int(upload_id),
            details=f"Downloaded upload #{upload_id}.",
            source_ip=source_ip,
            user_agent=user_agent,
        )

        return FileResponse(
            file_path,
            media_type="application/x-ndjson",
            filename=str(upload_row["original_file_name"]),
        )

