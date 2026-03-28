#!/bin/sh
set -eu

DATA_DIR="${TRAINING_HUB_STORAGE_DIR:-/app/data}"
SECRET_DIR="${SCAMSCREENER_RUNTIME_DIR:-${DATA_DIR}/runtime}"
SECRET_FILE="${SCAMSCREENER_SECRET_FILE:-${SECRET_DIR}/training-hub-secret.key}"
BOOTSTRAP_ADMIN_USERNAME="${SCAMSCREENER_BOOTSTRAP_ADMIN_USERNAME:-admin}"
HOST="${TRAINING_HUB_HOST:-0.0.0.0}"
PORT_VALUE="${PORT:-${TRAINING_HUB_PORT:-8080}}"
WORKERS="${WEB_CONCURRENCY:-1}"
EXTRA_TRUSTED_PROXIES="${SCAMSCREENER_EXTRA_TRUSTED_PROXIES:-}"

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

if [ -z "${TRAINING_HUB_TRUSTED_PROXIES:-}" ]; then
    TRAINING_HUB_TRUSTED_PROXIES="127.0.0.1"
fi

if [ -n "${EXTRA_TRUSTED_PROXIES}" ]; then
    TRAINING_HUB_TRUSTED_PROXIES="${TRAINING_HUB_TRUSTED_PROXIES},${EXTRA_TRUSTED_PROXIES}"
fi
export TRAINING_HUB_TRUSTED_PROXIES

exec uvicorn app.main:create_app --factory --host "${HOST}" --port "${PORT_VALUE}" --workers "${WORKERS}"
