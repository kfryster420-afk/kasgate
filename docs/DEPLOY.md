# Deploying the KasGate public testnet beta

This is the runbook for putting KasGate on the internet as a **Kaspa testnet-10 beta**. The website and the payment gateway launch together. Testers use free test KAS only. Mainnet is a later switch, after the beta, and is not done by this document.

Related docs: configuration and local operation live in [RUNBOOK.md](RUNBOOK.md). The wire protocol is in [CONTRACTS.md](CONTRACTS.md). The tester scenarios are in [beta/TEST_PLAN.md](beta/TEST_PLAN.md).

## 1. What you are setting up

| Piece | Where it runs | Public hostname |
| --- | --- | --- |
| Web app (Next.js, including `/beta`) | Docker on the VPS, port 3000 on loopback | `https://<KASGATE_DOMAIN>` and `https://www.<KASGATE_DOMAIN>` |
| Gateway (FastAPI) | Docker on the VPS, port 8000 on loopback | `https://api.<KASGATE_DOMAIN>` |
| Postgres 16 and Redis 7 | Docker on the VPS, loopback only | not published |
| DNS and TLS | Free Cloudflare account | the names above |
| Ingress | `cloudflared` on the VPS (Cloudflare Tunnel) | outbound only; no public 80/443 on the VPS |

The web app is a dynamic Next.js standalone server (`output: "standalone"`). For the beta it runs on the VPS and is published through the Tunnel, so Cloudflare still terminates TLS and holds the DNS on the free plan. Moving the web app onto Cloudflare Workers would need an OpenNext adapter and is a later migration, not a requirement to go live.

The gateway talks to Kaspa through **public nodes** (the wRPC Resolver). Leave `KASPA_RPC_URL` empty. An own `kaspad` is optional and is not part of this beta deploy.

## 2. What you need first

- A **domain**. Preferred: `kasgate.io`. Fallback if that is taken at checkout: `kasgate.dev`. Buy it in the Cloudflare account that will hold DNS. The domain is one variable, `KASGATE_DOMAIN`. Every public URL is derived from it.
- A **small Ubuntu VPS**, about $5–10 a month (Hetzner CX22 / CAX11 or similar). 2 vCPU and 4 GB RAM is comfortable; 2 GB will be tight. Ubuntu 24.04 is what the bootstrap script is tested against. You need SSH access as root or a sudo user.
- The **Cloudflare** account that owns the domain (free plan is enough for DNS, Universal SSL, and a Tunnel).
- A **testnet treasury address** starting with `kaspatest:`. This receives output 1 (the platform fee) during the beta. Create a throw-away testnet wallet for it. **Do not configure the mainnet `kaspa:` treasury address while the network is testnet-10** — the gateway will refuse to start if the prefix does not match the network.

## 3. Bootstrap the VPS

From this repository, as root on the VPS:

```bash
# After the repo is on the host, or copy bootstrap.sh first and let it clone:
sudo ./infra/deploy/bootstrap.sh \
  --repo-dir /opt/kasgate \
  --repo-url https://github.com/kasgate/kasgate.git \
  --user kasgate \
  --domain kasgate.io
```

The script is safe to re-run. It creates the `kasgate` user (sudo + docker), installs Docker and Compose, turns on unattended security upgrades, opens only the SSH port in `ufw`, installs `cloudflared` without logging in, clones the repo if needed, and writes `infra/.env` from `infra/deploy/env.production.example` if that file does not already exist.

Log out and back in as `kasgate` so the docker group applies.

## 4. Fill in secrets and the testnet treasury

```bash
chmod 600 /opt/kasgate/infra/.env
# generate:
python3 -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"   # ENCRYPTION_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(24))"                             # POSTGRES_PASSWORD and REDIS_PASSWORD
```

Edit `/opt/kasgate/infra/.env`:

- Set `ENCRYPTION_KEY`, `POSTGRES_PASSWORD`, and `REDIS_PASSWORD` to those values (URL-safe characters only in the passwords).
- Set `KASGATE_TREASURY_ADDRESS` to your **kaspatest:** address.
- Leave `KASPA_NETWORK=kaspa:testnet-10` and `KASPA_RPC_URL` empty.
- Leave `GATEWAY_BIND` and `WEB_BIND` at `127.0.0.1`.
- Leave `FORWARDED_ALLOW_IPS=127.0.0.1` so the gateway trusts `X-Forwarded-For` from `cloudflared` on loopback and rate-limits by client IP. Never set it to `*`.

If you did not pass `--domain` to bootstrap, set `KASGATE_DOMAIN` and run:

```bash
/opt/kasgate/infra/deploy/apply-domain.sh --repo-dir /opt/kasgate
```

That writes `PUBLIC_BASE_URL=https://api.<domain>` and `CORS_ORIGINS` for `https://<domain>` and `https://www.<domain>`. Re-run it whenever the domain changes, then rebuild the web image (`deploy-web.sh`): `NEXT_PUBLIC_GATEWAY_URL` is baked in at build time and must equal `PUBLIC_BASE_URL` exactly, or KasWare login challenges fail the Domain check.

## 5. Cloudflare Tunnel and DNS

On your laptop (Cloudflare account logged in) or on the VPS after `cloudflared tunnel login`:

```bash
cloudflared tunnel login
cloudflared tunnel create kasgate
# Note the tunnel UUID and the credentials file path printed by that command,
# usually /home/<user>/.cloudflared/<UUID>.json or /root/.cloudflared/<UUID>.json.
sudo install -d -m 700 /etc/cloudflared
sudo install -m 600 /home/kasgate/.cloudflared/<UUID>.json /etc/cloudflared/<UUID>.json

sudo /opt/kasgate/infra/deploy/render-tunnel-config.sh \
  --repo-dir /opt/kasgate \
  --tunnel-id <UUID> \
  --credentials-file /etc/cloudflared/<UUID>.json \
  --output /etc/cloudflared/config.yml
```

Point the hostnames at the tunnel (Cloudflare will create proxied CNAMEs to `<UUID>.cfargotunnel.com`):

```bash
cloudflared tunnel route dns kasgate kasgate.io
cloudflared tunnel route dns kasgate www.kasgate.io
cloudflared tunnel route dns kasgate api.kasgate.io
```

Install and start the service once the config is in place:

```bash
sudo cloudflared service install
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared --no-pager
```

In the Cloudflare dashboard, optionally add a redirect from `www.<domain>` to `https://<domain>` so session cookies stay on one host. Both names still need DNS records.

The credentials JSON and a logged-in `cert.pem` never go in git.

## 6. Start the stack and publish the web app

```bash
/opt/kasgate/infra/deploy/deploy-stack.sh --repo-dir /opt/kasgate
```

This builds and starts Postgres, Redis, the gateway, and the web app with `infra/deploy/docker-compose.prod.yml` (log rotation and memory limits). It refuses to start if secrets or the treasury address are still empty, or if `PUBLIC_BASE_URL` does not match `KASGATE_DOMAIN`.

To rebuild only the web image later (after a domain change or a frontend deploy):

```bash
/opt/kasgate/infra/deploy/deploy-web.sh --repo-dir /opt/kasgate
```

On the VPS, before you trust public DNS:

```bash
curl -sS http://127.0.0.1:8000/healthz
curl -sS http://127.0.0.1:3000/api/health
```

`/healthz` must report `network` as `kaspa:testnet-10`. Through the Tunnel:

- `https://<domain>/` shows the **Testnet beta** banner and a link to `/beta`.
- `https://<domain>/beta` is the getting-started guide (KasWare, official TN-10 faucet, register, SDK, bug reports).
- `https://api.<domain>/healthz` is the gateway.
- An unpaid `curl -i https://api.<domain>/p/<slug>` returns HTTP 402 (once you have created an endpoint).

Wallet challenges use `Domain: api.<domain>`. A paid call through the Tunnel must return 200, not 400 or 431: Cloudflare and `cloudflared` have to accept the large `PAYMENT-SIGNATURE` header (at least 64 KiB, same rule as in the runbook).

## 7. After the beta (not now)

Switching to mainnet is a separate, deliberate change: new `kaspa:` treasury address, `KASPA_NETWORK=kaspa:mainnet`, rebuild the web image, and the production checklist in [RUNBOOK.md](RUNBOOK.md#7-production-checklist). Do not put the mainnet treasury into `infra/.env` while the beta is still on testnet-10.

## 8. Go-live checklist

Complete every item before inviting testers.

**Domain and Cloudflare**

- [ ] Domain purchased (preferred `kasgate.io`, otherwise `kasgate.dev`) and nameservers are at Cloudflare.
- [ ] `KASGATE_DOMAIN` in `infra/.env` is that domain, and `apply-domain.sh` has been run.
- [ ] Tunnel `kasgate` exists; credentials file is mode 600 under `/etc/cloudflared/` and is not in git.
- [ ] DNS routes exist for the apex, `www`, and `api` hostnames, proxied through the tunnel.
- [ ] `cloudflared` is enabled and running (`systemctl status cloudflared`).
- [ ] Optional: `www` redirects to the apex.

**VPS**

- [ ] Ubuntu host bootstrapped with `infra/deploy/bootstrap.sh`.
- [ ] Non-root user can run `docker compose`.
- [ ] `ufw` denies incoming except SSH. Ports 3000, 8000, 5432, and 6379 are not reachable from the internet (`GATEWAY_BIND` / `WEB_BIND` are `127.0.0.1`).
- [ ] Unattended upgrades are on. The host has 2 vCPU / 4 GB RAM or you have watched memory with the compose limits.
- [ ] You can still log in over SSH after bootstrap (authorized keys were copied or already present).

**Secrets and network**

- [ ] `infra/.env` is mode 600, not in git, and is backed up encrypted off the host.
- [ ] `ENCRYPTION_KEY`, `POSTGRES_PASSWORD`, and `REDIS_PASSWORD` are unique random values, not the blanks in the example file.
- [ ] `KASGATE_TREASURY_ADDRESS` is a real **kaspatest:** address you control. The mainnet treasury address is not set.
- [ ] `KASPA_NETWORK=kaspa:testnet-10` and `KASPA_RPC_URL` is empty (public Resolver).
- [ ] `KASGATE_COOKIE_SECURE=true`, `RATE_LIMIT_ENABLED=true`, `RATE_LIMIT_FAIL_CLOSED=true`, `SETTLEMENT_MODE=accepted`, `FORWARDED_ALLOW_IPS=127.0.0.1`.

**Application**

- [ ] `deploy-stack.sh` completed and `docker compose … ps` shows postgres, redis, gateway, and web healthy.
- [ ] `http://127.0.0.1:8000/healthz` and `http://127.0.0.1:3000/api/health` succeed on the VPS.
- [ ] `https://<domain>/` shows the Testnet beta banner.
- [ ] `https://<domain>/beta` loads the getting-started page and the faucet link `https://faucet-tn10.kaspanet.io/` is correct.
- [ ] `https://api.<domain>/healthz` reports `kaspa:testnet-10`.
- [ ] You can register a merchant with KasWare on testnet-10, create an endpoint priced at 0.6 KAS, and complete one paid call. The explorer shows output 0 to the merchant and output 1 to the testnet treasury.
- [ ] A paid call through the Tunnel returns 200, not 400/431 (header size).
- [ ] Bug report, beta feedback, and private vulnerability links from `/beta` open the right GitHub forms. The `beta` label exists on the repository.

**Testers**

- [ ] The site is clearly marked as a testnet beta. The invite posts in the operator's notes are ready to send (they were drafted, not published, during prep).
- [ ] You are ready to point testers at `/beta`, the official faucet, and [docs/beta/TEST_PLAN.md](beta/TEST_PLAN.md).
