from __future__ import annotations

from pathlib import Path

from ..infra import db as sqlite3
from .storage_fs import _ensure_storage
from .storage_migrations import (
    _migrate_admin_mfa_challenge_columns,
    _migrate_audit_log_columns,
    _migrate_password_reset_token_columns,
    _migrate_training_cases_payload_json,
    _migrate_uploads_security_columns,
    _migrate_users_security_columns,
)
from .storage_schema_mariadb import _init_database_mariadb
from .storage_schema_sqlite import _init_database_sqlite


def _init_database(database_path: Path | str) -> None:
    if sqlite3.is_mariadb_target(database_path):
        _init_database_mariadb(database_path)
        return
    _init_database_sqlite(database_path)

