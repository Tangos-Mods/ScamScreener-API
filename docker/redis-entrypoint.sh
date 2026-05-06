#!/bin/sh
set -eu

SHARED_DIR="${SCAMSCREENER_SHARED_DIR:-/scamscreener-shared}"
RUNTIME_DIR="${SCAMSCREENER_REDIS_RUNTIME_DIR:-${SHARED_DIR}/runtime/redis}"
PASSWORD_FILE="${SCAMSCREENER_REDIS_PASSWORD_FILE:-${RUNTIME_DIR}/password}"
MAXMEMORY="${SCAMSCREENER_REDIS_MAXMEMORY:-128mb}"
MAXMEMORY_POLICY="${SCAMSCREENER_REDIS_MAXMEMORY_POLICY:-allkeys-lru}"

require_command() {
    command_name="$1"
    if ! command -v "${command_name}" >/dev/null 2>&1; then
        echo "Missing required command in Redis container: ${command_name}" >&2
        exit 1
    fi
}

generate_secret() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
        return
    fi
    if command -v od >/dev/null 2>&1; then
        od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
        return
    fi
    echo "Missing required command in Redis container: expected openssl or od for secret generation." >&2
    exit 1
}

ensure_secret_file() {
    file_path="$1"
    env_value="${2:-}"
    mode="${3:-600}"

    if [ -n "${env_value}" ]; then
        printf '%s' "${env_value}" > "${file_path}"
        chmod "${mode}" "${file_path}"
        printf '%s' "${env_value}"
        return
    fi

    if [ -s "${file_path}" ]; then
        chmod "${mode}" "${file_path}" 2>/dev/null || true
        cat "${file_path}"
        return
    fi

    value="$(generate_secret)"
    printf '%s' "${value}" > "${file_path}"
    chmod "${mode}" "${file_path}"
    printf '%s' "${value}"
}

main() {
    require_command redis-server

    mkdir -p "${RUNTIME_DIR}"
    chmod 755 "${RUNTIME_DIR}" 2>/dev/null || true
    umask 077

    REDIS_PASSWORD="$(ensure_secret_file "${PASSWORD_FILE}" "${SCAMSCREENER_REDIS_PASSWORD:-}" 644)"
    export REDIS_PASSWORD

    exec redis-server \
        --bind 0.0.0.0 \
        --protected-mode yes \
        --appendonly no \
        --save "" \
        --maxmemory "${MAXMEMORY}" \
        --maxmemory-policy "${MAXMEMORY_POLICY}" \
        --requirepass "${REDIS_PASSWORD}"
}

main "$@"
