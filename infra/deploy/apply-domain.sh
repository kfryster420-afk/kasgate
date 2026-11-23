#!/usr/bin/env bash
# Keep URL fields in infra/.env in sync with the single KASGATE_DOMAIN variable.
#
#   ./apply-domain.sh --repo-dir /opt/kasgate [--domain kasgate.io]
#
# If --domain is given, it is written to the env file first. Derived fields:
#   PUBLIC_BASE_URL=https://api.<domain>
#   CORS_ORIGINS=["https://<domain>","https://www.<domain>"]
# Secrets are not read or printed. Safe to re-run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

REPO_DIR=""
DOMAIN=""

usage() {
  sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-dir) REPO_DIR="${2:-}"; shift 2 ;;
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage 1 ;;
  esac
done

require_repo_dir "$REPO_DIR"
ENV_FILE="${REPO_DIR}/infra/.env"
[[ -f "$ENV_FILE" ]] || die "missing ${ENV_FILE}; copy infra/deploy/env.production.example to infra/.env first"

if [[ -n "$DOMAIN" ]]; then
  is_valid_domain "$DOMAIN" || die "--domain is not a valid domain: $DOMAIN"
  set_env "$ENV_FILE" KASGATE_DOMAIN "$DOMAIN"
else
  DOMAIN="$(load_domain_from_env "$ENV_FILE")"
fi

PUBLIC_BASE_URL="$(gateway_public_url "$DOMAIN")"
CORS="$(cors_origins "$DOMAIN")"
set_env "$ENV_FILE" PUBLIC_BASE_URL "$PUBLIC_BASE_URL"
set_env "$ENV_FILE" CORS_ORIGINS "$CORS"

log "KASGATE_DOMAIN=${DOMAIN}"
log "PUBLIC_BASE_URL=${PUBLIC_BASE_URL}"
log "CORS_ORIGINS=${CORS}"
log "rebuild the web image after a domain change: ${SCRIPT_DIR}/deploy-web.sh --repo-dir ${REPO_DIR}"
