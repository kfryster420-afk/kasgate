"""CONTRACT v2.12 free trial calls: signed per-key trials, atomic enforcement, daily cap, refunds, no fees."""

import asyncio
import base64
import json
import uuid
from typing import Any

import httpx
import pytest
from kaspa import PrivateKey, sign_message
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import (
    Endpoint,
    PrepaidDebit,
    Settlement,
    TrialCall,
    TrialDailyCount,
    TrialUsage,
    WebhookDelivery,
    WebhookEvent,
)
from app.proxy import trial_challenge_key
from app.schemas import (
    HEADER_PAYMENT_REQUIRED,
    HEADER_PAYMENT_SIGNATURE,
    HEADER_PREPAID_DEBIT,
    HEADER_TRIAL_CALL,
    HEADER_TRIAL_LIMIT,
    HEADER_TRIAL_OFFER,
    HEADER_TRIAL_RECEIPT,
    HEADER_TRIAL_REMAINING,
    TRIAL_SCHEME,
    TrialCallHeader,
    TrialChallenge,
    TrialOffer,
    TrialReceipt,
    TrialStatusRead,
    build_prepaid_debit_message,
    prepaid_body_hash,
)
from tests.conftest import (
    OTHER_ADDRESS,
    OTHER_KEY,
    PAYER_ADDRESS,
    PAYER_KEY,
    Seed,
    Upstream,
    bearer,
    make_asgi_client,
    make_signature_header,
    seed_endpoint,
)

# ---------------------------------------------------------------- helpers


def key_and_address(seed_byte: int) -> tuple[PrivateKey, str]:
    key = PrivateKey(f"{seed_byte:02x}" * 32)
    return key, key.to_address("testnet").to_string()


async def trial_challenge(
    client: httpx.AsyncClient,
    *,
    slug: str = "weather",
    path: str | None = None,
    method: str = "GET",
    body: bytes = b"",
    payer: str = PAYER_ADDRESS,
) -> httpx.Response:
    return await client.post(
        "/v1/trials/challenge",
        json={
            "slug": slug,
            "payer_address": payer,
            "method": method,
            "path": path if path is not None else f"/p/{slug}",
            "body_hash": prepaid_body_hash(body),
        },
    )


async def get_trial_challenge(client: httpx.AsyncClient, **kwargs: Any) -> TrialChallenge:
    response = await trial_challenge(client, **kwargs)
    assert response.status_code == 201, response.text
    return TrialChallenge.model_validate(response.json())


def trial_header(challenge: TrialChallenge, key: PrivateKey = PAYER_KEY, *, message: str | None = None) -> str:
    signature = sign_message(message if message is not None else challenge.message, key)
    return TrialCallHeader(nonce=challenge.nonce, signature=signature).to_header()


async def trial_call(
    client: httpx.AsyncClient,
    *,
    slug: str = "weather",
    key: PrivateKey = PAYER_KEY,
    payer: str = PAYER_ADDRESS,
    path: str | None = None,
) -> httpx.Response:
    target = path if path is not None else f"/p/{slug}"
    challenge = await get_trial_challenge(client, slug=slug, path=target, payer=payer)
    return await client.get(target, headers={HEADER_TRIAL_CALL: trial_header(challenge, key)})


def receipt_of(response: httpx.Response) -> TrialReceipt:
    return TrialReceipt.model_validate_json(base64.b64decode(response.headers[HEADER_TRIAL_RECEIPT]))


async def count(maker: async_sessionmaker[AsyncSession], model: Any, *where: Any) -> int:
    async with maker() as session:
        query = select(func.count()).select_from(model)
        for clause in where:
            query = query.where(clause)
        return int((await session.execute(query)).scalar_one())


async def usage_of(maker: async_sessionmaker[AsyncSession], endpoint_id: uuid.UUID) -> list[TrialUsage]:
    async with maker() as session:
        return list((await session.execute(select(TrialUsage).where(TrialUsage.endpoint_id == endpoint_id))).scalars())


@pytest.fixture
async def trial_shop(sessionmaker: async_sessionmaker[AsyncSession]) -> Seed:
    """`weather` (20 KAS on-chain) with 2 free trial calls per buyer key."""
    return await seed_endpoint(sessionmaker, free_trial_calls=2)


# ---------------------------------------------------------------- offer + challenge


async def test_402_advertises_the_trial_offer(client: httpx.AsyncClient, trial_shop: Seed) -> None:
    response = await client.get("/p/weather")
    assert response.status_code == 402
    assert HEADER_PAYMENT_REQUIRED in response.headers
    offer = TrialOffer.from_header(response.headers[HEADER_TRIAL_OFFER])
    assert offer.scheme == TRIAL_SCHEME
    assert offer.freeTrialCalls == 2
    assert offer.challengeUrl == "http://gateway.test/v1/trials/challenge"
    assert offer.statusUrl == "http://gateway.test/v1/trials/status?slug=weather"


async def test_no_trial_offer_without_free_calls(client: httpx.AsyncClient, seeded: Seed) -> None:
    response = await client.get("/p/weather")
    assert response.status_code == 402
    assert HEADER_TRIAL_OFFER not in response.headers
    challenge = await trial_challenge(client)
    assert challenge.status_code == 402
    assert challenge.json()["error"] == "trial_exhausted"


async def test_challenge_binds_the_request(client: httpx.AsyncClient, trial_shop: Seed) -> None:
    challenge = await get_trial_challenge(client, path="/p/weather?city=hanoi")
    lines = challenge.message.splitlines()
    assert "Purpose: kasgate:v1:trial-call" in lines
    assert f"Address: {PAYER_ADDRESS}" in lines
    assert f"Merchant: {trial_shop.merchant_id}" in lines
    assert "Method: GET" in lines
    assert "Path: /p/weather?city=hanoi" in lines
    assert f"Nonce: {challenge.nonce}" in lines
    assert (challenge.free_trial_calls, challenge.calls_used, challenge.remaining) == (2, 0, 2)


async def test_challenge_rejects_non_p2pk_and_unknown(client: httpx.AsyncClient, trial_shop: Seed) -> None:
    p2sh = "kaspatest:pqtsrmfkfsm2xvz7ntttc5tssqa0lrlywm2hnnm5x0zqljm0m8vcsch36lw7l"
    response = await trial_challenge(client, payer=p2sh)
    assert response.status_code in (400, 422), response.text
    assert (await trial_challenge(client, slug="nope")).status_code == 404
    assert (await trial_challenge(client, method="DELETE")).status_code == 405


# ---------------------------------------------------------------- happy path + exhaustion


async def test_trial_calls_then_normal_402(
    client: httpx.AsyncClient, trial_shop: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    first = await trial_call(client)
    assert first.status_code == 200, first.text
    assert first.json() == {"ok": True}
    assert first.headers[HEADER_TRIAL_LIMIT] == "2"
    assert first.headers[HEADER_TRIAL_REMAINING] == "1"
    receipt = receipt_of(first)
    assert (receipt.status, receipt.limit, receipt.remaining) == ("used", 2, 1)
    # the upstream never sees KasGate payment or trial headers
    forwarded = upstream.requests[-1]
    assert HEADER_TRIAL_CALL.lower() not in {k.lower() for k in forwarded.headers}

    second = await trial_call(client)
    assert second.status_code == 200
    assert second.headers[HEADER_TRIAL_REMAINING] == "0"

    # used up: the challenge route says so and a plain call gets the normal on-chain 402 without a trial offer
    exhausted = await trial_challenge(client)
    assert exhausted.status_code == 402
    assert exhausted.json()["error"] == "trial_exhausted"
    status = await client.get("/v1/trials/status", params={"slug": "weather", "payer_address": PAYER_ADDRESS})
    assert TrialStatusRead.model_validate(status.json()).remaining == 0

    # never a fee, never a settlement, never a prepaid debit
    assert await count(sessionmaker, Settlement) == 0
    assert await count(sessionmaker, PrepaidDebit) == 0
    assert await count(sessionmaker, TrialCall) == 2
    [usage] = await usage_of(sessionmaker, trial_shop.endpoint_id)
    assert usage.calls_used == 2

    # another key still has its own trial
    other = await trial_call(client, key=OTHER_KEY, payer=OTHER_ADDRESS)
    assert other.status_code == 200
    assert other.headers[HEADER_TRIAL_REMAINING] == "1"


async def test_exhausted_trial_call_returns_normal_402(
    client: httpx.AsyncClient, trial_shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    challenge = await get_trial_challenge(client)  # issued while calls were left
    async with sessionmaker() as session:
        await session.execute(update(Endpoint).values(free_trial_calls=1))
        await session.commit()
    assert (await trial_call(client)).status_code == 200
    late = await client.get("/p/weather", headers={HEADER_TRIAL_CALL: trial_header(challenge)})
    assert late.status_code == 402
    assert late.json()["error"] == "trial_exhausted"
    assert HEADER_PAYMENT_REQUIRED in late.headers  # pay straight away with the fresh on-chain challenge
    assert late.headers[HEADER_TRIAL_REMAINING] == "0"
    assert HEADER_TRIAL_OFFER not in late.headers
    assert await count(sessionmaker, TrialCall) == 1


async def test_prepaid_only_endpoint_trial(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await seed_endpoint(sessionmaker, slug="cheapo", price_sompi=1_000, prepaid_only=True, free_trial_calls=1)
    response = await client.get("/p/cheapo")
    assert response.status_code == 402
    assert HEADER_TRIAL_OFFER in response.headers
    assert (await trial_call(client, slug="cheapo")).status_code == 200
    again = await client.get("/p/cheapo")
    assert again.status_code == 402
    assert again.json()["error"] != "trial_exhausted"


# ---------------------------------------------------------------- races


async def test_concurrent_trial_calls_never_overrun(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession], upstream: Upstream
) -> None:
    seed = await seed_endpoint(sessionmaker, free_trial_calls=3)
    challenges = [await get_trial_challenge(client) for _ in range(10)]
    responses = await asyncio.gather(
        *(client.get("/p/weather", headers={HEADER_TRIAL_CALL: trial_header(c)}) for c in challenges)
    )
    codes = sorted(r.status_code for r in responses)
    assert codes == [200] * 3 + [402] * 7, [r.text for r in responses]
    assert all(r.json()["error"] == "trial_exhausted" for r in responses if r.status_code == 402)
    assert len(upstream.requests) == 3
    assert await count(sessionmaker, TrialCall) == 3
    [usage] = await usage_of(sessionmaker, seed.endpoint_id)
    assert usage.calls_used == 3
    remaining = sorted(r.headers[HEADER_TRIAL_REMAINING] for r in responses if r.status_code == 200)
    assert remaining == ["0", "1", "2"]


async def test_daily_cap_is_atomic_across_keys(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession], upstream: Upstream
) -> None:
    seed = await seed_endpoint(sessionmaker, free_trial_calls=5, trial_daily_cap=2)
    buyers = [key_and_address(0x30 + i) for i in range(6)]
    challenges = [(key, await get_trial_challenge(client, payer=address)) for key, address in buyers]
    responses = await asyncio.gather(
        *(client.get("/p/weather", headers={HEADER_TRIAL_CALL: trial_header(c, key)}) for key, c in challenges)
    )
    assert sorted(r.status_code for r in responses) == [200, 200, 402, 402, 402, 402]
    assert len(upstream.requests) == 2
    async with sessionmaker() as session:
        daily = (
            await session.execute(select(TrialDailyCount.calls).where(TrialDailyCount.endpoint_id == seed.endpoint_id))
        ).scalar_one()
    assert daily == 2
    # keys turned away by the cap were not charged a trial call
    assert sum(u.calls_used for u in await usage_of(sessionmaker, seed.endpoint_id)) == 2
    # and new challenges are refused while the cap is reached
    _, fresh = key_and_address(0x50)
    refused = await trial_challenge(client, payer=fresh)
    assert refused.status_code == 402


# ---------------------------------------------------------------- forgery + binding


async def test_forged_signatures_are_rejected(
    client: httpx.AsyncClient, trial_shop: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    challenge = await get_trial_challenge(client)
    # another key signing the buyer's challenge (spending someone else's trial)
    wrong_key = await client.get("/p/weather", headers={HEADER_TRIAL_CALL: trial_header(challenge, OTHER_KEY)})
    assert wrong_key.status_code == 402
    assert wrong_key.json()["error"] == "invalid_payment_header"
    # the right key over a different text
    tampered = challenge.message.replace("Path: /p/weather", "Path: /p/weather?x=1")
    altered = await client.get("/p/weather", headers={HEADER_TRIAL_CALL: trial_header(challenge, message=tampered)})
    assert altered.status_code == 402
    # a prepaid-debit signature cannot be replayed as a trial (different Purpose line)
    prepaid_text = build_prepaid_debit_message(
        domain="gateway.test",
        address=PAYER_ADDRESS,
        merchant_id=trial_shop.merchant_id,
        method="GET",
        path="/p/weather",
        body_hash=challenge.body_hash,
        amount_sompi=1,
        nonce=challenge.nonce,
        expires_at=challenge.expires_at,
    )
    cross = await client.get("/p/weather", headers={HEADER_TRIAL_CALL: trial_header(challenge, message=prepaid_text)})
    assert cross.status_code == 402
    # garbage signature + malformed header
    garbage = TrialCallHeader(nonce=challenge.nonce, signature="00" * 64).to_header()
    assert (await client.get("/p/weather", headers={HEADER_TRIAL_CALL: garbage})).status_code == 402
    assert (await client.get("/p/weather", headers={HEADER_TRIAL_CALL: "not-base64!"})).status_code == 402
    # unknown nonce
    unknown = TrialCallHeader(nonce="ab" * 16, signature=sign_message(challenge.message, PAYER_KEY)).to_header()
    assert (await client.get("/p/weather", headers={HEADER_TRIAL_CALL: unknown})).json()["error"] == "payment_expired"

    assert upstream.requests == []
    assert await count(sessionmaker, TrialCall) == 0
    # the genuine signature still works afterwards
    ok = await client.get("/p/weather", headers={HEADER_TRIAL_CALL: trial_header(challenge)})
    assert ok.status_code == 200


async def test_trial_is_bound_to_path_method_and_body(client: httpx.AsyncClient, trial_shop: Seed) -> None:
    challenge = await get_trial_challenge(client, path="/p/weather?city=hanoi")
    header = {HEADER_TRIAL_CALL: trial_header(challenge)}
    assert (await client.get("/p/weather?city=paris", headers=header)).status_code == 402
    assert (await client.post("/p/weather?city=hanoi", headers=header, content=b"{}")).status_code == 402
    assert (await client.get("/p/weather?city=hanoi", headers=header)).status_code == 200


async def test_trial_nonce_is_single_use(client: httpx.AsyncClient, trial_shop: Seed) -> None:
    challenge = await get_trial_challenge(client)
    header = {HEADER_TRIAL_CALL: trial_header(challenge)}
    assert (await client.get("/p/weather", headers=header)).status_code == 200
    replay = await client.get("/p/weather", headers=header)
    assert replay.status_code == 409
    assert replay.json()["error"] == "payment_replayed"


async def test_expired_trial_challenge(client: httpx.AsyncClient, trial_shop: Seed, redis: Any) -> None:
    challenge = await get_trial_challenge(client)
    key = trial_challenge_key(challenge.nonce)
    stored = json.loads(await redis.get(key))
    stored["expiresAt"] = "2020-01-01T00:00:00+00:00"
    await redis.set(key, json.dumps(stored))
    response = await client.get("/p/weather", headers={HEADER_TRIAL_CALL: trial_header(challenge)})
    assert response.status_code == 402
    assert response.json()["error"] == "payment_expired"


async def test_trial_cannot_be_combined_with_payment(client: httpx.AsyncClient, trial_shop: Seed) -> None:
    challenge = await get_trial_challenge(client)
    for other in (HEADER_PAYMENT_SIGNATURE, HEADER_PREPAID_DEBIT):
        response = await client.get(
            "/p/weather",
            headers={HEADER_TRIAL_CALL: trial_header(challenge), other: make_signature_header(None, challenge.nonce)},
        )
        assert response.status_code == 402
        assert response.json()["error"] == "invalid_payment_header"


# ---------------------------------------------------------------- limits + refunds


async def test_trial_calls_are_rate_limited_per_ip(
    client: httpx.AsyncClient, trial_shop: Seed, override_settings: Any
) -> None:
    override_settings(rl_trial_calls_per_ip_per_hour=1)
    assert (await trial_call(client, key=OTHER_KEY, payer=OTHER_ADDRESS)).status_code == 200
    limited = await trial_call(client)
    assert limited.status_code == 429
    elsewhere = make_asgi_client(client, "198.51.100.7")
    async with elsewhere:
        assert (await trial_call(elsewhere)).status_code == 200


async def test_upstream_5xx_gives_the_trial_back(
    client: httpx.AsyncClient, trial_shop: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    upstream.handler = lambda request: httpx.Response(503, json={"down": True})
    failed = await trial_call(client)
    assert failed.status_code == 503
    assert receipt_of(failed).status == "refunded"
    assert failed.headers[HEADER_TRIAL_REMAINING] == "2"
    [usage] = await usage_of(sessionmaker, trial_shop.endpoint_id)
    assert usage.calls_used == 0
    async with sessionmaker() as session:
        call = (await session.execute(select(TrialCall))).scalar_one()
    assert (call.status, call.upstream_status_code) == ("refunded", 503)
    assert call.refunded_at is not None

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    upstream.handler = boom
    unreachable = await trial_call(client)
    assert unreachable.status_code == 502
    assert receipt_of(unreachable).status == "refunded"
    [usage] = await usage_of(sessionmaker, trial_shop.endpoint_id)
    assert usage.calls_used == 0


async def test_upstream_4xx_still_uses_the_trial(
    client: httpx.AsyncClient, trial_shop: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    upstream.handler = lambda request: httpx.Response(404, json={"missing": True})
    response = await trial_call(client)
    assert response.status_code == 404
    assert receipt_of(response).status == "used"
    [usage] = await usage_of(sessionmaker, trial_shop.endpoint_id)
    assert usage.calls_used == 1


# ---------------------------------------------------------------- merchant side


async def test_merchant_trial_settings_validation(client: httpx.AsyncClient, seeded: Seed) -> None:
    base = {
        "slug": "trialapi",
        "name": "Trial API",
        "upstream_url": "https://api.upstream.test/t",
        "price_sompi": 100_000_000,
        "allowed_methods": ["GET"],
    }
    headers = bearer(seeded.api_key)
    for bad in (-1, 101, "3", 2.5, None, True):
        response = await client.post("/v1/endpoints", json={**base, "free_trial_calls": bad}, headers=headers)
        assert response.status_code == 422, (bad, response.text)
    for bad_cap in (0, 100_001, "5"):
        response = await client.post(
            "/v1/endpoints", json={**base, "free_trial_calls": 3, "trial_daily_cap": bad_cap}, headers=headers
        )
        assert response.status_code == 422, (bad_cap, response.text)
    created = await client.post("/v1/endpoints", json={**base, "free_trial_calls": 3}, headers=headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["free_trial_calls"], body["trial_daily_cap"]) == (3, 100)
    patched = await client.patch(
        f"/v1/endpoints/{body['id']}", json={"free_trial_calls": 0, "trial_daily_cap": 50}, headers=headers
    )
    assert patched.status_code == 200, patched.text
    assert (patched.json()["free_trial_calls"], patched.json()["trial_daily_cap"]) == (0, 50)
    nulled = await client.patch(f"/v1/endpoints/{body['id']}", json={"free_trial_calls": None}, headers=headers)
    assert nulled.status_code == 422


async def test_directory_and_catalog_show_trials(client: httpx.AsyncClient, trial_shop: Seed) -> None:
    directory = await client.get("/v1/directory")
    assert directory.status_code == 200
    [entry] = directory.json()["endpoints"]
    assert entry["free_trial_calls"] == 2


async def test_merchant_trial_call_log(
    client: httpx.AsyncClient, trial_shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await trial_call(client)
    log = await client.get("/v1/trials/calls", headers=bearer(trial_shop.api_key))
    assert log.status_code == 200
    [row] = log.json()
    assert (row["payer_address"], row["status"], row["request_path"]) == (PAYER_ADDRESS, "used", "/p/weather")
    assert (await client.get("/v1/trials/calls")).status_code == 401
    other = await seed_endpoint(sessionmaker, slug="other", address=OTHER_ADDRESS)
    assert (await client.get("/v1/trials/calls", headers=bearer(other.api_key))).json() == []


async def test_trial_use_emits_webhook_event(
    client: httpx.AsyncClient, trial_shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    created = await client.post(
        "/v1/webhooks",
        json={"url": "https://hooks.example.com/kasgate", "event_types": ["trial.used"]},
        headers=bearer(trial_shop.api_key),
    )
    assert created.status_code == 201, created.text
    await trial_call(client)
    async with sessionmaker() as session:
        events = list(
            (await session.execute(select(WebhookEvent).where(WebhookEvent.event_type == "trial.used"))).scalars()
        )
    assert len(events) == 1
    data = events[0].payload["data"]
    assert (data["endpoint_slug"], data["payer_address"], data["remaining"]) == ("weather", PAYER_ADDRESS, 1)
    assert await count(sessionmaker, WebhookDelivery) == 1
