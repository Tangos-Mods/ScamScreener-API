from __future__ import annotations

import argparse
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_update_runs_preflight_build_up_and_health_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    update_module = _load_script_module("scamscreener_update_test", "update.py")
    compose_ops = update_module.compose_ops
    context = _compose_context(compose_ops, tmp_path)
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "preflight.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(compose_ops, "require_command", lambda name: calls.append(("require", name)))
    monkeypatch.setattr(
        compose_ops,
        "run_command",
        lambda command, *, cwd, capture_output=False: calls.append(("command", command)) or SimpleNamespace(stdout=""),
    )
    monkeypatch.setattr(
        compose_ops,
        "run_compose",
        lambda _context, args, *, capture_output=False: calls.append(("compose", args)) or SimpleNamespace(stdout=""),
    )
    monkeypatch.setattr(
        compose_ops,
        "wait_for_service_health",
        lambda _context, service_name, timeout_seconds, **_kwargs: calls.append(
            ("wait", (service_name, timeout_seconds))
        ),
    )
    monkeypatch.setattr(
        compose_ops,
        "ensure_service_running",
        lambda _context, service_name: calls.append(("running", service_name)),
    )
    monkeypatch.setattr(
        compose_ops,
        "write_deployment_auth_marker",
        lambda _context, mode="external": calls.append(("marker", mode)),
    )

    args = argparse.Namespace(skip_preflight=False, skip_pull=False, health_timeout=120, log_tail_lines=40)

    assert update_module.run_update(context, args) == 0
    assert ("require", "docker") in calls
    assert ("require", "bash") in calls
    assert ("command", ["bash", str(tmp_path / "scripts" / "preflight.sh")]) in calls
    assert ("compose", ["build", "--pull"]) in calls
    assert ("compose", ["up", "-d", "--remove-orphans"]) in calls
    assert ("wait", ("scamscreener-db", 120)) in calls
    assert ("wait", ("scamscreener-hub", 120)) in calls
    assert ("wait", ("scamscreener-api", 120)) in calls
    assert ("wait", ("marketguard-hub", 120)) in calls
    assert ("compose", ["up", "-d", "--force-recreate", "caddy"]) in calls
    assert ("running", "caddy") in calls
    assert ("marker", "external") in calls
    assert ("compose", ["ps"]) in calls


def test_update_waits_for_optional_redis_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    update_module = _load_script_module("scamscreener_update_redis_test", "update.py")
    compose_ops = update_module.compose_ops
    context = _compose_context(
        compose_ops,
        tmp_path,
        env_contents=(
            "TRAINING_HUB_ENV=production\n"
            "MARKETGUARD_REDIS_ENABLED=true\n"
            "SCAMSCREENER_REDIS_MANAGED=true\n"
        ),
    )
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "preflight.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    compose_calls: list[list[str]] = []
    waits: list[str] = []

    monkeypatch.setattr(compose_ops, "require_command", lambda _name: None)
    monkeypatch.setattr(
        compose_ops,
        "run_command",
        lambda command, *, cwd, capture_output=False: SimpleNamespace(stdout=""),
    )
    monkeypatch.setattr(
        compose_ops,
        "run_compose",
        lambda _context, args, *, capture_output=False: compose_calls.append(args) or SimpleNamespace(stdout=""),
    )
    monkeypatch.setattr(
        compose_ops,
        "wait_for_service_health",
        lambda _context, service_name, timeout_seconds, **_kwargs: waits.append(service_name),
    )
    monkeypatch.setattr(
        compose_ops,
        "ensure_service_running",
        lambda _context, service_name: None,
    )
    monkeypatch.setattr(
        compose_ops,
        "write_deployment_auth_marker",
        lambda _context, mode="external": None,
    )

    args = argparse.Namespace(skip_preflight=False, skip_pull=True, health_timeout=90, log_tail_lines=40)
    assert update_module.run_update(context, args) == 0
    assert "scamscreener-redis" in waits
    assert "marketguard-hub" in waits
    assert compose_calls == [
        ["build"],
        ["up", "-d", "--remove-orphans"],
        ["up", "-d", "--force-recreate", "caddy"],
        ["ps"],
    ]


def test_update_rejects_running_legacy_local_auth_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    update_module = _load_script_module("scamscreener_update_legacy_auth_test", "update.py")
    compose_ops = update_module.compose_ops
    context = _compose_context(compose_ops, tmp_path)
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "preflight.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    monkeypatch.setattr(compose_ops, "require_command", lambda _name: None)
    monkeypatch.setattr(compose_ops, "detect_running_auth_mode", lambda _context: "local")

    with pytest.raises(RuntimeError, match="scripts/migrate.py"):
        update_module.run_update(
            context,
            argparse.Namespace(skip_preflight=False, skip_pull=False, health_timeout=90, log_tail_lines=40),
        )


def test_update_allows_runtime_probe_mismatch_when_external_marker_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    update_module = _load_script_module("scamscreener_update_marker_override_test", "update.py")
    compose_ops = update_module.compose_ops
    context = _compose_context(compose_ops, tmp_path)
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "preflight.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    monkeypatch.setattr(compose_ops, "require_command", lambda _name: None)
    monkeypatch.setattr(compose_ops, "detect_running_auth_mode", lambda _context: "local")
    monkeypatch.setattr(compose_ops, "read_deployment_auth_marker", lambda _context: "external")
    monkeypatch.setattr(
        compose_ops,
        "run_command",
        lambda command, *, cwd, capture_output=False: SimpleNamespace(stdout=""),
    )
    monkeypatch.setattr(
        compose_ops,
        "run_compose",
        lambda _context, args, *, capture_output=False: SimpleNamespace(stdout=""),
    )
    monkeypatch.setattr(
        compose_ops,
        "wait_for_service_health",
        lambda _context, service_name, timeout_seconds, **_kwargs: None,
    )
    monkeypatch.setattr(
        compose_ops,
        "ensure_service_running",
        lambda _context, service_name: None,
    )
    monkeypatch.setattr(
        compose_ops,
        "write_deployment_auth_marker",
        lambda _context, mode="external": None,
    )

    result = update_module.run_update(
        context,
        argparse.Namespace(skip_preflight=False, skip_pull=True, health_timeout=90, log_tail_lines=40),
    )

    assert result == 0
    captured = capsys.readouterr()
    assert "deployment marker says OAuth/OIDC is already active" in captured.err


def test_caddyfile_routes_marketguard_hub() -> None:
    caddyfile = (Path(__file__).resolve().parents[1] / "Caddyfile").read_text(encoding="utf-8")

    assert "handle_path /market*" in caddyfile
    assert "reverse_proxy marketguard-hub:8082" in caddyfile
    assert "/api/v1/ready" in caddyfile


def test_compose_marketguard_hub_healthcheck_uses_allowed_host_header() -> None:
    compose_file = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(encoding="utf-8")

    assert "/internal/health" in compose_file
    assert "headers={'Host': host, 'X-Forwarded-Proto': 'https'}" in compose_file


def test_reset_aborts_without_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reset_module = _load_script_module("scamscreener_reset_abort_test", "reset.py")
    compose_ops = reset_module.compose_ops
    context = _compose_context(compose_ops, tmp_path)

    compose_calls: list[list[str]] = []

    monkeypatch.setattr(compose_ops, "require_command", lambda _name: None)
    monkeypatch.setattr(reset_module, "_confirm_reset", lambda _skip_prompt, **_kwargs: False)
    monkeypatch.setattr(
        compose_ops,
        "run_compose",
        lambda _context, args, *, capture_output=False: compose_calls.append(args) or SimpleNamespace(stdout=""),
    )

    with pytest.raises(RuntimeError, match="Reset aborted by user."):
        reset_module.run_reset(context, argparse.Namespace(yes=False, prune_images=False))

    assert compose_calls == []


def test_reset_runs_down_with_volumes_and_optional_image_prune(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reset_module = _load_script_module("scamscreener_reset_run_test", "reset.py")
    compose_ops = reset_module.compose_ops
    context = _compose_context(compose_ops, tmp_path)

    compose_calls: list[list[str]] = []

    monkeypatch.setattr(compose_ops, "require_command", lambda _name: None)
    monkeypatch.setattr(reset_module, "_confirm_reset", lambda _skip_prompt, **_kwargs: True)
    monkeypatch.setattr(
        compose_ops,
        "run_compose",
        lambda _context, args, *, capture_output=False: compose_calls.append(args) or SimpleNamespace(stdout=""),
    )

    assert reset_module.run_reset(context, argparse.Namespace(yes=True, prune_images=True)) == 0
    assert compose_calls == [
        ["down", "--volumes", "--remove-orphans", "--rmi", "local"],
        ["ps"],
    ]


def test_migrate_backs_up_state_and_restarts_with_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    migrate_module = _load_script_module("scamscreener_migrate_run_test", "migrate.py")
    compose_ops = migrate_module.compose_ops
    context = _compose_context(compose_ops, tmp_path)
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "preflight.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    compose_calls: list[list[str]] = []
    command_calls: list[list[str]] = []
    update_calls: list[argparse.Namespace] = []

    monkeypatch.setattr(compose_ops, "require_command", lambda _name: None)
    monkeypatch.setattr(compose_ops, "deployment_state_exists", lambda _context: True)
    monkeypatch.setattr(compose_ops, "detect_running_auth_mode", lambda _context: "local")
    monkeypatch.setattr(compose_ops, "read_deployment_auth_marker", lambda _context: "")
    monkeypatch.setattr(
        compose_ops,
        "resolve_named_volumes",
        lambda _context: {
            "scamscreener_data": "project_scamscreener_data",
            "scamscreener_db_data": "project_scamscreener_db_data",
        },
    )
    monkeypatch.setattr(compose_ops, "backup_repo_tree", lambda _context, destination_dir: destination_dir / "repo")
    monkeypatch.setattr(
        compose_ops,
        "backup_named_volume",
        lambda volume_name, destination_dir, archive_name, *, cwd: destination_dir / archive_name,
    )
    monkeypatch.setattr(
        compose_ops,
        "run_command",
        lambda command, *, cwd, capture_output=False: command_calls.append(command) or SimpleNamespace(stdout=""),
    )
    monkeypatch.setattr(
        compose_ops,
        "run_compose",
        lambda _context, args, *, capture_output=False: compose_calls.append(args) or SimpleNamespace(stdout=""),
    )
    monkeypatch.setattr(
        migrate_module.update_script,
        "run_update",
        lambda _context, args: update_calls.append(args) or 0,
    )

    backup_dir = tmp_path / "backups"
    result = migrate_module.run_migrate(
        context,
        argparse.Namespace(
            skip_preflight=False,
            skip_pull=True,
            health_timeout=75,
            log_tail_lines=55,
            backup_dir=backup_dir,
        ),
    )

    assert result == 0
    assert command_calls == [["bash", str(tmp_path / "scripts" / "preflight.sh")]]
    assert compose_calls == [["down", "--remove-orphans"]]
    assert len(update_calls) == 1
    assert update_calls[0].skip_preflight is True
    assert update_calls[0].skip_pull is True
    assert update_calls[0].health_timeout == 75
    assert update_calls[0].log_tail_lines == 55
    assert (backup_dir / "manifest.json").is_file()


def test_migrate_rejects_already_external_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    migrate_module = _load_script_module("scamscreener_migrate_external_test", "migrate.py")
    compose_ops = migrate_module.compose_ops
    context = _compose_context(compose_ops, tmp_path)

    monkeypatch.setattr(compose_ops, "require_command", lambda _name: None)
    monkeypatch.setattr(compose_ops, "deployment_state_exists", lambda _context: True)
    monkeypatch.setattr(compose_ops, "detect_running_auth_mode", lambda _context: "external")
    monkeypatch.setattr(compose_ops, "read_deployment_auth_marker", lambda _context: "")

    with pytest.raises(RuntimeError, match="scripts/update.py"):
        migrate_module.run_migrate(
            context,
            argparse.Namespace(
                skip_preflight=True,
                skip_pull=False,
                health_timeout=75,
                log_tail_lines=55,
                backup_dir=tmp_path / "backups",
            ),
        )


def _compose_context(compose_ops_module, tmp_path: Path, *, env_contents: str = "TRAINING_HUB_ENV=production\n"):
    compose_file = tmp_path / "docker-compose.yml"
    env_file = tmp_path / ".env.production"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    env_file.write_text(env_contents, encoding="utf-8")
    return compose_ops_module.ComposeContext(
        repo_root=tmp_path,
        compose_file=compose_file,
        env_file=env_file,
    )


def _load_script_module(module_name: str, filename: str):
    scripts_dir = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    script_path = scripts_dir / filename
    spec = spec_from_file_location(module_name, script_path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
