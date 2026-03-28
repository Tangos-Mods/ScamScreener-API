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


def _init_database_mariadb(database_path: Path | str) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                created_at VARCHAR(40) NOT NULL,
                username VARCHAR(64) NOT NULL UNIQUE,
                email VARCHAR(254) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                is_admin TINYINT NOT NULL DEFAULT 0,
                last_login_at VARCHAR(40),
                failed_login_attempts INT NOT NULL DEFAULT 0,
                lockout_until VARCHAR(40)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                created_at VARCHAR(40) NOT NULL,
                user_id BIGINT NOT NULL,
                token_sha256 CHAR(64) NOT NULL UNIQUE,
                expires_at VARCHAR(40) NOT NULL,
                revoked_at VARCHAR(40),
                remote_addr VARCHAR(80) NOT NULL DEFAULT '',
                user_agent VARCHAR(300) NOT NULL DEFAULT '',
                revoke_reason VARCHAR(100) NOT NULL DEFAULT '',
                FOREIGN KEY (user_id) REFERENCES users(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS uploads (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                created_at VARCHAR(40) NOT NULL,
                user_id BIGINT NOT NULL,
                original_file_name VARCHAR(255) NOT NULL,
                stored_path VARCHAR(1024) NOT NULL,
                payload_sha256 CHAR(64) NOT NULL,
                case_count INT NOT NULL,
                size_bytes BIGINT NOT NULL,
                status VARCHAR(32) NOT NULL,
                duplicate_of_upload_id BIGINT,
                source_ip VARCHAR(80) NOT NULL DEFAULT '',
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (duplicate_of_upload_id) REFERENCES uploads(id),
                UNIQUE KEY idx_uploads_user_sha (user_id, payload_sha256),
                KEY idx_uploads_created_at (created_at),
                KEY idx_uploads_source_ip (source_ip)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS training_runs (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                created_at VARCHAR(40) NOT NULL,
                started_by_user_id BIGINT NOT NULL,
                upload_count INT NOT NULL,
                case_count INT NOT NULL,
                status VARCHAR(32) NOT NULL,
                command TEXT NOT NULL,
                bundle_path VARCHAR(1024) NOT NULL,
                output_log LONGTEXT NOT NULL,
                FOREIGN KEY (started_by_user_id) REFERENCES users(id),
                KEY idx_training_runs_created_at (created_at)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS training_cases (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                case_id VARCHAR(128) NOT NULL UNIQUE,
                created_at VARCHAR(40) NOT NULL,
                updated_at VARCHAR(40) NOT NULL,
                created_by_user_id BIGINT NOT NULL,
                source_upload_id BIGINT,
                status VARCHAR(32) NOT NULL DEFAULT 'submitted',
                label VARCHAR(128) NOT NULL DEFAULT '',
                outcome VARCHAR(64) NOT NULL DEFAULT '',
                tag_ids_json LONGTEXT NOT NULL,
                payload_json LONGTEXT NOT NULL,
                FOREIGN KEY (created_by_user_id) REFERENCES users(id),
                FOREIGN KEY (source_upload_id) REFERENCES uploads(id),
                KEY idx_training_cases_status (status)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_logs (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                created_at VARCHAR(40) NOT NULL,
                actor_user_id BIGINT NOT NULL,
                action VARCHAR(128) NOT NULL,
                target_type VARCHAR(64) NOT NULL DEFAULT '',
                target_id BIGINT,
                details LONGTEXT NOT NULL,
                source_ip VARCHAR(80) NOT NULL DEFAULT '',
                user_agent VARCHAR(300) NOT NULL DEFAULT '',
                FOREIGN KEY (actor_user_id) REFERENCES users(id),
                KEY idx_audit_logs_created_at (created_at),
                KEY idx_audit_logs_actor (actor_user_id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                created_at VARCHAR(40) NOT NULL,
                user_id BIGINT NOT NULL,
                token_sha256 CHAR(64) NOT NULL UNIQUE,
                expires_at VARCHAR(40) NOT NULL,
                consumed_at VARCHAR(40),
                source_ip VARCHAR(80) NOT NULL DEFAULT '',
                user_agent VARCHAR(300) NOT NULL DEFAULT '',
                FOREIGN KEY (user_id) REFERENCES users(id),
                KEY idx_password_reset_user (user_id),
                KEY idx_password_reset_expires (expires_at)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_mfa_challenges (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                created_at VARCHAR(40) NOT NULL,
                user_id BIGINT NOT NULL,
                token_sha256 CHAR(64) NOT NULL UNIQUE,
                code_sha256 CHAR(64) NOT NULL,
                expires_at VARCHAR(40) NOT NULL,
                consumed_at VARCHAR(40),
                failed_attempts INT NOT NULL DEFAULT 0,
                source_ip VARCHAR(80) NOT NULL DEFAULT '',
                user_agent VARCHAR(300) NOT NULL DEFAULT '',
                FOREIGN KEY (user_id) REFERENCES users(id),
                KEY idx_admin_mfa_user (user_id),
                KEY idx_admin_mfa_expires (expires_at)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rate_limit_hits (
                bucket_key VARCHAR(190) NOT NULL,
                bucket_start BIGINT NOT NULL,
                count INT NOT NULL,
                updated_at VARCHAR(40) NOT NULL,
                PRIMARY KEY (bucket_key, bucket_start),
                KEY idx_rate_limit_updated_at (updated_at)
            )
            """
        )
        _migrate_users_security_columns(connection)
        _migrate_uploads_security_columns(connection)
        _migrate_audit_log_columns(connection)
        _migrate_password_reset_token_columns(connection)
        _migrate_admin_mfa_challenge_columns(connection)
        _migrate_training_cases_payload_json(connection)
        connection.commit()

