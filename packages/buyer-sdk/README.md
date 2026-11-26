# kasgate-client

Buyer-side SDK for KasGate (x402 `kaspa-exact-v1`). `KasGateClient` wraps `httpx.AsyncClient`; on HTTP 402 it validates the
`PAYMENT-REQUIRED` challenge, enforces the client-side spend cap, builds and signs one Kaspa transaction
(`outputs[0]` = merchant, `outputs[1]` = platform treasury, change last) and retries with `PAYMENT-SIGNATURE`.

```python
from kasgate_client import KasGateClient, receipt_of

async with KasGateClient(PRIVATE_KEY_HEX, network="kaspa:testnet-10", max_price_sompi=1_000_000_000) as client:
    response = await client.get("http://localhost:8000/p/my-endpoint", max_price_kas="8")
    print(response.status_code, receipt_of(response))
```

`KasGateSyncClient` has the same API for blocking code. See `tests/` for the offline suite and
`../../tests/integration` for the live gateway suite.

## CONTRACT v2.x behaviour

- **Fee split.** `fee = max(ceil(total * 150 / 10_000), min_output_sompi)`, `merchant = total - fee`, and the merchant
  output must be at least `2 * min_output_sompi`. The floor comes from the gateway manifest (`/v1/directory`) with the
  contract default of 20_000_000 as fallback, so the minimum price is 60_000_000 sompi (0.6 KAS).
- **Nonce binding.** The signed transaction carries the payload `b"kasgate:v1:" + nonce`
  (`kasgate_client.payment_payload`) and `PAYMENT-SIGNATURE` echoes the nonce. An expired challenge triggers exactly
  one re-challenge (`payment_expired`); nothing is spent in between.
- **Inputs.** Change is either 0 or at least 30_000_000 sompi; the builder adds UTXOs rather than emit small change and
  raises `DustChangeError` (an `InsufficientFundsError`) instead of donating it to the miner. At most 16 distinct input
  addresses are used.
- **Payer proof (v2.6).** `PAYMENT-SIGNATURE` carries `payerProof`, `kaspa.sign_message("kasgate:v1:<nonce>:<txId>", key)`
  as 128 hex (`kasgate_client.payer_proof_message`), so a mempool observer cannot present your signed transaction as
  their own. The SDK self-verifies the proof before sending.
- **Known limitation.** Two equal UTXOs that each almost cover the payment (for example `[60.3M, 60.3M]` at the minimum
  price) raise `TransactionBuildError`: `kaspa.Generator` cannot be made to spend both without a non-zero priority fee.
  The SDK fails closed rather than donate to the miner; any extra UTXO resolves it.


## Catalog discovery

`KasGateClient.discover(gateway_url)` (and the same method on `KasGateSyncClient`) fetches the public catalog
(`GET /v1/catalog`, or `GET /.well-known/x402` when `well_known=True`). It never pays or signs. Pagination is
followed by default; pass `follow_pages=False` and `cursor` to walk pages yourself. Endpoints priced above the
client's `max_price_sompi` circuit breaker stay in `items` and are listed separately on `exceeding_max_price`;
`payable` is the subset the client would be willing to sign for.

```python
async with KasGateClient(PRIVATE_KEY_HEX, max_price_sompi=100_000_000) as client:
    found = await client.discover("http://localhost:8000", query="weather")
    for item in found.payable:
        print(item.slug, item.price_sompi, item.paid_url)
```

## Prepaid credit (CONTRACT v2.11)

Deposit once on-chain, then pay each call with one signed, single-use debit: no transaction, no UTXO per call, and
prices below the 0.6 KAS on-chain minimum become possible.

```python
async with KasGateClient(PRIVATE_KEY_HEX, max_price_kas="0.01") as client:
    receipt = await client.prepaid_deposit("https://gw.example", "weather", 100_000_000)  # 1 KAS total
    # credit = merchant share (output 0); fee = max(1.5 %, 0.2 KAS) to the treasury (output 1)
    response = await client.prepaid_request("GET", "https://gw.example/p/weather/today?city=hanoi")
    print(prepaid_receipt_of(response))   # debitId, nonce, amountSompi, balanceSompi, status
    print(await client.prepaid_balance("https://gw.example", "weather"))
```

- `prepaid_deposit` validates the quote exactly like a per-call challenge (outputs, fee floor, pinned fee_to,
  `expected_pay_to`, expiry); `amount_sompi` is the cap for that one payment and must be at least 0.6 KAS.
- `prepaid_request` asks `POST /v1/prepaid/challenge`, rebuilds the message from its own request (domain, payer,
  method, path + query, SHA-256 of the body, amount, nonce, expiry) and refuses to sign unless the server text matches
  byte for byte and the amount is within the cap. Nothing is retried automatically: a refunded failure (no upstream
  response or 5xx) comes back with a `refunded` receipt.
- Errors: `InsufficientPrepaidBalanceError` (402 insufficient_balance, nothing debited), `PaymentReplayedError`
  (409, nonce already used), `InvalidChallengeError` / `PriceCapExceededError` (nothing signed).
- The balance belongs to the depositing key at that merchant; use the same key for deposits and debits.

Refunds of unused credit (CONTRACT v2.13):

```python
refund = await client.prepaid_refund_request("https://gw.example", "weather")            # all spendable credit
refund = await client.prepaid_refund_request("https://gw.example", "weather", 25_000_000)  # or an amount (>= 0.2 KAS)
print(await client.prepaid_refunds("https://gw.example", "weather"))   # signed read; requested -> paying -> payout_submitted -> paid
print(await client.prepaid_refunds("https://gw.example", "weather", signed=True))  # also the merchant's reasons
await client.prepaid_refund_cancel("https://gw.example", "weather", refund.id)  # only while `requested`, no payout pending
```

- The request signs a refund text (purpose `kasgate:v1:prepaid-refund`) that the SDK rebuilds itself; it refuses to sign
  for another payer, endpoint, action, refund or amount. When no amount is given, it signs the amount the challenge
  names (the gateway's refundable default). The unsigned challenge does not report `spendable_sompi` or
  `refundable_sompi`; those live on the signed credits/status and on `prepaid_balance`. The signature moves no coins:
  the merchant pays the amount back on-chain to this key's address, with no platform fee.
- `GET /v1/prepaid/refunds/status` is a buyer-signed read (the gateway answers unsigned calls with 401): the SDK sends
  `X-KasGate-Balance-Timestamp` + `X-KasGate-Balance-Signature` over the balance-read text (purpose
  `kasgate:v1:prepaid-balance-read`, this domain, address, slug and unix time), the same read signature as
  `prepaid_balance`; it authorises nothing else. That list hides the merchant's reject reason and claim (`paying`
  reads as `requested`); `signed=True` signs a single-use `status` challenge (amount 0) instead to read them
  (`claimed_at`, `claim_expires_at`). A refund cancelled after the merchant had
  claimed it keeps its amount set aside until `hold_until`; a cancel is refused while a payout of it is on-chain.
  `late_paid_from` / `shortfall_sompi` show a payout that landed after a cancel or reject.
- `prepaid_balance` is a signed read by default (same headers as above) and reports `balance_sompi`, the ledger totals
  (`deposited`, `debited`, `refunded`, `expired`, `refund_hold_sompi`), `updated_at`, `spendable_sompi` and
  `expires_at`. `prepaid_balance(..., signed=False)` is the unsigned public read, which carries only `spendable_sompi`
  and `expires_at`; every other field of `PrepaidBalance` is then `None`. Read `spendable_sompi` for what calls can pay;
  nothing in the SDK falls back to `balance_sompi`. A balance for another payer address is refused.

## Failed-delivery credits (CONTRACT v2.14)

When a paid call fails for good (the upstream answered 5xx or not at all) and redelivery is closed, the gateway turns the
merchant share of that payment into prepaid credit and announces it in the `X-KasGate-Credit` response header. The
header is **unsigned**: the SDK treats it as a hint and never reports a credit as real until the gateway's **signed**
credit status lists it. (The gateway no longer sends a credit summary header on every response; only this hint remains.)

```python
from kasgate_client import credit_of

response = await client.request("GET", "https://gw.example/p/weather/today")   # 502 after payment
observation = credit_of(response)             # None when there is no header
observation.status                            # "hint": parsed, NOT checked
observation = await client.confirm_credit("https://gw.example", "weather", observation)
observation.status                            # "confirmed" | "unconfirmed"
observation.confirmation.basis                # "credit_id" | "increase" | "none"

# or let the client check by itself (signed status pages, only when a hint arrives):
client = KasGateClient(key, auto_confirm_credit=True, credit_page_budget=10)

# the signed status itself, any time (moves no coins, one fresh challenge per page):
status = await client.credit_status("https://gw.example", "weather", limit=100)   # BuyerCreditStatus
status.credited_sompi, status.credit_balance_sompi, status.refundable_sompi, status.credit_count   # count: all pages
[(c.credit_id, c.tx_id, c.credit_sompi, c.shortfall_sompi, c.reason) for c in status.credits]   # newest first
older = await client.credit_status("https://gw.example", "weather", before=status.next_cursor)  # None on the last page
```

- `credit_of(response)` returns a frozen, strict `CreditObservation(status, hint, error, confirmation)`, or `None` without
  the header. `status` is `hint` (parsed, unchecked), `confirmed`, `unconfirmed` (the signed status does not list it, or
  could not be read) or `invalid` (malformed, larger than 2 KiB, not a 5xx, or inconsistent with the payment it came
  with: another `txId`, an amount other than the merchant share, a balance below the credit). Nothing in the header can
  raise.
- `credit_status(gateway_url, slug, *, before=None, limit=None)` reads one page with the signed flow:
  `POST /v1/prepaid/credits/challenge {slug, payer_address}`, then `POST /v1/prepaid/credits/status {nonce, signature,
  before?, limit?}` (the body is the strict `CreditStatusRequest`: `limit` 1..100, gateway default 10; `before` the
  previous page's `next_cursor`). Challenges are single use, so every page asks for a new one. The challenge is checked before anything is signed (action
  `credits`, this payer and slug, amount `0`, no refund id, not expired, and the text rebuilt byte for byte from the
  fields); a refunds `status` challenge is refused. The reply is parsed into the strict `BuyerCreditStatus` (extra
  fields rejected, sompi are decimal strings read as ints, at most 100 `credits`, each with its `credit_id`, no
  credit_id twice, `next_cursor` required and null on the last page). Errors raise the SDK's typed errors
  (`PaymentRejectedError` carries the gateway's `status_code` / `error` such as `rate_limited` or `invalid_cursor`;
  replay `409` and expiry `410` have their own classes). The challenge's `spendable_sompi` / `refundable_sompi` are
  informational and optional. A key with no balance gets zeros and `credits == []`.
- `confirm_credit(gateway_url, slug, credit, *, baseline_credited_sompi=None)` walks the signed status 100 credits per
  page (a fresh challenge each, at most `credit_page_budget` pages, default 10, restart and re-walk included) until it
  meets the hinted credit. It says `confirmed` (`basis="credit_id"`) only when a credit's `credit_id` is the header's
  `creditId` and its `tx_id`, amount, reason and slug match the hint; the hinted `creditId` with other details, or the
  hinted `tx_id` under another `credit_id`, is `unconfirmed`. A 400 `invalid_cursor` restarts from the newest page once;
  a full walk that listed fewer credits than `credit_count` is walked again once. Only when the walk could not see every
  credit (budget spent, count still higher) can a baseline confirm it with the weaker `basis="increase"` (`credited_sompi`
  grew by at least the credit). `confirmation.pages_read` says how many pages were read. It never raises for gateway
  problems: they become `unconfirmed` with `confirmation.http_status`, `error_code` and a generic `detail`.
- Keys and signatures are never logged or put in an error.
- A credit is call-only prepaid credit at one merchant: it is not refundable and it is not spend that came back, so it
  must not be subtracted from a spend total. `KasGateSyncClient.credit_status` / `confirm_credit` are the blocking twins.

## Free trial calls (CONTRACT v2.12)

Merchants can give every buyer key N free calls per endpoint. `request()` uses them automatically before paying:
a 402 that carries `X-KasGate-Trial-Offer` is answered with ONE signed trial call; when the trial is used up the
gateway's normal 402 comes back and is paid as usual.

```python
async with KasGateClient(PRIVATE_KEY_HEX) as client:              # use_free_trials=True is the default
    response = await client.get("https://gw.example/p/weather")    # free while the trial lasts, then paid
    print(trial_receipt_of(response))   # trialCallId, nonce, limit, remaining, status (used | refunded)
    print(await client.trial_status("https://gw.example", "weather"))
    response = await client.trial_request("GET", "https://gw.example/p/weather")  # trial only, never pays
```

- The SDK asks `POST /v1/trials/challenge`, rebuilds the text from its own request (domain, payer, endpoint, method,
  path + query, body hash, nonce, expiry) and refuses to sign unless it matches byte for byte. The text carries no
  amount and its Purpose (`kasgate:v1:trial-call`) differs from every payment signature, so it can never move coins.
- A failed upstream (no response or 5xx) gives the trial call back (`refunded` receipt); nothing is retried.
- `trial_request` raises `PaymentRejectedError` with code `trial_exhausted` when no trial call is left.
- Pass `use_free_trials=False` to always pay.

## Verifying merchant webhooks (CONTRACT v2.12)

```python
from kasgate_client import verify_webhook_signature

ok = verify_webhook_signature(WEBHOOK_SECRET, request.headers["X-KasGate-Signature"], raw_body_bytes)
```

The header is `t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>`; deliveries older than 300 s are refused.
Verify the RAW body before parsing JSON and dedupe on `X-KasGate-Event-Id`.
