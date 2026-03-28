import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.training_hub.core.storage_migrations import (
    _migrate_training_cases_payload_json,
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
