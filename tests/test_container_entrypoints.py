from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_hub_entrypoint_disables_tls_for_managed_internal_db(tmp_path: Path) -> None:
    output = _run_entrypoint(
        tmp_path,
        {
            "SCAMSCREENER_APP_MODE": "hub",
            "SCAMSCREENER_DB_MANAGED": "true",
            "TRAINING_HUB_SECRET_KEY": "x" * 32,
        },
        [
            "TRAINING_HUB_DB_DRIVER",
            "TRAINING_HUB_DB_HOST",
            "TRAINING_HUB_DB_PORT",
            "TRAINING_HUB_DB_NAME",
            "TRAINING_HUB_DB_USER",
            "TRAINING_HUB_DB_PASSWORD",
            "TRAINING_HUB_DB_REQUIRE_TLS",
            "TRAINING_HUB_DB_SSL_CA",
        ],
    )

    assert output["TRAINING_HUB_DB_DRIVER"] == "mariadb"
    assert output["TRAINING_HUB_DB_HOST"] == "scamscreener-db"
    assert output["TRAINING_HUB_DB_PORT"] == "3306"
    assert output["TRAINING_HUB_DB_NAME"] == "scamscreener_hub"
    assert output["TRAINING_HUB_DB_USER"] == "scamscreener"
    assert output["TRAINING_HUB_DB_PASSWORD"] == "db-pass"
    assert output["TRAINING_HUB_DB_REQUIRE_TLS"] == "false"
    assert output["TRAINING_HUB_DB_SSL_CA"] == ""


def test_api_entrypoint_disables_tls_for_managed_internal_db(tmp_path: Path) -> None:
    output = _run_entrypoint(
        tmp_path,
        {
            "SCAMSCREENER_APP_MODE": "api",
            "SCAMSCREENER_DB_MANAGED": "true",
            "MARKETGUARD_REDIS_ENABLED": "false",
        },
        [
            "MARKETGUARD_DB_DRIVER",
            "MARKETGUARD_DB_HOST",
            "MARKETGUARD_DB_PORT",
            "MARKETGUARD_DB_NAME",
            "MARKETGUARD_DB_USER",
            "MARKETGUARD_DB_PASSWORD",
            "MARKETGUARD_DB_REQUIRE_TLS",
            "MARKETGUARD_DB_SSL_CA",
        ],
    )

    assert output["MARKETGUARD_DB_DRIVER"] == "mariadb"
    assert output["MARKETGUARD_DB_HOST"] == "scamscreener-db"
    assert output["MARKETGUARD_DB_PORT"] == "3306"
    assert output["MARKETGUARD_DB_NAME"] == "scamscreener_hub"
    assert output["MARKETGUARD_DB_USER"] == "scamscreener"
    assert output["MARKETGUARD_DB_PASSWORD"] == "db-pass"
    assert output["MARKETGUARD_DB_REQUIRE_TLS"] == "false"
    assert output["MARKETGUARD_DB_SSL_CA"] == ""


def test_marketguard_hub_entrypoint_uses_market_app_mode(tmp_path: Path) -> None:
    output = _run_entrypoint(
        tmp_path,
        {
            "SCAMSCREENER_APP_MODE": "market",
        },
        [],
    )

    assert output["UVICORN_ARGS"].startswith("app.marketguard_hub.main:create_app ")


def _run_entrypoint(tmp_path: Path, extra_env: dict[str, str], keys: list[str]) -> dict[str, str]:
    runtime_dir = tmp_path / "runtime" / "mariadb"
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "app-password").write_text("db-pass", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    (bin_dir / "python").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    uvicorn_lines = ["#!/bin/sh", 'printf "UVICORN_ARGS=%s\\n" "$*"']
    uvicorn_lines.extend(f'printf "{key}=%s\\n" "${{{key}:-}}"' for key in keys)
    (bin_dir / "uvicorn").write_text("\n".join(uvicorn_lines) + "\n", encoding="utf-8")

    for path in (bin_dir / "python", bin_dir / "uvicorn"):
        path.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "SCAMSCREENER_DB_PASSWORD_FILE": str(runtime_dir / "app-password"),
            "SCAMSCREENER_DB_HOST": "scamscreener-db",
            "SCAMSCREENER_DB_PORT": "3306",
            "SCAMSCREENER_DB_NAME": "scamscreener_hub",
            "SCAMSCREENER_DB_USER": "scamscreener",
        }
    )
    env.update(extra_env)

    script_path = Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh"
    result = subprocess.run(
        ["sh", str(script_path)],
        env=env,
        check=True,
        text=True,
        capture_output=True,
    )
    lines = [line for line in result.stdout.splitlines() if "=" in line]
    return dict(line.split("=", 1) for line in lines)
