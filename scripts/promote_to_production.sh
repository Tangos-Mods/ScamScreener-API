#!/usr/bin/env bash
set -euo pipefail

umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env"
SSL_DIR="${REPO_ROOT}/ops/mariadb/ssl"
CONF_DIR="${REPO_ROOT}/ops/mariadb/conf.d"
BACKUP_FILE="${REPO_ROOT}/.env.pre-production.$(date -u +%Y%m%dT%H%M%SZ).bak"
DB_CONTAINER="scamscreener-training-hub-db"

require_command() {
    local command_name="$1"
    if ! command -v "${command_name}" >/dev/null 2>&1; then
        echo "Missing required command: ${command_name}" >&2
        exit 1
    fi
}

get_env_value() {
    local key="$1"
    awk -F= -v key="${key}" '
        $1 == key {
            print substr($0, index($0, "=") + 1)
            found = 1
            exit
        }
        END {
            if (!found) {
                exit 1
            }
        }
    ' "${ENV_FILE}" | tr -d '\r'
}

set_env_value() {
    local key="$1"
    local value="$2"
    local tmp_file
    tmp_file="$(mktemp)"
    awk -v key="${key}" -v value="${value}" '
        BEGIN {
            replaced = 0
        }
        index($0, key "=") == 1 {
            if (!replaced) {
                print key "=" value
                replaced = 1
            }
            next
        }
        {
            print
        }
        END {
            if (!replaced) {
                print key "=" value
            }
        }
    ' "${ENV_FILE}" > "${tmp_file}"
    mv "${tmp_file}" "${ENV_FILE}"
}

require_non_empty_env() {
    local key="$1"
    local value
    value="$(get_env_value "${key}" 2>/dev/null || true)"
    if [[ -z "${value}" ]]; then
        echo "Required .env value is missing: ${key}" >&2
        exit 1
    fi
}

wait_for_mariadb() {
    local attempts=60
    local status=""
    for ((i=1; i<=attempts; i++)); do
        status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "${DB_CONTAINER}" 2>/dev/null || true)"
        if [[ "${status}" == "healthy" ]]; then
            return 0
        fi
        sleep 2
    done
    echo "MariaDB container did not become healthy. Last status: ${status}" >&2
    exit 1
}

generate_tls_material() {
    mkdir -p "${SSL_DIR}" "${CONF_DIR}"

    if [[ ! -f "${SSL_DIR}/ca.pem" || ! -f "${SSL_DIR}/ca-key.pem" ]]; then
        openssl genrsa -out "${SSL_DIR}/ca-key.pem" 4096
        openssl req -x509 -new -nodes \
            -key "${SSL_DIR}/ca-key.pem" \
            -sha256 \
            -days 3650 \
            -out "${SSL_DIR}/ca.pem" \
            -subj "/CN=ScamScreener Internal MariaDB CA"
    fi

    if [[ ! -f "${SSL_DIR}/server-cert.pem" || ! -f "${SSL_DIR}/server-key.pem" ]]; then
        local openssl_config
        openssl_config="$(mktemp)"
        cat > "${openssl_config}" <<'EOF'
[req]
default_bits = 4096
prompt = no
default_md = sha256
distinguished_name = dn
req_extensions = req_ext

[dn]
CN = mariadb

[req_ext]
subjectAltName = @alt_names
extendedKeyUsage = serverAuth

[alt_names]
DNS.1 = mariadb
DNS.2 = localhost
IP.1 = 127.0.0.1
EOF
        openssl genrsa -out "${SSL_DIR}/server-key.pem" 4096
        openssl req -new \
            -key "${SSL_DIR}/server-key.pem" \
            -out "${SSL_DIR}/server.csr" \
            -config "${openssl_config}"
        openssl x509 -req \
            -in "${SSL_DIR}/server.csr" \
            -CA "${SSL_DIR}/ca.pem" \
            -CAkey "${SSL_DIR}/ca-key.pem" \
            -CAcreateserial \
            -out "${SSL_DIR}/server-cert.pem" \
            -days 825 \
            -sha256 \
            -extensions req_ext \
            -extfile "${openssl_config}"
        rm -f "${SSL_DIR}/server.csr" "${openssl_config}"
    fi

    cat > "${CONF_DIR}/ssl.cnf" <<'EOF'
[mariadb]
ssl-ca=/etc/mysql/ssl/ca.pem
ssl-cert=/etc/mysql/ssl/server-cert.pem
ssl-key=/etc/mysql/ssl/server-key.pem
require_secure_transport=ON
EOF

    chmod 0644 "${SSL_DIR}/ca.pem" "${SSL_DIR}/server-cert.pem" "${SSL_DIR}/server-key.pem" "${CONF_DIR}/ssl.cnf"
    chmod 0600 "${SSL_DIR}/ca-key.pem"
}

normalize_domain() {
    local raw_domain="$1"
    raw_domain="${raw_domain#http://}"
    raw_domain="${raw_domain#https://}"
    raw_domain="${raw_domain%%/*}"
    printf '%s' "${raw_domain}"
}

sql_escape_string() {
    local raw_value="$1"
    printf '%s' "${raw_value}" | sed "s/'/''/g"
}

main() {
    require_command docker
    require_command openssl
    require_command awk
    require_command mktemp
    require_command sed

    if [[ ! -f "${ENV_FILE}" ]]; then
        echo "Missing .env file at ${ENV_FILE}" >&2
        exit 1
    fi

    cp "${ENV_FILE}" "${BACKUP_FILE}"

    require_non_empty_env "CADDY_SITE_ADDRESS"
    require_non_empty_env "TRAINING_HUB_SECRET_KEY"
    require_non_empty_env "TRAINING_HUB_DB_PASSWORD"
    require_non_empty_env "TRAINING_HUB_DB_ROOT_PASSWORD"
    require_non_empty_env "TRAINING_HUB_SMTP_HOST"
    require_non_empty_env "TRAINING_HUB_SMTP_FROM_EMAIL"

    local domain
    domain="$(normalize_domain "$(get_env_value "CADDY_SITE_ADDRESS")")"
    if [[ -z "${domain}" || "${domain}" == "localhost" || "${domain}" == "127.0.0.1" ]]; then
        echo "CADDY_SITE_ADDRESS must be set to a real public domain before promoting to production." >&2
        exit 1
    fi
    if [[ "${domain}" == *,* ]]; then
        echo "CADDY_SITE_ADDRESS must contain exactly one domain." >&2
        exit 1
    fi

    local secret_key
    secret_key="$(get_env_value "TRAINING_HUB_SECRET_KEY")"
    if [[ "${#secret_key}" -lt 32 ]]; then
        echo "TRAINING_HUB_SECRET_KEY must be at least 32 characters long." >&2
        exit 1
    fi

    local smtp_use_tls
    local smtp_use_starttls
    smtp_use_tls="$(get_env_value "TRAINING_HUB_SMTP_USE_TLS" 2>/dev/null || true)"
    smtp_use_starttls="$(get_env_value "TRAINING_HUB_SMTP_USE_STARTTLS" 2>/dev/null || true)"
    if [[ -z "${smtp_use_tls}" && -z "${smtp_use_starttls}" ]]; then
        set_env_value "TRAINING_HUB_SMTP_USE_STARTTLS" "true"
    elif [[ "${smtp_use_tls}" == "true" && "${smtp_use_starttls}" == "true" ]]; then
        echo "Only one of TRAINING_HUB_SMTP_USE_TLS or TRAINING_HUB_SMTP_USE_STARTTLS may be true." >&2
        exit 1
    fi

    generate_tls_material

    set_env_value "CADDY_SITE_ADDRESS" "${domain}"
    set_env_value "TRAINING_HUB_ALLOWED_HOSTS" "${domain}"
    set_env_value "TRAINING_HUB_ENV" "production"
    set_env_value "TRAINING_HUB_ENFORCE_HTTPS" "true"
    set_env_value "TRAINING_HUB_DB_REQUIRE_TLS" "true"
    set_env_value "TRAINING_HUB_DB_SSL_CA" "/etc/mysql/ssl/ca.pem"
    set_env_value "TRAINING_HUB_DB_SSL_VERIFY_HOSTNAME" "true"
    set_env_value "TRAINING_HUB_PASSWORD_RESET_SHOW_TOKEN" "false"
    set_env_value "TRAINING_HUB_ENABLE_RATE_LIMIT" "true"
    set_env_value "TRAINING_HUB_ENFORCE_ORIGIN_CHECK" "true"
    set_env_value "TRAINING_HUB_ADMIN_MFA_REQUIRED" "true"
    set_env_value "TRAINING_HUB_PASSWORD_RESET_SEND_EMAIL" "true"
    set_env_value "TRAINING_HUB_SESSION_BIND_USER_AGENT" "true"
    set_env_value "TRAINING_HUB_RETENTION_AUTO_ENABLED" "true"

    (
        cd "${REPO_ROOT}"
        docker compose stop mailpit >/dev/null 2>&1 || true
        docker compose up -d mariadb
        wait_for_mariadb

        local db_user
        local db_name
        local db_password
        local db_root_password
        local db_user_sql
        local db_name_sql
        local db_password_sql
        db_user="$(get_env_value "TRAINING_HUB_DB_USER" 2>/dev/null || printf '%s' 'scamscreener')"
        db_name="$(get_env_value "TRAINING_HUB_DB_NAME" 2>/dev/null || printf '%s' 'scamscreener_hub')"
        db_password="$(get_env_value "TRAINING_HUB_DB_PASSWORD")"
        db_root_password="$(get_env_value "TRAINING_HUB_DB_ROOT_PASSWORD")"
        db_user_sql="$(sql_escape_string "${db_user}")"
        db_name_sql="$(printf '%s' "${db_name}" | sed 's/`/``/g')"
        db_password_sql="$(sql_escape_string "${db_password}")"

        docker compose exec -T mariadb mariadb -uroot "-p${db_root_password}" <<SQL
CREATE USER IF NOT EXISTS '${db_user_sql}'@'%' IDENTIFIED BY '${db_password_sql}';
ALTER USER '${db_user_sql}'@'%' IDENTIFIED BY '${db_password_sql}';
GRANT ALL PRIVILEGES ON \`${db_name_sql}\`.* TO '${db_user_sql}'@'%';
FLUSH PRIVILEGES;
SQL

        docker compose up -d --build mariadb training-hub caddy
    )

    cat <<EOF
Production promotion completed.

Backup of previous .env:
  ${BACKUP_FILE}

Generated MariaDB TLS assets:
  ${SSL_DIR}
  ${CONF_DIR}/ssl.cnf

The stack is now configured for:
  - public Caddy HTTPS
  - production app mode
  - verified TLS between app and MariaDB
  - public blocking of /api/v1/health and /api/v1/metrics
EOF
}

main "$@"
