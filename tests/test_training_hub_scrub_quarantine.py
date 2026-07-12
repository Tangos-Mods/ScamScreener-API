from __future__ import annotations

import json
from pathlib import Path

from app.training_hub.config.settings import TrainingHubSettings
from app.training_hub.core.common import _now_utc_iso
from app.training_hub.core.content_scrubbing import _create_content_scrub_rule
from app.training_hub.core.storage import _init_database
from app.training_hub.core.storage_fs import _ensure_storage
from app.training_hub.core.upload_workflow import _accept_training_upload
from app.training_hub.infra import db as sqlite3


def test_scrub_rule_quarantines_matching_cases_and_keeps_clean_cases(tmp_path: Path) -> None:
    settings = _training_hub_settings(tmp_path)
    _ensure_storage(settings)
    _init_database(settings.database_path)
    _insert_user(settings.database_path, user_id=1)
    _create_content_scrub_rule(
        settings.database_path,
        actor_user_id=1,
        pattern_text="discord.gg",
        match_mode="contains",
        use_regex=False,
    )

    clean_case = _case_payload("case-clean", "hello there")
    quarantined_case = _case_payload("case-quarantine", "visit discord.gg/example")

    result = _accept_training_upload(
        settings,
        user_id=1,
        payload=_encode_cases(clean_case, quarantined_case),
        original_name="training-cases-v2.jsonl",
        source_ip="127.0.0.1",
        user_agent="ScamScreener/1.0+1.21.5",
    )

    assert result["status"] == "accepted"
    assert result["case_count"] == 2
    assert result["accepted_case_count"] == 1
    assert result["quarantined_case_count"] == 1
    assert result["inserted_cases"] == 1
    assert result["updated_cases"] == 0

    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        upload_row = connection.execute(
            "SELECT status, case_count, stored_path FROM uploads WHERE id = ?",
            (int(result["upload_id"]),),
        ).fetchone()
        assert upload_row is not None
        assert str(upload_row["status"]) == "accepted"
        assert int(upload_row["case_count"]) == 1

        case_rows = connection.execute("SELECT case_id FROM training_cases ORDER BY case_id ASC").fetchall()
        assert [str(row["case_id"]) for row in case_rows] == ["case-clean"]

    accepted_lines = _read_ndjson(Path(str(upload_row["stored_path"])))
    assert accepted_lines == [clean_case]

    quarantine_files = sorted(settings.quarantine_dir.glob("*.jsonl"))
    assert len(quarantine_files) == 1
    assert _read_ndjson(quarantine_files[0]) == [quarantined_case]


def test_scrub_rule_can_quarantine_an_entire_upload(tmp_path: Path) -> None:
    settings = _training_hub_settings(tmp_path)
    _ensure_storage(settings)
    _init_database(settings.database_path)
    _insert_user(settings.database_path, user_id=1)
    _create_content_scrub_rule(
        settings.database_path,
        actor_user_id=1,
        pattern_text="discord.gg",
        match_mode="contains",
        use_regex=False,
    )

    quarantined_case = _case_payload("case-only", "visit discord.gg/example")
    result = _accept_training_upload(
        settings,
        user_id=1,
        payload=_encode_cases(quarantined_case),
        original_name="training-cases-v2.jsonl",
        source_ip="127.0.0.1",
        user_agent="ScamScreener/1.0+1.21.5",
    )

    assert result["status"] == "quarantined"
    assert result["case_count"] == 1
    assert result["accepted_case_count"] == 0
    assert result["quarantined_case_count"] == 1
    assert result["inserted_cases"] == 0

    with sqlite3.connect(settings.database_path) as connection:
        connection.row_factory = sqlite3.Row
        upload_row = connection.execute(
            "SELECT status, case_count, stored_path FROM uploads WHERE id = ?",
            (int(result["upload_id"]),),
        ).fetchone()
        assert upload_row is not None
        assert str(upload_row["status"]) == "quarantined"
        assert int(upload_row["case_count"]) == 1
        assert connection.execute("SELECT COUNT(*) FROM training_cases").fetchone()[0] == 0

    assert _read_ndjson(Path(str(upload_row["stored_path"]))) == [quarantined_case]


def test_duplicate_accepted_upload_still_persists_new_quarantine_cases(tmp_path: Path) -> None:
    settings = _training_hub_settings(tmp_path)
    _ensure_storage(settings)
    _init_database(settings.database_path)
    _insert_user(settings.database_path, user_id=1)
    _create_content_scrub_rule(
        settings.database_path,
        actor_user_id=1,
        pattern_text="discord.gg",
        match_mode="contains",
        use_regex=False,
    )

    clean_case = _case_payload("case-clean", "hello there")
    first_quarantined_case = _case_payload("case-quarantine-1", "visit discord.gg/one")
    second_quarantined_case = _case_payload("case-quarantine-2", "visit discord.gg/two")

    first_result = _accept_training_upload(
        settings,
        user_id=1,
        payload=_encode_cases(clean_case, first_quarantined_case),
        original_name="training-cases-v2.jsonl",
        source_ip="127.0.0.1",
        user_agent="ScamScreener/1.0+1.21.5",
    )
    second_result = _accept_training_upload(
        settings,
        user_id=1,
        payload=_encode_cases(clean_case, second_quarantined_case),
        original_name="training-cases-v2.jsonl",
        source_ip="127.0.0.1",
        user_agent="ScamScreener/1.0+1.21.5",
    )

    assert first_result["status"] == "accepted"
    assert second_result["status"] == "duplicate"
    assert int(second_result["upload_id"]) == int(first_result["upload_id"])
    assert second_result["accepted_case_count"] == 1
    assert second_result["quarantined_case_count"] == 1

    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 1

    quarantine_files = sorted(settings.quarantine_dir.glob("*.jsonl"))
    assert len(quarantine_files) == 2
    quarantined_case_ids = sorted(
        case["caseId"]
        for file_path in quarantine_files
        for case in _read_ndjson(file_path)
    )
    assert quarantined_case_ids == ["case-quarantine-1", "case-quarantine-2"]


def _training_hub_settings(tmp_path: Path) -> TrainingHubSettings:
    return TrainingHubSettings(
        host="127.0.0.1",
        port=18080,
        database_url="",
        secret_key="test-secret-key-for-security-check-123456",
        session_ttl_minutes=240,
        max_upload_bytes=1024 * 1024,
        storage_dir=tmp_path / "data",
        pipeline_command="",
        project_root=tmp_path,
        admin_emails=set(),
        admin_usernames={"alice"},
        trusted_proxies=set(),
        enable_rate_limit=True,
        enforce_origin_check=True,
        smtp_use_starttls=False,
        api_docs_enabled=True,
    )


def _insert_user(database_path: Path | str, *, user_id: int) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO users (id, created_at, username, email, password_hash, is_admin)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (user_id, _now_utc_iso(), f"user{user_id}", f"user{user_id}@example.com", "hash"),
        )
        connection.commit()


def _case_payload(case_id: str, message_text: str) -> dict[str, object]:
    return {
        "format": "training_case_v2",
        "schemaVersion": 2,
        "caseId": case_id,
        "caseData": {
            "label": "risk",
            "messages": [{"text": message_text}],
            "caseSignalTagIds": ["signal"],
        },
        "observedPipeline": {"outcomeAtCapture": "risk"},
    }


def _encode_cases(*cases: dict[str, object]) -> bytes:
    return "\n".join(json.dumps(case, separators=(",", ":")) for case in cases).encode("utf-8")


def _read_ndjson(file_path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in file_path.read_text(encoding="utf-8").splitlines() if line.strip()]
