# Changelog

All notable changes to KasGate. Contract versions refer to [docs/CONTRACTS.md](docs/CONTRACTS.md).

## Unreleased

### Added: failed-delivery auto-credit (CONTRACT v2.14, after v2.13 / PR #1)

- When a paid call's upstream answers 5xx or not at all and redelivery is closed (the last allowed delivery failed,
  or the redelivery window plus `FAILED_DELIVERY_CREDIT_GRACE_SECONDS` passed), the merchant share of the payment is
  credited to the buyer's prepaid balance at that merchant. Nothing moves on-chain; the platform fee is not reversed.
  Inline credits come with an `X-KasGate-Credit` response header; window credits are made by the ledger worker.
  A credited call can no longer be redelivered.
- Idempotent per `tx_id`: each credit is one `prepaid_ledger_entries` row of the new kind `failed_delivery_credit`
  (unique partial index on `tx_id`, unique `settlement_id`, index on `slug`).
- Merchant opt-out: `GET`/`PUT /v1/prepaid/failed-delivery-policy` (on by default, never retroactive).
- `GET /v1/prepaid/credits` lists credits (filters `slug`, `payer_address`, `tx_id`); `/v1/prepaid/ledger` shows them.
- Balances report `credited_sompi`; the balance identity is now
  `balance = deposited + credited - debited - refunded - expired`.
- Signed webhook event `prepaid.credit.failed_delivery`, one per credit.
- k402 `kaspa-utxo` payments, non-P2PK payers and prepaid deposits are never credited.
- Alembic migration `0009_failed_delivery_credits` (down_revision `0008_prepaid_refunds`, guarded downgrade).
- Settings: `FAILED_DELIVERY_CREDIT_GRACE_SECONDS`, `FAILED_DELIVERY_CREDIT_LOOKBACK_SECONDS`.
- A credit extends `expires_at` like a deposit: `max(current expires_at, now + policy at credit time)`, never
  shortens a later expiry, never retroactive.
- A credit may exceed `PREPAID_MAX_BALANCE_SOMPI` (restitution of an already-paid call); a deposit still may not.
- Out of scope: no per-endpoint opt-out, no public credit lookup. Buyer-facing credit status will follow #1's
  buyer-signed status endpoint; SDK/MCP parsing of `X-KasGate-Credit` is a follow-up.
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
  `prepaid.refund.cancelled` and `prepaid.credit.expired`.
- Buyer SDK: `prepaid_refund_request`, `prepaid_refund_cancel`, `prepaid_refunds` (async and sync).
  MCP server: `prepaid_refund_request`, `prepaid_refund_cancel`, `prepaid_refunds`.
- Alembic migration `0008_prepaid_refunds` (tables `prepaid_refunds`, `prepaid_ledger_entries`; explicit indexes on
  `tx_id`, guarded downgrade).
- Settings: `LEDGER_WORKER_ENABLED`, `LEDGER_POLL_INTERVAL_SECONDS`, `LEDGER_BATCH_SIZE`,
  `PREPAID_PAYOUT_PENDING_MAX_SECONDS`.

The on-chain payment rules are unchanged: output 0 merchant, output 1 treasury, fee max(1.5 %, 0.2 KAS), 0.6 KAS
minimum for on-chain payments.
