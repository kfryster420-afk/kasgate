#!/usr/bin/env bash
# Build and start the production stack (Postgres, Redis, gateway, web) on the VPS.
#
#   ./deploy-stack.sh --repo-dir /opt/kasgate
#
# Requires a filled-in infra/.env (secrets plus KASGATE_DOMAIN). Safe to re-run:
# Compose rebuilds changed images and recreates only what changed. Does not start kaspad.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

REPO_DIR=""

usage() {
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-dir) REPO_DIR="${2:-}"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage 1 ;;
  esac
done

require_repo_dir "$REPO_DIR"
ENV_FILE="${REPO_DIR}/infra/.env"
[[ -f "$ENV_FILE" ]] || die "missing ${ENV_FILE}"
DOMAIN="$(load_domain_from_env "$ENV_FILE")"

# Confirm derived URLs match the domain, but do not print secret values.
expected_base="$(gateway_public_url "$DOMAIN")"
actual_base="$(grep -E '^PUBLIC_BASE_URL=' "$ENV_FILE" | tail -n 1 || true)"
actual_base="${actual_base#PUBLIC_BASE_URL=}"
[[ "$actual_base" == "$expected_base" ]] || die "PUBLIC_BASE_URL is '${actual_base}', expected ${expected_base}. Run apply-domain.sh first."

if grep -qE '^ENCRYPTION_KEY=$' "$ENV_FILE" \
  || grep -qE '^POSTGRES_PASSWORD=$' "$ENV_FILE" \
  || grep -qE '^REDIS_PASSWORD=$' "$ENV_FILE" \
  || grep -qE '^KASGATE_TREASURY_ADDRESS=$' "$ENV_FILE"; then
  die "infra/.env still has an empty ENCRYPTION_KEY, POSTGRES_PASSWORD, REDIS_PASSWORD, or KASGATE_TREASURY_ADDRESS"
fi

INFRA="${REPO_DIR}/infra"
log "building and starting the stack for ${DOMAIN} (gateway ${expected_base})"
compose_in_infra "$INFRA" --env-file "$ENV_FILE" up -d --build --remove-orphans postgres redis gateway web
compose_in_infra "$INFRA" --env-file "$ENV_FILE" ps
log "done. Check http://127.0.0.1:8000/healthz and http://127.0.0.1:3000/api/health on the VPS."
