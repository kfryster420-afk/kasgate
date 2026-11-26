# Free trial calls + merchant webhooks (CONTRACT v2.12, Alembic 0007_trials_webhooks)

Status: design for the last roadmap feature. Builds on CONTRACT v2.11 (prepaid credit) and reuses its signed-message
pattern. Paid calls are unchanged: output 0 merchant, output 1 treasury, fee max(ceil(1.5 %), 0.2 KAS), minimum
0.6 KAS. No mainnet treasury is configured by this work.

## 1. Free trial calls (`kasgate-trial-v1`)

### Merchant settings (per endpoint)
- `free_trial_calls` int, 0..100 (`TRIAL_MAX_CALLS`), default 0 = no trial. Unpaid calls each buyer key gets.
- `trial_daily_cap` int, 1..100000, default 100. Total trial calls the endpoint serves per UTC day, across all buyers
  (anti-abuse: buyer keys are free to mint, so the per-key count alone is not a Sybil defence).
- Both are on EndpointCreate / EndpointUpdate / EndpointRead, the OpenAPI import (`free_trial_calls` applies to every
  imported row) and the public catalog / directory (`free_trial_calls` only; the daily cap stays private).

### Buyer identity = a signed challenge (never IP or headers)
Same shape as a prepaid debit (CONTRACT v2.11), but its own Purpose and Redis namespace, so a trial signature can never
be a prepaid debit, a payerProof or a login:
```
KasGate
Domain: {netloc of PUBLIC_BASE_URL}
Purpose: kasgate:v1:trial-call
Address: {payer kaspa: P2PK address}
Merchant: {merchant_id}
Endpoint: {slug}
Method: {METHOD}
Path: {raw path + ?query}
Body-Hash: {sha256 hex | -}
Nonce: {32 hex}
Expires: {iso8601}
```
The buyer's identity is the x-only Schnorr key of a P2PK `kaspa:`/`kaspatest:` address (public data only; no seed or
private key ever reaches KasGate). The key, not the IP, is what the per-buyer count is keyed on.

Flow:
1. `/p/{slug}` without payment -> the usual 402, plus `X-KasGate-Trial-Offer: Base64(JSON{scheme:"kasgate-trial-v1",
   freeTrialCalls, challengeUrl, statusUrl})` when the endpoint has trials.
2. `POST /v1/trials/challenge {slug, payer_address, method, path, body_hash}` (public, unpaid-IP rate limit) -> 201
   TrialChallenge `{nonce, message, ..., free_trial_calls, calls_used, remaining, expires_at}`; stored as
   `kasgate:trial:challenge:{nonce}` EX `payment_max_age_seconds`. No trial / used up / today's cap reached -> 402
   `trial_exhausted` (no accepts; the buyer pays the 402 it already holds).
3. `/p/{slug}` with `X-KasGate-Trial: Base64(JSON{nonce, signature})`.

### Enforcement (Postgres is the source of truth, Redis guards the nonce and the IP)
Order on `/p`: parse -> request shape/size -> per-IP trial limit `rl_trial_calls_per_ip_per_hour` (Redis fixed window,
429) -> nonce already in `trial_calls` -> 409 -> challenge unknown/expired -> 402 `payment_expired` -> stored challenge
must match the request actually served (endpoint, merchant, method, raw path+query, body hash) and the signature must
verify under the stored key -> 402 `invalid_payment_header` -> `SET kasgate:trial:noncelock:{nonce} NX` (held -> 409)
-> ONE transaction:
  1. `INSERT trial_usages ... ON CONFLICT (endpoint_id, payer_public_key) DO NOTHING`
  2. `UPDATE trial_usages SET calls_used = calls_used + 1 WHERE ... AND calls_used < endpoint.free_trial_calls
     RETURNING calls_used` (row lock serialises concurrent calls; 0 rows -> `trial_exhausted`)
  3. `INSERT trial_daily_counts (endpoint_id, day, calls=1) ON CONFLICT DO UPDATE SET calls = calls + 1
     WHERE calls < trial_daily_cap RETURNING calls` (0 rows -> `trial_exhausted`, daily cap)
  4. `INSERT trial_calls` (unique nonce) + the `trial.used` outbox event (savepoint, see 2.4)
-> consume the challenge -> forward upstream. Concurrent calls can never exceed either limit (tested with a gather
race). No settlement row, no on-chain transaction and NEVER a fee: trial calls are free to the buyer and earn the
treasury nothing.

Exhausted on `/p` -> the normal fresh 402 (v1 + k402 + exact, or the prepaid offer on prepaid-only endpoints) with
error `trial_exhausted` and `X-KasGate-Trial-Remaining: 0`, so the SDK can pay straight away.

Response headers on a trial call: `X-KasGate-Trial-Limit`, `X-KasGate-Trial-Remaining`, `X-KasGate-Trial-Receipt`
(Base64 JSON `{trialCallId, nonce, limit, remaining, status: used|refunded}`). All trial headers are in
PAYMENT_HEADERS (stripped both ways). Combining `X-KasGate-Trial` with any payment header -> 402 invalid_payment_header.

Upstream gives no response or a 5xx -> the trial call is refunded (status `refunded`, `calls_used - 1`); the daily
counter is not refunded (conservative). 2xx-4xx stay used.

Public read: `GET /v1/trials/status?slug=&payer_address=` -> `{free_trial_calls, calls_used, remaining}`.
Merchant read: `GET /v1/trials/calls` [Bearer].

### Tables
- `endpoints.free_trial_calls` (CHECK 0..100), `endpoints.trial_daily_cap` (CHECK 1..100000).
- `trial_usages`: unique (endpoint_id, payer_public_key); index merchant_id; CHECK calls_used >= 0, key hex64.
- `trial_daily_counts`: PK (endpoint_id, day); CHECK calls >= 0.
- `trial_calls`: unique nonce; indexes usage_id, (endpoint_id, created_at), (merchant_id, created_at);
  CHECK status in (used, refunded), refunded_at iff refunded, nonce hex32, body_hash hex64.

## 2. Merchant webhooks

### 2.1 Registration
`POST /v1/webhooks {url, event_types[], description?}` [Bearer] -> 201 `{webhook, secret}`. The secret
(`whsec_` + 32 random bytes urlsafe) is shown ONCE (create and rotate-secret). Stored Fernet-encrypted with
ENCRYPTION_KEY (HMAC needs the raw key, so a one-way hash cannot work); only a 10-char prefix is kept in clear for the
UI. Never logged, never returned by list/read. Max 10 webhooks per merchant.

URL rules (Pydantic): `https://` only, no userinfo, no fragment, <= 2048 chars, host not localhost/.local/.internal and
not a private/loopback/link-local/reserved IP literal.

Event types: `payment.settled` (every /p payment format: kaspa-exact-v1, k402-kaspa-utxo, kaspa-exact-v2),
`prepaid.deposit`, `prepaid.debit`, `trial.used`, `endpoint.created`, `endpoint.updated`, plus `webhook.test` (only
from the test-fire route, always delivered to that one webhook). CONTRACT v2.13 adds `prepaid.refund.requested`,
`prepaid.refund.paid`, `prepaid.refund.rejected`, `prepaid.refund.cancelled` and `prepaid.credit.expired` (payloads
in docs/CONTRACTS.md, v2.13). Failed-delivery auto-credit adds `prepaid.credit.failed_delivery` (payload in
docs/FAILED_DELIVERY_CREDITS.md).

### 2.2 Delivery request
`POST <url>` with body = canonical JSON (sorted keys, no spaces)
`{"id":"evt_<32hex>","type":...,"api_version":"2.12","created_at":iso,"merchant_id":...,"data":{...}}`
Headers: `Content-Type: application/json`, `User-Agent: KasGate-Webhooks/2.12`, `X-KasGate-Event-Id`,
`X-KasGate-Event-Type`, `X-KasGate-Delivery-Id`, `X-KasGate-Timestamp: <unix seconds>`,
`X-KasGate-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>`.
Receivers verify with a constant-time compare and reject |now - t| > 300 s (replay window); they dedupe on the event id.

### 2.3 Durable outbox
- `webhooks`: merchant-owned config (url, event_types[], secret_encrypted, secret_prefix, is_active).
- `webhook_events`: one row per event, unique `event_id`. The id is deterministic for payment events
  (`evt_` + sha256(type:source id)[:32], e.g. settlement id), so a repeated emit is a no-op (ON CONFLICT DO NOTHING) and
  every redelivery carries the same id.
- `webhook_deliveries`: one row per (event, webhook), unique. status `pending | delivering | succeeded | dead`,
  attempts, next_attempt_at, locked_until, last_status_code, last_error, delivered_at.
  Partial index on (next_attempt_at) WHERE status in (pending, delivering) for the dispatcher.

Dispatcher (`app/webhooks.py`, an asyncio task started in the lifespan; safe with many replicas): claim a batch with
`UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)` setting `delivering`, `locked_until = now + 60 s`,
`attempts + 1`; deliver with bounded concurrency; 2xx -> `succeeded`; anything else -> `pending` with
`next_attempt_at = now + min(base * 2^(attempts-1), cap) * jitter(0.9..1.1)` (defaults base 30 s, cap 3600 s), or
`dead` after `webhook_max_attempts` (default 8, about 2 hours). A crashed worker's lease expires and the row is picked
up again. `POST /v1/webhooks/{id}/deliveries/{delivery_id}/redeliver` puts any non-delivering row back to `pending`,
attempts 0, due now.

### 2.4 Never on the payment path
Events are written in the SAME transaction as the payment/debit/trial row, inside a SAVEPOINT, after the main rows
are flushed (so an IntegrityError on the payment row still surfaces as 409). Any webhook-side failure rolls back to
the savepoint and is logged; the payment commits regardless. Network delivery only happens in the dispatcher, never
inline on `/p`, deposits or debits.

### 2.5 SSRF and limits
Delivery uses the existing GuardedTransport: DNS resolved at connect time, refused if ANY address is not global unicast
(private, loopback, link-local, CGNAT, multicast, v6-embedded v4), and the TCP connection goes to the validated IP
(no DNS rebinding). No redirects (3xx = failure), no proxies from env, total timeout 10 s, response body read capped at
64 KiB, request body capped at 64 KiB.

### 2.6 Merchant routes [Bearer]
GET/POST `/v1/webhooks`, GET/PATCH/DELETE `/v1/webhooks/{id}`, POST `/v1/webhooks/{id}/rotate-secret`,
POST `/v1/webhooks/{id}/test` (enqueue + one immediate attempt, returns the delivery), GET
`/v1/webhooks/{id}/deliveries?status=&limit=&offset=`, POST `/v1/webhooks/{id}/deliveries/{delivery_id}/redeliver`,
GET `/v1/webhooks/event-types`.

## 3. SDK, MCP, web
- buyer SDK: `TrialOffer`, `TrialChallenge`, `TrialReceipt` types; `KasGateClient(use_free_trials=True)` tries a trial
  (challenge -> sign with the payer key -> `X-KasGate-Trial`) before paying whenever a 402 carries the trial offer,
  and falls back to the normal payment on `trial_exhausted`; `trial_status(slug)`; `verify_webhook_signature()` helper.
- MCP: `trial_status` tool; `call_endpoint` uses trials automatically (with `use_free_trials` flag).
- web: endpoint form gets "Free trial calls" + "Daily trial cap"; Webhooks panel (create with one-time secret reveal,
  list, enable/disable, delete, test-fire, delivery log, redeliver) behind Next API routes.

## 4. Open questions
1. Sybil: keys are free, so a determined abuser gets up to `trial_daily_cap` calls per endpoint per day. A
   "funded address only" rule (UTXO lookup at challenge time) would raise the bar; not built.
2. Should the daily counter be refunded on upstream 5xx? Currently not.
3. Webhook secret rotation has no overlap window (old secret invalid immediately).
4. Event retention / pruning of `webhook_events` and `webhook_deliveries` (no TTL yet).
