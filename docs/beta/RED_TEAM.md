# KasGate internal red-team checklist and mainnet gate

This is the internal plan for running adversarial checks against the **deployed** public testnet-10 beta, and the pass/fail bar for promoting that beta to mainnet.

It complements [TEST_PLAN.md](TEST_PLAN.md), which is the external tester script (happy path, underpay, wrong recipient, replayed `txId`, expired nonce, minimum price 0.6 KAS, and the output-0 merchant / output-1 treasury split). Those seven cases remain required. This file covers what we do not ask outside testers to attempt: `payerProof` forgery, header fuzzing, rate-limit and forwarded-IP behaviour, SSRF through the upstream URL, and a few related settlement and auth cases.

Do not run this against a local gateway with `RATE_LIMIT_ENABLED=false` and call it a live-beta result. Use a dedicated throw-away merchant and payer. Prefer the existing forges in `tests/integration` (`txforge.py`, `test_live_payments.py`, `test_offline_frontrun.py`, `test_zz_rate_limits.py`) pointed at the beta `$GATEWAY`; otherwise build a valid `PAYMENT-SIGNATURE` with the buyer SDK and mutate only the field under test.

**Never paste seed phrases, private keys, API keys (`kg_…`, `kgs_…`), or `.env` contents into issues, chat, or logs.** A `txId` is safe to share. Security findings go to private vulnerability reporting, not a public issue (see [SECURITY.md](../../SECURITY.md)). Do not target third-party infrastructure; only the beta and hosts we control.

Record for every row: HTTP status, JSON `error` code, whether a `txId` was broadcast, whether a settlement row appeared, and whether merchant or treasury balances changed. A row **fails** if a payment settles when it should have been rejected, if a rejected header consumes a nonce that should remain usable, or if an internal address is contacted.

Replace `$GATEWAY` and `$WEB` with the published beta origins. Challenges expire after `PAYMENT_MAX_AGE_SECONDS` (default 120); use the `expiresAt` on the challenge you actually received.

---

## 1. External tester plan must already pass

Before starting section 2, confirm the seven scenarios in [TEST_PLAN.md](TEST_PLAN.md) succeed on the deployed URLs:

| TEST_PLAN.md | Expected on the live beta |
| --- | --- |
| 1. Happy path: pay, then retry | Unpaid call is 402 `payment_required`; paid retry is 200 (or the upstream status) with `PAYMENT-RESPONSE` `settled: true`. A 502 `upstream_error` that still carries `PAYMENT-RESPONSE` can be re-presented with the same signature inside the redelivery window (default 600 s, max 3 deliveries) without a second on-chain payment; after the window or the limit, 409. |
| 2. Underpay | 402 `underpaid` or `invalid_fee_split`, fresh challenge, call not settled. |
| 3. Wrong recipient | 402 `invalid_fee_split` or `invalid_payment_header`, call not settled. |
| 4. Replayed `txId` | 409 `payment_replayed`, no second credit. |
| 5. Expired nonce | 402 `payment_expired` with a fresh `PAYMENT-REQUIRED`. |
| 6. Minimum price 0.6 KAS | Below 0.6 KAS cannot be saved; a 0.6 KAS payment settles as 0.4 KAS merchant / 0.2 KAS treasury. |
| 7. Fee split on the explorer | Output 0 = `payTo` exact merchant amount; output 1 = `feeTo` exact fee; change is omitted or at least 0.3 KAS. Dashboard matches the `txId`. |

If any of those fail on the live beta, stop and fix them first.

---

## 2. Live-beta red-team checklist

### 2.1 `payerProof` forgery

All of these must return **402** `invalid_payment_header` (or a schema 402) and must not settle. A later honest payment from the victim wallet must still work (or be blocked only by a normal replay if that wallet already settled the nonce).

| # | Action | Expected (pass) |
| --- | --- | --- |
| P1 | Omit `payerProof`. | Rejected by the schema / 402. |
| P2 | Proof over the wrong nonce, or over the wrong `txId`. | 402. |
| P3 | Flip one bit of a valid proof, or send all-zero, truncated, uppercase, or non-hex proofs. | 402; malformed proofs must not reach a successful engine path. |
| P4 | Reuse a proof from an earlier settled payment on a new `txId` or nonce. | 402. |
| P5 | Front-runner presents a victim's signed `rawTx` with the front-runner's address and the front-runner's proof. | 402. |
| P6 | Front-runner claims the victim `payerAddress` but signs `payerProof` with a different key. | 402. |
| P7 | Swap outputs so the front-runner is paid, while keeping the victim's proof. | 402. |
| P8 | Rebind a victim transaction to a different challenge nonce. | 402. |
| P9 | ECDSA or script-hash payer address (`kaspatest:` but not the P2PK `q` form). | 402 `invalid_payment_header`. |
| P10 | Change `txId` in the header after the proof was made. | 402. |

### 2.2 Header fuzzing

| # | Action | Expected (pass) |
| --- | --- | --- |
| H1 | Truncated, non-Base64, or wrong-alphabet `PAYMENT-SIGNATURE` / `PAYMENT-REQUIRED` values. | 402 `invalid_payment_header`. The gateway must not 500. |
| H2 | Garbage `rawTx`, empty JSON, or a `txId` that does not match the transaction. | 402; no settlement. |
| H3 | `txId` or addresses with a `kaspa:` mainnet prefix while the beta is `kaspa:testnet-10`. | 402 `wrong_network` or `invalid_payment_header`. |
| H4 | Header larger than 40,960 bytes, or `rawTx` one character over the documented limit. | Rejected (402 or 431). Through the public reverse proxy, a *legal* max-size signature must still be accepted: a real paid call must return 200, not nginx 400/414/431. If legal payments fail only through the proxy, `large_client_header_buffers` are too small (need 64 KiB). |
| H5 | Unsigned transaction, or an input whose embedded UTXO amount has been inflated. | Must not settle. The live node path (not only the offline verifier) has to reject these; `SETTLEMENT_MODE` on beta must be `accepted`. |
| H6 | Payer address that owns no input of the transaction. | 402. |
| H7 | Payment drawing on 17 distinct input addresses (16 is the maximum). | Rejected before the node. 16 addresses is accepted if funds allow. |
| H8 | Header `nonce` differs from the bytes in the signed transaction payload; or the transaction has no payload / a payload other than `kasgate:v1:<nonce>`. | 402 `invalid_payment_header`. The issued nonce remains usable for a correct retry. |
| H9 | Pay endpoint B with a nonce issued for endpoint A. | 402 `invalid_payment_header` and a fresh challenge for B. A is unaffected. |

### 2.3 Rate limits

The public beta must run with `RATE_LIMIT_ENABLED=true` and `RATE_LIMIT_FAIL_CLOSED=true`. Limits are per caller IP. Defaults:

| Bucket | Default | Where |
| --- | --- | --- |
| Auth challenges | 10 per minute | `POST /v1/auth/challenge` |
| Auth verify | 10 per minute | `POST /v1/auth/verify` |
| Merchant registration | 5 per hour | `POST /v1/merchants` |
| Unpaid proxy / quote | 300 per minute | `/p/*` and `/v1/quote/*` (anything that mints a challenge) |

| # | Action | Expected (pass) |
| --- | --- | --- |
| L1 | Exceed each bucket from one IP. | HTTP **429**, `error` is `rate_limited`. A later request in a new window succeeds. |
| L2 | Send a client-supplied `X-Forwarded-For` (or similar) that is not from the trusted proxy. | Must not open a fresh bucket. Forged headers must not bypass L1. |
| L3 | Two real clients behind the public HTTPS proxy (different public IPs). | They must not share one bucket. If every user is 429 after one client's burst, `FORWARDED_ALLOW_IPS` is wrong (all traffic is attributed to the proxy). |
| L4 | A legal paid call after a challenge has been issued is not blocked by the unpaid-proxy bucket under normal load. | The payment still settles. The unpaid limit applies to challenge minting, not to presenting a valid signature. |
| L5 | Redis down (only if 01 approves a staged check). | **503**, not open access. Otherwise confirm the deployed setting is fail-closed and do not take production Redis down. |

### 2.4 SSRF via the upstream URL

Only use addresses and names we control, plus obvious reserved literals. Do not scan or probe third-party clouds.

| # | Action | Expected (pass) |
| --- | --- | --- |
| S1 | Create or update an endpoint with `upstream_url` set to `http://127.0.0.1/`, `http://localhost/`, `http://[::1]/`, `http://10.0.0.1/`, `http://192.168.0.1/`, `http://169.254.169.254/`, `http://100.64.0.1/`, or a `*.localhost` / `*.internal` host. | Rejected at save time. The endpoint is not stored with that URL. |
| S2 | Save a public hostname we control, point its DNS at a loopback or RFC1918 address, and make a paid call. | The gateway re-resolves at request time and must not open a connection to the private address. Settlement happens before the upstream call, so the buyer may already have paid: expect `upstream_error` / failed delivery, never a response body fetched from the internal target. A leaked internal response is a **fail**. |
| S3 | Upstream that redirects to a private URL. | The gateway must not follow the redirect to the private target. |
| S4 | Control: a public IP literal such as `https://1.1.1.1/cdn-cgi/trace` (or another agreed public test URL). | After payment, the upstream body is returned and `PAYMENT-RESPONSE` is set. |

### 2.5 Extra cases (beyond TEST_PLAN.md)

| # | Action | Expected (pass) |
| --- | --- | --- |
| X1 | Concurrent replay of one settled `PAYMENT-SIGNATURE` (at least 10 in-flight requests). | Exactly one request settles (one 200 or one successful redelivery). All others are 409 `payment_replayed`. The merchant is credited once. |
| X2 | After a successful payment, build a *second* transaction for the same nonce and submit it. | Rejected (402 `payment_expired` or 409). The first settlement remains the only credit. |
| X3 | Submit a valid-looking signature whose nonce was never issued. | 402 `payment_expired` with a fresh challenge. |
| X4 | Fail a payment (underpay or bad header) and retry the same issued nonce with a correct payment before `expiresAt`. | The nonce is still usable; the correct payment settles. The bad attempt does not remain as a settlement and does not poison the replay lock. |
| X5 | Overpay the treasury output, or pay a 0.2 + 0.2 KAS (20,000,000 / 20,000,000) split on a 0.6 KAS challenge. | 402 `invalid_fee_split` (or `underpaid`). The v2.3 minimum requires merchant output at least 0.4 KAS. |
| X6 | Swap output 0 and output 1 with otherwise exact amounts; or send output 1 to a different treasury; or send output 0 to a third party. | 402 `invalid_fee_split`. Covered in spirit by TEST_PLAN.md “wrong recipient”; this row is the explicit output-order matrix. |
| X7 | Dust change (below 0.3 KAS) at the minimum price. | Must not settle as a standard transaction. The SDK must refuse (`DustChangeError`) rather than emit dust. |
| X8 | Honest payment at a price above 13.33333333 KAS, where the fee is 1.5% (ceil) rather than the 0.2 KAS floor. Skip mid-beta if funds are short; required before mainnet. | Output 0 / output 1 match `compute_fee_split` exactly. |
| X9 | Paid call whose upstream then returns 5xx. | Response is 502 `upstream_error` **with** `PAYMENT-RESPONSE`. The same signature re-forwards without a second on-chain payment inside the window; after the window or the attempt limit it is 409. |
| X10 | Register and login challenges are single-use; replay a used or expired challenge. | Refused with the generic auth error. |
| X11 | `rotate-key` then use the old `kg_` key and an old `kgs_` session. | Both are invalid. A new key and a new session are issued. |
| X12 | `DELETE /v1/auth/session`. | 204 for a `kgs_` session; 400 for a `kg_` key; 401 for a repeat or unknown token. |
| X13 | `GET $GATEWAY/healthz` during the test window. | `network` is `kaspa:testnet-10`; `database`, `redis`, and `kaspa_rpc` are true. |
| X14 | Review gateway and web logs from the test window. | No private keys, `kg_` / `kgs_` tokens, or upstream header values. |

---

## 3. Pass/fail criteria for promoting the beta to mainnet

Do not switch `KASPA_NETWORK` to `kaspa:mainnet` until every **blocker** below is a pass. The operational boxes in [docs/RUNBOOK.md](../RUNBOOK.md) section 7 (TLS, nginx 64 KiB headers, `FORWARDED_ALLOW_IPS`, secrets, backups, monitoring, fresh database, offline treasury key) must also all be ticked. This section is the QA gate on top of that checklist.

### Blockers (any fail holds the release)

| Gate | Pass | Fail |
| --- | --- | --- |
| External happy path | All seven scenarios in [TEST_PLAN.md](TEST_PLAN.md) succeed on the deployed beta URLs, including the 0.4 / 0.2 KAS split on the testnet-10 explorer and in the merchant dashboard. | Registration, payment, receipt, or fee split is wrong or flaky. |
| Live red-team | Every row in sections 2.1–2.5 passes. In particular: no forged `payerProof`, underpay, swapped outputs, wrong recipient, nonce reuse, or replay can settle; SSRF never reaches a non-public address; rate limits are on and not bypassable with `X-Forwarded-For`. | Any settlement of a payment that should have been rejected, any SSRF hit on a private target, or rate limits off, shared, or forgeable. |
| Automated suites | Offline unit tests (gateway, buyer SDK, MCP server, web lint/typecheck/build) and the integration suite — including funded testnet-10 payments and the `ratelimit` tests — are green against the **exact images** intended for mainnet. | Any failing payment, auth, or security test on those images. |
| Settlement mode | `SETTLEMENT_MODE=accepted` (not `mempool`). Live check H5 (unsigned transaction, inflated UTXO) does not settle. | Mempool mode, or a forged or unsigned payment that settles. |
| Rate limits in production config | `RATE_LIMIT_ENABLED=true`, `RATE_LIMIT_FAIL_CLOSED=true`. The funded-test setting `RATE_LIMIT_ENABLED=false` is not in the mainnet env. | Limits disabled, or fail-open on Redis loss, in the mainnet config. |
| Public proxy header cap | A legal paid call through the public HTTPS proxy returns 200, not 400/414/431. | The proxy rejects legal `PAYMENT-SIGNATURE` headers. |
| Forwarded IP | Distinct clients have distinct rate-limit buckets; clients cannot forge `X-Forwarded-For`. `FORWARDED_ALLOW_IPS` is the proxy, never `*`. | A single shared bucket, or a forged-header bypass. |
| Secrets and key hygiene | No testnet private keys, API keys, or `.env` values in the repo, image layers, or logs. Mainnet treasury is a new `kaspa:q…` address; the treasury key never touches the server. Mainnet merchant and payer keys are new. | Any leaked secret, or a testnet key, treasury, or database reused on mainnet. |
| Data isolation | Mainnet uses a **new** database. Testnet merchants and settlements are not migrated (address prefixes would break payout checks). | Testnet data carried into mainnet. |
| TLS and URL | `PUBLIC_BASE_URL` is `https://…`; the process refuses to start on mainnet with `http://`. Web `NEXT_PUBLIC_*` matches that URL (image rebuilt). Cookies have `Secure`. | HTTP on mainnet, host mismatch, or mixed localhost / public hosts. |
| Open vulnerabilities | No open critical or high issues in private advisories or beta bug reports that affect payment verification, settlement, auth, SSRF, or secret handling. | Any such issue still open. |
| Independent review | An independent security audit has been completed and its findings on payment verification, replay, SSRF and auth have been fixed or explicitly accepted in writing by Kyle. | Shipping mainnet on testnet-only review with no audit and no written risk acceptance. |
| Soak | After the red-team pass, the public testnet beta has run with real testers and no new payment-verification bypass. `/healthz` stays healthy except for brief, understood `kaspa_rpc` blips; the settlement dead-letter list is empty (or every item reconciled). Chronic `503 rpc_unavailable` is solved or an own node is in place. | Unexplained lost settlements, a growing dead-letter queue, or a new bypass found in soak. |

### Required first mainnet transaction (after the blockers pass)

1. Health check shows `network: "kaspa:mainnet"` and all dependencies true.
2. Register one merchant with KasWare on mainnet.
3. Charge exactly 0.6 KAS from a small, newly funded wallet with a tight `max_price_kas` cap.
4. Confirm on `https://explorer.kaspa.org/txs/<txid>` that output 0 is the merchant (0.4 KAS) and output 1 is the mainnet treasury (0.2 KAS).
5. Confirm the dashboard, settlement record, analytics and tax CSV show that `txId`.

The integration suite will not spend on mainnet; this check is manual. Stop and roll back if the split, receipt or dashboard is wrong. There is no on-chain refund in the gateway.

### Residual risks that are acceptable only if the blockers pass

The offline verifier does not, by itself, fully check signature scripts or catch every inflated embedded UTXO or unsigned transaction; those cases are supposed to fail when the node accepts or rejects the transaction. That is acceptable on mainnet only with `SETTLEMENT_MODE=accepted` and with H5 passing on the live stack. Public-node RPC blips (occasional `503 rpc_unavailable`) are acceptable if they do not leak a nonce, do not double-settle, and the same valid signature can be retried. The known buyer-SDK limitation where two equal barely-sufficient UTXOs (for example two 0.603 KAS inputs at the minimum price) fail closed is acceptable; it must not be “fixed” by donating to the miner.

### Decision

- **Go:** every blocker is a pass, the first mainnet 0.6 KAS payment is correct, and Kyle signs off.
- **No-go:** any blocker fails. Stay on testnet-10, fix, and re-run the live red-team rows that failed plus a happy-path payment before asking again.
