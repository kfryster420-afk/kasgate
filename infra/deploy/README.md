# KasGate deploy scripts

Public testnet-beta hosting: the web and gateway run on a small Ubuntu VPS, published through a Cloudflare Tunnel so the VPS does not expose ports 80 or 443. DNS and TLS live on a free Cloudflare account. The domain is one variable, `KASGATE_DOMAIN` (preferred `kasgate.io`, fallback `kasgate.dev`).

The step-by-step runbook, including the go-live checklist, is **[docs/DEPLOY.md](../../docs/DEPLOY.md)**.

| Script | Purpose |
| --- | --- |
| `bootstrap.sh` | Idempotent VPS prep: non-root user, Docker, Compose, firewall (SSH only), unattended upgrades, `cloudflared`, repo checkout |
| `apply-domain.sh` | Writes `PUBLIC_BASE_URL` and `CORS_ORIGINS` from `KASGATE_DOMAIN` in `infra/.env` |
| `render-tunnel-config.sh` | Renders `cloudflared/config.yml.example` for `/etc/cloudflared/config.yml` |
| `deploy-stack.sh` | Builds and starts Postgres, Redis, the gateway and the web with the production override |
| `deploy-web.sh` | Rebuilds and rolls only the web image (needed after a domain change, because `NEXT_PUBLIC_*` is baked in at build time) |

`env.production.example` is the secret-free template; copy it to `infra/.env` and fill in values. Never commit `infra/.env`.
