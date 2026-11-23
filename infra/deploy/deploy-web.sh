#!/usr/bin/env bash
# Rebuild and roll the web image. NEXT_PUBLIC_GATEWAY_URL and NEXT_PUBLIC_KASPA_NETWORK
# are baked in at docker build time from PUBLIC_BASE_URL and KASPA_NETWORK, so any domain
# change must go through this script (or deploy-stack.sh) after apply-domain.sh.
#
#   ./deploy-web.sh --repo-dir /opt/kasgate
#
# The web app is a dynamic Next.js standalone server. For the public beta it runs on the
# VPS and is published at https://<KASGATE_DOMAIN> through the Cloudflare Tunnel (see
# docs/DEPLOY.md). This is not a Cloudflare Pages upload: Pages cannot host this server
# without an OpenNext adapter, which is a later migration, not a beta gate.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

REPO_DIR=""

usage() {
  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
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
expected_base="$(gateway_public_url "$DOMAIN")"
actual_base="$(grep -E '^PUBLIC_BASE_URL=' "$ENV_FILE" | tail -n 1 || true)"
actual_base="${actual_base#PUBLIC_BASE_URL=}"
[[ "$actual_base" == "$expected_base" ]] || die "PUBLIC_BASE_URL is '${actual_base}', expected ${expected_base}. Run apply-domain.sh first."

INFRA="${REPO_DIR}/infra"
log "rebuilding web for $(web_origin "$DOMAIN") with NEXT_PUBLIC_GATEWAY_URL=${expected_base}"
compose_in_infra "$INFRA" --env-file "$ENV_FILE" up -d --build --no-deps web
log "done. Confirm https://${DOMAIN}/beta shows the Testnet beta guide after the tunnel is up."
