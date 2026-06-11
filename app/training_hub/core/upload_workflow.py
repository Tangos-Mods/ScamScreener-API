from __future__ import annotations

import hashlib
import hmac
from typing import Any

from fastapi import HTTPException

from ..config.settings import TrainingHubSettings
from ..infra import db as sqlite3
from .admin_ops import _create_audit_log
from .common import _now_utc_iso
from .content_scrubbing import _apply_content_scrub_rules_to_cases
from .training_data import (
    _ensure_client_identity,
    _ingest_cases_from_upload,
    _json_dumps,
    _normalize_client_id,
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


def _serialize_training_upload_payload(parsed_cases: list[dict[str, Any]]) -> bytes:
    return "\n".join(_json_dumps(payload) for payload in parsed_cases).encode("utf-8")


def _accept_training_upload(
    settings: TrainingHubSettings,
    *,
    user_id: int | None = None,
    client_id: str | None = None,
    payload: bytes,
    original_name: str | None,
    source_ip: str,
    user_agent: str,
    audit_details_suffix: str = "",
) -> dict[str, Any]:
    if user_id is None and not (client_id or "").strip():
        raise HTTPException(status_code=400, detail="Upload identity is required.")

    parsed_cases = _parse_training_upload_payload(payload)
    parsed_cases, scrub_summary = _apply_content_scrub_rules_to_cases(settings.database_path, parsed_cases)
    stored_payload = payload
    if int(scrub_summary.get("fields_scrubbed", 0)) > 0:
        stored_payload = _serialize_training_upload_payload(parsed_cases)
    case_count = len(parsed_cases)
    payload_sha = hashlib.sha256(stored_payload).hexdigest()
    normalized_name = _safe_file_name(original_name)

    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        client_identity_id: int | None = None
        linked_user_id: int | None = None
        if client_id is not None and client_id.strip():
            client_identity = _ensure_client_identity(connection, _normalize_client_id(client_id))
            client_identity_id = int(client_identity["id"])
            if client_identity["linked_user_id"] is not None:
                linked_user_id = int(client_identity["linked_user_id"])

        if user_id is not None:
            own_existing = connection.execute(
                "SELECT id FROM uploads WHERE user_id = ? AND payload_sha256 = ?",
                (int(user_id), payload_sha),
            ).fetchone()
        else:
            own_existing = connection.execute(
                "SELECT id FROM uploads WHERE client_identity_id = ? AND payload_sha256 = ?",
                (int(client_identity_id or 0), payload_sha),
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
            int(user_id) if user_id is not None else None,
            int(client_identity_id) if client_identity_id is not None else None,
            source_ip,
            len(stored_payload),
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
        _write_payload(stored_path, stored_payload)

        cursor = connection.execute(
            """
            INSERT INTO uploads (
                created_at,
                user_id,
                client_identity_id,
                original_file_name,
                stored_path,
                payload_sha256,
                case_count,
                size_bytes,
                status,
                duplicate_of_upload_id,
                source_ip,
                user_agent
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now_utc_iso(),
                int(user_id) if user_id is not None else None,
                int(client_identity_id) if client_identity_id is not None else None,
                normalized_name,
                str(stored_path),
                payload_sha,
                case_count,
                len(stored_payload),
                "accepted",
                int(duplicate_row["id"]) if duplicate_row is not None else None,
                source_ip,
                (user_agent or "").strip()[:300],
            ),
        )
        connection.commit()
        upload_id = int(cursor.lastrowid)

    inserted_cases, updated_cases, skipped_rejected_cases = _ingest_cases_from_upload(
        settings.database_path,
        int(user_id) if user_id is not None else None,
        int(client_identity_id) if client_identity_id is not None else None,
        upload_id,
        parsed_cases,
    )
    details_suffix = audit_details_suffix
    if client_identity_id is not None and user_id is None:
        details_suffix = f" for client {_normalize_client_id(client_id or '')}{audit_details_suffix}"
    skipped_suffix = ""
    if skipped_rejected_cases:
        skipped_suffix = f" Skipped {int(skipped_rejected_cases)} tombstoned rejected cases."
    scrub_suffix = ""
    if int(scrub_summary.get("fields_scrubbed", 0)) > 0:
        scrub_suffix = (
            f" Scrubbed {int(scrub_summary['replacements_removed'])} matches across "
            f"{int(scrub_summary['fields_scrubbed'])} fields using {int(scrub_summary['rule_count'])} rules."
        )
    _create_audit_log(
        settings.database_path,
        actor_user_id=int(user_id) if user_id is not None else linked_user_id,
        action="upload.accepted",
        target_type="upload",
        target_id=upload_id,
        details=f"Accepted upload {upload_id} ({case_count} cases){details_suffix}.{scrub_suffix}{skipped_suffix}",
        source_ip=source_ip,
        user_agent=user_agent,
    )
    return {
        "status": "accepted",
        "upload_id": upload_id,
        "case_count": case_count,
        "inserted_cases": inserted_cases,
        "updated_cases": updated_cases,
        "skipped_rejected_cases": skipped_rejected_cases,
        "payload_sha256": payload_sha,
        "scrubbed_fields": int(scrub_summary.get("fields_scrubbed", 0)),
        "scrubbed_replacements": int(scrub_summary.get("replacements_removed", 0)),
    }


def _require_sha256_hex(value: str, *, header_name: str) -> str:
    normalized = (value or "").strip().lower()
    if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise HTTPException(status_code=400, detail=f"{header_name} must be a lowercase SHA-256 hex digest.")
    return normalized


def _validate_anonymous_upload_headers(
    *,
    client_id: str,
    payload: bytes,
    payload_sha_header: str,
    handshake_sha_header: str,
) -> tuple[str, str]:
    normalized_client_id = _normalize_client_id(client_id)
    normalized_payload_sha = _require_sha256_hex(payload_sha_header, header_name="X-ScamScreener-Payload-Sha256")
    recalculated_payload_sha = hashlib.sha256(payload).hexdigest()
    if not hmac.compare_digest(normalized_payload_sha, recalculated_payload_sha):
        raise HTTPException(status_code=400, detail="X-ScamScreener-Payload-Sha256 does not match the request body.")

    normalized_handshake_sha = _require_sha256_hex(
        handshake_sha_header,
        header_name="X-ScamScreener-Handshake-Sha256",
    )
    expected_handshake_sha = hashlib.sha256(f"{normalized_client_id}:{recalculated_payload_sha}".encode("utf-8")).hexdigest()
    if not hmac.compare_digest(normalized_handshake_sha, expected_handshake_sha):
        raise HTTPException(status_code=400, detail="X-ScamScreener-Handshake-Sha256 is invalid.")

    return normalized_client_id, recalculated_payload_sha
