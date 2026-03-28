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

    args = argparse.Namespace(skip_preflight=False, skip_pull=False, health_timeout=120, log_tail_lines=40)

    assert update_module.run_update(context, args) == 0
    assert ("require", "docker") in calls
    assert ("require", "bash") in calls
    assert ("command", ["bash", str(tmp_path / "scripts" / "preflight.sh")]) in calls
    assert ("compose", ["build", "--pull"]) in calls
    assert ("compose", ["up", "-d", "--remove-orphans"]) in calls
    assert ("wait", ("scamscreener", 120)) in calls
    assert ("running", "caddy") in calls
    assert ("compose", ["ps"]) in calls


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


def _compose_context(compose_ops_module, tmp_path: Path):
    compose_file = tmp_path / "docker-compose.yml"
    env_file = tmp_path / ".env.production"
    compose_file.write_text("services: {}\n", encoding="utf-8")
    env_file.write_text("TRAINING_HUB_ENV=production\n", encoding="utf-8")
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
