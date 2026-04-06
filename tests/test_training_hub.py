import hashlib
import io
import json
import re
import sqlite3
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.training_hub.main import TrainingHubSettings, create_app

CSRF_COOKIE_NAME = "training_hub_csrf"


def test_register_upload_and_dashboard(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    register = _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    assert register.status_code == 200
    assert "Your Case Contributions" in register.text

    upload = _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("training-cases-v2.jsonl", _valid_payload(), "application/x-ndjson")},
    )
    assert upload.status_code == 201
    assert "accepted with 1 cases" in upload.text

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "My Uploads" in dashboard.text
    assert "case_000001" not in dashboard.text

    with sqlite3.connect(settings.database_path) as connection:
        uploads = int(connection.execute("SELECT COUNT(*) FROM uploads").fetchone()[0])
        cases = int(connection.execute("SELECT COUNT(*) FROM training_cases").fetchone()[0])
        assert uploads == 1
        assert cases == 1


def test_user_can_delete_own_upload_and_rebuild_case_from_remaining_upload(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    shared_case_id = "case_shared_0001"
    alice_payload = _valid_payload(case_id=shared_case_id, label="risk", outcome="review")
    bob_payload = _valid_payload(case_id=shared_case_id, label="safe", outcome="safe")
    alice_upload_path = settings.uploads_dir / f"{hashlib.sha256(alice_payload.encode('utf-8')).hexdigest()}.jsonl"
    bob_upload_path = settings.uploads_dir / f"{hashlib.sha256(bob_payload.encode('utf-8')).hexdigest()}.jsonl"

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("alice-shared.jsonl", alice_payload, "application/x-ndjson")},
    )
    _post_form(client, "/logout", follow_redirects=True)

    _post_form(
        client,
        "/register",
        data={"username": "bob", "email": "bob@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("bob-shared.jsonl", bob_payload, "application/x-ndjson")},
    )
    _post_form(client, "/logout", follow_redirects=True)

    _post_form(
        client,
        "/login",
        data={"username_or_email": "alice", "password": "supersecret"},
        follow_redirects=True,
    )
    delete_response = _post_form(client, "/dashboard/uploads/1/delete")

    assert delete_response.status_code == 200
    assert "Deleted upload #1." in delete_response.text

    with sqlite3.connect(settings.database_path) as connection:
        upload_one = connection.execute("SELECT id FROM uploads WHERE id = 1").fetchone()
        upload_two = connection.execute("SELECT id FROM uploads WHERE id = 2").fetchone()
        case_row = connection.execute(
            "SELECT created_by_user_id, source_upload_id, label, outcome FROM training_cases WHERE case_id = ?",
            (shared_case_id,),
        ).fetchone()
        assert upload_one is None
        assert upload_two is not None
        assert case_row == (2, 2, "safe", "safe")

    assert not alice_upload_path.exists()
    assert bob_upload_path.exists()


def test_user_can_purge_own_uploads_and_cases_without_deleting_account(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("case-one.jsonl", _valid_payload(case_id="case_purge_0001"), "application/x-ndjson")},
    )
    _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("case-two.jsonl", _valid_payload(case_id="case_purge_0002"), "application/x-ndjson")},
    )

    response = _post_form(
        client,
        "/dashboard/data/purge",
        data={"current_password": "supersecret", "confirmation": "ERASE MY DATA"},
    )

    assert response.status_code == 200
    assert "Deleted 2 uploads." in response.text

    with sqlite3.connect(settings.database_path) as connection:
        user_row = connection.execute("SELECT id FROM users WHERE username = 'alice'").fetchone()
        upload_count = int(connection.execute("SELECT COUNT(*) FROM uploads").fetchone()[0])
        case_count = int(connection.execute("SELECT COUNT(*) FROM training_cases").fetchone()[0])
        upload_case_count = int(connection.execute("SELECT COUNT(*) FROM upload_cases").fetchone()[0])
        assert user_row is not None
        assert upload_count == 0
        assert case_count == 0
        assert upload_case_count == 0


def test_last_admin_cannot_delete_own_account(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    response = _post_form(
        client,
        "/dashboard/account/delete",
        data={"current_password": "supersecret", "confirmation": "DELETE MY ACCOUNT"},
    )

    assert response.status_code == 400
    assert "last remaining admin account" in response.text


def test_user_can_delete_own_account_and_related_records(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "owner", "email": "owner@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(client, "/logout", follow_redirects=True)

    _post_form(
        client,
        "/register",
        data={"username": "bob", "email": "bob@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    payload = _valid_payload(case_id="case_account_delete_0001")
    upload_response = _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("delete-me.jsonl", payload, "application/x-ndjson")},
    )
    assert upload_response.status_code == 201
    upload_path = settings.uploads_dir / f"{hashlib.sha256(payload.encode('utf-8')).hexdigest()}.jsonl"

    response = _post_form(
        client,
        "/dashboard/account/delete",
        data={"current_password": "supersecret", "confirmation": "DELETE MY ACCOUNT"},
    )

    assert response.status_code == 303
    assert response.headers.get("location") == "/login?notice=Account+deleted"

    with sqlite3.connect(settings.database_path) as connection:
        bob_row = connection.execute("SELECT id FROM users WHERE username = 'bob'").fetchone()
        bob_sessions = connection.execute("SELECT COUNT(*) FROM sessions WHERE user_id = 2").fetchone()[0]
        bob_uploads = connection.execute("SELECT COUNT(*) FROM uploads WHERE user_id = 2").fetchone()[0]
        audit_rows = connection.execute("SELECT COUNT(*) FROM audit_logs WHERE actor_user_id = 2").fetchone()[0]
        case_count = connection.execute("SELECT COUNT(*) FROM training_cases").fetchone()[0]
        assert bob_row is None
        assert int(bob_sessions) == 0
        assert int(bob_uploads) == 0
        assert int(audit_rows) == 0
        assert int(case_count) == 0

    assert not upload_path.exists()


def test_user_can_request_account_data_export_email(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(
        tmp_path,
        smtp_host="mail.local",
        smtp_port=1025,
        smtp_from_email="no-reply@scamscreener.local",
        smtp_use_starttls=False,
        data_export_cooldown_minutes=60,
    )
    delivered: list[tuple[str, str, str, bytes, int]] = []

    def _fake_send_export_email(
        _settings,
        recipient_email: str,
        requested_at: str,
        archive_name: str,
        archive_bytes: bytes,
        size_bytes: int,
    ) -> None:
        delivered.append((recipient_email, requested_at, archive_name, archive_bytes, size_bytes))

    monkeypatch.setattr("app.training_hub.core.data_exports.send_account_data_export_email", _fake_send_export_email)

    with TestClient(create_app(settings)) as client:
        _post_form(
            client,
            "/register",
            data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
            follow_redirects=True,
        )
        _post_form(
            client,
            "/dashboard/upload",
            files={"training_file": ("export.jsonl", _valid_payload(case_id="case_export_0001"), "application/x-ndjson")},
        )

        response = _post_form(
            client,
            "/dashboard/data-export/request",
            data={"current_password": "supersecret"},
        )

        assert response.status_code == 202
        assert "Account data export requested." in response.text

        timeout_at = time.time() + 2.0
        while time.time() < timeout_at and not delivered:
            time.sleep(0.02)

        assert len(delivered) == 1
        assert delivered[0][0] == "alice@example.com"
        assert delivered[0][2].endswith(".zip")
        assert delivered[0][4] == len(delivered[0][3])

        with zipfile.ZipFile(io.BytesIO(delivered[0][3])) as archive:
            names = set(archive.namelist())
            assert "account-data-export.json" in names
            upload_entries = [name for name in names if name.startswith("uploads/")]
            assert len(upload_entries) == 1
            manifest = json.loads(archive.read("account-data-export.json").decode("utf-8"))
            assert manifest["account"]["username"] == "alice"
            assert manifest["counts"]["uploads"] == 1
            assert manifest["trainingCasesCreatedByAccount"][0]["caseId"] == "case_export_0001"

        timeout_at = time.time() + 2.0
        export_row = None
        audit_row = None
        while time.time() < timeout_at:
            with sqlite3.connect(settings.database_path) as connection:
                export_row = connection.execute(
                    "SELECT status FROM data_export_requests WHERE user_id = 1 ORDER BY id DESC LIMIT 1"
                ).fetchone()
                audit_row = connection.execute(
                    "SELECT id FROM audit_logs WHERE action = 'account.data_export.sent' LIMIT 1"
                ).fetchone()
            if export_row == ("sent",) and audit_row is not None:
                break
            time.sleep(0.02)

        assert export_row == ("sent",)
        assert audit_row is not None


def test_api_client_can_login_upload_and_logout(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(client, "/logout", follow_redirects=True)

    login = client.post(
        "/api/v1/client/auth/login",
        json={"usernameOrEmail": "alice", "password": "supersecret"},
    )
    assert login.status_code == 200
    login_payload = login.json()
    assert login_payload["status"] == "ok"
    token = str(login_payload["sessionToken"])
    assert token

    upload = client.post(
        "/api/v1/client/uploads",
        content=_valid_payload(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-ndjson",
            "X-ScamScreener-Filename": "training-cases-v2.jsonl",
        },
    )
    assert upload.status_code == 201
    upload_payload = upload.json()
    assert upload_payload["status"] == "accepted"
    assert upload_payload["caseCount"] == 1
    assert upload_payload["insertedCases"] == 1
    assert upload_payload["updatedCases"] == 0

    logout = client.post(
        "/api/v1/client/auth/logout",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert logout.status_code == 200
    assert logout.json() == {"status": "ok"}

    revoked = client.post(
        "/api/v1/client/uploads",
        content=_valid_payload(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/x-ndjson"},
    )
    assert revoked.status_code == 401

    with sqlite3.connect(settings.database_path) as connection:
        uploads = int(connection.execute("SELECT COUNT(*) FROM uploads").fetchone()[0])
        cases = int(connection.execute("SELECT COUNT(*) FROM training_cases").fetchone()[0])
        logout_audit = connection.execute(
            "SELECT id FROM audit_logs WHERE action = 'auth.api.logout' LIMIT 1"
        ).fetchone()
        assert uploads == 1
        assert cases == 1
        assert logout_audit is not None


def test_api_client_upload_requires_bearer_session(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))

    response = client.post(
        "/api/v1/client/uploads",
        content=_valid_payload(),
        headers={"Content-Type": "application/x-ndjson"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Bearer session token required."


def test_api_client_login_blocks_admin_accounts_when_mfa_is_required(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        admin_mfa_required=True,
        smtp_host="mail.local",
        smtp_port=1025,
        smtp_from_email="no-reply@scamscreener.local",
        smtp_use_starttls=False,
    )
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(client, "/logout", follow_redirects=True)

    response = client.post(
        "/api/v1/client/auth/login",
        json={"usernameOrEmail": "alice", "password": "supersecret"},
    )

    assert response.status_code == 403
    assert "Use a non-admin account for client uploads." in response.json()["detail"]


def test_first_registered_user_is_admin(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))

    _post_form(
        client,
        "/register",
        data={"username": "dev", "email": "dev@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    admin_page = client.get("/admin")

    assert admin_page.status_code == 200
    assert "Bundle Control" in admin_page.text


def test_admin_page_formats_last_login_timestamp_in_utc(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "dev", "email": "dev@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            "UPDATE users SET last_login_at = ? WHERE username = ?",
            ("2026-03-28T18:00:00Z", "dev"),
        )
        connection.commit()

    admin_page = client.get("/admin")

    assert admin_page.status_code == 200
    assert "2026-03-28 18:00 UTC" in admin_page.text
    assert "2026-03-28T18:00:00Z" not in admin_page.text


def test_admin_page_shows_colored_admin_status_indicators(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "owner", "email": "owner@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(client, "/logout", follow_redirects=True)
    _post_form(
        client,
        "/register",
        data={"username": "bob", "email": "bob@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(client, "/logout", follow_redirects=True)
    _post_form(
        client,
        "/login",
        data={"username_or_email": "owner", "password": "supersecret"},
        follow_redirects=True,
    )

    admin_page = client.get("/admin")

    assert admin_page.status_code == 200
    assert 'class="status-indicator status-indicator-yes"' in admin_page.text
    assert 'class="status-indicator status-indicator-no"' in admin_page.text
    assert 'aria-label="Admin: yes"' in admin_page.text
    assert 'aria-label="Admin: no"' in admin_page.text


def test_non_admin_cannot_access_admin_page(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))

    _post_form(
        client,
        "/register",
        data={"username": "owner", "email": "owner@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(client, "/logout", follow_redirects=True)

    _post_form(
        client,
        "/register",
        data={"username": "player", "email": "player@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    admin_page = client.get("/admin")
    assert admin_page.status_code == 403


def test_registration_closed_mode_blocks_new_users(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path, registration_mode="closed")))
    register = _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
    )
    assert register.status_code == 403
    assert "Registration is currently disabled." in register.text


def test_registration_invite_mode_requires_valid_code(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        registration_mode="invite",
        registration_invite_code="invite-1234",
    )
    client = TestClient(create_app(settings))

    invalid = _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret", "invite_code": "wrong"},
    )
    assert invalid.status_code == 403
    assert "Invalid invite code." in invalid.text

    valid = _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret", "invite_code": "invite-1234"},
        follow_redirects=True,
    )
    assert valid.status_code == 200
    assert "Your Case Contributions" in valid.text


def test_forgot_password_and_reset_flow(tmp_path: Path) -> None:
    settings = _settings(tmp_path, password_reset_show_token=True)
    client = TestClient(create_app(settings))
    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(client, "/logout", follow_redirects=True)

    forgot = _post_form(client, "/forgot-password", data={"username_or_email": "alice"})
    assert forgot.status_code == 200
    assert "If an account exists for that identifier" in forgot.text

    token_match = re.search(r"/reset-password\?token=([A-Za-z0-9_\-\.]+)", forgot.text)
    assert token_match is not None
    token = token_match.group(1)

    reset_form = client.get(f"/reset-password?token={token}")
    assert reset_form.status_code == 200
    assert "Reset Password" in reset_form.text

    reset_done = _post_form(
        client,
        "/reset-password",
        data={"token": token, "new_password": "newsecret123", "new_password_confirm": "newsecret123"},
        follow_redirects=True,
    )
    assert reset_done.status_code == 200
    assert "Password reset successful" in reset_done.text

    old_login = _post_form(
        client,
        "/login",
        data={"username_or_email": "alice", "password": "supersecret"},
    )
    assert old_login.status_code == 401

    new_login = _post_form(
        client,
        "/login",
        data={"username_or_email": "alice", "password": "newsecret123"},
        follow_redirects=True,
    )
    assert new_login.status_code == 200
    assert "Your Case Contributions" in new_login.text

    with sqlite3.connect(settings.database_path) as connection:
        audit = connection.execute("SELECT id FROM audit_logs WHERE action = 'auth.password.reset' LIMIT 1").fetchone()
        assert audit is not None


def test_password_reset_token_is_single_use(tmp_path: Path) -> None:
    settings = _settings(tmp_path, password_reset_show_token=True)
    client = TestClient(create_app(settings))
    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(client, "/logout", follow_redirects=True)

    forgot = _post_form(client, "/forgot-password", data={"username_or_email": "alice"})
    token_match = re.search(r"/reset-password\?token=([A-Za-z0-9_\-\.]+)", forgot.text)
    assert token_match is not None
    token = token_match.group(1)

    first = _post_form(
        client,
        "/reset-password",
        data={"token": token, "new_password": "newsecret123", "new_password_confirm": "newsecret123"},
    )
    assert first.status_code == 303

    second = _post_form(
        client,
        "/reset-password",
        data={"token": token, "new_password": "othersecret123", "new_password_confirm": "othersecret123"},
    )
    assert second.status_code == 400
    assert "invalid or expired" in second.text.lower()


def test_forgot_password_sends_email_when_enabled(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(
        tmp_path,
        password_reset_send_email=True,
        password_reset_show_token=False,
        smtp_host="mail.local",
        smtp_port=1025,
        smtp_from_email="no-reply@scamscreener.local",
        smtp_use_starttls=False,
    )
    client = TestClient(create_app(settings))
    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(client, "/logout", follow_redirects=True)

    sent: list[tuple[str, str]] = []

    def _fake_send_email(_settings, recipient_email: str, reset_link: str, expires_at: str):
        sent.append((recipient_email, reset_link))

    monkeypatch.setattr("app.training_hub.routes.public.send_password_reset_email", _fake_send_email)

    forgot = _post_form(client, "/forgot-password", data={"username_or_email": "alice"})
    assert forgot.status_code == 200
    assert "If an account exists for that identifier" in forgot.text
    assert len(sent) == 1
    assert sent[0][0] == "alice@example.com"
    assert "/reset-password?token=" in sent[0][1]

    with sqlite3.connect(settings.database_path) as connection:
        sent_audit = connection.execute(
            "SELECT id FROM audit_logs WHERE action = 'auth.password.reset.email.sent' LIMIT 1"
        ).fetchone()
        assert sent_audit is not None


def test_forgot_password_uses_public_base_url_for_reset_email(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(
        tmp_path,
        password_reset_send_email=True,
        password_reset_show_token=False,
        smtp_host="mail.local",
        smtp_port=1025,
        smtp_from_email="no-reply@scamscreener.local",
        smtp_use_starttls=False,
        public_base_url="https://scamscreener.example.com",
    )
    client = TestClient(create_app(settings))
    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(client, "/logout", follow_redirects=True)

    sent: list[str] = []

    def _fake_send_email(_settings, _recipient_email: str, reset_link: str, _expires_at: str):
        sent.append(reset_link)

    monkeypatch.setattr("app.training_hub.routes.public.send_password_reset_email", _fake_send_email)

    forgot = _post_form(client, "/forgot-password", data={"username_or_email": "alice"})

    assert forgot.status_code == 200
    assert len(sent) == 1
    assert sent[0].startswith("https://scamscreener.example.com/reset-password?token=")


def test_legal_notice_page_hides_operator_status_badge_for_non_admins(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            _settings(
                tmp_path,
                site_operator_name="Pankraz01 (Tango)",
                site_postal_address="@tango_cgn",
                site_contact_channel="Discord: @tango_cgn",
                public_base_url="https://scamscreener.example.com",
            )
        )
    )

    response = client.get("/legal-notice")

    assert response.status_code == 200
    assert "Pankraz01 (Tango)" in response.text
    assert "Discord: @tango_cgn" in response.text
    assert "Legal Notice" in response.text
    assert "Compliance Warning" in response.text
    assert "serviceable postal address" in response.text
    assert "Operator details incomplete" not in response.text


def test_legal_notice_page_shows_operator_status_badge_for_admins(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        site_operator_name="Pankraz01 (Tango)",
        site_postal_address="42 Example Street, Example City",
        site_contact_channel="Discord: @tango_cgn",
        public_base_url="https://scamscreener.example.com",
    )
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    response = client.get("/legal-notice")

    assert response.status_code == 200
    assert "Operator details configured" in response.text


def test_login_page_shows_minecraft_credential_warning_and_footer_disclaimer(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))

    response = client.get("/login")

    assert response.status_code == 200
    assert "Do NOT enter your Minecraft credentials!" in response.text
    assert "ScamScreener © 2026 Pankraz01" in response.text
    assert "ScamScreener is in no way affiliated with Minecraft, Microsoft, or Mojang." in response.text


def test_privacy_page_lists_us_hosting_and_security_storage(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            _settings(
                tmp_path,
                site_operator_name="Pankraz01 (Tango)",
                site_contact_channel="Discord: @tango_cgn",
                site_privacy_contact="Discord DM: @tango_cgn",
                site_hosting_location="Ashburn, Virginia, USA",
                password_reset_send_email=True,
                smtp_host="smtp.example.com",
                smtp_from_email="no-reply@scamscreener.example.com",
                smtp_use_starttls=True,
            )
        )
    )

    response = client.get("/privacy")

    assert response.status_code == 200
    assert "Privacy Notice" in response.text
    assert "Ashburn, Virginia, USA" in response.text
    assert "training_hub_session" in response.text
    assert "training_hub_csrf" in response.text
    assert "smtp.example.com" in response.text
    assert "Password reset" in response.text


def test_admin_login_requires_mfa_when_enabled(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(
        tmp_path,
        admin_mfa_required=True,
        smtp_host="mail.local",
        smtp_port=1025,
        smtp_from_email="no-reply@scamscreener.local",
        smtp_use_starttls=False,
    )
    client = TestClient(create_app(settings))
    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(client, "/logout", follow_redirects=True)

    delivered_codes: list[tuple[str, str]] = []

    def _fake_send_email(_settings, recipient_email: str, code: str, expires_at: str):
        delivered_codes.append((recipient_email, code))

    monkeypatch.setattr("app.training_hub.routes.public.send_admin_mfa_email", _fake_send_email)

    login = _post_form(
        client,
        "/login",
        data={"username_or_email": "alice", "password": "supersecret"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert login.headers.get("location") == "/admin/mfa"
    assert len(delivered_codes) == 1
    assert delivered_codes[0][0] == "alice@example.com"

    blocked_admin = client.get("/admin", follow_redirects=False)
    assert blocked_admin.status_code == 303
    assert blocked_admin.headers.get("location") == "/login"

    mfa_page = client.get("/admin/mfa")
    assert mfa_page.status_code == 200
    assert "Admin Verification" in mfa_page.text

    verified = _post_form(
        client,
        "/admin/mfa",
        data={"code": delivered_codes[0][1]},
        follow_redirects=False,
    )
    assert verified.status_code == 303
    assert verified.headers.get("location") == "/admin"

    admin_page = client.get("/admin")
    assert admin_page.status_code == 200
    assert "Bundle Control" in admin_page.text

    with sqlite3.connect(settings.database_path) as connection:
        issued_audit = connection.execute(
            "SELECT id FROM audit_logs WHERE action = 'auth.mfa.challenge.issued' LIMIT 1"
        ).fetchone()
        verified_audit = connection.execute(
            "SELECT id FROM audit_logs WHERE action = 'auth.mfa.verified' LIMIT 1"
        ).fetchone()
        assert issued_audit is not None
        assert verified_audit is not None


def test_admin_mfa_rejects_invalid_code(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(
        tmp_path,
        admin_mfa_required=True,
        smtp_host="mail.local",
        smtp_port=1025,
        smtp_from_email="no-reply@scamscreener.local",
        smtp_use_starttls=False,
    )
    client = TestClient(create_app(settings))
    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(client, "/logout", follow_redirects=True)

    delivered_codes: list[str] = []

    def _fake_send_email(_settings, recipient_email: str, code: str, expires_at: str):
        delivered_codes.append(code)

    monkeypatch.setattr("app.training_hub.routes.public.send_admin_mfa_email", _fake_send_email)

    login = _post_form(
        client,
        "/login",
        data={"username_or_email": "alice", "password": "supersecret"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert login.headers.get("location") == "/admin/mfa"
    assert len(delivered_codes) == 1

    wrong_code = "000000" if delivered_codes[0] != "000000" else "999999"
    invalid = _post_form(
        client,
        "/admin/mfa",
        data={"code": wrong_code},
    )
    assert invalid.status_code == 401
    assert "Invalid verification code." in invalid.text

    valid = _post_form(
        client,
        "/admin/mfa",
        data={"code": delivered_codes[0]},
        follow_redirects=False,
    )
    assert valid.status_code == 303
    assert valid.headers.get("location") == "/admin"


def test_admin_mfa_delivery_failure_records_exception_detail(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(
        tmp_path,
        admin_mfa_required=True,
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_from_email="no-reply@scamscreener.local",
        smtp_use_tls=True,
        smtp_use_starttls=False,
    )
    client = TestClient(create_app(settings))
    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(client, "/logout", follow_redirects=True)

    def _failing_send_email(_settings, _recipient_email: str, _code: str, _expires_at: str):
        raise RuntimeError("SMTP AUTH failed")

    monkeypatch.setattr("app.training_hub.routes.public.send_admin_mfa_email", _failing_send_email)

    login = _post_form(
        client,
        "/login",
        data={"username_or_email": "alice", "password": "supersecret"},
    )

    assert login.status_code == 503
    assert "Admin verification code could not be delivered." in login.text

    with sqlite3.connect(settings.database_path) as connection:
        row = connection.execute(
            "SELECT details FROM audit_logs WHERE action = 'auth.mfa.challenge.email.failed' LIMIT 1"
        ).fetchone()
        assert row is not None
        assert "SMTP AUTH failed" in str(row[0])


def test_admin_mfa_challenge_is_bound_to_client(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(
        tmp_path,
        admin_mfa_required=True,
        smtp_host="mail.local",
        smtp_port=1025,
        smtp_from_email="no-reply@scamscreener.local",
        smtp_use_starttls=False,
        trusted_proxies={"testclient"},
    )
    client = TestClient(create_app(settings))
    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        headers={"X-Forwarded-For": "1.1.1.1", "User-Agent": "ScamScreenerAgent-A"},
        follow_redirects=True,
    )
    _post_form(
        client,
        "/logout",
        headers={"X-Forwarded-For": "1.1.1.1", "User-Agent": "ScamScreenerAgent-A"},
        follow_redirects=True,
    )

    delivered_codes: list[str] = []

    def _fake_send_email(_settings, recipient_email: str, code: str, expires_at: str):
        delivered_codes.append(code)

    monkeypatch.setattr("app.training_hub.routes.public.send_admin_mfa_email", _fake_send_email)

    login = _post_form(
        client,
        "/login",
        data={"username_or_email": "alice", "password": "supersecret"},
        headers={"X-Forwarded-For": "1.1.1.1", "User-Agent": "ScamScreenerAgent-A"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert login.headers.get("location") == "/admin/mfa"
    assert len(delivered_codes) == 1

    mismatch = _post_form(
        client,
        "/admin/mfa",
        data={"code": delivered_codes[0]},
        headers={"X-Forwarded-For": "2.2.2.2", "User-Agent": "ScamScreenerAgent-A"},
        follow_redirects=False,
    )
    assert mismatch.status_code == 303
    assert mismatch.headers.get("location", "").startswith("/login?")

    still_blocked = client.get("/admin", follow_redirects=False)
    assert still_blocked.status_code == 303
    assert still_blocked.headers.get("location") == "/login"


def test_admin_mfa_max_attempts_expires_challenge(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(
        tmp_path,
        admin_mfa_required=True,
        admin_mfa_max_attempts=2,
        smtp_host="mail.local",
        smtp_port=1025,
        smtp_from_email="no-reply@scamscreener.local",
        smtp_use_starttls=False,
    )
    client = TestClient(create_app(settings))
    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(client, "/logout", follow_redirects=True)

    delivered_codes: list[str] = []

    def _fake_send_email(_settings, recipient_email: str, code: str, expires_at: str):
        delivered_codes.append(code)

    monkeypatch.setattr("app.training_hub.routes.public.send_admin_mfa_email", _fake_send_email)

    login = _post_form(
        client,
        "/login",
        data={"username_or_email": "alice", "password": "supersecret"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert login.headers.get("location") == "/admin/mfa"
    assert len(delivered_codes) == 1

    first_wrong = _post_form(client, "/admin/mfa", data={"code": "000000"})
    assert first_wrong.status_code == 401
    assert "Invalid verification code." in first_wrong.text

    second_wrong = _post_form(client, "/admin/mfa", data={"code": "999999"}, follow_redirects=False)
    assert second_wrong.status_code == 303
    assert second_wrong.headers.get("location", "").startswith("/login?")

    blocked = _post_form(
        client,
        "/admin/mfa",
        data={"code": delivered_codes[0]},
        follow_redirects=False,
    )
    assert blocked.status_code == 303
    assert blocked.headers.get("location", "").startswith("/login?")


def test_upload_rejects_invalid_payload(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    invalid_payload = '{"format":"training_case_v2","schemaVersion":2}'
    upload = _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("training-cases-v2.jsonl", invalid_payload, "application/x-ndjson")},
    )
    assert upload.status_code == 400
    assert "missing caseId" in upload.text


def test_upload_rejects_file_above_max_bytes(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path, max_upload_bytes=128)))
    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    too_large_payload = (_valid_payload() + "\n") * 2
    upload = _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("training-cases-v2.jsonl", too_large_payload, "application/x-ndjson")},
    )
    assert upload.status_code == 413
    assert "File exceeds limit" in upload.text


def test_admin_bundle_creation_creates_audit_log(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "dev", "email": "dev@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("training-cases-v2.jsonl", _valid_payload(), "application/x-ndjson")},
    )

    run = _post_form(client, "/admin/train")
    assert run.status_code == 200
    assert "Training bundle built successfully." in run.text
    assert "/admin/runs/1/bundle" in run.text

    with sqlite3.connect(settings.database_path) as connection:
        row = connection.execute("SELECT status, upload_count, case_count FROM training_runs LIMIT 1").fetchone()
        assert row is not None
        assert row[0] == "prepared"
        assert row[1] == 1
        assert row[2] == 1

        audit = connection.execute(
            "SELECT action FROM audit_logs WHERE action = 'training.bundle.prepared' LIMIT 1"
        ).fetchone()
        assert audit is not None

    bundle = client.get("/admin/runs/1/bundle")
    assert bundle.status_code == 200
    assert bundle.text.strip() == _valid_payload()

    with sqlite3.connect(settings.database_path) as connection:
        bundle_download_audit = connection.execute(
            "SELECT action FROM audit_logs WHERE action = 'training.bundle.download' LIMIT 1"
        ).fetchone()
        assert bundle_download_audit is not None


def test_admin_user_management_grant_and_revoke(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "owner", "email": "owner@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(client, "/logout", follow_redirects=True)
    _post_form(
        client,
        "/register",
        data={"username": "bob", "email": "bob@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(client, "/logout", follow_redirects=True)
    _post_form(
        client,
        "/login",
        data={"username_or_email": "owner", "password": "supersecret"},
        follow_redirects=True,
    )

    with sqlite3.connect(settings.database_path) as connection:
        bob_id = int(connection.execute("SELECT id FROM users WHERE username = 'bob'").fetchone()[0])

    grant = _post_form(client, f"/admin/users/{bob_id}/admin", data={"action": "grant"}, follow_redirects=True)
    assert grant.status_code == 200
    assert "Granted admin to bob." in grant.text

    revoke = _post_form(client, f"/admin/users/{bob_id}/admin", data={"action": "revoke"}, follow_redirects=True)
    assert revoke.status_code == 200
    assert "Revoked admin from bob." in revoke.text

    with sqlite3.connect(settings.database_path) as connection:
        is_admin = int(connection.execute("SELECT is_admin FROM users WHERE id = ?", (bob_id,)).fetchone()[0])
        assert is_admin == 0
        grant_audit = connection.execute(
            "SELECT id FROM audit_logs WHERE action = 'user.admin.grant' AND target_id = ?",
            (bob_id,),
        ).fetchone()
        revoke_audit = connection.execute(
            "SELECT id FROM audit_logs WHERE action = 'user.admin.revoke' AND target_id = ?",
            (bob_id,),
        ).fetchone()
        assert grant_audit is not None
        assert revoke_audit is not None


def test_admin_page_shows_case_list_and_audit_log(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "dev", "email": "dev@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("training-cases-v2.jsonl", _valid_payload(), "application/x-ndjson")},
    )

    admin_page = client.get("/admin")
    assert admin_page.status_code == 200
    assert "Basic Case List" in admin_page.text
    assert "/admin/cases/1" in admin_page.text
    assert "Audit Log" in admin_page.text
    assert "upload.accepted" in admin_page.text


def test_admin_case_detail_page_is_readable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "dev", "email": "dev@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("training-cases-v2.jsonl", _valid_payload(), "application/x-ndjson")},
    )

    detail = client.get("/admin/cases/1")
    assert detail.status_code == 200
    assert "Case Detail" in detail.text
    assert "Observed Pipeline" in detail.text
    assert "Conversation" in detail.text
    assert "Stage Results" in detail.text
    assert "case_000001" in detail.text


def test_admin_can_delete_case_from_table(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "dev", "email": "dev@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("training-cases-v2.jsonl", _valid_payload(), "application/x-ndjson")},
    )

    deleted = _post_form(client, "/admin/cases/1/delete", data={"return_to": "admin"})
    assert deleted.status_code == 200
    assert "Deleted case case_000001." in deleted.text

    with sqlite3.connect(settings.database_path) as connection:
        case_count = int(connection.execute("SELECT COUNT(*) FROM training_cases").fetchone()[0])
        assert case_count == 0
        audit = connection.execute("SELECT id FROM audit_logs WHERE action = 'case.delete' LIMIT 1").fetchone()
        assert audit is not None


def test_admin_can_delete_case_from_detail_page(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "dev", "email": "dev@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("training-cases-v2.jsonl", _valid_payload(), "application/x-ndjson")},
    )

    deleted = _post_form(
        client,
        "/admin/cases/1/delete",
        data={"return_to": "detail"},
        follow_redirects=True,
    )
    assert deleted.status_code == 200
    assert "Deleted case case_000001." in deleted.text
    assert "Basic Case List" in deleted.text

    with sqlite3.connect(settings.database_path) as connection:
        case_count = int(connection.execute("SELECT COUNT(*) FROM training_cases").fetchone()[0])
        assert case_count == 0


def test_admin_can_create_and_restore_backup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "dev", "email": "dev@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("training-cases-v2.jsonl", _valid_payload(), "application/x-ndjson")},
    )

    backup = _post_form(client, "/admin/backups/create", follow_redirects=False)
    assert backup.status_code == 200
    backup_payload = backup.content
    assert len(backup_payload) > 0

    deleted = _post_form(client, "/admin/cases/1/delete", data={"return_to": "admin"})
    assert deleted.status_code == 200
    assert "Deleted case case_000001." in deleted.text

    restore = _post_form(
        client,
        "/admin/backups/restore",
        files={"backup_file": ("training-hub-backup.tar.gz", backup_payload, "application/gzip")},
    )
    assert restore.status_code == 200
    assert "Backup restore completed successfully." in restore.text

    with sqlite3.connect(settings.database_path) as connection:
        uploads = int(connection.execute("SELECT COUNT(*) FROM uploads").fetchone()[0])
        cases = int(connection.execute("SELECT COUNT(*) FROM training_cases").fetchone()[0])
        restored_audit = connection.execute(
            "SELECT id FROM audit_logs WHERE action = 'backup.restored' LIMIT 1"
        ).fetchone()
        assert uploads == 1
        assert cases == 1
        assert restored_audit is not None


def test_metrics_endpoint_exposes_prometheus_values(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("training-cases-v2.jsonl", _valid_payload(), "application/x-ndjson")},
    )

    metrics = client.get("/api/v1/metrics")
    assert metrics.status_code == 200
    assert "scamscreener_users_total 1" in metrics.text
    assert "scamscreener_uploads_total 1" in metrics.text
    assert "scamscreener_training_cases_total 1" in metrics.text
    assert "scamscreener_security_alert_failed_login_spike 0" in metrics.text


def test_failed_login_spike_raises_security_alert(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        security_alert_failed_login_threshold=1,
    )
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(client, "/logout", follow_redirects=True)

    failed = _post_form(
        client,
        "/login",
        data={"username_or_email": "alice", "password": "wrong-password"},
    )
    assert failed.status_code == 401

    with sqlite3.connect(settings.database_path) as connection:
        alert = connection.execute(
            "SELECT details FROM audit_logs WHERE action = 'security.alert.raised' LIMIT 1"
        ).fetchone()
        assert alert is not None
        assert "signal=auth.login.failed;" in str(alert[0])

    metrics = client.get("/api/v1/metrics")
    assert metrics.status_code == 200
    assert "scamscreener_security_alert_failed_login_spike 1" in metrics.text


def test_security_headers_are_applied(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("x-frame-options") == "DENY"
    assert response.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert response.headers.get("cross-origin-opener-policy") == "same-origin"
    assert response.headers.get("cross-origin-resource-policy") == "same-origin"
    assert response.headers.get("x-permitted-cross-domain-policies") == "none"
    assert response.headers.get("permissions-policy") == "geolocation=(), microphone=(), camera=(), payment=(), usb=()"
    assert "content-security-policy" in response.headers
    assert "strict-transport-security" not in response.headers


def test_register_rejects_invalid_csrf_token(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    token = _csrf_token(client)

    response = client.post(
        "/register",
        headers={"Origin": "http://testserver", "Referer": "http://testserver/register"},
        data={
            "username": "alice",
            "email": "alice@example.com",
            "password": "supersecret",
            "csrf_token": token + "-tampered",
        },
    )
    assert response.status_code == 403


def test_register_rejects_cross_site_origin(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    token = _csrf_token(client)
    response = client.post(
        "/register",
        headers={"Origin": "http://evil.example"},
        data={
            "username": "alice",
            "email": "alice@example.com",
            "password": "supersecret",
            "csrf_token": token,
        },
    )
    assert response.status_code == 403
    assert "Invalid request origin." in response.text


def test_register_allows_post_when_origin_check_disabled(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path, enforce_origin_check=False)))
    token = _csrf_token(client)
    response = client.post(
        "/register",
        headers={"Origin": "http://evil.example"},
        data={
            "username": "alice",
            "email": "alice@example.com",
            "password": "supersecret",
            "csrf_token": token,
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Your Case Contributions" in response.text


def test_login_rate_limit_returns_429(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))

    last_response = None
    for _ in range(13):
        last_response = _post_form(
            client,
            "/login",
            data={"username_or_email": "ghost", "password": "wrong-password"},
        )

    assert last_response is not None
    assert last_response.status_code == 429


def test_rate_limit_ignores_untrusted_forwarded_for(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path, trusted_proxies=set())))

    last_response = None
    for index in range(13):
        last_response = _post_form(
            client,
            "/login",
            data={"username_or_email": "ghost", "password": "wrong-password"},
            headers={"X-Forwarded-For": f"10.0.0.{index}"},
        )

    assert last_response is not None
    assert last_response.status_code == 429


def test_failed_login_for_known_user_writes_audit_log(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))
    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(client, "/logout", follow_redirects=True)

    failed = _post_form(
        client,
        "/login",
        data={"username_or_email": "alice", "password": "wrong-password"},
    )
    assert failed.status_code == 401

    with sqlite3.connect(settings.database_path) as connection:
        audit = connection.execute(
            "SELECT id FROM audit_logs WHERE action = 'auth.login.failed' LIMIT 1"
        ).fetchone()
        assert audit is not None


def test_account_lockout_triggers_after_repeated_wrong_password(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))
    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(client, "/logout", follow_redirects=True)

    fifth = None
    for _ in range(5):
        fifth = _post_form(
            client,
            "/login",
            data={"username_or_email": "alice", "password": "wrong-password"},
        )

    assert fifth is not None
    assert fifth.status_code == 429

    correct_while_locked = _post_form(
        client,
        "/login",
        data={"username_or_email": "alice", "password": "supersecret"},
    )
    assert correct_while_locked.status_code == 429

    with sqlite3.connect(settings.database_path) as connection:
        audit = connection.execute(
            "SELECT id FROM audit_logs WHERE action = 'auth.login.locked' LIMIT 1"
        ).fetchone()
        assert audit is not None


def test_logout_revokes_server_side_session(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    old_session = client.cookies.get("training_hub_session")
    assert old_session

    _post_form(client, "/logout", follow_redirects=True)
    client.cookies.set("training_hub_session", old_session)
    dashboard = client.get("/dashboard", follow_redirects=False)

    assert dashboard.status_code == 303
    assert dashboard.headers.get("location") == "/login"


def test_user_can_change_password(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))
    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    changed = _post_form(
        client,
        "/dashboard/password",
        data={
            "current_password": "supersecret",
            "new_password": "newsecret123",
            "new_password_confirm": "newsecret123",
        },
    )
    assert changed.status_code == 200
    assert "Password updated successfully." in changed.text

    _post_form(client, "/logout", follow_redirects=True)

    old_login = _post_form(
        client,
        "/login",
        data={"username_or_email": "alice", "password": "supersecret"},
    )
    assert old_login.status_code == 401

    new_login = _post_form(
        client,
        "/login",
        data={"username_or_email": "alice", "password": "newsecret123"},
        follow_redirects=True,
    )
    assert new_login.status_code == 200
    assert "Your Case Contributions" in new_login.text

    with sqlite3.connect(settings.database_path) as connection:
        audit = connection.execute(
            "SELECT id FROM audit_logs WHERE action = 'auth.password.changed' LIMIT 1"
        ).fetchone()
        assert audit is not None


def test_password_change_revokes_other_sessions(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    owner_client = TestClient(app)
    second_client = TestClient(app)

    _post_form(
        owner_client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(
        second_client,
        "/login",
        data={"username_or_email": "alice", "password": "supersecret"},
        follow_redirects=True,
    )

    changed = _post_form(
        owner_client,
        "/dashboard/password",
        data={
            "current_password": "supersecret",
            "new_password": "newsecret123",
            "new_password_confirm": "newsecret123",
        },
    )
    assert changed.status_code == 200

    second_dashboard = second_client.get("/dashboard", follow_redirects=False)
    assert second_dashboard.status_code == 303
    assert second_dashboard.headers.get("location") == "/login"


def test_user_can_revoke_other_sessions(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    app = create_app(settings)
    owner_client = TestClient(app)
    second_client = TestClient(app)

    _post_form(
        owner_client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(
        second_client,
        "/login",
        data={"username_or_email": "alice", "password": "supersecret"},
        follow_redirects=True,
    )

    revoke = _post_form(second_client, "/dashboard/sessions/revoke-others")
    assert revoke.status_code == 200
    assert "Revoked 1 other sessions." in revoke.text

    dashboard = owner_client.get("/dashboard", follow_redirects=False)
    assert dashboard.status_code == 303
    assert dashboard.headers.get("location") == "/login"


def test_session_bind_ip_revokes_session_on_ip_change(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        session_bind_ip=True,
        trusted_proxies={"testclient"},
    )
    client = TestClient(create_app(settings))

    registered = _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        headers={"X-Forwarded-For": "1.1.1.1"},
        follow_redirects=False,
    )
    assert registered.status_code == 303

    ok_dashboard = client.get("/dashboard", headers={"X-Forwarded-For": "1.1.1.1"})
    assert ok_dashboard.status_code == 200

    changed_ip = client.get("/dashboard", headers={"X-Forwarded-For": "2.2.2.2"}, follow_redirects=False)
    assert changed_ip.status_code == 303
    assert changed_ip.headers.get("location") == "/login"


def test_session_bind_user_agent_revokes_session_on_agent_change(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        session_bind_user_agent=True,
    )
    client = TestClient(create_app(settings))

    registered = _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        headers={"User-Agent": "ScamScreenerAgent-A"},
        follow_redirects=False,
    )
    assert registered.status_code == 303

    ok_dashboard = client.get("/dashboard", headers={"User-Agent": "ScamScreenerAgent-A"})
    assert ok_dashboard.status_code == 200

    changed_agent = client.get("/dashboard", headers={"User-Agent": "ScamScreenerAgent-B"}, follow_redirects=False)
    assert changed_agent.status_code == 303
    assert changed_agent.headers.get("location") == "/login"


def test_upload_daily_quota_by_user_is_enforced(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path, max_uploads_per_day_per_user=1)))
    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    first = _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("training-cases-v2.jsonl", _valid_payload(), "application/x-ndjson")},
    )
    assert first.status_code == 201

    second_payload = _valid_payload().replace("case_000001", "case_000002")
    second = _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("training-cases-v2-2.jsonl", second_payload, "application/x-ndjson")},
    )
    assert second.status_code == 429
    assert "Daily upload count limit reached for your account." in second.text


def test_https_enforcement_redirects_when_enabled(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path, enforce_https=True)))
    response = client.get("/api/v1/health", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers.get("location", "").startswith("https://testserver/")
    assert response.headers.get("strict-transport-security") == "max-age=31536000; includeSubDomains"


def test_https_enforcement_ignores_untrusted_forwarded_proto(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path, enforce_https=True, trusted_proxies=set())))
    response = client.get("/api/v1/health", headers={"X-Forwarded-Proto": "https"}, follow_redirects=False)

    assert response.status_code == 307
    assert response.headers.get("location", "").startswith("https://testserver/")


def test_https_enforcement_respects_trusted_forwarded_proto(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path, enforce_https=True, trusted_proxies={"testclient"})))
    response = client.get("/api/v1/health", headers={"X-Forwarded-Proto": "https"}, follow_redirects=False)

    assert response.status_code == 200
    assert response.headers.get("strict-transport-security") == "max-age=31536000; includeSubDomains"


def test_https_enforcement_respects_trusted_proxy_cidr(tmp_path: Path) -> None:
    from app.training_hub.core.common import _is_request_from_trusted_proxy

    request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))

    assert _is_request_from_trusted_proxy(request, {"127.0.0.0/8"}) is True


def test_upload_download_rejects_path_outside_upload_dir(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("training-cases-v2.jsonl", _valid_payload(), "application/x-ndjson")},
    )

    outside_path = tmp_path / "outside-upload.jsonl"
    outside_path.write_text(_valid_payload(), encoding="utf-8")
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("UPDATE uploads SET stored_path = ? WHERE id = 1", (str(outside_path),))
        connection.commit()

    response = client.get("/dashboard/uploads/1/download")
    assert response.status_code == 403


def test_upload_download_writes_audit_log(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("training-cases-v2.jsonl", _valid_payload(), "application/x-ndjson")},
    )

    response = client.get("/dashboard/uploads/1/download")
    assert response.status_code == 200

    with sqlite3.connect(settings.database_path) as connection:
        audit = connection.execute(
            "SELECT action FROM audit_logs WHERE action = 'upload.download' LIMIT 1"
        ).fetchone()
        assert audit is not None


def test_upload_download_rate_limit_returns_429(tmp_path: Path) -> None:
    settings = _settings(tmp_path, max_upload_downloads_per_minute_per_user=1)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("training-cases-v2.jsonl", _valid_payload(), "application/x-ndjson")},
    )

    first = client.get("/dashboard/uploads/1/download")
    assert first.status_code == 200

    second = client.get("/dashboard/uploads/1/download")
    assert second.status_code == 429


def test_admin_bundle_download_rejects_path_outside_bundle_dir(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "dev", "email": "dev@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("training-cases-v2.jsonl", _valid_payload(), "application/x-ndjson")},
    )
    _post_form(client, "/admin/train")

    outside_path = tmp_path / "outside-bundle.jsonl"
    outside_path.write_text(_valid_payload(), encoding="utf-8")
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("UPDATE training_runs SET bundle_path = ? WHERE id = 1", (str(outside_path),))
        connection.commit()

    response = client.get("/admin/runs/1/bundle")
    assert response.status_code == 403


def test_admin_bundle_download_rate_limit_returns_429(tmp_path: Path) -> None:
    settings = _settings(tmp_path, max_bundle_downloads_per_minute_per_user=1)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "dev", "email": "dev@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("training-cases-v2.jsonl", _valid_payload(), "application/x-ndjson")},
    )
    _post_form(client, "/admin/train")

    first = client.get("/admin/runs/1/bundle")
    assert first.status_code == 200

    second = client.get("/admin/runs/1/bundle")
    assert second.status_code == 429


def test_bootstrap_registration_requires_allowlist_when_empty_db(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path, admin_usernames=set())))

    response = _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
    )
    assert response.status_code == 503
    assert "bootstrap is locked" in response.text.lower()


def test_admin_retention_cleanup_prunes_old_rows_and_files(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        retention_sessions_days=1,
        retention_password_reset_days=1,
        retention_audit_logs_days=1,
        retention_uploads_days=1,
        retention_bundles_days=1,
        retention_rate_limit_days=1,
    )
    client = TestClient(create_app(settings))
    _post_form(
        client,
        "/register",
        data={"username": "dev", "email": "dev@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    old_iso = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat().replace("+00:00", "Z")
    old_bucket = int((datetime.now(timezone.utc) - timedelta(days=10)).timestamp())

    old_upload_path = settings.uploads_dir / "retention-old-upload.jsonl"
    old_upload_path.write_text(_valid_payload(), encoding="utf-8")
    old_bundle_path = settings.bundles_dir / "retention-old-bundle.jsonl"
    old_bundle_path.write_text(_valid_payload(), encoding="utf-8")

    with sqlite3.connect(settings.database_path) as connection:
        upload_cursor = connection.execute(
            """
            INSERT INTO uploads (
                created_at, user_id, original_file_name, stored_path, payload_sha256,
                case_count, size_bytes, status, duplicate_of_upload_id, source_ip
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                old_iso,
                1,
                "retention-old-upload.jsonl",
                str(old_upload_path),
                hashlib.sha256(b"retention-old-upload").hexdigest(),
                1,
                len(_valid_payload()),
                "accepted",
                None,
                "127.0.0.1",
            ),
        )
        old_upload_id = int(upload_cursor.lastrowid)
        case_cursor = connection.execute(
            """
            INSERT INTO training_cases (
                case_id, created_at, updated_at, created_by_user_id, source_upload_id,
                status, label, outcome, tag_ids_json, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "case_retention_0001",
                old_iso,
                old_iso,
                1,
                old_upload_id,
                "submitted",
                "risk",
                "review",
                "[]",
                "{}",
            ),
        )
        old_case_id = int(case_cursor.lastrowid)

        run_cursor = connection.execute(
            """
            INSERT INTO training_runs (
                created_at, started_by_user_id, upload_count, case_count, status, command, bundle_path, output_log
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                old_iso,
                1,
                1,
                1,
                "prepared",
                "",
                str(old_bundle_path),
                "old run",
            ),
        )
        old_run_id = int(run_cursor.lastrowid)

        connection.execute(
            """
            INSERT INTO sessions (created_at, user_id, token_sha256, expires_at, revoked_at, remote_addr, user_agent, revoke_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (old_iso, 1, "retention_old_session_token", old_iso, old_iso, "127.0.0.1", "pytest", "old"),
        )
        connection.execute(
            """
            INSERT INTO password_reset_tokens (created_at, user_id, token_sha256, expires_at, consumed_at, source_ip, user_agent)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                old_iso,
                1,
                hashlib.sha256(b"retention_old_reset_token").hexdigest(),
                old_iso,
                old_iso,
                "127.0.0.1",
                "pytest",
            ),
        )
        connection.execute(
            """
            INSERT INTO admin_mfa_challenges (
                created_at, user_id, token_sha256, code_sha256, expires_at, consumed_at, source_ip, user_agent
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                old_iso,
                1,
                hashlib.sha256(b"retention_old_mfa_token").hexdigest(),
                hashlib.sha256(b"retention_old_mfa_code").hexdigest(),
                old_iso,
                old_iso,
                "127.0.0.1",
                "pytest",
            ),
        )
        connection.execute(
            """
            INSERT INTO audit_logs (created_at, actor_user_id, action, target_type, target_id, details, source_ip, user_agent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (old_iso, 1, "retention.test.old", "system", None, "old audit", "127.0.0.1", "pytest"),
        )
        connection.execute(
            "INSERT INTO rate_limit_hits (bucket_key, bucket_start, count, updated_at) VALUES (?, ?, ?, ?)",
            ("retention.test.rate", old_bucket, 1, str(old_bucket)),
        )
        connection.commit()

    run_cleanup = _post_form(client, "/admin/retention/run")
    assert run_cleanup.status_code == 200
    assert "Retention cleanup completed." in run_cleanup.text

    with sqlite3.connect(settings.database_path) as connection:
        upload_row = connection.execute("SELECT id FROM uploads WHERE id = ?", (old_upload_id,)).fetchone()
        run_row = connection.execute("SELECT id FROM training_runs WHERE id = ?", (old_run_id,)).fetchone()
        case_row = connection.execute("SELECT source_upload_id FROM training_cases WHERE id = ?", (old_case_id,)).fetchone()
        session_row = connection.execute(
            "SELECT id FROM sessions WHERE token_sha256 = 'retention_old_session_token'"
        ).fetchone()
        token_row = connection.execute(
            "SELECT id FROM password_reset_tokens WHERE token_sha256 = ?",
            (hashlib.sha256(b"retention_old_reset_token").hexdigest(),),
        ).fetchone()
        mfa_row = connection.execute(
            "SELECT id FROM admin_mfa_challenges WHERE token_sha256 = ?",
            (hashlib.sha256(b"retention_old_mfa_token").hexdigest(),),
        ).fetchone()
        audit_row = connection.execute("SELECT id FROM audit_logs WHERE action = 'retention.test.old'").fetchone()
        rate_row = connection.execute(
            "SELECT 1 FROM rate_limit_hits WHERE bucket_key = 'retention.test.rate'"
        ).fetchone()

        assert upload_row is None
        assert run_row is None
        assert case_row is not None and case_row[0] is None
        assert session_row is None
        assert token_row is None
        assert mfa_row is None
        assert audit_row is None
        assert rate_row is None

    assert not old_upload_path.exists()
    assert not old_bundle_path.exists()


def test_auto_retention_worker_runs_when_enabled(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(
        tmp_path,
        retention_auto_enabled=True,
        retention_auto_interval_minutes=1,
    )

    cleanup_calls: list[int] = []

    def _fake_cleanup(_settings: TrainingHubSettings):
        cleanup_calls.append(1)
        return {
            "sessions": 0,
            "password_reset_tokens": 0,
            "admin_mfa_challenges": 0,
            "audit_logs": 0,
            "uploads": 0,
            "bundles": 0,
            "rate_limit_hits": 0,
        }

    monkeypatch.setattr("app.training_hub.main._run_retention_cleanup", _fake_cleanup)

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        timeout_at = time.time() + 1.0
        while time.time() < timeout_at and not cleanup_calls:
            time.sleep(0.02)

    assert cleanup_calls


def _settings(
    tmp_path: Path,
    enforce_https: bool = False,
    trusted_proxies: set[str] | None = None,
    admin_usernames: set[str] | None = None,
    enforce_origin_check: bool = True,
    max_uploads_per_day_per_user: int = 40,
    max_upload_bytes: int = 1024 * 1024,
    session_bind_ip: bool = False,
    session_bind_user_agent: bool = False,
    max_upload_downloads_per_minute_per_user: int = 60,
    max_bundle_downloads_per_minute_per_user: int = 30,
    data_export_cooldown_minutes: int = 60,
    data_export_max_archive_bytes: int = 20 * 1024 * 1024,
    registration_mode: str = "open",
    registration_invite_code: str = "",
    password_reset_ttl_minutes: int = 30,
    password_reset_show_token: bool = False,
    password_reset_send_email: bool = False,
    smtp_host: str = "",
    smtp_port: int = 587,
    smtp_username: str = "",
    smtp_password: str = "",
    smtp_from_email: str = "",
    smtp_use_tls: bool = False,
    smtp_use_starttls: bool = False,
    public_base_url: str = "",
    admin_mfa_required: bool = False,
    admin_mfa_ttl_minutes: int = 30,
    admin_mfa_max_attempts: int = 5,
    retention_sessions_days: int = 30,
    retention_password_reset_days: int = 7,
    retention_audit_logs_days: int = 180,
    retention_uploads_days: int = 365,
    retention_bundles_days: int = 365,
    retention_rate_limit_days: int = 7,
    retention_auto_enabled: bool = False,
    retention_auto_interval_minutes: int = 1440,
    backup_restore_max_bytes: int = 512 * 1024 * 1024,
    security_alert_window_minutes: int = 15,
    security_alert_cooldown_minutes: int = 15,
    security_alert_failed_login_threshold: int = 10,
    security_alert_mfa_failed_threshold: int = 6,
    security_alert_password_reset_threshold: int = 10,
    site_project_classification: str = "Private non-commercial community project",
    site_operator_name: str = "",
    site_postal_address: str = "",
    site_contact_channel: str = "",
    site_privacy_contact: str = "",
    site_hosting_location: str = "Ashburn, Virginia, USA",
) -> TrainingHubSettings:
    default_admin_usernames = {"alice", "dev", "owner"}
    return TrainingHubSettings(
        host="127.0.0.1",
        port=18080,
        database_url="",
        secret_key="test-secret-key-for-security-check-123456",
        session_ttl_minutes=240,
        max_upload_bytes=max_upload_bytes,
        storage_dir=tmp_path / "data",
        pipeline_command="",
        project_root=tmp_path,
        admin_emails=set(),
        admin_usernames=admin_usernames if admin_usernames is not None else default_admin_usernames,
        trusted_proxies=trusted_proxies if trusted_proxies is not None else set(),
        public_base_url=public_base_url,
        registration_mode=registration_mode,
        registration_invite_code=registration_invite_code,
        password_reset_ttl_minutes=password_reset_ttl_minutes,
        password_reset_show_token=password_reset_show_token,
        password_reset_send_email=password_reset_send_email,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_username=smtp_username,
        smtp_password=smtp_password,
        smtp_from_email=smtp_from_email,
        smtp_use_tls=smtp_use_tls,
        smtp_use_starttls=smtp_use_starttls,
        admin_mfa_required=admin_mfa_required,
        admin_mfa_ttl_minutes=admin_mfa_ttl_minutes,
        admin_mfa_max_attempts=admin_mfa_max_attempts,
        enforce_https=enforce_https,
        enable_rate_limit=True,
        enforce_origin_check=enforce_origin_check,
        session_bind_ip=session_bind_ip,
        session_bind_user_agent=session_bind_user_agent,
        max_upload_downloads_per_minute_per_user=max_upload_downloads_per_minute_per_user,
        max_bundle_downloads_per_minute_per_user=max_bundle_downloads_per_minute_per_user,
        data_export_cooldown_minutes=data_export_cooldown_minutes,
        data_export_max_archive_bytes=data_export_max_archive_bytes,
        max_uploads_per_day_per_user=max_uploads_per_day_per_user,
        retention_sessions_days=retention_sessions_days,
        retention_password_reset_days=retention_password_reset_days,
        retention_audit_logs_days=retention_audit_logs_days,
        retention_uploads_days=retention_uploads_days,
        retention_bundles_days=retention_bundles_days,
        retention_rate_limit_days=retention_rate_limit_days,
        retention_auto_enabled=retention_auto_enabled,
        retention_auto_interval_minutes=retention_auto_interval_minutes,
        backup_restore_max_bytes=backup_restore_max_bytes,
        security_alert_window_minutes=security_alert_window_minutes,
        security_alert_cooldown_minutes=security_alert_cooldown_minutes,
        security_alert_failed_login_threshold=security_alert_failed_login_threshold,
        security_alert_mfa_failed_threshold=security_alert_mfa_failed_threshold,
        security_alert_password_reset_threshold=security_alert_password_reset_threshold,
        site_project_classification=site_project_classification,
        site_operator_name=site_operator_name,
        site_postal_address=site_postal_address,
        site_contact_channel=site_contact_channel,
        site_privacy_contact=site_privacy_contact,
        site_hosting_location=site_hosting_location,
    )


def _csrf_token(client: TestClient) -> str:
    token = client.cookies.get(CSRF_COOKIE_NAME)
    if token:
        return str(token)
    client.get("/login")
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token is not None
    return str(token)


def _post_form(
    client: TestClient,
    path: str,
    data: dict[str, str] | None = None,
    files: dict[str, tuple[str, str, str]] | None = None,
    headers: dict[str, str] | None = None,
    follow_redirects: bool = False,
):
    form_data = dict(data or {})
    form_data.setdefault("csrf_token", _csrf_token(client))
    request_headers = {
        "Origin": "http://testserver",
        "Referer": f"http://testserver{path}",
    }
    if headers:
        request_headers.update(headers)
    return client.post(path, data=form_data, files=files, headers=request_headers, follow_redirects=follow_redirects)


def _valid_payload(
    case_id: str = "case_000001",
    label: str = "risk",
    outcome: str = "review",
) -> str:
    return (
        f'{{"format":"training_case_v2","schemaVersion":2,"caseId":"{case_id}",'
        f'"caseData":{{"label":"{label}","messages":[],"caseSignalTagIds":[]}},'
        f'"observedPipeline":{{"scoreAtCapture":0,"outcomeAtCapture":"{outcome}","decidedByStageId":"stage.rule","stageResults":[]}},'
        '"supervision":{"contextStage":{"targetLabel":"risk","signalMessageIndices":[],"contextMessageIndices":[],"excludedMessageIndices":[],"targetSignalTagIds":[]},'
        '"fixedStageCalibrations":[]}}'
    )
