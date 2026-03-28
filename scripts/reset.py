#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys

import compose_ops


RESET_CONFIRMATION = "RESET SCAMSCREENER"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Destroy the production ScamScreener Docker Compose deployment state for a clean restart.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt.",
    )
    parser.add_argument(
        "--prune-images",
        action="store_true",
        help="Also remove locally built Docker images from the compose stack.",
    )
    return parser.parse_args(argv)


def _confirm_reset(skip_prompt: bool, *, input_fn=input, stderr=sys.stderr) -> bool:
    if skip_prompt:
        return True

    print(
        "This will stop the production stack and delete all persistent ScamScreener data, including "
        "accounts, uploads, bundles, generated app secrets, rate-limit state, and Caddy certificate/config volumes.",
        file=stderr,
    )
    print(f"Type {RESET_CONFIRMATION!r} to continue:", file=stderr)
    response = input_fn().strip()
    return response == RESET_CONFIRMATION


def run_reset(context: compose_ops.ComposeContext, args: argparse.Namespace) -> int:
    compose_ops.require_command("docker")

    if not context.compose_file.is_file():
        raise FileNotFoundError(f"Compose file not found: {context.compose_file}")
    if not context.env_file.is_file():
        raise FileNotFoundError(f"Environment file not found: {context.env_file}")

    if not _confirm_reset(args.yes):
        raise RuntimeError("Reset aborted by user.")

    down_args = ["down", "--volumes", "--remove-orphans"]
    if args.prune_images:
        down_args.extend(["--rmi", "local"])

    compose_ops.run_compose(context, down_args)
    compose_ops.run_compose(context, ["ps"])

    print("Reset completed. You can now upload fresh files and run python3 scripts/update.py.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    context = compose_ops.ComposeContext.from_script(__file__)
    try:
        return run_reset(context, args)
    except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
