from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse, RedirectResponse

from ..infra import db as sqlite3
from ..core.hub_core import _create_audit_log
from ..config.settings import TrainingHubSettings
from .admin_utils import is_path_within as _is_path_within, request_meta as _request_meta


def register_admin_download_routes(app: FastAPI, settings: TrainingHubSettings) -> None:
    @app.get("/admin/quarantine/bundle")
    async def admin_download_quarantine_bundle(request: Request):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        if int(user["is_admin"]) != 1:
            raise HTTPException(status_code=403, detail="Admin access required.")

        def _build_quarantine_bundle() -> tuple[Path, int]:
            quarantine_dir = settings.quarantine_dir
            files = sorted(path for path in quarantine_dir.glob("*.jsonl") if path.is_file())
            if not files:
                raise HTTPException(status_code=404, detail="No quarantined cases available.")

            file_descriptor, bundle_name = tempfile.mkstemp(prefix="scrub-quarantine-", suffix=".jsonl")
            os.close(file_descriptor)
            bundle_path = Path(bundle_name)
            case_count = 0
            try:
                with bundle_path.open("wb") as output_handle:
                    wrote_any = False
                    for file_path in files:
                        with file_path.open("rb") as input_handle:
                            for raw_line in input_handle:
                                if not raw_line.strip():
                                    continue
                                if wrote_any and not raw_line.endswith(b"\n"):
                                    output_handle.write(raw_line + b"\n")
                                else:
                                    output_handle.write(raw_line)
                                wrote_any = True
                                case_count += 1
                if case_count <= 0:
                    bundle_path.unlink(missing_ok=True)
                    raise HTTPException(status_code=404, detail="No quarantined cases available.")
            except Exception:
                bundle_path.unlink(missing_ok=True)
                raise
            return bundle_path, case_count

        bundle_path, case_count = await run_in_threadpool(_build_quarantine_bundle)
        source_ip, user_agent = _request_meta(request, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="training.quarantine.download",
            target_type="system",
            target_id=None,
            details=f"Downloaded quarantined-case bundle with {case_count} cases.",
            source_ip=source_ip,
            user_agent=user_agent,
        )

        return FileResponse(
            bundle_path,
            media_type="application/x-ndjson",
            filename="scrub-quarantine-cases.jsonl",
            background=BackgroundTask(lambda: bundle_path.unlink(missing_ok=True)),
        )

    @app.get("/admin/runs/{run_id}/bundle")
    async def admin_download_bundle(request: Request, run_id: int):
        user = request.state.user
        if user is None:
            return RedirectResponse(url="/login", status_code=303)
        if int(user["is_admin"]) != 1:
            raise HTTPException(status_code=403, detail="Admin access required.")

        def _load_bundle_path(target_run_id: int) -> str | None:
            with sqlite3.connect(settings.database_path) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    "SELECT bundle_path FROM training_runs WHERE id = ?",
                    (target_run_id,),
                ).fetchone()
            if row is None:
                return None
            return str(row["bundle_path"])

        bundle_path_raw = await run_in_threadpool(_load_bundle_path, run_id)
        if bundle_path_raw is None:
            raise HTTPException(status_code=404, detail="Training run not found.")

        bundle_path = Path(bundle_path_raw)
        if not _is_path_within(settings.bundles_dir, bundle_path):
            raise HTTPException(status_code=403, detail="Bundle path is outside allowed storage.")
        bundle_exists = await run_in_threadpool(bundle_path.exists)
        if not bundle_exists:
            raise HTTPException(status_code=404, detail="Bundle file missing from disk.")

        source_ip, user_agent = _request_meta(request, settings)
        await run_in_threadpool(
            _create_audit_log,
            settings.database_path,
            actor_user_id=int(user["id"]),
            action="training.bundle.download",
            target_type="training_run",
            target_id=run_id,
            details=f"Downloaded training bundle for run #{run_id}.",
            source_ip=source_ip,
            user_agent=user_agent,
        )
        return FileResponse(bundle_path, media_type="application/x-ndjson", filename=bundle_path.name)


