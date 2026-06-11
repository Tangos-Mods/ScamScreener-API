from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_LOGICAL_VOLUME_NAMES = (
    "scamscreener_data",
    "scamscreener_db_data",
    "caddy_data",
    "caddy_config",
)
_DEPLOYMENT_AUTH_MARKER_PATH = "runtime/deployment-auth-mode"
_VOLUME_BACKUP_IMAGE = "busybox:1.36.1"


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


def utc_timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")


def running_services(context: ComposeContext) -> set[str]:
    try:
        result = run_compose(context, ["ps", "--services", "--status", "running"], capture_output=True)
    except subprocess.CalledProcessError:
        return set()
    return {
        line.strip()
        for line in (result.stdout or "").splitlines()
        if line.strip()
    }


def detect_running_auth_mode(context: ComposeContext) -> str:
    try:
        container_id = service_container_id(context, "scamscreener-hub")
    except (RuntimeError, subprocess.CalledProcessError):
        return "unavailable"

    probe_script = """
import urllib.error
import urllib.request

def status(path: str) -> int:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8080{path}", timeout=3) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:
        return -1

register_status = status("/register")
forgot_status = status("/forgot-password")
print("local" if register_status != 404 or forgot_status != 404 else "external")
""".strip()
    try:
        result = run_command(
            ["docker", "exec", container_id, "python", "-c", probe_script],
            cwd=context.repo_root,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        return "unknown"
    mode = (result.stdout or "").strip().lower()
    return mode if mode in {"local", "external"} else "unknown"


def list_docker_volumes(*, cwd: Path) -> list[str]:
    result = run_command(["docker", "volume", "ls", "--format", "{{.Name}}"], cwd=cwd, capture_output=True)
    return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def resolve_named_volumes(context: ComposeContext) -> dict[str, str]:
    available = list_docker_volumes(cwd=context.repo_root)
    resolved: dict[str, str] = {}
    for logical_name in _LOGICAL_VOLUME_NAMES:
        exact_matches = [name for name in available if name == logical_name]
        suffix_matches = [name for name in available if name.endswith(f"_{logical_name}")]
        candidates = sorted(set(exact_matches or suffix_matches))
        if len(candidates) > 1:
            raise RuntimeError(
                f"Ambiguous Docker volumes for {logical_name}: {', '.join(candidates)}. "
                "Set COMPOSE_PROJECT_NAME consistently or remove stale duplicate volumes."
            )
        if candidates:
            resolved[logical_name] = candidates[0]
    return resolved


def deployment_state_exists(context: ComposeContext) -> bool:
    if running_services(context):
        return True
    return bool(resolve_named_volumes(context))


def backup_repo_tree(context: ComposeContext, destination_dir: Path) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True)
    repo_backup_dir = destination_dir / "repo"
    if repo_backup_dir.exists():
        raise RuntimeError(f"Backup target already exists: {repo_backup_dir}")
    shutil.copytree(context.repo_root, repo_backup_dir, dirs_exist_ok=False)
    return repo_backup_dir


def backup_named_volume(volume_name: str, destination_dir: Path, archive_name: str, *, cwd: Path) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True)
    archive_path = destination_dir / archive_name
    safe_archive_name = re.sub(r"[^A-Za-z0-9._-]+", "-", archive_name).strip("-") or "volume-backup.tar.gz"
    run_command(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{volume_name}:/from:ro",
            "-v",
            f"{destination_dir}:/to",
            _VOLUME_BACKUP_IMAGE,
            "sh",
            "-c",
            f"cd /from && tar czf /to/{safe_archive_name} .",
        ],
        cwd=cwd,
    )
    return archive_path


def read_volume_text_file(volume_name: str, relative_path: str, *, cwd: Path) -> str:
    normalized_path = str(relative_path or "").strip().lstrip("/")
    if not normalized_path:
        return ""
    command = (
        f"if [ -f /volume/{normalized_path} ]; then cat /volume/{normalized_path}; fi"
    )
    result = run_command(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{volume_name}:/volume:ro",
            _VOLUME_BACKUP_IMAGE,
            "sh",
            "-c",
            command,
        ],
        cwd=cwd,
        capture_output=True,
    )
    return (result.stdout or "").strip()


def write_volume_text_file(volume_name: str, relative_path: str, contents: str, *, cwd: Path) -> None:
    normalized_path = str(relative_path or "").strip().lstrip("/")
    if not normalized_path:
        raise ValueError("relative_path must not be empty.")
    parent_dir = str(Path(normalized_path).parent).strip(".")
    safe_contents = (contents or "").replace("\\", "\\\\").replace('"', '\\"')
    mkdir_segment = f"mkdir -p /volume/{parent_dir} && " if parent_dir else ""
    run_command(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{volume_name}:/volume",
            _VOLUME_BACKUP_IMAGE,
            "sh",
            "-c",
            f'{mkdir_segment}printf "%s\\n" "{safe_contents}" > /volume/{normalized_path}',
        ],
        cwd=cwd,
    )


def read_deployment_auth_marker(context: ComposeContext) -> str:
    volume_name = resolve_named_volumes(context).get("scamscreener_data", "")
    if not volume_name:
        return ""
    try:
        return read_volume_text_file(volume_name, _DEPLOYMENT_AUTH_MARKER_PATH, cwd=context.repo_root)
    except subprocess.CalledProcessError:
        return ""


def write_deployment_auth_marker(context: ComposeContext, mode: str = "external") -> None:
    volume_name = resolve_named_volumes(context).get("scamscreener_data", "")
    if not volume_name:
        raise RuntimeError("Could not resolve the scamscreener_data volume to persist the deployment marker.")
    write_volume_text_file(volume_name, _DEPLOYMENT_AUTH_MARKER_PATH, mode.strip().lower(), cwd=context.repo_root)
