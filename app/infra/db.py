from __future__ import annotations

import sqlite3 as _sqlite3
from pathlib import Path
from urllib.parse import unquote, urlsplit

try:
    import pymysql
except ImportError:  # pragma: no cover - exercised only when MariaDB mode is used without dependency
    pymysql = None


class Row:
    """Marker type used by the app to request mapping-like rows."""


class _DBRow:
    def __init__(self, columns: list[str], values: tuple[object, ...]) -> None:
        self._columns = columns
        self._values = values
        self._by_name = {name: values[index] for index, name in enumerate(columns)}

    def __getitem__(self, key: int | str):
        if isinstance(key, int):
            return self._values[key]
        return self._by_name[key]

    def __iter__(self):
        return iter(self._columns)

    def __len__(self) -> int:
        return len(self._values)

    def keys(self) -> list[str]:
        return list(self._columns)

    def items(self):
        return self._by_name.items()

    def values(self):
        return self._by_name.values()


class _CursorWrapper:
    def __init__(self, cursor, row_factory) -> None:
        self._cursor = cursor
        self._row_factory = row_factory
        self._columns = [str(desc[0]) for desc in (cursor.description or [])]

    @property
    def rowcount(self) -> int:
        return int(getattr(self._cursor, "rowcount", 0) or 0)

    @property
    def lastrowid(self) -> int:
        return int(getattr(self._cursor, "lastrowid", 0) or 0)

    def fetchone(self):
        raw = self._cursor.fetchone()
        if raw is None:
            return None
        if self._row_factory is Row:
            return _DBRow(self._columns, tuple(raw))
        return raw

    def fetchall(self):
        rows = self._cursor.fetchall()
        if self._row_factory is Row:
            return [_DBRow(self._columns, tuple(row)) for row in rows]
        return rows


class _EmptyCursor:
    rowcount = 0
    lastrowid = 0

    @staticmethod
    def fetchone():
        return None

    @staticmethod
    def fetchall():
        return []


def _is_mariadb_dsn(value: object) -> bool:
    if isinstance(value, Path):
        return False
    text = str(value or "").strip().lower()
    return text.startswith("mariadb://") or text.startswith("mysql://")


def is_mariadb_target(value: object) -> bool:
    return _is_mariadb_dsn(value)


def _mariadb_params_from_dsn(dsn: str) -> dict[str, object]:
    parsed = urlsplit(dsn)
    database = parsed.path.lstrip("/")
    if not database:
        raise ValueError("MariaDB DSN must include a database name, e.g. mariadb://user:pass@host:3306/dbname")
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": int(parsed.port or 3306),
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": unquote(database),
        "charset": "utf8mb4",
        "autocommit": False,
    }


class _ConnectionWrapper:
    def __init__(self, raw_connection, driver: str) -> None:
        self._connection = raw_connection
        self._driver = driver
        self.row_factory = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            try:
                self.rollback()
            except Exception:
                pass
        self.close()
        return False

    def _rewrite_sql(self, sql: str) -> str | None:
        statement = str(sql or "")
        normalized = statement.strip().lower()
        if self._driver == "mariadb":
            if normalized.startswith("pragma "):
                return None
            if normalized == "begin immediate":
                return "START TRANSACTION"
            return statement.replace("?", "%s")
        return statement

    def execute(self, sql: str, params: tuple | list = ()):
        rewritten = self._rewrite_sql(sql)
        if rewritten is None:
            return _EmptyCursor()
        cursor = self._connection.cursor()
        cursor.execute(rewritten, tuple(params or ()))
        return _CursorWrapper(cursor, self.row_factory)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


def connect(database_target):
    if _is_mariadb_dsn(database_target):
        if pymysql is None:
            raise RuntimeError(
                "PyMySQL is required for MariaDB mode. Install dependencies from server/requirements.txt."
            )
        params = _mariadb_params_from_dsn(str(database_target))
        raw = pymysql.connect(**params)
        return _ConnectionWrapper(raw, "mariadb")
    raw = _sqlite3.connect(database_target)
    return _ConnectionWrapper(raw, "sqlite")

