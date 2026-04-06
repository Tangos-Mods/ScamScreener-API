from __future__ import annotations

from pathlib import Path

from ..infra import db as sqlite3
from .storage_migrations import (
    _migrate_admin_mfa_challenge_columns,
    _migrate_audit_log_columns,
    _migrate_password_reset_token_columns,
    _migrate_training_cases_payload_json,
    _migrate_uploads_security_columns,
    _migrate_users_security_columns,
)


def _init_database_sqlite(database_path: Path | str) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                username TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                last_login_at TEXT,
                failed_login_attempts INTEGER NOT NULL DEFAULT 0,
                lockout_until TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                token_sha256 TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                remote_addr TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT '',
                revoke_reason TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                original_file_name TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                case_count INTEGER NOT NULL,
                size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL,
                duplicate_of_upload_id INTEGER,
                source_ip TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (duplicate_of_upload_id) REFERENCES uploads(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS training_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                started_by_user_id INTEGER NOT NULL,
                upload_count INTEGER NOT NULL,
                case_count INTEGER NOT NULL,
                status TEXT NOT NULL,
                command TEXT NOT NULL,
                bundle_path TEXT NOT NULL,
                output_log TEXT NOT NULL,
                FOREIGN KEY (started_by_user_id) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS training_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                created_by_user_id INTEGER NOT NULL,
                source_upload_id INTEGER,
                status TEXT NOT NULL DEFAULT 'submitted',
                label TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL DEFAULT '',
                tag_ids_json TEXT NOT NULL DEFAULT '[]',
                payload_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (created_by_user_id) REFERENCES users(id),
                FOREIGN KEY (source_upload_id) REFERENCES uploads(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS upload_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                upload_id INTEGER NOT NULL,
                case_id TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL DEFAULT '',
                tag_ids_json TEXT NOT NULL DEFAULT '[]',
                payload_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (upload_id) REFERENCES uploads(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                actor_user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                target_type TEXT NOT NULL DEFAULT '',
                target_id INTEGER,
                details TEXT NOT NULL DEFAULT '',
                source_ip TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (actor_user_id) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                token_sha256 TEXT NOT NULL UNIQUE,
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                source_ip TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_mfa_challenges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                token_sha256 TEXT NOT NULL UNIQUE,
                code_sha256 TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                consumed_at TEXT,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                source_ip TEXT NOT NULL DEFAULT '',
                user_agent TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rate_limit_hits (
                bucket_key TEXT NOT NULL,
                bucket_start INTEGER NOT NULL,
                count INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (bucket_key, bucket_start)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS data_export_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                requested_email TEXT NOT NULL,
                status TEXT NOT NULL,
                requested_from_ip TEXT NOT NULL DEFAULT '',
                request_user_agent TEXT NOT NULL DEFAULT '',
                completed_at TEXT,
                failed_at TEXT,
                delivery_error TEXT NOT NULL DEFAULT '',
                archive_sha256 TEXT NOT NULL DEFAULT '',
                size_bytes INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        _migrate_users_security_columns(connection)
        _migrate_uploads_security_columns(connection)
        _migrate_audit_log_columns(connection)
        _migrate_password_reset_token_columns(connection)
        _migrate_admin_mfa_challenge_columns(connection)
        _migrate_training_cases_payload_json(connection)
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_uploads_user_sha ON uploads(user_id, payload_sha256)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_uploads_created_at ON uploads(created_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_training_runs_created_at ON training_runs(created_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_training_cases_status ON training_cases(status)")
        connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_upload_cases_upload_case ON upload_cases(upload_id, case_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_upload_cases_case_id ON upload_cases(case_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_uploads_source_ip ON uploads(source_ip)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_actor ON audit_logs(actor_user_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_password_reset_user ON password_reset_tokens(user_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_password_reset_expires ON password_reset_tokens(expires_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_admin_mfa_user ON admin_mfa_challenges(user_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_admin_mfa_expires ON admin_mfa_challenges(expires_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_data_export_requests_user ON data_export_requests(user_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_data_export_requests_status ON data_export_requests(status)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_data_export_requests_created_at ON data_export_requests(created_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_rate_limit_updated_at ON rate_limit_hits(updated_at)")
        connection.commit()

