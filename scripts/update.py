#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import compose_ops

_BASE_HEALTHCHECKED_SERVICES = ("scamscreener-db", "scamscreener-hub", "scamscreener-api", "marketguard-hub")
_RUNNING_ONLY_SERVICES = ("caddy",)
_FORCE_RECREATE_SERVICES = ("caddy",)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build and restart the production ScamScreener Docker Compose stack.",
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
        help="Seconds to wait for the application service health check.",
    )
    parser.add_argument(
        "--log-tail-lines",
        type=int,
        default=80,
        help="How many recent compose log lines to show on failure.",
    )
    return parser.parse_args(argv)


def _preflight_script(context: compose_ops.ComposeContext) -> Path:
    return context.repo_root / "scripts" / "preflight.sh"


def _configured_external_providers(context: compose_ops.ComposeContext) -> list[str]:
    providers: list[str] = []
    github_client_id = compose_ops.read_env_value(context.env_file, "TRAINING_HUB_GITHUB_OAUTH_CLIENT_ID")
    github_client_secret = compose_ops.read_env_value(context.env_file, "TRAINING_HUB_GITHUB_OAUTH_CLIENT_SECRET")
    authelia_issuer = compose_ops.read_env_value(context.env_file, "TRAINING_HUB_AUTHELIA_OIDC_ISSUER_URL")
    authelia_client_id = compose_ops.read_env_value(context.env_file, "TRAINING_HUB_AUTHELIA_OIDC_CLIENT_ID")
    authelia_client_secret = compose_ops.read_env_value(context.env_file, "TRAINING_HUB_AUTHELIA_OIDC_CLIENT_SECRET")
    if github_client_id and github_client_secret:
        providers.append("GitHub")
    if authelia_issuer and authelia_client_id and authelia_client_secret:
        providers.append("Authelia")
    return providers


def _bootstrap_admin_summary(context: compose_ops.ComposeContext) -> str:
    usernames = compose_ops.read_env_value(context.env_file, "TRAINING_HUB_ADMIN_USERNAMES")
    emails = compose_ops.read_env_value(context.env_file, "TRAINING_HUB_ADMIN_EMAILS")
    parts: list[str] = []
    if usernames:
        parts.append(f"usernames={usernames}")
    if emails:
        parts.append(f"emails={emails}")
    return ", ".join(parts) if parts else "not configured"


def _assert_not_running_legacy_local_auth_stack(context: compose_ops.ComposeContext) -> None:
    marker_mode = compose_ops.read_deployment_auth_marker(context).strip().lower()
    auth_mode = compose_ops.detect_running_auth_mode(context)
    if marker_mode == "external":
        if auth_mode == "local":
            print(
                "Warning: deployment marker says OAuth/OIDC is already active, "
                "but the runtime route probe still looks like legacy local auth. Continuing update.",
                file=sys.stderr,
            )
        return
    if auth_mode == "local":
        raise RuntimeError(
            "The running stack still exposes legacy local sign-in routes. "
            "Run python3 scripts/migrate.py for the one-time OAuth/OIDC cutover."
        )


def _print_success_summary(context: compose_ops.ComposeContext) -> None:
    public_base_url = compose_ops.read_env_value(context.env_file, "TRAINING_HUB_PUBLIC_BASE_URL")
    providers = _configured_external_providers(context)
    print("Update completed successfully.")
    if public_base_url:
        print(f"Login URL: {public_base_url.rstrip('/')}/login")
    if providers:
        print(f"External providers: {', '.join(providers)}")
    print(f"Bootstrap admin anchors: {_bootstrap_admin_summary(context)}")


def _write_marker_best_effort(context: compose_ops.ComposeContext) -> None:
    try:
        compose_ops.write_deployment_auth_marker(context, "external")
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Warning: could not persist the OAuth deployment marker: {exc}", file=sys.stderr)


def run_update(context: compose_ops.ComposeContext, args: argparse.Namespace) -> int:
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
    _assert_not_running_legacy_local_auth_stack(context)
    healthchecked_services = list(_BASE_HEALTHCHECKED_SERVICES)
    if compose_ops.marketguard_redis_enabled(context):
        healthchecked_services.append("scamscreener-redis")

    try:
        if not args.skip_preflight:
            compose_ops.run_command(["bash", str(preflight_script)], cwd=context.repo_root)

        build_args = ["build"]
        if not args.skip_pull:
            build_args.append("--pull")
        compose_ops.run_compose(context, build_args)
        compose_ops.run_compose(context, ["up", "-d", "--remove-orphans"])
        for service_name in healthchecked_services:
            compose_ops.wait_for_service_health(context, service_name, args.health_timeout)
        if _FORCE_RECREATE_SERVICES:
            compose_ops.run_compose(
                context,
                ["up", "-d", "--force-recreate", *_FORCE_RECREATE_SERVICES],
            )
        for service_name in _RUNNING_ONLY_SERVICES:
            compose_ops.ensure_service_running(context, service_name)
        _write_marker_best_effort(context)
        compose_ops.run_compose(context, ["ps"])
    except (RuntimeError, subprocess.CalledProcessError):
        compose_ops.show_compose_logs(context, tail_lines=args.log_tail_lines)
        raise

    _print_success_summary(context)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    context = compose_ops.ComposeContext.from_script(__file__)
    try:
        return run_update(context, args)
    except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
