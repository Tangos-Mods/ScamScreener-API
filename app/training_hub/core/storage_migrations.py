from __future__ import annotations

from ..infra import db as sqlite3


def _table_columns(connection, table_name: str) -> set[str]:
    columns: set[str] = set()

    try:
        pragma_rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    except Exception:
        pragma_rows = []

    for row in pragma_rows:
        if len(row) > 1:
            columns.add(str(row[1]).strip().lower())
    if columns:
        return columns

    safe_table_name = table_name.replace("`", "``")
    try:
        show_rows = connection.execute(f"SHOW COLUMNS FROM `{safe_table_name}`").fetchall()
    except Exception:
        show_rows = []

    for row in show_rows:
        if len(row) > 0:
            columns.add(str(row[0]).strip().lower())
    return columns


def _add_column_if_missing(
    connection,
    table_name: str,
    column_name: str,
    sqlite_sql: str,
    mariadb_sql: str,
) -> None:
    columns = _table_columns(connection, table_name)
    if column_name in columns:
        return

    try:
        connection.execute(sqlite_sql)
        return
    except Exception:
        pass

    connection.execute(mariadb_sql)


def _migrate_training_cases_payload_json(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(
        connection,
        "training_cases",
        "payload_json",
        "ALTER TABLE training_cases ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE training_cases ADD COLUMN payload_json LONGTEXT NOT NULL DEFAULT '{}'",
    )


def _migrate_users_security_columns(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(
        connection,
        "users",
        "failed_login_attempts",
        "ALTER TABLE users ADD COLUMN failed_login_attempts INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN failed_login_attempts INT NOT NULL DEFAULT 0",
    )
    _add_column_if_missing(
        connection,
        "users",
        "lockout_until",
        "ALTER TABLE users ADD COLUMN lockout_until TEXT",
        "ALTER TABLE users ADD COLUMN lockout_until VARCHAR(40)",
    )


def _migrate_uploads_security_columns(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(
        connection,
        "uploads",
        "source_ip",
        "ALTER TABLE uploads ADD COLUMN source_ip TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE uploads ADD COLUMN source_ip VARCHAR(80) NOT NULL DEFAULT ''",
    )


def _migrate_audit_log_columns(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(
        connection,
        "audit_logs",
        "source_ip",
        "ALTER TABLE audit_logs ADD COLUMN source_ip TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE audit_logs ADD COLUMN source_ip VARCHAR(80) NOT NULL DEFAULT ''",
    )
    _add_column_if_missing(
        connection,
        "audit_logs",
        "user_agent",
        "ALTER TABLE audit_logs ADD COLUMN user_agent TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE audit_logs ADD COLUMN user_agent VARCHAR(300) NOT NULL DEFAULT ''",
    )


def _migrate_password_reset_token_columns(connection: sqlite3.Connection) -> None:
    columns = _table_columns(connection, "password_reset_tokens")
    if not columns:
        return
    _add_column_if_missing(
        connection,
        "password_reset_tokens",
        "source_ip",
        "ALTER TABLE password_reset_tokens ADD COLUMN source_ip TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE password_reset_tokens ADD COLUMN source_ip VARCHAR(80) NOT NULL DEFAULT ''",
    )
    _add_column_if_missing(
        connection,
        "password_reset_tokens",
        "user_agent",
        "ALTER TABLE password_reset_tokens ADD COLUMN user_agent TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE password_reset_tokens ADD COLUMN user_agent VARCHAR(300) NOT NULL DEFAULT ''",
    )


def _migrate_admin_mfa_challenge_columns(connection: sqlite3.Connection) -> None:
    columns = _table_columns(connection, "admin_mfa_challenges")
    if not columns:
        return
    _add_column_if_missing(
        connection,
        "admin_mfa_challenges",
        "failed_attempts",
        "ALTER TABLE admin_mfa_challenges ADD COLUMN failed_attempts INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE admin_mfa_challenges ADD COLUMN failed_attempts INT NOT NULL DEFAULT 0",
    )
    _add_column_if_missing(
        connection,
        "admin_mfa_challenges",
        "source_ip",
        "ALTER TABLE admin_mfa_challenges ADD COLUMN source_ip TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE admin_mfa_challenges ADD COLUMN source_ip VARCHAR(80) NOT NULL DEFAULT ''",
    )
    _add_column_if_missing(
        connection,
        "admin_mfa_challenges",
        "user_agent",
        "ALTER TABLE admin_mfa_challenges ADD COLUMN user_agent TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE admin_mfa_challenges ADD COLUMN user_agent VARCHAR(300) NOT NULL DEFAULT ''",
    )

