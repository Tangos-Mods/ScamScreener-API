import os
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.append(str(Path(__file__).resolve().parents[1]))


def test_from_env_builds_non_tls_mariadb_dsn_for_local_compose(monkeypatch) -> None:
    _clear_training_hub_env(monkeypatch)
    settings_module = _load_settings_module()
    monkeypatch.setattr(settings_module, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("TRAINING_HUB_ENV", "development")
    monkeypatch.setenv("TRAINING_HUB_DB_DRIVER", "mariadb")
    monkeypatch.setenv("TRAINING_HUB_DB_HOST", "mariadb")
    monkeypatch.setenv("TRAINING_HUB_DB_PASSWORD", "local-pass")
    monkeypatch.setenv("TRAINING_HUB_DB_REQUIRE_TLS", "false")

    settings = settings_module.TrainingHubSettings.from_env()

    parsed = urlsplit(settings.database_url)
    assert parsed.scheme == "mariadb"
    assert parsed.hostname == "mariadb"
    assert parsed.query == ""


def test_from_env_builds_verified_tls_mariadb_dsn_for_production(monkeypatch) -> None:
    _clear_training_hub_env(monkeypatch)
    settings_module = _load_settings_module()
    monkeypatch.setattr(settings_module, "load_dotenv", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("TRAINING_HUB_ENV", "production")
    monkeypatch.setenv("TRAINING_HUB_ALLOWED_HOSTS", "scamscreener.example.com")
    monkeypatch.setenv("TRAINING_HUB_DB_DRIVER", "mariadb")
    monkeypatch.setenv("TRAINING_HUB_DB_HOST", "db.internal")
    monkeypatch.setenv("TRAINING_HUB_DB_PASSWORD", "prod-pass")
    monkeypatch.setenv("TRAINING_HUB_DB_REQUIRE_TLS", "true")
    monkeypatch.setenv("TRAINING_HUB_DB_SSL_CA", "/etc/ssl/certs/db-ca.pem")
    monkeypatch.setenv("TRAINING_HUB_SECRET_KEY", "x" * 32)
    monkeypatch.setenv("TRAINING_HUB_ADMIN_MFA_REQUIRED", "true")
    monkeypatch.setenv("TRAINING_HUB_ENFORCE_HTTPS", "true")
    monkeypatch.setenv("TRAINING_HUB_SMTP_HOST", "smtp.internal")
    monkeypatch.setenv("TRAINING_HUB_SMTP_FROM_EMAIL", "no-reply@scamscreener.example.com")

    settings = settings_module.TrainingHubSettings.from_env()

    parsed = urlsplit(settings.database_url)
    query = parse_qs(parsed.query)
    assert parsed.scheme == "mariadb"
    assert parsed.hostname == "db.internal"
    assert query["ssl_mode"] == ["verify-full"]
    assert query["ssl_ca"] == ["/etc/ssl/certs/db-ca.pem"]


def _clear_training_hub_env(monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith("TRAINING_HUB_"):
            monkeypatch.delenv(key, raising=False)


def _load_settings_module():
    settings_path = Path(__file__).resolve().parents[1] / "app" / "config" / "settings.py"
    spec = spec_from_file_location("training_hub_test_settings", settings_path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
