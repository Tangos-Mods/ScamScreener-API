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


def _migrate_training_case_tombstone_columns(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(
        connection,
        "training_cases",
        "content_deleted_at",
        "ALTER TABLE training_cases ADD COLUMN content_deleted_at TEXT",
        "ALTER TABLE training_cases ADD COLUMN content_deleted_at VARCHAR(40) NULL",
    )


def _migrate_client_identity_tables(connection: sqlite3.Connection) -> None:
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS client_identities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                normalized_client_id TEXT NOT NULL UNIQUE,
                linked_user_id INTEGER,
                linked_at TEXT,
                last_seen_at TEXT NOT NULL,
                FOREIGN KEY (linked_user_id) REFERENCES users(id)
            )
            """
        )
    except Exception:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS client_identities (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                created_at VARCHAR(40) NOT NULL,
                normalized_client_id VARCHAR(128) NOT NULL UNIQUE,
                linked_user_id BIGINT NULL,
                linked_at VARCHAR(40),
                last_seen_at VARCHAR(40) NOT NULL,
                FOREIGN KEY (linked_user_id) REFERENCES users(id)
            )
            """
        )

    _add_column_if_missing(
        connection,
        "uploads",
        "client_identity_id",
        "ALTER TABLE uploads ADD COLUMN client_identity_id INTEGER",
        "ALTER TABLE uploads ADD COLUMN client_identity_id BIGINT NULL",
    )
    _add_column_if_missing(
        connection,
        "training_cases",
        "created_by_client_identity_id",
        "ALTER TABLE training_cases ADD COLUMN created_by_client_identity_id INTEGER",
        "ALTER TABLE training_cases ADD COLUMN created_by_client_identity_id BIGINT NULL",
    )


def _migrate_users_security_columns(connection: sqlite3.Connection) -> None:
    _add_column_if_missing(
        connection,
        "users",
        "mfa_enabled",
        "ALTER TABLE users ADD COLUMN mfa_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN mfa_enabled TINYINT NOT NULL DEFAULT 0",
    )
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
    _add_column_if_missing(
        connection,
        "uploads",
        "user_agent",
        "ALTER TABLE uploads ADD COLUMN user_agent TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE uploads ADD COLUMN user_agent VARCHAR(300) NOT NULL DEFAULT ''",
    )
    columns = _table_columns(connection, "uploads")
    if not columns:
        return

    if "client_identity_id" not in columns:
        return

    # SQLite does not support altering nullability in place.
    try:
        pragma_rows = connection.execute("PRAGMA table_info(uploads)").fetchall()
    except Exception:
        pragma_rows = []
    if pragma_rows:
        user_id_row = next((row for row in pragma_rows if str(row[1]).strip().lower() == "user_id"), None)
        if user_id_row is not None and int(user_id_row[3] or 0) == 1:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS uploads__migration (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    user_id INTEGER,
                    client_identity_id INTEGER,
                    original_file_name TEXT NOT NULL,
                    stored_path TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    case_count INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    duplicate_of_upload_id INTEGER,
                    source_ip TEXT NOT NULL DEFAULT '',
                    user_agent TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (client_identity_id) REFERENCES client_identities(id),
                    FOREIGN KEY (duplicate_of_upload_id) REFERENCES uploads__migration(id)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO uploads__migration (
                    id, created_at, user_id, client_identity_id, original_file_name, stored_path,
                    payload_sha256, case_count, size_bytes, status, duplicate_of_upload_id, source_ip, user_agent
                )
                SELECT
                    id, created_at, user_id, client_identity_id, original_file_name, stored_path,
                    payload_sha256, case_count, size_bytes, status, duplicate_of_upload_id, source_ip, user_agent
                FROM uploads
                """
            )
            connection.execute("DROP TABLE uploads")
            connection.execute("ALTER TABLE uploads__migration RENAME TO uploads")
            return

    # MariaDB path.
    try:
        connection.execute("ALTER TABLE uploads MODIFY user_id BIGINT NULL")
    except Exception:
        pass


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
    try:
        pragma_rows = connection.execute("PRAGMA table_info(audit_logs)").fetchall()
    except Exception:
        pragma_rows = []
    if pragma_rows:
        actor_row = next((row for row in pragma_rows if str(row[1]).strip().lower() == "actor_user_id"), None)
        if actor_row is not None and int(actor_row[3] or 0) == 1:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_logs__migration (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    actor_user_id INTEGER,
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
                INSERT INTO audit_logs__migration (
                    id, created_at, actor_user_id, action, target_type, target_id, details, source_ip, user_agent
                )
                SELECT
                    id, created_at, actor_user_id, action, target_type, target_id, details, source_ip, user_agent
                FROM audit_logs
                """
            )
            connection.execute("DROP TABLE audit_logs")
            connection.execute("ALTER TABLE audit_logs__migration RENAME TO audit_logs")
            return

    try:
        connection.execute("ALTER TABLE audit_logs MODIFY actor_user_id BIGINT NULL")
    except Exception:
        pass


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


def _migrate_mfa_tables(connection: sqlite3.Connection) -> None:
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_flow_tokens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                flow_type TEXT NOT NULL,
                token_sha256 TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL DEFAULT '{}',
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
            CREATE TABLE IF NOT EXISTS user_totp_factors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                encrypted_secret TEXT NOT NULL,
                verified_at TEXT,
                last_used_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_passkeys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                credential_id TEXT NOT NULL UNIQUE,
                public_key TEXT NOT NULL,
                sign_count INTEGER NOT NULL DEFAULT 0,
                aaguid TEXT NOT NULL DEFAULT '',
                credential_device_type TEXT NOT NULL DEFAULT '',
                backed_up INTEGER NOT NULL DEFAULT 0,
                last_used_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS user_backup_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                code_sha256 TEXT NOT NULL,
                consumed_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_auth_flow_tokens_user ON auth_flow_tokens(user_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_auth_flow_tokens_expires ON auth_flow_tokens(expires_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_auth_flow_tokens_flow_type ON auth_flow_tokens(flow_type)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_user_totp_factors_user ON user_totp_factors(user_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_user_passkeys_user ON user_passkeys(user_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_user_backup_codes_user ON user_backup_codes(user_id)")
        return
    except Exception:
        pass

    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS auth_flow_tokens (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            created_at VARCHAR(40) NOT NULL,
            user_id BIGINT NOT NULL,
            flow_type VARCHAR(64) NOT NULL,
            token_sha256 CHAR(64) NOT NULL UNIQUE,
            payload_json LONGTEXT NOT NULL,
            expires_at VARCHAR(40) NOT NULL,
            consumed_at VARCHAR(40),
            failed_attempts INT NOT NULL DEFAULT 0,
            source_ip VARCHAR(80) NOT NULL DEFAULT '',
            user_agent VARCHAR(300) NOT NULL DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(id),
            KEY idx_auth_flow_tokens_user (user_id),
            KEY idx_auth_flow_tokens_expires (expires_at),
            KEY idx_auth_flow_tokens_flow_type (flow_type)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS user_totp_factors (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            created_at VARCHAR(40) NOT NULL,
            user_id BIGINT NOT NULL,
            label VARCHAR(80) NOT NULL,
            encrypted_secret LONGTEXT NOT NULL,
            verified_at VARCHAR(40),
            last_used_at VARCHAR(40),
            FOREIGN KEY (user_id) REFERENCES users(id),
            KEY idx_user_totp_factors_user (user_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS user_passkeys (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            created_at VARCHAR(40) NOT NULL,
            user_id BIGINT NOT NULL,
            label VARCHAR(80) NOT NULL,
            credential_id VARCHAR(255) NOT NULL UNIQUE,
            public_key LONGTEXT NOT NULL,
            sign_count BIGINT NOT NULL DEFAULT 0,
            aaguid VARCHAR(64) NOT NULL DEFAULT '',
            credential_device_type VARCHAR(64) NOT NULL DEFAULT '',
            backed_up TINYINT NOT NULL DEFAULT 0,
            last_used_at VARCHAR(40),
            FOREIGN KEY (user_id) REFERENCES users(id),
            KEY idx_user_passkeys_user (user_id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS user_backup_codes (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            created_at VARCHAR(40) NOT NULL,
            user_id BIGINT NOT NULL,
            code_sha256 CHAR(64) NOT NULL,
            consumed_at VARCHAR(40),
            FOREIGN KEY (user_id) REFERENCES users(id),
            KEY idx_user_backup_codes_user (user_id)
        )
        """
    )


def _migrate_training_case_identity_columns(connection: sqlite3.Connection) -> None:
    columns = _table_columns(connection, "training_cases")
    if not columns or "created_by_client_identity_id" not in columns:
        return

    try:
        pragma_rows = connection.execute("PRAGMA table_info(training_cases)").fetchall()
    except Exception:
        pragma_rows = []
    if pragma_rows:
        user_id_row = next((row for row in pragma_rows if str(row[1]).strip().lower() == "created_by_user_id"), None)
        if user_id_row is not None and int(user_id_row[3] or 0) == 1:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS training_cases__migration (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    created_by_user_id INTEGER,
                    created_by_client_identity_id INTEGER,
                    source_upload_id INTEGER,
                    status TEXT NOT NULL DEFAULT 'submitted',
                    label TEXT NOT NULL DEFAULT '',
                    outcome TEXT NOT NULL DEFAULT '',
                    tag_ids_json TEXT NOT NULL DEFAULT '[]',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    content_deleted_at TEXT,
                    FOREIGN KEY (created_by_user_id) REFERENCES users(id),
                    FOREIGN KEY (created_by_client_identity_id) REFERENCES client_identities(id),
                    FOREIGN KEY (source_upload_id) REFERENCES uploads(id)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO training_cases__migration (
                    id, case_id, created_at, updated_at, created_by_user_id, created_by_client_identity_id,
                    source_upload_id, status, label, outcome, tag_ids_json, payload_json, content_deleted_at
                )
                SELECT
                    id, case_id, created_at, updated_at, created_by_user_id, created_by_client_identity_id,
                    source_upload_id, status, label, outcome, tag_ids_json, payload_json, content_deleted_at
                FROM training_cases
                """
            )
            connection.execute("DROP TABLE training_cases")
            connection.execute("ALTER TABLE training_cases__migration RENAME TO training_cases")
            return

    try:
        connection.execute("ALTER TABLE training_cases MODIFY created_by_user_id BIGINT NULL")
    except Exception:
        pass

