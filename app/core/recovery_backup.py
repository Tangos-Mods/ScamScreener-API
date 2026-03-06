from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import shutil
import tarfile
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..infra import db as sqlite3
from ..config.settings import TrainingHubSettings
from .common import _is_path_within, _normalize_user_agent_for_binding, _now_utc_iso
from .session_auth import _hash_password, _validate_password


BACKUP_FORMAT_VERSION = 1
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
_BACKUP_LOCK = threading.Lock()

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
                json.dumps(export_payload, ensure_ascii=True, separators=(",", ":")),
                encoding="utf-8",
            )

            shutil.copytree(settings.uploads_dir, payload_dir / "uploads", dirs_exist_ok=True)
            shutil.copytree(settings.bundles_dir, payload_dir / "bundles", dirs_exist_ok=True)

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
            export_payload = json.loads(export_file.read_text(encoding="utf-8"))
            if str(export_payload.get("format", "")) != "scamscreener_training_hub_backup":
                raise ValueError("Backup archive format is invalid.")
            if int(export_payload.get("version", 0)) != BACKUP_FORMAT_VERSION:
                raise ValueError("Backup archive version is unsupported.")
            tables_payload = export_payload.get("tables")
            if not isinstance(tables_payload, dict):
                raise ValueError("Backup archive table payload is invalid.")

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
                        column_sql = ", ".join(columns)
                        placeholder_sql = ", ".join("?" for _ in columns)
                        insert_sql = f"INSERT INTO {table} ({column_sql}) VALUES ({placeholder_sql})"
                        for row in rows:
                            if not isinstance(row, dict):
                                raise ValueError(f"Backup row payload is invalid: {table}")
                            values = tuple(row.get(column) for column in columns)
                            connection.execute(insert_sql, values)
                            inserted += 1
                    restored_counts[table] = inserted
                connection.commit()

            source_root = export_file.parent
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

