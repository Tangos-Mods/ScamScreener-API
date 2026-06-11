import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.training_hub.core.storage_migrations import (
    _migrate_external_auth_tables,
    _migrate_training_case_tombstone_columns,
    _migrate_training_cases_payload_json,
    _migrate_uploads_security_columns,
    _migrate_users_security_columns,
)


class _FakeCursor:
    def __init__(self, rows):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)


class _FakeMariaDbConnection:
    def __init__(self, columns_by_table: dict[str, list[str]]) -> None:
        self.columns_by_table = columns_by_table
        self.executed: list[str] = []

    def execute(self, sql: str):
        self.executed.append(sql)
        normalized = " ".join(str(sql).strip().lower().split())
        if normalized.startswith("pragma table_info("):
            return _FakeCursor([])
        if normalized.startswith("show columns from `"):
            table_name = normalized.split("`", 2)[1]
            rows = [(column, None, None, None, None, None) for column in self.columns_by_table.get(table_name, [])]
            return _FakeCursor(rows)
        if " add column " in normalized and (
            " integer " in normalized
            or normalized.endswith(" text")
            or " text not null " in normalized
        ):
            raise RuntimeError("Simulated MariaDB syntax rejection for SQLite DDL.")
        return _FakeCursor([])


def test_migrate_training_cases_payload_json_uses_mariadb_fallback_when_pragma_is_unavailable() -> None:
    connection = _FakeMariaDbConnection({"training_cases": ["id", "case_id"]})

    _migrate_training_cases_payload_json(connection)

    assert (
        "ALTER TABLE training_cases ADD COLUMN payload_json LONGTEXT NOT NULL DEFAULT '{}'"
        in connection.executed
    )


def test_migrate_users_security_columns_uses_show_columns_for_mariadb_targets() -> None:
    connection = _FakeMariaDbConnection({"users": ["id", "username", "email", "password_hash", "is_admin"]})

    _migrate_users_security_columns(connection)

    assert "ALTER TABLE users ADD COLUMN failed_login_attempts INT NOT NULL DEFAULT 0" in connection.executed
    assert "ALTER TABLE users ADD COLUMN lockout_until VARCHAR(40)" in connection.executed


def test_migrate_uploads_security_columns_adds_user_agent_for_mariadb_targets() -> None:
    connection = _FakeMariaDbConnection(
        {
            "uploads": [
                "id",
                "created_at",
                "user_id",
                "client_identity_id",
                "original_file_name",
                "stored_path",
                "payload_sha256",
                "case_count",
                "size_bytes",
                "status",
                "duplicate_of_upload_id",
                "source_ip",
            ]
        }
    )

    _migrate_uploads_security_columns(connection)

    assert "ALTER TABLE uploads ADD COLUMN user_agent VARCHAR(300) NOT NULL DEFAULT ''" in connection.executed


def test_migrate_external_auth_tables_create_mariadb_compatible_tables() -> None:
    connection = _FakeMariaDbConnection({"users": ["id", "username", "email", "password_hash", "is_admin"]})

    _migrate_external_auth_tables(connection)

    assert any("CREATE TABLE IF NOT EXISTS external_auth_states" in sql for sql in connection.executed)
    assert any("CREATE TABLE IF NOT EXISTS external_identities" in sql for sql in connection.executed)


def test_migrate_training_case_tombstone_columns_uses_mariadb_fallback_when_needed() -> None:
    connection = _FakeMariaDbConnection(
        {
            "training_cases": [
                "id",
                "case_id",
                "created_at",
                "updated_at",
                "created_by_user_id",
                "created_by_client_identity_id",
                "source_upload_id",
                "status",
                "label",
                "outcome",
                "tag_ids_json",
                "payload_json",
            ]
        }
    )

    _migrate_training_case_tombstone_columns(connection)

    assert "ALTER TABLE training_cases ADD COLUMN content_deleted_at VARCHAR(40) NULL" in connection.executed
