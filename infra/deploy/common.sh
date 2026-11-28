# shellcheck shell=bash
# Shared helpers for KasGate deploy scripts. Source this file; do not execute it.
# Usage from a sibling script:
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   # shellcheck source=common.sh
#   source "${SCRIPT_DIR}/common.sh"

log() { printf '[kasgate-deploy] %s\n' "$*"; }
die() { printf '[kasgate-deploy] ERROR: %s\n' "$*" >&2; exit 1; }

is_valid_domain() {
  [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$ ]]
}

gateway_public_url() { printf 'https://api.%s' "$1"; }
web_origin() { printf 'https://%s' "$1"; }
www_origin() { printf 'https://www.%s' "$1"; }
cors_origins() { printf '["https://%s","https://www.%s"]' "$1" "$1"; }

# set_env FILE KEY VALUE — replace or append KEY=VALUE without sourcing the file.
# VALUE must be a single line. The file mode is preserved.
set_env() {
  local file="$1" key="$2" value="$3" tmp mode=""
  [[ -n "$file" && -n "$key" ]] || die "set_env requires file and key"
  [[ "$key" =~ ^[A-Z_][A-Z0-9_]*$ ]] || die "invalid env key: $key"
  if [[ "$value" == *$'\n'* ]]; then
    die "refusing to write a multi-line value for $key"
  fi
  mkdir -p "$(dirname "$file")"
  if [[ -f "$file" ]]; then
    mode="$(stat -c '%a' "$file")"
  else
    touch "$file"
    chmod 600 "$file"
    mode="600"
  fi
  tmp="$(mktemp)"
  awk -v key="$key" -v value="$value" '
    BEGIN { pat = "^" key "=" }
    $0 ~ pat { print key "=" value; found=1; next }
    { print }
    END { if (!found) print key "=" value }
  ' "$file" > "$tmp"
  cat "$tmp" > "$file"
  rm -f "$tmp"
  chmod "$mode" "$file"
}

# Prints KASGATE_DOMAIN from an env file. Does not source the file (it may hold secrets).
load_domain_from_env() {
  local file="$1" line domain=""
  [[ -f "$file" ]] || die "missing $file"
  line="$(grep -E '^KASGATE_DOMAIN=' "$file" | tail -n 1 || true)"
  domain="${line#KASGATE_DOMAIN=}"
  domain="${domain%$'\r'}"
  [[ -n "$domain" ]] || die "KASGATE_DOMAIN is not set in $file"
  is_valid_domain "$domain" || die "KASGATE_DOMAIN is not a valid domain: $domain"
  printf '%s' "$domain"
}

require_repo_dir() {
  local repo_dir="$1"
  [[ -n "$repo_dir" ]] || die "--repo-dir is required"
  [[ -f "${repo_dir}/infra/docker-compose.yml" ]] || die "${repo_dir}/infra/docker-compose.yml not found"
  [[ -f "${repo_dir}/infra/deploy/docker-compose.prod.yml" ]] || die "${repo_dir}/infra/deploy/docker-compose.prod.yml not found"
}

compose_in_infra() {
  # Run docker compose with the production override. Remaining args are passed through.
  local infra_dir="$1"
  shift
  docker compose -f "${infra_dir}/docker-compose.yml" -f "${infra_dir}/deploy/docker-compose.prod.yml" "$@"
}

default_repo_url() { printf '%s' "https://github.com/kasgate/kasgate.git"; }
