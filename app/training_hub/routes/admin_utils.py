from __future__ import annotations

import os
from pathlib import Path

from fastapi import HTTPException, Request, UploadFile

from ..config.settings import TrainingHubSettings


def is_path_within(base_dir: Path, candidate: Path) -> bool:
    base_resolved = base_dir.resolve(strict=False)
    candidate_resolved = candidate.resolve(strict=False)
    try:
        return candidate_resolved.is_relative_to(base_resolved)
    except AttributeError:
        base_text = str(base_resolved)
        candidate_text = str(candidate_resolved)
        return candidate_text == base_text or candidate_text.startswith(base_text + os.sep)


def is_request_from_trusted_proxy(request: Request, trusted_proxies: set[str]) -> bool:
    if "*" in trusted_proxies:
        return True
    client_host = request.client.host.strip().lower() if request.client and request.client.host else ""
    return bool(client_host and client_host in trusted_proxies)


def request_meta(request: Request, settings: TrainingHubSettings) -> tuple[str, str]:
    source_ip = request.client.host if request.client and request.client.host else ""
    if is_request_from_trusted_proxy(request, settings.trusted_proxies):
        forwarded_for = str(request.headers.get("x-forwarded-for", "")).strip()
        if forwarded_for:
            first_ip = forwarded_for.split(",")[0].strip()
            if first_ip:
                source_ip = first_ip
    user_agent = str(request.headers.get("user-agent", ""))
    return source_ip, user_agent


async def read_upload_bytes(upload_file: UploadFile, max_bytes: int, chunk_size: int = 64 * 1024) -> bytes:
    payload = bytearray()
    while True:
        chunk = await upload_file.read(chunk_size)
        if not chunk:
            break
        payload.extend(chunk)
        if len(payload) > max_bytes:
            raise HTTPException(status_code=413, detail=f"File exceeds limit ({max_bytes} bytes).")
    return bytes(payload)

