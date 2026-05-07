#!/bin/sh
set -eu

APP_MODE="${SCAMSCREENER_APP_MODE:-platform}"
DATA_DIR="${TRAINING_HUB_STORAGE_DIR:-/app/data}"
RUNTIME_DIR="${SCAMSCREENER_RUNTIME_DIR:-${DATA_DIR}/runtime}"
SECRET_DIR="${RUNTIME_DIR}"
SECRET_FILE="${SCAMSCREENER_SECRET_FILE:-${SECRET_DIR}/training-hub-secret.key}"
BOOTSTRAP_ADMIN_USERNAME="${SCAMSCREENER_BOOTSTRAP_ADMIN_USERNAME:-admin}"
HOST="${SCAMSCREENER_HOST:-${TRAINING_HUB_HOST:-0.0.0.0}}"
PORT_VALUE="${PORT:-${TRAINING_HUB_PORT:-8080}}"
WORKERS="${WEB_CONCURRENCY:-1}"
EXTRA_TRUSTED_PROXIES="${SCAMSCREENER_EXTRA_TRUSTED_PROXIES:-}"
DB_MANAGED="${SCAMSCREENER_DB_MANAGED:-false}"
DB_RUNTIME_DIR="${SCAMSCREENER_DB_RUNTIME_DIR:-${RUNTIME_DIR}/mariadb}"
DB_PASSWORD_FILE="${SCAMSCREENER_DB_PASSWORD_FILE:-${DB_RUNTIME_DIR}/app-password}"
DB_HOST="${SCAMSCREENER_DB_HOST:-scamscreener-db}"
DB_PORT="${SCAMSCREENER_DB_PORT:-3306}"
DB_NAME="${SCAMSCREENER_DB_NAME:-scamscreener_hub}"
DB_USER="${SCAMSCREENER_DB_USER:-scamscreener}"
REDIS_MANAGED="${SCAMSCREENER_REDIS_MANAGED:-false}"
REDIS_RUNTIME_DIR="${SCAMSCREENER_REDIS_RUNTIME_DIR:-${RUNTIME_DIR}/redis}"
REDIS_PASSWORD_FILE="${SCAMSCREENER_REDIS_PASSWORD_FILE:-${REDIS_RUNTIME_DIR}/password}"
REDIS_HOST="${SCAMSCREENER_REDIS_HOST:-scamscreener-redis}"
REDIS_PORT="${SCAMSCREENER_REDIS_PORT:-6379}"

case "${WORKERS}" in
    ''|*[!0-9]*)
        echo "WEB_CONCURRENCY must be a positive integer." >&2
        exit 1
        ;;
esac

if [ "${WORKERS}" -lt 1 ]; then
    echo "WEB_CONCURRENCY must be at least 1." >&2
    exit 1
fi

is_true() {
    case "${1:-}" in
        1|true|TRUE|yes|YES|on|ON) return 0 ;;
        *) return 1 ;;
    esac
}

wait_for_file() {
    file_path="$1"
    timeout_seconds="${2:-60}"
    elapsed=0
    while [ ! -s "${file_path}" ]; do
        if [ "${elapsed}" -ge "${timeout_seconds}" ]; then
            echo "Required file not available after ${timeout_seconds}s: ${file_path}" >&2
            exit 1
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
}

wait_for_tcp() {
    host="$1"
    port="$2"
    timeout_seconds="${3:-60}"
    elapsed=0
    while ! python - "${host}" "${port}" <<'PY'
import socket
import sys

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(1.0)
try:
    sock.connect((sys.argv[1], int(sys.argv[2])))
except OSError:
    raise SystemExit(1)
finally:
    sock.close()
PY
    do
        if [ "${elapsed}" -ge "${timeout_seconds}" ]; then
            echo "Required TCP service not available after ${timeout_seconds}s: ${host}:${port}" >&2
            exit 1
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
}

build_trusted_proxies() {
    trusted_proxies="${TRAINING_HUB_TRUSTED_PROXIES:-127.0.0.1}"
    if [ -n "${EXTRA_TRUSTED_PROXIES}" ]; then
        trusted_proxies="${trusted_proxies},${EXTRA_TRUSTED_PROXIES}"
    fi
    printf '%s' "${trusted_proxies}"
}

configure_managed_database() {
    if ! is_true "${DB_MANAGED}"; then
        return
    fi

    export TRAINING_HUB_DB_DRIVER="${TRAINING_HUB_DB_DRIVER:-mariadb}"
    export TRAINING_HUB_DB_HOST="${TRAINING_HUB_DB_HOST:-${DB_HOST}}"
    export TRAINING_HUB_DB_PORT="${TRAINING_HUB_DB_PORT:-${DB_PORT}}"
    export TRAINING_HUB_DB_NAME="${TRAINING_HUB_DB_NAME:-${DB_NAME}}"
    export TRAINING_HUB_DB_USER="${TRAINING_HUB_DB_USER:-${DB_USER}}"
    export TRAINING_HUB_DB_REQUIRE_TLS="${TRAINING_HUB_DB_REQUIRE_TLS:-false}"
    export TRAINING_HUB_DB_SSL_CA="${TRAINING_HUB_DB_SSL_CA:-}"

    if [ -z "${TRAINING_HUB_DB_PASSWORD:-}" ]; then
        wait_for_file "${DB_PASSWORD_FILE}" 120
        export TRAINING_HUB_DB_PASSWORD="$(cat "${DB_PASSWORD_FILE}")"
    fi
}

configure_managed_marketguard_database() {
    if ! is_true "${DB_MANAGED}"; then
        return
    fi

    export MARKETGUARD_DB_DRIVER="${MARKETGUARD_DB_DRIVER:-mariadb}"
    export MARKETGUARD_DB_HOST="${MARKETGUARD_DB_HOST:-${DB_HOST}}"
    export MARKETGUARD_DB_PORT="${MARKETGUARD_DB_PORT:-${DB_PORT}}"
    export MARKETGUARD_DB_NAME="${MARKETGUARD_DB_NAME:-${DB_NAME}}"
    export MARKETGUARD_DB_USER="${MARKETGUARD_DB_USER:-${DB_USER}}"
    export MARKETGUARD_DB_REQUIRE_TLS="${MARKETGUARD_DB_REQUIRE_TLS:-false}"
    export MARKETGUARD_DB_SSL_CA="${MARKETGUARD_DB_SSL_CA:-}"

    if [ -z "${MARKETGUARD_DB_PASSWORD:-}" ]; then
        wait_for_file "${DB_PASSWORD_FILE}" 120
        export MARKETGUARD_DB_PASSWORD="$(cat "${DB_PASSWORD_FILE}")"
    fi

    wait_for_tcp "${MARKETGUARD_DB_HOST}" "${MARKETGUARD_DB_PORT}" 120
}

configure_managed_marketguard_redis() {
    if ! is_true "${MARKETGUARD_REDIS_ENABLED:-false}"; then
        return
    fi
    if ! is_true "${REDIS_MANAGED}"; then
        return
    fi

    export MARKETGUARD_REDIS_HOST="${MARKETGUARD_REDIS_HOST:-${REDIS_HOST}}"
    export MARKETGUARD_REDIS_PORT="${MARKETGUARD_REDIS_PORT:-${REDIS_PORT}}"
    export MARKETGUARD_REDIS_DB="${MARKETGUARD_REDIS_DB:-0}"
    export MARKETGUARD_REDIS_REQUIRE_TLS="${MARKETGUARD_REDIS_REQUIRE_TLS:-false}"

    if [ -z "${MARKETGUARD_REDIS_PASSWORD:-}" ]; then
        wait_for_file "${REDIS_PASSWORD_FILE}" 120
        export MARKETGUARD_REDIS_PASSWORD="$(cat "${REDIS_PASSWORD_FILE}")"
    fi

    wait_for_tcp "${MARKETGUARD_REDIS_HOST}" "${MARKETGUARD_REDIS_PORT}" 120
}

configure_training_hub_runtime() {
    if [ -z "${TRAINING_HUB_SECRET_KEY:-}" ]; then
        umask 077
        mkdir -p "${SECRET_DIR}"
        if [ -s "${SECRET_FILE}" ]; then
            TRAINING_HUB_SECRET_KEY="$(cat "${SECRET_FILE}")"
        else
            TRAINING_HUB_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(48))')"
            printf '%s' "${TRAINING_HUB_SECRET_KEY}" > "${SECRET_FILE}"
            echo "Generated persistent TRAINING_HUB_SECRET_KEY at ${SECRET_FILE}." >&2
        fi
        export TRAINING_HUB_SECRET_KEY
    fi

    if [ -z "${TRAINING_HUB_ADMIN_USERNAMES:-}" ] && [ -n "${BOOTSTRAP_ADMIN_USERNAME}" ]; then
        export TRAINING_HUB_ADMIN_USERNAMES="${BOOTSTRAP_ADMIN_USERNAME}"
    fi

    export TRAINING_HUB_TRUSTED_PROXIES="$(build_trusted_proxies)"
    configure_managed_database
}

configure_marketguard_runtime() {
    trusted_proxies="$(build_trusted_proxies)"
    export MARKETGUARD_TRUSTED_PROXIES="${MARKETGUARD_TRUSTED_PROXIES:-${trusted_proxies}}"
    configure_managed_marketguard_database
    configure_managed_marketguard_redis
}

case "${APP_MODE}" in
    hub)
        configure_training_hub_runtime
        module_target="app.training_hub.main:create_app"
        ;;
    api)
        configure_marketguard_runtime
        module_target="app.marketguard_api.main:create_app"
        ;;
    market)
        module_target="app.marketguard_hub.main:create_app"
        ;;
    platform)
        configure_training_hub_runtime
        configure_marketguard_runtime
        module_target="app.main:create_app"
        ;;
    *)
        echo "Unsupported SCAMSCREENER_APP_MODE: ${APP_MODE}" >&2
        exit 1
        ;;
esac

exec uvicorn "${module_target}" --factory --host "${HOST}" --port "${PORT_VALUE}" --workers "${WORKERS}"
