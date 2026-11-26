# KasGate runbook

How to configure, start, fund, exercise, test and ship KasGate: the x402 v2 (`kaspa-exact-v1`) pay-per-call gateway on Kaspa. Every command and variable here exists in this repository. The protocol itself is in [`CONTRACTS.md`](CONTRACTS.md).

Contents: [1 Prerequisites](#1-prerequisites) | [2 Configure `.env`](#2-configure-infraenv) | [3 Start the stack](#3-start-the-stack) | [4 Testnet-10 wallet](#4-testnet-10-wallet) | [5 Register, create an endpoint, pay it](#5-register-a-merchant-create-an-endpoint-and-pay-it) | [6 Test suites](#6-test-suites) | [7 Production checklist](#7-production-checklist) | [8 Optional own node](#8-optional-own-node) | [9 Troubleshooting](#9-troubleshooting) | [10 Verified results](#10-verified-results)

To put the public testnet-10 beta on the internet (VPS, Cloudflare Tunnel, domain, go-live checklist), use [`DEPLOY.md`](DEPLOY.md). This runbook remains the source for configuration, local operation, tests and the later mainnet switch.

Terms used below: 1 KAS = 100,000,000 sompi. "Gateway" is `apps/gateway` (FastAPI). "Web" is `apps/web` (Next.js 15). Paths are relative to the repository root unless a `cd` says otherwise.

## 1. Prerequisites

| Need | Why |
| --- | --- |
| Docker Engine with the Compose v2 plugin (`docker compose`) | Runs Postgres 16, Redis 7, gateway and web (`infra/docker-compose.yml`). |
| Python 3.12 and `pip` | Local tests, wallet key generation, the buyer SDK. The gateway image is `python:3.12-slim`; the SDK and MCP server accept 3.10+. |
| Node 20 and npm | Only for running the web tests or `npm run dev` outside Docker (the web image is `node:20-alpine`). |
| Outbound HTTPS from the gateway host | To the public Kaspa node Resolver and the nodes it returns (WebSocket), to the KAS/USD price API (`KAS_USD_PRICE_URL`, CoinGecko by default), and to every merchant upstream. |
| A browser with the KasWare wallet | Merchant registration and login are wallet-signature only (a manual signing form exists as a fallback). |
| A Kaspa treasury address | Receives output 1 (the platform fee). Use a `kaspatest:` address on testnet-10 and a `kaspa:` address on mainnet. KasGate never holds keys. |
| Free host ports 3000 (web), 8000 (gateway), 127.0.0.1:5432 (Postgres), 127.0.0.1:6379 (Redis) | Published by compose. |

Hardware for the stack itself is modest (a 2 vCPU / 4 GB host is enough while you use public nodes). Only an own Kaspa node is heavy; see [section 8](#8-optional-own-node).

## 2. Configure `infra/.env`

```bash
cp infra/.env.example infra/.env
chmod 600 infra/.env          # .env is git-ignored; it holds secrets
```

Compose reads `infra/.env` twice: for `${...}` substitution and, through `env_file`, as the gateway container's environment. The gateway also gets `DATABASE_URL` and `REDIS_URL` from compose, built from the `POSTGRES_*` and `REDIS_PASSWORD` values, so do not set those two in `.env`. Settings are validated at startup (`apps/gateway/app/config.py`); a bad value stops the gateway with a clear error.

Generate the two secrets you must provide (the Fernet key is 32 random bytes in URL-safe base64, 44 characters, which is exactly what `Fernet.generate_key()` returns):

```bash
python3 -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"   # ENCRYPTION_KEY
python3 -c "import secrets; print(secrets.token_urlsafe(24))"                             # POSTGRES_PASSWORD / REDIS_PASSWORD
```

`token_urlsafe` can emit `-` and `_` only, which is what the connection URLs need. Do not put other punctuation in the Postgres or Redis passwords.

### Variables in `infra/.env.example`

| Variable | Default | Meaning |
| --- | --- | --- |
| `KASPA_NETWORK` | `kaspa:testnet-10` | `kaspa:testnet-10` or `kaspa:mainnet`. Also passed to the web build as `NEXT_PUBLIC_KASPA_NETWORK`. |
| `KASPA_RPC_URL` | empty | Fixed wRPC Borsh URL, for example `ws://kaspad:17210`. **Empty or blank means the public Resolver. This is the current decision: public nodes for now.** Set it only when you run your own node ([section 8](#8-optional-own-node)). |
| `KASPA_CONNECT_TIMEOUT_SECONDS` | `6` | Connect budget, split across 3 attempts. Each retry uses a fresh client. |
| `KASGATE_TREASURY_ADDRESS` | placeholder | **Required.** Output 1 of every payment. Must start with `kaspatest:` on testnet-10 and `kaspa:` on mainnet, or the gateway refuses to start. Startup checks only the prefix, not the checksum: the `.env.example` placeholder passes startup but is not a real address, so every payment challenge then fails. Replace it with a real address and confirm with an unpaid `curl -i` (section 5.2). |
| `MIN_OUTPUT_SOMPI` | `20000000` | KIP-9 storage-mass floor per output (0.2 KAS). It sets the flat fee and the minimum price (3 x floor = 0.6 KAS). Changing it changes the price floor shown in `/v1/directory`; leave it unless you know why. |
| `ENCRYPTION_KEY` | empty | **Required.** Fernet key (44 characters) that encrypts merchants' upstream secret headers at rest. Back it up: without it stored upstream headers cannot be decrypted. |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | `kasgate`, placeholder, `kasgate` | Database credentials. URL-safe characters only. |
| `REDIS_PASSWORD` | placeholder | Redis password (Redis runs with `--requirepass` and AOF). |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | The externally visible gateway URL, no trailing slash (one is stripped). **It must be the exact URL clients and the browser use to reach the gateway.** The wallet challenge text embeds `Domain: <lowercased netloc of PUBLIC_BASE_URL>` (host, plus `:port` only when the URL has one). Compose also builds the web image with `NEXT_PUBLIC_GATEWAY_URL` set to this value, and the web app refuses any challenge whose `Domain` differs. `https://` is mandatory on `kaspa:mainnet` (enforced at startup). Changing it requires rebuilding the web image (`docker compose up -d --build web`). |
| `CORS_ORIGINS` | `["http://localhost:3000"]` | JSON list of browser origins allowed to call the gateway directly. Put your web origin here (for example `["https://app.example.com"]`). |
| `PAYMENT_MAX_AGE_SECONDS` | `120` | Lifetime of an issued payment challenge (nonce). Allowed 10 to 3600. |
| `KAS_USD_PRICE_URL` | CoinGecko simple-price URL | Price source for the USD value stored with each settlement. If it is unreachable the last known rate is used, then the settlement is stored without a USD value. |
| `KASGATE_COOKIE_SECURE` | `true` | Web session cookie `Secure` flag. Set `false` only for plain-http hosts other than `localhost`. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Comma-separated proxy IPs or CIDRs whose `X-Forwarded-For` uvicorn trusts. See [section 7](#7-production-checklist): it must match what the gateway container sees as your reverse proxy, or every client shares one rate-limit bucket. Never `*` on a directly exposed port. |
| `RATE_LIMIT_ENABLED` | `true` | Redis fixed-window limits, per caller IP only. Turn off only for local integration testing. |
| `RATE_LIMIT_FAIL_CLOSED` | `true` | When Redis is down: `true` returns 503, `false` lets requests through. |
| `RL_MERCHANT_CREATE_PER_HOUR` | `5` | Merchant sign-ups per IP per hour. |
| `RL_AUTH_CHALLENGE_PER_MINUTE` | `10` | Auth challenges per IP per minute. |
| `RL_AUTH_VERIFY_PER_MINUTE` | `10` | Auth verifications per IP per minute. |
| `RL_UNPAID_PROXY_PER_MINUTE` | `300` | Unpaid hits per IP per minute on `/p/*` and `/v1/quote/*` (anything that mints a challenge). |
| `KASPAD_TESTNET`, `KASPAD_P2P_PORT`, `KASPAD_RPC_BORSH_PORT`, `KASPAD_RAM_SCALE` | `true`, `16211`, `17210`, `1.0` | Only used by the optional `node` profile ([section 8](#8-optional-own-node)). Ignored otherwise. |

### Gateway settings that are not in `.env.example`

`apps/gateway/app/config.py` also reads these. They have working defaults; add them to `infra/.env` only to change them.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SETTLEMENT_MODE` | `accepted` | `accepted` waits until the virtual chain accepts the transaction, up to `SETTLEMENT_TIMEOUT_SECONDS`. `mempool` returns as soon as the node accepts the submission; testnet demos only. |
| `SETTLEMENT_TIMEOUT_SECONDS` | `8` | Wait for acceptance (0 to 60). On timeout the buyer gets 503 `rpc_unavailable` and may retry with the same signature. |
| `KASPA_RPC_TIMEOUT_SECONDS` | `10` | Per-RPC-call timeout. |
| `REDELIVERY_WINDOW_SECONDS` | `600` | How long after settlement a buyer may re-present the same signature to get the upstream response again (0 to 3600). |
| `REDELIVERY_MAX_ATTEMPTS` | `3` | Maximum upstream deliveries per settlement (0 to 10). |
| `SESSION_TTL_SECONDS` | `604800` | Dashboard session lifetime (7 days). |
| `LEDGER_WORKER_ENABLED` | `true` | CONTRACT v2.13 prepaid ledger worker: expires due credit and re-checks refund payouts that were not visible yet. Safe on several replicas. |
| `LEDGER_POLL_INTERVAL_SECONDS` | `30` | Pause between ledger worker passes. |
| `LEDGER_BATCH_SIZE` | `100` | Balances expired and payouts re-checked per pass. |
| `PREPAID_PAYOUT_PENDING_MAX_SECONDS` | `86400` | A refund still not confirmed after this long goes back to `requested` (the merchant can pay again); its attempted tx_ids stay on record and keep being re-checked. |
| `PREPAID_PAYOUT_ATTEMPT_MAX_SECONDS` | `604800` | An attempted payout tx_id never confirmed for this long is `abandoned` (re-checks stop; the buyer may cancel again). |
| `PREPAID_PAYOUT_BACKOFF_BASE_SECONDS` / `PREPAID_PAYOUT_BACKOFF_MAX_SECONDS` | `30` / `1800` | Re-check backoff after a node error on an attempted payout (doubles per error). Node errors never revert a payout. |
| `PREPAID_REFUND_CLAIM_TTL_SECONDS` | `900` | How long a merchant's claim (`paying`) blocks the buyer's cancel without a posted payout. |
| `PREPAID_REFUND_CLAIM_MAX_SECONDS` | `3600` | Total claim time from the first claim (`first_claimed_at`); renewals past it are refused. `first_claimed_at` survives a payout revert, so after this window a reverted refund cannot be claimed again (the merchant pays unclaimed, or rejects); the pre-cancel scan and cooldown still apply. |
| `PREPAID_REFUND_CANCEL_COOLDOWN_SECONDS` | `1800` | After the buyer cancels a refund the merchant had claimed, the amount stays held this long (`hold_until`) before it is spendable again. `0` releases at once. |
| `PREPAID_PAYOUT_MAX_PENDING_ATTEMPTS` | `3` | Unconfirmed payout attempts allowed per refund (409 `too_many_attempts` beyond). |
| `PREPAID_PAYOUT_UNSEEN_BACKOFF_FRACTION` | `0.1` | An unseen attempt is re-checked after its age x this fraction (capped by `PREPAID_PAYOUT_BACKOFF_MAX_SECONDS`). |
| `RL_REFUND_PAYOUT_PER_MERCHANT_PER_MINUTE` | `20` | `POST /v1/prepaid/refunds/{id}/payout` per merchant per minute. |
| `RL_PREPAID_REFUND_PER_MINUTE` | `30` | Signed refund writes per client IP per minute (`POST /v1/prepaid/refunds`, `/cancel`, `/status`). |
| `TRUSTED_PROXY_IPS` | `[]` | JSON list of the exact IPs of your own reverse proxies, e.g. `["172.30.0.10"]` for a proxy container with a fixed address on the compose network (`ipv4_address`). Only from these peers is `X-Forwarded-For` used for per-IP rate limits (right-most hop that is not a trusted proxy). Empty = off: behind a proxy every client then shares the proxy's bucket. **Never list a Docker bridge or host network range** (e.g. `172.16.0.0/12`, `10.0.0.0/8`): with port publishing, every client's traffic can arrive from the bridge gateway IP, and it could then pick its own rate-limit bucket via `X-Forwarded-For`. The proxy must set `X-Forwarded-For` to the real client IP; the web app's server-side gateway calls do not do that yet, so listing the Next.js container changes nothing until they do. |

### Web variables

`apps/web/.env.example` lists `GATEWAY_INTERNAL_URL`, `NEXT_PUBLIC_GATEWAY_URL`, `NEXT_PUBLIC_KASPA_NETWORK` and `KASGATE_COOKIE_SECURE`. Under Docker Compose you do not set them: compose derives `NEXT_PUBLIC_GATEWAY_URL` from `PUBLIC_BASE_URL`, `NEXT_PUBLIC_KASPA_NETWORK` from `KASPA_NETWORK`, and sets `GATEWAY_INTERNAL_URL=http://gateway:8000`. `NEXT_PUBLIC_*` values are inlined at build time. Only when running `npm run dev` outside compose do you set them yourself, and `NEXT_PUBLIC_GATEWAY_URL` must equal the gateway's `PUBLIC_BASE_URL` exactly.

### Minimal testnet-10 `.env`

```dotenv
KASPA_NETWORK=kaspa:testnet-10
KASPA_RPC_URL=
KASGATE_TREASURY_ADDRESS=kaspatest:q...your-treasury-address...
ENCRYPTION_KEY=<44-character key from above>
POSTGRES_PASSWORD=<generated>
REDIS_PASSWORD=<generated>
PUBLIC_BASE_URL=http://localhost:8000
CORS_ORIGINS=["http://localhost:3000"]
```

(keep the other lines from `.env.example`). To get a treasury address, generate a key as in [section 4](#4-testnet-10-wallet) and use its address; keep that key if you want to spend the fees later. Do not reuse the throw-away keys that appear in test files.

## 3. Start the stack

```bash
cd infra
docker compose up --build -d
docker compose ps                  # postgres, redis, gateway, web should become "healthy"/"running"
docker compose logs -f gateway     # Ctrl-C to stop following
```

What happens on start:

1. Postgres and Redis start and report healthy (`pg_isready`, `redis-cli ping`).
2. The gateway image runs **`alembic upgrade head` and then uvicorn** (2 workers, `--proxy-headers`, `--h11-max-incomplete-event-size 65536`). Migrations are therefore applied automatically on every start. Current revisions: `0001_initial`, `0002_payment_nonce`. Check with `docker compose exec gateway alembic current` (expect `0002_payment_nonce (head)`).
3. The web container starts only after the gateway healthcheck passes (compose `depends_on: service_healthy`).

### Health checks

```bash
curl -s http://localhost:8000/healthz
# {"status":"ok","network":"kaspa:testnet-10","database":true,"redis":true,"kaspa_rpc":true}
curl -s http://localhost:3000/api/health     # {"status":"ok"}
curl -s http://localhost:8000/v1/directory   # public catalog; includes min_output_sompi
```

* `/healthz` always answers HTTP 200 and reports the real state in the body. `status` is `ok` only when `database`, `redis` and `kaspa_rpc` are all true. Alert on the body, not the status code.
* `kaspa_rpc` can be `false` for a few seconds after start while the engine connects (3 attempts, fresh client each time). With the public Resolver, some returned nodes never answer; the engine moves on. Wait 30 seconds and check again. If it stays `false`, see [section 9](#9-troubleshooting).
* The compose healthcheck for the gateway only requires HTTP 200 from `/healthz`, so a gateway with `kaspa_rpc:false` is still "healthy" to Docker. Payments get `503 rpc_unavailable` until the node connection is up.

Stop and reset:

```bash
docker compose down            # keep data volumes (pgdata, redisdata)
docker compose down -v         # also delete the database and Redis data (testnet resets only)
```

## 4. Testnet-10 wallet

Use a dedicated throw-away key. Never reuse a mainnet key, and never paste a private key into chat, a ticket or a command line that ends up in shell history.

### 4.1 Generate a key into a mode-600 file

Install the SDK first (it pulls in `kaspa==2.1.0`):

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e packages/buyer-sdk
```

Then write the key and its address. The key file is created with mode 600 and the key is never printed:

```bash
mkdir -p ~/kg-secrets && chmod 700 ~/kg-secrets
python - ~/kg-secrets/buyer.key <<'PY'
import os, secrets, sys
from kaspa import PrivateKey

path = sys.argv[1]
key_hex = secrets.token_hex(32)
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # refuses to overwrite an existing key
with os.fdopen(fd, "w") as handle:
    handle.write(key_hex + "\n")
address = PrivateKey(key_hex).to_address("testnet").to_string()
with open(path.removesuffix(".key") + ".addr", "w") as handle:
    handle.write(address + "\n")
print("address:", address)
PY
ls -l ~/kg-secrets      # buyer.key must be -rw-------
```

The address starts with `kaspatest:q`. That `q` form is a **P2PK Schnorr** address, and it is the only payer type the gateway accepts: it checks the payer script is `0x20 <32-byte x-only pubkey> 0xac` and verifies a Schnorr `payerProof` signature from that key (CONTRACT v2.6). Script-hash (`...p`) and ECDSA payers are refused with `invalid_payment_header`. A KasWare account whose address starts with `kaspatest:q` is fine as a payer, but the SDK needs the raw hex key, so export it or use a generated key as above.

### 4.2 Fund it from the faucet

1. Open <https://faucet-tn10.kaspanet.io/>, paste the `kaspatest:q...` address from `~/kg-secrets/buyer.addr`, and request coins.
2. Confirm the balance (replace the address):
   ```bash
   curl -s https://api-tn10.kaspa.org/addresses/kaspatest:q...your-address.../balance
   ```
   or open `https://explorer-tn10.kaspa.org/addresses/<address>`.
3. If the faucet refuses (it limits requests per address), wait and retry, or ask in the `#testnet` channel of the Kaspa Discord.

### 4.3 Payment rules the wallet must satisfy

| Rule | Value |
| --- | --- |
| Minimum price per call | **0.6 KAS** (60,000,000 sompi) = 3 x `MIN_OUTPUT_SOMPI`. Lower prices are rejected when an endpoint is created. |
| Platform fee | `max(ceil(price x 150 / 10,000), 0.2 KAS)`: a **flat 0.2 KAS up to 13.33333333 KAS** (1,333,333,333 sompi), 1.5% above. The merchant receives the rest. Example: 0.6 KAS = 0.4 merchant + 0.2 fee; 20 KAS = 19.7 merchant + 0.3 fee. |
| Output order | **Output 0 = merchant (exact merchant amount), output 1 = treasury (exact fee).** Anything after that may only be change. |
| Network fee | Paid by the buyer on top of the price. A 0.6 KAS payment paid 253,400 sompi (about 0.0025 KAS) on testnet-10. Budget at least price + 0.01 KAS. |
| Change | Must be **0 or at least 0.3 KAS** (30,000,000 sompi), because of the KIP-9 storage-mass limit (100,000 for standard transactions). The SDK adds another UTXO rather than emit small change and raises `DustChangeError` if it cannot. |
| Inputs | At most 16 distinct payer addresses per payment. At least one input must be spent from `payerAddress`. |
| Payload | The transaction payload must be `kasgate:v1:<nonce>`; the SDK does this. |

### 4.4 Split the balance into several UTXOs (advisable)

A faucet drip usually arrives as one large UTXO. That works, but several mid-sized UTXOs avoid the small-change problem and let parallel calls pay without waiting for change to confirm. Save this as `split_utxos.py`. It is a dry run unless you pass `--submit`:

```python
"""Split one testnet-10 balance into several equal UTXOs. Dry run unless --submit is given. The key is never printed."""
import argparse
import asyncio
import sys

from kaspa import (Address, Generator, NetworkId, PaymentOutput, PrivateKey, Resolver, RpcClient,
                   UtxoEntryReference)

parser = argparse.ArgumentParser()
parser.add_argument("key_file")
parser.add_argument("--count", type=int, default=5)
parser.add_argument("--kas-each", type=int, default=200)
parser.add_argument("--submit", action="store_true")
args = parser.parse_args()

NET = NetworkId("testnet-10")
key = PrivateKey(open(args.key_file).read().strip())
address = key.to_address("testnet").to_string()


def to_refs(entries):
    refs = []
    for e in entries:
        u = e["utxoEntry"]
        spk = u["scriptPublicKey"]
        if isinstance(spk, str):  # the node returns "<u16 version hex><script hex>"
            spk = {"version": int(spk[:4], 16), "script": spk[4:]}
        refs.append(UtxoEntryReference.from_dict({
            "address": e["address"], "outpoint": e["outpoint"], "amount": u["amount"], "scriptPublicKey": spk,
            "blockDaaScore": u["blockDaaScore"], "isCoinbase": u["isCoinbase"], "covenantId": u.get("covenantId")}))
    return refs


def build(refs):
    outputs = [PaymentOutput(Address(address), args.kas_each * 100_000_000) for _ in range(args.count)]
    pending = list(Generator(entries=refs, change_address=Address(address), network_id=NET, outputs=outputs,
                             priority_fee=0))
    if len(pending) != 1:
        sys.exit(f"the balance needs {len(pending)} transactions; lower --count or --kas-each")
    pending[0].sign([key])
    return pending[0]


async def main():
    client = RpcClient(resolver=Resolver(), network_id=NET)
    await client.connect(block_async_connect=True, timeout_duration=10_000)
    try:
        response = await client.get_utxos_by_addresses({"addresses": [address]})
        print("address:", address, "| UTXOs:", len(response["entries"]),
              "| balance KAS:", sum(e["utxoEntry"]["amount"] for e in response["entries"]) / 1e8)
        if not response["entries"]:
            sys.exit("no UTXOs: fund the address from the faucet first")
        tx = build(to_refs(response["entries"]))
        print("transaction", tx.transaction.id, "| network fee sompi:", tx.fee_amount)
        if args.submit:
            await client.submit_transaction({"transaction": tx.transaction, "allowOrphan": False})
            print("submitted")
        else:
            print("dry run: nothing submitted (add --submit)")
    finally:
        await client.disconnect()


asyncio.run(main())
```

```bash
python split_utxos.py ~/kg-secrets/buyer.key                # dry run: shows balance and the planned transaction
python split_utxos.py ~/kg-secrets/buyer.key --submit       # 5 x 200 TKAS plus change back to the same address
```

The balance must cover `count x kas-each` plus the network fee, so request a larger faucet amount than you split. Wait a few seconds, then re-run the dry run: it lists the new UTXO count.

## 5. Register a merchant, create an endpoint and pay it

### 5.1 Register in the UI

1. Open the web app at the origin that matches `PUBLIC_BASE_URL`'s host. Locally that is <http://localhost:3000> with `PUBLIC_BASE_URL=http://localhost:8000`. Use `localhost`, not `127.0.0.1`, if `PUBLIC_BASE_URL` says `localhost` (see [section 9](#9-troubleshooting), `challenge_rejected`).
2. In KasWare, switch to the **testnet-10** network and pick a Schnorr account. Its address starts with `kaspatest:q`.
3. Go to **/onboarding**. Step 1: enter that payout address, a display name and an optional email, then press **Register with KasWare**. The page requests a `register` challenge, checks its `Domain`, `Purpose`, `Address` and `Expires` lines, and only then asks KasWare to sign it (`signMessage`, Schnorr). The gateway verifies the signature and that the public key derives the address.
4. The `kg_...` API key is shown once in a confirm-to-continue panel. Store it in a password manager. The browser only ever keeps a `kgs_...` session cookie (`kg_session`, httpOnly, 7-day lifetime).
5. If KasWare is not detected, use the **or sign manually** form on the same page: sign the exact challenge text with any Kaspa wallet that can do Schnorr message signing, and paste the public key and signature (128 hex, or Base64 of 64 or 65 bytes).
6. Later logins use **/login** with the same wallet proof. Logging in does not rotate the API key.

Registration and login need a working Redis (challenges are stored there) regardless of `RATE_LIMIT_ENABLED`. With rate limits on, sign-ups are limited per IP (5 per hour by default).

### 5.2 Create an endpoint

Step 2 of the onboarding (or the dashboard) asks for:

* **Name**, optional description and category.
* **Upstream URL**: the API you are monetising. It must be a public `http(s)` URL. Literal private IPs and `*.localhost` / `*.internal` hosts are rejected when you save, and the gateway re-resolves DNS on every call and refuses non-public addresses.
* **Upstream headers** (optional, encrypted at rest): for example an `X-Api-Key` for your upstream.
* **Price** in KAS (at least 0.6) and the allowed methods. The page previews the fee split from `/v1/directory`.

Step 3 shows the live proxy URL: `<PUBLIC_BASE_URL>/p/<slug>`. Unpaid calls get `402`:

```bash
curl -i http://localhost:8000/p/<slug>
# HTTP/1.1 402 ... PAYMENT-REQUIRED: <Base64 JSON>   body {"error":"payment_required", ...}
curl -s http://localhost:8000/v1/quote/<slug>        # the same challenge as JSON (public)
```

The `PAYMENT-REQUIRED` header decodes to `x402Version: 2`, `scheme: kaspa-exact-v1`, `network`, `asset: KAS`, `amount` (total sompi), `payTo`, `platformFee{feeTo, feeAmount}`, `nonce` and `expiresAt`.

The same flow without the UI is `POST /v1/auth/challenge` (purpose `register`), sign, `POST /v1/merchants`, then `POST /v1/endpoints` with `Authorization: Bearer kg_...`. The exact `curl` forms are shown in the web app's snippets.

### 5.3 Pay it with the buyer SDK

```bash
. .venv/bin/activate          # the venv from section 4.1 (pip install -e packages/buyer-sdk)
export KASGATE_TEST_PAYER_KEY_FILE=~/kg-secrets/buyer.key
cat > paid_call.py <<'PY'
"""One paid call with the buyer SDK. The key is read from a file inside the process and never printed."""
import asyncio, os, sys
from kasgate_client import KasGateClient, receipt_of

url = sys.argv[1]
key = open(os.environ["KASGATE_TEST_PAYER_KEY_FILE"]).read().strip()

async def main() -> None:
    async with KasGateClient(key, network="kaspa:testnet-10", max_price_sompi=100_000_000) as client:
        response = await client.get(url, max_price_kas="0.6")
    print("status:", response.status_code)
    print("receipt:", receipt_of(response))
    print("body:", response.text[:600])

asyncio.run(main())
PY
python paid_call.py http://localhost:8000/p/<slug>
```

`max_price_sompi` is a hard ceiling for the client, and `max_price_kas` can only lower it per call. The SDK reads the 402, checks the network, fee split and expiry, builds one transaction (output 0 merchant, output 1 treasury, change last, payload `kasgate:v1:<nonce>`), signs it, adds the `payerProof`, and retries with `PAYMENT-SIGNATURE`. Expect HTTP 200 with the upstream body and a `PAYMENT-RESPONSE` header in roughly 2 to 4 seconds. The receipt carries the `txId`; check it at `https://explorer-tn10.kaspa.org/txs/<txid>`.

Then open **/dashboard**: the settlement appears with gross, merchant share, fee and USD value. The CSV tax export is `GET /v1/tax-report.csv`.

`KasGateSyncClient` has the same API for blocking code. If your upstream is slow or fails after payment, see the redelivery behaviour in [section 7](#7-production-checklist).

### 5.4 Pay it from an AI agent (MCP)

```bash
pip install -e packages/buyer-sdk -e packages/mcp-server
```

Add the server to your MCP client configuration:

```json
{"mcpServers": {"kasgate": {"command": "kasgate-mcp", "env": {"KASGATE_BUYER_PRIVATE_KEY": "<hex key of a funded payer>", "KASGATE_GATEWAY_URL": "http://localhost:8000", "KASGATE_NETWORK": "kaspa:testnet-10", "KASGATE_MAX_PRICE_KAS": "1"}}}}
```

Tools: `list_directory`, `search_catalog`, `inspect_endpoint`, `quote_endpoint(slug)` (pays nothing), `get_budget`, `call_with_budget` (session spend cap; reserves the quoted price before signing) and `call_paid_endpoint(slug, method, path, body, max_price_kas)`. `KASGATE_MAX_PRICE_KAS` (default 20) is a hard ceiling per call; the tool argument can only lower it. `KASGATE_SESSION_BUDGET_KAS` (default 20) caps total spend for `call_with_budget` in one MCP session. `KASGATE_RPC_URL` is optional (empty = public Resolver). Paid calls need the funded key. The MCP server reads the key from the environment only (there is no key-file option), so keep that config file private (mode 600), use a small dedicated agent wallet, and keep the cap low.

## 6. Test suites

### 6.1 Offline unit tests (no services, no funds)

```bash
# gateway: needs pgserver (embedded Postgres) or TEST_DATABASE_URL pointing at a Postgres server
cd apps/gateway
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
ruff check . && mypy --strict app

# buyer SDK and MCP server
cd ../../packages/buyer-sdk && pip install -e ".[test]" && pytest -q
cd ../mcp-server            && pip install -e ".[test]" && pytest -q

# web
cd ../../apps/web && npm ci && npm run typecheck && npm run lint && npm run build
```

### 6.2 Integration and red-team suite (`tests/integration`)

Run `pip` from inside `tests/integration`: its `requirements.txt` contains `-e ../../packages/buyer-sdk`, a path relative to the current directory.

```bash
cd tests/integration
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -e ../../packages/mcp-server
pytest -rs
```

Without a gateway the offline tests (`test_offline_*.py`) run and the live ones skip with a reason, which is expected. Markers (`pytest.ini`): `live` (needs a gateway), `funded` (spends testnet coins), `ratelimit` (excluded by default, `-m ratelimit` to run).

For the live tiers, start the stack with **`RATE_LIMIT_ENABLED=false`** in `infra/.env` (all host traffic reaches the gateway from one Docker bridge IP and the default limits would starve the suite), then `docker compose up -d` again.

| Variable | Meaning |
| --- | --- |
| `KASGATE_GATEWAY_URL` | Gateway base URL. Default `http://localhost:8000`. |
| `KASGATE_TEST_PAYER_KEY_FILE` | File with the funded payer's hex key. There is no default: set it explicitly (or use `KASGATE_TEST_PAYER_KEY`), otherwise the funded tests skip. The key is masked and never printed. |
| `KASGATE_TEST_PAYER_KEY` | The same key inline; wins over the file. Without either, funded tests skip. |
| `KASGATE_TEST_RPC_URL` | wRPC URL the test client uses for UTXO lookups. Default: public Resolver. |
| `KASGATE_TEST_UPSTREAM_URL` | Public upstream the gateway proxies to. Default `https://httpbin.org/anything`. |
| `KASGATE_TEST_FAILING_UPSTREAM_URL` | Upstream that answers 5xx or refuses. Default `https://httpbin.org/status/503`. |
| `KASGATE_TEST_PUBLIC_DOMAIN` | Expected challenge `Domain` when it differs from the netloc of `KASGATE_GATEWAY_URL`. |
| `KASGATE_TEST_TXLOG` | Optional JSONL file with one line per presented payment (txId, status, latency). |
| `KASGATE_TEST_SLOW=1` | Enables the test that waits for a challenge to expire (`payment_expired`), about 2 minutes. |
| `KASGATE_TEST_RPC_DOWN=1` | Only when the gateway runs with an unreachable node (`KASPA_RPC_URL=ws://127.0.0.1:1`): enables the 503 `rpc_unavailable` test. |
| `KASGATE_TEST_REDIS_DOWN=1` | Only when the gateway's Redis is stopped. |
| `KASGATE_RATE_LIMIT_ENABLED=true` | Mirror of the gateway's `RATE_LIMIT_ENABLED=true`; enables the `ratelimit` tests (see `tests/integration/README.md`). |

No-funds tier (tampered outputs, nonce and payload binding, front-running, the 16-address cap, catalog, auth) against a running gateway:

```bash
KASGATE_GATEWAY_URL=http://localhost:8000 pytest -rs
```

### 6.3 Funded end-to-end (testnet-10 only)

The suite refuses to run payments on mainnet. Requirements: a gateway with `kaspa_rpc:true` and `RATE_LIMIT_ENABLED=false`, and a funded, split payer key ([section 4](#4-testnet-10-wallet)). On hosts with synthetic DNS (for example a sandbox or test network where every hostname resolves to `198.18.0.1`) the SSRF guard correctly blocks hostname upstreams AFTER the payment has settled, which wastes coins. Use an IP-literal public upstream:

```bash
cd tests/integration
export KASGATE_TEST_PAYER_KEY_FILE=~/kg-secrets/buyer.key
export KASGATE_TEST_UPSTREAM_URL=https://1.1.1.1/cdn-cgi/trace
pytest -rs -m funded                 # real settlements: about 2.5 minutes, about 8.4 TKAS
pytest -rs                           # everything else too (add KASGATE_TEST_SLOW=1 for the expiry test)
```

Each funded payment at the minimum price moves 0.6 KAS (0.4 merchant, 0.2 treasury) plus about 0.0025 KAS network fee, paid to a throw-away merchant. `test_small_change_at_the_minimum_price...` needs a payer UTXO of about 1 KAS or less and skips otherwise; add one with the split script (`--count 1 --kas-each 1`) if you want it covered. Use `KASGATE_TEST_TXLOG=/tmp/kg-tx.jsonl` to keep a transaction trace.

The `ratelimit` tests need a separate gateway start with low limits and cannot share a run with the normal suite; follow `tests/integration/README.md`.

## 7. Production checklist

Do not go to mainnet until every box is ticked.

**Network and TLS**

- [ ] Terminate TLS in front of the gateway and the web app (nginx, Caddy or a load balancer) with valid certificates. `PUBLIC_BASE_URL` must be the `https://` gateway URL. The gateway refuses to start on `kaspa:mainnet` with an `http://` URL.
- [ ] Use separate hostnames, for example `https://pay.example.com` (gateway, `PUBLIC_BASE_URL`) and `https://app.example.com` (web, in `CORS_ORIGINS`). Rebuild the web image after changing `PUBLIC_BASE_URL`.
- [ ] **nginx must allow large request headers.** A `PAYMENT-SIGNATURE` header carries a signed transaction and can reach 40 KiB (the gateway accepts at most 40,960 bytes; nginx's default is 8 KiB per line and would answer 400 or 414 before KasGate sees it):

  ```nginx
  server {
      listen 443 ssl;
      server_name pay.example.com;
      # ssl_certificate / ssl_certificate_key ...

      large_client_header_buffers 4 64k;      # required for PAYMENT-SIGNATURE (up to 40 KiB)

      location / {
          proxy_pass http://127.0.0.1:8000;
          proxy_http_version 1.1;
          proxy_set_header Host $host;
          proxy_set_header X-Real-IP $remote_addr;
          proxy_set_header X-Forwarded-For $remote_addr;
          proxy_set_header X-Forwarded-Proto $scheme;
          proxy_read_timeout 90s;
      }
  }
  ```

  The gateway itself already runs uvicorn with `--h11-max-incomplete-event-size 65536`. Any other CDN or load balancer in the path needs an equivalent header limit of at least 64 KiB. Test it: a paid call through the proxy must return 200, not 400/431.
- [ ] Set `proxy_set_header X-Forwarded-For $remote_addr;` (overwrite, not append) so clients cannot forge their IP, and set **`FORWARDED_ALLOW_IPS`** to the address(es) of the proxy as the gateway container sees them. The gateway rate-limits on the connection's client IP, which uvicorn only rewrites from `X-Forwarded-For` when the peer is in `FORWARDED_ALLOW_IPS`. With nginx on the Docker host the peer is the compose network's gateway address, not `127.0.0.1`: find it with `docker network inspect kasgate_default` and allow that address or CIDR. If you skip this, all users share one bucket (`429 rate_limited` for everyone). Never use `*`.
- [ ] Do not publish what you do not need. `infra/docker-compose.yml` publishes web `3000` and gateway `8000` on all interfaces, and Postgres and Redis only on `127.0.0.1`. Firewall `3000` and `8000` from the internet so that only the reverse proxy (on the host) reaches them, or bind them to `127.0.0.1` with a compose override file.
- [ ] Browser cookies: keep `KASGATE_COOKIE_SECURE=true`.

**Secrets**

- [ ] `infra/.env` is mode 600, not in git (it is in `.gitignore`), and not world-readable on the host.
- [ ] Strong random `POSTGRES_PASSWORD` and `REDIS_PASSWORD`. Never keep the `change-me-...` placeholders.
- [ ] `ENCRYPTION_KEY` generated fresh for production and stored in a secrets manager as well as on the host. Losing it makes merchants' stored upstream headers unreadable; rotating it needs a re-encryption step that does not exist yet, so treat it as permanent.
- [ ] The treasury key never touches the server: only the address is configured. Keep that key offline or in a hardware wallet.
- [ ] No testnet or test key is reused on mainnet.

**Backups**

- [ ] Postgres is the system of record (merchants, endpoints, settlements). Schedule a daily dump and test a restore:

  ```bash
  cd infra
  docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > kasgate-$(date +%F).dump
  ```

  Also snapshot the `pgdata` volume or use managed Postgres with point-in-time recovery. Ship dumps off the host.
- [ ] Redis (AOF on, volume `redisdata`) holds challenges, replay locks, sessions and the dead-letter list. Losing it logs everyone out and expires pending challenges, but settled payments are in Postgres. Back it up if you want sessions to survive a rebuild.
- [ ] Back up `infra/.env` (encrypted) together with `ENCRYPTION_KEY`.

**Gateway settings**

- [ ] `RATE_LIMIT_ENABLED=true` and `RATE_LIMIT_FAIL_CLOSED=true` (the defaults). The funded-test configuration (`RATE_LIMIT_ENABLED=false`) must never reach production.
- [ ] `SETTLEMENT_MODE=accepted` (the default). `mempool` is for testnet demos only.
- [ ] Review `RL_*` values against expected traffic. All limits are per IP.
- [ ] Decide on the Kaspa connection. The current decision is **public nodes** (`KASPA_RPC_URL` empty). On testnet-10 about half the nodes the Resolver hands out never answered; the engine retries 3 times and works around it, but expect occasional `503 rpc_unavailable`. Moving to an own node is a one-line change later ([section 8](#8-optional-own-node)).

**The mainnet switch**

1. Complete [section 6](#6-test-suites) on testnet-10 against the exact production images.
2. Create a mainnet treasury address (`kaspa:q...`) you control offline.
3. In `infra/.env`: `KASPA_NETWORK=kaspa:mainnet`, `KASGATE_TREASURY_ADDRESS=kaspa:q...`, `PUBLIC_BASE_URL=https://pay.example.com`, `CORS_ORIGINS=["https://app.example.com"]`, fresh secrets. Leave `KASPA_RPC_URL` empty for public nodes.
4. `cd infra && docker compose up -d --build` (the web image must be rebuilt: `NEXT_PUBLIC_*` are build-time).
5. Use a **new database**. Do not reuse testnet merchants and settlements: addresses carry a network prefix and mixing them breaks the payout checks. Run `docker compose down -v` on a fresh host, or point `POSTGRES_DB` at a new database.
6. Check `/healthz` (`network: "kaspa:mainnet"`, all true), register a merchant with KasWare on mainnet, and make one real payment of 0.6 KAS from a small funded wallet; verify the split on `https://explorer.kaspa.org/txs/<txid>` (output 0 merchant, output 1 treasury). Mainnet coins have value: start with the minimum price and a low `max_price_kas` cap.
7. The integration suite will not spend on mainnet. Use the SDK or MCP server for that one manual check.

**Monitoring and redelivery**

- [ ] Poll `/healthz` and alert when the body says `"status":"degraded"` or any of `database`, `redis`, `kaspa_rpc` is false.
- [ ] Alert on gateway log lines at `ERROR`, in particular:
  - `was accepted by the node but its settlement could not be stored` followed by `unrecorded settlement pushed to kasgate:deadletter:settlements`: money moved on-chain but the database row is missing. The payload is in the Redis list; reconcile it by hand. Check the list length: `docker compose exec redis sh -c 'redis-cli -a "$REDIS_PASSWORD" --no-auth-warning LLEN kasgate:deadletter:settlements'` (should be 0).
  - `dead-letter queue unavailable` (the payload is only in the log line).
- [ ] Track the rate of `503 rpc_unavailable` and `429 rate_limited` at the proxy.
- [ ] **Redelivery.** KasGate settles on-chain first and calls the upstream second. If the upstream then fails (5xx or connection error), the buyer gets `502 upstream_error` **with** a `PAYMENT-RESPONSE`, the settlement stays `settled`, and **the payment is not refunded**. The buyer retries with the same `PAYMENT-SIGNATURE`: within `REDELIVERY_WINDOW_SECONDS` (600) and at most `REDELIVERY_MAX_ATTEMPTS` (3) deliveries, the gateway forwards the same request again without charging again (bound to the same payer, method and path). After the window or the limit, the answer is 409 and the money is simply spent. The SDK re-presents automatically for idempotent methods, `max_redeliveries` times. Non-idempotent calls (POST) are not retried automatically. Query the backlog of paid-but-undelivered calls:

```bash
cd infra
docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
SELECT tx_id, merchant_id, endpoint_id, upstream_status_code, delivery_attempts, settled_at
FROM settlements
WHERE status = 'settled' AND (upstream_status_code IS NULL OR upstream_status_code >= 500)
ORDER BY settled_at DESC LIMIT 50;
SQL
```

Rows older than the redelivery window that still show no 2xx result are payments a buyer paid for and never got. Alert when their count grows, contact the affected merchant about their upstream, and decide on a manual refund policy (the gateway has no refund function). Tell merchants this behaviour in your terms.
- [ ] **Prepaid refunds and expiry (CONTRACT v2.13).** A buyer's refund request only sets credit aside; the merchant claims it (`POST /v1/prepaid/refunds/{id}/claim`), pays it on-chain from their own wallet (exactly `amount_sompi` to `payout_address`, payload `payout_payload`, no platform fee) and posts the txId, or the signed transaction, to `POST /v1/prepaid/refunds/{id}/payout`. The ledger is debited only when that payout is accepted on-chain. Watch for refunds stuck in `payout_submitted`, attempts that stay `pending`, and requests merchants leave open:

```bash
cd infra
docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<'SQL'
SELECT id, merchant_id, status, amount_sompi, tx_id, last_error, created_at, payout_submitted_at
FROM prepaid_refunds
WHERE status IN ('requested', 'paying', 'payout_submitted')
ORDER BY created_at LIMIT 50;
SELECT p.refund_id, p.tx_id, p.method, p.payload_verified, p.check_count, p.error_count, p.last_error, p.created_at
FROM prepaid_refund_payouts p
WHERE p.status = 'pending'
ORDER BY p.created_at LIMIT 50;
SQL
```

  `payload_unverified` in `last_error` means the payout was accepted before the gateway ever saw its payload (it left the mempool before the txId was posted): ask the merchant to post the signed transaction JSON (`transaction`; KasWare `sendKaspa` returns it), which proves the payload. A growing `error_count` means node trouble (backoff, never a revert). After `PREPAID_PAYOUT_PENDING_MAX_SECONDS` a refund reverts to `requested` (`prepaid.refund.payout_reverted`) but its attempts are still re-checked until `PREPAID_PAYOUT_ATTEMPT_MAX_SECONDS`, so a late landing is recorded instead of being paid twice; the buyer cannot cancel meanwhile. Refunds with `late_paid_from` set were paid after a cancel or reject; `shortfall_sompi > 0` means the buyer had already spent that credit, which is between the merchant and the buyer. `duplicate` attempts mean the merchant paid twice. `scan` attempts were found by the gateway itself when a buyer tried to cancel a refund the merchant had claimed. Cancelled refunds with `hold_until` set still hold their amount until the ledger worker releases it; if `hold_until` is far in the past the worker is not running. Expiry runs in the ledger worker; keep `LEDGER_WORKER_ENABLED=true` on at least one replica. Tell merchants that an expiry policy applies only from a buyer's next deposit.
- [ ] Watch disk, memory and Postgres connections on the host. Redis uses AOF, so its volume grows.

### Migration 0008 revised in place

Revision `0008_prepaid_refunds` (CONTRACT v2.13) was revised in place twice before release (after 15d7bd4 and after 706855a) under the same revision id. A database migrated with an earlier draft is stamped `0008_prepaid_refunds` but lacks `prepaid_refund_payouts` and/or `prepaid_refunds.first_claimed_at`, `hold_until`. `alembic upgrade head` and the gateway's startup detect this and stop with a message instead of running on the wrong schema. To fix such a database (testnet/dev data only; nothing is deployed with the drafts):

```bash
cd infra
# The gateway container stops at start (its `alembic upgrade head` refuses), so run Alembic in a one-off container:
docker compose run --rm --entrypoint alembic gateway downgrade 0007_trials_webhooks   # works from every 0008 draft
docker compose run --rm --entrypoint alembic gateway upgrade head
docker compose up -d gateway
```

Outside Docker: `cd apps/gateway && alembic downgrade 0007_trials_webhooks && alembic upgrade head`. The downgrade refuses while refund or expiry data exists.

If the downgrade refuses because refunds, ledger rows or expiry settings exist, export what you need and rebuild the database from scratch (`alembic downgrade base` or drop and recreate it, then `alembic upgrade head`). Revision 0009 and later stack on `0008_prepaid_refunds` unchanged.

## 8. Optional own node

Public nodes are the current choice. Run your own `kaspad` later for reliability: set `KASPA_RPC_URL` to it and the gateway connects with a fixed wRPC Borsh URL instead of the Resolver.

* **Sizing (mainnet):** minimum **8 CPU cores, 16 GB RAM, 640 GB SSD** (local SSD or NVMe, not network storage); preferred 12 to 16 cores, 32 GB and 1 TB. A 4 vCPU / 12 GB host fails. Plan several hours for the initial sync, and treat the node as ready only when `isSynced` is true.
* **Compose profile:** `cd infra && docker compose --profile node up -d`, then set `KASPA_RPC_URL=ws://kaspad:17210` (testnet-10) or `ws://kaspad:17110` (mainnet) together with `KASPAD_TESTNET`, `KASPAD_P2P_PORT` and `KASPAD_RPC_BORSH_PORT` (values are in `infra/.env.example`). Keep `KASPA_RPC_URL` empty until `isSynced` is true.
* **One-shot server setup:** `infra/node/provision.sh` sets up a fresh Ubuntu 24.04 host (Docker, ufw, the `kasgate-kaspad.service` systemd unit): `sudo ./provision.sh --repo-dir /opt/kasgate --network mainnet`.
* Only the P2P port is published (TCP 16111 on mainnet, 16211 on testnet-10); the RPC port stays on the compose network. If a native testnet node already runs on the same host with default ports, the compose node's P2P port clashes with it.
* Full details (image digest, ports, firewall, sync check, upgrades, resync): [`infra/node/README.md`](../infra/node/README.md).

## 9. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Web shows `challenge_rejected` (or "challenge refused") when registering or logging in. | The challenge `Domain` differs from the web build's `NEXT_PUBLIC_GATEWAY_URL` netloc. Both come from `PUBLIC_BASE_URL`: the `Domain` is its lowercased netloc, so `localhost:8000` is not `127.0.0.1:8000` and `https://pay.example.com` is not `https://www.pay.example.com`. Fix: make `PUBLIC_BASE_URL` the exact URL you use, then `docker compose up -d --build web gateway`. When running the web outside compose, build it with `NEXT_PUBLIC_GATEWAY_URL` set to the same value and keep `GATEWAY_INTERNAL_URL` pointing at a reachable gateway (for example `http://127.0.0.1:8000`). |
| Gateway exits at startup with `KASGATE_TREASURY_ADDRESS must start with 'kaspatest:'` (or `kaspa:`). | Treasury address and `KASPA_NETWORK` disagree. (The `.env.example` placeholder does not stop startup, since only the prefix is checked, but it makes every unpaid call fail to build its 402 challenge: check the gateway log and replace the address.) |
| Gateway exits with `PUBLIC_BASE_URL must use https:// on kaspa:mainnet`. | Mainnet requires an HTTPS public URL. |
| After signing in you are bounced back to `/login` on a non-localhost plain-http host. | The session cookie is `Secure` and the browser drops it. Use HTTPS, or set `KASGATE_COOKIE_SECURE=false` for a plain-http test host only. |
| `503 rpc_unavailable` on a paid call; `/healthz` shows `"kaspa_rpc": false`. | The gateway has no live node connection, or the transaction was not accepted within `SETTLEMENT_TIMEOUT_SECONDS`. Nothing is lost: the replay lock is released and the same signature can be re-presented (it is not a 409). With the public Resolver, wait and retry; check `docker compose logs gateway` for connect attempts and egress rules for WebSocket traffic. With your own node, check it is synced (see `infra/node/README.md`) and that `KASPA_RPC_URL` is `ws://` plus the Borsh port. |
| Paid call returns `502 upstream_error` after the payment settled. | The upstream refused, timed out or failed. If the host has synthetic DNS (every name resolves to one fixed address), or the upstream resolves to a private/non-public IP, the SSRF guard blocks it (see next row). Otherwise fix the upstream and re-present the same `PAYMENT-SIGNATURE` within `REDELIVERY_WINDOW_SECONDS`. |
| Upstream is blocked by the SSRF guard (works in curl, fails through KasGate). | The gateway resolves the upstream host at request time and refuses any address that is not globally routable (private ranges, `100.64.0.0/10`, loopback, link-local, ULA, names like nip.io that resolve to them), and does not follow redirects. Use a public upstream or a public IP literal (for example `https://1.1.1.1/cdn-cgi/trace` for tests). A host whose DNS answers `198.18.0.1` for every name will fail every hostname upstream after payment. |
| SDK raises `TransactionBuildError` or `DustChangeError` ("InsufficientFundsError"). | The payer has no combination of UTXOs that pays price + network fee and leaves change of 0 or at least 0.3 KAS. Typical with one UTXO only slightly above the price, or two UTXOs that each almost cover it (for example two of 0.603 KAS at the 0.6 KAS price). Fix: fund more, or split the balance ([section 4.4](#44-split-the-balance-into-several-utxos-advisable)). The SDK fails closed rather than donate dust to the miner. |
| `402 invalid_payment_header` on a payment the SDK built. | The payer address is not P2PK Schnorr (`kaspatest:q...` / `kaspa:q...`), the `payerProof` is wrong, the payment draws from more than 16 payer addresses, or no input is spent from `payerAddress`. Use a plain Schnorr key. |
| `402 payment_expired` with a fresh `PAYMENT-REQUIRED`. | The nonce expired (`PAYMENT_MAX_AGE_SECONDS`, default 120 s) or was consumed. Nothing was spent; the SDK re-challenges once automatically. |
| `409 payment_replayed`. | That txId or nonce is already settled or locked. Redelivery is only allowed for the same payer, method and path, within the window, after an upstream failure. |
| `429 rate_limited` for everyone behind your proxy. | `FORWARDED_ALLOW_IPS` does not include the proxy's address, so all users share the proxy's IP ([section 7](#7-production-checklist)). |
| Browser or `curl` gets 400/431/414 only on paid calls through nginx. | `large_client_header_buffers 4 64k;` is missing ([section 7](#7-production-checklist)). |
| Funded tests all skip. | No key (`KASGATE_TEST_PAYER_KEY_FILE` or `KASGATE_TEST_PAYER_KEY`), or the gateway reports `kaspa_rpc:false`, or the gateway is not on testnet-10. Run `pytest -rs` to see each skip reason. |
| Funded tests fail with 502 right after settling. | Synthetic DNS: set `KASGATE_TEST_UPSTREAM_URL=https://1.1.1.1/cdn-cgi/trace`. |
| Many integration tests fail with 429. | The gateway runs with rate limits on. Set `RATE_LIMIT_ENABLED=false` for test runs (never in production). |
| `docker compose` complains `PUBLIC_BASE_URL is required`. | `infra/.env` is missing or you ran compose from a directory other than `infra/`. |

## 10. Verified results

Funded testnet-10 run (on the real Kaspa engine with the public Resolver, `SETTLEMENT_MODE=accepted`, rate limits off):

* **Funded tier (`-m funded`): 11 passed, 1 skipped** in about 155 s. The skipped test is the small-change probe, which needs a payer UTXO of about 1 KAS or less.
* **Full integration suite against the live gateway: 316 passed, 4 skipped, 3 deselected** (199 s), plus the slow challenge-expiry test (`payment_expired`, about 122 s) and the node-down test (`503 rpc_unavailable`, the lock released so a retry is not a 409).
* The tests covered the happy path, a settled txId reused (409), a settled nonce with a second transaction (409), 10 concurrent replays of one signature (exactly one 200 and nine 409), an unsigned or inflated transaction (402), a payer that owns no input (402), a front-runner against the victim (thief 402 in 0.03 s, victim 200), and an upstream failure followed by redelivery (`delivery_attempts` 1 to 2).
* Every payment split on-chain exactly as specified: 0.6 KAS = 0.4 KAS to the merchant (output 0) + 0.2 KAS to the treasury (output 1) + about 0.0025 KAS network fee, with the payload `kasgate:v1:<nonce>`. Settlement latency was about 1.1 to 3.0 s end to end.
* Spend: 8.435476 TKAS over 14 transactions.

Example transactions on the testnet-10 explorer:

* Happy-path payment from the funded run (`e710f434…be0673`): [`e710f43443383858728eab5331d10691c4e087754e880efd6b350a1b65be0673`](https://explorer-tn10.kaspa.org/txs/e710f43443383858728eab5331d10691c4e087754e880efd6b350a1b65be0673).
* Full-stack check through the website (register, create endpoint, paid call, dashboard, rotate key, logout): [`143037df56473da6be7dbf1ce3299cfb1eadf4a873e12ae61725abec55d94696`](https://explorer-tn10.kaspa.org/txs/143037df56473da6be7dbf1ce3299cfb1eadf4a873e12ae61725abec55d94696). Paid call returned 200 in 2.6 s, 0.4 KAS to the merchant and 0.2 KAS to the treasury; the dashboard, settlements, analytics and CSV all showed it as settled on the first poll.

Offline suites on the commit this runbook was written against:

| Suite | Result |
| --- | --- |
| Gateway (`apps/gateway`, `pytest -q`) | 430 passed; `ruff check .` clean; `mypy --strict app` clean |
| Buyer SDK (`packages/buyer-sdk`) | 210 passed |
| MCP server (`packages/mcp-server`) | 83 passed |
| Integration, offline tier (`tests/integration`, no gateway running) | 210 passed, 113 skipped (live tests), 3 deselected |

Verified on 2026-11-20: a merchant registration and a login signed by the real KasWare 0.11.6 Chrome extension on Testnet 10 (address kaspatest:qr347rhlq8u5qdx9m2yyvx9c242t44f2qwwtujtej38ufk6w9pdusnkmycha3), both accepted by the gateway. The mainnet switch ([section 7](#7-production-checklist)) has not been exercised.
