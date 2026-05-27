import hashlib
import io
import json
import re
import sys
import time
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from starlette.requests import Request

sys.path.append(str(Path(__file__).resolve().parents[1]))

from app.training_hub.infra import db as training_db
from app.training_hub.core.mfa import (
    _generate_passkey_auth_options,
    _generate_passkey_registration_options,
    _verify_passkey_authentication,
    _verify_passkey_registration,
    _totp_at,
)
from app.training_hub.main import TrainingHubSettings, create_app
from app.training_hub.routes.public_utils import webauthn_request_context

CSRF_COOKIE_NAME = "training_hub_csrf"
THEME_CSS_PATH = Path(__file__).resolve().parents[1] / "css" / "training-hub.css"


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
    assert "Latest uploads" in dashboard.text
    assert "case_000001" not in dashboard.text

    with training_db.connect(settings.database_path) as connection:
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

    with training_db.connect(settings.database_path) as connection:
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

    with training_db.connect(settings.database_path) as connection:
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


def test_account_delete_confirmation_failure_preserves_non_sensitive_confirmation(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    failed = _post_form(
        client,
        "/dashboard/account/delete",
        data={"current_password": "supersecret", "confirmation": "DELETE"},
    )

    assert failed.status_code == 400
    assert _action_disclosure_open(failed.text, "account-delete")
    assert 'value="DELETE"' in failed.text
    assert "Type DELETE MY ACCOUNT exactly to confirm permanent account deletion." in failed.text


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

    with training_db.connect(settings.database_path) as connection:
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
            assert manifest["account"]["mfaEnabled"] is False
            assert manifest["mfa"]["totpFactors"] == []
            assert manifest["mfa"]["passkeys"] == []
            assert manifest["counts"]["uploads"] == 1
            assert manifest["trainingCasesCreatedByAccount"][0]["caseId"] == "case_export_0001"

        timeout_at = time.time() + 2.0
        export_row = None
        audit_row = None
        while time.time() < timeout_at:
            with training_db.connect(settings.database_path) as connection:
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

    with training_db.connect(settings.database_path) as connection:
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


def test_anonymous_api_client_upload_accepts_client_id_handshake(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))
    payload = _valid_payload()

    response = client.post(
        "/api/v1/client/uploads/anonymous",
        content=payload,
        headers=_anonymous_upload_headers(payload, client_id="  local-mod-01  ", filename="linked-history.jsonl"),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "accepted"
    assert body["caseCount"] == 1
    assert body["insertedCases"] == 1
    assert body["updatedCases"] == 0

    with training_db.connect(settings.database_path) as connection:
        upload_row = connection.execute(
            """
            SELECT up.user_id, up.client_identity_id, ci.normalized_client_id
            FROM uploads up
            JOIN client_identities ci ON ci.id = up.client_identity_id
            WHERE up.id = 1
            """
        ).fetchone()
        case_row = connection.execute(
            """
            SELECT created_by_user_id, created_by_client_identity_id, source_upload_id
            FROM training_cases
            WHERE case_id = 'case_000001'
            """
        ).fetchone()

    assert upload_row == (None, 1, "local-mod-01")
    assert case_row == (None, 1, 1)


def test_anonymous_api_client_upload_rejects_handshake_mismatch(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    payload = _valid_payload()
    headers = _anonymous_upload_headers(payload, client_id="local-mod-01")
    headers["X-ScamScreener-Handshake-Sha256"] = "0" * 64

    response = client.post(
        "/api/v1/client/uploads/anonymous",
        content=payload,
        headers=headers,
    )

    assert response.status_code == 400
    assert "Handshake" in response.json()["detail"]


def test_linked_client_uploads_appear_in_dashboard_history(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))
    payload = _valid_payload()

    anonymous_upload = client.post(
        "/api/v1/client/uploads/anonymous",
        content=payload,
        headers=_anonymous_upload_headers(payload, client_id="linked-client", filename="linked-history.jsonl"),
    )
    assert anonymous_upload.status_code == 201

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    with training_db.connect(settings.database_path) as connection:
        connection.execute(
            """
            UPDATE client_identities
            SET linked_user_id = ?, linked_at = ?
            WHERE normalized_client_id = ?
            """,
            (1, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "linked-client"),
        )
        connection.commit()

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "linked-history.jsonl" in dashboard.text

    download = client.get("/dashboard/uploads/1/download")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/x-ndjson")


def test_user_can_link_client_id_from_account_page(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))
    payload = _valid_payload()

    anonymous_upload = client.post(
        "/api/v1/client/uploads/anonymous",
        content=payload,
        headers=_anonymous_upload_headers(payload, client_id="  Linked-Client-01  ", filename="linked-history.jsonl"),
    )
    assert anonymous_upload.status_code == 201

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    response = _post_form(
        client,
        "/dashboard/account/client-ids/link",
        data={"client_id": "  Linked-Client-01  ", "current_password": "supersecret"},
    )

    assert response.status_code == 200
    assert "Client ID linked-client-01 linked." in response.text
    assert "linked-client-01" in response.text

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "linked-history.jsonl" in dashboard.text

    with training_db.connect(settings.database_path) as connection:
        link_row = connection.execute(
            "SELECT linked_user_id FROM client_identities WHERE normalized_client_id = ?",
            ("linked-client-01",),
        ).fetchone()
        assert link_row == (1,)


def test_link_client_id_rejects_unknown_id(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    response = _post_form(
        client,
        "/dashboard/account/client-ids/link",
        data={"client_id": "unknown-client-id", "current_password": "supersecret"},
    )

    assert response.status_code == 404
    assert "Upload once from the mod before linking it here." in response.text


def test_user_can_unlink_client_id_and_detach_historical_uploads(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))
    payload = _valid_payload()

    anonymous_upload = client.post(
        "/api/v1/client/uploads/anonymous",
        content=payload,
        headers=_anonymous_upload_headers(payload, client_id="detach-client", filename="linked-history.jsonl"),
    )
    assert anonymous_upload.status_code == 201

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(
        client,
        "/dashboard/account/client-ids/link",
        data={"client_id": "detach-client", "current_password": "supersecret"},
    )

    with training_db.connect(settings.database_path) as connection:
        client_identity_row = connection.execute(
            "SELECT id FROM client_identities WHERE normalized_client_id = ?",
            ("detach-client",),
        ).fetchone()
    assert client_identity_row is not None

    response = _post_form(
        client,
        f"/dashboard/account/client-ids/{int(client_identity_row[0])}/unlink",
        data={"current_password": "supersecret"},
    )

    assert response.status_code == 200
    assert "Client ID detach-client unlinked." in response.text

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "linked-history.jsonl" not in dashboard.text

    with training_db.connect(settings.database_path) as connection:
        link_row = connection.execute(
            "SELECT linked_user_id, linked_at FROM client_identities WHERE normalized_client_id = ?",
            ("detach-client",),
        ).fetchone()
        assert link_row == (None, None)


def test_client_link_failure_reopens_disclosure_and_preserves_client_id(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))
    payload = _valid_payload()

    anonymous_upload = client.post(
        "/api/v1/client/uploads/anonymous",
        content=payload,
        headers=_anonymous_upload_headers(payload, client_id="  Linked-Client-01  ", filename="linked-history.jsonl"),
    )
    assert anonymous_upload.status_code == 201

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    response = _post_form(
        client,
        "/dashboard/account/client-ids/link",
        data={"client_id": "  Linked-Client-01  ", "current_password": "wrongsecret"},
    )

    assert response.status_code == 401
    assert _action_disclosure_open(response.text, "client-link")
    assert 'value="  Linked-Client-01  "' in response.text
    assert "Current password is incorrect." in response.text


def test_client_unlink_failure_reopens_only_the_target_row_action(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))
    payload = _valid_payload()

    anonymous_upload = client.post(
        "/api/v1/client/uploads/anonymous",
        content=payload,
        headers=_anonymous_upload_headers(payload, client_id="detach-client", filename="linked-history.jsonl"),
    )
    assert anonymous_upload.status_code == 201

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(
        client,
        "/dashboard/account/client-ids/link",
        data={"client_id": "detach-client", "current_password": "supersecret"},
    )

    with training_db.connect(settings.database_path) as connection:
        client_identity_row = connection.execute(
            "SELECT id FROM client_identities WHERE normalized_client_id = ?",
            ("detach-client",),
        ).fetchone()
    assert client_identity_row is not None

    failed = _post_form(
        client,
        f"/dashboard/account/client-ids/{int(client_identity_row[0])}/unlink",
        data={"current_password": "wrongsecret"},
    )

    action_id = f"client-unlink-{int(client_identity_row[0])}"
    assert failed.status_code == 401
    assert _action_disclosure_open(failed.text, action_id)
    assert "Current password is incorrect." in failed.text


def test_link_client_id_rejects_other_users_existing_link(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))
    payload = _valid_payload()

    anonymous_upload = client.post(
        "/api/v1/client/uploads/anonymous",
        content=payload,
        headers=_anonymous_upload_headers(payload, client_id="owned-client", filename="owned-history.jsonl"),
    )
    assert anonymous_upload.status_code == 201

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(
        client,
        "/dashboard/account/client-ids/link",
        data={"client_id": "owned-client", "current_password": "supersecret"},
    )
    _post_form(client, "/logout", follow_redirects=True)

    _post_form(
        client,
        "/register",
        data={"username": "bob", "email": "bob@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    response = _post_form(
        client,
        "/dashboard/account/client-ids/link",
        data={"client_id": "owned-client", "current_password": "supersecret"},
    )

    assert response.status_code == 409
    assert "already linked to another account" in response.text

    with training_db.connect(settings.database_path) as connection:
        link_row = connection.execute(
            "SELECT linked_user_id FROM client_identities WHERE normalized_client_id = ?",
            ("owned-client",),
        ).fetchone()
        assert link_row == (1,)


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
    assert "Operations Overview" in admin_page.text


def test_admin_page_formats_last_login_timestamp_in_utc(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "dev", "email": "dev@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    with training_db.connect(settings.database_path) as connection:
        connection.execute(
            "UPDATE users SET last_login_at = ? WHERE username = ?",
            ("2026-03-28T18:00:00Z", "dev"),
        )
        connection.commit()

    admin_page = client.get("/admin/users")

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

    admin_page = client.get("/admin/users")

    assert admin_page.status_code == 200
    assert 'class="status-indicator status-indicator-yes"' in admin_page.text
    assert 'class="status-indicator status-indicator-no"' in admin_page.text
    assert 'aria-label="Admin: yes"' in admin_page.text
    assert 'aria-label="Admin: no"' in admin_page.text


def test_admin_user_page_shows_access_policy_switches(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))

    _post_form(
        client,
        "/register",
        data={"username": "owner", "email": "owner@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    admin_page = client.get("/admin/users")

    assert admin_page.status_code == 200
    assert "Disable Login" in admin_page.text
    assert "Disable Signup" in admin_page.text
    assert 'role="switch"' in admin_page.text


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


def test_disable_signup_policy_blocks_registration_and_hides_register_link(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "owner", "email": "owner@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    policy_response = _post_form(
        client,
        "/admin/users/access-policy",
        data={"policy_key": "disable_signup", "enabled": "1"},
        follow_redirects=True,
    )
    assert policy_response.status_code == 200
    assert "Disable Signup enabled." in policy_response.text

    _post_form(client, "/logout", follow_redirects=True)

    register_form = client.get("/register")
    assert register_form.status_code == 403
    assert "Sign up is currently disabled by an administrator." in register_form.text

    login_form = client.get("/login")
    assert login_form.status_code == 200
    assert "Registration is currently closed." in login_form.text

    register_submit = _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
    )
    assert register_submit.status_code == 403
    assert "Sign up is currently disabled by an administrator." in register_submit.text


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


def test_disable_login_policy_blocks_non_admin_web_and_api_login(tmp_path: Path) -> None:
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
        data={"username": "player", "email": "player@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(client, "/logout", follow_redirects=True)
    _post_form(
        client,
        "/login",
        data={"username_or_email": "owner", "password": "supersecret"},
        follow_redirects=True,
    )

    policy_response = _post_form(
        client,
        "/admin/users/access-policy",
        data={"policy_key": "disable_login_non_admin", "enabled": "1"},
        follow_redirects=True,
    )
    assert policy_response.status_code == 200
    assert "Disable Login enabled." in policy_response.text

    _post_form(client, "/logout", follow_redirects=True)

    blocked_login = _post_form(
        client,
        "/login",
        data={"username_or_email": "player", "password": "supersecret"},
    )
    assert blocked_login.status_code == 403
    assert "Login is currently disabled for non-admin accounts." in blocked_login.text

    admin_login = _post_form(
        client,
        "/login",
        data={"username_or_email": "owner", "password": "supersecret"},
        follow_redirects=True,
    )
    assert admin_login.status_code == 200
    assert "Your Case Contributions" in admin_login.text

    _post_form(client, "/logout", follow_redirects=True)
    api_login = client.post(
        "/api/v1/client/auth/login",
        json={"usernameOrEmail": "player", "password": "supersecret"},
    )
    assert api_login.status_code == 403
    assert api_login.json()["detail"] == "Login is currently disabled for non-admin accounts."


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
    assert 'name="new_password"' in reset_form.text
    assert 'name="new_password_confirm"' in reset_form.text
    assert "data-sensitive-action" not in reset_form.text

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

    with training_db.connect(settings.database_path) as connection:
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

    with training_db.connect(settings.database_path) as connection:
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

    response = client.get("/legal-notice", follow_redirects=False)

    assert response.status_code == 200
    assert "app-sidebar" in response.text
    assert "Legal Notice" in response.text
    assert 'href="/legal-notice"' in response.text


def test_login_page_shows_minecraft_credential_warning_and_footer_disclaimer(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))

    response = client.get("/login")

    assert response.status_code == 200
    assert "Do NOT enter your Minecraft credentials!" in response.text
    assert "ScamScreener © 2026 Pankraz01" in response.text
    assert "ScamScreener is in no way affiliated with Minecraft, Microsoft, or Mojang." in response.text


def test_hub_pages_use_local_bootstrap_assets(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))

    landing = client.get("/", follow_redirects=False)
    login = client.get("/login")

    assert landing.status_code == 303
    assert landing.headers.get("location") == "/dashboard"
    assert login.status_code == 200
    assert '/css/bootstrap.min.css' in login.text
    assert '/css/training-hub.css' in login.text
    assert "cdn.jsdelivr.net" not in login.text
    assert "tailwindcss" not in login.text


def test_theme_css_uses_the_new_brand_palette_without_legacy_primary_blue() -> None:
    css = THEME_CSS_PATH.read_text(encoding="utf-8")

    assert "#B006F9".lower() in css.lower()
    assert "#CC60FB".lower() in css.lower()
    assert "#F7E6FE".lower() in css.lower()
    assert "--hub-nav-bg: #b006f9;" in css.lower()
    assert "--hub-nav-active-bg: rgba(255, 255, 255, 0.96);" in css.lower()
    assert "#1f5f8b" not in css.lower()
    assert "#214f71" not in css.lower()
    assert "#163e5b" not in css.lower()
    assert "31, 95, 139" not in css


def test_theme_css_defines_layout_stable_control_feedback_with_reduced_motion_fallback() -> None:
    css = THEME_CSS_PATH.read_text(encoding="utf-8").lower()

    assert "--hub-control-height:" in css
    assert "--hub-control-radius: 8px;" in css
    assert "--hub-focus-ring:" in css
    assert "prefers-reduced-motion: reduce" in css
    assert ".workspace-disclosure-toggle" in css
    assert "filter: none;" in css


def test_dashboard_renders_workspace_sidebar_and_account_navigation(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "app-sidebar" in response.text
    assert "workspace-nav" in response.text
    assert "Workspace" in response.text
    assert "Account" in response.text
    assert "Reference" in response.text
    assert 'href="/dashboard/uploads"' in response.text
    assert 'href="/account/security"' in response.text
    assert 'href="/account/sessions"' in response.text
    assert 'href="/account/clients"' in response.text
    assert 'href="/account/privacy"' in response.text
    assert 'href="/legal-notice"' in response.text
    assert 'href="/privacy"' in response.text
    assert re.search(r'href="/dashboard"[^>]*aria-current="page"', response.text) is not None
    assert "Go to Uploads" in response.text
    assert "Delete My Account" not in response.text


def test_sidebar_disclosure_opens_the_relevant_group_for_each_context(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    uploads_page = client.get("/dashboard/uploads")
    account_page = client.get("/dashboard/account")
    sessions_page = client.get("/account/sessions")
    account_privacy_page = client.get("/account/privacy")
    privacy_page = client.get("/privacy")

    assert uploads_page.status_code == 200
    assert uploads_page.text.count('<details class="workspace-disclosure" open>') == 1
    assert re.search(r'href="/dashboard/uploads"[^>]*aria-current="page"', uploads_page.text) is not None
    assert "Upload training cases" in uploads_page.text
    assert "Delete My Account" not in uploads_page.text

    assert account_page.status_code == 200
    assert account_page.text.count('<details class="workspace-disclosure" open>') == 1
    assert re.search(r'href="/account/security"[^>]*aria-current="page"', account_page.text) is not None
    assert "MFA overview" in account_page.text
    assert "Registered passkeys" in account_page.text

    assert sessions_page.status_code == 200
    assert sessions_page.text.count('<details class="workspace-disclosure" open>') == 1
    assert re.search(r'href="/account/sessions"[^>]*aria-current="page"', sessions_page.text) is not None
    assert "Revoke Other Sessions" in sessions_page.text

    assert account_privacy_page.status_code == 200
    assert account_privacy_page.text.count('<details class="workspace-disclosure" open>') == 1
    assert re.search(r'href="/account/privacy"[^>]*aria-current="page"', account_privacy_page.text) is not None
    assert "Delete My Account" in account_privacy_page.text

    assert privacy_page.status_code == 200
    assert privacy_page.text.count('<details class="workspace-disclosure" open>') == 1
    assert re.search(r'href="/privacy"[^>]*aria-current="page"', privacy_page.text) is not None


def test_account_pages_render_sensitive_actions_as_closed_disclosures_by_default(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            _settings(
                tmp_path,
                smtp_host="mail.local",
                smtp_port=1025,
                smtp_from_email="no-reply@scamscreener.local",
                smtp_use_starttls=False,
            )
        )
    )

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    security_page = client.get("/account/security")
    clients_page = client.get("/account/clients")
    privacy_page = client.get("/account/privacy")

    assert security_page.status_code == 200
    assert _action_disclosure_present(security_page.text, "password-change")
    assert _action_disclosure_present(security_page.text, "totp-enroll")
    assert _action_disclosure_present(security_page.text, "passkey-register")
    assert _action_disclosure_present(security_page.text, "backup-codes-regenerate")
    assert not _action_disclosure_open(security_page.text, "password-change")
    assert not _action_disclosure_open(security_page.text, "totp-enroll")

    assert clients_page.status_code == 200
    assert _action_disclosure_present(clients_page.text, "client-link")
    assert not _action_disclosure_open(clients_page.text, "client-link")

    assert privacy_page.status_code == 200
    assert _action_disclosure_present(privacy_page.text, "data-export-request")
    assert _action_disclosure_present(privacy_page.text, "data-purge")
    assert _action_disclosure_present(privacy_page.text, "account-delete")
    assert not _action_disclosure_open(privacy_page.text, "data-export-request")
    assert not _action_disclosure_open(privacy_page.text, "account-delete")


def test_admin_renders_workspace_sidebar_and_primary_controls(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    response = client.get("/admin")

    assert response.status_code == 200
    assert "app-sidebar" in response.text
    assert "workspace-nav" in response.text
    assert "Admin" in response.text
    assert "Build Training Bundle" in response.text
    assert "Delete Rejected" in response.text
    assert 'href="/admin/users"' in response.text
    assert 'href="/admin/system"' in response.text
    assert response.text.count('<details class="workspace-disclosure" open>') == 1
    assert re.search(r'href="/admin"[^>]*aria-current="page"', response.text) is not None
    assert "Choose a focused area" in response.text


def test_admin_sidebar_shows_mfa_setup_entry_until_admin_migration_is_complete(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path, admin_mfa_required=True)))

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    response = client.get("/account/security")

    assert response.status_code == 200
    assert "Finish MFA setup" in response.text
    assert 'href="/account/security?notice=Complete+MFA+setup#admin-mfa-setup"' in response.text
    assert "Unlock admin pages after adding an Authenticator App or Passkey." in response.text
    assert 'href="/admin/users"' not in response.text
    assert 'href="/admin/system"' not in response.text


def test_admin_subpages_render_separate_operational_areas(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    users_page = client.get("/admin/users")
    cases_page = client.get("/admin/cases")
    runs_page = client.get("/admin/runs")
    system_page = client.get("/admin/system")

    assert users_page.status_code == 200
    assert "User Management" in users_page.text
    assert "Audit log" not in users_page.text

    assert cases_page.status_code == 200
    assert "Case Review Queue" in cases_page.text
    assert "Training runs" not in cases_page.text

    assert runs_page.status_code == 200
    assert "Training Run History" in runs_page.text
    assert "User Management" not in runs_page.text
    assert runs_page.text.count('<details class="workspace-disclosure" open>') == 1
    assert re.search(r'href="/admin/runs"[^>]*aria-current="page"', runs_page.text) is not None

    assert system_page.status_code == 200
    assert "System Controls and Audit" in system_page.text
    assert "Restore Backup" in system_page.text
    assert system_page.text.count('<details class="workspace-disclosure" open>') == 1
    assert re.search(r'href="/admin/system"[^>]*aria-current="page"', system_page.text) is not None


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


def test_privacy_page_redirects_logged_in_users_to_dashboard_section(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    response = client.get("/privacy", follow_redirects=False)

    assert response.status_code == 200
    assert "app-sidebar" in response.text
    assert "Privacy Notice" in response.text
    assert 'href="/privacy"' in response.text


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
    assert login.headers.get("location") == "/mfa"
    assert len(delivered_codes) == 1
    assert delivered_codes[0][0] == "alice@example.com"

    blocked_admin = client.get("/admin", follow_redirects=False)
    assert blocked_admin.status_code == 303
    assert blocked_admin.headers.get("location") == "/login"

    mfa_page = client.get("/mfa")
    assert mfa_page.status_code == 200
    assert "Verify your login" in mfa_page.text

    verified = _post_form(
        client,
        "/mfa",
        data={"code": delivered_codes[0][1]},
        follow_redirects=False,
    )
    assert verified.status_code == 303
    assert verified.headers.get("location") == "/account/security?notice=Complete+MFA+setup"

    admin_page = client.get("/admin")
    assert admin_page.status_code == 200
    assert "Complete MFA setup" in admin_page.text
    assert "Security" in admin_page.text

    with training_db.connect(settings.database_path) as connection:
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
    assert login.headers.get("location") == "/mfa"
    assert len(delivered_codes) == 1

    wrong_code = "000000" if delivered_codes[0] != "000000" else "999999"
    invalid = _post_form(
        client,
        "/mfa",
        data={"code": wrong_code},
    )
    assert invalid.status_code == 401
    assert "Invalid verification code." in invalid.text

    valid = _post_form(
        client,
        "/mfa",
        data={"code": delivered_codes[0]},
        follow_redirects=False,
    )
    assert valid.status_code == 303
    assert valid.headers.get("location") == "/account/security?notice=Complete+MFA+setup"


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
    assert "Verification code could not be delivered." in login.text

    with training_db.connect(settings.database_path) as connection:
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
    assert login.headers.get("location") == "/mfa"
    assert len(delivered_codes) == 1

    mismatch = _post_form(
        client,
        "/mfa",
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
    assert login.headers.get("location") == "/mfa"
    assert len(delivered_codes) == 1

    first_wrong = _post_form(client, "/mfa", data={"code": "000000"})
    assert first_wrong.status_code == 401
    assert "Invalid verification code." in first_wrong.text

    second_wrong = _post_form(client, "/mfa", data={"code": "999999"}, follow_redirects=False)
    assert second_wrong.status_code == 303
    assert second_wrong.headers.get("location", "").startswith("/login?")

    blocked = _post_form(
        client,
        "/mfa",
        data={"code": delivered_codes[0]},
        follow_redirects=False,
    )
    assert blocked.status_code == 303
    assert blocked.headers.get("location", "").startswith("/login?")


def test_user_can_enable_totp_and_use_it_for_login(tmp_path: Path) -> None:
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

    enrollment = _post_form(
        client,
        "/account/security/totp/enroll",
        data={"current_password": "supersecret", "label": "Primary Authenticator"},
    )
    assert enrollment.status_code == 200
    assert "Verify one code to activate it." in enrollment.text
    assert "data:image/svg+xml;base64," in enrollment.text

    secret = _extract_pending_totp_secret(enrollment.text)
    enrollment_token = _extract_hidden_input_value(enrollment.text, "enrollment_token")
    verified = _post_form(
        client,
        "/account/security/totp/verify",
        data={"enrollment_token": enrollment_token, "code": _current_totp_code(secret)},
    )
    assert verified.status_code == 200
    assert "Authenticator app verified and activated." in verified.text

    _post_form(client, "/logout", follow_redirects=True)

    login = _post_form(
        client,
        "/login",
        data={"username_or_email": "bob", "password": "supersecret"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert login.headers.get("location") == "/mfa"

    mfa = _post_form(
        client,
        "/mfa",
        data={"code": _current_totp_code(secret)},
        follow_redirects=False,
    )
    assert mfa.status_code == 303
    assert mfa.headers.get("location") == "/dashboard"

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200
    assert "Your Case Contributions" in dashboard.text


def test_totp_verification_accepts_one_minute_clock_drift_when_configured(tmp_path: Path) -> None:
    settings = _settings(tmp_path, totp_skew_steps=2)
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

    enrollment = _post_form(
        client,
        "/account/security/totp/enroll",
        data={"current_password": "supersecret", "label": "Primary Authenticator"},
    )
    assert enrollment.status_code == 200

    secret = _extract_pending_totp_secret(enrollment.text)
    enrollment_token = _extract_hidden_input_value(enrollment.text, "enrollment_token")
    drifted_code = _totp_at(secret, int(datetime.now(timezone.utc).timestamp()) - 60)

    verified = _post_form(
        client,
        "/account/security/totp/verify",
        data={"enrollment_token": enrollment_token, "code": drifted_code},
    )
    assert verified.status_code == 200
    assert "Authenticator app verified and activated." in verified.text


def test_backup_code_can_be_used_only_once_for_login(tmp_path: Path) -> None:
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
    enrollment = _post_form(
        client,
        "/account/security/totp/enroll",
        data={"current_password": "supersecret", "label": "Recovery Anchor"},
    )
    secret = _extract_pending_totp_secret(enrollment.text)
    enrollment_token = _extract_hidden_input_value(enrollment.text, "enrollment_token")
    _post_form(
        client,
        "/account/security/totp/verify",
        data={"enrollment_token": enrollment_token, "code": _current_totp_code(secret)},
    )

    backup_codes_page = _post_form(
        client,
        "/account/security/backup-codes/regenerate",
        data={"current_password": "supersecret"},
    )
    assert backup_codes_page.status_code == 200
    first_backup_code = _extract_first_backup_code(backup_codes_page.text)

    _post_form(client, "/logout", follow_redirects=True)
    login = _post_form(
        client,
        "/login",
        data={"username_or_email": "bob", "password": "supersecret"},
        follow_redirects=False,
    )
    assert login.headers.get("location") == "/mfa"

    first_use = _post_form(
        client,
        "/mfa",
        data={"code": first_backup_code},
        follow_redirects=False,
    )
    assert first_use.status_code == 303
    assert first_use.headers.get("location") == "/dashboard"

    _post_form(client, "/logout", follow_redirects=True)
    second_login = _post_form(
        client,
        "/login",
        data={"username_or_email": "bob", "password": "supersecret"},
        follow_redirects=False,
    )
    assert second_login.headers.get("location") == "/mfa"

    reused = _post_form(client, "/mfa", data={"code": first_backup_code})
    assert reused.status_code == 401
    assert "Invalid verification code." in reused.text


def test_passkey_registration_and_identifier_first_login(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    passkey_script = client.get("/js/passkeys.js")
    assert passkey_script.status_code == 200
    assert "navigator.credentials" in passkey_script.text

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
    login_page = client.get("/login")
    assert '<script src="/js/passkeys.js"></script>' in login_page.text
    _post_form(
        client,
        "/login",
        data={"username_or_email": "bob", "password": "supersecret"},
        follow_redirects=True,
    )

    def _fake_registration_options(
        _settings,
        *,
        user_id: int,
        label: str,
        rp_id: str = "",
        expected_origin: str = "",
        source_ip: str = "",
        user_agent: str = "",
    ):
        assert user_id == 2
        assert label == "Laptop"
        assert rp_id == "testserver"
        assert expected_origin == "http://testserver"
        return {"token": "register-flow", "options_json": json.dumps({"challenge": "Y2hhbGxlbmdl"})}

    def _fake_registration_verify(
        _settings,
        *,
        user_id: int,
        flow_token: str,
        credential: dict,
        source_ip: str = "",
        user_agent: str = "",
    ):
        assert user_id == 2
        assert flow_token == "register-flow"
        assert credential["id"] == "credential-1"
        return {"ok": True, "label": "Laptop"}

    monkeypatch.setattr(
        "app.training_hub.routes.public_dashboard_account._generate_passkey_registration_options",
        _fake_registration_options,
    )
    monkeypatch.setattr(
        "app.training_hub.routes.public_dashboard_account._verify_passkey_registration",
        _fake_registration_verify,
    )

    options = client.post(
        "/account/security/passkeys/register/options",
        json={"label": "Laptop", "currentPassword": "supersecret"},
        headers={"Origin": "http://testserver", "Referer": "http://testserver/account/security"},
    )
    assert options.status_code == 200
    assert options.json()["flowToken"] == "register-flow"

    verify = client.post(
        "/account/security/passkeys/register/verify",
        json={"flowToken": "register-flow", "credential": {"id": "credential-1"}},
        headers={"Origin": "http://testserver", "Referer": "http://testserver/account/security"},
    )
    assert verify.status_code == 200
    assert verify.json()["notice"] == "Passkey registered."

    _post_form(client, "/logout", follow_redirects=True)

    def _fake_auth_options(
        _settings,
        *,
        user_id: int,
        purpose: str,
        rp_id: str = "",
        expected_origin: str = "",
        source_ip: str = "",
        user_agent: str = "",
        login_flow_id: int | None = None,
    ):
        assert user_id == 2
        assert purpose == "passwordless-login"
        assert rp_id == "testserver"
        assert expected_origin == "http://testserver"
        assert login_flow_id is None
        return {
            "ok": True,
            "flow_token": "login-passkey-flow",
            "options_json": json.dumps({"challenge": "Y2hhbGxlbmdl", "allowCredentials": []}),
        }

    def _fake_auth_verify(_settings, *, flow_token: str, credential: dict, source_ip: str = "", user_agent: str = ""):
        assert flow_token == "login-passkey-flow"
        assert credential["id"] == "credential-1"
        return {"ok": True, "user_id": 2, "purpose": "passwordless-login", "login_flow_id": None}

    monkeypatch.setattr("app.training_hub.routes.public_auth_login._generate_passkey_auth_options", _fake_auth_options)
    monkeypatch.setattr("app.training_hub.routes.public_auth_login._verify_passkey_authentication", _fake_auth_verify)

    login_options = client.post(
        "/login/passkey/options",
        json={"identifier": "bob"},
        headers={"Origin": "http://testserver", "Referer": "http://testserver/login"},
    )
    assert login_options.status_code == 200
    assert login_options.json()["flowToken"] == "login-passkey-flow"

    login_verify = client.post(
        "/login/passkey/verify",
        json={"flowToken": "login-passkey-flow", "credential": {"id": "credential-1"}},
        headers={"Origin": "http://testserver", "Referer": "http://testserver/login"},
    )
    assert login_verify.status_code == 200
    assert login_verify.json()["redirectUrl"] == "/dashboard"

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200


def test_webauthn_request_context_uses_configured_rp_id_even_when_request_host_differs(tmp_path: Path) -> None:
    settings = replace(
        _settings(
            tmp_path,
            public_base_url="https://scamscreener.creepans.net",
            enforce_origin_check=False,
        ),
        allowed_hosts={"testserver", "internal.local", "scamscreener.creepans.net"},
        webauthn_rp_id="scamscreener.creepans.net",
        webauthn_origins=("https://scamscreener.creepans.net",),
    )
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/login/passkey/options",
            "raw_path": b"/login/passkey/options",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"internal.local"),
                (b"origin", b"https://scamscreener.creepans.net"),
                (b"referer", b"https://scamscreener.creepans.net/login"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("internal.local", 80),
        }
    )

    rp_id, origin = webauthn_request_context(request, settings)

    assert rp_id == "scamscreener.creepans.net"
    assert origin == "https://scamscreener.creepans.net"


def test_passkey_authentication_verification_failure_returns_safe_error(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    create_app(settings)

    with training_db.connect(settings.database_path) as connection:
        connection.execute(
            """
            INSERT INTO user_passkeys (
                created_at,
                user_id,
                label,
                credential_id,
                public_key,
                sign_count,
                aaguid,
                credential_device_type,
                backed_up,
                last_used_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                "2026-01-01T00:00:00Z",
                1,
                "Laptop",
                "credential-1",
                "cHVibGljLWtleQ",
                0,
                "",
                "multi_device",
                1,
            ),
        )
        connection.commit()

    auth_options = _generate_passkey_auth_options(settings, user_id=1, purpose="passwordless-login")
    assert auth_options["ok"] is True

    from webauthn.helpers.exceptions import InvalidAuthenticationResponse

    def _boom(**_kwargs):
        raise InvalidAuthenticationResponse("Unexpected RP ID hash")

    monkeypatch.setattr("app.training_hub.core.mfa.verify_authentication_response", _boom)

    result = _verify_passkey_authentication(
        settings,
        flow_token=str(auth_options["flow_token"]),
        credential={"id": "credential-1", "rawId": "credential-1", "type": "public-key", "response": {}},
    )

    assert result == {
        "ok": False,
        "error": "Passkey authentication could not be verified.",
        "status_code": 400,
    }


def test_discoverable_passkey_login_options_work_without_identifier(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(client, "/logout", follow_redirects=True)

    def _fake_auth_options(
        _settings,
        *,
        user_id: int,
        purpose: str,
        discoverable: bool = False,
        rp_id: str = "",
        expected_origin: str = "",
        source_ip: str = "",
        user_agent: str = "",
        login_flow_id: int | None = None,
    ):
        assert user_id == 1
        assert purpose == "passwordless-login"
        assert discoverable is True
        assert rp_id == "testserver"
        assert expected_origin == "http://testserver"
        assert login_flow_id is None
        return {
            "ok": True,
            "flow_token": "discoverable-login-flow",
            "options_json": json.dumps({"challenge": "Y2hhbGxlbmdl", "allowCredentials": []}),
        }

    monkeypatch.setattr("app.training_hub.routes.public_auth_login._generate_passkey_auth_options", _fake_auth_options)

    login_options = client.post(
        "/login/passkey/options",
        json={"identifier": ""},
        headers={"Origin": "http://testserver", "Referer": "http://testserver/login"},
    )
    assert login_options.status_code == 200
    assert login_options.json()["flowToken"] == "discoverable-login-flow"


def test_passkey_script_handles_redirect_and_incomplete_option_payloads(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    passkey_script = client.get("/js/passkeys.js")
    assert passkey_script.status_code == 200
    assert "redirectUrl" in passkey_script.text
    assert "did not return valid credential options." in passkey_script.text
    assert "The server returned an unexpected response." in passkey_script.text

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    logged_in_options = client.post(
        "/login/passkey/options",
        json={"identifier": "alice"},
        headers={"Origin": "http://testserver", "Referer": "http://testserver/login"},
    )
    assert logged_in_options.status_code == 200
    assert logged_in_options.json() == {"ok": True, "redirectUrl": "/dashboard"}


def test_account_confirm_password_flow_can_complete_totp_enrollment_start(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    start = _post_form(
        client,
        "/account/security/totp/enroll",
        data={"label": "Primary Authenticator"},
        follow_redirects=False,
    )
    assert start.status_code == 303
    assert start.headers.get("location") == "/account/confirm"

    confirm_page = client.get("/account/confirm")
    assert confirm_page.status_code == 200
    assert "Confirm authenticator setup" in confirm_page.text
    assert "Confirm with Password" in confirm_page.text

    completed = _post_form(
        client,
        "/account/confirm/password",
        data={"current_password": "supersecret"},
        follow_redirects=True,
    )
    assert completed.status_code == 200
    assert "Authenticator setup created. Verify one code to activate it." in completed.text
    assert "Verify Authenticator App" in completed.text


def test_account_confirm_passkey_registration_ready_page_loads_passkey_script_for_first_passkey(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    start = _post_form(
        client,
        "/account/security/passkeys/register",
        data={"label": "Chrome"},
        follow_redirects=False,
    )
    assert start.status_code == 303
    assert start.headers.get("location") == "/account/confirm"

    completed = _post_form(
        client,
        "/account/confirm/password",
        data={"current_password": "supersecret"},
        follow_redirects=True,
    )
    assert completed.status_code == 200
    assert "Authentication is complete. Finish the passkey registration on this device now." in completed.text
    assert 'id="account_confirm_register_passkey_button"' in completed.text
    assert '<script src="/js/passkeys.js"></script>' in completed.text


def test_passkey_registration_options_require_discoverable_credentials(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "bob", "email": "bob@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    options = _generate_passkey_registration_options(
        settings,
        user_id=1,
        label="Chrome",
        rp_id="testserver",
        expected_origin="http://testserver",
    )
    public_key = json.loads(str(options["options_json"]))
    assert public_key["authenticatorSelection"]["residentKey"] == "required"
    assert public_key["authenticatorSelection"]["requireResidentKey"] is True
    assert public_key["authenticatorSelection"]["userVerification"] == "preferred"
    assert public_key["hints"] == ["client-device", "hybrid", "security-key"]


def test_user_can_register_multiple_distinct_passkeys(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "bob", "email": "bob@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    verified_credentials = [
        SimpleNamespace(
            credential_id=b"credential-1",
            credential_public_key=b"public-key-1",
            sign_count=0,
            aaguid="",
            credential_device_type="multi_device",
            credential_backed_up=True,
        ),
        SimpleNamespace(
            credential_id=b"credential-2",
            credential_public_key=b"public-key-2",
            sign_count=0,
            aaguid="",
            credential_device_type="multi_device",
            credential_backed_up=True,
        ),
    ]

    def _fake_verify_registration_response(**_kwargs):
        assert verified_credentials
        return verified_credentials.pop(0)

    monkeypatch.setattr("app.training_hub.core.mfa.verify_registration_response", _fake_verify_registration_response)

    first_options = _generate_passkey_registration_options(
        settings,
        user_id=1,
        label="Chrome",
        rp_id="testserver",
        expected_origin="http://testserver",
    )
    first_result = _verify_passkey_registration(
        settings,
        user_id=1,
        flow_token=str(first_options["token"]),
        credential={"id": "credential-1"},
    )
    assert first_result == {"ok": True, "label": "Chrome"}

    second_options = _generate_passkey_registration_options(
        settings,
        user_id=1,
        label="iPhone",
        rp_id="testserver",
        expected_origin="http://testserver",
    )
    second_result = _verify_passkey_registration(
        settings,
        user_id=1,
        flow_token=str(second_options["token"]),
        credential={"id": "credential-2"},
    )
    assert second_result == {"ok": True, "label": "iPhone"}

    with training_db.connect(settings.database_path) as connection:
        count = int(connection.execute("SELECT COUNT(*) FROM user_passkeys WHERE user_id = 1").fetchone()[0])
        labels = [row[0] for row in connection.execute("SELECT label FROM user_passkeys WHERE user_id = 1 ORDER BY id ASC").fetchall()]

    assert count == 2
    assert labels == ["Chrome", "iPhone"]


def test_admin_user_with_registered_passkey_can_complete_generic_mfa_flow(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path, admin_mfa_required=True)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(client, "/logout", follow_redirects=True)

    with training_db.connect(settings.database_path) as connection:
        connection.execute("UPDATE users SET mfa_enabled = 1 WHERE id = 1")
        connection.execute(
            """
            INSERT INTO user_passkeys (
                created_at, user_id, label, credential_id, public_key, sign_count,
                aaguid, credential_device_type, backed_up, last_used_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                1,
                "Laptop",
                "credential-1",
                "cHVibGljLWtleQ",
                0,
                "",
                "single_device",
                0,
                None,
            ),
        )
        connection.commit()

    def _fake_auth_options(
        _settings,
        *,
        user_id: int,
        purpose: str,
        rp_id: str = "",
        expected_origin: str = "",
        source_ip: str = "",
        user_agent: str = "",
        login_flow_id: int | None = None,
    ):
        assert user_id == 1
        assert purpose == "mfa"
        assert rp_id == "testserver"
        assert expected_origin == "http://testserver"
        assert login_flow_id is not None
        return {
            "ok": True,
            "flow_token": "mfa-passkey-flow",
            "options_json": json.dumps({"challenge": "Y2hhbGxlbmdl", "allowCredentials": []}),
        }

    def _fake_auth_verify(_settings, *, flow_token: str, credential: dict, source_ip: str = "", user_agent: str = ""):
        assert flow_token == "mfa-passkey-flow"
        assert credential["id"] == "credential-1"
        return {"ok": True, "user_id": 1, "purpose": "mfa", "login_flow_id": 1}

    monkeypatch.setattr("app.training_hub.routes.public_auth_mfa._generate_passkey_auth_options", _fake_auth_options)
    monkeypatch.setattr("app.training_hub.routes.public_auth_mfa._verify_passkey_authentication", _fake_auth_verify)

    login = _post_form(
        client,
        "/login",
        data={"username_or_email": "alice", "password": "supersecret"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    assert login.headers.get("location") == "/mfa"

    mfa_page = client.get("/mfa")
    assert mfa_page.status_code == 200
    assert "Use Passkey" in mfa_page.text
    assert "sent to" not in mfa_page.text
    assert '<script src="/js/passkeys.js"></script>' in mfa_page.text

    options = client.post(
        "/mfa/passkey/options",
        headers={"Origin": "http://testserver", "Referer": "http://testserver/mfa"},
    )
    assert options.status_code == 200
    assert options.json()["flowToken"] == "mfa-passkey-flow"

    verify = client.post(
        "/mfa/passkey/verify",
        json={"flowToken": "mfa-passkey-flow", "credential": {"id": "credential-1"}},
        headers={"Origin": "http://testserver", "Referer": "http://testserver/mfa"},
    )
    assert verify.status_code == 200
    assert verify.json()["redirectUrl"] == "/admin"

    admin = client.get("/admin")
    assert admin.status_code == 200
    assert "Operations Overview" in admin.text


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

    runs_page = client.get("/admin/runs")
    assert runs_page.status_code == 200
    assert "/admin/runs/1/bundle" in runs_page.text

    with training_db.connect(settings.database_path) as connection:
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

    with training_db.connect(settings.database_path) as connection:
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

    with training_db.connect(settings.database_path) as connection:
        bob_id = int(connection.execute("SELECT id FROM users WHERE username = 'bob'").fetchone()[0])

    grant = _post_form(client, f"/admin/users/{bob_id}/admin", data={"action": "grant"}, follow_redirects=True)
    assert grant.status_code == 200
    assert "Granted admin to bob." in grant.text

    revoke = _post_form(client, f"/admin/users/{bob_id}/admin", data={"action": "revoke"}, follow_redirects=True)
    assert revoke.status_code == 200
    assert "Revoked admin from bob." in revoke.text

    with training_db.connect(settings.database_path) as connection:
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


def test_admin_user_management_can_delete_user_with_email_notification(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(
        tmp_path,
        smtp_host="mail.local",
        smtp_port=1025,
        smtp_from_email="no-reply@scamscreener.local",
        smtp_use_starttls=False,
    )
    client = TestClient(create_app(settings))
    delivered: list[tuple[str, str, str]] = []

    def _fake_send_account_deletion_email(_settings, recipient_email: str, username: str, deleted_at: str) -> None:
        delivered.append((recipient_email, username, deleted_at))

    monkeypatch.setattr("app.training_hub.routes.admin_users.send_account_deletion_email", _fake_send_account_deletion_email)

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

    with training_db.connect(settings.database_path) as connection:
        bob_id = int(connection.execute("SELECT id FROM users WHERE username = 'bob'").fetchone()[0])

    users_page = client.get("/admin/users")
    assert users_page.status_code == 200
    assert f"/admin/users/{bob_id}/delete" in users_page.text
    assert "Send the user an email notification about the deletion." in users_page.text

    delete_response = _post_form(
        client,
        f"/admin/users/{bob_id}/delete",
        data={"confirm_delete": "yes", "notify_user_email": "yes"},
        follow_redirects=True,
    )
    assert delete_response.status_code == 200
    assert "Deleted user bob." in delete_response.text
    assert "Notification email sent to bob@example.com." in delete_response.text

    assert len(delivered) == 1
    assert delivered[0][0] == "bob@example.com"
    assert delivered[0][1] == "bob"

    with training_db.connect(settings.database_path) as connection:
        bob_row = connection.execute("SELECT id FROM users WHERE id = ?", (bob_id,)).fetchone()
        delete_audit = connection.execute(
            "SELECT id FROM audit_logs WHERE action = 'user.delete' AND target_id = ?",
            (bob_id,),
        ).fetchone()
        email_audit = connection.execute(
            "SELECT id FROM audit_logs WHERE action = 'user.delete.email.sent' AND target_id = ?",
            (bob_id,),
        ).fetchone()
        assert bob_row is None
        assert delete_audit is not None
        assert email_audit is not None


def test_admin_user_management_delete_requires_server_side_confirmation(tmp_path: Path) -> None:
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

    with training_db.connect(settings.database_path) as connection:
        bob_id = int(connection.execute("SELECT id FROM users WHERE username = 'bob'").fetchone()[0])

    delete_response = _post_form(client, f"/admin/users/{bob_id}/delete", data={}, follow_redirects=True)
    assert delete_response.status_code == 400
    assert "Confirm the deletion before removing the user account." in delete_response.text

    with training_db.connect(settings.database_path) as connection:
        bob_row = connection.execute("SELECT id FROM users WHERE id = ?", (bob_id,)).fetchone()
        assert bob_row is not None


def test_admin_pages_show_case_list_and_audit_log_on_their_separate_views(tmp_path: Path) -> None:
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

    cases_page = client.get("/admin/cases")
    system_page = client.get("/admin/system")
    assert cases_page.status_code == 200
    assert "Case Review Queue" in cases_page.text
    assert "/admin/cases/1" in cases_page.text
    assert system_page.status_code == 200
    assert "Audit log" in system_page.text
    assert "upload.accepted" in system_page.text


def test_admin_overview_shows_case_status_breakdown(tmp_path: Path) -> None:
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
        files={"training_file": ("training-cases-v2.jsonl", _valid_payload(case_id="case_overview_approved"), "application/x-ndjson")},
    )
    _post_form(client, "/admin/cases/1/status", data={"action": "approve"}, follow_redirects=True)
    _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("training-cases-v2.jsonl", _valid_payload(case_id="case_overview_rejected"), "application/x-ndjson")},
    )
    _post_form(client, "/admin/cases/2/status", data={"action": "reject"}, follow_redirects=True)

    admin_page = client.get("/admin")

    assert admin_page.status_code == 200
    assert "Approved: 1" in admin_page.text
    assert "Rejected: 1" in admin_page.text


def test_admin_cases_page_shows_short_case_id_label_badges_and_mod_version(tmp_path: Path) -> None:
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
        files={
            "training_file": (
                "training-cases-v2.jsonl",
                _valid_payload(case_id="case.05f94929-2994-4f23-ae6f-7571ac74a63e.review-3", label="risk", outcome="review"),
                "application/x-ndjson",
            )
        },
        headers={"User-Agent": "ScamScreener/1.4.2+1.20.6"},
    )
    _post_form(
        client,
        "/dashboard/upload",
        files={
            "training_file": (
                "training-cases-v2.jsonl",
                _valid_payload(case_id="case.5dfeb993-0f9f-4e19-91e6-26df33d0caa1.review-2", label="safe", outcome="safe"),
                "application/x-ndjson",
            )
        },
        headers={"User-Agent": "ScamScreener/2.0.0+1.21.1"},
    )

    cases_page = client.get("/admin/cases")

    assert cases_page.status_code == 200
    assert "05f94929-3" in cases_page.text
    assert "5dfeb993-2" in cases_page.text
    assert "case.05f94929-2994-4f23-ae6f-7571ac74a63e.review-3" not in cases_page.text
    assert "Created By" not in cases_page.text
    assert "Mod Version" in cases_page.text
    assert "1.4.2 1.20.6" in cases_page.text
    assert "2.0.0 1.21.1" in cases_page.text
    assert "chip-label-risk" in cases_page.text
    assert "chip-label-safe" in cases_page.text


def test_admin_cases_page_filters_by_status_label_and_message_text(tmp_path: Path) -> None:
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
        files={
            "training_file": (
                "training-cases-v2.jsonl",
                _valid_payload(
                    case_id="case_filter_submitted",
                    label="safe",
                    outcome="review",
                    messages='["i am legit"]',
                ),
                "application/x-ndjson",
            )
        },
    )
    _post_form(
        client,
        "/dashboard/upload",
        files={
            "training_file": (
                "training-cases-v2.jsonl",
                _valid_payload(
                    case_id="case_filter_approved",
                    label="risk",
                    outcome="review",
                    messages='["discord carry scam"]',
                ),
                "application/x-ndjson",
            )
        },
    )
    _post_form(client, "/admin/cases/2/status", data={"action": "approve"}, follow_redirects=True)
    _post_form(
        client,
        "/dashboard/upload",
        files={
            "training_file": (
                "training-cases-v2.jsonl",
                _valid_payload(
                    case_id="case_filter_rejected",
                    label="risk",
                    outcome="review",
                    messages='["totally harmless"]',
                ),
                "application/x-ndjson",
            )
        },
    )
    _post_form(client, "/admin/cases/3/status", data={"action": "reject"}, follow_redirects=True)

    status_filtered = client.get("/admin/cases?filter=status:submitted")
    assert status_filtered.status_code == 200
    assert 'value="status:submitted"' in status_filtered.text
    assert 'role="combobox"' in status_filtered.text
    assert 'data-case-filter-suggestions' in status_filtered.text
    assert "case_filter_submitted" in status_filtered.text
    assert "case_filter_approved" not in status_filtered.text
    assert "case_filter_rejected" not in status_filtered.text

    label_filtered = client.get("/admin/cases?filter=label:safe")
    assert label_filtered.status_code == 200
    assert "case_filter_submitted" in label_filtered.text
    assert "case_filter_approved" not in label_filtered.text
    assert "case_filter_rejected" not in label_filtered.text

    text_filtered = client.get("/admin/cases?filter=i%20am%20legit")
    assert text_filtered.status_code == 200
    assert "case_filter_submitted" in text_filtered.text
    assert "case_filter_approved" not in text_filtered.text
    assert "case_filter_rejected" not in text_filtered.text


def test_admin_cases_page_sorts_columns_ascending_and_descending(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "dev", "email": "dev@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    for case_id in ("case_sort_c", "case_sort_a", "case_sort_b"):
        _post_form(
            client,
            "/dashboard/upload",
            files={
                "training_file": (
                    "training-cases-v2.jsonl",
                    _valid_payload(case_id=case_id, label="safe", outcome="review"),
                    "application/x-ndjson",
                )
            },
        )

    ascending = client.get("/admin/cases?sort_by=case_id&sort_dir=asc")
    assert ascending.status_code == 200
    assert 'href="/admin/cases?sort_by=case_id&amp;sort_dir=desc"' in ascending.text
    assert ascending.text.index("case_sort_a") < ascending.text.index("case_sort_b") < ascending.text.index("case_sort_c")

    descending = client.get("/admin/cases?sort_by=case_id&sort_dir=desc")
    assert descending.status_code == 200
    assert 'href="/admin/cases?sort_by=case_id&amp;sort_dir=asc"' in descending.text
    assert descending.text.index("case_sort_c") < descending.text.index("case_sort_b") < descending.text.index("case_sort_a")


def test_delete_rejected_hides_cases_and_preserves_tombstone_block_on_reupload(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))

    _post_form(
        client,
        "/register",
        data={"username": "dev", "email": "dev@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    rejected_case_id = "case_deleted_rejected_0001"
    _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("training-cases-v2.jsonl", _valid_payload(case_id=rejected_case_id, label="risk", outcome="review"), "application/x-ndjson")},
    )
    _post_form(client, "/admin/cases/1/status", data={"action": "reject"}, follow_redirects=True)

    deleted = _post_form(
        client,
        "/admin/cases/rejected/delete",
        data={},
        follow_redirects=True,
    )
    assert deleted.status_code == 200
    assert "Deleted content for 1 rejected cases and kept their case IDs blocked." in deleted.text

    cases_page = client.get("/admin/cases")
    assert cases_page.status_code == 200
    assert rejected_case_id not in cases_page.text

    with training_db.connect(settings.database_path) as connection:
        tombstone = connection.execute(
            "SELECT case_id, status, label, outcome, tag_ids_json, payload_json, source_upload_id, content_deleted_at FROM training_cases WHERE id = 1"
        ).fetchone()
        upload_case_count = int(connection.execute("SELECT COUNT(*) FROM upload_cases WHERE case_id = ?", (rejected_case_id,)).fetchone()[0])
        assert tombstone is not None
        assert str(tombstone[0]) == rejected_case_id
        assert str(tombstone[1]) == "rejected"
        assert str(tombstone[2]) == ""
        assert str(tombstone[3]) == ""
        assert str(tombstone[4]) == "[]"
        assert str(tombstone[5]) == "{}"
        assert tombstone[6] is None
        assert str(tombstone[7]) != ""
        assert upload_case_count == 0

    reupload = _post_form(
        client,
        "/dashboard/upload",
        files={"training_file": ("training-cases-v2.jsonl", _valid_payload(case_id=rejected_case_id, label="safe", outcome="safe"), "application/x-ndjson")},
        follow_redirects=True,
    )
    assert reupload.status_code == 201
    assert "Rejected-case tombstones skipped: 1." in reupload.text

    cases_page_after = client.get("/admin/cases")
    assert cases_page_after.status_code == 200
    assert rejected_case_id not in cases_page_after.text

    with training_db.connect(settings.database_path) as connection:
        tombstone_after = connection.execute(
            "SELECT status, label, outcome, payload_json, content_deleted_at FROM training_cases WHERE id = 1"
        ).fetchone()
        upload_case_count_after = int(connection.execute("SELECT COUNT(*) FROM upload_cases WHERE case_id = ?", (rejected_case_id,)).fetchone()[0])
        case_row_count = int(connection.execute("SELECT COUNT(*) FROM training_cases WHERE case_id = ?", (rejected_case_id,)).fetchone()[0])
        assert tombstone_after is not None
        assert str(tombstone_after[0]) == "rejected"
        assert str(tombstone_after[1]) == ""
        assert str(tombstone_after[2]) == ""
        assert str(tombstone_after[3]) == "{}"
        assert str(tombstone_after[4]) != ""
        assert upload_case_count_after == 0
        assert case_row_count == 1


def test_admin_case_detail_page_is_readable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    client = TestClient(create_app(settings))
    messages = (
        '[{"index":0,"role":"other","text":"joo wassup"},'
        '{"index":1,"role":"other","text":"i am legit"},'
        '{"index":2,"role":"other","text":"trust me"},'
        '{"index":3,"role":"other","text":"its a legit middleman"}]'
    )

    _post_form(
        client,
        "/register",
        data={"username": "dev", "email": "dev@example.com", "password": "supersecret"},
        follow_redirects=True,
    )
    _post_form(
        client,
        "/dashboard/upload",
        files={
            "training_file": (
                "training-cases-v2.jsonl",
                _valid_payload(
                    messages=messages,
                    signal_message_indices="[2,3]",
                    context_message_indices="[1]",
                    excluded_message_indices="[0]",
                ),
                "application/x-ndjson",
            )
        },
    )

    detail = client.get("/admin/cases/1")
    assert detail.status_code == 200
    assert "Case Detail" in detail.text
    assert "Observed Pipeline" in detail.text
    assert "Conversation" in detail.text
    assert "Stage Results" in detail.text
    assert "case_000001" in detail.text
    assert "Excluded" in detail.text
    assert "Context" in detail.text
    assert "Signal" in detail.text
    assert "OTHER" not in detail.text


def test_dashboard_and_admin_core_controls_remain_visible_after_reskin(tmp_path: Path) -> None:
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

    dashboard = client.get("/dashboard")
    admin = client.get("/admin")
    uploads_page = client.get("/dashboard/uploads")
    account_page = client.get("/dashboard/account")
    sessions_page = client.get("/account/sessions")
    privacy_page = client.get("/account/privacy")
    system_page = client.get("/admin/system")
    cases_page = client.get("/admin/cases")

    assert dashboard.status_code == 200
    assert "Go to Uploads" in dashboard.text
    assert "Open Account Page" in dashboard.text

    assert uploads_page.status_code == 200
    assert "Upload File" in uploads_page.text

    assert account_page.status_code == 200
    assert "MFA overview" in account_page.text
    assert "Registered passkeys" in account_page.text

    assert sessions_page.status_code == 200
    assert "Revoke Other Sessions" in sessions_page.text

    assert privacy_page.status_code == 200
    assert "Account data export email is currently unavailable" in privacy_page.text
    assert "Delete My Account" in privacy_page.text

    assert admin.status_code == 200
    assert "Build Training Bundle" in admin.text
    assert "Choose a focused area" in admin.text

    assert system_page.status_code == 200
    assert "Restore Backup" in system_page.text
    assert "Audit log" in system_page.text

    assert cases_page.status_code == 200
    assert "Case Review Queue" in cases_page.text


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

    with training_db.connect(settings.database_path) as connection:
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
    assert "Case Review Queue" in deleted.text

    with training_db.connect(settings.database_path) as connection:
        case_count = int(connection.execute("SELECT COUNT(*) FROM training_cases").fetchone()[0])
        assert case_count == 0


def test_admin_case_detail_page_shows_approve_and_reject_actions(tmp_path: Path) -> None:
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
    assert "Approve Case" in detail.text
    assert "Reject Case" in detail.text
    assert ("Mark Risk" in detail.text) or ("Mark Safe" in detail.text)
    assert 'action="/admin/cases/1/status"' in detail.text
    assert 'action="/admin/cases/1/label"' in detail.text


def test_admin_can_approve_case_from_detail_page(tmp_path: Path) -> None:
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

    approved = _post_form(
        client,
        "/admin/cases/1/status",
        data={"action": "approve"},
        follow_redirects=True,
    )

    assert approved.status_code == 200
    assert "Approved case case_000001." in approved.text

    with training_db.connect(settings.database_path) as connection:
        case_row = connection.execute("SELECT status FROM training_cases WHERE id = 1").fetchone()
        audit = connection.execute("SELECT id FROM audit_logs WHERE action = 'case.approved' LIMIT 1").fetchone()
        assert case_row is not None
        assert str(case_row[0]) == "approved"
        assert audit is not None


def test_admin_can_reject_case_from_detail_page(tmp_path: Path) -> None:
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

    rejected = _post_form(
        client,
        "/admin/cases/1/status",
        data={"action": "reject"},
        follow_redirects=True,
    )

    assert rejected.status_code == 200
    assert "Rejected case case_000001." in rejected.text

    with training_db.connect(settings.database_path) as connection:
        case_row = connection.execute("SELECT status FROM training_cases WHERE id = 1").fetchone()
        audit = connection.execute("SELECT id FROM audit_logs WHERE action = 'case.rejected' LIMIT 1").fetchone()
        assert case_row is not None
        assert str(case_row[0]) == "rejected"
        assert audit is not None


def test_admin_can_mark_case_safe_from_detail_page(tmp_path: Path) -> None:
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
        files={"training_file": ("training-cases-v2.jsonl", _valid_payload(label="risk"), "application/x-ndjson")},
    )

    marked = _post_form(
        client,
        "/admin/cases/1/label",
        data={"action": "mark-safe"},
        follow_redirects=True,
    )

    assert marked.status_code == 200
    assert "Marked case case_000001 as safe." in marked.text

    with training_db.connect(settings.database_path) as connection:
        case_row = connection.execute("SELECT label FROM training_cases WHERE id = 1").fetchone()
        audit = connection.execute("SELECT id FROM audit_logs WHERE action = 'case.label.safe' LIMIT 1").fetchone()
        assert case_row is not None
        assert str(case_row[0]) == "safe"
        assert audit is not None


def test_admin_can_mark_case_risk_from_detail_page(tmp_path: Path) -> None:
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
        files={"training_file": ("training-cases-v2.jsonl", _valid_payload(label="safe"), "application/x-ndjson")},
    )

    marked = _post_form(
        client,
        "/admin/cases/1/label",
        data={"action": "mark-risk"},
        follow_redirects=True,
    )

    assert marked.status_code == 200
    assert "Marked case case_000001 as risk." in marked.text

    with training_db.connect(settings.database_path) as connection:
        case_row = connection.execute("SELECT label FROM training_cases WHERE id = 1").fetchone()
        audit = connection.execute("SELECT id FROM audit_logs WHERE action = 'case.label.risk' LIMIT 1").fetchone()
        assert case_row is not None
        assert str(case_row[0]) == "risk"
        assert audit is not None


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

    with training_db.connect(settings.database_path) as connection:
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

    with training_db.connect(settings.database_path) as connection:
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

    with training_db.connect(settings.database_path) as connection:
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

    with training_db.connect(settings.database_path) as connection:
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

    with training_db.connect(settings.database_path) as connection:
        audit = connection.execute(
            "SELECT id FROM audit_logs WHERE action = 'auth.password.changed' LIMIT 1"
        ).fetchone()
        assert audit is not None


def test_password_change_failure_reopens_sensitive_action_without_rehydrating_passwords(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path)))
    _post_form(
        client,
        "/register",
        data={"username": "alice", "email": "alice@example.com", "password": "supersecret"},
        follow_redirects=True,
    )

    failed = _post_form(
        client,
        "/dashboard/password",
        data={
            "current_password": "wrongsecret",
            "new_password": "newsecret123",
            "new_password_confirm": "newsecret123",
        },
    )

    assert failed.status_code == 401
    assert _action_disclosure_open(failed.text, "password-change")
    assert "Current password is incorrect." in failed.text
    assert 'value="newsecret123"' not in failed.text


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
    with training_db.connect(settings.database_path) as connection:
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

    with training_db.connect(settings.database_path) as connection:
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
    with training_db.connect(settings.database_path) as connection:
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

    with training_db.connect(settings.database_path) as connection:
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
            INSERT INTO auth_flow_tokens (
                created_at, user_id, flow_type, token_sha256, payload_json, expires_at, consumed_at,
                failed_attempts, source_ip, user_agent
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                old_iso,
                1,
                "login-mfa",
                hashlib.sha256(b"retention_old_auth_flow").hexdigest(),
                "{}",
                old_iso,
                old_iso,
                0,
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

    with training_db.connect(settings.database_path) as connection:
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
        auth_flow_row = connection.execute(
            "SELECT id FROM auth_flow_tokens WHERE token_sha256 = ?",
            (hashlib.sha256(b"retention_old_auth_flow").hexdigest(),),
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
        assert auth_flow_row is None
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
            "auth_flow_tokens": 0,
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
    totp_skew_steps: int = 2,
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
        totp_skew_steps=totp_skew_steps,
        webauthn_rp_id="testserver",
        webauthn_rp_name="ScamScreener",
        webauthn_origins=("http://testserver",),
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


def _extract_hidden_input_value(html: str, name: str) -> str:
    match = re.search(rf'<input[^>]+name="{re.escape(name)}"[^>]+value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _extract_pending_totp_secret(html: str) -> str:
    match = re.search(r'<strong class="summary-metric" style="font-size:1rem;">([A-Z2-7]+)</strong>', html)
    assert match is not None
    return match.group(1)


def _extract_first_backup_code(html: str) -> str:
    match = re.search(r"<code>([A-Z0-9]{4}-[A-Z0-9]{4})</code>", html)
    assert match is not None
    return match.group(1)


def _action_disclosure_present(html: str, action_id: str) -> bool:
    return f'data-sensitive-action="{action_id}"' in html


def _action_disclosure_open(html: str, action_id: str) -> bool:
    return (
        re.search(
            rf'<details[^>]+data-sensitive-action="{re.escape(action_id)}"[^>]*\bopen\b',
            html,
        )
        is not None
    )


def _current_totp_code(secret: str) -> str:
    return _totp_at(secret, int(datetime.now(timezone.utc).timestamp()))


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


def _anonymous_upload_headers(
    payload: str,
    *,
    client_id: str,
    filename: str = "training-cases-v2.jsonl",
    user_agent: str = "ScamScreener/1.0.0+1.20.1",
) -> dict[str, str]:
    normalized_client_id = client_id.strip().lower()
    payload_sha = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    handshake_sha = hashlib.sha256(f"{normalized_client_id}:{payload_sha}".encode("utf-8")).hexdigest()
    return {
        "Content-Type": "application/x-ndjson",
        "X-ScamScreener-Filename": filename,
        "X-ScamScreener-Client-Id": client_id,
        "X-ScamScreener-Payload-Sha256": payload_sha,
        "X-ScamScreener-Handshake-Sha256": handshake_sha,
        "User-Agent": user_agent,
    }


def _valid_payload(
    case_id: str = "case_000001",
    label: str = "risk",
    outcome: str = "review",
    messages: str = "[]",
    signal_message_indices: str = "[]",
    context_message_indices: str = "[]",
    excluded_message_indices: str = "[]",
) -> str:
    return (
        f'{{"format":"training_case_v2","schemaVersion":2,"caseId":"{case_id}",'
        f'"caseData":{{"label":"{label}","messages":{messages},"caseSignalTagIds":[]}},'
        f'"observedPipeline":{{"scoreAtCapture":0,"outcomeAtCapture":"{outcome}","decidedByStageId":"stage.rule","stageResults":[]}},'
        f'"supervision":{{"contextStage":{{"targetLabel":"risk","signalMessageIndices":{signal_message_indices},"contextMessageIndices":{context_message_indices},"excludedMessageIndices":{excluded_message_indices},"targetSignalTagIds":[]}},'
        '"fixedStageCalibrations":[]}}'
    )
