#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import compose_ops
import update as update_script

_BACKUP_ARCHIVES = {
    "scamscreener_data": "scamscreener-data-pre-oauth.tar.gz",
    "scamscreener_db_data": "scamscreener-db-pre-oauth.tar.gz",
    "caddy_data": "caddy-data-pre-oauth.tar.gz",
    "caddy_config": "caddy-config-pre-oauth.tar.gz",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backup and cut over an existing split Compose deployment from local sign-in to OAuth/OIDC.",
    )
    parser.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip the bash preflight validation.",
    )
    parser.add_argument(
        "--skip-pull",
        action="store_true",
        help="Skip upstream pulls during the Docker build step.",
    )
    parser.add_argument(
        "--health-timeout",
        type=int,
        default=180,
        help="Seconds to wait for each application service health check.",
    )
    parser.add_argument(
        "--log-tail-lines",
        type=int,
        default=80,
        help="How many recent compose log lines to show on failure.",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        help="Optional target directory for migration backups.",
    )
    return parser.parse_args(argv)


def _default_backup_dir(context: compose_ops.ComposeContext) -> Path:
    base_dir = context.repo_root.parent / "scamscreener-migration-backups"
    return base_dir / compose_ops.utc_timestamp_slug()


def _preflight_script(context: compose_ops.ComposeContext) -> Path:
    return context.repo_root / "scripts" / "preflight.sh"


def _assert_migration_candidate(context: compose_ops.ComposeContext) -> tuple[str, str]:
    if not compose_ops.deployment_state_exists(context):
        raise RuntimeError(
            "No existing deployment state was detected. Use python3 scripts/update.py for a fresh OAuth/OIDC deploy."
        )

    auth_mode = compose_ops.detect_running_auth_mode(context)
    marker_mode = compose_ops.read_deployment_auth_marker(context).strip().lower()
    if marker_mode == "external" or auth_mode == "external":
        raise RuntimeError(
            "The existing deployment already looks like an OAuth/OIDC stack. Use python3 scripts/update.py instead."
        )
    return auth_mode, marker_mode


def _write_backup_manifest(
    backup_dir: Path,
    *,
    auth_mode: str,
    marker_mode: str,
    resolved_volumes: dict[str, str],
) -> Path:
    manifest_path = backup_dir / "manifest.json"
    manifest = {
        "auth_mode": auth_mode,
        "marker_mode": marker_mode,
        "volumes": resolved_volumes,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path


def _backup_existing_state(context: compose_ops.ComposeContext, backup_dir: Path) -> dict[str, str]:
    resolved_volumes = compose_ops.resolve_named_volumes(context)
    if not resolved_volumes:
        raise RuntimeError("Could not resolve any persistent Compose volumes to back up before migration.")

    compose_ops.backup_repo_tree(context, backup_dir)
    for logical_name, archive_name in _BACKUP_ARCHIVES.items():
        volume_name = resolved_volumes.get(logical_name)
        if not volume_name:
            continue
        compose_ops.backup_named_volume(volume_name, backup_dir, archive_name, cwd=context.repo_root)
    return resolved_volumes


def run_migrate(context: compose_ops.ComposeContext, args: argparse.Namespace) -> int:
    compose_ops.require_command("docker")
    if not args.skip_preflight:
        compose_ops.require_command("bash")

    if not context.compose_file.is_file():
        raise FileNotFoundError(f"Compose file not found: {context.compose_file}")
    if not context.env_file.is_file():
        raise FileNotFoundError(f"Environment file not found: {context.env_file}")

    preflight_script = _preflight_script(context)
    if not args.skip_preflight and not preflight_script.is_file():
        raise FileNotFoundError(f"Preflight script not found: {preflight_script}")

    auth_mode, marker_mode = _assert_migration_candidate(context)
    backup_dir = (args.backup_dir or _default_backup_dir(context)).expanduser().resolve()
    if backup_dir.exists():
        raise RuntimeError(f"Backup directory already exists: {backup_dir}")
    backup_dir.mkdir(parents=True, exist_ok=False)

    resolved_volumes = _backup_existing_state(context, backup_dir)
    _write_backup_manifest(
        backup_dir,
        auth_mode=auth_mode,
        marker_mode=marker_mode,
        resolved_volumes=resolved_volumes,
    )

    if not args.skip_preflight:
        compose_ops.run_command(["bash", str(preflight_script)], cwd=context.repo_root)

    compose_ops.run_compose(context, ["down", "--remove-orphans"])
    update_args = argparse.Namespace(
        skip_preflight=True,
        skip_pull=args.skip_pull,
        health_timeout=args.health_timeout,
        log_tail_lines=args.log_tail_lines,
    )
    update_script.run_update(context, update_args)

    print(f"Migration backups: {backup_dir}")
    print("Rollback inputs: repo snapshot plus the archived Compose volumes in that directory.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    context = compose_ops.ComposeContext.from_script(__file__)
    try:
        return run_migrate(context, args)
    except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
