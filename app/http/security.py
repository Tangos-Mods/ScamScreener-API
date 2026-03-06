from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import Response

from ..config.settings import TrainingHubSettings


def _is_request_from_trusted_proxy(request: Request, trusted_proxies: set[str]) -> bool:
    if "*" in trusted_proxies:
        return True

    client_host = ""
    if request.client is not None and request.client.host:
        client_host = request.client.host.strip().lower()
    if not client_host:
        return False
    return client_host in trusted_proxies


def _request_is_https(request: Request, settings: TrainingHubSettings) -> bool:
    if request.url.scheme == "https":
        return True

    if not _is_request_from_trusted_proxy(request, settings.trusted_proxies):
        return False

    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    if not forwarded_proto:
        return False
    first_proto = forwarded_proto.split(",")[0].strip().lower()
    return first_proto == "https"


def _origin_from_header(value: str) -> str:
    normalized = (value or "").strip()
    if not normalized:
        return ""
    parsed = urlsplit(normalized)
    scheme = (parsed.scheme or "").lower().strip()
    netloc = (parsed.netloc or "").lower().strip()
    if scheme not in {"http", "https"} or not netloc:
        return ""
    return f"{scheme}://{netloc}"


def _expected_origin(request: Request, settings: TrainingHubSettings) -> str:
    scheme = "https" if _request_is_https(request, settings) else "http"
    host = (request.headers.get("host", "") or "").strip().lower()
    if not host:
        host = (request.url.netloc or "").strip().lower()
    return f"{scheme}://{host}" if host else ""


def _is_same_origin_post(request: Request, settings: TrainingHubSettings) -> bool:
    if request.method.upper() != "POST":
        return True

    expected = _expected_origin(request, settings)
    if not expected:
        return False

    origin_header = str(request.headers.get("origin", "")).strip()
    if origin_header and origin_header.lower() != "null":
        return _origin_from_header(origin_header) == expected

    referer_header = str(request.headers.get("referer", "")).strip()
    if referer_header:
        return _origin_from_header(referer_header) == expected

    return False


def _apply_security_headers(response: Response, enforce_https: bool) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
    response.headers["Permissions-Policy"] = (
        "geolocation=(), microphone=(), camera=(), payment=(), usb=()"
    )
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "object-src 'none'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:"
    )
    if enforce_https:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


def _client_ip(request: Request, settings: TrainingHubSettings) -> str:
    if _is_request_from_trusted_proxy(request, settings.trusted_proxies):
        forwarded_for = request.headers.get("x-forwarded-for", "")
        if forwarded_for:
            first = forwarded_for.split(",")[0].strip()
            if first:
                return first
    if request.client is not None and request.client.host:
        return request.client.host
    return "unknown"

