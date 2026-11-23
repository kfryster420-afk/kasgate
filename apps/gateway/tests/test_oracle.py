from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

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
    QuotedRate,
    _encode_quoted,
    extract_rate,
    median_rate,
    parse_coingecko,
    parse_coinpaprika,
    parse_kraken,
    parse_mexc,
    reject_outliers,
    usd_value,
)

CG = "https://prices.test/coingecko"
CP = "https://prices.test/coinpaprika"
KR = "https://prices.test/kraken"
MX = "https://prices.test/mexc"
DEFAULT_CG = "https://api.coingecko.com/api/v3/simple/price?ids=kaspa&vs_currencies=usd"


def oracle_settings(**updates: Any) -> Settings:
    values: dict[str, Any] = {
        "kas_usd_price_url": DEFAULT_CG,
        "oracle_source_coingecko": CG,
        "oracle_source_coinpaprika": CP,
        "oracle_source_kraken": KR,
        "oracle_source_mexc": "",
        "oracle_min_sources": 2,
        "oracle_cache_ttl_seconds": 60,
        "oracle_max_staleness_seconds": 300,
        "oracle_timeout_seconds": 3.0,
        "oracle_outlier_max_deviation": Decimal("0.5"),
    }
    values.update(updates)
    return get_settings().model_copy(update=values)


class Clock:
    def __init__(self, when: datetime | None = None) -> None:
        self.when = when or datetime(2026, 11, 23, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.when


@pytest.fixture
def settings() -> Settings:
    return oracle_settings()


@pytest.fixture
async def redis_client() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.flushall()
    await client.aclose()


def _mock_three(
    *,
    coingecko: object = 0.10,
    paprika: object = 0.11,
    kraken: object = 0.12,
) -> None:
    if isinstance(coingecko, Exception):
        respx.get(CG).mock(side_effect=coingecko)
    elif isinstance(coingecko, httpx.Response):
        respx.get(CG).mock(return_value=coingecko)
    else:
        respx.get(CG).mock(return_value=httpx.Response(200, json={"kaspa": {"usd": coingecko}}))
    if isinstance(paprika, Exception):
        respx.get(CP).mock(side_effect=paprika)
    elif isinstance(paprika, httpx.Response):
        respx.get(CP).mock(return_value=paprika)
    else:
        respx.get(CP).mock(return_value=httpx.Response(200, json={"quotes": {"USD": {"price": paprika}}}))
    if isinstance(kraken, Exception):
        respx.get(KR).mock(side_effect=kraken)
    elif isinstance(kraken, httpx.Response):
        respx.get(KR).mock(return_value=kraken)
    else:
        respx.get(KR).mock(
            return_value=httpx.Response(200, json={"error": [], "result": {"KASUSD": {"c": [str(kraken), "1"]}}})
        )


def _oracle(settings: Settings, redis_client: fakeredis.aioredis.FakeRedis, clock: Clock | None = None) -> KasUsdOracle:
    return KasUsdOracle(settings, redis_client, now=clock or Clock())


# ---------------------------------------------------------------- parsers & median


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


def test_source_parsers() -> None:
    assert parse_coingecko({"kaspa": {"usd": "0.2"}}) == Decimal("0.20000000")
    assert parse_coinpaprika({"quotes": {"USD": {"price": 0.3}}}) == Decimal("0.30000000")
    assert parse_kraken({"error": [], "result": {"KASUSD": {"c": ["0.4", "10"]}}}) == Decimal("0.40000000")
    assert parse_kraken({"error": ["Busy"], "result": {"KASUSD": {"c": ["0.4", "10"]}}}) is None
    assert parse_mexc({"symbol": "KASUSDT", "price": "0.5"}) == Decimal("0.50000000")
    assert parse_mexc({"price": "-1"}) is None
    assert parse_mexc({"price": "Infinity"}) is None


def test_median_and_outlier_rejection() -> None:
    assert median_rate([Decimal("0.10"), Decimal("0.11"), Decimal("0.12")]) == Decimal("0.11000000")
    assert median_rate([Decimal("0.10"), Decimal("0.12")]) == Decimal("0.11000000")
    kept = reject_outliers([Decimal("0.10"), Decimal("0.11"), Decimal("5.00")], Decimal("0.5"))
    assert kept == [Decimal("0.10"), Decimal("0.11")]
    # two sources that disagree wildly: both fall outside 50% of their midpoint, so both drop
    assert reject_outliers([Decimal("0.10"), Decimal("5.00")], Decimal("0.5")) == []


def test_usd_value_quantised_to_eight_places() -> None:
    assert usd_value(100_000_000, Decimal("0.12345678")) == Decimal("0.12345678")
    assert usd_value(1_000_000_000, Decimal("0.1")) == Decimal("1.00000000")
    assert usd_value(1, Decimal("0.1")) == Decimal("0.00000000")
    assert str(usd_value(123_456_789, Decimal("0.2"))) == "0.24691358"
    assert usd_value(0, Decimal("0.1")) == Decimal("0")


# ---------------------------------------------------------------- live fetch, cache, last-good, fail-closed


@respx.mock
async def test_medians_three_sources_and_caches(settings: Settings, redis_client: fakeredis.aioredis.FakeRedis) -> None:
    clock = Clock()
    _mock_three(coingecko=0.10, paprika=0.11, kraken=0.12)
    oracle = _oracle(settings, redis_client, clock)
    assert await oracle.get_rate() == Decimal("0.11000000")
    assert await oracle.get_rate() == Decimal("0.11000000")  # cache
    assert respx.calls.call_count == 3
    stored = QuotedRate(rate=Decimal("0.11000000"), quoted_at=clock.when)
    assert await redis_client.get(RATE_CACHE_KEY) == _encode_quoted(stored)
    assert await redis_client.get(RATE_LAST_KEY) == _encode_quoted(stored)
    assert 0 < await redis_client.ttl(RATE_CACHE_KEY) <= 60
    assert await redis_client.ttl(RATE_LAST_KEY) == -1


@respx.mock
async def test_cache_hit_skips_http(settings: Settings, redis_client: fakeredis.aioredis.FakeRedis) -> None:
    clock = Clock()
    _mock_three(coingecko=9, paprika=9, kraken=9)
    quoted = QuotedRate(rate=Decimal("0.05000000"), quoted_at=clock.when)
    await redis_client.set(RATE_CACHE_KEY, _encode_quoted(quoted), ex=60)
    assert await _oracle(settings, redis_client, clock).get_rate() == Decimal("0.05000000")
    assert respx.calls.call_count == 0


@respx.mock
async def test_falls_back_to_fresh_last_good_when_sources_fail(
    settings: Settings, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    clock = Clock()
    _mock_three(
        coingecko=httpx.ConnectTimeout("slow"),
        paprika=httpx.Response(429),
        kraken=httpx.ConnectError("down"),
    )
    last = QuotedRate(rate=Decimal("0.07000000"), quoted_at=clock.when - timedelta(seconds=60))
    await redis_client.set(RATE_LAST_KEY, _encode_quoted(last))
    assert await _oracle(settings, redis_client, clock).get_rate() == Decimal("0.07000000")


@respx.mock
async def test_stale_last_good_is_not_used(settings: Settings, redis_client: fakeredis.aioredis.FakeRedis) -> None:
    clock = Clock()
    _mock_three(
        coingecko=httpx.ConnectError("down"),
        paprika=httpx.ConnectError("down"),
        kraken=httpx.ConnectError("down"),
    )
    last = QuotedRate(rate=Decimal("0.07000000"), quoted_at=clock.when - timedelta(seconds=301))
    await redis_client.set(RATE_LAST_KEY, _encode_quoted(last))
    with pytest.raises(OracleUnavailableError, match="fresh enough"):
        await _oracle(settings, redis_client, clock).get_rate()


@respx.mock
async def test_legacy_bare_rate_last_good_is_treated_as_stale(
    settings: Settings, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    _mock_three(
        coingecko=httpx.ConnectError("down"),
        paprika=httpx.ConnectError("down"),
        kraken=httpx.ConnectError("down"),
    )
    await redis_client.set(RATE_LAST_KEY, "0.07000000")
    with pytest.raises(OracleUnavailableError):
        await _oracle(settings, redis_client).get_rate()


@respx.mock
async def test_raises_when_all_sources_fail_and_there_is_no_last_good(
    settings: Settings, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    _mock_three(
        coingecko=httpx.ConnectError("down"),
        paprika=httpx.ConnectError("down"),
        kraken=httpx.ConnectError("down"),
    )
    with pytest.raises(OracleUnavailableError, match="fresh enough"):
        await _oracle(settings, redis_client).get_rate()


@respx.mock
async def test_only_one_live_source_fails_closed(
    settings: Settings, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    _mock_three(coingecko=0.10, paprika=httpx.ConnectError("down"), kraken=httpx.Response(500))
    with pytest.raises(OracleUnavailableError, match="only 1 of 3"):
        await _oracle(settings, redis_client).get_rate()


@respx.mock
async def test_configured_with_a_single_source_fails_when_two_are_required(
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    settings = oracle_settings(oracle_source_coinpaprika="", oracle_source_kraken="", oracle_min_sources=2)
    respx.get(CG).mock(return_value=httpx.Response(200, json={"kaspa": {"usd": 0.1}}))
    with pytest.raises(OracleUnavailableError, match="only 1 KAS/USD source"):
        await _oracle(settings, redis_client).get_rate()
    assert respx.calls.call_count == 0


@respx.mock
async def test_one_source_is_enough_when_configured(
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    settings = oracle_settings(oracle_source_coinpaprika="", oracle_source_kraken="", oracle_min_sources=1)
    respx.get(CG).mock(return_value=httpx.Response(200, json={"kaspa": {"usd": 0.2}}))
    assert await _oracle(settings, redis_client).get_rate() == Decimal("0.20000000")


@respx.mock
async def test_outlier_is_dropped_and_median_of_the_rest_is_used(
    settings: Settings, redis_client: fakeredis.aioredis.FakeRedis
) -> None:
    _mock_three(coingecko=0.10, paprika=0.11, kraken=5.00)
    assert await _oracle(settings, redis_client).get_rate() == Decimal("0.10500000")


@respx.mock
async def test_two_wildly_disagreeing_sources_fail_closed(
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    settings = oracle_settings(oracle_source_kraken="")
    respx.get(CG).mock(return_value=httpx.Response(200, json={"kaspa": {"usd": 0.10}}))
    respx.get(CP).mock(return_value=httpx.Response(200, json={"quotes": {"USD": {"price": 5.00}}}))
    with pytest.raises(OracleUnavailableError, match="outlier"):
        await _oracle(settings, redis_client).get_rate()


@respx.mock
async def test_garbage_payloads_are_unusable(settings: Settings, redis_client: fakeredis.aioredis.FakeRedis) -> None:
    _mock_three(
        coingecko=httpx.Response(200, json={"kaspa": {"usd": "n/a"}}),
        paprika=httpx.Response(200, text="<html>"),
        kraken=httpx.Response(200, json={"error": [], "result": {}}),
    )
    with pytest.raises(OracleUnavailableError):
        await _oracle(settings, redis_client).get_rate()


@respx.mock
async def test_fresh_fetch_overwrites_last(settings: Settings, redis_client: fakeredis.aioredis.FakeRedis) -> None:
    clock = Clock()
    _mock_three(coingecko=0.2, paprika=0.2, kraken=0.2)
    await redis_client.set(
        RATE_LAST_KEY, _encode_quoted(QuotedRate(rate=Decimal("0.01"), quoted_at=clock.when - timedelta(hours=1)))
    )
    assert await _oracle(settings, redis_client, clock).get_rate() == Decimal("0.20000000")
    assert await redis_client.get(RATE_LAST_KEY) == _encode_quoted(
        QuotedRate(rate=Decimal("0.20000000"), quoted_at=clock.when)
    )


@respx.mock
async def test_mexc_is_included_when_configured(
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    settings = oracle_settings(oracle_source_mexc=MX, oracle_source_kraken="")
    respx.get(CG).mock(return_value=httpx.Response(200, json={"kaspa": {"usd": 0.10}}))
    respx.get(CP).mock(return_value=httpx.Response(200, json={"quotes": {"USD": {"price": 0.10}}}))
    respx.get(MX).mock(return_value=httpx.Response(200, json={"symbol": "KASUSDT", "price": "0.16"}))
    # median of 0.10, 0.10, 0.16 = 0.10
    assert await _oracle(settings, redis_client).get_rate() == Decimal("0.10000000")
