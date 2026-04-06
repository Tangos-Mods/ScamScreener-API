from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config.settings import TrainingHubSettings
from ..infra import db as sqlite3
from ..services.mailer import send_account_data_export_email
from .admin_ops import _create_audit_log
from .common import _now_utc_iso
from .training_data import _safe_file_name

logger = logging.getLogger(__name__)

EXPORT_SCHEMA_VERSION = 1


def _user_data_export_requests(database_path: Path | str, user_id: int, limit: int = 10) -> list[dict[str, Any]]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, created_at, status, completed_at, failed_at, size_bytes, delivery_error
            FROM data_export_requests
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (int(user_id), int(limit)),
        ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "created_at": str(row["created_at"]),
            "status": str(row["status"]),
            "completed_at": str(row["completed_at"] or ""),
            "failed_at": str(row["failed_at"] or ""),
            "size_bytes": int(row["size_bytes"] or 0),
            "delivery_error": str(row["delivery_error"] or ""),
        }
        for row in rows
    ]


def _queue_user_data_export_request(
    settings: TrainingHubSettings,
    user_id: int,
    source_ip: str,
    user_agent: str,
) -> dict[str, Any]:
    if not _email_delivery_available(settings):
        return {
            "ok": False,
            "error": "Account data export emails are unavailable until SMTP is configured.",
            "status_code": 503,
        }

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat().replace("+00:00", "Z")
    cooldown_cutoff = now - timedelta(minutes=int(settings.data_export_cooldown_minutes))

    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        user_row = connection.execute(
            "SELECT id, email FROM users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
        if user_row is None:
            return {"ok": False, "error": "Account not found.", "status_code": 404}

        pending_row = connection.execute(
            """
            SELECT id
            FROM data_export_requests
            WHERE user_id = ? AND status IN ('pending', 'processing')
            LIMIT 1
            """,
            (int(user_id),),
        ).fetchone()
        if pending_row is not None:
            return {
                "ok": False,
                "error": "A data export request for your account is already pending.",
                "status_code": 409,
            }

        latest_row = connection.execute(
            """
            SELECT created_at
            FROM data_export_requests
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (int(user_id),),
        ).fetchone()
        if latest_row is not None:
            latest_created_at = _parse_utc_iso(str(latest_row["created_at"]))
            if latest_created_at >= cooldown_cutoff:
                return {
                    "ok": False,
                    "error": (
                        "Please wait before requesting another account data export email. "
                        f"Cooldown: {int(settings.data_export_cooldown_minutes)} minutes."
                    ),
                    "status_code": 429,
                }

        cursor = connection.execute(
            """
            INSERT INTO data_export_requests (
                created_at,
                user_id,
                requested_email,
                status,
                requested_from_ip,
                request_user_agent,
                completed_at,
                failed_at,
                delivery_error,
                archive_sha256,
                size_bytes
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, '', '', 0)
            """,
            (
                now_iso,
                int(user_id),
                str(user_row["email"]),
                "pending",
                (source_ip or "").strip()[:80],
                (user_agent or "").strip()[:300],
            ),
        )
        connection.commit()

    return {
        "ok": True,
        "request_id": int(cursor.lastrowid),
        "created_at": now_iso,
        "recipient_email": str(user_row["email"]),
    }


def _process_next_data_export_request(settings: TrainingHubSettings) -> bool:
    claimed = _claim_next_data_export_request(settings.database_path)
    if claimed is None:
        return False

    request_id = int(claimed["id"])
    actor_user_id = int(claimed["user_id"])
    source_ip = str(claimed["requested_from_ip"] or "")
    user_agent = str(claimed["request_user_agent"] or "")

    try:
        if not _email_delivery_available(settings):
            raise _DataExportFailure("Email delivery is not configured.")

        export_payload = _build_user_data_export_archive(
            settings,
            actor_user_id,
            request_id=request_id,
            requested_at=str(claimed["created_at"]),
            requested_email=str(claimed["requested_email"]),
        )
        send_account_data_export_email(
            settings,
            recipient_email=str(claimed["requested_email"]),
            requested_at=str(claimed["created_at"]),
            archive_name=str(export_payload["archive_name"]),
            archive_bytes=bytes(export_payload["archive_bytes"]),
            size_bytes=int(export_payload["size_bytes"]),
        )
    except _DataExportFailure as exception:
        _mark_data_export_request_failed(
            settings.database_path,
            request_id,
            actor_user_id,
            source_ip,
            user_agent,
            str(exception),
        )
        return True
    except Exception:
        logger.exception("Account data export delivery failed for request_id=%s.", request_id)
        _mark_data_export_request_failed(
            settings.database_path,
            request_id,
            actor_user_id,
            source_ip,
            user_agent,
            "Email delivery failed.",
        )
        return True

    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            """
            UPDATE data_export_requests
            SET status = 'sent', completed_at = ?, failed_at = NULL, delivery_error = ?, archive_sha256 = ?, size_bytes = ?
            WHERE id = ?
            """,
            (
                _now_utc_iso(),
                "",
                str(export_payload["archive_sha256"]),
                int(export_payload["size_bytes"]),
                int(request_id),
            ),
        )
        connection.commit()

    _create_audit_log(
        settings.database_path,
        actor_user_id=actor_user_id,
        action="account.data_export.sent",
        target_type="data_export_request",
        target_id=request_id,
        details=f"Sent account data export request #{request_id}.",
        source_ip=source_ip,
        user_agent=user_agent,
    )
    return True


def _claim_next_data_export_request(database_path: Path | str) -> dict[str, Any] | None:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT id, user_id, created_at, requested_email, requested_from_ip, request_user_agent
            FROM data_export_requests
            WHERE status = 'pending'
            ORDER BY created_at ASC, id ASC
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None

        cursor = connection.execute(
            "UPDATE data_export_requests SET status = 'processing' WHERE id = ? AND status = 'pending'",
            (int(row["id"]),),
        )
        connection.commit()
        if int(cursor.rowcount or 0) != 1:
            return None
    return dict(row)


def _mark_data_export_request_failed(
    database_path: Path | str,
    request_id: int,
    actor_user_id: int,
    source_ip: str,
    user_agent: str,
    reason: str,
) -> None:
    safe_reason = (reason or "Account data export failed.").strip()[:300]
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE data_export_requests
            SET status = 'failed', failed_at = ?, completed_at = NULL, delivery_error = ?, archive_sha256 = '', size_bytes = 0
            WHERE id = ?
            """,
            (_now_utc_iso(), safe_reason, int(request_id)),
        )
        connection.commit()

    user_exists = False
    with sqlite3.connect(database_path) as connection:
        row = connection.execute("SELECT id FROM users WHERE id = ?", (int(actor_user_id),)).fetchone()
        user_exists = row is not None
    if user_exists:
        _create_audit_log(
            database_path,
            actor_user_id=actor_user_id,
            action="account.data_export.failed",
            target_type="data_export_request",
            target_id=int(request_id),
            details=f"Account data export request #{request_id} failed.",
            source_ip=source_ip,
            user_agent=user_agent,
        )


def _build_user_data_export_archive(
    settings: TrainingHubSettings,
    user_id: int,
    *,
    request_id: int,
    requested_at: str,
    requested_email: str,
) -> dict[str, Any]:
    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        user_row = connection.execute(
            """
            SELECT id, created_at, username, email, is_admin, last_login_at
            FROM users
            WHERE id = ?
            """,
            (int(user_id),),
        ).fetchone()
        if user_row is None:
            raise _DataExportFailure("Account not found.")

        upload_rows = connection.execute(
            """
            SELECT id, created_at, original_file_name, stored_path, payload_sha256, case_count, size_bytes, status, duplicate_of_upload_id, source_ip
            FROM uploads
            WHERE user_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (int(user_id),),
        ).fetchall()
        upload_ids = [int(row["id"]) for row in upload_rows]

        sessions = connection.execute(
            """
            SELECT id, created_at, expires_at, revoked_at, remote_addr, user_agent, revoke_reason
            FROM sessions
            WHERE user_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (int(user_id),),
        ).fetchall()
        reset_rows = connection.execute(
            """
            SELECT created_at, expires_at, consumed_at, source_ip, user_agent
            FROM password_reset_tokens
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (int(user_id),),
        ).fetchall()
        mfa_rows = connection.execute(
            """
            SELECT created_at, expires_at, consumed_at, failed_attempts, source_ip, user_agent
            FROM admin_mfa_challenges
            WHERE user_id = ?
            ORDER BY created_at DESC
            """,
            (int(user_id),),
        ).fetchall()
        run_rows = connection.execute(
            """
            SELECT id, created_at, upload_count, case_count, status
            FROM training_runs
            WHERE started_by_user_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (int(user_id),),
        ).fetchall()
        export_rows = connection.execute(
            """
            SELECT id, created_at, requested_email, status, completed_at, failed_at, size_bytes
            FROM data_export_requests
            WHERE user_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (int(user_id),),
        ).fetchall()
        audit_actor_rows = connection.execute(
            """
            SELECT created_at, action, target_type, target_id, source_ip, user_agent
            FROM audit_logs
            WHERE actor_user_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (int(user_id),),
        ).fetchall()
        audit_target_rows = connection.execute(
            """
            SELECT created_at, action, target_type, target_id, source_ip, user_agent
            FROM audit_logs
            WHERE target_type = 'user' AND target_id = ? AND actor_user_id != ?
            ORDER BY created_at DESC, id DESC
            """,
            (int(user_id), int(user_id)),
        ).fetchall()
        created_case_rows = connection.execute(
            """
            SELECT id, case_id, created_at, updated_at, status, label, outcome, tag_ids_json, payload_json, source_upload_id
            FROM training_cases
            WHERE created_by_user_id = ?
            ORDER BY updated_at DESC, id DESC
            """,
            (int(user_id),),
        ).fetchall()
        sourced_case_rows = []
        if upload_ids:
            sourced_case_rows = connection.execute(
                """
                SELECT id, case_id, created_at, updated_at, status, label, outcome, tag_ids_json, payload_json, source_upload_id
                FROM training_cases
                """
                + f" WHERE source_upload_id IN ({_placeholders(len(upload_ids))})"
                + " ORDER BY updated_at DESC, id DESC",
                tuple(upload_ids),
            ).fetchall()

    warnings: list[str] = []
    payload = {
        "exportVersion": EXPORT_SCHEMA_VERSION,
        "generatedAt": _now_utc_iso(),
        "request": {
            "id": int(request_id),
            "requestedAt": str(requested_at),
            "requestedEmail": str(requested_email),
        },
        "account": {
            "id": int(user_row["id"]),
            "createdAt": str(user_row["created_at"]),
            "username": str(user_row["username"]),
            "email": str(user_row["email"]),
            "isAdmin": int(user_row["is_admin"]) == 1,
            "lastLoginAt": str(user_row["last_login_at"] or ""),
        },
        "sessions": [
            {
                "id": int(row["id"]),
                "createdAt": str(row["created_at"]),
                "expiresAt": str(row["expires_at"]),
                "revokedAt": str(row["revoked_at"] or ""),
                "remoteAddress": str(row["remote_addr"] or ""),
                "userAgent": str(row["user_agent"] or ""),
                "revokeReason": str(row["revoke_reason"] or ""),
            }
            for row in sessions
        ],
        "passwordResetRequests": [
            {
                "createdAt": str(row["created_at"]),
                "expiresAt": str(row["expires_at"]),
                "consumedAt": str(row["consumed_at"] or ""),
                "sourceIp": str(row["source_ip"] or ""),
                "userAgent": str(row["user_agent"] or ""),
            }
            for row in reset_rows
        ],
        "adminMfaChallenges": [
            {
                "createdAt": str(row["created_at"]),
                "expiresAt": str(row["expires_at"]),
                "consumedAt": str(row["consumed_at"] or ""),
                "failedAttempts": int(row["failed_attempts"] or 0),
                "sourceIp": str(row["source_ip"] or ""),
                "userAgent": str(row["user_agent"] or ""),
            }
            for row in mfa_rows
        ],
        "uploads": [
            {
                "id": int(row["id"]),
                "createdAt": str(row["created_at"]),
                "originalFileName": str(row["original_file_name"]),
                "payloadSha256": str(row["payload_sha256"]),
                "caseCount": int(row["case_count"]),
                "sizeBytes": int(row["size_bytes"]),
                "status": str(row["status"]),
                "duplicateOfUploadId": int(row["duplicate_of_upload_id"]) if row["duplicate_of_upload_id"] is not None else None,
                "sourceIp": str(row["source_ip"] or ""),
                "serverPathRedacted": True,
            }
            for row in upload_rows
        ],
        "trainingCasesCreatedByAccount": [_serialize_case_row(row) for row in created_case_rows],
        "trainingCasesCurrentlySourcedFromAccountUploads": [_serialize_case_row(row) for row in sourced_case_rows],
        "trainingRuns": [
            {
                "id": int(row["id"]),
                "createdAt": str(row["created_at"]),
                "uploadCount": int(row["upload_count"]),
                "caseCount": int(row["case_count"]),
                "status": str(row["status"]),
                "bundleContentsRedacted": True,
            }
            for row in run_rows
        ],
        "auditLogsAsActor": [
            {
                "createdAt": str(row["created_at"]),
                "action": str(row["action"]),
                "targetType": str(row["target_type"] or ""),
                "targetId": int(row["target_id"]) if row["target_id"] is not None else None,
                "sourceIp": str(row["source_ip"] or ""),
                "userAgent": str(row["user_agent"] or ""),
                "detailsRedacted": True,
            }
            for row in audit_actor_rows
        ],
        "auditLogsTargetingAccount": [
            {
                "createdAt": str(row["created_at"]),
                "action": str(row["action"]),
                "targetType": str(row["target_type"] or ""),
                "targetId": int(row["target_id"]) if row["target_id"] is not None else None,
                "sourceIp": str(row["source_ip"] or ""),
                "userAgent": str(row["user_agent"] or ""),
                "detailsRedacted": True,
            }
            for row in audit_target_rows
        ],
        "dataExportRequests": [
            {
                "id": int(row["id"]),
                "createdAt": str(row["created_at"]),
                "requestedEmail": str(row["requested_email"]),
                "status": str(row["status"]),
                "completedAt": str(row["completed_at"] or ""),
                "failedAt": str(row["failed_at"] or ""),
                "sizeBytes": int(row["size_bytes"] or 0),
            }
            for row in export_rows
        ],
        "redactions": [
            "Password hashes, session token hashes, reset token hashes, and MFA code hashes are omitted for security.",
            "Internal absolute storage paths, audit-log detail text, training bundle contents, and bundle execution logs are not included.",
        ],
        "warnings": warnings,
        "counts": {
            "uploads": len(upload_rows),
            "sessions": len(sessions),
            "createdCases": len(created_case_rows),
            "sourcedCases": len(sourced_case_rows),
            "auditLogsAsActor": len(audit_actor_rows),
            "auditLogsTargetingAccount": len(audit_target_rows),
        },
    }

    upload_archive_entries: list[tuple[Path, str]] = []
    for row in upload_rows:
        upload_file_path = Path(str(row["stored_path"] or ""))
        if not _path_is_safe(settings.uploads_dir, upload_file_path):
            warnings.append(f"Upload #{int(row['id'])} raw file path was outside allowed storage and was skipped.")
            continue
        if not upload_file_path.exists():
            warnings.append(f"Upload #{int(row['id'])} raw file is missing from disk.")
            continue
        upload_archive_entries.append(
            (
                upload_file_path,
                f"uploads/upload-{int(row['id'])}-{_safe_file_name(str(row['original_file_name']))}",
            )
        )

    archive_base = f"account-data-export-{int(user_row['id'])}-{_archive_timestamp()}"
    archive_name = f"{archive_base}.zip"
    with tempfile.TemporaryDirectory() as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        archive_path = temp_dir / archive_name
        with zipfile.ZipFile(archive_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "account-data-export.json",
                json.dumps(payload, ensure_ascii=True, indent=2).encode("utf-8"),
            )
            for upload_file_path, archive_name_entry in upload_archive_entries:
                archive.write(upload_file_path, arcname=archive_name_entry)

        size_bytes = archive_path.stat().st_size
        if size_bytes > int(settings.data_export_max_archive_bytes):
            raise _DataExportFailure("Account data export exceeds the configured email attachment limit.")

        archive_bytes = archive_path.read_bytes()

    return {
        "archive_name": archive_name,
        "archive_bytes": archive_bytes,
        "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "size_bytes": len(archive_bytes),
    }


def _serialize_case_row(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "caseId": str(row["case_id"]),
        "createdAt": str(row["created_at"]),
        "updatedAt": str(row["updated_at"]),
        "status": str(row["status"]),
        "label": str(row["label"] or ""),
        "outcome": str(row["outcome"] or ""),
        "tagIds": _json_load_list(str(row["tag_ids_json"] or "[]")),
        "payload": _json_load_object(str(row["payload_json"] or "{}")),
        "sourceUploadId": int(row["source_upload_id"]) if row["source_upload_id"] is not None else None,
    }


def _email_delivery_available(settings: TrainingHubSettings) -> bool:
    return bool(settings.smtp_host.strip() and settings.smtp_from_email.strip())


def _parse_utc_iso(value: str) -> datetime:
    normalized = (value or "").strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json_load_list(value: str) -> list[Any]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    return decoded if isinstance(decoded, list) else []


def _json_load_object(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _archive_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(max(1, int(count))))


def _path_is_safe(base_dir: Path, candidate: Path) -> bool:
    base_resolved = base_dir.resolve(strict=False)
    candidate_resolved = candidate.resolve(strict=False)
    try:
        candidate_resolved.relative_to(base_resolved)
        return True
    except ValueError:
        return False


class _DataExportFailure(RuntimeError):
    pass
