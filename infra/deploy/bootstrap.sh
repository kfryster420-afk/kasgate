#!/usr/bin/env bash
# Provision a fresh Ubuntu host to run the KasGate public testnet beta
# (gateway + web on loopback, published through a Cloudflare Tunnel).
#
#   sudo ./bootstrap.sh --repo-dir /opt/kasgate \
#     [--repo-url https://github.com/kasgate/kasgate.git] \
#     [--user kasgate] [--ssh-port 22] [--domain kasgate.io]
#
# What it does (safe to re-run):
#   1. creates a non-root user (sudo + docker) and copies root authorized_keys if the
#      user has none yet
#   2. installs Docker Engine and the Compose plugin from Docker's official apt repository
#   3. enables unattended-upgrades for security updates
#   4. ufw: deny incoming by default, allow SSH only (the tunnel is outbound; 3000/8000
#      stay on loopback)
#   5. installs the cloudflared package (does not log in or create a tunnel)
#   6. uses the repo at --repo-dir (clones --repo-url if the directory does not exist)
#   7. copies env.production.example to infra/.env if missing (mode 600; no secrets filled)
#   8. if --domain is given, writes KASGATE_DOMAIN and the derived URL fields
#
# It never starts the stack and never writes ENCRYPTION_KEY, database passwords, or a
# treasury address. Fill those in, then follow docs/DEPLOY.md.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# When this script is run from a git clone, common.sh sits beside it. When the file
# has been copied alone onto a blank host, we only need common.sh after the repo exists,
# so helpers used before that are defined inline below as well.
if [[ -f "${SCRIPT_DIR}/common.sh" ]]; then
  # shellcheck source=common.sh
  source "${SCRIPT_DIR}/common.sh"
else
  log() { printf '[kasgate-deploy] %s\n' "$*"; }
  die() { printf '[kasgate-deploy] ERROR: %s\n' "$*" >&2; exit 1; }
fi

REPO_DIR=""
REPO_URL="https://github.com/kasgate/kasgate.git"
APP_USER="kasgate"
SSH_PORT="22"
DOMAIN=""

usage() {
  sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

is_valid_domain() {
  [[ "$1" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$ ]]
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-dir) REPO_DIR="${2:-}"; shift 2 ;;
    --repo-url) REPO_URL="${2:-}"; shift 2 ;;
    --user) APP_USER="${2:-}"; shift 2 ;;
    --ssh-port) SSH_PORT="${2:-}"; shift 2 ;;
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) printf 'unknown argument: %s\n' "$1" >&2; usage 1 ;;
  esac
done

[[ -n "$REPO_DIR" ]] || die "--repo-dir is required"
[[ "$SSH_PORT" =~ ^[0-9]+$ ]] || die "--ssh-port must be a number"
[[ "$SSH_PORT" -ge 1 && "$SSH_PORT" -le 65535 ]] || die "--ssh-port is out of range"
[[ "$APP_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || die "--user must be a valid Linux username"
if [[ -n "$DOMAIN" ]]; then
  is_valid_domain "$DOMAIN" || die "--domain is not a valid domain: $DOMAIN"
fi

[[ "$(id -u)" -eq 0 ]] || die "run as root (sudo)"
# shellcheck source=/dev/null
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || die "this script supports Ubuntu only (found ${PRETTY_NAME:-unknown})"
if [[ "${VERSION_ID:-}" != "24.04" ]]; then
  log "warning: tested on Ubuntu 24.04, found ${PRETTY_NAME:-unknown}"
fi

export DEBIAN_FRONTEND=noninteractive

install_base_packages() {
  log "installing base packages"
  apt-get update -y
  apt-get install -y ca-certificates curl git ufw unattended-upgrades apt-listchanges fail2ban
}

configure_unattended_upgrades() {
  log "enabling unattended-upgrades for security updates"
  cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
CONF
  cat > /etc/apt/apt.conf.d/51kasgate-unattended <<'CONF'
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
CONF
  systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true
}

create_app_user() {
  if id -u "$APP_USER" >/dev/null 2>&1; then
    log "user ${APP_USER} already exists"
  else
    log "creating user ${APP_USER}"
    adduser --disabled-password --gecos "KasGate" "$APP_USER"
  fi
  usermod -aG sudo "$APP_USER"
  local src_keys dst_keys
  src_keys="/root/.ssh/authorized_keys"
  dst_keys="/home/${APP_USER}/.ssh/authorized_keys"
  if [[ -s "$src_keys" ]]; then
    install -d -m 700 -o "$APP_USER" -g "$APP_USER" "/home/${APP_USER}/.ssh"
    if [[ ! -s "$dst_keys" ]]; then
      install -m 600 -o "$APP_USER" -g "$APP_USER" "$src_keys" "$dst_keys"
      log "copied root authorized_keys to ${APP_USER}"
    else
      log "${dst_keys} already present; leaving it unchanged"
    fi
  else
    log "no /root/.ssh/authorized_keys to copy; make sure ${APP_USER} can still log in"
  fi
}

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
  usermod -aG docker "$APP_USER"
}

configure_firewall() {
  log "configuring ufw: allow ${SSH_PORT}/tcp (SSH) only; deny all other incoming"
  command -v ufw >/dev/null 2>&1 || apt-get install -y ufw
  ufw default deny incoming >/dev/null
  ufw default allow outgoing >/dev/null
  ufw allow "${SSH_PORT}/tcp" comment 'SSH' >/dev/null
  ufw --force enable >/dev/null
  ufw status verbose
}

install_cloudflared() {
  if command -v cloudflared >/dev/null 2>&1; then
    log "cloudflared already installed: $(cloudflared --version | head -n 1)"
    return
  fi
  log "installing cloudflared from packages.cloudflare.com"
  install -m 0755 -d /etc/apt/keyrings
  if [[ ! -s /etc/apt/keyrings/cloudflare-main.gpg ]]; then
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o /etc/apt/keyrings/cloudflare-main.gpg
    chmod a+r /etc/apt/keyrings/cloudflare-main.gpg
  fi
  local codename
  codename="${UBUNTU_CODENAME:-${VERSION_CODENAME:-noble}}"
  printf 'deb [signed-by=/etc/apt/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared %s main\n' \
    "$codename" > /etc/apt/sources.list.d/cloudflared.list
  apt-get update -y
  apt-get install -y cloudflared
  log "cloudflared installed: $(cloudflared --version | head -n 1)"
}

prepare_repo() {
  if [[ ! -d "$REPO_DIR" ]]; then
    [[ -n "$REPO_URL" ]] || die "$REPO_DIR does not exist; pass --repo-url to clone it"
    log "cloning $REPO_URL into $REPO_DIR"
    git clone --depth 1 "$REPO_URL" "$REPO_DIR"
  else
    log "using existing repo at $REPO_DIR (not pulling; update it yourself)"
  fi
  [[ -f "$REPO_DIR/infra/docker-compose.yml" ]] || die "$REPO_DIR/infra/docker-compose.yml not found"
  [[ -f "$REPO_DIR/infra/deploy/env.production.example" ]] || die "$REPO_DIR/infra/deploy/env.production.example not found"
  chown -R "${APP_USER}:${APP_USER}" "$REPO_DIR"
}

prepare_env() {
  local env_file="${REPO_DIR}/infra/.env"
  local example="${REPO_DIR}/infra/deploy/env.production.example"
  if [[ ! -f "$env_file" ]]; then
    install -m 600 -o "$APP_USER" -g "$APP_USER" "$example" "$env_file"
    log "created ${env_file} from env.production.example; fill in the secrets before starting the stack"
  else
    log "${env_file} already exists; leaving secrets and values unchanged"
    chmod 600 "$env_file"
    chown "${APP_USER}:${APP_USER}" "$env_file"
  fi
  if [[ -n "$DOMAIN" ]]; then
    if [[ -x "${REPO_DIR}/infra/deploy/apply-domain.sh" ]]; then
      sudo -u "$APP_USER" "${REPO_DIR}/infra/deploy/apply-domain.sh" --repo-dir "$REPO_DIR" --domain "$DOMAIN"
    else
      die "apply-domain.sh is missing after checkout"
    fi
  fi
}

install_base_packages
configure_unattended_upgrades
create_app_user
install_docker
configure_firewall
install_cloudflared
prepare_repo
prepare_env

log "bootstrap complete."
log "Next: fill ${REPO_DIR}/infra/.env (treasury kaspatest: address, ENCRYPTION_KEY, passwords),"
log "create the Cloudflare Tunnel, then follow docs/DEPLOY.md. Do not start the stack until secrets are set."
log "Log in as ${APP_USER} (docker group applies on the next login)."
