from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import tarfile
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config.settings import TrainingHubSettings
from ..infra import db as sqlite3
from .common import _is_path_within, _now_utc_iso


BACKUP_FORMAT_VERSION = 1
BACKUP_MANIFEST_FORMAT = "scamscreener_training_hub_backup_manifest"
BACKUP_TABLE_ORDER = [
    "users",
    "sessions",
    "uploads",
    "training_runs",
    "training_cases",
    "audit_logs",
    "password_reset_tokens",
    "admin_mfa_challenges",
    "rate_limit_hits",
]
BACKUP_TABLE_COLUMNS: dict[str, set[str]] = {
    "users": {
        "id",
        "created_at",
        "username",
        "email",
        "password_hash",
        "is_admin",
        "last_login_at",
        "failed_login_attempts",
        "lockout_until",
    },
    "sessions": {
        "id",
        "created_at",
        "user_id",
        "token_sha256",
        "expires_at",
        "revoked_at",
        "remote_addr",
        "user_agent",
        "revoke_reason",
    },
    "uploads": {
        "id",
        "created_at",
        "user_id",
        "original_file_name",
        "stored_path",
        "payload_sha256",
        "case_count",
        "size_bytes",
        "status",
        "duplicate_of_upload_id",
        "source_ip",
    },
    "training_runs": {
        "id",
        "created_at",
        "started_by_user_id",
        "upload_count",
        "case_count",
        "status",
        "command",
        "bundle_path",
        "output_log",
    },
    "training_cases": {
        "id",
        "case_id",
        "created_at",
        "updated_at",
        "created_by_user_id",
        "source_upload_id",
        "status",
        "label",
        "outcome",
        "tag_ids_json",
        "payload_json",
    },
    "audit_logs": {
        "id",
        "created_at",
        "actor_user_id",
        "action",
        "target_type",
        "target_id",
        "details",
        "source_ip",
        "user_agent",
    },
    "password_reset_tokens": {
        "id",
        "created_at",
        "user_id",
        "token_sha256",
        "expires_at",
        "consumed_at",
        "source_ip",
        "user_agent",
    },
    "admin_mfa_challenges": {
        "id",
        "created_at",
        "user_id",
        "token_sha256",
        "code_sha256",
        "expires_at",
        "consumed_at",
        "failed_attempts",
        "source_ip",
        "user_agent",
    },
    "rate_limit_hits": {
        "bucket_key",
        "bucket_start",
        "count",
        "updated_at",
    },
}
_BACKUP_LOCK = threading.Lock()


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sha256_file(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _build_manifest_payload(payload_root: Path, created_at: str) -> dict[str, Any]:
    files: dict[str, str] = {}
    for file_path in sorted(path for path in payload_root.rglob("*") if path.is_file() and path.name != "manifest.json"):
        relative_path = file_path.relative_to(payload_root).as_posix()
        files[relative_path] = _sha256_file(file_path)
    return {
        "format": BACKUP_MANIFEST_FORMAT,
        "version": BACKUP_FORMAT_VERSION,
        "created_at": created_at,
        "files": files,
    }


def _manifest_signature(manifest_payload: dict[str, Any], secret_key: str) -> str:
    return hmac.new(
        (secret_key or "").encode("utf-8"),
        _canonical_json_bytes(manifest_payload),
        hashlib.sha256,
    ).hexdigest()


def _validate_manifest(source_root: Path, manifest: dict[str, Any], secret_key: str) -> None:
    signature = str(manifest.get("signature_sha256", "")).strip()
    if not signature:
        raise ValueError("Backup manifest signature is missing.")

    signed_payload = dict(manifest)
    signed_payload.pop("signature_sha256", None)
    if str(signed_payload.get("format", "")) != BACKUP_MANIFEST_FORMAT:
        raise ValueError("Backup manifest format is invalid.")
    if int(signed_payload.get("version", 0)) != BACKUP_FORMAT_VERSION:
        raise ValueError("Backup manifest version is unsupported.")

    expected_signature = _manifest_signature(signed_payload, secret_key)
    if not hmac.compare_digest(signature, expected_signature):
        raise ValueError("Backup manifest signature is invalid.")

    file_hashes = signed_payload.get("files")
    if not isinstance(file_hashes, dict):
        raise ValueError("Backup manifest file list is invalid.")

    actual_files = {
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    expected_files = set(file_hashes.keys())
    if actual_files != expected_files:
        raise ValueError("Backup manifest does not match extracted files.")

    for relative_path, expected_hash in file_hashes.items():
        relative_text = str(relative_path or "").strip()
        if not relative_text:
            raise ValueError("Backup manifest contains invalid file path.")
        relative = Path(relative_text)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Backup manifest contains unsafe file path.")

        file_path = source_root / relative
        if not _is_path_within(source_root, file_path) or not file_path.exists() or not file_path.is_file():
            raise ValueError("Backup manifest references missing file.")

        expected_hash_text = str(expected_hash or "").strip().lower()
        if len(expected_hash_text) != 64 or any(char not in "0123456789abcdef" for char in expected_hash_text):
            raise ValueError("Backup manifest contains invalid file hash.")

        actual_hash = _sha256_file(file_path)
        if not hmac.compare_digest(actual_hash, expected_hash_text):
            raise ValueError("Backup file integrity check failed.")


def _create_backup_archive(settings: TrainingHubSettings) -> dict[str, Any]:
    backup_created_at = _now_utc_iso()
    backup_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_name = f"training-hub-backup-{backup_stamp}.tar.gz"
    backup_path = settings.backups_dir / backup_name

    with _BACKUP_LOCK:
        with tempfile.TemporaryDirectory(prefix="backup-build-", dir=str(settings.backups_dir)) as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            payload_dir = temp_dir / "backup"
            payload_dir.mkdir(parents=True, exist_ok=True)

            export_payload: dict[str, Any] = {
                "format": "scamscreener_training_hub_backup",
                "version": BACKUP_FORMAT_VERSION,
                "created_at": backup_created_at,
                "tables": {},
            }
            row_counts: dict[str, int] = {}

            with sqlite3.connect(settings.database_path) as connection:
                connection.row_factory = sqlite3.Row
                for table in BACKUP_TABLE_ORDER:
                    rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table}").fetchall()]
                    export_payload["tables"][table] = rows
                    row_counts[table] = len(rows)

            (payload_dir / "database_export.json").write_text(
                json.dumps(export_payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
                encoding="utf-8",
            )
            shutil.copytree(settings.uploads_dir, payload_dir / "uploads", dirs_exist_ok=True)
            shutil.copytree(settings.bundles_dir, payload_dir / "bundles", dirs_exist_ok=True)

            manifest_payload = _build_manifest_payload(payload_dir, backup_created_at)
            manifest = dict(manifest_payload)
            manifest["signature_sha256"] = _manifest_signature(manifest_payload, settings.secret_key)
            (payload_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=True, separators=(",", ":"), sort_keys=True),
                encoding="utf-8",
            )

            with tarfile.open(backup_path, mode="w:gz") as archive:
                archive.add(payload_dir, arcname="backup")

    return {
        "backup_path": str(backup_path),
        "backup_name": backup_name,
        "created_at": backup_created_at,
        "row_counts": row_counts,
        "size_bytes": int(backup_path.stat().st_size) if backup_path.exists() else 0,
    }


def _restore_backup_archive(settings: TrainingHubSettings, archive_path: Path) -> dict[str, Any]:
    if not archive_path.exists():
        raise FileNotFoundError("Backup archive was not found.")

    with _BACKUP_LOCK:
        with tempfile.TemporaryDirectory(prefix="backup-restore-", dir=str(settings.backups_dir)) as temp_dir_text:
            temp_dir = Path(temp_dir_text)
            with tarfile.open(archive_path, mode="r:*") as archive:
                members = archive.getmembers()
                for member in members:
                    member_path = Path(member.name)
                    if member.issym() or member.islnk():
                        raise ValueError("Backup archive contains unsupported link entries.")
                    if member_path.is_absolute() or ".." in member_path.parts:
                        raise ValueError("Backup archive contains unsafe paths.")
                archive.extractall(path=temp_dir, filter="data")

            export_file = next(temp_dir.rglob("database_export.json"), None)
            if export_file is None:
                raise ValueError("Backup archive is missing database_export.json.")
            source_root = export_file.parent
            manifest_file = source_root / "manifest.json"
            if not manifest_file.exists():
                raise ValueError("Backup archive is missing manifest.json.")

            manifest_payload = json.loads(manifest_file.read_text(encoding="utf-8"))
            if not isinstance(manifest_payload, dict):
                raise ValueError("Backup manifest is invalid.")
            _validate_manifest(source_root, manifest_payload, settings.secret_key)

            export_payload = json.loads(export_file.read_text(encoding="utf-8"))
            if str(export_payload.get("format", "")) != "scamscreener_training_hub_backup":
                raise ValueError("Backup archive format is invalid.")
            if int(export_payload.get("version", 0)) != BACKUP_FORMAT_VERSION:
                raise ValueError("Backup archive version is unsupported.")
            tables_payload = export_payload.get("tables")
            if not isinstance(tables_payload, dict):
                raise ValueError("Backup archive table payload is invalid.")

            unexpected_tables = {str(name) for name in tables_payload.keys()} - set(BACKUP_TABLE_ORDER)
            if unexpected_tables:
                raise ValueError("Backup archive contains unexpected tables.")

            restored_counts: dict[str, int] = {}
            with sqlite3.connect(settings.database_path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                for table in reversed(BACKUP_TABLE_ORDER):
                    connection.execute(f"DELETE FROM {table}")

                for table in BACKUP_TABLE_ORDER:
                    rows = tables_payload.get(table, [])
                    if not isinstance(rows, list):
                        raise ValueError(f"Backup table payload is invalid: {table}")

                    inserted = 0
                    if rows:
                        first_row = rows[0]
                        if not isinstance(first_row, dict):
                            raise ValueError(f"Backup row payload is invalid: {table}")

                        columns = list(first_row.keys())
                        allowed_columns = BACKUP_TABLE_COLUMNS[table]
                        if not columns or len(columns) != len(set(columns)):
                            raise ValueError(f"Backup row payload is invalid: {table}")
                        if any(column not in allowed_columns for column in columns):
                            raise ValueError(f"Backup row payload has unsupported columns: {table}")

                        column_set = set(columns)
                        column_sql = ", ".join(columns)
                        placeholder_sql = ", ".join("?" for _ in columns)
                        insert_sql = f"INSERT INTO {table} ({column_sql}) VALUES ({placeholder_sql})"

                        for row in rows:
                            if not isinstance(row, dict):
                                raise ValueError(f"Backup row payload is invalid: {table}")
                            if set(row.keys()) != column_set:
                                raise ValueError(f"Backup row payload has inconsistent columns: {table}")
                            values = tuple(row[column] for column in columns)
                            connection.execute(insert_sql, values)
                            inserted += 1
                    restored_counts[table] = inserted
                connection.commit()

            staged_uploads = temp_dir / "restore-uploads"
            staged_bundles = temp_dir / "restore-bundles"
            source_uploads = source_root / "uploads"
            source_bundles = source_root / "bundles"
            staged_uploads.mkdir(parents=True, exist_ok=True)
            staged_bundles.mkdir(parents=True, exist_ok=True)
            if source_uploads.exists():
                shutil.copytree(source_uploads, staged_uploads, dirs_exist_ok=True)
            if source_bundles.exists():
                shutil.copytree(source_bundles, staged_bundles, dirs_exist_ok=True)

            if settings.uploads_dir.exists():
                shutil.rmtree(settings.uploads_dir)
            if settings.bundles_dir.exists():
                shutil.rmtree(settings.bundles_dir)
            shutil.move(str(staged_uploads), str(settings.uploads_dir))
            shutil.move(str(staged_bundles), str(settings.bundles_dir))

    return {
        "restored_at": _now_utc_iso(),
        "row_counts": restored_counts,
    }
