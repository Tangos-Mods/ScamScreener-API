from __future__ import annotations

import hashlib
from typing import Any

from fastapi import HTTPException

from ..config.settings import TrainingHubSettings
from ..infra import db as sqlite3
from .admin_ops import _create_audit_log
from .common import _now_utc_iso
from .training_data import (
    _ingest_cases_from_upload,
    _parse_training_cases,
    _safe_file_name,
    _upload_quota_violation,
    _write_payload,
)


def _parse_training_upload_payload(payload: bytes) -> list[dict[str, Any]]:
    if not payload:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    try:
        payload_text = payload.decode("utf-8")
    except UnicodeDecodeError as exception:
        raise HTTPException(status_code=400, detail=f"File must be UTF-8 encoded. {exception}") from exception
    return _parse_training_cases(payload_text)


def _accept_training_upload(
    settings: TrainingHubSettings,
    *,
    user_id: int,
    payload: bytes,
    original_name: str | None,
    source_ip: str,
    user_agent: str,
    audit_details_suffix: str = "",
) -> dict[str, Any]:
    parsed_cases = _parse_training_upload_payload(payload)
    case_count = len(parsed_cases)
    payload_sha = hashlib.sha256(payload).hexdigest()
    normalized_name = _safe_file_name(original_name)

    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        own_existing = connection.execute(
            "SELECT id FROM uploads WHERE user_id = ? AND payload_sha256 = ?",
            (int(user_id), payload_sha),
        ).fetchone()
        if own_existing is not None:
            return {
                "status": "duplicate",
                "upload_id": int(own_existing["id"]),
                "case_count": case_count,
                "payload_sha256": payload_sha,
            }

        duplicate_row = connection.execute(
            "SELECT id FROM uploads WHERE payload_sha256 = ? ORDER BY id ASC LIMIT 1",
            (payload_sha,),
        ).fetchone()

        quota_error = _upload_quota_violation(
            settings.database_path,
            settings,
            int(user_id),
            source_ip,
            len(payload),
            case_count,
        )
        if quota_error:
            return {
                "status": "quota-exceeded",
                "error": quota_error,
                "case_count": case_count,
                "payload_sha256": payload_sha,
            }

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
                int(user_id),
                normalized_name,
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
        upload_id = int(cursor.lastrowid)

    inserted_cases, updated_cases = _ingest_cases_from_upload(
        settings.database_path,
        int(user_id),
        upload_id,
        parsed_cases,
    )
    _create_audit_log(
        settings.database_path,
        actor_user_id=int(user_id),
        action="upload.accepted",
        target_type="upload",
        target_id=upload_id,
        details=f"Accepted upload {upload_id} ({case_count} cases){audit_details_suffix}.",
        source_ip=source_ip,
        user_agent=user_agent,
    )
    return {
        "status": "accepted",
        "upload_id": upload_id,
        "case_count": case_count,
        "inserted_cases": inserted_cases,
        "updated_cases": updated_cases,
        "payload_sha256": payload_sha,
    }
