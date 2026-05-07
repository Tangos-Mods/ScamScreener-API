from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = int(raw.strip())
    except ValueError:
        return default
    return max(min_value, min(max_value, parsed))


def _env_csv_set(name: str) -> set[str]:
    raw = os.getenv(name, "")
    values: set[str] = set()
    for part in raw.split(","):
        normalized = part.strip().lower()
        if normalized:
            values.add(normalized)
    return values


@dataclass(frozen=True)
class MarketGuardHubSettings:
    host: str = "0.0.0.0"
    port: int = 8082
    public_base_url: str = ""
    allowed_hosts: tuple[str, ...] = ("testserver",)
    enforce_https: bool = False
    base_path: str = "/market"
    refresh_interval_seconds: int = 60

    @classmethod
    def from_env(cls) -> "MarketGuardHubSettings":
        base_dir = Path(__file__).resolve().parents[2]
        load_dotenv(base_dir / ".env")

        public_base_url = (os.getenv("TRAINING_HUB_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
        allowed_hosts = _env_csv_set("TRAINING_HUB_ALLOWED_HOSTS")
        if public_base_url:
            public_host = (urlsplit(public_base_url).hostname or "").strip().lower()
            if public_host:
                allowed_hosts.add(public_host)
        if not allowed_hosts:
            allowed_hosts.add("testserver")

        return cls(
            host=(os.getenv("SCAMSCREENER_HOST", os.getenv("TRAINING_HUB_HOST", "0.0.0.0")) or "0.0.0.0").strip()
            or "0.0.0.0",
            port=_env_int("PORT", 8082, 1, 65535),
            public_base_url=public_base_url,
            allowed_hosts=tuple(sorted(allowed_hosts)),
            enforce_https=_env_bool("TRAINING_HUB_ENFORCE_HTTPS", False),
            refresh_interval_seconds=60,
        )
