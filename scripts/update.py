#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import compose_ops


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

    try:
        if not args.skip_preflight:
            compose_ops.run_command(["bash", str(preflight_script)], cwd=context.repo_root)

        build_args = ["build"]
        if not args.skip_pull:
            build_args.append("--pull")
        compose_ops.run_compose(context, build_args)
        compose_ops.run_compose(context, ["up", "-d", "--remove-orphans"])
        compose_ops.wait_for_service_health(context, "scamscreener", args.health_timeout)
        compose_ops.ensure_service_running(context, "caddy")
        compose_ops.run_compose(context, ["ps"])
    except (RuntimeError, subprocess.CalledProcessError):
        compose_ops.show_compose_logs(context, tail_lines=args.log_tail_lines)
        raise

    print("Update completed successfully.")
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
