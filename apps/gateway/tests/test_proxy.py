"""The HTTP 402 handshake, Redis replay protection, and forwarding behaviour of /p/{slug}."""

import asyncio
import base64
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.kaspa_engine import RpcUnavailableError
from app.models import Settlement, SettlementStatus
from app.proxy import REPLAY_LOCK_PREFIX, REPLAY_LOCK_TTL_SECONDS, build_upstream_url, lock_key
from app.schemas import (
    HEADER_PAYMENT_REQUIRED,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
    PaymentRequired,
    PaymentResponse,
)
from tests.conftest import (
    MERCHANT_ADDRESS,
    PAYER_ADDRESS,
    PRICE_SOMPI,
    TREASURY,
    UPSTREAM_SECRET,
    Seed,
    StubEngine,
    StubOracle,
    Upstream,
    make_signature_header,
    seed_endpoint,
)

TX = "ab" * 32


async def settlements(maker: async_sessionmaker[AsyncSession]) -> list[Settlement]:
    async with maker() as session:
        return list((await session.execute(select(Settlement))).scalars())


def pay(tx_id: str = TX) -> dict[str, str]:
    return {HEADER_PAYMENT_SIGNATURE: make_signature_header(tx_id)}


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
    response = await client.delete("/p/weather", headers=pay())
    assert response.status_code == 405
    assert response.json()["error"] == "method_not_allowed"
    assert response.headers["allow"] == "GET, POST"


# ---------------------------------------------------------------- 402 handshake


async def test_missing_signature_returns_402_with_challenge(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, upstream: Upstream, redis: Any
) -> None:
    response = await client.get("/p/weather")
    assert response.status_code == 402
    required = PaymentRequired.from_header(response.headers[HEADER_PAYMENT_REQUIRED])
    assert required.amount == str(PRICE_SOMPI)
    assert required.payTo == MERCHANT_ADDRESS
    assert required.platformFee.feeTo == TREASURY
    assert int(required.platformFee.feeAmount) == 30_000_000  # 1.5 %
    assert required.merchant_amount_sompi == 1_970_000_000  # 98.5 %
    body = response.json()
    assert body["error"] == "payment_required"
    assert body["accepts"]["amount"] == str(PRICE_SOMPI)
    assert stub_engine.calls == []
    assert upstream.requests == []
    assert [key for key in await redis.keys("*") if not key.startswith("kasgate:rl:")] == []


@pytest.mark.parametrize(
    "header",
    [
        "not base64 !!!",
        base64.b64encode(b"not json").decode(),
        base64.b64encode(json.dumps({"txId": "abc"}).encode()).decode(),
        base64.b64encode(json.dumps({"txId": "AB" * 32, "rawTx": "{}", "payerAddress": "nope"}).encode()).decode(),
    ],
)
async def test_bad_signature_header_returns_402_invalid_payment_header(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any, header: str
) -> None:
    response = await client.get("/p/weather", headers={HEADER_PAYMENT_SIGNATURE: header})
    assert response.status_code == 402
    assert response.json()["error"] == "invalid_payment_header"
    PaymentRequired.from_header(response.headers[HEADER_PAYMENT_REQUIRED])
    assert stub_engine.calls == []
    assert await redis.keys("*") == []


async def test_wrong_network_address_in_signature_is_402_wrong_network(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine
) -> None:
    mainnet_payer = "kaspa:q" + "q" * 60
    header = base64.b64encode(json.dumps({"txId": TX, "rawTx": "{}", "payerAddress": mainnet_payer}).encode()).decode()
    response = await client.get("/p/weather", headers={HEADER_PAYMENT_SIGNATURE: header})
    assert response.status_code == 402
    assert response.json()["error"] == "wrong_network"
    PaymentRequired.from_header(response.headers[HEADER_PAYMENT_REQUIRED])
    assert stub_engine.calls == []


async def test_valid_payment_is_forwarded_and_settled(
    client: httpx.AsyncClient,
    seeded: Seed,
    stub_engine: StubEngine,
    upstream: Upstream,
    redis: Any,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    response = await client.get(
        "/p/weather/forecast/paris?units=metric&q=a%20b",
        headers={
            **pay(),
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
    assert row.status is SettlementStatus.SETTLED
    assert row.upstream_status_code == 200
    assert row.amount_sompi == PRICE_SOMPI
    assert row.merchant_amount_sompi == 1_970_000_000
    assert row.fee_amount_sompi == 30_000_000
    assert row.merchant_address == MERCHANT_ADDRESS
    assert row.treasury_address == TREASURY
    assert row.kas_usd_rate == Decimal("0.125")
    assert row.usd_value_at_settlement == Decimal("2.5")
    assert row.request_path == "/p/weather/forecast/paris"
    assert row.settled_at is not None
    assert await redis.get(lock_key(TX)) is not None


async def test_post_body_and_content_type_are_forwarded(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream
) -> None:
    response = await client.post("/p/weather", json={"city": "Oslo"}, headers=pay())
    assert response.status_code == 200
    sent = upstream.requests[0]
    assert sent.method == "POST"
    assert json.loads(sent.content) == {"city": "Oslo"}
    assert sent.headers["content-type"] == "application/json"
    assert str(sent.url) == "https://api.upstream.test/v1"


async def test_upstream_response_cannot_spoof_payment_headers(client: httpx.AsyncClient, seeded: Seed) -> None:
    response = await client.get("/p/weather", headers=pay())
    PaymentResponse.from_header(response.headers[HEADER_PAYMENT_RESPONSE])


async def test_upstream_error_status_is_passed_through(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    upstream.handler = lambda request: httpx.Response(418, text="teapot")
    response = await client.get("/p/weather", headers=pay())
    assert response.status_code == 418
    assert response.text == "teapot"
    assert HEADER_PAYMENT_RESPONSE in response.headers
    assert (await settlements(sessionmaker))[0].upstream_status_code == 418


async def test_upstream_redirects_are_not_followed(client: httpx.AsyncClient, seeded: Seed, upstream: Upstream) -> None:
    upstream.handler = lambda request: httpx.Response(302, headers={"Location": "http://169.254.169.254/latest"})
    response = await client.get("/p/weather", headers=pay())
    assert response.status_code == 302
    assert len(upstream.requests) == 1


@pytest.mark.parametrize("suffix", ["%2e%2e/admin", "a/%2E%2E/%2e%2e/b", "%2e/x", "..%2fadmin"])
async def test_path_traversal_is_rejected_before_payment(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, upstream: Upstream, suffix: str
) -> None:
    response = await client.get(f"/p/weather/{suffix}", headers=pay())
    assert response.status_code == 400
    assert stub_engine.calls == []
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
    response = await client.get("/p/weather", headers=pay())
    assert response.status_code == 200
    assert PaymentResponse.from_header(response.headers[HEADER_PAYMENT_RESPONSE]).usdValueAtSettlement == 0
    row = (await settlements(sessionmaker))[0]
    assert row.kas_usd_rate is None
    assert row.usd_value_at_settlement is None


# ---------------------------------------------------------------- replay protection


async def test_lock_uses_documented_key_and_ttl(client: httpx.AsyncClient, seeded: Seed, redis: Any) -> None:
    assert REPLAY_LOCK_PREFIX == "kasgate:tx:"
    assert REPLAY_LOCK_TTL_SECONDS == 600
    await client.get("/p/weather", headers=pay())
    assert 0 < await redis.ttl(f"kasgate:tx:{TX}") <= 600


async def test_second_use_of_a_transaction_is_409(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, upstream: Upstream
) -> None:
    assert (await client.get("/p/weather", headers=pay())).status_code == 200
    second = await client.get("/p/weather", headers=pay())
    assert second.status_code == 409
    assert second.json()["error"] == "payment_replayed"
    assert HEADER_PAYMENT_RESPONSE not in second.headers
    assert stub_engine.calls == [TX]
    assert len(upstream.requests) == 1


async def test_replay_after_lock_expiry_is_caught_by_settlement_row(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any
) -> None:
    assert (await client.get("/p/weather", headers=pay())).status_code == 200
    await redis.delete(lock_key(TX))
    second = await client.get("/p/weather", headers=pay())
    assert second.status_code == 409
    assert second.json()["error"] == "payment_replayed"
    assert stub_engine.calls == [TX]


async def test_preheld_lock_blocks_the_request(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any
) -> None:
    await redis.set(lock_key(TX), "1", nx=True, ex=600)
    response = await client.get("/p/weather", headers=pay())
    assert response.status_code == 409
    assert stub_engine.calls == []
    assert await redis.get(lock_key(TX)) == "1"


async def test_concurrent_duplicates_only_one_succeeds(
    client: httpx.AsyncClient,
    seeded: Seed,
    stub_engine: StubEngine,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    stub_engine.delay = 0.05
    responses = await asyncio.gather(*(client.get("/p/weather", headers=pay()) for _ in range(12)))
    codes = sorted(r.status_code for r in responses)
    assert codes == [200] + [409] * 11
    assert stub_engine.calls == [TX]
    assert len(upstream.requests) == 1
    assert len(await settlements(sessionmaker)) == 1


async def test_concurrent_distinct_transactions_all_succeed(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    stub_engine.delay = 0.02
    tx_ids = [f"{i:064x}" for i in range(1, 9)]
    responses = await asyncio.gather(*(client.get("/p/weather", headers=pay(tx)) for tx in tx_ids))
    assert [r.status_code for r in responses] == [200] * 8
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Settlement)) == 8


@pytest.mark.parametrize(
    "code", ["underpaid", "invalid_fee_split", "wrong_network", "tx_rejected", "invalid_payment_header"]
)
async def test_verification_failure_is_402_and_releases_lock(
    client: httpx.AsyncClient,
    seeded: Seed,
    stub_engine: StubEngine,
    upstream: Upstream,
    redis: Any,
    sessionmaker: async_sessionmaker[AsyncSession],
    code: str,
) -> None:
    stub_engine.fail(code, "nope")
    response = await client.get("/p/weather", headers=pay())
    assert response.status_code == 402
    assert response.json()["error"] == code
    assert response.json()["message"] == "nope"
    PaymentRequired.from_header(response.headers[HEADER_PAYMENT_REQUIRED])
    assert await redis.get(lock_key(TX)) is None
    assert upstream.requests == []
    assert await settlements(sessionmaker) == []

    stub_engine.error = None
    retry = await client.get("/p/weather", headers=pay())
    assert retry.status_code == 200
    assert await redis.get(lock_key(TX)) is not None


async def test_unknown_engine_code_maps_to_tx_rejected(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine
) -> None:
    stub_engine.fail("something_else")
    response = await client.get("/p/weather", headers=pay())
    assert response.status_code == 402
    assert response.json()["error"] == "tx_rejected"


async def test_node_unavailable_is_503_and_releases_lock(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any
) -> None:
    stub_engine.error = RpcUnavailableError("node down")
    response = await client.get("/p/weather", headers=pay())
    assert response.status_code == 503
    assert response.json()["error"] == "rpc_unavailable"
    assert response.headers["retry-after"] == "5"
    assert await redis.get(lock_key(TX)) is None


async def test_unexpected_engine_crash_is_503_and_releases_lock(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any
) -> None:
    stub_engine.error = RuntimeError("boom")
    response = await client.get("/p/weather", headers=pay())
    assert response.status_code == 503
    assert await redis.get(lock_key(TX)) is None


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
    response = await client.get("/p/weather", headers=pay())
    assert response.status_code == 502
    assert response.json()["error"] == "upstream_error"
    assert PaymentResponse.from_header(response.headers[HEADER_PAYMENT_RESPONSE]).txId == TX
    rows = await settlements(sessionmaker)
    assert len(rows) == 1
    assert rows[0].status is SettlementStatus.SETTLED
    assert rows[0].upstream_status_code is None
    assert await redis.get(lock_key(TX)) is not None
    assert (await client.get("/p/weather", headers=pay())).status_code == 409


async def test_upstream_timeout_is_502(client: httpx.AsyncClient, seeded: Seed, upstream: Upstream) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    upstream.handler = slow
    response = await client.get("/p/weather", headers=pay())
    assert response.status_code == 502
    assert HEADER_PAYMENT_RESPONSE in response.headers


async def test_redis_outage_fails_closed(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RedisConnectionError("down")

    monkeypatch.setattr(redis, "set", broken)
    response = await client.get("/p/weather", headers=pay())
    assert response.status_code == 503
    assert stub_engine.calls == []


async def test_inactive_merchant_endpoint_is_404(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from sqlalchemy import update

    from app.models import Merchant

    seed = await seed_endpoint(sessionmaker, slug="gone")
    async with sessionmaker() as session:
        await session.execute(update(Merchant).where(Merchant.id == seed.merchant_id).values(is_active=False))
        await session.commit()
    assert (await client.get("/p/gone")).status_code == 404


# ---------------------------------------------------------------- unrecorded settlements


def _break_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy.exc import OperationalError

    async def failing_commit(self: AsyncSession) -> None:
        raise OperationalError("INSERT", {}, Exception("database is down"))

    monkeypatch.setattr(AsyncSession, "commit", failing_commit)


async def test_unrecorded_settlement_is_dead_lettered(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    redis: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _break_commit(monkeypatch)
    with caplog.at_level("ERROR", logger="kasgate.proxy"):
        response = await client.get("/p/weather/x", headers=pay())
    assert response.status_code == 503
    assert upstream.requests == []
    assert await redis.get(lock_key(TX)) is not None
    queued = await redis.lrange("kasgate:deadletter:settlements", 0, -1)
    assert len(queued) == 1
    payload = json.loads(queued[0])
    assert payload["txId"] == TX
    assert payload["endpointId"] == str(seeded.endpoint_id)
    assert payload["endpointSlug"] == "weather"
    assert payload["merchantId"] == str(seeded.merchant_id)
    assert payload["amountSompi"] == PRICE_SOMPI
    assert payload["merchantAmountSompi"] == 1_970_000_000
    assert payload["platformFeeSompi"] == 30_000_000
    assert payload["payerAddress"] == PAYER_ADDRESS
    assert payload["merchantAddress"] == MERCHANT_ADDRESS
    assert payload["treasuryAddress"] == TREASURY
    assert Decimal(payload["kasUsdRate"]) == Decimal("0.125")
    assert Decimal(payload["usdValueAtSettlement"]) == Decimal("2.5")
    assert payload["requestPath"] == "/p/weather/x"
    assert payload["settledAt"] and payload["recordedAt"]
    assert any(r.levelname == "ERROR" and TX in r.getMessage() for r in caplog.records)
    monkeypatch.undo()
    assert (await client.get("/p/weather", headers=pay())).status_code == 409


async def test_dead_letter_failure_logs_payload_and_still_returns_503(
    client: httpx.AsyncClient,
    seeded: Seed,
    redis: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def broken_lpush(*args: Any, **kwargs: Any) -> None:
        raise RedisConnectionError("down")

    _break_commit(monkeypatch)
    monkeypatch.setattr(redis, "lpush", broken_lpush)
    with caplog.at_level("ERROR", logger="kasgate.proxy"):
        response = await client.get("/p/weather", headers=pay())
    assert response.status_code == 503
    logged = [r.getMessage() for r in caplog.records if "unrecorded settlement payload" in r.getMessage()]
    assert len(logged) == 1
    assert f'"txId": "{TX}"' in logged[0]
    assert '"merchantAmountSompi": 1970000000' in logged[0]
    assert await redis.get(lock_key(TX)) is not None
