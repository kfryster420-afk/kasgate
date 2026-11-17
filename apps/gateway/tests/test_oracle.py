from __future__ import annotations

import os
from collections.abc import AsyncIterator
from decimal import Decimal

import fakeredis.aioredis
import httpx
import pytest
import respx
from kaspa import PrivateKey

os.environ.setdefault("KASGATE_TREASURY_ADDRESS", PrivateKey("02" * 32).to_address("testnet").to_string())
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://kasgate:kasgate@localhost:5432/kasgate")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ENCRYPTION_KEY", "A" * 43 + "=")

from app.config import Settings, get_settings  # noqa: E402
from app.oracle import (  # noqa: E402
    RATE_CACHE_KEY,
    RATE_LAST_KEY,
    KasUsdOracle,
    OracleUnavailableError,
    extract_rate,
    usd_value,
)

PRICE_URL = "https://prices.test/kaspa"


@pytest.fixture
def settings() -> Settings:
    return get_settings().model_copy(update={"kas_usd_price_url": PRICE_URL})


@pytest.fixture
async def redis_client() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.flushall()
    await client.aclose()


@respx.mock
async def test_fetches_coingecko_shape_and_caches(
    settings: Settings, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    route = respx.get(PRICE_URL).mock(return_value=httpx.Response(200, json={"kaspa": {"usd": 0.123456789}}))
    oracle = KasUsdOracle(settings, redis_client)
    assert await oracle.get_rate() == Decimal("0.12345679")
    assert await oracle.get_rate() == Decimal("0.12345679")
    assert route.call_count == 1
    assert await redis_client.get(RATE_CACHE_KEY) == "0.12345679"
    assert await redis_client.get(RATE_LAST_KEY) == "0.12345679"
    assert 0 < await redis_client.ttl(RATE_CACHE_KEY) <= 60
    assert await redis_client.ttl(RATE_LAST_KEY) == -1


@respx.mock
async def test_cache_hit_skips_http(settings: Settings, redis_client: fakeredis.aioredis.FakeRedis) -> None:
    route = respx.get(PRICE_URL).mock(return_value=httpx.Response(200, json={"kaspa": {"usd": 9}}))
    await redis_client.set(RATE_CACHE_KEY, "0.05000000", ex=60)
    assert await KasUsdOracle(settings, redis_client).get_rate() == Decimal("0.05000000")
    assert route.call_count == 0


@respx.mock
async def test_falls_back_to_last_rate_when_fetch_fails(
    settings: Settings, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    respx.get(PRICE_URL).mock(side_effect=httpx.ConnectTimeout("slow"))
    await redis_client.set(RATE_LAST_KEY, "0.07000000")
    assert await KasUsdOracle(settings, redis_client).get_rate() == Decimal("0.07000000")


@respx.mock
async def test_falls_back_on_http_error_status(settings: Settings, redis_client: fakeredis.aioredis.FakeRedis) -> None:
    respx.get(PRICE_URL).mock(return_value=httpx.Response(429))
    await redis_client.set(RATE_LAST_KEY, "0.08")
    assert await KasUsdOracle(settings, redis_client).get_rate() == Decimal("0.08")


@respx.mock
async def test_raises_when_fetch_and_fallback_both_fail(
    settings: Settings, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    respx.get(PRICE_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(OracleUnavailableError, match="no previously stored"):
        await KasUsdOracle(settings, redis_client).get_rate()


@respx.mock
async def test_garbage_payload_is_an_error(settings: Settings, redis_client: fakeredis.aioredis.FakeRedis) -> None:
    respx.get(PRICE_URL).mock(return_value=httpx.Response(200, json={"kaspa": {"usd": "n/a"}}))
    with pytest.raises(OracleUnavailableError):
        await KasUsdOracle(settings, redis_client).get_rate()


@respx.mock
async def test_non_json_payload_is_an_error(settings: Settings, redis_client: fakeredis.aioredis.FakeRedis) -> None:
    respx.get(PRICE_URL).mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(OracleUnavailableError):
        await KasUsdOracle(settings, redis_client).get_rate()


@respx.mock
async def test_fresh_fetch_overwrites_last(settings: Settings, redis_client: fakeredis.aioredis.FakeRedis) -> None:
    respx.get(PRICE_URL).mock(return_value=httpx.Response(200, json={"price": "0.2"}))
    await redis_client.set(RATE_LAST_KEY, "0.01")
    assert await KasUsdOracle(settings, redis_client).get_rate() == Decimal("0.20000000")
    assert await redis_client.get(RATE_LAST_KEY) == "0.20000000"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"kaspa": {"usd": 0.1}}, Decimal("0.10000000")),
        ({"price": "0.1234"}, Decimal("0.12340000")),
        ({"data": {"price": 0.5}}, Decimal("0.50000000")),
        ({"result": [{"price": "0.3"}]}, Decimal("0.30000000")),
        ({"usd": 2}, Decimal("2.00000000")),
        ([{"symbol": "KASUSDT", "price": "0.04"}], Decimal("0.04000000")),
        (0.25, Decimal("0.25000000")),
        ({"kaspa": {"usd": 0}}, None),
        ({"kaspa": {"usd": -1}}, None),
        ({"kaspa": {"usd": True}}, None),
        ({"kaspa": {"usd": "NaN"}}, None),
        ({"unrelated": 1}, None),
        ("hello", None),
    ],
)
def test_extract_rate(payload: object, expected: Decimal | None) -> None:
    assert extract_rate(payload) == expected


def test_usd_value_quantised_to_eight_places() -> None:
    assert usd_value(100_000_000, Decimal("0.12345678")) == Decimal("0.12345678")
    assert usd_value(1_000_000_000, Decimal("0.1")) == Decimal("1.00000000")
    assert usd_value(1, Decimal("0.1")) == Decimal("0.00000000")
    assert str(usd_value(123_456_789, Decimal("0.2"))) == "0.24691358"
    assert usd_value(0, Decimal("0.1")) == Decimal("0")
