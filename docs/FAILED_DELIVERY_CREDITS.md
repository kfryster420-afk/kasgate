# Failed-delivery auto-credit

KasGate settles a payment on-chain **before** it calls the merchant's upstream. When the upstream then fails, the
buyer may re-present the same payment for a free redelivery, within `REDELIVERY_WINDOW_SECONDS` (default 600) and at
most `REDELIVERY_MAX_ATTEMPTS` (default 3) deliveries. Until now, once redelivery was closed the money was simply
spent. With failed-delivery auto-credit, the **merchant share** of such a payment becomes prepaid credit for that
buyer at that merchant (CONTRACT v2.13 prepaid balance, scheme `kasgate-prepaid-v1`), spendable on any of the
merchant's endpoints with ordinary `X-KasGate-Prepaid` debits. This credit is **call-only**: it is never refunded
on-chain.

CONTRACT v2.14 (additive, in [CONTRACTS.md](CONTRACTS.md)) depends on v2.13 / PR #1 and must merge after it.

## Delivery outcome

Every settlement records how its last delivery ended in `settlements.delivery_outcome`:

| Outcome | When |
| --- | --- |
| `pending` | Written with the settlement (and again when a redelivery is claimed), **before** the upstream call. |
| `delivered` | The upstream answered with any status below 500. |
| `failed` | `delivery_failure` = `upstream_5xx`, `connect_error`, `timeout` or `oversized`. |
| `credited` | The failure was credited (terminal; no redelivery). |

Only `failed` can be credited. NULL (settled before migration 0009) and `pending` (the outcome write failed, or the
process died during the call) are never credited. If the outcome cannot be stored, KasGate logs at ERROR and pushes
the outcome JSON to the Redis list `kasgate:deadletter:delivery-outcomes`; the buyer still gets the upstream's answer.

If a settlement is still `pending` after the redelivery window plus grace (the same wait as a `window_expired`
credit, on the database clock, inside the lookback), the ledger worker logs ERROR once (`delivery outcome still
pending after the redelivery window`) and sets `pending_alerted_at`. That is the alert (operators already alert on
ERROR lines). The row is not credited; reconcile `delivery_outcome` by hand from the dead-letter list or the
process log.

## When a call is credited

A settlement is credited at most once when all of these hold:

1. `status = 'settled'`, `delivery_outcome = 'failed'` and the failure is creditable under the policy stamped on the
   settlement:
   * `upstream_5xx` and `connect_error`: always.
   * `timeout`: unless the merchant excluded timeouts (`credit_timeouts = false`) **before the call settled**.
   * `oversized` (the upstream answered, the gateway refused to relay more than its body limit): never.
   * any status below 500: never (that is `delivered`).
2. Redelivery is closed:
   * `attempts_exhausted`: `delivery_attempts >= REDELIVERY_MAX_ATTEMPTS`, or redelivery is switched off
     (`REDELIVERY_WINDOW_SECONDS=0`). Credited **inline**: the failing response itself (the relayed 5xx, or
     `502 upstream_error`) carries the `X-KasGate-Credit` header.
   * `window_expired`: `settled_at + REDELIVERY_WINDOW_SECONDS + ceil(UPSTREAM_TIMEOUT_SECONDS) +
     FAILED_DELIVERY_CREDIT_GRACE_SECONDS <= now()`, by the database clock. Credited by the ledger worker's sweep.
     The wait follows the configured upstream timeout, so a redelivery that started just before the window closed
     has finished first. The redelivery claim also uses the database clock.
3. The payer key is proven: `kaspa-exact-v1` and `kaspa-exact-v2` payments from a P2PK Schnorr address. k402
   `kaspa-utxo` payments (no payer proof) and non-P2PK payers are never credited, nor are prepaid deposits (their
   merchant share is already credit).

Once a settlement is credited, the redelivery claim refuses it (`409 payment_replayed`), so a buyer can never get
both a redelivery and a credit.

## What is credited

`credit_sompi = min(merchant_amount_sompi, max(0, PREPAID_MAX_BALANCE_SOMPI - balance))`; the rest is the
`shortfall_sompi`, kept on the ledger row and announced by a signed `prepaid.credit.failed_delivery_shortfall`
webhook. A fully clamped credit is a ledger row with amount 0 and the whole share as shortfall; it still closes the
settlement. The platform fee (output 1) is not reversed and nothing moves on-chain.

Before crediting, overdue credit on that balance expires (v2.13 order). A credit extends the balance expiry like a
deposit, using the v2.13 policy **in force at credit time**, never retroactively: `expires_at = max(current
expires_at, now + policy days)`. A later existing expiry is never shortened; credit promised "never" stays
non-expiring; a "never" policy credit clears the expiry.

## Call-only credit and v2.13 refunds

The balance identity becomes `balance = deposited + credited - debited - refunded - expired`. The unspent credit is
tracked separately in `credit_balance_sompi` (`0 <= credit_balance_sompi <= LEAST(credited_sompi, balance -
refund_hold_sompi)`):

* **Calls spend credit first**, then deposited sompi. `prepaid_debits.credit_sompi` is the part of each debit paid
  from credit; a debit restored after an upstream failure gives that part back to the credit.
* **Refunds take deposited, unspent sompi only**: `refundable = max(0, balance - refund_hold - credit_balance)`
  (`refund_hold` includes an open refund and a cancel cooldown). The refund
  challenge's default amount is `refundable_sompi`; asking for more is `409 insufficient_balance`. So the refund
  hold never covers credit, and credit keeps paying calls while a refund is open.
* A **late payout** of a cancelled or rejected refund re-debits deposited sompi first, then credit (the coins
  already reached the buyer, so the same sompi never stays spendable too).
* **Expiry** drops the unspent credit with the rest of the spendable balance.

## Merchant policy (opt-out)

On by default, timeouts included. `GET /v1/prepaid/failed-delivery-policy` [Bearer] returns
`{enabled, credit_timeouts, enabled_at, disabled_at}`; `PUT` takes `{"enabled"?: bool, "credit_timeouts"?: bool}`
(at least one). The policy in force when a call **settles** is stamped on its settlement, so a later change never
adds or drops credit for calls already paid. There is no per-endpoint override (out of scope).

## API

* `GET /v1/prepaid/credits` [Bearer]: the merchant's failed-delivery credits, newest first. Filters `slug`,
  `payer_address`, `tx_id`; `limit` (1-200, default 50), `offset`. Each item: `id`, `kind`, `tx_id`,
  `settlement_id`, `endpoint_id`, `slug`, `payer_address`, `payment_scheme`, `amount_sompi` (paid on-chain),
  `fee_sompi`, `credit_sompi`, `shortfall_sompi`, `balance_after_sompi`, `reason`
  (`attempts_exhausted|window_expired`), `delivery_failure`, `upstream_status_code` (>= 500 or null),
  `delivery_attempts`, `settled_at`, `created_at`. Amounts are decimal strings.
* `GET /v1/prepaid/ledger` [Bearer] lists credit rows too (with `settlement_id`, `slug`, `credit_reason`,
  `shortfall_sompi`).
* Balances (public `GET /v1/prepaid/balance`, merchant `GET /v1/prepaid/balances`) and the refund challenge report
  `credited_sompi`, `credit_balance_sompi` and `refundable_sompi`.
* Response header `X-KasGate-Credit` (Base64 JSON, exposed via CORS, stripped from upstream responses so an
  upstream cannot forge it): `{creditId, kind, txId, merchantId, creditSompi, balanceSompi, reason,
  shortfallSompi?}`, sent only when something was credited.
* Buyer view: `POST /v1/prepaid/credits/challenge` `{slug, payer_address}` then signed
  `POST /v1/prepaid/credits/status` `{nonce, signature}` (action `credits`; same challenge store, message layout,
  Purpose, key binding, single-use consume and rate limit as refunds/status). Reply is
  `{merchant_id, credited_sompi, credit_balance_sompi, refundable_sompi, credit_count, credits}` with the newest
  10 credits as `{tx_id, slug, credit_sompi, shortfall_sompi, reason, created_at}`. A buyer with no credits gets
  zeros and `credits: []`. A refunds `status` challenge cannot be replayed here, and the reverse is refused.
  `GET` and signed `POST /v1/prepaid/refunds/status` stay bare JSON arrays of refunds.

## Webhooks

Signed like every KasGate webhook (`X-KasGate-Signature`), with deterministic event ids from the ledger entry id.

* `prepaid.credit.failed_delivery` (when `credit_sompi > 0`): `ledger_entry_id`, `kind`, `settlement_id`,
  `tx_id`, `payment_scheme`, `endpoint_id`, `endpoint_slug`, `payer_address`, `amount_sompi`, `fee_sompi`,
  `credit_sompi`, `shortfall_sompi`, `balance_sompi`, `reason`, `delivery_failure`, `upstream_status_code`,
  `delivery_attempts`, `settled_at`.
* `prepaid.credit.failed_delivery_shortfall` (when `shortfall_sompi > 0`): the same fields plus
  `merchant_share_sompi` and `max_balance_sompi`.

## Data model (migration `0009_failed_delivery_credits`, after `0008_prepaid_refunds`)

* `merchants`: `failed_delivery_credit_enabled_at` (NOT NULL, default now()), `failed_delivery_credit_disabled_at`,
  `failed_delivery_credit_timeouts` (default true).
* `settlements`: `delivery_outcome`, `delivery_failure` (set iff failed/credited), `failed_delivery_credit_policy`
  (`errors|errors_and_timeouts`, NULL = opted out), `pending_alerted_at`, with CHECKs; partial indexes
  `ix_settlements_undelivered` and `ix_settlements_stale_pending`. Existing settlements keep NULL and are never
  credited.
* `prepaid_balances`: `credited_sompi`, `credit_balance_sompi`; the identity CHECK includes `credited_sompi`.
* `prepaid_debits.credit_sompi` (`BETWEEN 0 AND amount_sompi`).
* `prepaid_ledger_entries`: kind `failed_delivery_credit`; `settlement_id` (FK RESTRICT, unique), `slug`,
  `credit_reason`, `shortfall_sompi` (credit rows only). The amount CHECK is 0008's exactly plus a fully clamped
  credit row (`amount 0, shortfall > 0`). Unique partial index on `tx_id` for credit rows.
* `webhook_events` event-type CHECK gains both events.
* Downgrade refuses while any credit row, credited balance, credit-paid debit, credit event or webhook subscribed
  to one exists.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `UPSTREAM_TIMEOUT_SECONDS` | 30 (max 120) | One upstream attempt; also sizes the sweep wait and the delivery lock. |
| `FAILED_DELIVERY_CREDIT_GRACE_SECONDS` | 30 (1-3600) | Extra wait after window + upstream timeout before the sweep credits. |
| `FAILED_DELIVERY_CREDIT_LOOKBACK_SECONDS` | 604800 (1 h-90 d) | The sweep only looks at settlements this recent. |

The sweep runs in the ledger worker (`LEDGER_WORKER_ENABLED`, `LEDGER_POLL_INTERVAL_SECONDS`, batches of
`LEDGER_BATCH_SIZE`). With the worker off, only `attempts_exhausted` credits happen.

## Out of scope

* No per-endpoint override of the failed-delivery policy (merchant-level only).
* No other public credit-lookup route; the buyer's view is the signed `POST /v1/prepaid/credits/status` above.
  SDK and MCP reading of `X-KasGate-Credit` and of that endpoint is 05's follow-up.
