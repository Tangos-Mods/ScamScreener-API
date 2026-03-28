#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-${REPO_ROOT}/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env.production}"

require_command() {
    local command_name="$1"
    if ! command -v "${command_name}" >/dev/null 2>&1; then
        echo "Missing required command: ${command_name}" >&2
        exit 1
    fi
}

require_file() {
    local file_path="$1"
    if [[ ! -f "${file_path}" ]]; then
        echo "Required file not found: ${file_path}" >&2
        exit 1
    fi
}

read_env_value() {
    local key="$1"
    awk -v key="${key}" '
        function trim(value) {
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
            return value
        }

        /^[[:space:]]*#/ { next }
        /^[[:space:]]*$/ { next }

        {
            line = $0
            sub(/\r$/, "", line)
            line = trim(line)
            sub(/^export[[:space:]]+/, "", line)

            separator = index(line, "=")
            if (separator == 0) {
                next
            }

            current_key = trim(substr(line, 1, separator - 1))
            if (current_key != key) {
                next
            }

            value = trim(substr(line, separator + 1))

            if (length(value) >= 2) {
                single_quote = sprintf("%c", 39)
                first = substr(value, 1, 1)
                last = substr(value, length(value), 1)
                if ((first == "\"" && last == "\"") || (first == single_quote && last == single_quote) || (first == "`" && last == "`")) {
                    value = substr(value, 2, length(value) - 2)
                }
            }

            print value
            found = 1
            exit
        }

        END {
            if (!found) {
                exit 1
            }
        }
    ' "${ENV_FILE}"
}

require_non_empty_env() {
    local key="$1"
    local value
    value="$(read_env_value "${key}" 2>/dev/null || true)"
    if [[ -z "${value}" ]]; then
        echo "Required environment value missing in ${ENV_FILE}: ${key}" >&2
        exit 1
    fi
}

normalize_domain() {
    local raw_domain="$1"
    raw_domain="${raw_domain#http://}"
    raw_domain="${raw_domain#https://}"
    raw_domain="${raw_domain%%/*}"
    printf '%s' "${raw_domain}"
}

main() {
    require_command docker
    require_command awk
    require_file "${COMPOSE_FILE}"
    require_file "${ENV_FILE}"

    require_non_empty_env "CADDY_SITE_ADDRESS"
    require_non_empty_env "TRAINING_HUB_PUBLIC_BASE_URL"
    require_non_empty_env "TRAINING_HUB_ENV"
    require_non_empty_env "TRAINING_HUB_ENFORCE_HTTPS"
    require_non_empty_env "TRAINING_HUB_ADMIN_MFA_REQUIRED"
    require_non_empty_env "TRAINING_HUB_PASSWORD_RESET_SEND_EMAIL"
    require_non_empty_env "TRAINING_HUB_SMTP_HOST"
    require_non_empty_env "TRAINING_HUB_SMTP_PORT"
    require_non_empty_env "TRAINING_HUB_SMTP_FROM_EMAIL"

    local caddy_site_address
    local public_base_url
    local env_name
    local enforce_https
    local admin_mfa_required
    local password_reset_send_email
    local smtp_use_tls
    local smtp_use_starttls
    local caddy_host
    local public_host
    local site_operator_name
    local site_postal_address
    local site_contact_channel

    caddy_site_address="$(read_env_value "CADDY_SITE_ADDRESS")"
    public_base_url="$(read_env_value "TRAINING_HUB_PUBLIC_BASE_URL")"
    env_name="$(read_env_value "TRAINING_HUB_ENV")"
    enforce_https="$(read_env_value "TRAINING_HUB_ENFORCE_HTTPS")"
    admin_mfa_required="$(read_env_value "TRAINING_HUB_ADMIN_MFA_REQUIRED")"
    password_reset_send_email="$(read_env_value "TRAINING_HUB_PASSWORD_RESET_SEND_EMAIL")"
    smtp_use_tls="$(read_env_value "TRAINING_HUB_SMTP_USE_TLS" 2>/dev/null || true)"
    smtp_use_starttls="$(read_env_value "TRAINING_HUB_SMTP_USE_STARTTLS" 2>/dev/null || true)"
    site_operator_name="$(read_env_value "TRAINING_HUB_SITE_OPERATOR_NAME" 2>/dev/null || true)"
    site_postal_address="$(read_env_value "TRAINING_HUB_SITE_POSTAL_ADDRESS" 2>/dev/null || true)"
    site_contact_channel="$(read_env_value "TRAINING_HUB_SITE_CONTACT_CHANNEL" 2>/dev/null || true)"

    caddy_host="$(normalize_domain "${caddy_site_address}")"
    public_host="$(normalize_domain "${public_base_url}")"

    if [[ -z "${caddy_host}" || "${caddy_host}" == "localhost" || "${caddy_host}" == "127.0.0.1" ]]; then
        echo "CADDY_SITE_ADDRESS must be set to the real public domain." >&2
        exit 1
    fi

    if [[ "${public_base_url}" != https://* ]]; then
        echo "TRAINING_HUB_PUBLIC_BASE_URL must start with https:// in production." >&2
        exit 1
    fi

    if [[ "${caddy_host}" != "${public_host}" ]]; then
        echo "CADDY_SITE_ADDRESS and TRAINING_HUB_PUBLIC_BASE_URL must point to the same host." >&2
        exit 1
    fi

    if [[ "${env_name}" != "production" ]]; then
        echo "TRAINING_HUB_ENV must be set to production." >&2
        exit 1
    fi

    if [[ "${enforce_https}" != "true" ]]; then
        echo "TRAINING_HUB_ENFORCE_HTTPS must be true." >&2
        exit 1
    fi

    if [[ "${admin_mfa_required}" != "true" ]]; then
        echo "TRAINING_HUB_ADMIN_MFA_REQUIRED must be true." >&2
        exit 1
    fi

    if [[ "${password_reset_send_email}" != "true" ]]; then
        echo "TRAINING_HUB_PASSWORD_RESET_SEND_EMAIL must be true." >&2
        exit 1
    fi

    if [[ "${smtp_use_tls}" == "true" && "${smtp_use_starttls}" == "true" ]]; then
        echo "Only one of TRAINING_HUB_SMTP_USE_TLS and TRAINING_HUB_SMTP_USE_STARTTLS may be true." >&2
        exit 1
    fi

    if [[ "${smtp_use_tls}" != "true" && "${smtp_use_starttls}" != "true" ]]; then
        echo "Enable either TRAINING_HUB_SMTP_USE_TLS or TRAINING_HUB_SMTP_USE_STARTTLS." >&2
        exit 1
    fi

    if [[ "$(stat -c '%a' "${ENV_FILE}" 2>/dev/null || true)" != "600" ]]; then
        echo "Warning: ${ENV_FILE} should ideally have mode 600." >&2
    fi

    if [[ -z "${site_operator_name}" ]]; then
        echo "Warning: TRAINING_HUB_SITE_OPERATOR_NAME is empty; /impressum will not identify an operator." >&2
    fi

    if [[ -z "${site_postal_address}" ]]; then
        echo "Warning: TRAINING_HUB_SITE_POSTAL_ADDRESS is empty; this is likely insufficient for a German/EU public impressum." >&2
    elif [[ "${site_postal_address}" == @* ]]; then
        echo "Warning: TRAINING_HUB_SITE_POSTAL_ADDRESS currently looks like a handle, not a serviceable postal address." >&2
    fi

    if [[ -z "${site_contact_channel}" ]]; then
        echo "Warning: TRAINING_HUB_SITE_CONTACT_CHANNEL is empty; users will not see a public contact path." >&2
    fi

    (
        cd "${REPO_ROOT}"
        docker compose -f "${COMPOSE_FILE}" --env-file "${ENV_FILE}" config >/dev/null
    )

    echo "Preflight checks passed."
}

main "$@"
