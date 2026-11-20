"""Wallet-proof registration and login, kgs_ sessions, key rotation, merchant uniqueness, and the auth rate limits."""

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from kaspa import PrivateKey
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Merchant
from app.schemas import AUTH_PURPOSE, REGISTER_PURPOSE, MerchantCreated, SessionIssued, build_auth_challenge
from app.security import (
    AUTH_CHALLENGE_TTL_SECONDS,
    auth_challenge_key,
    hash_api_key,
    new_auth_challenge,
    parse_auth_challenge,
    prune_session_index,
    session_digest,
    session_index_key,
    session_key,
)
from tests.conftest import (
    MERCHANT_ADDRESS,
    MERCHANT_KEY,
    OTHER_ADDRESS,
    OTHER_KEY,
    Seed,
    SettingsOverride,
    bearer,
    get_challenge,
    login,
    make_asgi_client,
    proof_fields,
    register,
)


def verify_body(
    challenge: str,
    key: PrivateKey = MERCHANT_KEY,
    address: str = MERCHANT_ADDRESS,
    *,
    x_only: bool = False,
    signed: str | None = None,
) -> dict[str, str]:
    return {"kaspa_address": address, **proof_fields(challenge, key, x_only=x_only, signed=signed)}


def register_body(
    challenge: str,
    key: PrivateKey = MERCHANT_KEY,
    address: str = MERCHANT_ADDRESS,
    *,
    email: str | None = None,
    signed: str | None = None,
) -> dict[str, str]:
    body = {"kaspa_address": address, "display_name": "Shop", **proof_fields(challenge, key, signed=signed)}
    if email:
        body["email"] = email
    return body


async def stored_challenge(redis: Any, purpose: str, address: str, issued_at: datetime) -> str:
    """Put a challenge that Redis really holds, but which was issued at `issued_at` (so it may be expired)."""
    data = new_auth_challenge("login" if purpose == "login" else "register", address, now=issued_at)
    await redis.set(auth_challenge_key(data.purpose, address, data.nonce), data.message, ex=300)
    return data.message


# ---------------------------------------------------------------- challenge


@pytest.mark.parametrize(("purpose", "text"), [("login", AUTH_PURPOSE), ("register", REGISTER_PURPOSE)])
async def test_challenge_format_storage_and_ttl(client: httpx.AsyncClient, redis: Any, purpose: str, text: str) -> None:
    before = datetime.now(UTC)
    challenge = await get_challenge(client, purpose=purpose)
    lines = challenge.challenge.split("\n")
    assert lines[0] == "KasGate"
    assert lines[1] == "Domain: gateway.test"
    assert lines[2] == f"Purpose: {text}"
    assert lines[3] == f"Address: {MERCHANT_ADDRESS}"
    assert lines[4].startswith("Nonce: ") and len(lines[4]) == len("Nonce: ") + 32
    assert lines[5] == f"Expires: {challenge.expires_at.isoformat()}"
    assert before + timedelta(seconds=AUTH_CHALLENGE_TTL_SECONDS - 5) <= challenge.expires_at
    assert challenge.expires_at <= before + timedelta(seconds=AUTH_CHALLENGE_TTL_SECONDS + 5)
    nonce = lines[4].removeprefix("Nonce: ")
    key = f"kasgate:auth:{purpose}:{MERCHANT_ADDRESS}:{nonce}"
    assert key == auth_challenge_key(purpose, MERCHANT_ADDRESS, nonce)  # type: ignore[arg-type]
    assert await redis.get(key) == challenge.challenge
    assert 0 < await redis.ttl(key) <= 300
    assert (await get_challenge(client, purpose=purpose)).challenge != challenge.challenge


async def test_challenge_purpose_defaults_to_login(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/auth/challenge", json={"kaspa_address": MERCHANT_ADDRESS})
    assert f"Purpose: {AUTH_PURPOSE}" in response.json()["challenge"]


async def test_challenge_domain_is_the_lowercased_netloc_of_public_base_url(
    client: httpx.AsyncClient, override_settings: SettingsOverride
) -> None:
    override_settings(public_base_url="http://Gateway.Test:8000/")
    challenge = await get_challenge(client)
    assert challenge.challenge.split("\n")[1] == "Domain: gateway.test:8000"


async def test_challenge_rejects_invalid_input(client: httpx.AsyncClient) -> None:
    assert (await client.post("/v1/auth/challenge", json={"kaspa_address": "nope"})).status_code == 422
    body = {"kaspa_address": MERCHANT_ADDRESS, "purpose": "admin"}
    assert (await client.post("/v1/auth/challenge", json=body)).status_code == 422


def test_challenge_parser_is_strict() -> None:
    issued = new_auth_challenge("register", MERCHANT_ADDRESS)
    parsed = parse_auth_challenge(issued.message)
    assert (parsed.purpose, parsed.address, parsed.nonce, parsed.expires_at) == (
        "register",
        MERCHANT_ADDRESS,
        issued.nonce,
        issued.expires_at,
    )
    message = issued.message
    other_domain = build_auth_challenge(
        REGISTER_PURPOSE, "evil.test", MERCHANT_ADDRESS, issued.nonce, issued.expires_at
    )
    for broken in (
        message + "\n",
        message.replace("KasGate", "Other"),
        message.replace("Domain: ", "Domain:  "),
        other_domain,
        message.replace(REGISTER_PURPOSE, "Do something else"),
        message.replace(issued.nonce, issued.nonce.upper()),
        message.replace("+00:00", ""),
        "x",
    ):
        with pytest.raises(ValueError):
            parse_auth_challenge(broken)


# ---------------------------------------------------------------- registration needs a wallet proof


async def test_register_returns_key_and_session_once(client: httpx.AsyncClient, redis: Any) -> None:
    before = datetime.now(UTC)
    challenge = await get_challenge(client, purpose="register")
    response = await client.post("/v1/merchants", json=register_body(challenge.challenge, email="Owner@Example.com"))
    assert response.status_code == 201, response.text
    created = MerchantCreated.model_validate(response.json())
    assert created.api_key.startswith("kg_")
    assert created.session_token.startswith("kgs_")
    assert before + timedelta(seconds=604_790) <= created.expires_at <= before + timedelta(seconds=604_810)
    assert created.merchant.kaspa_address == MERCHANT_ADDRESS
    assert created.merchant.email == "owner@example.com"
    assert "api_key" not in (await client.get("/v1/merchants/me", headers=bearer(created.api_key))).json()
    # the session is stored hashed, as kasgate:session:{sha256(token)} -> merchant id, for session_ttl_seconds
    key = f"kasgate:session:{hash_api_key(created.session_token)}"
    assert key == session_key(created.session_token)
    assert await redis.get(key) == str(created.merchant.id)
    assert 604_700 < await redis.ttl(key) <= 604_800
    assert await redis.keys("kasgate:session:*") == [key]
    # both credentials authenticate
    for token in (created.api_key, created.session_token):
        me = await client.get("/v1/merchants/me", headers=bearer(token))
        assert me.status_code == 200 and me.json()["id"] == str(created.merchant.id)


async def test_session_lifetime_comes_from_settings(
    client: httpx.AsyncClient, redis: Any, override_settings: SettingsOverride
) -> None:
    override_settings(session_ttl_seconds=3600)
    created = await register(client)
    assert 3500 < await redis.ttl(session_key(created.session_token)) <= 3600
    assert created.expires_at <= datetime.now(UTC) + timedelta(seconds=3601)


async def test_register_without_wallet_proof_is_422(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/merchants", json={"kaspa_address": MERCHANT_ADDRESS, "display_name": "Shop"})
    assert response.status_code == 422
    fields = {error["loc"][-1] for error in response.json()["detail"]}
    assert {"challenge", "public_key", "signature"} <= fields


async def _no_merchants(maker: async_sessionmaker[AsyncSession]) -> bool:
    async with maker() as session:
        return await session.scalar(select(func.count()).select_from(Merchant)) == 0


async def test_register_with_bad_signature_is_401_and_creates_nothing(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession], redis: Any
) -> None:
    challenge = await get_challenge(client, purpose="register")
    response = await client.post("/v1/merchants", json=register_body(challenge.challenge, signed="something else"))
    assert response.status_code == 401
    assert await _no_merchants(sessionmaker)
    assert await redis.keys("kasgate:session:*") == []


async def test_register_with_the_key_of_another_address_is_401(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    challenge = await get_challenge(client, MERCHANT_ADDRESS, "register")
    response = await client.post("/v1/merchants", json=register_body(challenge.challenge, key=OTHER_KEY))
    assert response.status_code == 401
    assert await _no_merchants(sessionmaker)


async def test_register_cannot_claim_an_address_with_someone_elses_challenge(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    victim = await get_challenge(client, MERCHANT_ADDRESS, "register")
    body = register_body(victim.challenge, key=OTHER_KEY, address=OTHER_ADDRESS)
    assert (await client.post("/v1/merchants", json=body)).status_code == 401
    assert await _no_merchants(sessionmaker)


async def test_login_challenge_cannot_register(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    challenge = await get_challenge(client, purpose="login")
    assert (await client.post("/v1/merchants", json=register_body(challenge.challenge))).status_code == 401
    assert await _no_merchants(sessionmaker)


async def test_register_challenge_cannot_log_in(client: httpx.AsyncClient) -> None:
    await register(client)
    challenge = await get_challenge(client, purpose="register")
    assert (await client.post("/v1/auth/verify", json=verify_body(challenge.challenge))).status_code == 401


async def test_register_challenge_is_single_use(client: httpx.AsyncClient) -> None:
    challenge = await get_challenge(client, purpose="register")
    body = register_body(challenge.challenge)
    assert (await client.post("/v1/merchants", json=body)).status_code == 201
    assert (await client.post("/v1/merchants", json=body)).status_code == 401


async def test_register_with_an_expired_challenge_is_401(client: httpx.AsyncClient, redis: Any) -> None:
    message = await stored_challenge(redis, "register", MERCHANT_ADDRESS, datetime.now(UTC) - timedelta(seconds=400))
    assert (await client.post("/v1/merchants", json=register_body(message))).status_code == 401


async def test_duplicate_address_and_email_are_409_after_the_proof(client: httpx.AsyncClient) -> None:
    await register(client, MERCHANT_KEY, email="a@example.com")
    again = await get_challenge(client, MERCHANT_ADDRESS, "register")
    same_address = await client.post("/v1/merchants", json=register_body(again.challenge))
    assert same_address.status_code == 409
    assert "address" in same_address.json()["detail"]
    other = await get_challenge(client, OTHER_ADDRESS, "register")
    same_email = await client.post(
        "/v1/merchants",
        json=register_body(other.challenge, key=OTHER_KEY, address=OTHER_ADDRESS, email="A@Example.com"),
    )
    assert same_email.status_code == 409
    assert "email" in same_email.json()["detail"]


async def test_duplicate_check_does_not_leak_to_someone_without_the_key(client: httpx.AsyncClient) -> None:
    await register(client)
    challenge = await get_challenge(client, MERCHANT_ADDRESS, "register")
    forged = register_body(challenge.challenge, key=OTHER_KEY)
    response = await client.post("/v1/merchants", json=forged)
    assert response.status_code == 401  # not 409: only the owner learns the address is taken


async def test_concurrent_duplicate_registration_returns_one_201(client: httpx.AsyncClient) -> None:
    challenges = [await get_challenge(client, MERCHANT_ADDRESS, "register") for _ in range(4)]
    responses = await asyncio.gather(
        *(client.post("/v1/merchants", json=register_body(c.challenge)) for c in challenges)
    )
    assert sorted(r.status_code for r in responses) == [201, 409, 409, 409]


# ---------------------------------------------------------------- login issues a session


async def test_login_issues_a_session_and_does_not_rotate_the_api_key(client: httpx.AsyncClient, redis: Any) -> None:
    created = await register(client)
    challenge = await get_challenge(client)
    response = await client.post("/v1/auth/verify", json=verify_body(challenge.challenge))
    assert response.status_code == 200, response.text
    issued = SessionIssued.model_validate(response.json())
    assert "api_key" not in response.json()
    assert issued.merchant.id == created.merchant.id
    assert issued.session_token.startswith("kgs_")
    assert issued.session_token != created.session_token
    assert issued.expires_at > datetime.now(UTC) + timedelta(days=6)
    assert await redis.get(session_key(issued.session_token)) == str(created.merchant.id)
    assert 604_700 < await redis.ttl(session_key(issued.session_token)) <= 604_800

    assert (await client.get("/v1/merchants/me", headers=bearer(issued.session_token))).status_code == 200
    assert (await client.get("/v1/merchants/me", headers=bearer(created.api_key))).status_code == 200
    assert (await client.get("/v1/merchants/me", headers=bearer(created.session_token))).status_code == 200


@pytest.mark.parametrize("x_only", [False, True])
async def test_both_public_key_forms_work(client: httpx.AsyncClient, x_only: bool) -> None:
    await register(client)
    challenge = await get_challenge(client)
    body = verify_body(challenge.challenge, x_only=x_only)
    assert len(body["public_key"]) == (64 if x_only else 66)
    assert (await client.post("/v1/auth/verify", json=body)).status_code == 200


async def test_challenge_is_single_use(client: httpx.AsyncClient) -> None:
    await register(client)
    challenge = await get_challenge(client)
    body = verify_body(challenge.challenge)
    assert (await client.post("/v1/auth/verify", json=body)).status_code == 200
    assert (await client.post("/v1/auth/verify", json=body)).status_code == 401


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


async def test_valid_signature_from_wrong_address_key_is_401(client: httpx.AsyncClient) -> None:
    await register(client)
    challenge = await get_challenge(client)
    assert (
        await client.post("/v1/auth/verify", json=verify_body(challenge.challenge, key=OTHER_KEY))
    ).status_code == 401


async def test_challenge_with_a_foreign_domain_is_401(client: httpx.AsyncClient, redis: Any) -> None:
    await register(client)
    expires = datetime.now(UTC) + timedelta(minutes=5)
    message = build_auth_challenge(AUTH_PURPOSE, "evil.test", MERCHANT_ADDRESS, "ab" * 16, expires)
    await redis.set(auth_challenge_key("login", MERCHANT_ADDRESS, "ab" * 16), message, ex=300)
    assert (await client.post("/v1/auth/verify", json=verify_body(message))).status_code == 401


async def test_unknown_address_gets_the_same_challenge_shape_and_a_generic_401(
    client: httpx.AsyncClient, redis: Any
) -> None:
    await register(client)
    known = await client.post("/v1/auth/challenge", json={"kaspa_address": MERCHANT_ADDRESS})
    unknown = await client.post("/v1/auth/challenge", json={"kaspa_address": OTHER_ADDRESS})
    assert known.status_code == unknown.status_code == 200
    assert known.json().keys() == unknown.json().keys()
    assert known.headers.keys() == unknown.headers.keys()
    nonce = unknown.json()["challenge"].split("Nonce: ")[1][:32]
    assert await redis.get(f"kasgate:auth:login:{OTHER_ADDRESS}:{nonce}")

    body = verify_body(unknown.json()["challenge"], key=OTHER_KEY, address=OTHER_ADDRESS)
    response = await client.post("/v1/auth/verify", json=body)
    assert response.status_code == 401
    assert response.json() == await _generic_failure(client)


async def _generic_failure(client: httpx.AsyncClient) -> dict[str, Any]:
    """The body every failed login must produce: a valid signature over a challenge that was never issued."""
    forged = new_auth_challenge("login", MERCHANT_ADDRESS)
    response = await client.post("/v1/auth/verify", json=verify_body(forged.message))
    assert response.status_code == 401
    result: dict[str, Any] = response.json()
    return result


async def _post_verify(client: httpx.AsyncClient, body: dict[str, str]) -> dict[str, Any]:
    response = await client.post("/v1/auth/verify", json=body)
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
    expired = await stored_challenge(redis, "login", MERCHANT_ADDRESS, datetime.now(UTC) - timedelta(seconds=400))
    bodies.append(await _post_verify(client, verify_body(expired)))
    other_address = await get_challenge(client, OTHER_ADDRESS)
    bodies.append(await _post_verify(client, verify_body(other_address.challenge)))
    register_purpose = await get_challenge(client, purpose="register")
    bodies.append(await _post_verify(client, verify_body(register_purpose.challenge)))
    assert bodies == [expected] * len(bodies)


async def test_any_verify_attempt_consumes_the_challenge(client: httpx.AsyncClient, redis: Any) -> None:
    await register(client)
    challenge = await get_challenge(client)
    assert (
        await client.post("/v1/auth/verify", json=verify_body(challenge.challenge, signed="nope"))
    ).status_code == 401
    assert await redis.keys("kasgate:auth:*") == []
    assert (await client.post("/v1/auth/verify", json=verify_body(challenge.challenge))).status_code == 401


async def test_unknown_address_attempt_also_consumes_the_challenge(client: httpx.AsyncClient, redis: Any) -> None:
    challenge = await get_challenge(client, OTHER_ADDRESS)
    body = verify_body(challenge.challenge, key=OTHER_KEY, address=OTHER_ADDRESS)
    assert (await client.post("/v1/auth/verify", json=body)).status_code == 401
    assert await redis.keys("kasgate:auth:*") == []


@pytest.mark.parametrize("variant", ["base64_64", "base64_65", "upper_hex"])
async def test_signature_encodings_are_normalised(client: httpx.AsyncClient, variant: str) -> None:
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
    assert (await client.post("/v1/auth/verify", json=body)).status_code == 200


async def test_expired_challenge_is_401(client: httpx.AsyncClient, redis: Any) -> None:
    await register(client)
    message = await stored_challenge(redis, "login", MERCHANT_ADDRESS, datetime.now(UTC) - timedelta(seconds=400))
    assert (await client.post("/v1/auth/verify", json=verify_body(message))).status_code == 401


async def test_challenge_missing_from_redis_is_401(client: httpx.AsyncClient, redis: Any) -> None:
    await register(client)
    challenge = await get_challenge(client)
    await redis.delete(*await redis.keys("kasgate:auth:*"))
    assert (await client.post("/v1/auth/verify", json=verify_body(challenge.challenge))).status_code == 401


async def test_self_made_challenge_is_401(client: httpx.AsyncClient) -> None:
    await register(client)
    forged = new_auth_challenge("login", MERCHANT_ADDRESS)
    assert (await client.post("/v1/auth/verify", json=verify_body(forged.message))).status_code == 401


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


async def test_disabled_merchant_cannot_log_in(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    created = await register(client)
    async with sessionmaker() as session:
        await session.execute(update(Merchant).where(Merchant.id == created.merchant.id).values(is_active=False))
        await session.commit()
    challenge = await get_challenge(client)
    assert (await client.post("/v1/auth/verify", json=verify_body(challenge.challenge))).status_code == 403


# ---------------------------------------------------------------- the Bearer dependency: kg_ keys and kgs_ sessions


async def test_session_token_authenticates_every_merchant_route(client: httpx.AsyncClient) -> None:
    created = await register(client)
    headers = bearer(created.session_token)
    assert (await client.get("/v1/endpoints", headers=headers)).status_code == 200
    assert (await client.get("/v1/settlements", headers=headers)).status_code == 200
    assert (await client.get("/v1/analytics", headers=headers)).status_code == 200


@pytest.mark.parametrize(
    "token", ["kgs_" + "a" * 43, "kgs_short", "kgs_", "kgx_" + "a" * 43, "Bearer", "kg_" + "a" * 43]
)
async def test_unknown_tokens_are_401(client: httpx.AsyncClient, token: str) -> None:
    response = await client.get("/v1/merchants/me", headers=bearer(token))
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_a_session_is_not_an_api_key_and_vice_versa(client: httpx.AsyncClient) -> None:
    created = await register(client)
    api_key_as_session = created.api_key.replace("kg_", "kgs_", 1)
    session_as_api_key = created.session_token.replace("kgs_", "kg_", 1)
    assert (await client.get("/v1/merchants/me", headers=bearer(api_key_as_session))).status_code == 401
    assert (await client.get("/v1/merchants/me", headers=bearer(session_as_api_key))).status_code == 401


async def test_expired_session_is_401(client: httpx.AsyncClient, redis: Any) -> None:
    created = await register(client)
    await redis.delete(session_key(created.session_token))
    assert (await client.get("/v1/merchants/me", headers=bearer(created.session_token))).status_code == 401
    assert (await client.get("/v1/merchants/me", headers=bearer(created.api_key))).status_code == 200


async def test_session_of_a_disabled_merchant_is_403(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    created = await register(client)
    async with sessionmaker() as session:
        await session.execute(update(Merchant).where(Merchant.id == created.merchant.id).values(is_active=False))
        await session.commit()
    assert (await client.get("/v1/merchants/me", headers=bearer(created.session_token))).status_code == 403


async def test_session_for_a_deleted_merchant_is_401(
    client: httpx.AsyncClient, redis: Any, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    created = await register(client)
    async with sessionmaker() as session:
        merchant = await session.get(Merchant, created.merchant.id)
        assert merchant is not None
        await session.delete(merchant)
        await session.commit()
    assert (await client.get("/v1/merchants/me", headers=bearer(created.session_token))).status_code == 401


async def test_corrupt_session_value_is_401(client: httpx.AsyncClient, redis: Any) -> None:
    created = await register(client)
    await redis.set(session_key(created.session_token), "not-a-uuid", ex=60)
    assert (await client.get("/v1/merchants/me", headers=bearer(created.session_token))).status_code == 401


async def test_session_check_fails_closed_when_redis_is_down(
    client: httpx.AsyncClient, redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await register(client)

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise RedisConnectionError("down")

    monkeypatch.setattr(redis, "get", broken)
    response = await client.get("/v1/merchants/me", headers=bearer(created.session_token))
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert (await client.get("/v1/merchants/me", headers=bearer(created.api_key))).status_code == 200


# ---------------------------------------------------------------- rotate-key


async def test_rotate_key_returns_new_key_and_a_session_and_kills_the_old_key(
    client: httpx.AsyncClient, seeded: Seed, redis: Any
) -> None:
    response = await client.post("/v1/merchants/me/rotate-key", headers=bearer(seeded.api_key))
    assert response.status_code == 200, response.text
    rotated = MerchantCreated.model_validate(response.json())
    assert rotated.merchant.id == seeded.merchant_id
    assert rotated.api_key != seeded.api_key
    assert rotated.session_token.startswith("kgs_")
    assert rotated.expires_at > datetime.now(UTC) + timedelta(days=6)
    assert await redis.get(session_key(rotated.session_token)) == str(seeded.merchant_id)
    assert 604_700 < await redis.ttl(session_key(rotated.session_token)) <= 604_800
    assert (await client.get("/v1/merchants/me", headers=bearer(seeded.api_key))).status_code == 401
    assert (await client.get("/v1/merchants/me", headers=bearer(rotated.api_key))).status_code == 200
    assert (await client.get("/v1/merchants/me", headers=bearer(rotated.session_token))).status_code == 200
    assert (await client.post("/v1/merchants/me/rotate-key", headers=bearer(seeded.api_key))).status_code == 401


async def test_rotate_key_revokes_every_old_session_and_the_new_one_works(
    client: httpx.AsyncClient, redis: Any
) -> None:
    created = await register(client)
    issued = await login(client)
    second_login = await login(client)
    old_sessions = [created.session_token, issued.session_token, second_login.session_token]
    index = session_index_key(created.merchant.id)
    assert await redis.scard(index) == 3

    response = await client.post("/v1/merchants/me/rotate-key", headers=bearer(issued.session_token))
    assert response.status_code == 200
    rotated = MerchantCreated.model_validate(response.json())

    for token in old_sessions:
        assert (await client.get("/v1/merchants/me", headers=bearer(token))).status_code == 401
        assert await redis.exists(session_key(token)) == 0
    assert (await client.get("/v1/merchants/me", headers=bearer(created.api_key))).status_code == 401
    assert (await client.get("/v1/merchants/me", headers=bearer(rotated.api_key))).status_code == 200
    assert (await client.get("/v1/merchants/me", headers=bearer(rotated.session_token))).status_code == 200
    # only the new session is left, and it is the only member of the rebuilt index
    assert await redis.smembers(index) == {session_digest(rotated.session_token)}
    assert await redis.keys("kasgate:session:*") == [session_key(rotated.session_token)]


async def test_rotate_key_by_api_key_also_revokes_sessions_and_cleans_the_index(
    client: httpx.AsyncClient, redis: Any
) -> None:
    created = await register(client)
    index = session_index_key(created.merchant.id)
    rotated = MerchantCreated.model_validate(
        (await client.post("/v1/merchants/me/rotate-key", headers=bearer(created.api_key))).json()
    )
    assert (await client.get("/v1/merchants/me", headers=bearer(created.session_token))).status_code == 401
    assert await redis.smembers(index) == {session_digest(rotated.session_token)}
    # rotating twice leaves exactly one session behind each time
    again = MerchantCreated.model_validate(
        (await client.post("/v1/merchants/me/rotate-key", headers=bearer(rotated.api_key))).json()
    )
    assert (await client.get("/v1/merchants/me", headers=bearer(rotated.session_token))).status_code == 401
    assert await redis.smembers(index) == {session_digest(again.session_token)}


async def test_rotate_key_leaves_other_merchants_sessions_alone(client: httpx.AsyncClient) -> None:
    mine = await register(client, MERCHANT_KEY)
    theirs = await register(client, OTHER_KEY)
    await client.post("/v1/merchants/me/rotate-key", headers=bearer(mine.api_key))
    assert (await client.get("/v1/merchants/me", headers=bearer(theirs.session_token))).status_code == 200
    assert (await client.get("/v1/merchants/me", headers=bearer(theirs.api_key))).status_code == 200


async def test_rotate_key_revokes_sessions_before_the_new_key_exists(
    client: httpx.AsyncClient, redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await register(client)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RedisConnectionError("down")

    monkeypatch.setattr(redis, "pipeline", broken)
    response = await client.post("/v1/merchants/me/rotate-key", headers=bearer(created.api_key))
    assert response.status_code == 503
    monkeypatch.undo()
    assert (await client.get("/v1/merchants/me", headers=bearer(created.api_key))).status_code == 200  # not rotated


async def test_rotate_key_stores_only_the_hash(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    rotated = MerchantCreated.model_validate(
        (await client.post("/v1/merchants/me/rotate-key", headers=bearer(seeded.api_key))).json()
    )
    async with sessionmaker() as session:
        stored = (await session.execute(select(Merchant).where(Merchant.id == seeded.merchant_id))).scalar_one()
        assert stored.api_key_hash == hash_api_key(rotated.api_key)


async def test_rotate_key_requires_auth(client: httpx.AsyncClient) -> None:
    assert (await client.post("/v1/merchants/me/rotate-key")).status_code == 401


# ---------------------------------------------------------------- session index and logout


async def test_every_issued_session_is_indexed_with_a_matching_expiry(client: httpx.AsyncClient, redis: Any) -> None:
    created = await register(client)
    issued = await login(client)
    index = session_index_key(created.merchant.id)
    assert index == f"kasgate:sessions:{created.merchant.id}"
    assert await redis.smembers(index) == {
        session_digest(created.session_token),
        session_digest(issued.session_token),
    }
    newest_ttl = await redis.ttl(session_key(issued.session_token))
    assert await redis.ttl(index) >= newest_ttl - 1  # never shorter than the newest session


async def test_index_expiry_follows_session_ttl_setting(
    client: httpx.AsyncClient, redis: Any, override_settings: SettingsOverride
) -> None:
    override_settings(session_ttl_seconds=3600)
    created = await register(client)
    assert 3500 < await redis.ttl(session_index_key(created.merchant.id)) <= 3600


async def test_stale_members_are_pruned_when_a_new_session_is_issued_and_on_read(
    client: httpx.AsyncClient, redis: Any
) -> None:
    created = await register(client)
    index = session_index_key(created.merchant.id)
    await redis.delete(session_key(created.session_token))  # that session expired
    await redis.sadd(index, "f" * 64)  # a digest that never existed
    assert len(await redis.smembers(index)) == 2
    assert await prune_session_index(redis, created.merchant.id) == []
    assert await redis.smembers(index) == set()
    await redis.sadd(index, "e" * 64)
    issued = await login(client)  # issuing prunes too
    assert await redis.smembers(index) == {session_digest(issued.session_token)}


async def test_logout_invalidates_only_that_session(client: httpx.AsyncClient, redis: Any) -> None:
    created = await register(client)
    other = await login(client)
    response = await client.delete("/v1/auth/session", headers=bearer(created.session_token))
    assert response.status_code == 204
    assert response.content == b""
    assert await redis.exists(session_key(created.session_token)) == 0
    assert await redis.smembers(session_index_key(created.merchant.id)) == {session_digest(other.session_token)}
    # the logged-out token is dead; the other session and the API key are untouched
    assert (await client.get("/v1/merchants/me", headers=bearer(created.session_token))).status_code == 401
    assert (await client.get("/v1/merchants/me", headers=bearer(other.session_token))).status_code == 200
    assert (await client.get("/v1/merchants/me", headers=bearer(created.api_key))).status_code == 200


async def test_logging_out_twice_is_401_the_second_time(client: httpx.AsyncClient) -> None:
    created = await register(client)
    assert (await client.delete("/v1/auth/session", headers=bearer(created.session_token))).status_code == 204
    again = await client.delete("/v1/auth/session", headers=bearer(created.session_token))
    assert again.status_code == 401
    assert again.headers["www-authenticate"] == "Bearer"


async def test_last_logout_leaves_no_index_behind(client: httpx.AsyncClient, redis: Any) -> None:
    created = await register(client)
    await client.delete("/v1/auth/session", headers=bearer(created.session_token))
    assert await redis.keys("kasgate:session:*") == []
    assert await redis.keys("kasgate:sessions:*") == []  # an emptied set is removed by Redis


async def test_logout_with_an_api_key_is_400_and_changes_nothing(client: httpx.AsyncClient, redis: Any) -> None:
    created = await register(client)
    response = await client.delete("/v1/auth/session", headers=bearer(created.api_key))
    assert response.status_code == 400
    assert "rotate-key" in response.json()["detail"]
    assert (await client.get("/v1/merchants/me", headers=bearer(created.api_key))).status_code == 200
    assert (await client.get("/v1/merchants/me", headers=bearer(created.session_token))).status_code == 200
    assert await redis.scard(session_index_key(created.merchant.id)) == 1


async def test_logout_requires_credentials(client: httpx.AsyncClient) -> None:
    assert (await client.delete("/v1/auth/session")).status_code == 401
    assert (await client.delete("/v1/auth/session", headers=bearer("kgs_" + "a" * 43))).status_code == 401


async def test_logout_fails_closed_when_redis_is_down(
    client: httpx.AsyncClient, redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await register(client)
    real_pipeline = redis.pipeline

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RedisConnectionError("down")

    monkeypatch.setattr(redis, "pipeline", broken)
    response = await client.delete("/v1/auth/session", headers=bearer(created.session_token))
    assert response.status_code == 503
    monkeypatch.setattr(redis, "pipeline", real_pipeline)
    assert (await client.get("/v1/merchants/me", headers=bearer(created.session_token))).status_code == 200


# ---------------------------------------------------------------- bounds


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
    keys = [PrivateKey(f"{i:02x}" * 32) for i in range(10, 17)]
    codes: list[int] = []
    for key in keys:
        address = key.to_address("testnet").to_string()
        challenge = await get_challenge(client, address, "register")
        response = await client.post("/v1/merchants", json=register_body(challenge.challenge, key, address))
        codes.append(response.status_code)
    assert codes == [201] * 5 + [429] * 2
    assert 0 < int(await redis.ttl("kasgate:rl:merchants:ip:127.0.0.1")) <= 3600


async def test_challenge_is_limited_per_ip_only(client: httpx.AsyncClient, redis: Any) -> None:
    for _ in range(10):
        assert (await client.post("/v1/auth/challenge", json={"kaspa_address": MERCHANT_ADDRESS})).status_code == 200
    blocked = await client.post("/v1/auth/challenge", json={"kaspa_address": MERCHANT_ADDRESS})
    assert blocked.status_code == 429
    assert 0 < int(blocked.headers["retry-after"]) <= 60
    assert await redis.keys("kasgate:rl:auth_challenge:addr:*") == []
    # a different IP can still ask for the same address: a per-address limit would let anyone lock a victim out
    async with make_asgi_client(client, "10.1.1.1") as other_ip:
        for _ in range(10):
            assert (
                await other_ip.post("/v1/auth/challenge", json={"kaspa_address": MERCHANT_ADDRESS})
            ).status_code == 200
    await redis.delete("kasgate:rl:auth_challenge:ip:127.0.0.1")
    assert (await client.post("/v1/auth/challenge", json={"kaspa_address": MERCHANT_ADDRESS})).status_code == 200


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
