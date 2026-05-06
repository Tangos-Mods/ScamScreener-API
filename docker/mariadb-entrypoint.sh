#!/bin/sh
set -eu

SHARED_DIR="${SCAMSCREENER_SHARED_DIR:-/scamscreener-shared}"
RUNTIME_DIR="${SCAMSCREENER_DB_RUNTIME_DIR:-${SHARED_DIR}/runtime/mariadb}"
APP_PASSWORD_FILE="${SCAMSCREENER_DB_PASSWORD_FILE:-${RUNTIME_DIR}/app-password}"
ROOT_PASSWORD_FILE="${SCAMSCREENER_DB_ROOT_PASSWORD_FILE:-${RUNTIME_DIR}/root-password}"
DB_NAME="${SCAMSCREENER_DB_NAME:-${MARIADB_DATABASE:-scamscreener_hub}}"
DB_USER="${SCAMSCREENER_DB_USER:-${MARIADB_USER:-scamscreener}}"

resolve_server_binary() {
    if command -v mariadbd >/dev/null 2>&1; then
        printf '%s' "mariadbd"
        return
    fi
    if command -v mysqld >/dev/null 2>&1; then
        printf '%s' "mysqld"
        return
    fi
    echo "Missing MariaDB server binary in container: expected mariadbd or mysqld." >&2
    exit 1
}

require_command() {
    command_name="$1"
    if ! command -v "${command_name}" >/dev/null 2>&1; then
        echo "Missing required command in MariaDB container: ${command_name}" >&2
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
    echo "Missing required command in MariaDB container: expected openssl or od for secret generation." >&2
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
    require_command docker-entrypoint.sh

    mkdir -p "${RUNTIME_DIR}"
    chmod 755 "${RUNTIME_DIR}" 2>/dev/null || true
    umask 077

    export MARIADB_DATABASE="${DB_NAME}"
    export MARIADB_USER="${DB_USER}"
    export MARIADB_PASSWORD="$(ensure_secret_file "${APP_PASSWORD_FILE}" "${MARIADB_PASSWORD:-}" 644)"
    export MARIADB_ROOT_PASSWORD="$(ensure_secret_file "${ROOT_PASSWORD_FILE}" "${MARIADB_ROOT_PASSWORD:-}")"

    server_binary="$(resolve_server_binary)"

    exec docker-entrypoint.sh "${server_binary}" \
        --character-set-server=utf8mb4 \
        --collation-server=utf8mb4_unicode_ci
}

main "$@"
