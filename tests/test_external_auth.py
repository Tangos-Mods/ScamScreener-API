from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.training_hub.config.settings import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME, TrainingHubSettings
from app.training_hub.core.external_auth import ExternalAuthProfile
from app.training_hub.core.mfa import _create_auth_flow
from app.training_hub.core.session_auth import _create_session
from app.training_hub.core.session_auth_password import _hash_password
from app.training_hub.infra import db as sqlite3
from app.training_hub.main import create_training_hub_app


def _settings(tmp_path: Path) -> TrainingHubSettings:
    return TrainingHubSettings(
        host="127.0.0.1",
        port=18080,
        database_url="",
        secret_key="test-secret-key-for-security-check-123456",
        session_ttl_minutes=240,
        max_upload_bytes=5 * 1024 * 1024,
        storage_dir=tmp_path / "data",
        pipeline_command="",
        project_root=tmp_path,
        admin_emails=set(),
        admin_usernames={"owner"},
        trusted_proxies=set(),
        public_base_url="http://testserver",
        github_oauth_client_id="github-client",
        github_oauth_client_secret="github-secret",
        github_oauth_allowed_logins=("owner",),
        authelia_oidc_issuer_url="https://auth.example.com",
        authelia_oidc_client_id="authelia-client",
        authelia_oidc_client_secret="authelia-secret",
        authelia_oidc_allowed_subjects=("authelia-owner",),
        webauthn_rp_id="testserver",
        webauthn_rp_name="ScamScreener",
        webauthn_origins=("http://testserver",),
        enforce_https=False,
        enable_rate_limit=True,
        enforce_origin_check=False,
    )


def _create_user(settings: TrainingHubSettings, *, username: str, email: str, is_admin: bool = True) -> int:
    create_training_hub_app(settings)
    with sqlite3.connect(settings.database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO users (created_at, username, email, password_hash, is_admin, mfa_enabled, last_login_at)
            VALUES (?, ?, ?, ?, ?, 0, ?)
            """,
            (
                "2026-01-01T00:00:00Z",
                username,
                email,
                _hash_password("unused-external-auth-password"),
                1 if is_admin else 0,
                "2026-01-01T00:00:00Z",
            ),
        )
        connection.commit()
    return int(cursor.lastrowid)


def _link_identity(
    settings: TrainingHubSettings,
    *,
    user_id: int,
    provider: str,
    issuer: str,
    subject: str,
    username: str,
    email: str,
) -> None:
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            """
            INSERT INTO external_identities (
                created_at,
                updated_at,
                user_id,
                provider,
                issuer,
                subject,
                email,
                username,
                last_login_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:00:00Z",
                int(user_id),
                provider,
                issuer,
                subject,
                email,
                username,
                "2026-01-01T00:00:00Z",
            ),
        )
        connection.commit()


def _login_client(client: TestClient, settings: TrainingHubSettings, *, user_id: int) -> None:
    token = _create_session(
        settings.database_path,
        user_id=user_id,
        ttl_minutes=settings.session_ttl_minutes,
        remote_addr="testclient",
        user_agent="testclient",
        secret_key=settings.secret_key,
    )
    client.cookies.set(SESSION_COOKIE_NAME, token)


def _set_confirm_flow_cookie(
    client: TestClient,
    settings: TrainingHubSettings,
    *,
    user_id: int,
    page: str,
    action: str,
    values: dict[str, str] | None = None,
) -> str:
    flow = _create_auth_flow(
        settings,
        user_id=user_id,
        flow_type="account-confirm",
        payload={"page": page, "action": action, "values": dict(values or {})},
        ttl_minutes=10,
        source_ip="testclient",
        user_agent="testclient",
    )
    client.cookies.set("training_hub_account_confirm", str(flow["token"]))
    return str(flow["token"])


def _csrf_token(client: TestClient) -> str:
    token = client.cookies.get(CSRF_COOKIE_NAME)
    if token:
        return str(token)
    client.get("/login")
    token = client.cookies.get(CSRF_COOKIE_NAME)
    assert token is not None
    return str(token)


def test_login_page_lists_external_providers(tmp_path: Path) -> None:
    client = TestClient(create_training_hub_app(_settings(tmp_path)))

    response = client.get("/login")

    assert response.status_code == 200
    assert "Continue with GitHub" in response.text
    assert "Continue with Authelia" in response.text
    assert "Register" not in response.text


def test_external_auth_start_redirects_to_provider(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(create_training_hub_app(_settings(tmp_path)))

    def _fake_start(_settings, *, provider: str, next_path: str, reauth: bool, source_ip: str = "", user_agent: str = ""):
        assert provider == "github"
        assert next_path == "/dashboard"
        assert reauth is False
        return {"ok": True, "redirect_url": "https://github.com/login/oauth/authorize?client_id=test"}

    monkeypatch.setattr("app.training_hub.routes.public_auth_external.create_external_auth_redirect", _fake_start)

    response = client.get("/auth/external/github", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "https://github.com/login/oauth/authorize?client_id=test"


def test_external_auth_callback_sets_session_cookie_and_redirects(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    app = create_training_hub_app(settings)
    client = TestClient(app)

    profile = ExternalAuthProfile(
        provider="github",
        issuer="https://github.com",
        subject="12345",
        email="owner@example.com",
        username="owner",
        display_name="Owner",
    )

    def _fake_exchange(_settings, *, provider: str, state: str, code: str, source_ip: str = "", user_agent: str = ""):
        assert provider == "github"
        assert state == "state-123"
        assert code == "code-123"
        return {"ok": True, "redirect_path": "/dashboard", "profile": profile}

    monkeypatch.setattr("app.training_hub.routes.public_auth_external.complete_external_auth_exchange", _fake_exchange)

    callback = client.get("/auth/external/github/callback?state=state-123&code=code-123", follow_redirects=False)

    assert callback.status_code == 303
    assert callback.headers["location"] == "/dashboard"

    dashboard = client.get("/dashboard")
    assert dashboard.status_code == 200


def test_legacy_local_auth_routes_are_absent(tmp_path: Path) -> None:
    client = TestClient(create_training_hub_app(_settings(tmp_path)))

    assert client.get("/register", follow_redirects=False).status_code == 404
    assert client.post("/forgot-password", follow_redirects=False).status_code == 404
    assert client.post("/login/passkey/options", follow_redirects=False).status_code == 404
    assert client.post("/api/v1/client/auth/login", json={"usernameOrEmail": "owner", "password": "secret"}).status_code == 404


def test_account_security_page_only_shows_external_provider_status(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    user_id = _create_user(settings, username="owner", email="owner@example.com")
    _link_identity(
        settings,
        user_id=user_id,
        provider="github",
        issuer="https://github.com",
        subject="12345",
        username="owner",
        email="owner@example.com",
    )
    client = TestClient(create_training_hub_app(settings))
    _login_client(client, settings, user_id=user_id)

    response = client.get("/account/security")

    assert response.status_code == 200
    assert "Provider-managed authentication" in response.text
    assert "Local passwords, password reset, authenticator apps, passkeys, and backup codes" in response.text
    assert "Update password" not in response.text
    assert "Registered passkeys" not in response.text
    assert "Backup codes" not in response.text


def test_account_confirm_shows_provider_selection_for_multiple_identities(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    user_id = _create_user(settings, username="owner", email="owner@example.com")
    _link_identity(
        settings,
        user_id=user_id,
        provider="github",
        issuer="https://github.com",
        subject="12345",
        username="owner",
        email="owner@example.com",
    )
    _link_identity(
        settings,
        user_id=user_id,
        provider="authelia",
        issuer="https://auth.example.com",
        subject="authelia-owner",
        username="owner",
        email="owner@example.com",
    )
    client = TestClient(create_training_hub_app(settings))
    _login_client(client, settings, user_id=user_id)
    _set_confirm_flow_cookie(client, settings, user_id=user_id, page="privacy", action="data-purge")

    response = client.get("/account/confirm")

    assert response.status_code == 200
    assert "Continue with GitHub" in response.text
    assert "Continue with Authelia" in response.text


def test_account_confirm_single_provider_starts_external_reauth(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    user_id = _create_user(settings, username="owner", email="owner@example.com")
    _link_identity(
        settings,
        user_id=user_id,
        provider="github",
        issuer="https://github.com",
        subject="12345",
        username="owner",
        email="owner@example.com",
    )
    client = TestClient(create_training_hub_app(settings))
    _login_client(client, settings, user_id=user_id)
    confirm_token = _set_confirm_flow_cookie(client, settings, user_id=user_id, page="privacy", action="data-purge")

    def _fake_start(_settings, *, provider: str, next_path: str, reauth: bool, source_ip: str = "", user_agent: str = ""):
        assert provider == "github"
        assert reauth is True
        assert confirm_token in next_path
        return {"ok": True, "redirect_url": "https://github.com/login/oauth/authorize?stepup=1"}

    monkeypatch.setattr("app.training_hub.routes.public_dashboard_account.create_external_auth_redirect", _fake_start)

    response = client.get("/account/confirm", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/account/confirm/external/github"

    launch = client.get("/account/confirm/external/github", follow_redirects=False)
    assert launch.status_code == 303
    assert launch.headers["location"] == "https://github.com/login/oauth/authorize?stepup=1"


def test_external_step_up_callback_executes_confirmed_action_for_same_user(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    user_id = _create_user(settings, username="owner", email="owner@example.com")
    _link_identity(
        settings,
        user_id=user_id,
        provider="github",
        issuer="https://github.com",
        subject="12345",
        username="owner",
        email="owner@example.com",
    )
    client = TestClient(create_training_hub_app(settings))
    _login_client(client, settings, user_id=user_id)
    confirm_token = _set_confirm_flow_cookie(client, settings, user_id=user_id, page="privacy", action="data-purge")

    profile = ExternalAuthProfile(
        provider="github",
        issuer="https://github.com",
        subject="12345",
        email="owner@example.com",
        username="owner",
        display_name="Owner",
    )

    def _fake_exchange(_settings, *, provider: str, state: str, code: str, source_ip: str = "", user_agent: str = ""):
        assert provider == "github"
        return {
            "ok": True,
            "redirect_path": f"/account/confirm/complete?confirm_token={confirm_token}",
            "profile": profile,
        }

    monkeypatch.setattr("app.training_hub.routes.public_auth_external.complete_external_auth_exchange", _fake_exchange)

    callback = client.get("/auth/external/github/callback?state=state-123&code=code-123", follow_redirects=False)
    assert callback.status_code == 303
    assert callback.headers["location"] == "/account/confirm/complete"

    completed = client.get("/account/confirm/complete")
    assert completed.status_code == 200
    assert "Deleted 0 uploads." in completed.text

    with sqlite3.connect(settings.database_path) as connection:
        audit_rows = connection.execute(
            "SELECT action FROM audit_logs WHERE actor_user_id = ? ORDER BY id ASC",
            (user_id,),
        ).fetchall()
    assert ("auth.external.step_up.success",) in audit_rows
    assert ("account.data.purged",) in audit_rows


def test_external_step_up_callback_rejects_mismatched_user(tmp_path: Path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    owner_id = _create_user(settings, username="owner", email="owner@example.com")
    other_id = _create_user(settings, username="other", email="other@example.com", is_admin=False)
    _link_identity(
        settings,
        user_id=other_id,
        provider="github",
        issuer="https://github.com",
        subject="99999",
        username="other",
        email="other@example.com",
    )
    client = TestClient(create_training_hub_app(settings))
    _login_client(client, settings, user_id=owner_id)
    confirm_token = _set_confirm_flow_cookie(client, settings, user_id=owner_id, page="privacy", action="data-purge")

    profile = ExternalAuthProfile(
        provider="github",
        issuer="https://github.com",
        subject="99999",
        email="other@example.com",
        username="other",
        display_name="Other",
    )

    def _fake_exchange(_settings, *, provider: str, state: str, code: str, source_ip: str = "", user_agent: str = ""):
        return {
            "ok": True,
            "redirect_path": f"/account/confirm/complete?confirm_token={confirm_token}",
            "profile": profile,
        }

    monkeypatch.setattr("app.training_hub.routes.public_auth_external.complete_external_auth_exchange", _fake_exchange)

    callback = client.get("/auth/external/github/callback?state=state-123&code=code-123", follow_redirects=False)

    assert callback.status_code == 303
    assert callback.headers["location"] == "/account/privacy?error=External+confirmation+did+not+match+this+account"

    with sqlite3.connect(settings.database_path) as connection:
        blocked = connection.execute(
            "SELECT action, target_id FROM audit_logs WHERE actor_user_id = ? ORDER BY id DESC LIMIT 1",
            (owner_id,),
        ).fetchone()
    assert blocked == ("auth.external.step_up.blocked", owner_id)


def test_logout_revokes_session_after_external_login(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    user_id = _create_user(settings, username="owner", email="owner@example.com")
    client = TestClient(create_training_hub_app(settings))
    _login_client(client, settings, user_id=user_id)

    response = client.post(
        "/logout",
        data={"csrf_token": _csrf_token(client)},
        headers={"Origin": "http://testserver", "Referer": "http://testserver/logout"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login?notice=Signed+out"
    assert client.get("/dashboard", follow_redirects=False).status_code == 303
