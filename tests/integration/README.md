# KasGate integration & red-team suite

```
tests/integration/
  conftest.py                    fixtures (gateway probe, merchant/endpoint registration, funded payer, UTXO pool)
  txforge.py                     low-level tx forge that bypasses every SDK safety check (malicious txs)
  test_offline_engine.py         gateway KaspaEngine.verify vs SDK-built and forged txs (no services)
  test_offline_contract_probes.py  schema probes; xfail(strict) = documented gap that asserts the secure behaviour
  test_offline_auth.py           wallet-login primitives (challenge format, sign_message verification, Base64 sigs)
  test_live_catalog.py           402 challenge, registration rules, floor 422, tax CSV, directory/quote   [gateway]
  test_auth.py                   wallet login round trip, replay, unknown address, Base64, rotate-key   [gateway]
  test_live_payments.py          happy path, replay race x10, tampering, SDK end-to-end              [gateway + funds]
  test_zz_rate_limits.py         429 behaviour, marker `ratelimit`, opt-in                              [gateway, special config]
```

Offline unit tests for the SDK live in `packages/buyer-sdk/tests`, for the MCP server in `packages/mcp-server/tests`.

## Install and run

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r tests/integration/requirements.txt -e packages/mcp-server
cd tests/integration && pytest -rs          # offline tests always run; live tests skip with a reason
```

## Gateway configuration for this suite

All host traffic reaches the gateway from one Docker bridge IP, and the default limits (5 merchant sign-ups per hour,
10 auth calls per minute) would starve the suite. **Run the gateway with `RATE_LIMIT_ENABLED=false` in `infra/.env`**
(owned by 01; this suite never edits it). Everything else uses the defaults in `infra/.env.example`.

| variable | meaning |
| --- | --- |
| `KASGATE_GATEWAY_URL` | gateway base URL, default `http://localhost:8000` |
| `KASGATE_TEST_PAYER_KEY` | hex private key of a funded **kaspa:testnet-10** address. Without it the funded tests skip |
| `KASGATE_TEST_RPC_URL` | wRPC URL for the test client's UTXO lookups (default: public resolver) |
| `KASGATE_TEST_UPSTREAM_URL` | public upstream the gateway proxies to, default `https://httpbin.org/anything` |
| `KASGATE_TEST_RPC_DOWN=1` | only when the gateway runs with an unreachable node: enables the 503 `rpc_unavailable` test |
| `KASGATE_RATE_LIMIT_ENABLED=true` | mirror of the gateway's `RATE_LIMIT_ENABLED=true`; enables the `ratelimit` tests |

Live payment tests pay a throw-away merchant address on testnet: three real payments of one minimum price
(~13.34 KAS) plus miner fees. Every tampered transaction is rejected before it reaches the node. The suite refuses to
run payments on mainnet.

## Rate-limit (429) tests

They are excluded by default (`addopts = -m "not ratelimit"`) and skip unless `KASGATE_RATE_LIMIT_ENABLED=true`.
Run them separately against a gateway started with limiting on and low limits (names from `infra/.env.example`):

```bash
# infra/.env
RATE_LIMIT_ENABLED=true
RL_MERCHANT_CREATE_PER_HOUR=3
RL_AUTH_CHALLENGE_PER_MINUTE=4
RL_AUTH_VERIFY_PER_MINUTE=4

# tests
KASGATE_RATE_LIMIT_ENABLED=true RL_MERCHANT_CREATE_PER_HOUR=3 RL_AUTH_CHALLENGE_PER_MINUTE=4 \
RL_AUTH_VERIFY_PER_MINUTE=4 pytest -m ratelimit
```

The test environment must mirror the gateway's `RL_*` values (read through the gateway's own `Settings`). Each run
exhausts the caller IP's budget for the window: clear the `kasgate:rl:*` Redis keys or restart Redis between runs, and
never run the normal suite against that low-limit configuration.

## Reading the results

`xfail(strict=True)` tests assert the *secure* behaviour and currently fail on purpose: each one is tagged with a
`GAP-n` id from the red-team report. When the gateway fixes a gap the test XPASSes and, being strict, fails the run so
the marker gets removed.
