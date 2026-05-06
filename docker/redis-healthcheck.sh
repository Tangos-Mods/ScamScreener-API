#!/bin/sh
set -eu

SHARED_DIR="${SCAMSCREENER_SHARED_DIR:-/scamscreener-shared}"
RUNTIME_DIR="${SCAMSCREENER_REDIS_RUNTIME_DIR:-${SHARED_DIR}/runtime/redis}"
PASSWORD_FILE="${SCAMSCREENER_REDIS_PASSWORD_FILE:-${RUNTIME_DIR}/password}"

if [ ! -s "${PASSWORD_FILE}" ]; then
    echo "Redis password file not ready: ${PASSWORD_FILE}" >&2
    exit 1
fi

exec redis-cli -a "$(cat "${PASSWORD_FILE}")" ping
