# Changelog

All notable changes to KasGate. Contract versions refer to [docs/CONTRACTS.md](docs/CONTRACTS.md).

## Unreleased

### Changed: privacy residuals, in-flight cap, orphan sweeper, single-use refunds-status signatures (PR #6)

- **Unsigned deposit responses no longer carry the balance.** `PrepaidDepositQuote` and `PrepaidDepositReceipt`
  (including the idempotent replay) drop `balance_sompi`. Buyer SDK models keep it optional; the MCP
  `prepaid_deposit` tool no longer returns `balance_kas`/`balance_sompi` (use `prepaid_balance`).
- **`prepaid_cap_exceeded` is generic** (quote and deposit): "deposit would exceed the prepaid balance cap; use a
  signed balance read for details". No balance, headroom, credit or cap value. The quote error is now the standard
  `{error, message}` instead of FastAPI's `{detail}`.
- **The quote counts in-flight debits** (`status='debited' AND upstream_status_code IS NULL`) through the same
  helper as the deposit, and the failed-delivery credit clamp counts them too, so all three agree.
- **`POST /v1/prepaid/refunds/challenge`:** an omitted `amount_sompi` on a request needs the buyer-signed balance-read
  headers (new error `refund_amount_required`, 422); explicit amounts work as before. Errors carry no figures. The
  unsigned challenge checks only the public spendable total; the refundable cap (spendable minus call-only credit)
  is enforced when the signed request is submitted to `POST /v1/prepaid/refunds`. SDK `prepaid_refund_request`
  without an amount now signs the challenge request.
- **Accepted by design (documented):** spendable minus on-chain deposits reveals call-only credit/spent amounts to
  anyone who knows a buyer address; headroom can be probed through quote/challenge status codes.
- **Orphaned in-flight debit sweeper** (`PREPAID_DEBIT_SWEEPER_ENABLED=true`, `..._INTERVAL_SECONDS=60`,
  `PREPAID_DEBIT_ORPHAN_AGE_SECONDS=900`, `..._BATCH_SIZE=100`): refunds debits whose upstream call never recorded a
  status, idempotently and safely across replicas (conditional UPDATE). No migration.
- **`GET /v1/prepaid/refunds/status` signatures are single use** (`kasgate:sigreplay:refunds-status:{sha256}`;
  replay is 401 `invalid_balance_signature`, Redis down is 503). The SDK never reuses a timestamp.

### Added: failed-delivery auto-credit (CONTRACT v2.14, after v2.13 / PR #1)

- When a paid call's last delivery failed for good (upstream 5xx, connection error, or a timeout unless the merchant
  excluded timeouts) and redelivery is closed (the last allowed delivery failed, or the redelivery window plus
  `ceil(UPSTREAM_TIMEOUT_SECONDS)` plus `FAILED_DELIVERY_CREDIT_GRACE_SECONDS` passed, by the database clock), the
  merchant share of the payment is credited to the buyer's prepaid balance at that merchant. Nothing moves on-chain;
  the platform fee is not reversed. Inline credits come with an `X-KasGate-Credit` response header; window credits
  are made by the ledger worker. A credited call can no longer be redelivered.
- Explicit `settlements.delivery_outcome` (`pending` before the upstream call, then `delivered`, `failed` +
  `delivery_failure`, or `credited`). Only `failed` is credited; NULL/`pending` never. A failed outcome write is
  logged at ERROR and dead-lettered to `kasgate:deadletter:delivery-outcomes`.
- A settlement still `pending` after the redelivery window plus grace is logged at ERROR once (ledger-worker
  sweep; deduped by `pending_alerted_at`) and is not credited. Alert on that line in the runbook.
- Never credited: oversized responses, any status below 500, k402 `kaspa-utxo` payments, non-P2PK payers, prepaid
  deposits.
- The merchant policy in force at settle time is stamped on the settlement (`failed_delivery_credit_policy`), so a
  later opt-out or timeout toggle never drops or adds credit for calls already paid.
- Merchant policy: `GET`/`PUT /v1/prepaid/failed-delivery-policy` `{enabled, credit_timeouts}` (on by default,
  timeouts included).
- Failure credit is call-only and non-refundable: tracked in `credit_balance_sompi`, spent before deposited sompi
  (`prepaid_debits.credit_sompi`), excluded from refunds, refund holds and cancel cooldown holds
  (`refundable = max(0, balance - hold - credit_balance)`); a late v2.13 payout re-debits deposited sompi first.
  Spendable for calls follows v2.13: balance minus the hold (open refund or unreleased cooldown, including a
  cooldown that outlasts `hold_until` while a payout attempt is still pending).
- Credits are clamped to `PREPAID_MAX_BALANCE_SOMPI`; the cut part is `shortfall_sompi` on the ledger row and a
  signed `prepaid.credit.failed_delivery_shortfall` webhook.
- Idempotent per `tx_id`: each credit is one `prepaid_ledger_entries` row of the new kind `failed_delivery_credit`
  (unique partial index on `tx_id`, unique `settlement_id`, index on `slug`).
- `GET /v1/prepaid/credits` lists credits (filters `slug`, `payer_address`, `tx_id`); `/v1/prepaid/ledger` shows them.
- Balances report `credited_sompi`, `credit_balance_sompi`, `refundable_sompi`; the balance
  identity is now `balance = deposited + credited - debited - refunded - expired`. Unsigned
  `POST /v1/prepaid/credits/challenge` and `POST /v1/prepaid/refunds/challenge` omit `spendable_sompi` and
  `refundable_sompi` so unspent call-only credit cannot be derived without a signature; the signed status is
  the only source for those values (`amount_sompi` on a refunds request challenge still defaults to refundable).
- Signed `POST /v1/prepaid/credits/challenge` + `POST /v1/prepaid/credits/status` returns the buyer's credit
  totals and a page of credits (action `credits`; same challenge path as refunds/status). Each item includes
  `credit_id` (the ledger entry UUID, the same value as `creditId` on `X-KasGate-Credit`). The status body
  accepts optional `before` (opaque cursor) and `limit` (default 10, max 100) and returns `next_cursor` (null
  when there are no more pages). Each page needs a fresh challenge. The cursor is tamper-evident by binding to
  merchant and key, not a secret. Credit `created_at` uses the database clock. `GET` and signed
  `POST /v1/prepaid/refunds/status` stay bare JSON arrays of refunds.
- Signed webhook events `prepaid.credit.failed_delivery` and `prepaid.credit.failed_delivery_shortfall`.
- A credit extends `expires_at` like a deposit: `max(current expires_at, now + policy at credit time)`, never
  shortens a later expiry, never retroactive.
- Alembic migration `0009_failed_delivery_credits` (down_revision `0008_prepaid_refunds`; its amount CHECK keeps
  0008's 0-sompi refund rows; guarded downgrade; partial index `ix_prepaid_ledger_entries_credit_balance_created`
  on credit rows for the credits/status page).
- Settings: `UPSTREAM_TIMEOUT_SECONDS` (was a constant), `FAILED_DELIVERY_CREDIT_GRACE_SECONDS` (now 30 s on top of
  the upstream timeout), `FAILED_DELIVERY_CREDIT_LOOKBACK_SECONDS`.
- Out of scope: no per-endpoint policy override, no other public credit lookup. SDK/MCP parsing of
  `X-KasGate-Credit` and of `POST /v1/prepaid/credits/status` is a follow-up.
- Docs: [docs/CONTRACTS.md](docs/CONTRACTS.md) v2.14, [docs/FAILED_DELIVERY_CREDITS.md](docs/FAILED_DELIVERY_CREDITS.md),
  runbook.

### Added: prepaid refunds and credit expiry (CONTRACT v2.13)

- Buyers can ask for unused prepaid credit back: `POST /v1/prepaid/refunds/challenge`, `POST /v1/prepaid/refunds`,
  `POST /v1/prepaid/refunds/{id}/cancel`, `GET /v1/prepaid/refunds/status`. Requests are signed by the depositing key
  (purpose `kasgate:v1:prepaid-refund`), hold the amount at once and debit nothing yet.
- Merchants pay refunds on-chain from their own wallet (no platform fee, no treasury output) and record them with
  `POST /v1/prepaid/refunds/{id}/payout` (a broadcast `tx_id`, or a signed transaction carrying the refund payload).
  The gateway checks for an output paying the buyer exactly the refund amount and debits the ledger only once the
  payout is seen; otherwise the refund is `payout_submitted` and a background ledger worker re-checks it. Merchants can
  `reject` a request with a reason and list requests (`GET /v1/prepaid/refunds`) and ledger entries
  (`GET /v1/prepaid/ledger`).
- Optional prepaid credit expiry: merchant default via `PATCH /v1/merchants/me {prepaid_expiry_days}`, per-endpoint
  override via `prepaid_expiry_days` on endpoints. Applied at deposit time, never retroactively; expired credit stops
  paying calls and is swept into the ledger.
- Balances report `refunded_sompi`, `expired_sompi`, `refund_hold_sompi`, `spendable_sompi` and `expires_at`.
- Webhook events `prepaid.refund.requested`, `prepaid.refund.paid`, `prepaid.refund.rejected`,
  `prepaid.refund.cancelled`, `prepaid.refund.payout_reverted` and `prepaid.credit.expired`.
- Buyer SDK: `prepaid_refund_request`, `prepaid_refund_cancel`, `prepaid_refunds` (async and sync).
  MCP server: `prepaid_refund_request`, `prepaid_refund_cancel`, `prepaid_refunds`.
- Alembic migration `0008_prepaid_refunds` (tables `prepaid_refunds`, `prepaid_refund_payouts`, `prepaid_ledger_entries`; explicit indexes on
  `tx_id`, guarded downgrade).
- Settings: `LEDGER_WORKER_ENABLED`, `LEDGER_POLL_INTERVAL_SECONDS`, `LEDGER_BATCH_SIZE`,
  `PREPAID_PAYOUT_PENDING_MAX_SECONDS`.

### Security: refund red-team fixes (CONTRACT v2.13, before release)

- Claim/lock: new refund status `paying` (`POST /v1/prepaid/refunds/{id}/claim`, call it before broadcasting). The buyer
  cannot cancel a claimed refund (the claim lapses after `PREPAID_REFUND_CLAIM_TTL_SECONDS`), and every payout is
  recorded (`payout_submitted`) BEFORE the node is asked or the transaction is broadcast, so a cancel can no longer race
  the payout and double-refund.
- Late payout backstop: a payout that confirms after the refund was cancelled or rejected is still recorded (`paid`
  with `late_paid_from`, re-debit, `shortfall_sompi`, ledger row, `prepaid.refund.paid`).
- Payout attempt history: new table `prepaid_refund_payouts` keeps every attempted tx_id (unique index on `tx_id`);
  pending attempts are re-checked even after a revert, so a payout that lands late is still recorded and the merchant is
  not asked to pay twice. Only a definite `invalid_payout` resolves an attempt; node errors back off
  (`PREPAID_PAYOUT_BACKOFF_BASE_SECONDS`, `PREPAID_PAYOUT_BACKOFF_MAX_SECONDS`); unseen attempts are abandoned after
  `PREPAID_PAYOUT_ATTEMPT_MAX_SECONDS`. Cancel and reject are refused while an attempt is pending. A corrected tx_id is
  a new attempt; both are kept (`GET /v1/prepaid/refunds/{id}/payouts`). New webhook `prepaid.refund.payout_reverted`.
- Payload binding: tx_id payouts must carry `kasgate:v1:refund:<id hex>` too (KasWare
  `sendKaspa(payout_address, amount_sompi, {payload})`), so a buyer's self-transfer or another merchant's payout of
  the same amount cannot be claimed. `verify_payout` takes the `refund_id`.
- Payouts are marked paid only after virtual-chain acceptance, also with `SETTLEMENT_MODE=mempool`.
- `GET /v1/prepaid/refunds/status` no longer shows the merchant's reason or payout errors; the buyer reads them with a
  signed `status` challenge and `POST /v1/prepaid/refunds/status` (SDK `prepaid_refunds(..., signed=True)`, MCP
  `prepaid_refunds(include_reasons=true)`).
- A cancel aimed at the wrong refund no longer burns the challenge. `POST /v1/prepaid/refunds`, `/cancel` and
  `/status` are rate limited per IP (`RL_PREPAID_REFUND_PER_MINUTE`, default 30).
- `TRUSTED_PROXY_IPS` (default off): per-IP rate limits honour `X-Forwarded-For` only from those proxies, using the
  right-most untrusted hop. List only your proxy's fixed IP, never a Docker bridge range.
- The refund error codes are now part of the `X402ErrorBody.error` vocabulary; the documented error list also gains
  `price_unavailable`, `invalid_exact_payment`, `payment_expired`, `insufficient_balance` and `trial_exhausted`.
- Migration `0008_prepaid_refunds` was revised in place (it is not on main yet).

### Security: refund red-team fixes, round 2 (CONTRACT v2.13, before release)

- Claim lapse: cancelling a refund the merchant ever claimed first asks the node (mempool and UTXO index of the
  payout address) for a transaction that may be its payout; a new one is recorded as a `scan` attempt and the cancel
  is refused (409), and a node outage refuses it (503) without burning the challenge. Such a cancel keeps the amount
  held until `hold_until` (`PREPAID_REFUND_CANCEL_COOLDOWN_SECONDS`, 1800) before it is spendable; a payout landing in
  that window is debited in full. The public status no longer shows the claim (`paying` reads as `requested`, claim
  times null); the merchant (and the signed buyer status) get `claim_expires_at`.
- Bounded claims: `first_claimed_at`; renewals stop after `PREPAID_REFUND_CLAIM_MAX_SECONDS` (3600).
- Attempt flooding: at most `PREPAID_PAYOUT_MAX_PENDING_ATTEMPTS` (3) pending attempts per refund (409
  `too_many_attempts`); an abandoned attempt is never revived by a bare tx_id (409 `payout_abandoned`), and a
  transaction proof keeps its original age; unseen attempts back off with their age
  (`PREPAID_PAYOUT_UNSEEN_BACKOFF_FRACTION`); `POST /payout` is rate limited per merchant
  (`RL_REFUND_PAYOUT_PER_MERCHANT_PER_MINUTE`, 20).
- A late payout never closes a newer refund that the merchant is paying (an unlapsed claim, a pending attempt or
  `payout_submitted`); the difference is recorded as shortfall.
- Migration guard: `alembic upgrade`/`stamp` and the gateway startup refuse a database migrated with an earlier draft
  of `0008_prepaid_refunds` and say how to fix it (downgrade to 0007, upgrade again); 0008's downgrade now works from
  the first draft too. See RUNBOOK "Migration 0008 revised in place".
- Webhook data of refund events gains `hold_until`. `prepaid_refund_payouts.method` gains `scan`.

### Security: refund red-team fixes, round 3 (CONTRACT v2.13, before release)

- The cooldown hold is not released while any payout attempt for that refund is still pending (it stays until the
  attempt is confirmed — debited in full from the hold — or resolved invalid/abandoned).
- The pending-attempt cap applies only to bare `tx_id` posts. A signed `{transaction}` proof is exempt (it is checked
  locally) and `scan` attempts do not count toward it.
- The pre-cancel UTXO scan ignores outputs with `blockDaaScore` at or below the virtual DAA stored at the first claim
  (`first_claimed_daa_score`).
- CONTRACTS Limits now names the remaining shortfall cases accurately, including a merchant who posts a `tx_id`
  without ever claiming (no scan, no cooldown).

### Security: refund red-team fix, round 4 (CONTRACT v2.13, before release)

- A claim renewal no longer backfills a missing `first_claimed_daa_score`. The score is written only in the same
  write that sets `first_claimed_at` the first time; if the node could not be asked then, it stays null and the
  pre-cancel UTXO filter is skipped (as CONTRACTS already said).
- CONTRACTS Limits adds the shortfall case of a payout accepted at or before `first_claimed_daa_score` (broadcast
  before or at the first claim), and keeps the null-score skip. Merchants should claim before broadcasting.

### Security: earlier-features v2 (CONTRACT v2.12.1 / v2.13, before release)

- SSRF: the SIIT IPv4-translated prefix `0:0:0:0:ffff:0:0:0/96` (`::ffff:0:a.b.c.d`) is refused by `is_forbidden_ip`
  and webhook URL validation, like `::/96` and `fec0::/10`.
- Prepaid cap: the cap is re-read after the balance row lock, so a drop while a deposit is queued is 422 before
  submit, not a settled-then-DLQ 503. In-flight prepaid debits (`prepaid_debits` with `status = 'debited'` and
  `upstream_status_code` IS NULL) are counted under that same lock; a deposit that would pass only by ignoring a
  hanging debit is 422, so a later 5xx refund cannot land the balance past the cap. Refunds are not clamped.
- Unsigned `GET /v1/prepaid/balance` returns only `spendable_sompi` and `expires_at`; `balance_sompi`, `updated_at`,
  identity and ledger totals are null. 402 prepaid offers' `balanceUrl` is this read: use `spendable_sompi`. A
  buyer-signed read (and merchant `GET /balances`) still fills every field.
- `GET /v1/prepaid/refunds/status` is buyer-signed with the same headers as the balance read over Purpose
  `kasgate:v1:refunds-status-read`. Unsigned or invalid -> 401 `invalid_balance_signature`. Reasons and the claim
  stay withheld; `POST /refunds/status` with a `status` challenge is unchanged. The buyer SDK signs the default
  `prepaid_refunds` GET; MCP `prepaid_refunds` follows it.

The on-chain payment rules are unchanged: output 0 merchant, output 1 treasury, fee max(1.5 %, 0.2 KAS), 0.6 KAS
minimum for on-chain payments.
