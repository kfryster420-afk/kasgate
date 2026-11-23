#!/usr/bin/env bash
# Render the Cloudflare Tunnel origin config from the template and KASGATE_DOMAIN.
#
#   ./render-tunnel-config.sh --repo-dir /opt/kasgate \
#     --tunnel-id <UUID> --credentials-file /etc/cloudflared/<UUID>.json \
#     [--output /etc/cloudflared/config.yml]
#
# Safe to re-run: the output file is overwritten. Credentials are never read.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

REPO_DIR=""
TUNNEL_ID=""
CREDENTIALS_FILE=""
OUTPUT=""

usage() {
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-dir) REPO_DIR="${2:-}"; shift 2 ;;
    --tunnel-id) TUNNEL_ID="${2:-}"; shift 2 ;;
    --credentials-file) CREDENTIALS_FILE="${2:-}"; shift 2 ;;
    --output) OUTPUT="${2:-}"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage 1 ;;
  esac
done

require_repo_dir "$REPO_DIR"
[[ -n "$TUNNEL_ID" ]] || die "--tunnel-id is required"
[[ -n "$CREDENTIALS_FILE" ]] || die "--credentials-file is required"
[[ "$TUNNEL_ID" =~ ^[A-Za-z0-9-]+$ ]] || die "--tunnel-id has unexpected characters"
[[ "$CREDENTIALS_FILE" == /* ]] || die "--credentials-file must be an absolute path"
[[ "$CREDENTIALS_FILE" != *$'\n'* ]] || die "--credentials-file must be a single line"

ENV_FILE="${REPO_DIR}/infra/.env"
DOMAIN="$(load_domain_from_env "$ENV_FILE")"
TEMPLATE="${SCRIPT_DIR}/cloudflared/config.yml.example"
[[ -f "$TEMPLATE" ]] || die "missing template ${TEMPLATE}"

if [[ -z "$OUTPUT" ]]; then
  OUTPUT="${SCRIPT_DIR}/cloudflared/config.yml"
fi

tmp="$(mktemp)"
sed \
  -e "s|__TUNNEL_ID__|${TUNNEL_ID}|g" \
  -e "s|__CREDENTIALS_FILE__|${CREDENTIALS_FILE}|g" \
  -e "s|__KASGATE_DOMAIN__|${DOMAIN}|g" \
  "$TEMPLATE" > "$tmp"

if grep -q '__[A-Z0-9_]*__' "$tmp"; then
  rm -f "$tmp"
  die "unreplaced placeholder left in the rendered tunnel config"
fi

umask 077
mkdir -p "$(dirname "$OUTPUT")"
cat "$tmp" > "$OUTPUT"
rm -f "$tmp"
chmod 600 "$OUTPUT"
log "wrote ${OUTPUT} for domain ${DOMAIN} (api/www/apex -> loopback origins)"
