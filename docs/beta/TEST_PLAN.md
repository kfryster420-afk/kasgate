# KasGate public testnet beta — tester plan

This is the set of scenarios we want external testers to run against the public KasGate beta on **Kaspa testnet-10**. It is not an internal CI suite. Use the web app with KasWare, or the buyer SDK / MCP server, against the beta gateway URL you were given.

KasGate is testnet-verified only. Mainnet has not launched. Use throw-away testnet wallets and never send mainnet funds.

**Never paste seed phrases, private keys, wallet recovery words, API keys (`kg_…`, `kgs_…`), or `.env` contents into an issue, chat, screenshot, or log paste.** A transaction id (`txId`) is safe to share.

## Before you start

- A Kaspa testnet-10 wallet (KasWare is the supported browser wallet) with enough spendable balance for several payments of at least 0.6 KAS plus network fees and change.
- Access to the beta web app and gateway. Paid endpoints look like `https://<gateway>/p/<slug>`.
- A way to inspect a transaction: `https://explorer-tn10.kaspa.org/txs/<txid>`.
- About 15–20 minutes, including one wait of just over two minutes for the expired-nonce case (challenges last `PAYMENT_MAX_AGE_SECONDS`, default 120 seconds; use the `expiresAt` in the challenge you received).

If you are a merchant tester, also register with KasWare, create at least one paid endpoint priced at the 0.6 KAS minimum, and note the merchant address shown in the dashboard.

## What to record for every scenario

Write these down as you go. If something fails, file a [bug report](https://github.com/kfryster420-afk/kasgate/issues/new?template=bug_report.yml) with the same fields. Report security problems only through [private vulnerability reporting](https://github.com/kfryster420-afk/kasgate/security/advisories/new), not a public issue (see [SECURITY.md](https://github.com/kfryster420-afk/kasgate/blob/main/SECURITY.md)).

- Scenario name and whether it passed or failed
- Role (buyer or merchant) and network (`kaspa:testnet-10`)
- Endpoint slug or paid URL
- HTTP status and, on errors, the `error` field from the JSON body
- `txId` when a transaction was built or broadcast, plus the explorer link
- For a 402 challenge, the decoded `PAYMENT-REQUIRED` JSON: `amount`, `payTo`, `platformFee.feeTo`, `platformFee.feeAmount`, `nonce`, `expiresAt` (no keys)
- For a success, the decoded `PAYMENT-RESPONSE` JSON: `txId`, `settled`, `network`
- Browser name and version, and KasWare version (or buyer SDK / MCP server package version)
- Step where it diverged, expected vs actual, and whether funds left the wallet
- Relevant log lines with secrets removed

## Fee split (applies to every paid call)

Every valid payment, through the KasGate SDK or a k402 client (scenario 8), is a single Kaspa transaction with this output order:

| Output | Recipient | Amount |
| --- | --- | --- |
| 0 | Merchant (`payTo` from `PAYMENT-REQUIRED`) | price minus the platform fee |
| 1 | Treasury (`platformFee.feeTo`) | the platform fee, exact |
| 2+ | Change back to the buyer, if any | either omitted (zero change) or at least 0.3 KAS (30,000,000 sompi) |

The fee is `max(ceil(price × 150 / 10,000), 0.2 KAS)`. The minimum endpoint price is **0.6 KAS** (60,000,000 sompi). At that price the split is **0.4 KAS to the merchant and 0.2 KAS to the treasury**. Amounts in the challenge headers are sompi strings (1 KAS = 100,000,000 sompi). For `PAYMENT-SIGNATURE` payments the transaction payload must be the bytes `kasgate:v1:` plus the challenge `nonce`; k402 payments have an empty payload.

## Scenarios

### 1. Happy path: pay, then retry

Call a paid endpoint with no payment header.

- Expected: HTTP **402**, `error` is `payment_required`, and a `PAYMENT-REQUIRED` header. The body also carries the challenge. Nothing is spent.

Build and sign one payment that matches that challenge (correct `payTo`, treasury, amounts, payload nonce, and payer proof if you are using the SDK or a wallet flow that attaches it). Retry the same request with a `PAYMENT-SIGNATURE` header.

- Expected: the upstream response (typically HTTP **200**), a `PAYMENT-RESPONSE` header with `settled: true` and a `txId`, and the same `txId` visible on the testnet-10 explorer and, for the merchant, in the dashboard settlements list. Settlement should complete in a few seconds.

If the first paid attempt returns **502** `upstream_error` but still includes `PAYMENT-RESPONSE`, the payment already settled. Retry with the **same** `PAYMENT-SIGNATURE` (do not build a second transaction) within the redelivery window (default 10 minutes). Expected: the upstream is tried again and you are not charged a second time. A later retry after the window, or after the delivery attempt limit, should return **409** without a new on-chain payment.

### 2. Underpay

Take a fresh 402 challenge and submit a signed payment whose total is below the `amount` in `PAYMENT-REQUIRED` (one sompi short is enough), or whose treasury output is below `platformFee.feeAmount`.

- Expected: HTTP **402**, `error` is `underpaid` or `invalid_fee_split`, and a fresh `PAYMENT-REQUIRED`. The underpaying transaction must not settle the call. Note whether the bad transaction was broadcast and whether funds moved.

### 3. Wrong recipient

Take a fresh challenge and pay the correct total, but send output 0 or output 1 to the wrong address (swap merchant and treasury, or substitute any other testnet address).

- Expected: HTTP **402**, `error` is `invalid_fee_split` (or `invalid_payment_header` if the header itself is rejected), and a fresh challenge. The call must not settle. Record the `txId` if one was broadcast and confirm the intended merchant was not credited for this call.

### 4. Replayed txId

Complete scenario 1 so you have a settled `txId` and the `PAYMENT-SIGNATURE` you already used. Submit that same signature again on a new request to the same endpoint (or reuse the same `txId` in a new signature).

- Expected: HTTP **409**, `error` is `payment_replayed`. No second settlement, no second credit to the merchant, and no second treasury payment.

### 5. Expired nonce

Request a challenge and **do not pay it**. Wait until after the `expiresAt` timestamp in `PAYMENT-REQUIRED` (default lifetime is 120 seconds). Then submit a otherwise-valid payment that still uses that expired `nonce`.

- Expected: HTTP **402**, `error` is `payment_expired`, and a **fresh** `PAYMENT-REQUIRED` with a new `nonce` and `expiresAt`. The expired nonce must not be reusable. If you already broadcast a transaction for the stale nonce, say so and include its `txId`; the call itself must still be unpaid.

### 6. Minimum price 0.6 KAS

As a merchant, try to create or update an endpoint priced below 0.6 KAS (for example 0.5 KAS).

- Expected: the request is rejected. The endpoint is not saved at that price.

Then create (or use) an endpoint priced at exactly 0.6 KAS and complete a valid payment as in scenario 1.

- Expected: the payment settles. On the explorer, output 0 is 40,000,000 sompi (0.4 KAS) to the merchant and output 1 is 20,000,000 sompi (0.2 KAS) to the treasury.

### 7. Fee split: output 0 merchant, output 1 treasury

Using the successful 0.6 KAS payment from scenario 6 (or another successful payment if you already have one), open `https://explorer-tn10.kaspa.org/txs/<txid>` and check the outputs against the challenge that was paid.

- Expected: output 0 is the merchant `payTo` address for the exact merchant amount; output 1 is the treasury `feeTo` address for the exact fee; any further output is change to the buyer and is either absent or at least 0.3 KAS. The dashboard settlement, if you are the merchant, shows the same `txId` and amounts.

### 8. k402 client (`kaspa-utxo`), fee still enforced

For testers who use the [k402](https://github.com/kaspa-402/k402) Python library instead of the KasGate SDK. KasGate does not support `kaspa-session` or `kaspa-channel`.

Call a paid endpoint with no payment header and read the JSON body.

- Expected: HTTP **402** with `"k402": "0.2"` and an `accepts` list. `accepts[0]` is the KasGate `kaspa-exact-v1` terms (the same as `PAYMENT-REQUIRED`). `accepts[1]` has `scheme: "kaspa-utxo"`, `network: "testnet-10"` (not `kaspa:testnet-10`), `pay_to` = the merchant, `amount_sompi` = the merchant share, `facilitator_fee.to` = the treasury and `facilitator_fee.sompi` = the fee, a `payment_id` (`p_` + 16 hex) and `expires`. `amount_sompi + facilitator_fee.sompi` is the endpoint price plus a random tag under 0.01 KAS (1,000,000 sompi), and the fee is `max(ceil(total × 1.5 %), 0.2 KAS)` of that total. Record the whole `accepts[1]` object (it contains no secrets).

Pay with `k402.Client(payer=...)` and an unmodified `kaspa-utxo` payer. **Known upstream issue:** with `kaspa==2.1.0`, `k402.wallet.HotWallet(key, network="testnet-10")` fails in its constructor with `Invalid network type: testnet-10`. Report it if you see it, then use a payer that does what `HotWallet.pay` does: `create_transactions(network_id="testnet-10", outputs=[PaymentOutput(pay_to, amount_sompi), PaymentOutput(facilitator_fee.to, facilitator_fee.sompi)], change_address=<your address>)`, sign, submit, and return the txid. Never paste the key into a report.

- Expected: the client retries with `X-K402-Payment: kaspa-utxo <txid> <payment_id>` and gets the upstream response with a `PAYMENT-RESPONSE` header. It may get one or two `402 payment_not_found` first while the node sees the transaction; the client retries those on its own. On the explorer, output 0 pays the merchant exactly `amount_sompi`, output 1 pays the treasury exactly `facilitator_fee.sompi`, and the payload is empty. The merchant dashboard shows the settlement "via k402" with no payer address.

Then try to get the call without paying the fee. Each needs a fresh offer and is a separate small payment:

- Pay only output 0 (no treasury output), or the fee 1 sompi short, or the fee to another address, or the two outputs in swapped order. Expected: **402** `invalid_fee_split` every time and no upstream response, even though the merchant output is correct.
- Replay a settled header, the same `txid` with a new `payment_id`, or that `txid` in a KasGate `PAYMENT-SIGNATURE`. Expected: **409** `payment_replayed`.
- Send `X-K402-Payment` empty, with two or four tokens, or with `kaspa-session`. Expected: **402** `invalid_payment_header`.

### 9. kaspa-x402 client (`exact` / `kaspa-exact-v2`), no platform fee

Use the official `@kaspa-x402/client` 1.0.1 on testnet-10 with a funded testnet wallet as its funding provider, pointed at your endpoint's `/p/<slug>` URL. If you cannot run Node, skip this scenario and say so in your summary.

- Decode the 402's `PAYMENT-REQUIRED` header. Expected: `x402Version` 2, `resource.url` equal to the exact URL you called, `accepts[0]` with `scheme` `exact`, `extra.binding` `kaspa-exact-v2`, `extra.profile` `standard-native`, `payTo` the merchant and `amount` the full price, and `accepts[1]` the `kaspa-exact-v1` terms. The KasGate SDK and scenario 1 still work against the same endpoint.
- Pay it with the client. Expected: the upstream response, and a `PAYMENT-RESPONSE` whose `transaction` is your txId, `amount` is the full price, `extensions.kaspa.finality` is `accepted` and `extensions.kasgate.platformFeeSompi` is `"0"`. On the explorer, one output pays the merchant exactly the price, any second output is your change, there is no treasury output and the payload is empty. The dashboard shows the settlement with a fee of 0.
- Retry the same paid request with the same payment identifier after an upstream 5xx. Expected: redelivered without a second payment. Reuse the identifier for a different URL or body. Expected: **409** `payment_replayed` (`kaspa_payment_identifier_conflict`).
- Change the body or query after signing, or send the payment to another endpoint. Expected: **402** `invalid_exact_payment` or `payment_expired`, and nothing is served.
- Replay the settled txId in a KasGate `PAYMENT-SIGNATURE` or an `X-K402-Payment` header. Expected: **409** `payment_replayed`.
- On a USD-priced endpoint, compare the `exact` amount with the `kaspa-exact-v1` `amount` from the same 402. Expected: they are equal.

### 10. Prepaid credit (`kasgate-prepaid-v1`)

Merchant: create one endpoint with **Prepaid only** on and a price below the minimum, e.g. 0.001 KAS. Buyer: use the buyer SDK (`prepaid_deposit`, `prepaid_request`, `prepaid_balance`) or the MCP tools (`prepaid_deposit`, `call_prepaid_endpoint`, `prepaid_balance`).

- Call the prepaid-only endpoint without paying. Expected: **402** with no `PAYMENT-REQUIRED` header and `accepts` holding exactly one `kasgate-prepaid-v1` offer (`amount` the price in sompi, `prepaidOnly: true`, `minDepositSompi` `"60000000"`). The catalog lists only `kasgate-prepaid-v1` for it.
- Ask for a debit challenge before depositing. Expected: **402** `insufficient_balance`, and nothing is debited.
- Deposit 1 KAS. Expected: one transaction with output 0 = 0.8 KAS to the merchant and output 1 = 0.2 KAS to the treasury, payload `kasgate:v1:` plus the quote nonce; the receipt shows `credit_sompi` 80000000 and the balance read shows 0.8 KAS. Re-posting the same deposit returns the same receipt (200); using its txId to pay a normal `/p` call is **409** `payment_replayed`.
- Make a prepaid call. Expected: the upstream response and an `X-KasGate-Prepaid-Receipt` with `status` `debited`, `amountSompi` the price and `balanceSompi` reduced by it; no new transaction on the explorer. Send the same `X-KasGate-Prepaid` header again. Expected: **409** `payment_replayed` and the balance unchanged.
- Sign a challenge, then change the query string or body before sending. Expected: **402** `invalid_payment_header` and nothing debited.
- If you can make the upstream fail (5xx or offline), make one prepaid call. Expected: the receipt says `refunded` and the balance is back where it was.
- Merchant: the dashboard "Prepaid credit" panel shows the buyer's balance, the deposit (with credit and fee) and each debit.

## When you are done

File one bug report per distinct failure, using the [bug report form](https://github.com/kfryster420-afk/kasgate/issues/new?template=bug_report.yml). If every scenario passed, you can open a single issue summarising the run (which scenarios, network, one example `txId`, browser and KasWare versions) or send that summary to whoever invited you to the beta.

Product ideas go in a [feature request](https://github.com/kfryster420-afk/kasgate/issues/new?template=feature_request.yml). Overall impressions, confusing steps, and suggestions can go in a [beta feedback](https://github.com/kfryster420-afk/kasgate/issues/new?template=beta_feedback.yml) report. Payment-verification bypasses, replay holes, key leaks, and similar issues go only to private vulnerability reporting.
