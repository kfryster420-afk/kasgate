# KasGate integration & red-team suite

```
tests/integration/
  conftest.py                    fixtures (gateway probe, merchant/endpoint registration, funded payer, UTXO pool)
  txforge.py                     low-level tx forge that bypasses every SDK safety check (malicious txs, 17-address txs)
  wallet.py                      wallet-proof helpers (challenge, register, login) for the auth flows
  test_offline_engine.py         gateway KaspaEngine.verify vs SDK-built and forged txs (no services)
  test_offline_contract_probes.py  schema probes; xfail(strict) = documented gap that asserts the secure behaviour
  test_offline_auth.py           wallet-login primitives (challenge format, sign_message verification, Base64 sigs)
  test_offline_frontrun.py       front-running closed (v2.6): front-runner variants against the real engine's payerProof check
  test_offline_secrets.py        the funded key loads from a file and can never be rendered as text
  test_live_catalog.py           402 challenge, registration rules, floor 422, tax CSV, directory/quote   [gateway]
  test_auth.py                   wallet register/login, replay, Base64, rotate-key, DELETE /v1/auth/session   [gateway]
  test_live_payments.py          tampering, nonce binding, payerProof front-running, 17-address cap [gateway]; settlement, replay race, SDK, redelivery [gateway + funds]
  test_zz_rate_limits.py         429 behaviour, marker `ratelimit`, opt-in                              [gateway, special config]
```

Offline unit tests for the SDK live in `packages/buyer-sdk/tests`, for the MCP server in `packages/mcp-server/tests`.

## Install and run

```bash
python -m venv .venv && . .venv/bin/activate
# from the repository root:
pip install -e packages/buyer-sdk -e packages/mcp-server -r tests/integration/requirements.txt
pytest -rs tests/integration          # offline tests always run; live tests skip with a reason
```

## Gateway configuration for this suite

All host traffic reaches the gateway from one Docker bridge IP, and the default limits (5 merchant sign-ups per hour,
10 auth calls per minute) would starve the suite. **Run the gateway with `RATE_LIMIT_ENABLED=false` in `infra/.env`**
(this suite never edits it). Everything else uses the defaults in `infra/.env.example`.

| variable | meaning |
| --- | --- |
| `KASGATE_GATEWAY_URL` | gateway base URL, default `http://localhost:8000` |
| `KASGATE_TEST_PAYER_KEY_FILE` | file holding the funded payer's hex key (no default). The key is never printed or logged (`SecretKey` masks it); don't assert on it directly |
| `KASGATE_TEST_PAYER_KEY` | the same key inline; wins over the file. With neither variable set, the funded tests skip with a reason naming both |
| `KASGATE_TEST_RPC_URL` | wRPC URL for the test client's UTXO lookups (default: public resolver) |
| `KASGATE_TEST_UPSTREAM_URL` | public upstream the gateway proxies to, default `https://httpbin.org/anything` |
| `KASGATE_TEST_FAILING_UPSTREAM_URL` | upstream that answers 5xx or refuses, default `https://httpbin.org/status/503` |
| `KASGATE_TEST_TXLOG` | optional JSONL file: one line per presented payment (txId, status, latency) |
| `KASGATE_TEST_RPC_DOWN=1` | only when the gateway runs with an unreachable node: enables the 503 `rpc_unavailable` test |
| `KASGATE_TEST_REDIS_DOWN=1` | only when the gateway's Redis is stopped: enables the 503 test for `DELETE /v1/auth/session` |
| `KASGATE_TEST_SLOW=1` | enables the test that waits for a payment challenge to expire (`payment_expired`) |
| `KASGATE_RATE_LIMIT_ENABLED=true` | mirror of the gateway's `RATE_LIMIT_ENABLED=true`; enables the `ratelimit` tests |

Two tiers of live payment tests (`test_live_payments.py`):

* **No funds / no node.** Tampered outputs, the nonce and payload binding, `payment_expired`, and the 16-address cap
  all spend made-up UTXOs and must be refused before the gateway asks the node, so they run against any gateway.
* **Funded.** Real settlements at the minimum price (CONTRACT v2.3: **0.6 KAS** = 60_000_000 sompi, split 40M merchant
  / 20M treasury) plus miner fees, paid to a throw-away merchant, and the cases only the node can judge. They skip when
  the gateway reports `kaspa_rpc=false` or no key is configured. The suite refuses to run payments on mainnet.

Inputs follow CONTRACT v2.3: change must be 0 or >= 0.3 KAS, and a payment may draw on at most 16 payer addresses.

## Running against a local gateway without a node

A local launcher (not part of this repository) can start the gateway on embedded Postgres + fakeredis with the Kaspa
engine stubbed out. It reports `kaspa_rpc=false`: catalog, auth, nonce and tampering tests run, the funded ones skip.

## Rate-limit (429) tests

They are excluded by default (`addopts = -m "not ratelimit"`) and skip unless `KASGATE_RATE_LIMIT_ENABLED=true`.
Run them separately against a gateway started with limiting on and low limits (names from `infra/.env.example`):

```bash
# infra/.env
RATE_LIMIT_ENABLED=true   # limits are per caller IP only
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

`xfail(strict=True)` tests assert the *secure* behaviour and currently fail on purpose: each one documents a
finding from security testing. When the gateway fixes it the test XPASSes and, being strict, fails the run so
the marker gets removed.

## Funded runs from a sandboxed host

The gateway's SSRF guard resolves the upstream host itself and refuses non-public addresses. A sandbox whose DNS answers
with a fake range (for example `198.18.0.0/15`) therefore makes every hostname upstream fail with 502 AFTER the payment
settled. Point the suite at a literal public IP instead, e.g. `KASGATE_TEST_UPSTREAM_URL=https://1.1.1.1/cdn-cgi/trace`.
The `test_small_change_at_the_minimum_price...` probe needs a payer UTXO of about 1 KAS or less (it skips otherwise).
