from __future__ import annotations

import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.training_hub.config.settings import TrainingHubSettings
from app.training_hub.core.common import _format_utc_timestamp
from app.training_hub.services import mailer


def test_account_data_export_email_includes_zip_attachment_and_html_alternative(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    sent_messages = []

    monkeypatch.setattr(mailer, "_send_message", lambda _settings, message: sent_messages.append(message))

    mailer.send_account_data_export_email(
        settings,
        recipient_email="alice@example.com",
        requested_at="2026-03-28T18:00:00Z",
        archive_name="account-data-export.zip",
        archive_bytes=b"zip-bytes",
        size_bytes=9,
    )

    assert len(sent_messages) == 1
    message = sent_messages[0]
    assert message["Subject"] == "ScamScreener Account Data Export"

    plain_body = message.get_body(preferencelist=("plain",))
    html_body = message.get_body(preferencelist=("html",))
    attachments = list(message.iter_attachments())

    assert plain_body is not None
    assert html_body is not None
    assert "Requested at (UTC): 2026-03-28 18:00 UTC" in plain_body.get_content()
    assert "account-data-export.zip (9 bytes)" in plain_body.get_content()
    assert "Account Data Export" in html_body.get_content()
    assert "account-data-export.zip" in html_body.get_content()
    assert "2026-03-28 18:00 UTC" in html_body.get_content()
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "account-data-export.zip"


def test_account_deletion_email_includes_deleted_timestamp_and_username(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    sent_messages = []

    monkeypatch.setattr(mailer, "_send_message", lambda _settings, message: sent_messages.append(message))

    mailer.send_account_deletion_email(
        settings,
        recipient_email="alice@example.com",
        username="alice",
        deleted_at="2026-03-28T18:00:00Z",
    )

    assert len(sent_messages) == 1
    message = sent_messages[0]
    assert message["Subject"] == "ScamScreener Account Deleted"

    plain_body = message.get_body(preferencelist=("plain",))
    html_body = message.get_body(preferencelist=("html",))

    assert plain_body is not None
    assert html_body is not None
    assert "Username: alice" in plain_body.get_content()
    assert "Deleted at (UTC): 2026-03-28 18:00 UTC" in plain_body.get_content()
    assert "Account Deleted" in html_body.get_content()
    assert "alice" in html_body.get_content()
    assert "2026-03-28 18:00 UTC" in html_body.get_content()


def test_format_utc_timestamp_normalizes_iso_and_preserves_seconds() -> None:
    assert _format_utc_timestamp("2026-03-28T18:00:00Z") == "2026-03-28 18:00 UTC"
    assert _format_utc_timestamp("2026-03-28T18:00:05Z") == "2026-03-28 18:00:05 UTC"
    assert _format_utc_timestamp("not-a-date") == "not-a-date"


def _settings(tmp_path: Path) -> TrainingHubSettings:
    return TrainingHubSettings(
        host="127.0.0.1",
        port=8080,
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
        public_base_url="https://scamscreener.example.com",
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_username="no-reply@scamscreener.example.com",
        smtp_password="secret",
        smtp_from_email="no-reply@scamscreener.example.com",
        smtp_use_tls=True,
        smtp_use_starttls=False,
    )
