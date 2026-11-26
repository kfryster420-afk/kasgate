# Failed-delivery auto-credit

KasGate settles a payment on-chain **before** it calls the merchant's upstream. When the upstream then fails, the
buyer may re-present the same payment for a free redelivery, within `REDELIVERY_WINDOW_SECONDS` (default 600) and at
most `REDELIVERY_MAX_ATTEMPTS` (default 3) deliveries. Until now, once redelivery was closed the money was simply
spent. With failed-delivery auto-credit, the **merchant share** of such a payment becomes prepaid credit for that
buyer at that merchant (CONTRACT v2.13 prepaid balance, scheme `kasgate-prepaid-v1`), spendable on any of the
merchant's endpoints with ordinary `X-KasGate-Prepaid` debits.

CONTRACT v2.14 (additive, in [CONTRACTS.md](CONTRACTS.md)) depends on v2.13 / PR #1 and must merge after it.

## When a call is credited

A settlement is credited exactly once when all of these hold:

1. `status = 'settled'` and the **last** delivery failed: the upstream answered 5xx, or gave no response (timeout,
   connection error, oversized response; `upstream_status_code IS NULL`). Any 1xx-4xx answer, or a later
   successful redelivery, is never credited.
2. Redelivery is closed:
   * `attempts_exhausted`: `delivery_attempts >= REDELIVERY_MAX_ATTEMPTS`, or redelivery is switched off
     (`REDELIVERY_WINDOW_SECONDS=0`). Credited **inline**: the failing response itself (the relayed 5xx, or
     `502 upstream_error`) carries the `X-KasGate-Credit` header.
   * `window_expired`: `settled_at + REDELIVERY_WINDOW_SECONDS + FAILED_DELIVERY_CREDIT_GRACE_SECONDS <= now`.
     Credited by the ledger worker's sweep. The grace (default 60 s, minimum 31 s) is longer than one upstream
     attempt (30 s), so a redelivery that started just before the window closed has finished first.
3. The payer key is proven: `kaspa-exact-v1` and `kaspa-exact-v2` payments from a P2PK Schnorr address. k402
   `kaspa-utxo` payments (no payer proof) and non-P2PK payers are never credited, nor are prepaid deposits (their
   merchant share is already credit).
4. The merchant's policy covered the call when it was **settled** (see below).

Once a settlement is credited, the redelivery claim refuses it (`409 payment_replayed`), so a buyer can never get
both a redelivery and a credit.

## What is credited

`credit_sompi = settlement.merchant_amount_sompi` (output 0). The platform fee (output 1) is not reversed and
nothing moves on-chain: the merchant was paid by the original transaction, and the credit is calls the merchant
owes, like any prepaid balance. Before crediting, overdue credit on that balance expires (v2.13 order). A credit
extends the balance expiry like a deposit, using the v2.13 policy **in force at credit time** (the endpoint's
`prepaid_expiry_days`, else the merchant default), never retroactively: `expires_at = max(current expires_at,
now + policy days)`. A later existing expiry is never shortened. Credit already promised "never" stays
non-expiring when a later credit lands under a policy; a "never" policy credit clears the expiry.

The balance identity becomes
`balance = deposited + credited - debited - refunded - expired`, with `credited_sompi >= 0`. A credit may take
the balance above `PREPAID_MAX_BALANCE_SOMPI` (a deposit that would is still 422): this is restitution of sompi
the buyer already paid on-chain, not a new deposit.

## Merchant policy (opt-out)

On by default. `GET /v1/prepaid/failed-delivery-policy` [Bearer] returns
`{enabled, enabled_at, disabled_at}`; `PUT /v1/prepaid/failed-delivery-policy {"enabled": false}` opts out and
`{"enabled": true}` opts back in. A change is never retroactive: a call is covered only if it settled after the
latest `enabled_at` and before `disabled_at` (if any). Opting out does not drop credits for calls that were
settled while the policy was on. Merchants that existed before migration 0009 are covered from the upgrade time.
There is no per-endpoint override of this opt-out (out of scope; expiry may still follow a v2.13 per-endpoint
`prepaid_expiry_days`, because that is the deposit expiry policy, not this merchant-level credit policy).

## API

* `GET /v1/prepaid/credits` [Bearer]: the merchant's failed-delivery credits, newest first. Filters `slug`,
  `payer_address`, `tx_id`; `limit` (1-200, default 50), `offset`. Each item: `id`, `kind`
  (`failed_delivery_credit`), `tx_id`, `settlement_id`, `endpoint_id`, `slug`, `payer_address`, `payment_scheme`,
  `amount_sompi` (paid on-chain), `fee_sompi`, `credit_sompi`, `balance_after_sompi`, `reason`
  (`attempts_exhausted|window_expired`), `upstream_status_code` (>= 500 or null), `delivery_attempts`,
  `settled_at`, `created_at`. Amounts are decimal strings.
* `GET /v1/prepaid/ledger` [Bearer] lists credit rows too (kind `failed_delivery_credit`, direction `credit`,
  with `settlement_id`, `slug`, `credit_reason`).
* Balances (public `GET /v1/prepaid/balance?slug=&payer_address=`, merchant `GET /v1/prepaid/balances`) report
  `credited_sompi`.
* Response header `X-KasGate-Credit` (Base64 JSON, exposed via CORS, stripped from upstream responses so an
  upstream cannot forge it): `{creditId, kind, txId, merchantId, creditSompi, balanceSompi, reason}`.
* There is no public credit-lookup route (out of scope). Buyers see `X-KasGate-Credit` and `credited_sompi` on
  the public balance. Buyer-facing credit status will follow the buyer-signed status endpoint from v2.13 / #1;
  SDK and MCP parsing of `X-KasGate-Credit` is a follow-up (agent 05).

## Webhook

Event `prepaid.credit.failed_delivery`, one per credit (deterministic event id from the ledger entry id), signed
like every KasGate webhook (`X-KasGate-Signature: t=<unix>,v1=<HMAC-SHA256(secret, "<t>.<raw body>")>`).
`data`: `ledger_entry_id`, `kind`, `settlement_id`, `tx_id`, `payment_scheme`, `endpoint_id`, `endpoint_slug`,
`payer_address`, `amount_sompi`, `fee_sompi`, `credit_sompi`, `balance_sompi`, `reason`, `upstream_status_code`,
`delivery_attempts`, `settled_at`.

## Data model (migration `0009_failed_delivery_credits`, after `0008_prepaid_refunds`)

* `merchants.failed_delivery_credit_enabled_at` (NOT NULL, default now()), `failed_delivery_credit_disabled_at`.
* `prepaid_balances.credited_sompi` (default 0, CHECK >= 0); the balance identity CHECK includes it.
* `prepaid_ledger_entries`: kind CHECK gains `failed_delivery_credit`; new columns `settlement_id` (FK settlements,
  RESTRICT), `slug`, `credit_reason`. CHECKs: credit rows (and only they) carry settlement, slug and reason, and
  credit rows also carry `tx_id` and direction `credit`; `credit_reason IN (attempts_exhausted, window_expired)`.
  Indexes: unique partial `ix_prepaid_ledger_entries_credit_tx_id` (`tx_id WHERE kind = 'failed_delivery_credit'`),
  unique `ix_prepaid_ledger_entries_settlement_id`, `ix_prepaid_ledger_entries_slug`.
* `settlements`: partial index `ix_settlements_undelivered` on `settled_at` for the sweep.
* `webhook_events` event-type CHECK gains `prepaid.credit.failed_delivery`.
* Downgrade refuses while any credit row, non-zero `credited_sompi`, credit webhook event or webhook subscribed to
  it exists.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `FAILED_DELIVERY_CREDIT_GRACE_SECONDS` | 60 (31-3600) | Wait after the redelivery window before the sweep credits. |
| `FAILED_DELIVERY_CREDIT_LOOKBACK_SECONDS` | 604800 (1 h-90 d) | The sweep only looks at settlements this recent. |

The sweep runs in the ledger worker (`LEDGER_WORKER_ENABLED`, `LEDGER_POLL_INTERVAL_SECONDS`, batches of
`LEDGER_BATCH_SIZE`). With the worker off, only `attempts_exhausted` credits happen.

## Out of scope

* No per-endpoint override of the failed-delivery opt-out policy (merchant-level only).
* No public credit-lookup route. Buyer-facing credit status will come with #1's buyer-signed status endpoint
  later. SDK and MCP reading of `X-KasGate-Credit` is 05's follow-up.
