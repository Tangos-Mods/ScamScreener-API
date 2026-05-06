#!/bin/sh
set -eu

SHARED_DIR="${SCAMSCREENER_SHARED_DIR:-/scamscreener-shared}"
RUNTIME_DIR="${SCAMSCREENER_DB_RUNTIME_DIR:-${SHARED_DIR}/runtime/mariadb}"
ROOT_PASSWORD_FILE="${SCAMSCREENER_DB_ROOT_PASSWORD_FILE:-${RUNTIME_DIR}/root-password}"

if [ ! -s "${ROOT_PASSWORD_FILE}" ]; then
    echo "MariaDB root password file not ready: ${ROOT_PASSWORD_FILE}" >&2
    exit 1
fi

exec mariadb-admin --protocol=socket --user=root --password="$(cat "${ROOT_PASSWORD_FILE}")" ping
