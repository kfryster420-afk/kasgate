"""Wallet-signature login, key rotation, merchant uniqueness, and the Redis rate limits."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from kaspa import PrivateKey, sign_message
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Merchant
from app.schemas import AuthChallenge, MerchantCreated
from app.security import (
    AUTH_CHALLENGE_TTL_SECONDS,
    LoginChallenge,
    challenge_key,
    format_login_challenge,
    hash_api_key,
    parse_login_challenge,
)
from tests.conftest import (
    MERCHANT_ADDRESS,
    MERCHANT_KEY,
    OTHER_ADDRESS,
    OTHER_KEY,
    Seed,
    make_asgi_client,
)


def bearer(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


async def register(
    client: httpx.AsyncClient, address: str = MERCHANT_ADDRESS, email: str | None = None
) -> MerchantCreated:
    body: dict[str, Any] = {"kaspa_address": address, "display_name": "Wallet Merchant"}
    if email:
        body["email"] = email
    response = await client.post("/v1/merchants", json=body)
    assert response.status_code == 201, response.text
    return MerchantCreated.model_validate(response.json())


async def get_challenge(client: httpx.AsyncClient, address: str = MERCHANT_ADDRESS) -> AuthChallenge:
    response = await client.post("/v1/auth/challenge", json={"kaspa_address": address})
    assert response.status_code == 200, response.text
    return AuthChallenge.model_validate(response.json())


def verify_body(
    challenge: str,
    key: PrivateKey = MERCHANT_KEY,
    address: str = MERCHANT_ADDRESS,
    *,
    x_only: bool = False,
    signed: str | None = None,
) -> dict[str, str]:
    public_key = key.to_public_key().to_string()
    return {
        "kaspa_address": address,
        "challenge": challenge,
        "public_key": public_key[2:] if x_only else public_key,
        "signature": sign_message(signed if signed is not None else challenge, key),
    }


# ---------------------------------------------------------------- challenge


async def test_challenge_format_storage_and_ttl(client: httpx.AsyncClient, redis: Any) -> None:
    before = datetime.now(UTC)
    challenge = await get_challenge(client)
    lines = challenge.challenge.split("\n")
    assert lines[0] == "KasGate login"
    assert lines[1] == f"Address: {MERCHANT_ADDRESS}"
    assert lines[2].startswith("Nonce: ") and len(lines[2]) == len("Nonce: ") + 32
    assert lines[3] == f"Expires: {challenge.expires_at.isoformat()}"
    assert before + timedelta(seconds=AUTH_CHALLENGE_TTL_SECONDS - 5) <= challenge.expires_at
    assert challenge.expires_at <= before + timedelta(seconds=AUTH_CHALLENGE_TTL_SECONDS + 5)
    nonce = lines[2].removeprefix("Nonce: ")
    key = f"kasgate:auth:{MERCHANT_ADDRESS}:{nonce}"
    assert key == challenge_key(MERCHANT_ADDRESS, nonce)
    assert await redis.get(key) == challenge.challenge
    assert 0 < await redis.ttl(key) <= 300
    other = await get_challenge(client)
    assert other.challenge != challenge.challenge


async def test_challenge_rejects_invalid_address(client: httpx.AsyncClient) -> None:
    assert (await client.post("/v1/auth/challenge", json={"kaspa_address": "nope"})).status_code == 422


def test_challenge_parser_is_strict() -> None:
    parsed = LoginChallenge(MERCHANT_ADDRESS, "ab" * 16, datetime(2030, 1, 1, tzinfo=UTC))
    message = format_login_challenge(parsed)
    assert parse_login_challenge(message) == parsed
    for broken in (
        message + "\n",
        message.replace("KasGate login", "Other login"),
        message.replace("Nonce: ", "Nonce:  "),
        message.replace("ab" * 16, "AB" * 16),
        message.replace("+00:00", ""),
        "x",
    ):
        with pytest.raises(ValueError):
            parse_login_challenge(broken)


# ---------------------------------------------------------------- verify


async def test_verify_rotates_key_and_invalidates_the_old_one(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    created = await register(client)
    challenge = await get_challenge(client)
    response = await client.post("/v1/auth/verify", json=verify_body(challenge.challenge))
    assert response.status_code == 200, response.text
    rotated = MerchantCreated.model_validate(response.json())
    assert rotated.merchant.id == created.merchant.id
    assert rotated.api_key.startswith("kg_")
    assert rotated.api_key != created.api_key

    assert (await client.get("/v1/merchants/me", headers=bearer(created.api_key))).status_code == 401
    assert (await client.get("/v1/merchants/me", headers=bearer(rotated.api_key))).status_code == 200
    async with sessionmaker() as session:
        stored = (await session.execute(select(Merchant).where(Merchant.id == created.merchant.id))).scalar_one()
        assert stored.api_key_hash == hash_api_key(rotated.api_key)


async def test_verify_accepts_x_only_public_key(client: httpx.AsyncClient) -> None:
    await register(client)
    challenge = await get_challenge(client)
    response = await client.post("/v1/auth/verify", json=verify_body(challenge.challenge, x_only=True))
    assert response.status_code == 200


async def test_challenge_is_single_use(client: httpx.AsyncClient) -> None:
    created = await register(client)
    challenge = await get_challenge(client)
    body = verify_body(challenge.challenge)
    first = await client.post("/v1/auth/verify", json=body)
    assert first.status_code == 200
    second = await client.post("/v1/auth/verify", json=body)
    assert second.status_code == 401
    me = await client.get("/v1/merchants/me", headers=bearer(first.json()["api_key"]))
    assert me.status_code == 200
    assert (await client.get("/v1/merchants/me", headers=bearer(created.api_key))).status_code == 401


async def test_concurrent_verifies_of_one_challenge_only_one_wins(client: httpx.AsyncClient) -> None:
    await register(client)
    challenge = await get_challenge(client)
    body = verify_body(challenge.challenge)
    responses = await asyncio.gather(*(client.post("/v1/auth/verify", json=body) for _ in range(6)))
    assert sorted(r.status_code for r in responses) == [200] + [401] * 5


async def test_signature_over_a_different_message_is_401(client: httpx.AsyncClient) -> None:
    created = await register(client)
    challenge = await get_challenge(client)
    response = await client.post("/v1/auth/verify", json=verify_body(challenge.challenge, signed="something else"))
    assert response.status_code == 401
    assert (await client.get("/v1/merchants/me", headers=bearer(created.api_key))).status_code == 200


async def test_signature_by_another_key_is_401(client: httpx.AsyncClient) -> None:
    created = await register(client)
    challenge = await get_challenge(client)
    body = verify_body(challenge.challenge)
    body["signature"] = sign_message(challenge.challenge, OTHER_KEY)
    assert (await client.post("/v1/auth/verify", json=body)).status_code == 401
    assert (await client.get("/v1/merchants/me", headers=bearer(created.api_key))).status_code == 200


async def test_valid_signature_from_wrong_address_key_is_401(client: httpx.AsyncClient) -> None:
    created = await register(client)
    challenge = await get_challenge(client)
    response = await client.post("/v1/auth/verify", json=verify_body(challenge.challenge, key=OTHER_KEY))
    assert response.status_code == 401
    assert (await client.get("/v1/merchants/me", headers=bearer(created.api_key))).status_code == 200


async def test_unknown_address_gets_the_same_challenge_shape_and_a_generic_401(
    client: httpx.AsyncClient, redis: Any
) -> None:
    await register(client)
    known = await client.post("/v1/auth/challenge", json={"kaspa_address": MERCHANT_ADDRESS})
    unknown = await client.post("/v1/auth/challenge", json={"kaspa_address": OTHER_ADDRESS})
    assert known.status_code == unknown.status_code == 200
    assert known.json().keys() == unknown.json().keys()
    assert known.headers.keys() == unknown.headers.keys()
    assert await redis.get(f"kasgate:auth:{OTHER_ADDRESS}:{unknown.json()['challenge'].split('Nonce: ')[1][:32]}")

    body = verify_body(unknown.json()["challenge"], key=OTHER_KEY, address=OTHER_ADDRESS)
    response = await client.post("/v1/auth/verify", json=body)
    assert response.status_code == 401
    assert response.json() == await _generic_failure(client)


async def _generic_failure(client: httpx.AsyncClient) -> dict[str, Any]:
    """The body every failed login must produce: a valid signature over a challenge that was never issued."""
    forged = LoginChallenge(MERCHANT_ADDRESS, "99" * 16, datetime.now(UTC) + timedelta(minutes=5))
    response = await client.post("/v1/auth/verify", json=verify_body(format_login_challenge(forged)))
    assert response.status_code == 401
    result: dict[str, Any] = response.json()
    return result


async def test_every_login_failure_has_the_identical_401_body(client: httpx.AsyncClient, redis: Any) -> None:
    await register(client)
    expected = await _generic_failure(client)
    bodies: list[dict[str, Any]] = []

    unknown = await get_challenge(client, OTHER_ADDRESS)
    bodies.append(await _post_verify(client, verify_body(unknown.challenge, OTHER_KEY, OTHER_ADDRESS)))

    bad_signature = await get_challenge(client)
    bodies.append(await _post_verify(client, verify_body(bad_signature.challenge, signed="other")))

    wrong_key = await get_challenge(client)
    bodies.append(await _post_verify(client, verify_body(wrong_key.challenge, key=OTHER_KEY)))

    used = await get_challenge(client)
    assert (await client.post("/v1/auth/verify", json=verify_body(used.challenge))).status_code == 200
    bodies.append(await _post_verify(client, verify_body(used.challenge)))

    expired = LoginChallenge(MERCHANT_ADDRESS, "cd" * 16, datetime.now(UTC) - timedelta(seconds=1))
    message = format_login_challenge(expired)
    await redis.set(challenge_key(MERCHANT_ADDRESS, expired.nonce), message, ex=300)
    bodies.append(await _post_verify(client, verify_body(message)))

    other_address = await get_challenge(client, OTHER_ADDRESS)
    bodies.append(await _post_verify(client, verify_body(other_address.challenge)))

    assert bodies == [expected] * len(bodies)


async def _post_verify(client: httpx.AsyncClient, body: dict[str, str]) -> dict[str, Any]:
    response = await client.post("/v1/auth/verify", json=body)
    assert response.status_code == 401
    result: dict[str, Any] = response.json()
    return result


async def test_any_verify_attempt_consumes_the_challenge(client: httpx.AsyncClient, redis: Any) -> None:
    created = await register(client)
    challenge = await get_challenge(client)
    bad = await client.post("/v1/auth/verify", json=verify_body(challenge.challenge, signed="nope"))
    assert bad.status_code == 401
    assert await redis.keys("kasgate:auth:*") == []
    good = await client.post("/v1/auth/verify", json=verify_body(challenge.challenge))
    assert good.status_code == 401
    assert (await client.get("/v1/merchants/me", headers=bearer(created.api_key))).status_code == 200


async def test_unknown_address_attempt_also_consumes_the_challenge(client: httpx.AsyncClient, redis: Any) -> None:
    challenge = await get_challenge(client, OTHER_ADDRESS)
    body = verify_body(challenge.challenge, key=OTHER_KEY, address=OTHER_ADDRESS)
    assert (await client.post("/v1/auth/verify", json=body)).status_code == 401
    assert await redis.keys("kasgate:auth:*") == []


@pytest.mark.parametrize("variant", ["base64_64", "base64_65", "upper_hex"])
async def test_signature_encodings_are_normalised(client: httpx.AsyncClient, variant: str) -> None:
    import base64

    await register(client)
    challenge = await get_challenge(client)
    body = verify_body(challenge.challenge)
    raw = bytes.fromhex(body["signature"])
    if variant == "base64_64":
        body["signature"] = base64.b64encode(raw).decode()
    elif variant == "base64_65":
        body["signature"] = base64.b64encode(b"\x1b" + raw).decode()
    else:
        body["signature"] = body["signature"].upper()
    response = await client.post("/v1/auth/verify", json=body)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("x_only", [False, True])
async def test_both_public_key_forms_work(client: httpx.AsyncClient, x_only: bool) -> None:
    await register(client)
    challenge = await get_challenge(client)
    body = verify_body(challenge.challenge, x_only=x_only)
    assert len(body["public_key"]) == (64 if x_only else 66)
    assert (await client.post("/v1/auth/verify", json=body)).status_code == 200


async def test_expired_challenge_is_401(client: httpx.AsyncClient, redis: Any) -> None:
    await register(client)
    expired = LoginChallenge(MERCHANT_ADDRESS, "cd" * 16, datetime.now(UTC) - timedelta(seconds=1))
    message = format_login_challenge(expired)
    await redis.set(challenge_key(MERCHANT_ADDRESS, expired.nonce), message, ex=300)
    assert (await client.post("/v1/auth/verify", json=verify_body(message))).status_code == 401


async def test_challenge_missing_from_redis_is_401(client: httpx.AsyncClient, redis: Any) -> None:
    await register(client)
    challenge = await get_challenge(client)
    await redis.flushall()
    assert (await client.post("/v1/auth/verify", json=verify_body(challenge.challenge))).status_code == 401


async def test_self_made_challenge_is_401(client: httpx.AsyncClient) -> None:
    await register(client)
    forged = LoginChallenge(MERCHANT_ADDRESS, "ef" * 16, datetime.now(UTC) + timedelta(minutes=5))
    assert (await client.post("/v1/auth/verify", json=verify_body(format_login_challenge(forged)))).status_code == 401


async def test_challenge_for_another_address_is_401(client: httpx.AsyncClient) -> None:
    await register(client)
    challenge = await get_challenge(client, OTHER_ADDRESS)
    assert (await client.post("/v1/auth/verify", json=verify_body(challenge.challenge))).status_code == 401


@pytest.mark.parametrize(
    "overrides",
    [
        {"public_key": "00" * 33},
        {"public_key": "zz" * 33},
        {"public_key": "02" * 20},
        {"signature": "ab" * 10},
        {"signature": "gg" * 64},
        {"challenge": "short"},
    ],
)
async def test_malformed_verify_fields_are_422(client: httpx.AsyncClient, overrides: dict[str, str]) -> None:
    await register(client)
    challenge = await get_challenge(client)
    body = {**verify_body(challenge.challenge), **overrides}
    assert (await client.post("/v1/auth/verify", json=body)).status_code == 422


async def test_all_zero_signature_is_401(client: httpx.AsyncClient) -> None:
    await register(client)
    challenge = await get_challenge(client)
    body = {**verify_body(challenge.challenge), "signature": "00" * 64}
    assert (await client.post("/v1/auth/verify", json=body)).status_code == 401


async def test_disabled_merchant_cannot_recover_a_key(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    created = await register(client)
    async with sessionmaker() as session:
        await session.execute(update(Merchant).where(Merchant.id == created.merchant.id).values(is_active=False))
        await session.commit()
    challenge = await get_challenge(client)
    assert (await client.post("/v1/auth/verify", json=verify_body(challenge.challenge))).status_code == 403


# ---------------------------------------------------------------- rotate-key


async def test_rotate_key_returns_new_key_and_kills_old(client: httpx.AsyncClient, seeded: Seed) -> None:
    response = await client.post("/v1/merchants/me/rotate-key", headers=bearer(seeded.api_key))
    assert response.status_code == 200
    rotated = MerchantCreated.model_validate(response.json())
    assert rotated.merchant.id == seeded.merchant_id
    assert rotated.api_key != seeded.api_key
    assert (await client.get("/v1/merchants/me", headers=bearer(seeded.api_key))).status_code == 401
    assert (await client.get("/v1/merchants/me", headers=bearer(rotated.api_key))).status_code == 200
    assert (await client.post("/v1/merchants/me/rotate-key", headers=bearer(seeded.api_key))).status_code == 401


async def test_rotate_key_requires_auth(client: httpx.AsyncClient) -> None:
    assert (await client.post("/v1/merchants/me/rotate-key")).status_code == 401


# ---------------------------------------------------------------- uniqueness & bounds


async def test_duplicate_address_and_email_are_409(client: httpx.AsyncClient) -> None:
    await register(client, MERCHANT_ADDRESS, email="a@example.com")
    same_address = await client.post("/v1/merchants", json={"kaspa_address": MERCHANT_ADDRESS, "display_name": "x"})
    assert same_address.status_code == 409
    assert "address" in same_address.json()["detail"]
    same_email = await client.post(
        "/v1/merchants", json={"kaspa_address": OTHER_ADDRESS, "display_name": "x", "email": "A@Example.com"}
    )
    assert same_email.status_code == 409
    assert "email" in same_email.json()["detail"]


async def test_concurrent_duplicate_registration_returns_one_409(client: httpx.AsyncClient) -> None:
    body = {"kaspa_address": MERCHANT_ADDRESS, "display_name": "x"}
    responses = await asyncio.gather(*(client.post("/v1/merchants", json=body) for _ in range(4)))
    assert sorted(r.status_code for r in responses) == [201, 409, 409, 409]


@pytest.mark.parametrize(
    ("query", "status_code"),
    [
        ("limit=1", 200),
        ("limit=200", 200),
        ("limit=0", 422),
        ("limit=201", 422),
        ("offset=-1", 422),
        ("limit=abc", 422),
    ],
)
async def test_settlement_pagination_bounds(
    client: httpx.AsyncClient, seeded: Seed, query: str, status_code: int
) -> None:
    response = await client.get(f"/v1/settlements?{query}", headers=bearer(seeded.api_key))
    assert response.status_code == status_code
    if status_code == 422:
        assert isinstance(response.json()["detail"], list)


# ---------------------------------------------------------------- rate limits


async def test_merchant_registration_is_limited_to_5_per_hour_per_ip(client: httpx.AsyncClient, redis: Any) -> None:
    keys = [PrivateKey(f"{i:02x}" * 32).to_address("testnet").to_string() for i in range(1, 8)]
    for address in keys[:5]:
        assert (
            await client.post("/v1/merchants", json={"kaspa_address": address, "display_name": "x"})
        ).status_code == 201
    blocked = await client.post("/v1/merchants", json={"kaspa_address": keys[5], "display_name": "x"})
    assert blocked.status_code == 429
    assert 0 < int(blocked.headers["retry-after"]) <= 3600
    assert 0 < await redis.ttl("kasgate:rl:merchants:ip:127.0.0.1") <= 3600
    async with make_asgi_client(client, "10.9.9.9") as other_ip:
        ok = await other_ip.post("/v1/merchants", json={"kaspa_address": keys[6], "display_name": "x"})
        assert ok.status_code == 201


async def test_rate_limit_window_resets_when_key_expires(client: httpx.AsyncClient, redis: Any) -> None:
    for _ in range(10):
        assert (await client.post("/v1/auth/challenge", json={"kaspa_address": MERCHANT_ADDRESS})).status_code == 200
    blocked = await client.post("/v1/auth/challenge", json={"kaspa_address": MERCHANT_ADDRESS})
    assert blocked.status_code == 429
    assert 0 < int(blocked.headers["retry-after"]) <= 60
    await redis.delete("kasgate:rl:auth_challenge:ip:127.0.0.1", f"kasgate:rl:auth_challenge:addr:{MERCHANT_ADDRESS}")
    assert (await client.post("/v1/auth/challenge", json={"kaspa_address": MERCHANT_ADDRESS})).status_code == 200


async def test_challenge_is_limited_per_address_independent_of_ip(client: httpx.AsyncClient, redis: Any) -> None:
    for _ in range(10):
        assert (await client.post("/v1/auth/challenge", json={"kaspa_address": MERCHANT_ADDRESS})).status_code == 200
    assert await redis.get(f"kasgate:rl:auth_challenge:addr:{MERCHANT_ADDRESS}") == "10"
    async with make_asgi_client(client, "10.1.1.1") as other_ip:
        blocked = await other_ip.post("/v1/auth/challenge", json={"kaspa_address": MERCHANT_ADDRESS})
        assert blocked.status_code == 429
        assert "retry-after" in blocked.headers
        assert (await other_ip.post("/v1/auth/challenge", json={"kaspa_address": OTHER_ADDRESS})).status_code == 200


async def test_challenge_is_limited_per_ip_across_addresses(client: httpx.AsyncClient) -> None:
    addresses = [PrivateKey(f"{i:02x}" * 32).to_address("testnet").to_string() for i in range(10, 22)]
    codes = [(await client.post("/v1/auth/challenge", json={"kaspa_address": a})).status_code for a in addresses]
    assert codes == [200] * 10 + [429] * 2


async def test_verify_is_limited_to_10_per_minute_per_ip(client: httpx.AsyncClient) -> None:
    await register(client)
    challenge = await get_challenge(client)
    body = verify_body(challenge.challenge, signed="wrong")
    codes = [(await client.post("/v1/auth/verify", json=body)).status_code for _ in range(12)]
    assert codes[0] == 401
    assert codes[-1] == 429
    assert codes.count(429) == 2
