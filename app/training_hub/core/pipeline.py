from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import tarfile
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from ..infra import db as sqlite3
from ..config.settings import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, TRAINING_FORMAT, TRAINING_SCHEMA_VERSION, TrainingHubSettings

from .common import _now_utc_iso


def _run_training_pipeline(
    settings: TrainingHubSettings,
    started_by_user_id: int,
    bundle_override_path: Path | None = None,
) -> dict[str, Any]:
    included_uploads = 0
    included_cases = 0

    if bundle_override_path is not None:
        bundle_path = bundle_override_path
        if not bundle_path.exists():
            return {
                "status": "failed",
                "message": "Dataset bundle file does not exist.",
                "run_id": None,
                "output_log": "",
            }
        included_uploads = 1
        included_cases = _count_non_empty_lines(bundle_path)
        if included_cases <= 0:
            return {"status": "failed", "message": "Dataset bundle is empty.", "run_id": None, "output_log": ""}
    else:
        with sqlite3.connect(settings.database_path) as connection:
            connection.row_factory = sqlite3.Row
            upload_rows = connection.execute(
                """
                SELECT stored_path, case_count
                FROM uploads
                WHERE status = 'accepted'
                ORDER BY created_at ASC
                """
            ).fetchall()

        if not upload_rows:
            return {"status": "failed", "message": "No accepted uploads available for training.", "run_id": None, "output_log": ""}

        bundle_timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        bundle_path = settings.bundles_dir / f"training-bundle-{bundle_timestamp}.jsonl"

        with bundle_path.open("wb") as target:
            for row in upload_rows:
                source_path = Path(str(row["stored_path"]))
                if not source_path.exists():
                    continue

                payload = source_path.read_bytes().strip()
                if not payload:
                    continue

                if included_uploads > 0:
                    target.write(b"\n")
                target.write(payload)
                included_uploads += 1
                included_cases += int(row["case_count"])

        if included_uploads == 0:
            return {"status": "failed", "message": "No readable upload files found on disk.", "run_id": None, "output_log": ""}

    status = "prepared"
    output_log = f"Prepared training bundle with {included_uploads} source files and {included_cases} cases at {bundle_path}."

    output_log = output_log.strip()
    if len(output_log) > 20_000:
        output_log = output_log[:20_000] + "\n... (truncated)"

    with sqlite3.connect(settings.database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO training_runs (
                created_at,
                started_by_user_id,
                upload_count,
                case_count,
                status,
                command,
                bundle_path,
                output_log
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now_utc_iso(),
                started_by_user_id,
                included_uploads,
                included_cases,
                status,
                "",
                str(bundle_path),
                output_log,
            ),
        )
        run_id = int(cursor.lastrowid)
        connection.commit()

    if status == "failed":
        return {
            "status": "failed",
            "message": "Bundle creation failed. Check Training Runs log.",
            "run_id": run_id,
            "output_log": output_log,
        }
    return {
        "status": "prepared",
        "message": "Training bundle built successfully.",
        "run_id": run_id,
        "output_log": output_log,
    }


def _count_non_empty_lines(file_path: Path) -> int:
    if not file_path.exists():
        return 0
    with file_path.open("r", encoding="utf-8") as source:
        return sum(1 for line in source if line.strip())

