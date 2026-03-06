from __future__ import annotations

from ..infra import db as sqlite3


def _migrate_training_cases_payload_json(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1]).strip().lower()
        for row in connection.execute("PRAGMA table_info(training_cases)").fetchall()
    }
    if "payload_json" not in columns:
        connection.execute("ALTER TABLE training_cases ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'")


def _migrate_users_security_columns(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1]).strip().lower()
        for row in connection.execute("PRAGMA table_info(users)").fetchall()
    }
    if "failed_login_attempts" not in columns:
        connection.execute("ALTER TABLE users ADD COLUMN failed_login_attempts INTEGER NOT NULL DEFAULT 0")
    if "lockout_until" not in columns:
        connection.execute("ALTER TABLE users ADD COLUMN lockout_until TEXT")


def _migrate_uploads_security_columns(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1]).strip().lower()
        for row in connection.execute("PRAGMA table_info(uploads)").fetchall()
    }
    if "source_ip" not in columns:
        connection.execute("ALTER TABLE uploads ADD COLUMN source_ip TEXT NOT NULL DEFAULT ''")


def _migrate_audit_log_columns(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1]).strip().lower()
        for row in connection.execute("PRAGMA table_info(audit_logs)").fetchall()
    }
    if "source_ip" not in columns:
        connection.execute("ALTER TABLE audit_logs ADD COLUMN source_ip TEXT NOT NULL DEFAULT ''")
    if "user_agent" not in columns:
        connection.execute("ALTER TABLE audit_logs ADD COLUMN user_agent TEXT NOT NULL DEFAULT ''")


def _migrate_password_reset_token_columns(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1]).strip().lower()
        for row in connection.execute("PRAGMA table_info(password_reset_tokens)").fetchall()
    }
    if not columns:
        return
    if "source_ip" not in columns:
        connection.execute("ALTER TABLE password_reset_tokens ADD COLUMN source_ip TEXT NOT NULL DEFAULT ''")
    if "user_agent" not in columns:
        connection.execute("ALTER TABLE password_reset_tokens ADD COLUMN user_agent TEXT NOT NULL DEFAULT ''")


def _migrate_admin_mfa_challenge_columns(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1]).strip().lower()
        for row in connection.execute("PRAGMA table_info(admin_mfa_challenges)").fetchall()
    }
    if not columns:
        return
    if "failed_attempts" not in columns:
        connection.execute("ALTER TABLE admin_mfa_challenges ADD COLUMN failed_attempts INTEGER NOT NULL DEFAULT 0")
    if "source_ip" not in columns:
        connection.execute("ALTER TABLE admin_mfa_challenges ADD COLUMN source_ip TEXT NOT NULL DEFAULT ''")
    if "user_agent" not in columns:
        connection.execute("ALTER TABLE admin_mfa_challenges ADD COLUMN user_agent TEXT NOT NULL DEFAULT ''")

