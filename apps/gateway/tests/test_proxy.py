"""The HTTP 402 handshake, nonce binding, Redis replay protection, redelivery and forwarding of /p/{slug}."""

import asyncio
import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from kaspa import PrivateKey
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.kaspa_engine import RpcUnavailableError
from app.models import Endpoint, Merchant, Settlement, SettlementStatus
from app.proxy import (
    NONCE_KEY_PREFIX,
    REPLAY_LOCK_PREFIX,
    REPLAY_LOCK_TTL_SECONDS,
    build_upstream_url,
    lock_key,
    nonce_key,
    nonce_lock_key,
)
from app.schemas import (
    HEADER_PAYMENT_REQUIRED,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
    PaymentRequired,
    PaymentResponse,
    PaymentSignature,
    X402ErrorBody,
    compute_fee_split,
)
from tests.conftest import (
    MERCHANT_ADDRESS,
    OTHER_ADDRESS,
    OTHER_KEY,
    PAYER_ADDRESS,
    PAYER_KEY,
    PRICE_SOMPI,
    TREASURY,
    UPSTREAM_SECRET,
    Seed,
    SettingsOverride,
    StubEngine,
    StubOracle,
    Upstream,
    fetch_challenge,
    make_signature_header,
    paid_headers,
    payer_proof,
    seed_endpoint,
)

TX = "ab" * 32
SPLIT = compute_fee_split(PRICE_SOMPI)


async def settlements(maker: async_sessionmaker[AsyncSession]) -> list[Settlement]:
    async with maker() as session:
        return list((await session.execute(select(Settlement).order_by(Settlement.created_at))).scalars())


async def pay(client: httpx.AsyncClient, tx_id: str = TX, slug: str = "weather") -> dict[str, str]:
    return await paid_headers(client, tx_id, slug)


def signature_header(tx_id: str, nonce: str, payer_key: PrivateKey = PAYER_KEY, proof: str | None = None) -> str:
    return make_signature_header(tx_id, nonce, payer_key=payer_key, proof=proof)


def nonce_of(headers: dict[str, str]) -> str:
    return PaymentSignature.from_header(headers[HEADER_PAYMENT_SIGNATURE]).nonce


async def nonce_keys(redis: Any) -> list[str]:
    return sorted(await redis.keys(f"{NONCE_KEY_PREFIX}*"))


# ---------------------------------------------------------------- 404 / 405


async def test_unknown_slug_is_404(client: httpx.AsyncClient, seeded: Seed) -> None:
    response = await client.get("/p/nope")
    assert response.status_code == 404
    assert response.json()["error"] == "endpoint_not_found"
    assert HEADER_PAYMENT_REQUIRED not in response.headers


async def test_inactive_endpoint_is_404(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await seed_endpoint(sessionmaker, slug="retired", active=False)
    assert (await client.get("/p/retired")).status_code == 404


async def test_method_not_allowed_is_405_before_payment(client: httpx.AsyncClient, seeded: Seed) -> None:
    response = await client.delete("/p/weather", headers={HEADER_PAYMENT_SIGNATURE: signature_header(TX, "a" * 32)})
    assert response.status_code == 405
    assert response.json()["error"] == "method_not_allowed"
    assert response.headers["allow"] == "GET, POST"


# ---------------------------------------------------------------- 402 challenge and nonce storage


async def test_missing_signature_returns_402_with_nonce_bound_challenge(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, upstream: Upstream, redis: Any
) -> None:
    before = datetime.now(UTC)
    response = await client.get("/p/weather")
    assert response.status_code == 402
    required = PaymentRequired.from_header(response.headers[HEADER_PAYMENT_REQUIRED])
    assert required.amount == str(PRICE_SOMPI)
    assert required.payTo == MERCHANT_ADDRESS
    assert required.platformFee.feeTo == TREASURY
    assert int(required.platformFee.feeAmount) == SPLIT.fee_sompi == 30_000_000  # 1.5 %
    assert required.merchant_amount_sompi == SPLIT.merchant_sompi == 1_970_000_000  # 98.5 %
    assert len(required.nonce) == 32
    assert before + timedelta(seconds=115) <= required.expiresAt <= before + timedelta(seconds=125)
    body = response.json()
    assert body["error"] == "payment_required"
    assert body["accepts"][0]["nonce"] == required.nonce
    assert body["k402"] == "0.2" and body["accepts"][1]["scheme"] == "kaspa-utxo"
    assert stub_engine.calls == [] and stub_engine.verify_calls == []
    assert upstream.requests == []


async def test_challenge_is_stored_under_its_nonce_with_the_slug_and_ttl(
    client: httpx.AsyncClient, seeded: Seed, redis: Any
) -> None:
    assert NONCE_KEY_PREFIX == "kasgate:nonce:"
    required = await fetch_challenge(client)
    key = f"kasgate:nonce:{required.nonce}"
    assert key == nonce_key(required.nonce)
    stored = json.loads(await redis.get(key))
    assert stored["slug"] == "weather"
    assert PaymentRequired.model_validate(stored["required"]) == required
    assert 0 < await redis.ttl(key) <= 120


async def test_every_challenge_has_a_fresh_nonce(client: httpx.AsyncClient, seeded: Seed, redis: Any) -> None:
    nonces = {(await fetch_challenge(client)).nonce for _ in range(5)}
    assert len(nonces) == 5
    assert len(await nonce_keys(redis)) == 5


async def test_challenge_ttl_follows_payment_max_age(
    client: httpx.AsyncClient, seeded: Seed, redis: Any, override_settings: SettingsOverride
) -> None:
    override_settings(payment_max_age_seconds=30)
    required = await fetch_challenge(client)
    assert 0 < await redis.ttl(nonce_key(required.nonce)) <= 30
    assert required.expiresAt <= datetime.now(UTC) + timedelta(seconds=31)


@pytest.mark.parametrize(
    "header",
    [
        "not base64 !!!",
        base64.b64encode(b"not json").decode(),
        base64.b64encode(json.dumps({"txId": "abc"}).encode()).decode(),
        base64.b64encode(
            json.dumps({"txId": "AB" * 32, "rawTx": "{}", "payerAddress": "nope", "nonce": "a" * 32}).encode()
        ).decode(),
        base64.b64encode(  # a v1 header without a nonce
            json.dumps({"txId": TX, "rawTx": "{}", "payerAddress": PAYER_ADDRESS}).encode()
        ).decode(),
    ],
)
async def test_bad_signature_header_returns_402_invalid_payment_header_with_fresh_challenge(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any, header: str
) -> None:
    response = await client.get("/p/weather", headers={HEADER_PAYMENT_SIGNATURE: header})
    assert response.status_code == 402
    assert response.json()["error"] == "invalid_payment_header"
    required = PaymentRequired.from_header(response.headers[HEADER_PAYMENT_REQUIRED])
    assert await redis.exists(nonce_key(required.nonce)) == 1
    assert stub_engine.calls == [] and stub_engine.verify_calls == []
    assert await redis.keys("kasgate:tx:*") == []


async def test_wrong_network_address_in_signature_is_402_wrong_network(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine
) -> None:
    mainnet_payer = PrivateKey("07" * 32).to_address("mainnet").to_string()
    header = base64.b64encode(
        json.dumps(
            {
                "txId": TX,
                "rawTx": "{}",
                "payerAddress": mainnet_payer,
                "nonce": "a" * 32,
                "payerProof": payer_proof("a" * 32, TX, PrivateKey("07" * 32)),
            }
        ).encode()
    ).decode()
    response = await client.get("/p/weather", headers={HEADER_PAYMENT_SIGNATURE: header})
    assert response.status_code == 402
    assert response.json()["error"] == "wrong_network"
    PaymentRequired.from_header(response.headers[HEADER_PAYMENT_REQUIRED])
    assert stub_engine.calls == []


async def test_wrong_payer_proof_is_402_invalid_payment_header_and_takes_no_locks(
    client: httpx.AsyncClient,
    seeded: Seed,
    stub_engine: StubEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Any,
    upstream: Upstream,
) -> None:
    """CONTRACT v2.6: `engine.verify` rejects a proof the payer key did not sign; the proxy just relays that."""
    required = await fetch_challenge(client)
    wrong_proofs = {
        "signed by another key": payer_proof(required.nonce, TX, OTHER_KEY),
        "signed for another txId": payer_proof(required.nonce, "cd" * 32),
        "signed for another nonce": payer_proof("b" * 32, TX),
    }
    for label, proof in wrong_proofs.items():
        header = make_signature_header(TX, required.nonce, payer_key=PAYER_KEY, proof=proof)
        response = await client.get("/p/weather", headers={HEADER_PAYMENT_SIGNATURE: header})
        assert response.status_code == 402, label
        assert response.json()["error"] == "invalid_payment_header", label
        PaymentRequired.from_header(response.headers[HEADER_PAYMENT_REQUIRED])  # a fresh challenge to retry with
        assert await redis.keys("kasgate:tx:*") == [], label
        assert await redis.keys("kasgate:noncelock:*") == [], label
        assert await settlements(sessionmaker) == [], label
        assert stub_engine.resolve_calls == [] and stub_engine.calls == [], label  # never reached the node
        assert upstream.requests == [], label
    assert stub_engine.verify_calls == [TX] * len(wrong_proofs)
    # the original challenge is untouched, and the right proof still pays with it
    assert await redis.exists(nonce_key(required.nonce)) == 1
    paid = {HEADER_PAYMENT_SIGNATURE: make_signature_header(TX, required.nonce)}
    assert (await client.get("/p/weather", headers=paid)).status_code == 200


async def test_payment_signature_without_payer_proof_is_402_invalid_payment_header(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any
) -> None:
    required = await fetch_challenge(client)
    header = base64.b64encode(
        json.dumps({"txId": TX, "rawTx": "{}", "payerAddress": PAYER_ADDRESS, "nonce": required.nonce}).encode()
    ).decode()
    response = await client.get("/p/weather", headers={HEADER_PAYMENT_SIGNATURE: header})
    assert response.status_code == 402
    assert response.json()["error"] == "invalid_payment_header"
    assert stub_engine.verify_calls == [] and await redis.keys("kasgate:tx:*") == []


# ---------------------------------------------------------------- nonce binding on the paid retry


async def test_missing_nonce_is_402_payment_expired_with_a_fresh_challenge(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any
) -> None:
    unknown = "f" * 32
    response = await client.get("/p/weather", headers={HEADER_PAYMENT_SIGNATURE: signature_header(TX, unknown)})
    assert response.status_code == 402
    assert response.json()["error"] == "payment_expired"
    fresh = PaymentRequired.from_header(response.headers[HEADER_PAYMENT_REQUIRED])
    assert fresh.nonce != unknown
    assert await redis.exists(nonce_key(fresh.nonce)) == 1
    assert stub_engine.calls == [] and stub_engine.verify_calls == []
    assert await redis.keys("kasgate:tx:*") == []


async def test_nonce_that_expired_in_redis_is_payment_expired(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any
) -> None:
    headers = await pay(client)
    await redis.delete(nonce_key(nonce_of(headers)))
    response = await client.get("/p/weather", headers=headers)
    assert response.status_code == 402
    assert response.json()["error"] == "payment_expired"
    assert stub_engine.calls == []


async def test_nonce_past_its_expires_at_is_payment_expired_even_if_redis_still_has_it(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any
) -> None:
    headers = await pay(client)
    key = nonce_key(nonce_of(headers))
    stored = json.loads(await redis.get(key))
    stored["required"]["expiresAt"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await redis.set(key, json.dumps(stored), ex=60)
    response = await client.get("/p/weather", headers=headers)
    assert response.status_code == 402
    assert response.json()["error"] == "payment_expired"
    assert stub_engine.calls == []


async def test_corrupt_stored_challenge_is_payment_expired(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any
) -> None:
    headers = await pay(client)
    await redis.set(nonce_key(nonce_of(headers)), "{not json", ex=60)
    response = await client.get("/p/weather", headers=headers)
    assert response.status_code == 402
    assert response.json()["error"] == "payment_expired"


async def test_nonce_issued_for_another_endpoint_is_rejected(
    client: httpx.AsyncClient,
    seeded: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    stub_engine: StubEngine,
    redis: Any,
) -> None:
    await seed_endpoint(sessionmaker, slug="other", address=OTHER_ADDRESS)
    headers = await pay(client, slug="other")
    response = await client.get("/p/weather", headers=headers)
    assert response.status_code == 402
    assert response.json()["error"] == "invalid_payment_header"
    assert "different endpoint" in response.json()["message"]
    assert stub_engine.calls == [] and stub_engine.verify_calls == []
    assert await redis.exists(nonce_key(nonce_of(headers))) == 1


async def test_paid_retry_uses_the_issued_challenge_not_the_current_price(
    client: httpx.AsyncClient,
    seeded: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    stub_engine: StubEngine,
) -> None:
    headers = await pay(client)
    new_price = 3_000_000_000
    async with sessionmaker() as session:
        await session.execute(update(Endpoint).values(price_sompi=new_price))
        await session.commit()
    assert (await client.get("/p/weather", headers=headers)).status_code == 200
    row = (await settlements(sessionmaker))[0]
    assert row.amount_sompi == PRICE_SOMPI
    assert row.merchant_amount_sompi == SPLIT.merchant_sompi


# ---------------------------------------------------------------- the paid retry


async def test_valid_payment_is_forwarded_and_settled(
    client: httpx.AsyncClient,
    seeded: Seed,
    stub_engine: StubEngine,
    upstream: Upstream,
    redis: Any,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    headers = await pay(client)
    nonce = nonce_of(headers)
    response = await client.get(
        "/p/weather/forecast/paris?units=metric&q=a%20b",
        headers={
            **headers,
            "Cookie": "tracking=1",
            "X-Forwarded-For": "203.0.113.9",
            "Authorization": "Bearer agent-token",
            "Accept": "application/json",
            "X-Custom": "kept",
        },
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert response.headers["x-upstream"] == "yes"
    assert "set-cookie" not in response.headers

    paid = PaymentResponse.from_header(response.headers[HEADER_PAYMENT_RESPONSE])
    assert paid.txId == TX
    assert paid.settled is True
    assert paid.network == "kaspa:testnet-10"
    assert paid.usdValueAtSettlement == Decimal("2.50000000")  # 20 KAS * 0.125

    assert stub_engine.verify_calls == [TX]
    assert stub_engine.calls == [TX]
    assert len(upstream.requests) == 1
    sent = upstream.requests[0]
    assert sent.method == "GET"
    assert str(sent.url) == "https://api.upstream.test/v1/forecast/paris?units=metric&q=a%20b"
    assert sent.headers["authorization"] == UPSTREAM_SECRET
    assert sent.headers["x-custom"] == "kept"
    assert "cookie" not in sent.headers
    assert "x-forwarded-for" not in sent.headers
    assert not any(name.lower().startswith("payment-") for name in sent.headers)

    rows = await settlements(sessionmaker)
    assert len(rows) == 1
    row = rows[0]
    assert row.tx_id == TX
    assert row.payment_nonce == nonce
    assert row.delivery_attempts == 1
    assert row.status is SettlementStatus.SETTLED
    assert row.upstream_status_code == 200
    assert row.amount_sompi == PRICE_SOMPI
    assert row.merchant_amount_sompi == SPLIT.merchant_sompi
    assert row.fee_amount_sompi == SPLIT.fee_sompi
    assert row.merchant_address == MERCHANT_ADDRESS
    assert row.treasury_address == TREASURY
    assert row.kas_usd_rate == Decimal("0.125")
    assert row.usd_value_at_settlement == Decimal("2.5")
    assert row.request_path == "/p/weather/forecast/paris"
    assert row.settled_at is not None
    assert await redis.get(lock_key(TX)) is not None
    assert await redis.get(nonce_key(nonce)) is None  # consumed after settlement


async def test_post_body_and_content_type_are_forwarded(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream
) -> None:
    response = await client.post("/p/weather", json={"city": "Oslo"}, headers=await pay(client))
    assert response.status_code == 200
    sent = upstream.requests[0]
    assert sent.method == "POST"
    assert json.loads(sent.content) == {"city": "Oslo"}
    assert sent.headers["content-type"] == "application/json"
    assert str(sent.url) == "https://api.upstream.test/v1"


async def test_upstream_response_cannot_spoof_payment_headers(client: httpx.AsyncClient, seeded: Seed) -> None:
    response = await client.get("/p/weather", headers=await pay(client))
    PaymentResponse.from_header(response.headers[HEADER_PAYMENT_RESPONSE])


async def test_upstream_error_status_is_passed_through(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    upstream.handler = lambda request: httpx.Response(418, text="teapot")
    response = await client.get("/p/weather", headers=await pay(client))
    assert response.status_code == 418
    assert response.text == "teapot"
    assert HEADER_PAYMENT_RESPONSE in response.headers
    assert (await settlements(sessionmaker))[0].upstream_status_code == 418


async def test_upstream_redirects_are_not_followed(client: httpx.AsyncClient, seeded: Seed, upstream: Upstream) -> None:
    upstream.handler = lambda request: httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"})
    response = await client.get("/p/weather", headers=await pay(client))
    assert response.status_code == 302
    assert len(upstream.requests) == 1


@pytest.mark.parametrize("suffix", ["%2e%2e/admin", "a/%2E%2E/%2e%2e/b", "%2e/x", "..%2fadmin"])
async def test_path_traversal_is_rejected_before_payment(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, upstream: Upstream, suffix: str
) -> None:
    response = await client.get(f"/p/weather/{suffix}", headers=await pay(client))
    assert response.status_code == 400
    assert stub_engine.calls == [] and stub_engine.verify_calls == []
    assert upstream.requests == []


def test_upstream_url_building_keeps_authority_and_merges_queries() -> None:
    url = build_upstream_url("https://api.example.com/v1/?key=1", "a b/c", "x=2")
    assert str(url) == "https://api.example.com/v1/a%20b/c?key=1&x=2"
    assert str(build_upstream_url("https://api.example.com", "", "")) == "https://api.example.com/"
    hostile = build_upstream_url("https://api.example.com/v1", "//evil.test/x", "")
    assert hostile.host == "api.example.com"


async def test_oracle_failure_still_settles_without_usd_value(
    client: httpx.AsyncClient,
    seeded: Seed,
    stub_oracle: StubOracle,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    stub_oracle.rate = None
    response = await client.get("/p/weather", headers=await pay(client))
    assert response.status_code == 200
    assert PaymentResponse.from_header(response.headers[HEADER_PAYMENT_RESPONSE]).usdValueAtSettlement == 0
    row = (await settlements(sessionmaker))[0]
    assert row.kas_usd_rate is None
    assert row.usd_value_at_settlement is None


# ---------------------------------------------------------------- lock ordering (owner rule)


async def test_pure_verify_runs_before_any_lock_and_the_tx_lock_is_held_before_submit_and_forwarding(
    client: httpx.AsyncClient,
    seeded: Seed,
    stub_engine: StubEngine,
    upstream: Upstream,
    redis: Any,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    seen: dict[str, Any] = {}

    async def at_submit(signature: PaymentSignature) -> None:
        seen["tx_lock"] = await redis.get(lock_key(signature.txId))
        seen["tx_lock_ttl"] = await redis.ttl(lock_key(signature.txId))
        seen["nonce_still_stored"] = await redis.exists(nonce_key(signature.nonce))
        seen["upstream_calls"] = len(upstream.requests)
        seen["settlements"] = len(await settlements(sessionmaker))

    def at_verify(signature: PaymentSignature) -> None:
        seen["events_at_verify"] = list(stub_engine.events)

    async def at_resolve(signature: PaymentSignature) -> None:
        seen["tx_lock_at_resolve"] = await redis.get(lock_key(signature.txId))
        seen["nonce_lock_at_resolve"] = await redis.get(nonce_lock_key(signature.nonce))
        seen["upstream_at_resolve"] = len(upstream.requests)

    stub_engine.before_submit = at_submit
    stub_engine.before_resolve = at_resolve
    stub_engine.on_verify = at_verify
    headers = await pay(client)
    # Not yet locked when the pure verify runs: checked from the outside with a direct Redis read afterwards.
    assert await redis.keys("kasgate:tx:*") == []
    assert (await client.get("/p/weather", headers=headers)).status_code == 200
    assert stub_engine.events == ["verify", "resolve_inputs", "settle"]
    assert seen["events_at_verify"] == ["verify"]
    assert seen["tx_lock_at_resolve"] == "1"  # even the node lookups run under the lock
    assert seen["nonce_lock_at_resolve"] == TX
    assert seen["upstream_at_resolve"] == 0
    assert seen["tx_lock"] == "1"
    assert 0 < seen["tx_lock_ttl"] <= REPLAY_LOCK_TTL_SECONDS
    assert seen["nonce_still_stored"] == 1  # the nonce is consumed only after the settlement is stored
    assert seen["upstream_calls"] == 0
    assert seen["settlements"] == 0


async def test_pure_verify_failure_never_touches_a_lock_and_keeps_the_nonce(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any, upstream: Upstream
) -> None:
    stub_engine.fail_verify("underpaid", "short")
    headers = await pay(client)
    response = await client.get("/p/weather", headers=headers)
    assert response.status_code == 402
    assert response.json()["error"] == "underpaid"
    assert PaymentRequired.from_header(response.headers[HEADER_PAYMENT_REQUIRED]).nonce == nonce_of(headers)
    assert stub_engine.calls == []
    assert await redis.keys("kasgate:tx:*") == []
    assert await redis.keys("kasgate:noncelock:*") == []
    assert await redis.exists(nonce_key(nonce_of(headers))) == 1
    assert upstream.requests == []

    stub_engine.verify_error = None
    assert (await client.get("/p/weather", headers=headers)).status_code == 200


# ---------------------------------------------------------------- replay protection


async def test_lock_uses_documented_key_and_ttl(client: httpx.AsyncClient, seeded: Seed, redis: Any) -> None:
    assert REPLAY_LOCK_PREFIX == "kasgate:tx:"
    assert REPLAY_LOCK_TTL_SECONDS == 600
    headers = await pay(client)
    await client.get("/p/weather", headers=headers)
    assert 0 < await redis.ttl(f"kasgate:tx:{TX}") <= 600
    assert 0 < await redis.ttl(nonce_lock_key(nonce_of(headers))) <= 600


async def test_second_use_of_a_transaction_is_409(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, upstream: Upstream
) -> None:
    first = await pay(client)
    assert (await client.get("/p/weather", headers=first)).status_code == 200
    second = await client.get("/p/weather", headers=await pay(client))  # same tx, fresh nonce
    assert second.status_code == 409
    assert second.json()["error"] == "payment_replayed"
    assert HEADER_PAYMENT_RESPONSE not in second.headers
    assert stub_engine.calls == [TX]
    assert len(upstream.requests) == 1


async def test_consumed_nonce_cannot_be_reused_with_another_transaction(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine
) -> None:
    headers = await pay(client, "11" * 32)
    assert (await client.get("/p/weather", headers=headers)).status_code == 200
    reuse = {HEADER_PAYMENT_SIGNATURE: signature_header("22" * 32, nonce_of(headers))}
    response = await client.get("/p/weather", headers=reuse)
    assert response.status_code == 409
    assert response.json()["error"] == "payment_replayed"
    assert stub_engine.calls == ["11" * 32]


async def test_replay_after_lock_expiry_is_caught_by_settlement_row(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any
) -> None:
    assert (await client.get("/p/weather", headers=await pay(client))).status_code == 200
    await redis.delete(lock_key(TX))
    second = await client.get("/p/weather", headers=await pay(client))
    assert second.status_code == 409
    assert second.json()["error"] == "payment_replayed"
    assert stub_engine.calls == [TX]


async def test_preheld_lock_blocks_the_request_and_is_left_alone(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any
) -> None:
    await redis.set(lock_key(TX), "1", nx=True, ex=600)
    headers = await pay(client)
    response = await client.get("/p/weather", headers=headers)
    assert response.status_code == 409
    assert stub_engine.calls == []
    assert await redis.get(lock_key(TX)) == "1"
    assert await redis.get(nonce_lock_key(nonce_of(headers))) is None  # our own claim is given back
    assert await redis.exists(nonce_key(nonce_of(headers))) == 1


async def test_preheld_nonce_claim_blocks_a_different_transaction(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any
) -> None:
    headers = await pay(client)
    await redis.set(nonce_lock_key(nonce_of(headers)), "someone-else", nx=True, ex=600)
    response = await client.get("/p/weather", headers=headers)
    assert response.status_code == 409
    assert stub_engine.calls == []
    assert await redis.get(lock_key(TX)) is None


async def test_concurrent_duplicates_only_one_succeeds(
    client: httpx.AsyncClient,
    seeded: Seed,
    stub_engine: StubEngine,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    stub_engine.delay = 0.05
    headers = await pay(client)
    responses = await asyncio.gather(*(client.get("/p/weather", headers=headers) for _ in range(12)))
    codes = sorted(r.status_code for r in responses)
    assert codes == [200] + [409] * 11
    assert stub_engine.calls == [TX]
    assert len(upstream.requests) == 1
    assert len(await settlements(sessionmaker)) == 1


async def test_concurrent_different_transactions_on_one_nonce_submit_only_one(
    client: httpx.AsyncClient,
    seeded: Seed,
    stub_engine: StubEngine,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    stub_engine.delay = 0.05
    nonce = (await fetch_challenge(client)).nonce
    tx_ids = [f"{i:064x}" for i in range(1, 7)]
    responses = await asyncio.gather(
        *(client.get("/p/weather", headers={HEADER_PAYMENT_SIGNATURE: signature_header(tx, nonce)}) for tx in tx_ids)
    )
    assert sorted(r.status_code for r in responses) == [200] + [409] * 5
    assert len(stub_engine.calls) == 1
    assert len(upstream.requests) == 1
    assert len(await settlements(sessionmaker)) == 1


async def test_concurrent_distinct_transactions_all_succeed(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    stub_engine.delay = 0.02
    tx_ids = [f"{i:064x}" for i in range(1, 9)]
    all_headers = [await pay(client, tx) for tx in tx_ids]
    responses = await asyncio.gather(*(client.get("/p/weather", headers=h) for h in all_headers))
    assert [r.status_code for r in responses] == [200] * 8
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Settlement)) == 8


async def _inject_settlement(
    maker: async_sessionmaker[AsyncSession], seed: Seed, *, tx_id: str, nonce: str, **overrides: Any
) -> None:
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tx_id": tx_id,
        "endpoint_id": seed.endpoint_id,
        "merchant_id": seed.merchant_id,
        "network": "kaspa:testnet-10",
        "payer_address": PAYER_ADDRESS,
        "merchant_address": MERCHANT_ADDRESS,
        "treasury_address": TREASURY,
        "amount_sompi": PRICE_SOMPI,
        "merchant_amount_sompi": SPLIT.merchant_sompi,
        "fee_amount_sompi": SPLIT.fee_sompi,
        "status": SettlementStatus.SETTLED,
        "request_method": "GET",
        "request_path": "/p/weather",
        "payment_nonce": nonce,
        "created_at": now,
        "settled_at": now,
    }
    values.update(overrides)
    async with maker() as session:
        session.add(Settlement(**values))
        await session.commit()


async def test_unique_payment_nonce_race_is_409_and_nothing_is_forwarded(
    client: httpx.AsyncClient,
    seeded: Seed,
    stub_engine: StubEngine,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Any,
) -> None:
    """A racer stores a settlement for the same nonce after our pre-checks (e.g. after a Redis flush)."""
    headers = await pay(client)
    nonce = nonce_of(headers)

    async def racer_wins(signature: PaymentSignature) -> None:
        await _inject_settlement(sessionmaker, seeded, tx_id="99" * 32, nonce=nonce)

    stub_engine.before_submit = racer_wins
    response = await client.get("/p/weather", headers=headers)
    assert response.status_code == 409
    assert response.json()["error"] == "payment_replayed"
    assert upstream.requests == []
    rows = await settlements(sessionmaker)
    assert [r.tx_id for r in rows] == ["99" * 32]
    assert await redis.exists(nonce_key(nonce)) == 1  # not consumed by the loser


async def test_unique_tx_id_race_is_409_and_nothing_is_forwarded(
    client: httpx.AsyncClient,
    seeded: Seed,
    stub_engine: StubEngine,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    headers = await pay(client)

    async def racer_wins(signature: PaymentSignature) -> None:
        await _inject_settlement(sessionmaker, seeded, tx_id=TX, nonce="e" * 32)

    stub_engine.before_submit = racer_wins
    response = await client.get("/p/weather", headers=headers)
    assert response.status_code == 409
    assert upstream.requests == []
    assert len(await settlements(sessionmaker)) == 1


# ---------------------------------------------------------------- verification failures


@pytest.mark.parametrize(
    "code", ["underpaid", "invalid_fee_split", "wrong_network", "tx_rejected", "invalid_payment_header"]
)
async def test_verification_failure_is_402_releases_locks_and_keeps_the_nonce(
    client: httpx.AsyncClient,
    seeded: Seed,
    stub_engine: StubEngine,
    upstream: Upstream,
    redis: Any,
    sessionmaker: async_sessionmaker[AsyncSession],
    code: str,
) -> None:
    stub_engine.fail(code, "nope")
    headers = await pay(client)
    nonce = nonce_of(headers)
    response = await client.get("/p/weather", headers=headers)
    assert response.status_code == 402
    assert response.json()["error"] == code
    assert response.json()["message"] == "nope"
    assert PaymentRequired.from_header(response.headers[HEADER_PAYMENT_REQUIRED]).nonce == nonce
    assert await redis.get(lock_key(TX)) is None
    assert await redis.get(nonce_lock_key(nonce)) is None
    assert await redis.exists(nonce_key(nonce)) == 1
    assert upstream.requests == []
    assert await settlements(sessionmaker) == []

    stub_engine.error = None
    retry = await client.get("/p/weather", headers=headers)
    assert retry.status_code == 200
    assert await redis.get(lock_key(TX)) is not None
    assert await redis.exists(nonce_key(nonce)) == 0


@pytest.mark.parametrize("code", ["invalid_payment_header", "tx_rejected"])
async def test_input_resolution_failure_is_402_releases_locks_and_never_settles(
    client: httpx.AsyncClient,
    seeded: Seed,
    stub_engine: StubEngine,
    upstream: Upstream,
    redis: Any,
    code: str,
) -> None:
    stub_engine.fail_resolve(code, "inputs do not match the node")
    headers = await pay(client)
    response = await client.get("/p/weather", headers=headers)
    assert response.status_code == 402
    assert response.json()["error"] == code
    assert PaymentRequired.from_header(response.headers[HEADER_PAYMENT_REQUIRED]).nonce == nonce_of(headers)
    assert stub_engine.calls == []  # settle (the on-chain submission) was never reached
    assert stub_engine.events == ["verify", "resolve_inputs"]
    assert await redis.get(lock_key(TX)) is None
    assert await redis.get(nonce_lock_key(nonce_of(headers))) is None
    assert await redis.exists(nonce_key(nonce_of(headers))) == 1
    assert upstream.requests == []
    stub_engine.resolve_error = None
    assert (await client.get("/p/weather", headers=headers)).status_code == 200


async def test_node_unavailable_during_input_resolution_is_503_and_releases_locks(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any
) -> None:
    stub_engine.resolve_error = RpcUnavailableError("node down")
    headers = await pay(client)
    response = await client.get("/p/weather", headers=headers)
    assert response.status_code == 503
    assert response.json()["error"] == "rpc_unavailable"
    assert stub_engine.calls == []
    assert await redis.get(lock_key(TX)) is None
    assert await redis.get(nonce_lock_key(nonce_of(headers))) is None
    assert await redis.exists(nonce_key(nonce_of(headers))) == 1


async def test_unexpected_input_resolution_crash_is_503_and_releases_lock(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any
) -> None:
    stub_engine.resolve_error = RuntimeError("boom")
    response = await client.get("/p/weather", headers=await pay(client))
    assert response.status_code == 503
    assert response.json()["error"] == "service_unavailable"
    assert await redis.get(lock_key(TX)) is None


async def test_unknown_engine_code_maps_to_tx_rejected(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine
) -> None:
    stub_engine.fail("something_else")
    response = await client.get("/p/weather", headers=await pay(client))
    assert response.status_code == 402
    assert response.json()["error"] == "tx_rejected"


async def test_node_unavailable_is_503_releases_locks_and_keeps_the_nonce(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any
) -> None:
    stub_engine.error = RpcUnavailableError("node down")
    headers = await pay(client)
    response = await client.get("/p/weather", headers=headers)
    assert response.status_code == 503
    assert response.json()["error"] == "rpc_unavailable"
    X402ErrorBody.model_validate(response.json())
    assert response.headers["retry-after"] == "5"
    assert await redis.get(lock_key(TX)) is None
    assert await redis.get(nonce_lock_key(nonce_of(headers))) is None
    assert await redis.exists(nonce_key(nonce_of(headers))) == 1
    stub_engine.error = None
    assert (await client.get("/p/weather", headers=headers)).status_code == 200


async def test_unexpected_engine_crash_is_503_and_releases_lock(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any
) -> None:
    stub_engine.error = RuntimeError("boom")
    response = await client.get("/p/weather", headers=await pay(client))
    assert response.status_code == 503
    assert response.json()["error"] == "service_unavailable"
    assert response.headers["retry-after"] == "5"
    assert await redis.get(lock_key(TX)) is None


async def test_unexpected_pure_verify_crash_is_503(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any
) -> None:
    stub_engine.verify_error = RuntimeError("boom")
    response = await client.get("/p/weather", headers=await pay(client))
    assert response.status_code == 503
    assert response.json()["error"] == "service_unavailable"
    assert await redis.keys("kasgate:tx:*") == []


async def test_upstream_failure_is_502_keeps_settlement_and_lock(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    redis: Any,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    upstream.handler = boom
    response = await client.get("/p/weather", headers=await pay(client))
    assert response.status_code == 502
    assert response.json()["error"] == "upstream_error"
    assert PaymentResponse.from_header(response.headers[HEADER_PAYMENT_RESPONSE]).txId == TX
    rows = await settlements(sessionmaker)
    assert len(rows) == 1
    assert rows[0].status is SettlementStatus.SETTLED
    assert rows[0].upstream_status_code is None
    assert await redis.get(lock_key(TX)) is not None
    assert (await client.get("/p/weather", headers=await pay(client))).status_code == 409


async def test_upstream_timeout_is_502(client: httpx.AsyncClient, seeded: Seed, upstream: Upstream) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    upstream.handler = slow
    response = await client.get("/p/weather", headers=await pay(client))
    assert response.status_code == 502
    assert HEADER_PAYMENT_RESPONSE in response.headers


async def test_redis_outage_on_the_paid_retry_fails_closed(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = await pay(client)

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RedisConnectionError("down")

    monkeypatch.setattr(redis, "set", broken)
    response = await client.get("/p/weather", headers=headers)
    assert response.status_code == 503
    assert response.json()["error"] == "service_unavailable"
    assert response.headers["retry-after"] == "5"
    X402ErrorBody.model_validate(response.json())
    assert stub_engine.calls == []


async def test_redis_outage_when_issuing_a_challenge_is_503(
    client: httpx.AsyncClient, seeded: Seed, redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_set = redis.set

    async def broken_for_nonces(key: str, *args: Any, **kwargs: Any) -> Any:
        if key.startswith(NONCE_KEY_PREFIX):
            raise RedisConnectionError("down")
        return await real_set(key, *args, **kwargs)

    monkeypatch.setattr(redis, "set", broken_for_nonces)
    response = await client.get("/p/weather")
    assert response.status_code == 503
    assert response.json()["error"] == "service_unavailable"
    assert HEADER_PAYMENT_REQUIRED not in response.headers


async def test_inactive_merchant_endpoint_is_404(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    seed = await seed_endpoint(sessionmaker, slug="gone")
    async with sessionmaker() as session:
        await session.execute(update(Merchant).where(Merchant.id == seed.merchant_id).values(is_active=False))
        await session.commit()
    assert (await client.get("/p/gone")).status_code == 404


# ---------------------------------------------------------------- redelivery (CONTRACT v2)


def _fail_upstream(upstream: Upstream, status: int | None = None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if status is None:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(status, text="boom")

    upstream.handler = handler


async def test_failed_delivery_is_redelivered_with_the_same_payment(
    client: httpx.AsyncClient,
    seeded: Seed,
    stub_engine: StubEngine,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Any,
) -> None:
    _fail_upstream(upstream)
    headers = await pay(client)
    assert (await client.get("/p/weather", headers=headers)).status_code == 502
    upstream.handler = None
    retry = await client.get("/p/weather", headers=headers)
    assert retry.status_code == 200
    assert retry.json() == {"ok": True}
    paid = PaymentResponse.from_header(retry.headers[HEADER_PAYMENT_RESPONSE])
    assert paid.txId == TX
    assert paid.usdValueAtSettlement == Decimal("2.5")
    assert stub_engine.calls == [TX]  # never re-verified or re-submitted on-chain
    assert stub_engine.verify_calls == [TX]
    assert len(upstream.requests) == 2
    row = (await settlements(sessionmaker))[0]
    assert row.delivery_attempts == 2
    assert row.upstream_status_code == 200
    assert await redis.get("kasgate:delivery:" + TX) is None
    # delivered now: a third call is a plain replay
    assert (await client.get("/p/weather", headers=headers)).status_code == 409


@pytest.mark.parametrize("failing_status", [500, 502, 503])
async def test_upstream_5xx_is_redelivered(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    failing_status: int,
) -> None:
    _fail_upstream(upstream, failing_status)
    headers = await pay(client)
    first = await client.get("/p/weather", headers=headers)
    assert first.status_code == failing_status
    assert (await settlements(sessionmaker))[0].upstream_status_code == failing_status
    upstream.handler = None
    assert (await client.get("/p/weather", headers=headers)).status_code == 200


@pytest.mark.parametrize("status", [200, 302, 404, 418])
async def test_non_5xx_delivery_is_never_redelivered(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, status: int
) -> None:
    upstream.handler = lambda request: httpx.Response(status, text="x")
    headers = await pay(client)
    assert (await client.get("/p/weather", headers=headers)).status_code == status
    again = await client.get("/p/weather", headers=headers)
    assert again.status_code == 409
    assert again.json()["error"] == "payment_replayed"
    assert len(upstream.requests) == 1


async def test_redelivery_is_capped_by_max_attempts(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _fail_upstream(upstream)
    headers = await pay(client)
    codes = [(await client.get("/p/weather", headers=headers)).status_code for _ in range(5)]
    assert codes == [502, 502, 502, 409, 409]  # redelivery_max_attempts = 3 deliveries in total
    assert len(upstream.requests) == 3
    assert (await settlements(sessionmaker))[0].delivery_attempts == 3


async def test_redelivery_max_attempts_comes_from_settings(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, override_settings: SettingsOverride
) -> None:
    override_settings(redelivery_max_attempts=1)
    _fail_upstream(upstream)
    headers = await pay(client)
    assert (await client.get("/p/weather", headers=headers)).status_code == 502
    assert (await client.get("/p/weather", headers=headers)).status_code == 409
    assert len(upstream.requests) == 1


async def test_redelivery_can_be_switched_off_with_a_zero_window(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, override_settings: SettingsOverride
) -> None:
    override_settings(redelivery_window_seconds=0)
    _fail_upstream(upstream)
    headers = await pay(client)
    assert (await client.get("/p/weather", headers=headers)).status_code == 502
    assert (await client.get("/p/weather", headers=headers)).status_code == 409


async def test_redelivery_window_expires(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _fail_upstream(upstream)
    headers = await pay(client)
    assert (await client.get("/p/weather", headers=headers)).status_code == 502
    async with sessionmaker() as session:
        await session.execute(update(Settlement).values(settled_at=datetime.now(UTC) - timedelta(seconds=601)))
        await session.commit()
    upstream.handler = None
    assert (await client.get("/p/weather", headers=headers)).status_code == 409
    async with sessionmaker() as session:
        await session.execute(update(Settlement).values(settled_at=datetime.now(UTC) - timedelta(seconds=590)))
        await session.commit()
    assert (await client.get("/p/weather", headers=headers)).status_code == 200


async def test_redelivery_requires_the_same_nonce(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, stub_engine: StubEngine
) -> None:
    _fail_upstream(upstream)
    assert (await client.get("/p/weather", headers=await pay(client))).status_code == 502
    upstream.handler = None
    other_nonce = await pay(client)  # same txId, a different (live) nonce
    assert (await client.get("/p/weather", headers=other_nonce)).status_code == 409
    assert stub_engine.calls == [TX]


async def test_redelivery_requires_the_same_endpoint(
    client: httpx.AsyncClient,
    seeded: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    upstream: Upstream,
) -> None:
    await seed_endpoint(sessionmaker, slug="other", address=OTHER_ADDRESS)
    _fail_upstream(upstream)
    headers = await pay(client)
    assert (await client.get("/p/weather", headers=headers)).status_code == 502
    upstream.handler = None
    assert (await client.get("/p/other", headers=headers)).status_code == 409


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/p/weather"), ("GET", "/p/weather/other/path")],
)
async def test_redelivery_requires_the_same_method_and_path(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, method: str, path: str
) -> None:
    _fail_upstream(upstream)
    headers = await pay(client)
    assert (await client.get("/p/weather", headers=headers)).status_code == 502
    upstream.handler = None
    assert (await client.request(method, path, headers=headers)).status_code == 409


async def test_redelivery_requires_the_same_payer(client: httpx.AsyncClient, seeded: Seed, upstream: Upstream) -> None:
    _fail_upstream(upstream)
    headers = await pay(client)
    assert (await client.get("/p/weather", headers=headers)).status_code == 502
    upstream.handler = None
    swapped = {HEADER_PAYMENT_SIGNATURE: signature_header(TX, nonce_of(headers), payer_key=OTHER_KEY)}
    assert (await client.get("/p/weather", headers=swapped)).status_code == 409


async def test_inflight_delivery_blocks_redelivery(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, redis: Any
) -> None:
    _fail_upstream(upstream)
    headers = await pay(client)
    assert (await client.get("/p/weather", headers=headers)).status_code == 502
    await redis.set("kasgate:delivery:" + TX, "1", ex=60)
    upstream.handler = None
    assert (await client.get("/p/weather", headers=headers)).status_code == 409
    assert len(upstream.requests) == 1
    assert await redis.get("kasgate:delivery:" + TX) == "1"  # not ours to clear


async def test_concurrent_redeliveries_never_exceed_the_attempt_cap(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _fail_upstream(upstream)
    headers = await pay(client)
    assert (await client.get("/p/weather", headers=headers)).status_code == 502
    responses = await asyncio.gather(*(client.get("/p/weather", headers=headers) for _ in range(10)))
    codes = [r.status_code for r in responses]
    row = (await settlements(sessionmaker))[0]
    assert row.delivery_attempts <= 3
    assert codes.count(502) == row.delivery_attempts - 1
    assert len(upstream.requests) == row.delivery_attempts
    assert set(codes) <= {502, 409}


async def test_redelivery_attempt_that_fails_again_keeps_the_settlement(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _fail_upstream(upstream, 503)
    headers = await pay(client)
    assert (await client.get("/p/weather", headers=headers)).status_code == 503
    second = await client.get("/p/weather", headers=headers)
    assert second.status_code == 503
    assert HEADER_PAYMENT_RESPONSE in second.headers
    rows = await settlements(sessionmaker)
    assert len(rows) == 1 and rows[0].delivery_attempts == 2 and rows[0].status is SettlementStatus.SETTLED


# ---------------------------------------------------------------- unrecorded settlements


def _break_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_commit(self: AsyncSession) -> None:
        raise OperationalError("INSERT", {}, Exception("database is down"))

    monkeypatch.setattr(AsyncSession, "commit", failing_commit)


async def test_unrecorded_settlement_is_dead_lettered_and_keeps_the_nonce(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    redis: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    headers = await pay(client)
    nonce = nonce_of(headers)
    _break_commit(monkeypatch)
    with caplog.at_level("ERROR", logger="kasgate.proxy"):
        response = await client.get("/p/weather/x", headers=headers)
    assert response.status_code == 503
    assert response.json()["error"] == "service_unavailable"
    assert response.headers["retry-after"] == "5"
    X402ErrorBody.model_validate(response.json())
    assert upstream.requests == []
    assert await redis.get(lock_key(TX)) is not None
    assert await redis.exists(nonce_key(nonce)) == 1  # consumed only once the settlement is stored
    queued = await redis.lrange("kasgate:deadletter:settlements", 0, -1)
    assert len(queued) == 1
    payload = json.loads(queued[0])
    assert payload["txId"] == TX
    assert payload["paymentNonce"] == nonce
    assert payload["endpointId"] == str(seeded.endpoint_id)
    assert payload["endpointSlug"] == "weather"
    assert payload["merchantId"] == str(seeded.merchant_id)
    assert payload["amountSompi"] == PRICE_SOMPI
    assert payload["merchantAmountSompi"] == SPLIT.merchant_sompi
    assert payload["platformFeeSompi"] == SPLIT.fee_sompi
    assert payload["payerAddress"] == PAYER_ADDRESS
    assert payload["merchantAddress"] == MERCHANT_ADDRESS
    assert payload["treasuryAddress"] == TREASURY
    assert Decimal(payload["kasUsdRate"]) == Decimal("0.125")
    assert Decimal(payload["usdValueAtSettlement"]) == Decimal("2.5")
    assert payload["requestPath"] == "/p/weather/x"
    assert payload["settledAt"] and payload["recordedAt"]
    assert any(r.levelname == "ERROR" and TX in r.getMessage() for r in caplog.records)
    monkeypatch.undo()
    assert (await client.get("/p/weather", headers=await pay(client))).status_code == 409


async def test_dead_letter_failure_logs_payload_and_still_returns_503(
    client: httpx.AsyncClient,
    seeded: Seed,
    redis: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken_lpush(*args: Any, **kwargs: Any) -> None:
        raise RedisConnectionError("down")

    headers = await pay(client)
    _break_commit(monkeypatch)
    monkeypatch.setattr(redis, "lpush", broken_lpush)
    with caplog.at_level("ERROR", logger="kasgate.proxy"):
        response = await client.get("/p/weather", headers=headers)
    assert response.status_code == 503
    logged = [r.getMessage() for r in caplog.records if "unrecorded settlement payload" in r.getMessage()]
    assert len(logged) == 1
    assert f'"txId": "{TX}"' in logged[0]
    assert f'"merchantAmountSompi": {SPLIT.merchant_sompi}' in logged[0]
    assert await redis.get(lock_key(TX)) is not None


async def test_unreadable_upstream_credentials_are_503_service_unavailable_before_payment(
    client: httpx.AsyncClient,
    seeded: Seed,
    stub_engine: StubEngine,
    redis: Any,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    headers = await pay(client)
    async with sessionmaker() as session:
        await session.execute(update(Endpoint).values(upstream_headers_encrypted=b"not-a-fernet-token"))
        await session.commit()
    response = await client.get("/p/weather", headers=headers)
    assert response.status_code == 503
    assert X402ErrorBody.model_validate(response.json()).error == "service_unavailable"
    assert response.headers["retry-after"] == "5"
    assert stub_engine.calls == [] and stub_engine.verify_calls == []
    assert await redis.get(lock_key(TX)) is None
    assert await redis.exists(nonce_key(nonce_of(headers))) == 1


async def test_database_check_constraints_back_the_nonce_binding(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    assert (await client.get("/p/weather", headers=await pay(client))).status_code == 200
    async with sessionmaker() as session:
        count = await session.scalar(text("SELECT count(*) FROM settlements WHERE payment_nonce ~ '^[0-9a-f]{32}$'"))
        assert count == 1


# ---------------------------------------------------------------- USD-priced challenges


USD_MICROS = 1_000_000
USD_RATE = Decimal("0.12500000")


async def test_usd_challenge_locks_converted_sompi_even_if_the_rate_then_changes(
    client: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    stub_oracle: StubOracle,
    stub_engine: StubEngine,
    redis: Any,
) -> None:
    from app.models import PriceCurrency
    from app.schemas import usd_micros_to_sompi

    await seed_endpoint(
        sessionmaker,
        slug="usd-weather",
        address=OTHER_ADDRESS,
        price_currency=PriceCurrency.USD,
        price_usd_micros=USD_MICROS,
        price_sompi=None,
    )
    locked = usd_micros_to_sompi(USD_MICROS, USD_RATE)
    response = await client.get("/p/usd-weather")
    assert response.status_code == 402
    required = PaymentRequired.from_header(response.headers[HEADER_PAYMENT_REQUIRED])
    assert required.amount == str(locked)
    assert required.extra is not None
    assert required.extra.kasUsdRate == "0.12500000"
    assert required.extra.quotedAt.tzinfo is not None
    # changing the live rate must not change the stored challenge
    stub_oracle.rate = Decimal("0.25000000")
    headers = {HEADER_PAYMENT_SIGNATURE: make_signature_header(TX, required.nonce)}
    paid = await client.get("/p/usd-weather", headers=headers)
    assert paid.status_code == 200, paid.text
    row = (await settlements(sessionmaker))[0]
    assert row.amount_sompi == locked
    assert row.kas_usd_rate == Decimal("0.12500000")
    assert row.usd_value_at_settlement == Decimal("1.00000000")  # 8 KAS * 0.125
    assert stub_engine.calls == [TX]
    # a fresh challenge now uses the new rate (4 KAS)
    fresh = await client.get("/p/usd-weather")
    assert fresh.status_code == 402
    again = PaymentRequired.from_header(fresh.headers[HEADER_PAYMENT_REQUIRED])
    assert again.amount == str(usd_micros_to_sompi(USD_MICROS, Decimal("0.25000000")))
    assert again.nonce != required.nonce


async def test_usd_challenge_fails_closed_when_the_oracle_is_down(
    client: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    stub_oracle: StubOracle,
    stub_engine: StubEngine,
    redis: Any,
) -> None:
    from app.models import PriceCurrency

    await seed_endpoint(
        sessionmaker,
        slug="usd-down",
        address=OTHER_ADDRESS,
        price_currency=PriceCurrency.USD,
        price_usd_micros=USD_MICROS,
        price_sompi=None,
    )
    stub_oracle.rate = None
    response = await client.get("/p/usd-down")
    assert response.status_code == 503
    assert response.json()["error"] == "price_unavailable"
    assert "retry-after" in {k.lower() for k in response.headers}
    assert HEADER_PAYMENT_REQUIRED not in response.headers
    assert stub_engine.verify_calls == [] and stub_engine.calls == []
    assert await redis.keys("kasgate:tx:*") == []
    assert await redis.keys("kasgate:noncelock:*") == []
    assert await redis.keys("kasgate:nonce:*") == []
    assert await settlements(sessionmaker) == []
    # a paid-looking retry also fails closed (it needs a fresh challenge first, which also needs the oracle)
    from tests.conftest import make_signature_header as _sig

    paid = await client.get("/p/usd-down", headers={HEADER_PAYMENT_SIGNATURE: _sig(TX, "a" * 32)})
    assert paid.status_code == 503
    assert paid.json()["error"] == "price_unavailable"
    assert await redis.keys("kasgate:tx:*") == []


async def test_kas_priced_challenge_omits_extra_and_does_not_need_the_oracle(
    client: httpx.AsyncClient, seeded: Seed, stub_oracle: StubOracle
) -> None:
    stub_oracle.rate = None
    response = await client.get("/p/weather")
    assert response.status_code == 402
    required = PaymentRequired.from_header(response.headers[HEADER_PAYMENT_REQUIRED])
    assert required.amount == str(PRICE_SOMPI)
    assert required.extra is None
    assert "extra" not in required.model_dump(mode="json", exclude_none=True)
