from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


def _env_int(name: str, default: int, min_value: int, max_value: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = int(raw.strip())
    except ValueError:
        return default
    return max(min_value, min(max_value, parsed))


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


def _env_csv_set(name: str, fallback_name: str = "") -> set[str]:
    raw = os.getenv(name, "")
    if not raw.strip() and fallback_name:
        raw = os.getenv(fallback_name, "")

    values: set[str] = set()
    for part in raw.split(","):
        normalized = part.strip().lower()
        if normalized:
            values.add(normalized)
    return values


def _env_https_url(name: str, default: str) -> str:
    raw = (os.getenv(name, default) or default).strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError(f"{name} must be an absolute https URL.")
    return raw


@dataclass(frozen=True)
class MarketGuardSettings:
    hypixel_api_base_url: str
    request_timeout_seconds: int = 10
    max_parallel_pages: int = 8
    snapshot_retries: int = 3
    cache_ttl_seconds: int = 60
    stale_if_error_seconds: int = 300
    lowestbin_rate_limit_per_minute: int = 30
    http_user_agent: str = "ScamScreener-MarketGuard/1.0"
    trusted_proxies: set[str] = field(default_factory=set)
    api_docs_enabled: bool = True

    @classmethod
    def from_env(cls) -> "MarketGuardSettings":
        base_dir = Path(__file__).resolve().parents[2]
        load_dotenv(base_dir / ".env")

        settings = cls(
            hypixel_api_base_url=_env_https_url(
                "MARKETGUARD_HYPIXEL_API_BASE_URL",
                "https://api.hypixel.net/v2",
            ),
            request_timeout_seconds=_env_int("MARKETGUARD_REQUEST_TIMEOUT_SECONDS", 10, 1, 60),
            max_parallel_pages=_env_int("MARKETGUARD_MAX_PARALLEL_PAGES", 8, 1, 64),
            snapshot_retries=_env_int("MARKETGUARD_SNAPSHOT_RETRIES", 3, 1, 10),
            cache_ttl_seconds=_env_int("MARKETGUARD_CACHE_TTL_SECONDS", 60, 5, 900),
            stale_if_error_seconds=_env_int("MARKETGUARD_STALE_IF_ERROR_SECONDS", 300, 5, 3600),
            lowestbin_rate_limit_per_minute=_env_int("MARKETGUARD_LOWESTBIN_RATE_LIMIT_PER_MINUTE", 30, 0, 600),
            http_user_agent=(os.getenv("MARKETGUARD_HTTP_USER_AGENT", "ScamScreener-MarketGuard/1.0") or "").strip()
            or "ScamScreener-MarketGuard/1.0",
            trusted_proxies=_env_csv_set("MARKETGUARD_TRUSTED_PROXIES", fallback_name="TRAINING_HUB_TRUSTED_PROXIES"),
            api_docs_enabled=_env_bool("MARKETGUARD_API_DOCS_ENABLED", True),
        )
        if settings.stale_if_error_seconds < settings.cache_ttl_seconds:
            raise ValueError("MARKETGUARD_STALE_IF_ERROR_SECONDS must be greater than or equal to CACHE_TTL_SECONDS.")
        return settings
