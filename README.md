# KasGate

**A zero-code x402 v2 pay-per-call gateway on Kaspa.** Put KasGate in front of any HTTP API and callers, including AI agents, pay per request in KAS. No SDK on the merchant side, no deploy, no custody.

> **Status: testnet-verified, mainnet not yet launched.** Everything below has been exercised on Kaspa testnet-10 with real transactions. KasGate has not had an independent security audit. Do not use it with mainnet funds yet.

A public testnet-10 beta is being prepared. Testers should start at `/beta` on the site (KasWare, the official TN-10 faucet, registering an API, the buyer SDK, and how to report bugs) and follow [docs/beta/TEST_PLAN.md](docs/beta/TEST_PLAN.md). Operators bringing up the public stack should follow [docs/DEPLOY.md](docs/DEPLOY.md).

## How it works

1. A merchant signs in with a Kaspa wallet (KasWare), registers an upstream API URL and sets a price in KAS or USD. USD prices are converted to KAS at challenge time from a fail-closed multi-source oracle, and the sompi amount is locked so it cannot change between the 402 and payment.
2. KasGate publishes a paywalled URL, `https://<gateway>/p/<slug>`. A request without payment gets **HTTP 402** and a `PAYMENT-REQUIRED` header describing the price, a one-time nonce and where the money goes.
3. The buyer (or its agent) builds and signs **one Kaspa transaction** and retries with a `PAYMENT-SIGNATURE` header.
4. The gateway verifies it, submits it to the Kaspa network, waits until it is accepted, then forwards the request to the merchant's upstream and returns the response with a `PAYMENT-RESPONSE` receipt.

The scheme is `kaspa-exact-v1`. The full wire format, error codes and routes are in [docs/CONTRACTS.md](docs/CONTRACTS.md).

Listed endpoints are also published as a public, machine-readable catalog at `GET /.well-known/x402` and `GET /v1/catalog` on the gateway (and as `GET /llms.txt` for agents). The buyer SDK can fetch it with `KasGateClient.discover()`, and the MCP server can search it and pay under a session spend cap. The catalog never includes API keys, emails, upstream URLs, or merchant settlement addresses.

Merchants can import a paid endpoint per operation from an OpenAPI 3.0 or 3.1 document (JSON or YAML) in the dashboard, via `POST /v1/endpoints/import`. Preview writes nothing; create is all-or-nothing. The contract is in [docs/CONTRACTS.md](docs/CONTRACTS.md) (v2.8) and [docs/OPENAPI_IMPORT.md](docs/OPENAPI_IMPORT.md).

### k402 wallets and agents work too

Every 402 is also a [k402](https://github.com/kaspa-402/k402) offer body (`{"k402": "0.2", "accepts": [...]}` with a `kaspa-utxo` offer), so an unmodified k402 client (`k402.Client` with a `kaspa-utxo` payer such as `HotWallet`) can pay a KasGate endpoint with no KasGate SDK. It pays the offer itself and retries with `X-K402-Payment: kaspa-utxo <txid> <payment_id>`.

**The platform fee still applies on this path.** The offer quotes the merchant share as `amount_sompi` and the KasGate fee as `facilitator_fee`, which the official wallet pays as output 1. The gateway looks the transaction up on its own node and serves the call only if output 0 pays the merchant exactly and output 1 pays the treasury exactly (checked by index and by address). A missing, short, swapped or redirected fee output is refused. **Anti-front-running amount tag:** each k402 offer adds a random tag of under 0.01 KAS (1,000,000 sompi) to the merchant share. k402 transactions carry no nonce or payer proof, so the exact amounts are what tie a transaction to one offer; the tag makes another caller's offer for the same endpoint very unlikely to match your transaction. For USD-priced endpoints the offer uses the same locked KAS quote as the `PAYMENT-REQUIRED` challenge, and if the KAS/USD price is unavailable the gateway answers `503 price_unavailable` with no offer at all. Details are in CONTRACT v2.9 of [docs/CONTRACTS.md](docs/CONTRACTS.md). Only `kaspa-utxo` is supported; `kaspa-session` and `kaspa-channel` are not.

Known upstream issue: with `kaspa==2.1.0`, `k402.wallet.HotWallet(..., network="testnet-10")` fails in its constructor ("Invalid network type"). Its `pay()` and `k402.Client` work, and mainnet is unaffected. Testnet-10 testers need a payer that builds the same transaction (see the k402 scenario in [docs/beta/TEST_PLAN.md](docs/beta/TEST_PLAN.md)).

### Works with kaspa-x402 wallets and agents

Every 402 also offers the community [kaspa-x402](https://github.com/elldeeone/kaspa-x402) `exact` payment (binding `kaspa-exact-v2`, profile `standard-native`). The `PAYMENT-REQUIRED` header is a standard x402 v2 envelope: the `exact` entry first, then KasGate's own `kaspa-exact-v1` terms. So an unmodified `@kaspa-x402/client` (1.0.1) can pay a KasGate endpoint with no KasGate SDK. It signs one transaction that pays the merchant, binds it to the exact request (method, URL, body) with a signed authorization, and retries with `PAYMENT-SIGNATURE`. The gateway verifies the payment the same way the official verifier does, waits until the network accepts it, and returns the standard `SettlementResponse` in `PAYMENT-RESPONSE`.

**No platform fee on kaspa-x402 payments during the beta.** On this path the merchant receives the full price and there is no treasury output. KasGate and k402 payments are unchanged and still carry the platform fee. USD-priced endpoints offer the same locked KAS amount on every path, and if the KAS/USD price is unavailable the gateway answers `503 price_unavailable` with no offer at all. Repeating a payment with the same payment identifier is idempotent. Two KasGate policies apply on this path: the merchant output must be at least 0.2 KAS, and signatures must use SIGHASH_ALL. Details are in CONTRACT v2.10 of [docs/CONTRACTS.md](docs/CONTRACTS.md). Batch settlement and the additive and hash-chain exact profiles are not supported.

The KasGate buyer SDK reads the new envelope. Copies of the SDK from before CONTRACT v2.10 reject the envelope header and need to be updated.

### Prepaid credit for micro-priced calls

A buyer can deposit once and then pay many calls without a transaction each (CONTRACT v2.11, scheme `kasgate-prepaid-v1`). The deposit is an ordinary KasGate payment of at least 0.6 KAS to the merchant: output 0 pays the merchant, output 1 pays the platform fee (`max(1.5%, 0.2 KAS)`), and the merchant share becomes credit for that buyer key at that merchant. Each later call asks `POST /v1/prepaid/challenge` for a one-time challenge bound to the exact method, path, query and body hash, signs it with the depositing key and sends it as `X-KasGate-Prepaid`. The gateway debits the balance atomically (it can never go negative, and a replayed signature debits at most once) and returns an `X-KasGate-Prepaid-Receipt`. If the upstream gives no response or a 5xx, the debit is refunded.

Merchants can mark an endpoint **prepaid only** and price it below the 0.6 KAS on-chain minimum (down to 1 sompi); its 402 then offers only the prepaid terms. Every other endpoint accepts both per-call payments and prepaid debits. KasGate still holds no coins: the deposit already paid the merchant on-chain, and unused credit is something the merchant owes in calls. The buyer SDK (`prepaid_deposit`, `prepaid_request`, `prepaid_balance`) and the MCP server (`prepaid_deposit`, `call_prepaid_endpoint`, `prepaid_balance`) support it, and the dashboard shows balances, deposits and debits.

**Refunds and expiry (CONTRACT v2.13).** A buyer can ask for unused credit back with a signed request (`POST /v1/prepaid/refunds/challenge`, then `POST /v1/prepaid/refunds`; SDK `prepaid_refund_request`, MCP `prepaid_refund_request`). The amount is set aside at once. The merchant pays it on-chain from their own wallet, exactly the refund amount to the depositing address with no platform fee, and posts the txId (or a signed transaction) to `POST /v1/prepaid/refunds/{id}/payout`; the gateway debits the ledger only once it sees that payout, and records it in `GET /v1/prepaid/ledger`. Merchants can also make credit expire after N days (`PATCH /v1/merchants/me`, or per endpoint with `prepaid_expiry_days`); a policy applies from a buyer's next deposit, never to credit already promised. Webhooks: `prepaid.refund.requested|paid|rejected|cancelled` and `prepaid.credit.expired`. Details in [docs/CONTRACTS.md](docs/CONTRACTS.md#contract-v213--prepaid-refunds-and-credit-expiry-additive-supersedes-there-is-no-withdrawal-route-in-v211).

### Free trial calls and merchant webhooks

Merchants can give each buyer wallet a few free calls per endpoint (CONTRACT v2.12, scheme `kasgate-trial-v1`, 0 to 100 calls, plus a daily cap across all buyers). The buyer proves the wallet with a signed, single-use challenge bound to the exact request; the counters are enforced atomically in Postgres, so concurrent calls can never exceed the trial. Trial calls move no coins and carry no fee. Once the trial is used up the gateway answers with its normal 402 and the call is paid as usual. The buyer SDK and the MCP paying tools use an offered trial automatically before paying.

Merchants can also register HTTPS webhooks for `payment.settled`, `prepaid.deposit`, `prepaid.debit`, `trial.used`, `endpoint.created` and `endpoint.updated`. Deliveries are HMAC-SHA256 signed with a per-webhook secret (shown once, stored encrypted) and a timestamp for replay protection, written to a durable outbox in the same transaction as the payment, retried with exponential backoff, dead-lettered after 8 attempts and redeliverable from the dashboard. Webhook targets go through the same SSRF guard as upstream APIs, and a failing webhook can never slow down or fail a payment.

### Non-custodial by construction

Funds never pass through KasGate. Every payment is a single on-chain transaction signed by the buyer:

| Output | Receives | Amount |
| --- | --- | --- |
| 0 | The merchant's own wallet | The price minus the platform fee |
| 1 | The platform treasury | `max(1.5%, 0.2 KAS)` of the price (also on k402 payments) |
| 2+ | Change back to the buyer | Only change is allowed here |

The fee is `max(ceil(price x 150 / 10,000), 0.2 KAS)`. The minimum endpoint price is 0.6 KAS, so that both outputs stay above Kaspa's storage-mass limit. At the minimum price a payment is 0.4 KAS to the merchant and 0.2 KAS to the treasury. KasGate holds no private keys.

## Architecture

| Component | Path | Stack | Role |
| --- | --- | --- | --- |
| Gateway | [`apps/gateway`](apps/gateway) | Python 3.12, FastAPI, PostgreSQL 16, Redis 7 | The 402 paywall proxy, payment verification and settlement, merchant API, price oracle |
| Web | [`apps/web`](apps/web) | Next.js 15, TypeScript, Tailwind | Landing page, wallet sign-up and login, dashboard, settlements, analytics, tax CSV, public directory |
| Buyer SDK | [`packages/buyer-sdk`](packages/buyer-sdk) | Python, httpx | `KasGateClient`: discovers the public catalog and pays 402 challenges automatically, with a client-side spend cap |
| MCP server | [`packages/mcp-server`](packages/mcp-server) | Python, MCP over stdio | Lets an AI agent search the catalog, inspect an endpoint, and pay for calls under a per-call maximum and a session spend cap |
| Integration tests | [`tests/integration`](tests/integration) | pytest | Offline, live and funded red-team suites against a running gateway |
| Infra | [`infra`](infra) | Docker Compose | Postgres, Redis, gateway, web, and an optional own `kaspad` node |

The gateway talks to Kaspa through public nodes (the wRPC Resolver) by default. You can point it at your own node with `KASPA_RPC_URL`.

## Quickstart

The full step-by-step guide is **[docs/RUNBOOK.md](docs/RUNBOOK.md)**: configuring `infra/.env`, starting the stack with Docker Compose, funding a testnet-10 wallet, registering a merchant, creating an endpoint, paying it with the SDK or the MCP server, running every test suite, and the production checklist. Public testnet-beta hosting (VPS, Cloudflare Tunnel, go-live checklist) is in **[docs/DEPLOY.md](docs/DEPLOY.md)**.

In short:

```bash
cp infra/.env.example infra/.env     # then set ENCRYPTION_KEY and the passwords, see the runbook
cd infra && docker compose up -d --build
```

Offline tests (no funds, no network access to Kaspa needed):

```bash
cd apps/gateway && pip install -r requirements-dev.txt && pytest -q && ruff check . && mypy --strict app
pip install -e "packages/buyer-sdk[test]" -e "packages/mcp-server[test]"
(cd packages/buyer-sdk && pytest -q) && (cd packages/mcp-server && pytest -q)
cd apps/web && npm ci && npm run typecheck && npm run lint && npm run build
```

## Security model

Details and rationale are in [docs/CONTRACTS.md](docs/CONTRACTS.md) (the `CONTRACT v2.x` sections).

- **Nonce payload binding.** Each 402 carries a one-time nonce that the gateway stores. The signed transaction must carry the payload `kasgate:v1:<nonce>`. The gateway verifies against the challenge it issued, never one the client sends, and consumes the nonce only after settlement.
- **Payer proof (`payerProof`).** The buyer also signs `kasgate:v1:<nonce>:<txId>` with the key of the paying address, and at least one input must be spent from that address. Someone who only sees the transaction in the mempool cannot present it as their own, which closes front-running.
- **Replay locks.** A Redis lock per transaction id and per nonce, plus a unique database constraint on the nonce, so a payment settles once. Concurrent replays of one signature give exactly one success and `409` for the rest.
- **Exact amounts and outputs.** The gateway checks the outputs, the fee split, the network and the input values it reads from the node itself, not the values the client claims.
- **SSRF guard.** Merchant upstreams must resolve to global IP addresses. DNS is resolved at request time and pinned for the connection, private and loopback ranges are refused, and redirects are not followed.
- **Wallet-proof auth.** Registration and login need a signed, single-use, domain-bound challenge from the merchant's wallet. API keys are shown once and stored only as a hash. Upstream secret headers are encrypted at rest.
- **Rate limits and fail-closed behaviour.** Per-IP limits on sign-up, auth and unpaid requests, and a `503` rather than a silent pass if Redis is down.

Known behaviour you should read before running it: settlement happens on-chain *before* the upstream is called. If the upstream then fails, the buyer retries with the same `PAYMENT-SIGNATURE` and the gateway redelivers without charging again, within a limited window. If the last delivery still fails, the merchant share becomes prepaid credit for that buyer at that merchant (failed-delivery auto-credit, merchants can opt out; nothing is refunded on-chain), see [docs/FAILED_DELIVERY_CREDITS.md](docs/FAILED_DELIVERY_CREDITS.md). See the production checklist in the runbook.

To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Verified testnet results

Run on Kaspa testnet-10 with the real engine, public nodes and `SETTLEMENT_MODE=accepted`. Details are in [section 10 of the runbook](docs/RUNBOOK.md#10-verified-results).

- **Funded end-to-end tier: 11 passed, 1 skipped**, covering the happy path, replays, tampered transactions, a front-runner and an upstream failure with redelivery. Full integration suite against the live gateway: 316 passed, 4 skipped.
- Every payment split on-chain exactly as specified: 0.4 KAS to the merchant (output 0), 0.2 KAS to the treasury (output 1), with the payload `kasgate:v1:<nonce>`. Settlement took about 1 to 3 seconds.
- Full website flow (wallet sign-up, endpoint creation, paid call, dashboard, key rotation, logout) and a sign-up and login with the real KasWare extension.

Example transactions on the testnet-10 explorer:

- Funded-run happy path: [`e710f434…be0673`](https://explorer-tn10.kaspa.org/txs/e710f43443383858728eab5331d10691c4e087754e880efd6b350a1b65be0673)
- Full-stack run through the website: [`143037df…d94696`](https://explorer-tn10.kaspa.org/txs/143037df56473da6be7dbf1ce3299cfb1eadf4a873e12ae61725abec55d94696)

Offline suites (no network, no funds): gateway 614 tests (the k402 end-to-end test needs the k402 checkout, see `K402_SRC`), buyer SDK 217, MCP server 90, integration offline tier 210. They run in CI on every push and pull request.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Please read [SECURITY.md](SECURITY.md) before reporting anything security related.

## License

[MIT](LICENSE) © 2026 KasGate contributors
