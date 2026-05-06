import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.training_hub.http import rate_limit


class _FakeCursor:
    def __init__(self, *, row=None, rowcount: int = 0) -> None:
        self._row = row
        self.rowcount = rowcount

    def fetchone(self):
        return self._row

    def fetchall(self):
        if self._row is None:
            return []
        return [self._row]


class _FakeConnection:
    def __init__(self, steps: list[dict[str, object]]) -> None:
        self._steps = list(steps)
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql: str, params=()):
        assert self._steps, f"Unexpected SQL: {sql}"
        step = self._steps.pop(0)
        expected = str(step["contains"])
        normalized = " ".join(str(sql).split()).lower()
        assert expected in normalized, f"Expected SQL containing {expected!r}, got {sql!r}"
        if "params" in step:
            assert tuple(params) == tuple(step["params"])
        if "exc" in step:
            raise step["exc"]
        return _FakeCursor(
            row=step.get("row"),
            rowcount=int(step.get("rowcount", 0)),
        )

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        return None


def test_mariadb_rate_limiter_retries_duplicate_insert(monkeypatch) -> None:
    attempts = [
        _FakeConnection(
            [
                {"contains": "begin immediate"},
                {"contains": "delete from rate_limit_hits"},
                {"contains": "update rate_limit_hits", "rowcount": 0},
                {"contains": "select count from rate_limit_hits", "row": None},
                {"contains": "insert into rate_limit_hits", "exc": RuntimeError("1062 Duplicate entry")},
            ]
        ),
        _FakeConnection(
            [
                {"contains": "begin immediate"},
                {"contains": "delete from rate_limit_hits"},
                {"contains": "update rate_limit_hits", "rowcount": 0},
                {"contains": "select count from rate_limit_hits", "row": None},
                {"contains": "insert into rate_limit_hits"},
            ]
        ),
    ]

    monkeypatch.setattr(rate_limit.sqlite3, "is_mariadb_target", lambda _target: True)
    monkeypatch.setattr(rate_limit.sqlite3, "connect", lambda _target: attempts.pop(0))

    limiter = rate_limit._DatabaseRateLimiter("mariadb://user:pass@db.internal:3306/scamscreener")

    allowed, retry_after = limiter.allow("ip:127.0.0.1", 5, 60)

    assert allowed is True
    assert retry_after == 0
    assert attempts == []


def test_mariadb_rate_limiter_blocks_after_limit(monkeypatch) -> None:
    attempts = [
        _FakeConnection(
            [
                {"contains": "begin immediate"},
                {"contains": "delete from rate_limit_hits"},
                {"contains": "update rate_limit_hits", "rowcount": 0},
                {"contains": "select count from rate_limit_hits", "row": (5,)},
            ]
        )
    ]

    monkeypatch.setattr(rate_limit.sqlite3, "is_mariadb_target", lambda _target: True)
    monkeypatch.setattr(rate_limit.sqlite3, "connect", lambda _target: attempts.pop(0))

    limiter = rate_limit._DatabaseRateLimiter("mariadb://user:pass@db.internal:3306/scamscreener")

    allowed, retry_after = limiter.allow("user:1", 5, 60)

    assert allowed is False
    assert retry_after >= 1
    assert attempts == []
