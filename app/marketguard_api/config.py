from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlsplit

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


def _env_text(name: str, default: str = "") -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip()


def _build_mariadb_url(
    *,
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
    require_tls: bool,
    ssl_ca: str,
) -> str:
    if not host:
        host = "127.0.0.1"
    if not database:
        raise ValueError("MARKETGUARD_DB_NAME must not be empty.")
    query: list[str] = []
    if require_tls:
        query.append("ssl_mode=verify-full")
        if ssl_ca:
            query.append(f"ssl_ca={quote(ssl_ca, safe='/')}")
    query_suffix = f"?{'&'.join(query)}" if query else ""
    return (
        f"mariadb://{quote(user, safe='')}:{quote(password, safe='')}@"
        f"{host}:{int(port)}/{quote(database, safe='')}{query_suffix}"
    )


def _build_redis_url(
    *,
    host: str,
    port: int,
    database: int,
    password: str,
    require_tls: bool,
) -> str:
    scheme = "rediss" if require_tls else "redis"
    auth = f":{quote(password, safe='')}@" if password else ""
    return f"{scheme}://{auth}{host}:{int(port)}/{int(database)}"


@dataclass(frozen=True)
class MarketGuardSettings:
    hypixel_api_base_url: str
    database_url: str
    hypixel_api_key: str = ""
    request_timeout_seconds: int = 10
    max_parallel_pages: int = 8
    snapshot_retries: int = 3
    cache_ttl_seconds: int = 60
    stale_if_error_seconds: int = 300
    history_retention_days: int = 45
    lowestbin_rate_limit_per_minute: int = 30
    players_rate_limit_per_minute: int = 3
    players_max_upstream_concurrency: int = 4
    http_user_agent: str = "ScamScreener-MarketGuard/1.0"
    trusted_proxies: set[str] = field(default_factory=set)
    local_cache_enabled: bool = True
    local_cache_ttl_seconds: int = 15
    local_cache_max_entries: int = 32
    redis_enabled: bool = False
    redis_url: str = ""
    redis_cache_ttl_seconds: int = 60
    redis_key_prefix: str = "marketguard:response"
    api_docs_enabled: bool = True

    @classmethod
    def from_env(cls) -> "MarketGuardSettings":
        base_dir = Path(__file__).resolve().parents[2]
        load_dotenv(base_dir / ".env")
        database_url = _env_text("MARKETGUARD_DATABASE_URL")
        database_driver = (_env_text("MARKETGUARD_DB_DRIVER", "mariadb") or "mariadb").lower()
        if database_driver != "mariadb":
            raise ValueError("MARKETGUARD_DB_DRIVER must be mariadb.")
        if not database_url:
            database_url = _build_mariadb_url(
                host=_env_text("MARKETGUARD_DB_HOST", "127.0.0.1") or "127.0.0.1",
                port=_env_int("MARKETGUARD_DB_PORT", 3306, 1, 65535),
                database=_env_text("MARKETGUARD_DB_NAME", "scamscreener_hub"),
                user=_env_text("MARKETGUARD_DB_USER", "scamscreener"),
                password=_env_text("MARKETGUARD_DB_PASSWORD"),
                require_tls=_env_bool("MARKETGUARD_DB_REQUIRE_TLS", False),
                ssl_ca=_env_text("MARKETGUARD_DB_SSL_CA"),
            )
        if urlsplit(database_url).scheme.lower() not in {"mariadb", "mysql"}:
            raise ValueError("MARKETGUARD_DATABASE_URL must use the mariadb:// or mysql:// scheme.")

        redis_enabled = _env_bool("MARKETGUARD_REDIS_ENABLED", False)
        redis_url = _env_text("MARKETGUARD_REDIS_URL")
        if redis_enabled and not redis_url:
            redis_url = _build_redis_url(
                host=_env_text("MARKETGUARD_REDIS_HOST", "127.0.0.1") or "127.0.0.1",
                port=_env_int("MARKETGUARD_REDIS_PORT", 6379, 1, 65535),
                database=_env_int("MARKETGUARD_REDIS_DB", 0, 0, 15),
                password=_env_text("MARKETGUARD_REDIS_PASSWORD"),
                require_tls=_env_bool("MARKETGUARD_REDIS_REQUIRE_TLS", False),
            )

        settings = cls(
            hypixel_api_base_url=_env_https_url(
                "MARKETGUARD_HYPIXEL_API_BASE_URL",
                "https://api.hypixel.net/v2",
            ),
            database_url=database_url,
            hypixel_api_key=_env_text("MARKETGUARD_HYPIXEL_API_KEY"),
            request_timeout_seconds=_env_int("MARKETGUARD_REQUEST_TIMEOUT_SECONDS", 10, 1, 60),
            max_parallel_pages=_env_int("MARKETGUARD_MAX_PARALLEL_PAGES", 8, 1, 64),
            snapshot_retries=_env_int("MARKETGUARD_SNAPSHOT_RETRIES", 3, 1, 10),
            cache_ttl_seconds=_env_int("MARKETGUARD_CACHE_TTL_SECONDS", 60, 5, 900),
            stale_if_error_seconds=_env_int("MARKETGUARD_STALE_IF_ERROR_SECONDS", 300, 5, 3600),
            history_retention_days=_env_int("MARKETGUARD_HISTORY_RETENTION_DAYS", 45, 31, 365),
            lowestbin_rate_limit_per_minute=_env_int("MARKETGUARD_LOWESTBIN_RATE_LIMIT_PER_MINUTE", 30, 0, 600),
            players_rate_limit_per_minute=_env_int("MARKETGUARD_PLAYERS_RATE_LIMIT_PER_MINUTE", 3, 0, 60),
            players_max_upstream_concurrency=_env_int("MARKETGUARD_PLAYERS_MAX_UPSTREAM_CONCURRENCY", 4, 1, 10),
            http_user_agent=(os.getenv("MARKETGUARD_HTTP_USER_AGENT", "ScamScreener-MarketGuard/1.0") or "").strip()
            or "ScamScreener-MarketGuard/1.0",
            trusted_proxies=_env_csv_set("MARKETGUARD_TRUSTED_PROXIES", fallback_name="TRAINING_HUB_TRUSTED_PROXIES"),
            local_cache_enabled=_env_bool("MARKETGUARD_LOCAL_CACHE_ENABLED", True),
            local_cache_ttl_seconds=_env_int("MARKETGUARD_LOCAL_CACHE_TTL_SECONDS", 15, 1, 900),
            local_cache_max_entries=_env_int("MARKETGUARD_LOCAL_CACHE_MAX_ENTRIES", 32, 1, 1024),
            redis_enabled=redis_enabled,
            redis_url=redis_url,
            redis_cache_ttl_seconds=_env_int("MARKETGUARD_REDIS_CACHE_TTL_SECONDS", 60, 1, 900),
            redis_key_prefix=_env_text("MARKETGUARD_REDIS_KEY_PREFIX", "marketguard:response") or "marketguard:response",
            api_docs_enabled=_env_bool("MARKETGUARD_API_DOCS_ENABLED", True),
        )
        if settings.stale_if_error_seconds < settings.cache_ttl_seconds:
            raise ValueError("MARKETGUARD_STALE_IF_ERROR_SECONDS must be greater than or equal to CACHE_TTL_SECONDS.")
        if settings.redis_enabled and not settings.redis_url:
            raise ValueError("MARKETGUARD_REDIS_ENABLED requires a usable Redis URL or host/port configuration.")
        return settings
