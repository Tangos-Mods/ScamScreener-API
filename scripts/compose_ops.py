from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ComposeContext:
    repo_root: Path
    compose_file: Path
    env_file: Path

    @classmethod
    def from_script(cls, script_file: str | Path) -> "ComposeContext":
        script_path = Path(script_file).resolve()
        repo_root = script_path.parent.parent
        compose_file = Path(os.getenv("COMPOSE_FILE", str(repo_root / "docker-compose.yml")))
        env_file = Path(os.getenv("ENV_FILE", str(repo_root / ".env.production")))
        return cls(
            repo_root=repo_root,
            compose_file=compose_file.resolve(),
            env_file=env_file.resolve(),
        )


def require_command(command_name: str) -> None:
    if shutil.which(command_name) is None:
        raise RuntimeError(f"Missing required command: {command_name}")


def compose_base_command(context: ComposeContext) -> list[str]:
    command = [
        "docker",
        "compose",
        "-f",
        str(context.compose_file),
        "--env-file",
        str(context.env_file),
    ]
    for profile in active_compose_profiles(context):
        command.extend(["--profile", profile])
    return command


def read_env_value(env_file: Path, key: str) -> str:
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ""
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        name, separator, value = line.partition("=")
        if separator != "=" or name.strip() != key:
            continue
        normalized = value.strip()
        if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in {'"', "'", "`"}:
            normalized = normalized[1:-1]
        return normalized
    return ""


def is_true(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def marketguard_redis_enabled(context: ComposeContext) -> bool:
    enabled = is_true(read_env_value(context.env_file, "MARKETGUARD_REDIS_ENABLED") or "false")
    managed = read_env_value(context.env_file, "SCAMSCREENER_REDIS_MANAGED")
    managed_enabled = True if not managed else is_true(managed)
    return enabled and managed_enabled


def active_compose_profiles(context: ComposeContext) -> list[str]:
    profiles: list[str] = []
    if marketguard_redis_enabled(context):
        profiles.append("marketguard-redis")
    return profiles


def run_command(
    command: list[str],
    *,
    cwd: Path,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        check=True,
        text=True,
        capture_output=capture_output,
    )


def run_compose(
    context: ComposeContext,
    args: list[str],
    *,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return run_command(compose_base_command(context) + args, cwd=context.repo_root, capture_output=capture_output)


def service_container_id(context: ComposeContext, service_name: str) -> str:
    result = run_compose(context, ["ps", "-q", service_name], capture_output=True)
    container_id = (result.stdout or "").strip()
    if not container_id:
        raise RuntimeError(f"Could not resolve container for compose service: {service_name}")
    return container_id


def inspect_container(container_id: str, format_string: str, *, cwd: Path) -> str:
    result = run_command(
        ["docker", "inspect", "--format", format_string, container_id],
        cwd=cwd,
        capture_output=True,
    )
    return (result.stdout or "").strip()


def wait_for_service_health(
    context: ComposeContext,
    service_name: str,
    timeout_seconds: int,
    *,
    poll_interval_seconds: int = 3,
) -> None:
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        container_id = service_container_id(context, service_name)
        health_status = inspect_container(
            container_id,
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
            cwd=context.repo_root,
        )
        if health_status in {"healthy", "none"}:
            return
        if health_status == "unhealthy":
            raise RuntimeError(f"Compose service {service_name} became unhealthy.")
        time.sleep(poll_interval_seconds)

    raise RuntimeError(f"Timed out waiting for compose service {service_name} to become healthy.")


def ensure_service_running(context: ComposeContext, service_name: str) -> None:
    container_id = service_container_id(context, service_name)
    running_state = inspect_container(container_id, "{{.State.Running}}", cwd=context.repo_root)
    if running_state != "true":
        raise RuntimeError(f"Compose service {service_name} is not running.")


def show_compose_logs(context: ComposeContext, *, tail_lines: int) -> None:
    try:
        run_compose(context, ["logs", f"--tail={tail_lines}"])
    except subprocess.CalledProcessError:
        pass
