"""Live wallet registration, login and key-rotation tests (CONTRACT v2 / v2.2).

  POST /v1/auth/challenge {kaspa_address, purpose: login|register}  -> always 200
  POST /v1/merchants        {.., challenge, public_key, signature}  -> 201 MerchantCreated (+ kgs_ session), proof first
  POST /v1/auth/verify      {kaspa_address, challenge, ..}          -> 200 SessionIssued, NEVER rotates the api key
  POST /v1/merchants/me/rotate-key [Bearer kg_ or kgs_]             -> the only call that rotates the api key; it
                                                                       revokes ALL sessions, the caller's included (v2.4)
  DELETE /v1/auth/session [Bearer kgs_]                              -> 204; 400 for a kg_ key; 401 when repeated/unknown

Every test registers its own throw-away wallet, so the session-wide `auth` fixture is never touched. The suite expects
RATE_LIMIT_ENABLED=false on the gateway (see README); `post` waits out one 429 as a safety net.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from app.schemas import AUTH_PURPOSE, REGISTER_PURPOSE

from conftest import GATEWAY_URL
from wallet import Wallet, bearer, get_challenge, me, post, proof, register, verify

pytestmark = pytest.mark.live

LOGIN_FAILURE = "login failed: invalid address, challenge or signature"
REGISTRATION_FAILURE = "registration failed: invalid challenge or signature"
EXPECTED_DOMAIN = os.environ.get("KASGATE_TEST_PUBLIC_DOMAIN", urlsplit(GATEWAY_URL).netloc).lower()


def registered(gateway_url: str, **kwargs: Any) -> dict[str, Any]:
    wallet = Wallet()
    response = register(gateway_url, wallet, **kwargs)
    if response.status_code == 429:
        pytest.skip("POST /v1/merchants is rate limited: run the gateway with RATE_LIMIT_ENABLED=false for this suite")
    assert response.status_code == 201, response.text
    body = response.json()
    return {
        "wallet": wallet,
        "api_key": body["api_key"],
        "session_token": body["session_token"],
        "id": body["merchant"]["id"],
        "body": body,
    }


@pytest.fixture
def account(gateway_url: str, network: str) -> dict[str, Any]:
    return registered(gateway_url)


@pytest.fixture(scope="module")
def shared_account(gateway_url: str, network: str) -> dict[str, Any]:
    return registered(gateway_url)


# ---------------------------------------------------------------- challenge shape


@pytest.mark.parametrize(("purpose", "text"), [(None, AUTH_PURPOSE), ("login", AUTH_PURPOSE), ("register", REGISTER_PURPOSE)])
def test_challenge_response_matches_the_contract(gateway_url: str, network: str, purpose: str | None, text: str) -> None:
    wallet = Wallet()
    body = get_challenge(gateway_url, wallet.address, purpose)
    assert set(body) == {"kaspa_address", "challenge", "expires_at"}
    assert body["kaspa_address"] == wallet.address
    lines = body["challenge"].split("\n")
    assert len(lines) == 6 and lines[0] == "KasGate"
    assert lines[1] == f"Domain: {EXPECTED_DOMAIN}"
    assert lines[2] == f"Purpose: {text}"
    assert lines[3] == f"Address: {wallet.address}"
    assert lines[4].startswith("Nonce: ") and len(lines[4].removeprefix("Nonce: ")) == 32
    expires = datetime.fromisoformat(lines[5].removeprefix("Expires: "))
    assert datetime.fromisoformat(body["expires_at"]) == expires
    assert datetime.now(UTC) < expires <= datetime.now(UTC) + timedelta(minutes=10)


def test_each_challenge_has_a_fresh_nonce(gateway_url: str, network: str) -> None:
    address = Wallet().address
    first, second = get_challenge(gateway_url, address), get_challenge(gateway_url, address)
    assert first["challenge"] != second["challenge"]


def test_unknown_purpose_is_422(gateway_url: str, network: str) -> None:
    response = post(f"{gateway_url}/v1/auth/challenge", json={"kaspa_address": Wallet().address, "purpose": "admin"})
    assert response.status_code == 422


def test_mainnet_prefixed_address_cannot_request_a_challenge(gateway_url: str, network: str) -> None:
    response = post(
        f"{gateway_url}/v1/auth/challenge",
        json={"kaspa_address": "kaspa:qr0lr4ml9fn3chekrqmjdkergxl93l4wrk3dankcgvjq776s9wn9jkdskewva"},
    )
    assert response.status_code == 422


def test_address_with_a_bad_checksum_cannot_request_a_challenge(gateway_url: str, network: str) -> None:
    address = Wallet().address
    broken = address[:-1] + ("q" if address[-1] != "q" else "p")
    response = post(f"{gateway_url}/v1/auth/challenge", json={"kaspa_address": broken})
    assert response.status_code == 422


# ---------------------------------------------------------------- registration proves address ownership


def test_registration_returns_api_key_and_session_and_both_authenticate(gateway_url: str, account: dict[str, Any]) -> None:
    body = account["body"]
    assert set(body) >= {"merchant", "api_key", "session_token", "expires_at"}
    assert body["api_key"].startswith("kg_") and body["session_token"].startswith("kgs_")
    assert body["merchant"]["kaspa_address"] == account["wallet"].address
    assert datetime.fromisoformat(body["expires_at"]) > datetime.now(UTC)
    for token in (account["api_key"], account["session_token"]):
        response = me(gateway_url, token)
        assert response.status_code == 200 and response.json()["id"] == account["id"]


def test_registration_without_a_wallet_proof_is_422(gateway_url: str, network: str) -> None:
    wallet = Wallet()
    legacy = post(f"{gateway_url}/v1/merchants", json={"kaspa_address": wallet.address, "display_name": "no proof"})
    assert legacy.status_code == 422
    partial = post(
        f"{gateway_url}/v1/merchants",
        json={"kaspa_address": wallet.address, "display_name": "x", "challenge": "c" * 40},
    )
    assert partial.status_code == 422


def test_registration_with_a_login_challenge_is_401(gateway_url: str, network: str) -> None:
    wallet = Wallet()
    login_challenge = get_challenge(gateway_url, wallet.address, "login")["challenge"]
    response = register(gateway_url, wallet, challenge=login_challenge)
    assert response.status_code == 401 and response.json()["detail"] == REGISTRATION_FAILURE


def test_registration_cannot_claim_an_address_the_signer_does_not_own(gateway_url: str, network: str) -> None:
    victim, attacker = Wallet(), Wallet()
    challenge = get_challenge(gateway_url, victim.address, "register")["challenge"]  # anyone may request this
    forged = [
        register(gateway_url, attacker, challenge=challenge, kaspa_address=victim.address),  # attacker key + signature
        register(gateway_url, victim, challenge=challenge, signature=attacker.sign(challenge)),  # victim key, forged sig
        register(
            gateway_url, victim, challenge=challenge, public_key=attacker.public_key, signature=attacker.sign(challenge)
        ),
    ]
    for response in forged:
        assert response.status_code == 401, response.text
        assert response.json()["detail"] == REGISTRATION_FAILURE
    # the victim can still register afterwards: failed attempts squatted nothing
    assert register(gateway_url, victim).status_code == 201


def test_registration_challenge_is_single_use(gateway_url: str, network: str) -> None:
    wallet = Wallet()
    challenge = get_challenge(gateway_url, wallet.address, "register")["challenge"]
    signature = wallet.sign(challenge)
    first = register(gateway_url, wallet, challenge=challenge, signature=signature)
    second = register(gateway_url, wallet, challenge=challenge, signature=signature)
    assert first.status_code == 201, first.text
    assert second.status_code == 401


def test_duplicate_address_is_409_only_for_the_proven_owner(gateway_url: str, account: dict[str, Any]) -> None:
    wallet: Wallet = account["wallet"]
    owner = register(gateway_url, wallet)
    assert owner.status_code == 409
    stranger = Wallet()
    challenge = get_challenge(gateway_url, wallet.address, "register")["challenge"]
    probe = register(gateway_url, stranger, challenge=challenge, kaspa_address=wallet.address)
    assert probe.status_code == 401, "an unproven caller must not learn that the address is already registered"


def test_duplicate_email_is_409(gateway_url: str, network: str) -> None:
    email = f"qa-{secrets.token_hex(4)}@example.com"
    assert register(gateway_url, Wallet(), email=email).status_code == 201
    assert register(gateway_url, Wallet(), email=email).status_code == 409


def test_mainnet_address_cannot_register_on_testnet(gateway_url: str, network: str) -> None:
    wallet = Wallet()
    challenge = get_challenge(gateway_url, wallet.address, "register")["challenge"]
    response = register(
        gateway_url,
        wallet,
        challenge=challenge,
        kaspa_address="kaspa:qr0lr4ml9fn3chekrqmjdkergxl93l4wrk3dankcgvjq776s9wn9jkdskewva",
    )
    assert response.status_code == 422


# ---------------------------------------------------------------- login: sessions, no key rotation


def test_login_issues_a_session_and_does_not_rotate_the_api_key(gateway_url: str, account: dict[str, Any]) -> None:
    wallet: Wallet = account["wallet"]
    challenge = get_challenge(gateway_url, wallet.address, "login")["challenge"]
    response = verify(gateway_url, wallet, challenge)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"merchant", "session_token", "expires_at"}, "login must not return an api_key"
    assert body["session_token"].startswith("kgs_") and body["session_token"] != account["session_token"]
    assert body["merchant"]["id"] == account["id"] and body["merchant"]["kaspa_address"] == wallet.address
    assert datetime.fromisoformat(body["expires_at"]) > datetime.now(UTC)

    assert me(gateway_url, account["api_key"]).status_code == 200, "login rotated the api key"
    assert me(gateway_url, account["session_token"]).status_code == 200, "login killed the registration session"
    assert me(gateway_url, body["session_token"]).status_code == 200


def test_session_token_authenticates_merchant_endpoints(gateway_url: str, account: dict[str, Any], min_price: int) -> None:
    created = httpx.post(
        f"{gateway_url}/v1/endpoints",
        headers=bearer(account["session_token"]),
        json={"name": "session-check", "upstream_url": "https://example.com/api", "price_sompi": min_price},
        timeout=10,
    )
    assert created.status_code == 201, created.text
    listing = httpx.get(f"{gateway_url}/v1/endpoints", headers=bearer(account["api_key"]), timeout=10)
    assert [e["id"] for e in listing.json()] == [created.json()["id"]]  # same merchant behind both credentials


def test_forged_or_foreign_session_tokens_are_401(gateway_url: str, account: dict[str, Any]) -> None:
    for token in ("kgs_" + secrets.token_urlsafe(32), "kgs_", "kgs_" + "A" * 4000, account["session_token"][:-2] + "xx"):
        assert me(gateway_url, token).status_code == 401, token[:12]


def test_compressed_and_x_only_public_keys_both_work(gateway_url: str, account: dict[str, Any]) -> None:
    wallet: Wallet = account["wallet"]
    challenge = get_challenge(gateway_url, wallet.address)["challenge"]
    assert verify(gateway_url, wallet, challenge, public_key=wallet.x_only_public_key).status_code == 200


def test_reused_challenge_is_401(gateway_url: str, account: dict[str, Any]) -> None:
    wallet: Wallet = account["wallet"]
    challenge = get_challenge(gateway_url, wallet.address)["challenge"]
    signature = wallet.sign(challenge)
    first = verify(gateway_url, wallet, challenge, signature=signature)
    second = verify(gateway_url, wallet, challenge, signature=signature)
    assert first.status_code == 200, first.text
    assert second.status_code == 401 and second.json()["detail"] == LOGIN_FAILURE
    assert me(gateway_url, first.json()["session_token"]).status_code == 200  # the replay did not kill the session


def test_concurrent_logins_with_one_challenge_succeed_exactly_once(gateway_url: str, account: dict[str, Any]) -> None:
    wallet: Wallet = account["wallet"]
    challenge = get_challenge(gateway_url, wallet.address)["challenge"]
    body = proof(wallet, challenge)

    async def race() -> list[httpx.Response]:
        async with httpx.AsyncClient(timeout=30) as client:
            return await asyncio.gather(*(client.post(f"{gateway_url}/v1/auth/verify", json=body) for _ in range(5)))

    statuses = Counter(r.status_code for r in asyncio.run(race()))
    assert statuses[200] == 1 and statuses[401] + statuses[429] == 4, statuses


def test_base64_signature_is_accepted(gateway_url: str, account: dict[str, Any]) -> None:
    wallet: Wallet = account["wallet"]
    challenge = get_challenge(gateway_url, wallet.address)["challenge"]
    raw = bytes.fromhex(wallet.sign(challenge))
    assert verify(gateway_url, wallet, challenge, signature=base64.b64encode(raw).decode()).status_code == 200


def test_base64_signature_with_a_header_byte_is_accepted(gateway_url: str, account: dict[str, Any]) -> None:
    wallet: Wallet = account["wallet"]
    challenge = get_challenge(gateway_url, wallet.address)["challenge"]
    raw = b"\x1f" + bytes.fromhex(wallet.sign(challenge))
    assert verify(gateway_url, wallet, challenge, signature=base64.b64encode(raw).decode()).status_code == 200


# ---------------------------------------------------------------- purpose binding


def test_register_challenge_cannot_log_in(gateway_url: str, account: dict[str, Any]) -> None:
    wallet: Wallet = account["wallet"]
    challenge = get_challenge(gateway_url, wallet.address, "register")["challenge"]
    response = verify(gateway_url, wallet, challenge)
    assert response.status_code == 401 and response.json()["detail"] == LOGIN_FAILURE


def test_login_challenge_cannot_be_replayed_as_a_registration(gateway_url: str, network: str) -> None:
    """A signed login proof of a not-yet-registered wallet must not double as its registration."""
    wallet = Wallet()
    login_challenge = get_challenge(gateway_url, wallet.address, "login")["challenge"]
    assert register(gateway_url, wallet, challenge=login_challenge).status_code == 401


# ---------------------------------------------------------------- failures are generic 401s


def test_unknown_address_gets_a_challenge_but_cannot_log_in(gateway_url: str, shared_account: dict[str, Any]) -> None:
    stranger = Wallet()
    body = get_challenge(gateway_url, stranger.address)  # no enumeration: 200 for an address nobody registered
    assert body["kaspa_address"] == stranger.address
    unknown = verify(gateway_url, stranger, body["challenge"])
    assert unknown.status_code == 401
    real: Wallet = shared_account["wallet"]
    real_challenge = get_challenge(gateway_url, real.address)["challenge"]
    bad_signature = verify(gateway_url, real, real_challenge, signature=stranger.sign(real_challenge))
    assert bad_signature.status_code == 401
    assert unknown.json() == bad_signature.json() and unknown.json()["detail"] == LOGIN_FAILURE


def test_signature_from_a_different_key_is_401(gateway_url: str, shared_account: dict[str, Any]) -> None:
    wallet: Wallet = shared_account["wallet"]
    attacker = Wallet()
    challenge = get_challenge(gateway_url, wallet.address)["challenge"]
    assert verify(gateway_url, wallet, challenge, signature=attacker.sign(challenge)).status_code == 401
    assert verify(
        gateway_url, wallet, challenge, signature=attacker.sign(challenge), public_key=attacker.public_key
    ).status_code == 401
    assert me(gateway_url, shared_account["api_key"]).status_code == 200


def test_signature_over_a_different_message_is_401(gateway_url: str, shared_account: dict[str, Any]) -> None:
    wallet: Wallet = shared_account["wallet"]
    challenge = get_challenge(gateway_url, wallet.address)["challenge"]
    other = get_challenge(gateway_url, wallet.address)["challenge"]
    assert verify(gateway_url, wallet, challenge, signature=wallet.sign(other)).status_code == 401


def test_challenge_issued_for_one_address_cannot_log_in_another(gateway_url: str, shared_account: dict[str, Any]) -> None:
    victim: Wallet = shared_account["wallet"]
    attacker = Wallet()
    challenge = get_challenge(gateway_url, attacker.address)["challenge"]
    response = verify(gateway_url, attacker, challenge, kaspa_address=victim.address)
    assert response.status_code == 401


@pytest.mark.parametrize("mutation", ["nonce", "whitespace", "expiry", "domain", "purpose", "garbage", "forged"])
def test_tampered_or_forged_challenges_are_401(gateway_url: str, shared_account: dict[str, Any], mutation: str) -> None:
    wallet: Wallet = shared_account["wallet"]
    challenge = get_challenge(gateway_url, wallet.address)["challenge"]
    forged = {
        "nonce": challenge.replace("Nonce: ", "Nonce: 0"),
        "whitespace": challenge + " ",
        "expiry": challenge.rsplit("Expires: ", 1)[0] + "Expires: 2999-01-01T00:00:00+00:00",
        "domain": challenge.replace(f"Domain: {EXPECTED_DOMAIN}", "Domain: phish.example", 1),  # GAP-12: origin binding
        "purpose": challenge.replace(AUTH_PURPOSE, REGISTER_PURPOSE, 1),
        "garbage": "not a login challenge, but long enough to pass length checks",
        "forged": (
            f"KasGate\nDomain: {EXPECTED_DOMAIN}\nPurpose: {AUTH_PURPOSE}\nAddress: {wallet.address}\n"
            f"Nonce: {secrets.token_hex(16)}\nExpires: 2999-01-01T00:00:00+00:00"
        ),
    }[mutation]
    response = verify(gateway_url, wallet, forged)
    assert response.status_code == 401, response.text
    assert response.json()["detail"] == LOGIN_FAILURE


def test_failed_verification_burns_the_challenge(gateway_url: str, shared_account: dict[str, Any]) -> None:
    wallet: Wallet = shared_account["wallet"]
    attacker = Wallet()
    challenge = get_challenge(gateway_url, wallet.address)["challenge"]
    assert verify(gateway_url, wallet, challenge, signature=attacker.sign(challenge)).status_code == 401
    assert verify(gateway_url, wallet, challenge).status_code == 401  # even the owner's good signature is now late


# ---------------------------------------------------------------- rotate-key: the only call that rotates


def test_rotate_key_invalidates_the_old_key_immediately(gateway_url: str, account: dict[str, Any]) -> None:
    old_key = account["api_key"]
    response = httpx.post(f"{gateway_url}/v1/merchants/me/rotate-key", headers=bearer(old_key), timeout=10)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["api_key"] != old_key and body["merchant"]["id"] == account["id"]
    assert body["session_token"].startswith("kgs_") and "expires_at" in body  # CONTRACT v2.2
    assert me(gateway_url, old_key).status_code == 401
    assert me(gateway_url, body["api_key"]).status_code == 200
    again = httpx.post(f"{gateway_url}/v1/merchants/me/rotate-key", headers=bearer(old_key), timeout=10)
    assert again.status_code == 401


def rotate(gateway_url: str, token: str) -> httpx.Response:
    return httpx.post(f"{gateway_url}/v1/merchants/me/rotate-key", headers=bearer(token), timeout=10)


def login(gateway_url: str, wallet: Wallet) -> str:
    response = verify(gateway_url, wallet, get_challenge(gateway_url, wallet.address, "login")["challenge"])
    assert response.status_code == 200, response.text
    return response.json()["session_token"]


def test_rotate_key_works_with_a_session_token_and_the_callers_session_dies_with_it(gateway_url: str, account: dict[str, Any]) -> None:
    """CONTRACT v2.4: rotate-key revokes ALL sessions, including the kgs_ token that made the call."""
    caller = account["session_token"]
    response = rotate(gateway_url, caller)
    assert response.status_code == 200, response.text
    body = response.json()
    assert me(gateway_url, caller).status_code == 401, "the caller's own session survived the rotation"
    assert me(gateway_url, account["api_key"]).status_code == 401
    assert me(gateway_url, body["api_key"]).status_code == 200
    assert body["session_token"].startswith("kgs_") and body["session_token"] != caller
    assert me(gateway_url, body["session_token"]).status_code == 200, "the session returned by rotate-key must work"


def test_rotate_key_revokes_every_session_of_the_merchant(gateway_url: str, account: dict[str, Any]) -> None:
    wallet: Wallet = account["wallet"]
    sessions = [account["session_token"], login(gateway_url, wallet), login(gateway_url, wallet)]
    assert all(me(gateway_url, token).status_code == 200 for token in sessions)
    response = rotate(gateway_url, account["api_key"])  # called with the API key: no session is the caller
    assert response.status_code == 200, response.text
    assert [me(gateway_url, token).status_code for token in sessions] == [401, 401, 401]
    assert me(gateway_url, response.json()["session_token"]).status_code == 200


def test_rotate_key_does_not_touch_other_merchants_sessions(gateway_url: str, account: dict[str, Any], shared_account: dict[str, Any]) -> None:
    assert rotate(gateway_url, account["api_key"]).status_code == 200
    assert me(gateway_url, shared_account["session_token"]).status_code == 200
    assert me(gateway_url, shared_account["api_key"]).status_code == 200


def test_a_wallet_can_log_back_in_after_rotating(gateway_url: str, account: dict[str, Any]) -> None:
    assert rotate(gateway_url, account["session_token"]).status_code == 200
    token = login(gateway_url, account["wallet"])
    assert me(gateway_url, token).status_code == 200


# ---------------------------------------------------------------- DELETE /v1/auth/session (CONTRACT v2.4)


def logout(gateway_url: str, token: str) -> httpx.Response:
    return httpx.delete(f"{gateway_url}/v1/auth/session", headers=bearer(token), timeout=10)


def test_logout_ends_only_the_presented_session(gateway_url: str, account: dict[str, Any]) -> None:
    other = login(gateway_url, account["wallet"])
    response = logout(gateway_url, account["session_token"])
    assert response.status_code == 204 and response.content == b""
    assert me(gateway_url, account["session_token"]).status_code == 401
    assert me(gateway_url, other).status_code == 200, "logout killed a different session"
    assert me(gateway_url, account["api_key"]).status_code == 200, "logout revoked the API key"


def test_logout_with_an_api_key_is_400_and_changes_nothing(gateway_url: str, account: dict[str, Any]) -> None:
    response = logout(gateway_url, account["api_key"])
    assert response.status_code == 400, response.text
    assert "rotate-key" in response.text
    assert me(gateway_url, account["api_key"]).status_code == 200
    assert me(gateway_url, account["session_token"]).status_code == 200


def test_logging_out_twice_is_401(gateway_url: str, account: dict[str, Any]) -> None:
    assert logout(gateway_url, account["session_token"]).status_code == 204
    assert logout(gateway_url, account["session_token"]).status_code == 401


def test_logout_with_unknown_or_malformed_tokens_is_401(gateway_url: str, network: str) -> None:
    for token in ("kgs_" + secrets.token_urlsafe(32), "kgs_", "kg_" + secrets.token_urlsafe(32)):
        assert logout(gateway_url, token).status_code == 401, token[:8]
    assert httpx.delete(f"{gateway_url}/v1/auth/session", timeout=10).status_code in {401, 403}


def test_a_session_cannot_log_out_another_merchants_session(gateway_url: str, account: dict[str, Any], shared_account: dict[str, Any]) -> None:
    """Tokens are bearer-scoped to their merchant: presenting merchant A's token never revokes merchant B's."""
    assert logout(gateway_url, account["session_token"]).status_code == 204
    assert me(gateway_url, shared_account["session_token"]).status_code == 200


def test_logged_out_session_cannot_rotate_the_key(gateway_url: str, account: dict[str, Any]) -> None:
    assert logout(gateway_url, account["session_token"]).status_code == 204
    assert rotate(gateway_url, account["session_token"]).status_code == 401
    assert me(gateway_url, account["api_key"]).status_code == 200  # the failed rotation did not rotate


@pytest.mark.skipif(
    os.environ.get("KASGATE_TEST_REDIS_DOWN") != "1",
    reason="set KASGATE_TEST_REDIS_DOWN=1 only when the gateway's Redis is stopped",
)
def test_logout_is_503_when_redis_is_down(gateway_url: str) -> None:
    """Sessions live in Redis: with it down the session lookup (and so the logout) answers 503 + Retry-After, never 204."""
    response = logout(gateway_url, "kgs_" + secrets.token_urlsafe(32))
    assert response.status_code == 503, (response.status_code, response.text)
    assert response.headers.get("retry-after")


def test_rotate_key_requires_a_valid_bearer_token(gateway_url: str, network: str) -> None:
    assert httpx.post(f"{gateway_url}/v1/merchants/me/rotate-key", timeout=10).status_code in {401, 403}
    wrong = httpx.post(
        f"{gateway_url}/v1/merchants/me/rotate-key", headers=bearer("kg_" + secrets.token_urlsafe(32)), timeout=10
    )
    assert wrong.status_code == 401


def test_rotated_key_still_serves_the_merchants_endpoints(gateway_url: str, account: dict[str, Any], min_price: int) -> None:
    created = httpx.post(
        f"{gateway_url}/v1/endpoints",
        headers=bearer(account["api_key"]),
        json={"name": "rotation-check", "upstream_url": "https://example.com/api", "price_sompi": min_price},
        timeout=10,
    )
    assert created.status_code == 201, created.text
    rotated = httpx.post(f"{gateway_url}/v1/merchants/me/rotate-key", headers=bearer(account["api_key"]), timeout=10)
    listing = httpx.get(f"{gateway_url}/v1/endpoints", headers=bearer(rotated.json()["api_key"]), timeout=10)
    assert listing.status_code == 200 and [e["id"] for e in listing.json()] == [created.json()["id"]]


def test_malformed_bearer_values_are_rejected(gateway_url: str, network: str) -> None:
    for header in ("Bearer", "Basic abc", "bearer kg_x", "Bearer " + "A" * 5000):
        response = httpx.get(f"{gateway_url}/v1/merchants/me", headers={"Authorization": header}, timeout=10)
        assert response.status_code in {401, 403}, (header[:20], response.status_code)
