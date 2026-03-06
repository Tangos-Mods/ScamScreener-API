from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from ..core.hub_core import _create_audit_log, _create_backup_archive, _render_admin, _restore_backup_archive, _validate_csrf_token
from ..config.settings import TrainingHubSettings
from .admin_utils import read_upload_bytes as _read_upload_bytes, request_meta as _request_meta


def register_admin_backup_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    @app.post("/admin/backups/create")
    async def admin_create_backup(request: Request, csrf_token: str = Form(...)):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        if int(user["is_admin"]) != 1:
            raise HTTPException(status_code=403, detail="Admin access required.")
        _validate_csrf_token(request, csrf_token)

        backup_result = await run_in_threadpool(_create_backup_archive, settings)
        source_ip, user_agent = _request_meta(request, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="backup.created",
            target_type="backup",
            target_id=None,
            details=(
                f"Created backup {backup_result['backup_name']} "
                f"({int(backup_result.get('size_bytes', 0))} bytes)."
            ),
            source_ip=source_ip,
            user_agent=user_agent,
        )
        return FileResponse(
            Path(str(backup_result["backup_path"])),
            media_type="application/gzip",
            filename=str(backup_result["backup_name"]),
        )

    @app.post("/admin/backups/restore", response_class=HTMLResponse)
    async def admin_restore_backup(
        request: Request,
        backup_file: UploadFile = File(...),
        csrf_token: str = Form(...),
    ):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        if int(user["is_admin"]) != 1:
            raise HTTPException(status_code=403, detail="Admin access required.")
        _validate_csrf_token(request, csrf_token)

        if backup_file is None:
            return await run_in_threadpool(
                _render_admin,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error="No backup file uploaded.",
                status_code=400,
            )

        try:
            payload = await _read_upload_bytes(backup_file, int(settings.backup_restore_max_bytes))
        except HTTPException as exception:
            return await run_in_threadpool(
                _render_admin,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error=str(exception.detail),
                status_code=exception.status_code,
            )
        if not payload:
            return await run_in_threadpool(
                _render_admin,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error="Uploaded backup file is empty.",
                status_code=400,
            )

        temp_restore_file = settings.backups_dir / f"restore-{secrets.token_hex(16)}.tar.gz"

        def _restore_from_temp() -> dict[str, Any]:
            temp_restore_file.write_bytes(payload)
            try:
                return _restore_backup_archive(settings, temp_restore_file)
            finally:
                if temp_restore_file.exists():
                    try:
                        temp_restore_file.unlink()
                    except OSError:
                        pass

        try:
            restore_result = await run_in_threadpool(_restore_from_temp)
        except (ValueError, FileNotFoundError) as exception:
            return await run_in_threadpool(
                _render_admin,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error=f"Backup restore failed: {exception}",
                status_code=400,
            )
        except Exception:
            return await run_in_threadpool(
                _render_admin,
                request=request,
                templates=app.state.templates,
                settings=settings,
                user=user,
                error="Backup restore failed due to an internal error.",
                status_code=500,
            )

        source_ip, user_agent = _request_meta(request, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="backup.restored",
            target_type="backup",
            target_id=None,
            details=f"Restored backup. Rows: {restore_result.get('row_counts', {})}",
            source_ip=source_ip,
            user_agent=user_agent,
        )
        return await run_in_threadpool(
            _render_admin,
            request=request,
            templates=app.state.templates,
            settings=settings,
            user=user,
            notice="Backup restore completed successfully.",
        )


