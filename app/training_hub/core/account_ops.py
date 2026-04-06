from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

from ..config.settings import TrainingHubSettings
from ..infra import db as sqlite3
from .common import _now_utc_iso
from .session_auth_password import _verify_password
from .training_data import _parse_training_cases, _upsert_upload_case_entries

logger = logging.getLogger(__name__)


def _verify_user_action_password(database_path: Path | str, user_id: int, current_password: str) -> dict[str, Any]:
    normalized_password = current_password or ""
    if not normalized_password:
        return {"ok": False, "error": "Current password is required.", "status_code": 400}

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id, username, email, is_admin, password_hash FROM users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
    if row is None:
        return {"ok": False, "error": "Account not found.", "status_code": 404}
    if not _verify_password(normalized_password, str(row["password_hash"] or "")):
        return {"ok": False, "error": "Current password is incorrect.", "status_code": 401}
    return {
        "ok": True,
        "user": {
            "id": int(row["id"]),
            "username": str(row["username"]),
            "email": str(row["email"]),
            "is_admin": int(row["is_admin"]),
        },
    }


def _delete_user_upload(settings: TrainingHubSettings, user_id: int, upload_id: int) -> dict[str, Any]:
    _backfill_upload_case_index(settings)
    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        target_rows = connection.execute(
            """
            SELECT id, user_id, stored_path, payload_sha256, case_count
            FROM uploads
            WHERE id = ? AND user_id = ?
            """,
            (int(upload_id), int(user_id)),
        ).fetchall()
        if not target_rows:
            return {"ok": False, "error": "Upload not found.", "status_code": 404}

        summary = _purge_upload_rows_in_connection(connection, settings, int(user_id), list(target_rows))
        connection.commit()

    _delete_paths(summary["cleanup_paths"])
    return {
        "ok": True,
        "upload_id": int(upload_id),
        "deleted_uploads": int(summary["deleted_uploads"]),
        "deleted_cases": int(summary["deleted_cases"]),
        "rebuilt_cases": int(summary["rebuilt_cases"]),
    }


def _purge_user_uploads(settings: TrainingHubSettings, user_id: int) -> dict[str, Any]:
    _backfill_upload_case_index(settings)
    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        upload_rows = connection.execute(
            """
            SELECT id, user_id, stored_path, payload_sha256, case_count
            FROM uploads
            WHERE user_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (int(user_id),),
        ).fetchall()
        summary = _purge_upload_rows_in_connection(connection, settings, int(user_id), list(upload_rows))
        connection.commit()

    _delete_paths(summary["cleanup_paths"])
    return {
        "ok": True,
        "deleted_uploads": int(summary["deleted_uploads"]),
        "deleted_cases": int(summary["deleted_cases"]),
        "rebuilt_cases": int(summary["rebuilt_cases"]),
    }


def _delete_user_account(settings: TrainingHubSettings, user_id: int) -> dict[str, Any]:
    _backfill_upload_case_index(settings)
    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        user_row = connection.execute(
            "SELECT id, username, email, is_admin FROM users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
        if user_row is None:
            return {"ok": False, "error": "Account not found.", "status_code": 404}

        if int(user_row["is_admin"]) == 1:
            admin_count_row = connection.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()
            admin_count = int(admin_count_row[0] if admin_count_row is not None else 0)
            if admin_count <= 1:
                return {
                    "ok": False,
                    "error": "Deleting the last remaining admin account is disabled.",
                    "status_code": 400,
                }

        pending_export = connection.execute(
            """
            SELECT id
            FROM data_export_requests
            WHERE user_id = ? AND status IN ('pending', 'processing')
            LIMIT 1
            """,
            (int(user_id),),
        ).fetchone()
        if pending_export is not None:
            return {
                "ok": False,
                "error": "Wait until your pending data export request finishes before deleting the account.",
                "status_code": 409,
            }

        upload_rows = connection.execute(
            """
            SELECT id, user_id, stored_path, payload_sha256, case_count
            FROM uploads
            WHERE user_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (int(user_id),),
        ).fetchall()
        purge_summary = _purge_upload_rows_in_connection(connection, settings, int(user_id), list(upload_rows))

        session_rows = connection.execute("SELECT id FROM sessions WHERE user_id = ?", (int(user_id),)).fetchall()
        session_ids = [int(row["id"]) for row in session_rows]

        run_rows = connection.execute(
            "SELECT id, bundle_path FROM training_runs WHERE started_by_user_id = ?",
            (int(user_id),),
        ).fetchall()
        run_ids = [int(row["id"]) for row in run_rows]
        bundle_cleanup_paths = _collect_unreferenced_bundle_paths(settings, list(run_rows))

        export_rows = connection.execute(
            "SELECT id FROM data_export_requests WHERE user_id = ?",
            (int(user_id),),
        ).fetchall()
        export_request_ids = [int(row["id"]) for row in export_rows]

        if run_ids:
            _delete_rows_for_ids(connection, "training_runs", "id", run_ids)
        if session_ids:
            _delete_rows_for_ids(connection, "sessions", "id", session_ids)
        connection.execute("DELETE FROM password_reset_tokens WHERE user_id = ?", (int(user_id),))
        connection.execute("DELETE FROM admin_mfa_challenges WHERE user_id = ?", (int(user_id),))
        if export_request_ids:
            _delete_rows_for_ids(connection, "data_export_requests", "id", export_request_ids)

        _delete_audit_logs_for_user_resources(
            connection,
            int(user_id),
            purge_summary["deleted_upload_ids"],
            session_ids,
            run_ids,
            export_request_ids,
        )

        connection.execute("DELETE FROM users WHERE id = ?", (int(user_id),))
        connection.commit()

    _delete_paths(purge_summary["cleanup_paths"] + bundle_cleanup_paths)
    logger.info(
        "Deleted user account user_id=%s uploads=%s deleted_cases=%s rebuilt_cases=%s.",
        int(user_id),
        purge_summary["deleted_uploads"],
        purge_summary["deleted_cases"],
        purge_summary["rebuilt_cases"],
    )
    return {
        "ok": True,
        "username": str(user_row["username"]),
        "deleted_uploads": int(purge_summary["deleted_uploads"]),
        "deleted_cases": int(purge_summary["deleted_cases"]),
        "rebuilt_cases": int(purge_summary["rebuilt_cases"]),
    }


def _backfill_upload_case_index(settings: TrainingHubSettings) -> dict[str, int]:
    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        upload_rows = connection.execute(
            """
            SELECT up.id, up.stored_path
            FROM uploads up
            LEFT JOIN upload_cases uc ON uc.upload_id = up.id
            GROUP BY up.id, up.stored_path
            HAVING COUNT(uc.id) = 0
            ORDER BY up.created_at ASC, up.id ASC
            """
        ).fetchall()

        indexed = 0
        skipped = 0
        failed = 0
        for row in upload_rows:
            upload_path = Path(str(row["stored_path"] or ""))
            if not _path_is_safe(settings.uploads_dir, upload_path) or not upload_path.exists():
                skipped += 1
                continue

            try:
                payload_text = upload_path.read_text(encoding="utf-8")
                parsed_cases = _parse_training_cases(payload_text)
                _upsert_upload_case_entries(connection, int(row["id"]), parsed_cases)
                indexed += 1
            except Exception:
                failed += 1
                logger.exception("Upload-case index backfill failed for upload_id=%s.", int(row["id"]))
        if indexed:
            connection.commit()
    return {"indexed": indexed, "skipped": skipped, "failed": failed}


def _purge_upload_rows_in_connection(
    connection,
    settings: TrainingHubSettings,
    user_id: int,
    upload_rows: list[Any],
) -> dict[str, Any]:
    if not upload_rows:
        affected_case_ids = _case_ids_created_by_user(connection, int(user_id))
        rebuild_summary = _rebuild_training_cases(connection, affected_case_ids)
        return {
            "deleted_uploads": 0,
            "deleted_cases": int(rebuild_summary["deleted"]),
            "rebuilt_cases": int(rebuild_summary["inserted"] + rebuild_summary["updated"]),
            "deleted_upload_ids": [],
            "cleanup_paths": [],
        }

    target_upload_ids = [int(row["id"]) for row in upload_rows]
    affected_case_ids = _collect_affected_case_ids(connection, int(user_id), target_upload_ids)

    _null_training_case_sources(connection, target_upload_ids)
    _repoint_duplicate_upload_references(connection, upload_rows, set(target_upload_ids))
    _delete_rows_for_ids(connection, "upload_cases", "upload_id", target_upload_ids)
    _delete_rows_for_ids(connection, "uploads", "id", target_upload_ids)

    rebuild_summary = _rebuild_training_cases(connection, affected_case_ids)
    cleanup_paths = _collect_unreferenced_upload_paths(connection, settings, upload_rows)
    return {
        "deleted_uploads": len(target_upload_ids),
        "deleted_cases": int(rebuild_summary["deleted"]),
        "rebuilt_cases": int(rebuild_summary["inserted"] + rebuild_summary["updated"]),
        "deleted_upload_ids": target_upload_ids,
        "cleanup_paths": cleanup_paths,
    }


def _case_ids_created_by_user(connection, user_id: int) -> set[str]:
    rows = connection.execute(
        "SELECT DISTINCT case_id FROM training_cases WHERE created_by_user_id = ?",
        (int(user_id),),
    ).fetchall()
    return {str(row["case_id"] or "").strip() for row in rows if str(row["case_id"] or "").strip()}


def _collect_affected_case_ids(connection, user_id: int, upload_ids: list[int]) -> set[str]:
    case_ids: set[str] = set()
    for batch in _batched(upload_ids, 200):
        case_rows = connection.execute(
            f"SELECT DISTINCT case_id FROM upload_cases WHERE upload_id IN ({_placeholders(len(batch))})",
            tuple(batch),
        ).fetchall()
        for row in case_rows:
            case_id = str(row["case_id"] or "").strip()
            if case_id:
                case_ids.add(case_id)

        training_rows = connection.execute(
            (
                "SELECT DISTINCT case_id FROM training_cases "
                f"WHERE created_by_user_id = ? OR source_upload_id IN ({_placeholders(len(batch))})"
            ),
            (int(user_id), *batch),
        ).fetchall()
        for row in training_rows:
            case_id = str(row["case_id"] or "").strip()
            if case_id:
                case_ids.add(case_id)
    return case_ids


def _null_training_case_sources(connection, upload_ids: list[int]) -> None:
    for batch in _batched(upload_ids, 200):
        connection.execute(
            f"UPDATE training_cases SET source_upload_id = NULL WHERE source_upload_id IN ({_placeholders(len(batch))})",
            tuple(batch),
        )


def _repoint_duplicate_upload_references(connection, upload_rows: list[Any], excluded_upload_ids: set[int]) -> None:
    excluded = sorted(excluded_upload_ids)
    excluded_sql = _placeholders(len(excluded))
    replacements: dict[str, int | None] = {}

    for row in upload_rows:
        payload_sha = str(row["payload_sha256"] or "")
        if payload_sha in replacements:
            replacement_id = replacements[payload_sha]
        elif excluded:
            replacement_row = connection.execute(
                (
                    "SELECT id FROM uploads "
                    f"WHERE payload_sha256 = ? AND id NOT IN ({excluded_sql}) "
                    "ORDER BY id ASC LIMIT 1"
                ),
                (payload_sha, *excluded),
            ).fetchone()
            replacement_id = int(replacement_row["id"]) if replacement_row is not None else None
            replacements[payload_sha] = replacement_id
        else:
            replacement_id = None

        connection.execute(
            "UPDATE uploads SET duplicate_of_upload_id = ? WHERE duplicate_of_upload_id = ?",
            (replacement_id, int(row["id"])),
        )


def _rebuild_training_cases(connection, affected_case_ids: set[str]) -> dict[str, int]:
    if not affected_case_ids:
        return {"inserted": 0, "updated": 0, "deleted": 0}

    now = _now_utc_iso()
    inserted = 0
    updated = 0
    deleted = 0
    case_versions = _remaining_case_versions(connection, affected_case_ids)

    for case_id in sorted(affected_case_ids):
        version = case_versions.get(case_id)
        if version is None:
            cursor = connection.execute("DELETE FROM training_cases WHERE case_id = ?", (case_id,))
            deleted += int(cursor.rowcount or 0)
            continue

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
                    str(version["first_created_at"]),
                    now,
                    int(version["first_user_id"]),
                    int(version["latest_upload_id"]),
                    "submitted",
                    str(version["latest_label"]),
                    str(version["latest_outcome"]),
                    str(version["latest_tags_json"]),
                    str(version["latest_payload_json"]),
                ),
            )
            inserted += 1
            continue

        connection.execute(
            """
            UPDATE training_cases
            SET created_at = ?, updated_at = ?, created_by_user_id = ?, source_upload_id = ?,
                status = ?, label = ?, outcome = ?, tag_ids_json = ?, payload_json = ?
            WHERE case_id = ?
            """,
            (
                str(version["first_created_at"]),
                now,
                int(version["first_user_id"]),
                int(version["latest_upload_id"]),
                "submitted",
                str(version["latest_label"]),
                str(version["latest_outcome"]),
                str(version["latest_tags_json"]),
                str(version["latest_payload_json"]),
                case_id,
            ),
        )
        updated += 1
    return {"inserted": inserted, "updated": updated, "deleted": deleted}


def _remaining_case_versions(connection, affected_case_ids: set[str]) -> dict[str, dict[str, Any]]:
    versions: dict[str, dict[str, Any]] = {}
    for batch in _batched(sorted(affected_case_ids), 200):
        rows = connection.execute(
            """
            SELECT
                uc.case_id,
                uc.label,
                uc.outcome,
                uc.tag_ids_json,
                uc.payload_json,
                up.id AS upload_id,
                up.created_at AS upload_created_at,
                up.user_id AS upload_user_id
            FROM upload_cases uc
            JOIN uploads up ON up.id = uc.upload_id
            """
            + f" WHERE uc.case_id IN ({_placeholders(len(batch))})"
            + " ORDER BY uc.case_id ASC, up.created_at ASC, up.id ASC",
            tuple(batch),
        ).fetchall()
        for row in rows:
            case_id = str(row["case_id"])
            payload = {
                "label": str(row["label"] or ""),
                "outcome": str(row["outcome"] or ""),
                "tags_json": str(row["tag_ids_json"] or "[]"),
                "payload_json": str(row["payload_json"] or "{}"),
                "upload_id": int(row["upload_id"]),
                "upload_created_at": str(row["upload_created_at"]),
                "upload_user_id": int(row["upload_user_id"]),
            }
            current = versions.get(case_id)
            if current is None:
                current = {
                    "first_created_at": payload["upload_created_at"],
                    "first_user_id": payload["upload_user_id"],
                    "latest_upload_id": payload["upload_id"],
                    "latest_label": payload["label"],
                    "latest_outcome": payload["outcome"],
                    "latest_tags_json": payload["tags_json"],
                    "latest_payload_json": payload["payload_json"],
                }
                versions[case_id] = current
                continue

            current["latest_upload_id"] = payload["upload_id"]
            current["latest_label"] = payload["label"]
            current["latest_outcome"] = payload["outcome"]
            current["latest_tags_json"] = payload["tags_json"]
            current["latest_payload_json"] = payload["payload_json"]
    return versions


def _collect_unreferenced_upload_paths(connection, settings: TrainingHubSettings, upload_rows: list[Any]) -> list[Path]:
    cleanup_paths: list[Path] = []
    seen: set[str] = set()
    for row in upload_rows:
        path_text = str(row["stored_path"] or "")
        if not path_text or path_text in seen:
            continue
        seen.add(path_text)
        remaining = connection.execute("SELECT COUNT(*) FROM uploads WHERE stored_path = ?", (path_text,)).fetchone()
        remaining_count = int(remaining[0] if remaining is not None else 0)
        file_path = Path(path_text)
        if remaining_count == 0 and _path_is_safe(settings.uploads_dir, file_path):
            cleanup_paths.append(file_path)
    return cleanup_paths


def _collect_unreferenced_bundle_paths(settings: TrainingHubSettings, run_rows: list[Any]) -> list[Path]:
    cleanup_paths: list[Path] = []
    for row in run_rows:
        path_text = str(row["bundle_path"] or "")
        if not path_text:
            continue
        file_path = Path(path_text)
        if _path_is_safe(settings.bundles_dir, file_path):
            cleanup_paths.append(file_path)
    return cleanup_paths


def _delete_audit_logs_for_user_resources(
    connection,
    user_id: int,
    upload_ids: list[int],
    session_ids: list[int],
    run_ids: list[int],
    export_request_ids: list[int],
) -> None:
    connection.execute(
        "DELETE FROM audit_logs WHERE actor_user_id = ? OR (target_type = 'user' AND target_id = ?)",
        (int(user_id), int(user_id)),
    )
    if upload_ids:
        _delete_audit_logs_for_targets(connection, "upload", upload_ids)
    if session_ids:
        _delete_audit_logs_for_targets(connection, "session", session_ids)
    if run_ids:
        _delete_audit_logs_for_targets(connection, "training_run", run_ids)
    if export_request_ids:
        _delete_audit_logs_for_targets(connection, "data_export_request", export_request_ids)


def _delete_audit_logs_for_targets(connection, target_type: str, target_ids: list[int]) -> None:
    for batch in _batched(target_ids, 200):
        connection.execute(
            (
                "DELETE FROM audit_logs "
                f"WHERE target_type = ? AND target_id IN ({_placeholders(len(batch))})"
            ),
            (str(target_type), *batch),
        )


def _delete_rows_for_ids(connection, table_name: str, column_name: str, ids: list[int]) -> None:
    for batch in _batched(ids, 200):
        connection.execute(
            f"DELETE FROM {table_name} WHERE {column_name} IN ({_placeholders(len(batch))})",
            tuple(batch),
        )


def _delete_paths(paths: list[Path]) -> None:
    for file_path in paths:
        try:
            if file_path.exists():
                file_path.unlink()
        except OSError:
            logger.exception("Failed to delete file path=%s during account cleanup.", file_path)


def _path_is_safe(base_dir: Path, candidate: Path) -> bool:
    base_resolved = base_dir.resolve(strict=False)
    candidate_resolved = candidate.resolve(strict=False)
    try:
        candidate_resolved.relative_to(base_resolved)
        return True
    except ValueError:
        return False


def _placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(max(1, int(count))))


def _batched(values: Iterable[int | str], batch_size: int) -> list[list[int | str]]:
    batch: list[int | str] = []
    batches: list[list[int | str]] = []
    for value in values:
        batch.append(value)
        if len(batch) >= batch_size:
            batches.append(batch)
            batch = []
    if batch:
        batches.append(batch)
    return batches
