#!/usr/bin/env bash
# Provision a fresh Ubuntu 24.04 host to run KasGate's own Kaspa node (compose profile `node`).
#
#   sudo ./provision.sh --repo-dir /opt/kasgate [--repo-url https://github.com/<org>/kasgate.git] \
#                       [--network testnet-10|mainnet] [--ssh-port 22]
#
# What it does (safe to re-run):
#   1. installs Docker Engine + compose plugin from Docker's official apt repository
#   2. ufw: deny incoming by default, allow SSH and the Kaspa P2P port only (16211 testnet-10 / 16111 mainnet)
#   3. uses the repo at --repo-dir (clones --repo-url there if the directory does not exist yet)
#   4. writes the KASPAD_* settings into infra/.env (created from infra/.env.example if missing; no secrets are set)
#   5. installs and starts the systemd unit kasgate-kaspad.service, which runs `docker compose --profile node up -d kaspad`
# It never publishes the RPC port and never starts the gateway, Postgres or Redis.
set -euo pipefail

REPO_DIR=""
REPO_URL=""
NETWORK="testnet-10"
SSH_PORT="22"
UNIT_NAME="kasgate-kaspad.service"

usage() {
  sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

log() { printf '[provision] %s\n' "$*"; }
die() { printf '[provision] ERROR: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-dir) REPO_DIR="${2:-}"; shift 2 ;;
    --repo-url) REPO_URL="${2:-}"; shift 2 ;;
    --network) NETWORK="${2:-}"; shift 2 ;;
    --ssh-port) SSH_PORT="${2:-}"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage 1 ;;
  esac
done

[[ -n "$REPO_DIR" ]] || die "--repo-dir is required"
[[ "$SSH_PORT" =~ ^[0-9]+$ ]] || die "--ssh-port must be a number"
case "$NETWORK" in
  testnet-10) KASPAD_TESTNET=true;  P2P_PORT=16211; RPC_BORSH_PORT=17210; KASPA_NETWORK_VALUE="kaspa:testnet-10" ;;
  mainnet)    KASPAD_TESTNET=false; P2P_PORT=16111; RPC_BORSH_PORT=17110; KASPA_NETWORK_VALUE="kaspa:mainnet" ;;
  *) die "--network must be testnet-10 or mainnet" ;;
esac

[[ "$(id -u)" -eq 0 ]] || die "run as root (sudo)"
# shellcheck source=/dev/null
. /etc/os-release
[[ "${ID:-}" == "ubuntu" && "${VERSION_ID:-}" == "24.04" ]] || log "warning: tested on Ubuntu 24.04, found ${PRETTY_NAME:-unknown}"

export DEBIAN_FRONTEND=noninteractive

install_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    log "docker + compose plugin already installed: $(docker --version)"
  else
    log "installing Docker Engine from download.docker.com"
    apt-get update -y
    apt-get install -y ca-certificates curl
    install -m 0755 -d /etc/apt/keyrings
    if [[ ! -s /etc/apt/keyrings/docker.asc ]]; then
      curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
      chmod a+r /etc/apt/keyrings/docker.asc
    fi
    local arch codename
    arch="$(dpkg --print-architecture)"
    codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-noble}}"
    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu %s stable\n' \
      "$arch" "$codename" > /etc/apt/sources.list.d/docker.list
    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  fi
  systemctl enable --now docker
}

configure_firewall() {
  log "configuring ufw: allow ${SSH_PORT}/tcp (SSH) and ${P2P_PORT}/tcp (Kaspa P2P ${NETWORK}) only"
  command -v ufw >/dev/null 2>&1 || apt-get install -y ufw
  ufw default deny incoming >/dev/null
  ufw default allow outgoing >/dev/null
  ufw allow "${SSH_PORT}/tcp" comment 'SSH' >/dev/null
  ufw allow "${P2P_PORT}/tcp" comment "Kaspa P2P ${NETWORK}" >/dev/null
  ufw --force enable >/dev/null
  ufw status verbose
}

prepare_repo() {
  if [[ ! -d "$REPO_DIR" ]]; then
    [[ -n "$REPO_URL" ]] || die "$REPO_DIR does not exist; pass --repo-url to clone it"
    command -v git >/dev/null 2>&1 || apt-get install -y git
    log "cloning $REPO_URL into $REPO_DIR"
    git clone --depth 1 "$REPO_URL" "$REPO_DIR"
  else
    log "using existing repo at $REPO_DIR (not pulling; update it yourself)"
  fi
  [[ -f "$REPO_DIR/infra/docker-compose.yml" ]] || die "$REPO_DIR/infra/docker-compose.yml not found"
  grep -qE '^  kaspad:' "$REPO_DIR/infra/docker-compose.yml" \
    || die "infra/docker-compose.yml has no kaspad service (apply the node profile change first)"
}

# set_env KEY VALUE: replace an existing KEY= line in infra/.env or append it.
set_env() {
  local key="$1" value="$2" file="$REPO_DIR/infra/.env"
  if grep -qE "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

configure_env() {
  local file="$REPO_DIR/infra/.env"
  if [[ ! -f "$file" ]]; then
    cp "$REPO_DIR/infra/.env.example" "$file"
    chmod 600 "$file"
    log "created $file from .env.example: fill in the secrets before starting the gateway"
  fi
  set_env KASPAD_TESTNET "$KASPAD_TESTNET"
  set_env KASPAD_P2P_PORT "$P2P_PORT"
  set_env KASPAD_RPC_BORSH_PORT "$RPC_BORSH_PORT"
  if grep -qE "^KASPA_NETWORK=" "$file" && ! grep -qE "^KASPA_NETWORK=${KASPA_NETWORK_VALUE}$" "$file"; then
    log "warning: infra/.env KASPA_NETWORK differs from --network ${NETWORK}; the gateway and node must agree"
  fi
  log "node settings written to $file (KASPA_RPC_URL left unchanged; set ws://kaspad:${RPC_BORSH_PORT} once synced)"
}

install_unit() {
  local compose_bin infra_dir
  compose_bin="$(command -v docker)"
  infra_dir="$(cd "$REPO_DIR/infra" && pwd)"
  cat > "/etc/systemd/system/${UNIT_NAME}" <<UNIT
[Unit]
Description=KasGate Kaspa node (docker compose profile node)
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=${infra_dir}
ExecStart=${compose_bin} compose --profile node up -d kaspad
ExecStop=${compose_bin} compose --profile node stop -t 120 kaspad
TimeoutStartSec=600
TimeoutStopSec=180

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable "$UNIT_NAME" >/dev/null
  systemctl restart "$UNIT_NAME"
  log "systemd unit ${UNIT_NAME} enabled and started"
}

install_docker
configure_firewall
prepare_repo
configure_env
install_unit

log "done. Follow sync with: cd $REPO_DIR/infra && docker compose logs -f kaspad"
log "RPC is internal only (ws://kaspad:${RPC_BORSH_PORT} on the compose network); P2P ${P2P_PORT}/tcp is open."
