from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from ..infra import db as sqlite3
from ..core.hub_core import (
    _create_audit_log,
    _delete_user_upload,
    _refresh_user,
    _render_dashboard,
    _validate_csrf_token,
)
from ..core.upload_workflow import _accept_training_upload
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
        user_id = int(user["id"])
        source_ip, user_agent = _request_meta(request, settings)

        try:
            upload_result = await run_in_threadpool(
                _accept_training_upload,
                settings,
                user_id=user_id,
                payload=payload,
                original_name=training_file.filename,
                source_ip=source_ip,
                user_agent=user_agent,
            )
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

        if str(upload_result.get("status", "")) == "quota-exceeded":
            return await run_in_threadpool(
                _render_dashboard,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error=str(upload_result["error"]),
                status_code=429,
            )
        if str(upload_result.get("status", "")) == "duplicate":
            return await run_in_threadpool(
                _render_dashboard,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                notice=f"File already uploaded in your account (upload #{int(upload_result['upload_id'])}).",
            )

        upload_id = int(upload_result["upload_id"])
        case_count = int(upload_result["case_count"])
        inserted_cases = int(upload_result["inserted_cases"])
        updated_cases = int(upload_result["updated_cases"])
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

    @app.post("/dashboard/uploads/{upload_id}/delete", response_class=HTMLResponse)
    async def delete_own_upload(request: Request, upload_id: int, csrf_token: str = Form(...)):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        _validate_csrf_token(request, csrf_token)

        delete_result = await run_in_threadpool(
            _delete_user_upload,
            settings,
            int(user["id"]),
            int(upload_id),
        )
        if not bool(delete_result.get("ok")):
            return await run_in_threadpool(
                _render_dashboard,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error=str(delete_result.get("error", "Upload deletion failed.")),
                status_code=int(delete_result.get("status_code", 400)),
            )

        source_ip, user_agent = _request_meta(request, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="upload.delete.self",
            target_type="upload",
            target_id=int(upload_id),
            details=(
                f"Deleted own upload #{int(upload_id)}. "
                f"Cases deleted: {int(delete_result['deleted_cases'])}, rebuilt: {int(delete_result['rebuilt_cases'])}."
            ),
            source_ip=source_ip,
            user_agent=user_agent,
        )

        refreshed_user = await run_in_threadpool(_refresh_user, settings.database_path, int(user["id"])) or user
        return await run_in_threadpool(
            _render_dashboard,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=refreshed_user,
            notice=(
                f"Deleted upload #{int(upload_id)}. "
                f"Cases removed: {int(delete_result['deleted_cases'])}, rebuilt from remaining uploads: {int(delete_result['rebuilt_cases'])}."
            ),
        )

