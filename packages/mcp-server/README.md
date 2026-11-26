# kasgate-mcp

Stdio MCP server for KasGate. Tools:

| tool | purpose |
| --- | --- |
| `list_directory` | `GET /v1/directory`: the public catalog of paid endpoints. |
| `search_catalog(query, limit, cursor)` | `GET /v1/catalog`: search listed endpoints by slug, name, or description. |
| `inspect_endpoint(slug)` | `GET /v1/catalog/{slug}`: one listed endpoint (price, methods, paid URL, input schema). |
| `quote_endpoint(slug)` | `GET /v1/quote/{slug}`: price and fee split, nothing is paid. |
| `get_budget` | session cap, per-call maximum, spent, reserved, and remaining, in sompi. |
| `call_with_budget(slug, method, path, body, max_price_kas)` | pays a 402 under the session spend cap: the catalog price is reserved before signing, released on failure, and never exceeds the per-call max or the remaining cap. |
| `call_paid_endpoint(slug, method, path, body, max_price_kas)` | calls `/p/{slug}/{path}` and pays the 402 challenge, refusing anything above the cap. Does not draw down the session cap. |
| `prepaid_deposit(slug, amount_kas)` | CONTRACT v2.11: funds a prepaid balance at the endpoint's merchant with one on-chain payment (>= 0.6 KAS; merchant share = credit, fee max(1.5 %, 0.2 KAS) to the treasury). Reserved against the session cap and per-call max before signing, committed on success. |
| `prepaid_balance(slug)` | this key's prepaid balance at that merchant (read only); `credited_sompi` is the cumulative failed-delivery credit. |
| `call_prepaid_endpoint(slug, method, path, body, max_price_kas)` | calls `/p/{slug}/{path}` paid by one signed debit from the prepaid balance (no transaction). Refuses to sign unless the debit text matches the request and the amount is within the cap; returns the debit receipt (`debited` or `refunded`). Does not draw the session cap again. |
| `prepaid_refund_request(slug, amount_kas?)` | CONTRACT v2.13: asks the merchant to pay unused credit back to this key's address (all spendable credit when `amount_kas` is omitted; at least 0.2 KAS). Signs one refund request (checked against what was asked), never a transaction; does not touch the session cap. |
| `prepaid_refund_cancel(slug, refund_id)` | withdraws a refund the merchant has not paid yet; the held credit becomes spendable again. |
| `prepaid_refunds(slug)` | this key's refund requests at that merchant, newest first (read only). |
| `trial_status(slug)` | CONTRACT v2.12: how many free trial calls this buyer key has left on the endpoint. Signs nothing. |
| `call_free_trial(slug, method, path, body)` | makes ONE call from the endpoint's free trial and never pays (fails with `trial_exhausted` when none is left). The signed trial text is checked against the request and carries no amount. A failed upstream gives the trial back (`refunded`). The paying tools also use a free trial automatically before paying: see `trial` in their result. |

## Environment

| variable | meaning |
| --- | --- |
| `KASGATE_BUYER_PRIVATE_KEY_FILE` | **Preferred.** Path of a file holding the buyer's 64-character hex private key (`~` is expanded, surrounding whitespace and the trailing newline are stripped). Wins over `KASGATE_BUYER_PRIVATE_KEY` when both are set (a warning is logged to stderr, never the key). The file must be a regular file that is not readable by group or others: create it with `umask 077` or `chmod 600`, otherwise the server refuses to start. |
| `KASGATE_ALLOW_INSECURE_KEY_FILE` | `1`/`true`/`yes`/`on` downgrades the permission refusal to a warning on stderr. Only for setups where file modes are meaningless (some mounted volumes). |
| `KASGATE_BUYER_PRIVATE_KEY` | The key inline, as hex. Convenient but visible in the process environment and in MCP client config files; prefer the file. Without either variable the server still lists and quotes, but paid calls are disabled. |
| `KASGATE_GATEWAY_URL` | Gateway base URL, default `http://localhost:8000`. |
| `KASGATE_NETWORK` | `kaspa:testnet-10` (default) or `kaspa:mainnet`. |
| `KASGATE_RPC_URL` | Optional wRPC URL for UTXO lookups (default: the public resolver). |
| `KASGATE_MAX_PRICE_KAS` | Hard ceiling per call, default `20`; the `max_price_kas` tool argument can only lower it. |
| `KASGATE_SESSION_BUDGET_KAS` | Session spend cap for `call_with_budget`, default `20`. Concurrent calls reserve the quoted price atomically and cannot together exceed this cap. |
| `KASGATE_REQUEST_TIMEOUT` | Seconds the server waits for a gateway response (including the payment settlement), default `60`. Must be a positive number or the server exits at startup. |
| `KASGATE_AUTO_CONFIRM_CREDIT` | `1` (default) checks an `X-KasGate-Credit` hint against the public `/v1/prepaid/balance` before the paid-call tools report it as confirmed; `0` leaves it an unverified hint. A credit is never reported as confirmed without that check. |
| `KASGATE_USE_FREE_TRIALS` | `1` (default) lets the paying tools answer a 402 that offers a free trial with a signed trial call before paying; `0` always pays. Trial calls never count against the session budget. |

## Failed-delivery credits (CONTRACT v2.14)

`call_paid_endpoint` and `call_with_budget` add a `credit` field when the gateway sends `X-KasGate-Credit` (a 5xx after
payment whose merchant share became call-only prepaid credit); it is `null` otherwise. The header is unsigned, so:

| `credit.status` | meaning |
| --- | --- |
| `confirmed` | the public balance shows the credit (`confirmed: true`); only then are `credit_sompi` / `credit_kas` set at the top of `credit` |
| `hint` | parsed but not checked (auto-confirm off): the amounts are only under `credit.hint` and `confirmed` is `false` |
| `unconfirmed` | checked and the balance does not show it: do not rely on it |
| `invalid` | malformed, oversized or inconsistent with the payment: ignored, `credit.error` says why |

The credit is never subtracted from the session budget: the payment stays spent (`spent_sompi` is unchanged) because the
credit can only be used for more calls at that merchant and cannot be refunded.

```json
{"status_code": 502, "credit": {"status": "confirmed", "confirmed": true, "credit_sompi": 1477500000, "credit_kas": "14.775",
 "hint": {"credit_id": "…", "tx_id": "…", "merchant_id": "…", "credit_sompi": 1477500000, "reason": "attempts_exhausted"},
 "confirmation": {"basis": "cumulative", "credited_sompi": 1477500000, "baseline_credited_sompi": null, "detail": "…"},
 "error": null, "note": "Confirmed against the public prepaid balance. This is call-only credit … not refundable …"}}
```

The key's contents are never logged, never part of an error message and not shown in `repr(Config)`. A missing, malformed
(not exactly 64 hex characters), binary or non-regular key file makes the server exit with a message that names the path
only.

```json
{"mcpServers": {"kasgate": {"command": "kasgate-mcp", "env": {"KASGATE_BUYER_PRIVATE_KEY_FILE": "/home/me/.config/kasgate/buyer.key", "KASGATE_GATEWAY_URL": "http://localhost:8000"}}}}
```

Works with mcp 2.x (`MCPServer`) and falls back to mcp 1.x (`FastMCP`).
