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


def _upload_quota_violation(
    database_path: Path,
    settings: TrainingHubSettings,
    user_id: int,
    source_ip: str,
    new_size_bytes: int,
    new_case_count: int,
) -> str:
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    day_start_iso = day_start.isoformat().replace("+00:00", "Z")
    day_end_iso = day_end.isoformat().replace("+00:00", "Z")

    with sqlite3.connect(database_path) as connection:
        user_row = connection.execute(
            """
            SELECT
                COUNT(*) AS upload_count,
                COALESCE(SUM(size_bytes), 0) AS total_bytes,
                COALESCE(SUM(case_count), 0) AS total_cases
            FROM uploads
            WHERE status = 'accepted' AND user_id = ? AND created_at >= ? AND created_at < ?
            """,
            (int(user_id), day_start_iso, day_end_iso),
        ).fetchone()

        user_upload_count = int(user_row[0] if user_row is not None else 0)
        user_total_bytes = int(user_row[1] if user_row is not None else 0)
        user_total_cases = int(user_row[2] if user_row is not None else 0)

        if user_upload_count + 1 > settings.max_uploads_per_day_per_user:
            return "Daily upload count limit reached for your account."
        if user_total_bytes + int(new_size_bytes) > settings.max_upload_bytes_per_day_per_user:
            return "Daily upload size limit reached for your account."
        if user_total_cases + int(new_case_count) > settings.max_upload_cases_per_day_per_user:
            return "Daily case-count limit reached for your account."

        normalized_ip = (source_ip or "").strip()
        if normalized_ip:
            ip_row = connection.execute(
                """
                SELECT COUNT(*)
                FROM uploads
                WHERE status = 'accepted' AND source_ip = ? AND created_at >= ? AND created_at < ?
                """,
                (normalized_ip, day_start_iso, day_end_iso),
            ).fetchone()
            ip_upload_count = int(ip_row[0] if ip_row is not None else 0)
            if ip_upload_count + 1 > settings.max_uploads_per_day_per_ip:
                return "Daily upload count limit reached for your IP."

        global_row = connection.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM uploads WHERE status = 'accepted'"
        ).fetchone()
        global_bytes = int(global_row[0] if global_row is not None else 0)
        if global_bytes + int(new_size_bytes) > settings.global_upload_storage_cap_bytes:
            return "Global upload storage capacity reached."

    return ""


def _json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))


def _extract_case_fields(payload: dict[str, Any]) -> tuple[str, str, list[str]]:
    case_data = payload.get("caseData")
    observed = payload.get("observedPipeline")
    case_data_obj = case_data if isinstance(case_data, dict) else {}
    observed_obj = observed if isinstance(observed, dict) else {}

    label = str(case_data_obj.get("label", "")).strip()
    outcome = str(observed_obj.get("outcomeAtCapture", "")).strip()
    raw_tags = case_data_obj.get("caseSignalTagIds", [])
    if not isinstance(raw_tags, list):
        raw_tags = []
    tags = [str(value).strip() for value in raw_tags if str(value).strip()]
    return label, outcome, tags


def _ingest_cases_from_upload(
    database_path: Path,
    user_id: int,
    upload_id: int,
    parsed_cases: list[dict[str, Any]],
) -> tuple[int, int]:
    inserted = 0
    updated = 0
    now = _now_utc_iso()
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        for payload in parsed_cases:
            case_id = str(payload.get("caseId", "")).strip()
            if not case_id:
                raise HTTPException(status_code=400, detail="Case payload is missing caseId.")

            label, outcome, tags = _extract_case_fields(payload)
            existing = connection.execute("SELECT id FROM training_cases WHERE case_id = ?", (case_id,)).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO training_cases (
                        case_id,
                        created_at,
                        updated_at,
                        created_by_user_id,
                        source_upload_id,
                        status,
                        label,
                        outcome,
                        tag_ids_json,
                        payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        case_id,
                        now,
                        now,
                        user_id,
                        upload_id,
                        "submitted",
                        label,
                        outcome,
                        _json_dumps(tags),
                        _json_dumps(payload),
                    ),
                )
                inserted += 1
            else:
                connection.execute(
                    """
                    UPDATE training_cases
                    SET updated_at = ?, label = ?, outcome = ?, tag_ids_json = ?, source_upload_id = ?, payload_json = ?
                    WHERE case_id = ?
                    """,
                    (now, label, outcome, _json_dumps(tags), upload_id, _json_dumps(payload), case_id),
                )
                updated += 1
        connection.commit()
    return inserted, updated


def _global_stats(database_path: Path) -> dict[str, int]:
    with sqlite3.connect(database_path) as connection:
        users = int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])
        uploads = int(connection.execute("SELECT COUNT(*) FROM uploads").fetchone()[0])
    return {"users": users, "uploads": uploads}


def _user_uploads(database_path: Path, user_id: int) -> list[sqlite3.Row]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """
            SELECT id, created_at, original_file_name, case_count, size_bytes, payload_sha256, duplicate_of_upload_id
            FROM uploads
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (user_id,),
        ).fetchall()


def _parse_training_cases(payload_text: str) -> list[dict[str, Any]]:
    parsed_cases: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(payload_text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exception:
            raise HTTPException(status_code=400, detail=f"Invalid JSON on line {line_number}: {exception.msg}") from exception

        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail=f"Line {line_number} must be a JSON object.")
        if str(payload.get("format", "")).strip().lower() != TRAINING_FORMAT:
            raise HTTPException(status_code=400, detail=f"Line {line_number} has unsupported format.")

        schema_version = payload.get("schemaVersion")
        try:
            schema_version_int = int(schema_version)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"Line {line_number} has invalid schemaVersion.")
        if schema_version_int != TRAINING_SCHEMA_VERSION:
            raise HTTPException(status_code=400, detail=f"Line {line_number} has unsupported schemaVersion {schema_version_int}.")

        case_id = str(payload.get("caseId", "")).strip()
        if not case_id:
            raise HTTPException(status_code=400, detail=f"Line {line_number} is missing caseId.")

        parsed_cases.append(payload)

    if not parsed_cases:
        raise HTTPException(status_code=400, detail="No training cases found in payload.")
    return parsed_cases


def _write_payload(target_path: Path, payload: bytes) -> None:
    if target_path.exists():
        return
    temp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    temp_path.write_bytes(payload)
    temp_path.replace(target_path)


def _safe_file_name(file_name: str | None) -> str:
    if file_name is None or not file_name.strip():
        return "training-cases-v2.jsonl"
    return Path(file_name.strip()).name

