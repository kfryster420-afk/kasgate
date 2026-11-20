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
