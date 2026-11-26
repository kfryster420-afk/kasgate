# Changelog

All notable changes to KasGate. Contract versions refer to [docs/CONTRACTS.md](docs/CONTRACTS.md).

## Unreleased

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

The on-chain payment rules are unchanged: output 0 merchant, output 1 treasury, fee max(1.5 %, 0.2 KAS), 0.6 KAS
minimum for on-chain payments.
