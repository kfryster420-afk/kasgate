"""Settings-driven rate limits, fail-closed/open behaviour, unpaid /p/* limiting, and Redis outages."""

from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from kaspa import PrivateKey
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.models import Settlement
from app.proxy import lock_key
from app.schemas import HEADER_PAYMENT_REQUIRED, HEADER_PAYMENT_SIGNATURE, X402ErrorBody
from tests.conftest import MERCHANT_ADDRESS, Seed, StubEngine, Upstream, make_asgi_client, make_signature_header

TX = "cd" * 32

SettingsOverride = Callable[..., None]


@pytest.fixture
def override_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[SettingsOverride]:
    def apply(**values: object) -> None:
        for name, value in values.items():
            monkeypatch.setenv(name.upper(), str(value))
        get_settings.cache_clear()

    yield apply
    monkeypatch.undo()
    get_settings.cache_clear()


class DownPipeline:
    async def __aenter__(self) -> "DownPipeline":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def incr(self, *args: Any) -> None:
        return None

    def expire(self, *args: Any, **kwargs: Any) -> None:
        return None

    def ttl(self, *args: Any) -> None:
        return None

    async def execute(self) -> None:
        raise RedisConnectionError("redis is down")


class DownRedis:
    """Every command fails like an unreachable server."""

    def pipeline(self, *args: Any, **kwargs: Any) -> DownPipeline:
        return DownPipeline()

    def __getattr__(self, name: str) -> Callable[..., Any]:
        async def fail(*args: Any, **kwargs: Any) -> None:
            raise RedisConnectionError("redis is down")

        return fail


class ForbiddenRedis:
    """Fails the test if anything touches Redis."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"Redis was touched: {name}")


def new_address(index: int) -> str:
    return PrivateKey(f"{index:02x}" * 32).to_address("testnet").to_string()


async def register(client: httpx.AsyncClient, index: int) -> int:
    response = await client.post("/v1/merchants", json={"kaspa_address": new_address(index), "display_name": "x"})
    return response.status_code


# ---------------------------------------------------------------- limits come from settings


async def test_v1_429_keeps_the_detail_body(client: httpx.AsyncClient, override_settings: SettingsOverride) -> None:
    override_settings(rl_merchant_create_per_hour=1)
    assert await register(client, 1) == 201
    blocked = await client.post("/v1/merchants", json={"kaspa_address": new_address(2), "display_name": "x"})
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "rate limit exceeded; retry later"}
    assert int(blocked.headers["retry-after"]) > 0


async def test_merchant_limit_comes_from_settings(
    client: httpx.AsyncClient, override_settings: SettingsOverride
) -> None:
    override_settings(rl_merchant_create_per_hour=2)
    assert [await register(client, i) for i in range(1, 5)] == [201, 201, 429, 429]


async def test_challenge_limit_comes_from_settings(
    client: httpx.AsyncClient, override_settings: SettingsOverride
) -> None:
    override_settings(rl_auth_challenge_per_minute=3)
    codes = [
        (await client.post("/v1/auth/challenge", json={"kaspa_address": new_address(i)})).status_code
        for i in range(1, 6)
    ]
    assert codes == [200, 200, 200, 429, 429]


async def test_verify_limit_comes_from_settings(client: httpx.AsyncClient, override_settings: SettingsOverride) -> None:
    override_settings(rl_auth_verify_per_minute=2)
    body = {
        "kaspa_address": MERCHANT_ADDRESS,
        "challenge": "KasGate login\nAddress: x\nNonce: y\nExpires: z",
        "public_key": "02" + "11" * 32,
        "signature": "ab" * 64,
    }
    codes = [(await client.post("/v1/auth/verify", json=body)).status_code for _ in range(4)]
    assert codes == [401, 401, 429, 429]


async def test_limit_default_values(client: httpx.AsyncClient) -> None:
    settings = get_settings()
    assert settings.rate_limit_enabled is True
    assert settings.rate_limit_fail_closed is True
    assert settings.rl_merchant_create_per_hour == 5
    assert settings.rl_auth_challenge_per_minute == 10
    assert settings.rl_auth_verify_per_minute == 10
    assert settings.rl_unpaid_proxy_per_minute == 300


# ---------------------------------------------------------------- unpaid proxy limit


async def test_unpaid_proxy_requests_are_limited_per_ip(
    client: httpx.AsyncClient, seeded: Seed, redis: Any, override_settings: SettingsOverride
) -> None:
    override_settings(rl_unpaid_proxy_per_minute=3)
    codes = [(await client.get("/p/weather")).status_code for _ in range(5)]
    assert codes == [402, 402, 402, 429, 429]
    blocked = await client.get("/p/weather")
    assert blocked.status_code == 429
    assert blocked.json()["error"] == "rate_limited"
    assert blocked.json()["accepts"] is None
    assert 0 < int(blocked.headers["retry-after"]) <= 60
    assert X402ErrorBody.model_validate(blocked.json()).error == "rate_limited"
    assert 0 < await redis.ttl("kasgate:rl:proxy_unpaid:ip:127.0.0.1") <= 60
    async with make_asgi_client(client, "10.2.2.2") as other:
        assert (await other.get("/p/weather")).status_code == 402


async def test_unknown_slugs_also_count_as_unpaid(
    client: httpx.AsyncClient, seeded: Seed, override_settings: SettingsOverride
) -> None:
    override_settings(rl_unpaid_proxy_per_minute=2)
    codes = [(await client.get("/p/ghost")).status_code for _ in range(3)]
    assert codes == [404, 404, 429]


async def test_paid_requests_are_never_limited(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any, override_settings: SettingsOverride
) -> None:
    override_settings(rl_unpaid_proxy_per_minute=1)
    for i in range(1, 6):
        response = await client.get(
            "/p/weather", headers={HEADER_PAYMENT_SIGNATURE: make_signature_header(f"{i:064x}")}
        )
        assert response.status_code == 200
    assert await redis.get("kasgate:rl:proxy_unpaid:ip:127.0.0.1") is None
    assert (await client.get("/p/weather")).status_code == 402
    assert (await client.get("/p/weather")).status_code == 429


async def test_directory_is_not_limited(
    client: httpx.AsyncClient, seeded: Seed, override_settings: SettingsOverride
) -> None:
    override_settings(rl_unpaid_proxy_per_minute=1, rl_merchant_create_per_hour=1, rl_auth_challenge_per_minute=1)
    assert [(await client.get("/v1/directory")).status_code for _ in range(15)] == [200] * 15
    assert [(await client.get("/v1/quote/weather")).status_code for _ in range(5)] == [200] * 5


# ---------------------------------------------------------------- disabled


async def test_disabled_limiter_never_limits_and_never_touches_redis(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seed, override_settings: SettingsOverride
) -> None:
    override_settings(
        rate_limit_enabled="false",
        rl_merchant_create_per_hour=1,
        rl_auth_challenge_per_minute=1,
        rl_auth_verify_per_minute=1,
        rl_unpaid_proxy_per_minute=1,
    )
    app.state.redis = ForbiddenRedis()
    assert [await register(client, i) for i in range(6, 10)] == [201] * 4
    body = {
        "kaspa_address": MERCHANT_ADDRESS,
        "challenge": "not a real challenge",
        "public_key": "02" + "11" * 32,
        "signature": "ab" * 64,
    }
    assert [(await client.post("/v1/auth/verify", json=body)).status_code for _ in range(4)] == [401] * 4
    assert [(await client.get("/p/ghost")).status_code for _ in range(5)] == [404] * 5


# ---------------------------------------------------------------- Redis outage


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/v1/merchants", {"kaspa_address": new_address(30), "display_name": "x"}),
        ("POST", "/v1/auth/challenge", {"kaspa_address": MERCHANT_ADDRESS}),
        (
            "POST",
            "/v1/auth/verify",
            {
                "kaspa_address": MERCHANT_ADDRESS,
                "challenge": "x" * 20,
                "public_key": "02" + "11" * 32,
                "signature": "ab" * 64,
            },
        ),
        ("GET", "/p/weather", None),
    ],
)
async def test_fail_closed_returns_503_when_redis_is_down(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seed, method: str, path: str, body: dict[str, Any] | None
) -> None:
    assert get_settings().rate_limit_fail_closed is True
    app.state.redis = DownRedis()
    response = await client.request(method, path, json=body)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    if path.startswith("/p/"):
        assert X402ErrorBody.model_validate(response.json()).error == "service_unavailable"
    else:
        assert response.json() == {"detail": "service temporarily unavailable"}


async def test_fail_open_passes_when_redis_is_down(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seed, override_settings: SettingsOverride
) -> None:
    override_settings(rate_limit_fail_closed="false")
    app.state.redis = DownRedis()
    assert await register(client, 31) == 201
    unpaid = await client.get("/p/weather")
    assert unpaid.status_code == 402
    assert HEADER_PAYMENT_REQUIRED in unpaid.headers


async def test_paid_proxy_request_is_503_not_500_when_redis_is_down(
    client: httpx.AsyncClient,
    app: FastAPI,
    seeded: Seed,
    stub_engine: StubEngine,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    override_settings: SettingsOverride,
) -> None:
    for fail_closed in ("true", "false"):
        override_settings(rate_limit_fail_closed=fail_closed)
        app.state.redis = DownRedis()
        response = await client.get("/p/weather", headers={HEADER_PAYMENT_SIGNATURE: make_signature_header(TX)})
        assert response.status_code == 503
        assert response.json()["error"] == "service_unavailable"
        assert response.headers["retry-after"] == "5"
        X402ErrorBody.model_validate(response.json())
    assert stub_engine.calls == []
    assert upstream.requests == []
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Settlement)) == 0


async def test_lock_is_not_counted_as_taken_after_a_redis_outage(
    client: httpx.AsyncClient, app: FastAPI, seeded: Seed, stub_engine: StubEngine, redis: Any
) -> None:
    healthy = app.state.redis
    app.state.redis = DownRedis()
    header = {HEADER_PAYMENT_SIGNATURE: make_signature_header(TX)}
    assert (await client.get("/p/weather", headers=header)).status_code == 503
    app.state.redis = healthy
    assert await redis.get(lock_key(TX)) is None
    assert (await client.get("/p/weather", headers=header)).status_code == 200
    assert stub_engine.calls == [TX]
