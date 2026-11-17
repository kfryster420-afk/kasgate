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
