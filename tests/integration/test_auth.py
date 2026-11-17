"""Live wallet-login and key-rotation tests (POST /v1/auth/challenge, /v1/auth/verify, /v1/merchants/me/rotate-key).

Login rotates the merchant's API key, so these tests use their own throw-away merchant and never touch the
session-wide `auth` fixture. The suite expects RATE_LIMIT_ENABLED=false on the gateway; if limiting is on anyway, `post` waits out a 429 once.
"""

from __future__ import annotations

import asyncio
import base64
import secrets
import time
from collections import Counter
from typing import Any

import httpx
import kaspa
import pytest
from conftest import address_for_key

pytestmark = pytest.mark.live

GENERIC_FAILURE = "login failed: invalid address, challenge or signature"


def post(url: str, **kwargs: Any) -> httpx.Response:
    """POST that sleeps through one 429 (auth routes allow 10 requests per minute per IP)."""
    response = httpx.post(url, timeout=15, **kwargs)
    if response.status_code == 429:
        time.sleep(min(int(response.headers.get("retry-after", "30")) + 1, 65))
        response = httpx.post(url, timeout=15, **kwargs)
    return response


class Wallet:
    def __init__(self, key_hex: str | None = None) -> None:
        self.key_hex = key_hex or secrets.token_hex(32)
        self.key = kaspa.PrivateKey(self.key_hex)
        self.address = address_for_key(self.key_hex)
        self.public_key = self.key.to_public_key().to_string()

    def sign(self, message: str) -> str:
        return kaspa.sign_message(message, self.key)


def get_challenge(gateway_url: str, address: str) -> dict[str, Any]:
    response = post(f"{gateway_url}/v1/auth/challenge", json={"kaspa_address": address})
    assert response.status_code == 200, response.text
    return response.json()


def verify(gateway_url: str, wallet: Wallet, challenge: str, **overrides: Any) -> httpx.Response:
    body = {
        "kaspa_address": wallet.address,
        "challenge": challenge,
        "public_key": wallet.public_key,
        "signature": wallet.sign(challenge),
    }
    body.update(overrides)
    return post(f"{gateway_url}/v1/auth/verify", json=body)


def bearer(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def me(gateway_url: str, api_key: str) -> httpx.Response:
    return httpx.get(f"{gateway_url}/v1/merchants/me", headers=bearer(api_key), timeout=10)


@pytest.fixture
def account(gateway_url: str, network: str) -> dict[str, Any]:
    """A freshly registered merchant whose wallet key we hold. Costs one POST /v1/merchants (5/hour per IP)."""
    wallet = Wallet()
    response = post(
        f"{gateway_url}/v1/merchants", json={"kaspa_address": wallet.address, "display_name": f"qa-auth-{secrets.token_hex(3)}"}
    )
    if response.status_code == 429:
        pytest.skip("POST /v1/merchants is rate limited: run the gateway with RATE_LIMIT_ENABLED=false for this suite")
    assert response.status_code == 201, response.text
    return {"wallet": wallet, "api_key": response.json()["api_key"], "id": response.json()["merchant"]["id"]}


@pytest.fixture(scope="module")
def shared_account(gateway_url: str, network: str) -> dict[str, Any]:
    wallet = Wallet()
    response = post(
        f"{gateway_url}/v1/merchants", json={"kaspa_address": wallet.address, "display_name": f"qa-auth-{secrets.token_hex(3)}"}
    )
    if response.status_code == 429:
        pytest.skip("POST /v1/merchants is rate limited: run the gateway with RATE_LIMIT_ENABLED=false for this suite")
    assert response.status_code == 201, response.text
    return {"wallet": wallet, "api_key": response.json()["api_key"], "id": response.json()["merchant"]["id"]}


# ---------------------------------------------------------------- challenge shape


def test_challenge_response_matches_the_contract(gateway_url: str, shared_account: dict[str, Any]) -> None:
    wallet: Wallet = shared_account["wallet"]
    body = get_challenge(gateway_url, wallet.address)
    assert set(body) == {"kaspa_address", "challenge", "expires_at"}
    assert body["kaspa_address"] == wallet.address
    lines = body["challenge"].split("\n")
    assert lines[0] == "KasGate login" and lines[1] == f"Address: {wallet.address}"
    assert lines[2].startswith("Nonce: ") and len(lines[2].removeprefix("Nonce: ")) == 32
    assert lines[3].startswith("Expires: ")


def test_each_challenge_has_a_fresh_nonce(gateway_url: str, shared_account: dict[str, Any]) -> None:
    address = shared_account["wallet"].address
    first, second = get_challenge(gateway_url, address), get_challenge(gateway_url, address)
    assert first["challenge"] != second["challenge"]


def test_mainnet_prefixed_address_cannot_request_a_challenge(gateway_url: str) -> None:
    response = post(
        f"{gateway_url}/v1/auth/challenge",
        json={"kaspa_address": "kaspa:qr0lr4ml9fn3chekrqmjdkergxl93l4wrk3dankcgvjq776s9wn9jkdskewva"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------- login round trip


def test_wallet_login_rotates_the_key_and_invalidates_the_old_one(gateway_url: str, account: dict[str, Any]) -> None:
    wallet: Wallet = account["wallet"]
    old_key = account["api_key"]
    assert me(gateway_url, old_key).status_code == 200

    challenge = get_challenge(gateway_url, wallet.address)["challenge"]
    response = verify(gateway_url, wallet, challenge)
    assert response.status_code == 200, response.text
    body = response.json()
    new_key = body["api_key"]
    assert new_key.startswith("kg_") and new_key != old_key
    assert body["merchant"]["id"] == account["id"] and body["merchant"]["kaspa_address"] == wallet.address

    assert me(gateway_url, old_key).status_code == 401
    assert me(gateway_url, new_key).status_code == 200


def test_compressed_and_x_only_public_keys_both_work(gateway_url: str, account: dict[str, Any]) -> None:
    wallet: Wallet = account["wallet"]
    x_only = wallet.key.to_public_key().to_x_only_public_key().to_string()
    challenge = get_challenge(gateway_url, wallet.address)["challenge"]
    assert verify(gateway_url, wallet, challenge, public_key=x_only).status_code == 200


def test_reused_challenge_is_401(gateway_url: str, account: dict[str, Any]) -> None:
    wallet: Wallet = account["wallet"]
    challenge = get_challenge(gateway_url, wallet.address)["challenge"]
    signature = wallet.sign(challenge)
    first = verify(gateway_url, wallet, challenge, signature=signature)
    second = verify(gateway_url, wallet, challenge, signature=signature)
    assert first.status_code == 200, first.text
    assert second.status_code == 401
    assert second.json()["detail"] == GENERIC_FAILURE
    # the key minted by the first login must survive the failed replay
    assert me(gateway_url, first.json()["api_key"]).status_code == 200


def test_concurrent_logins_with_one_challenge_succeed_exactly_once(gateway_url: str, account: dict[str, Any]) -> None:
    wallet: Wallet = account["wallet"]
    challenge = get_challenge(gateway_url, wallet.address)["challenge"]
    body = {
        "kaspa_address": wallet.address,
        "challenge": challenge,
        "public_key": wallet.public_key,
        "signature": wallet.sign(challenge),
    }

    async def race() -> list[httpx.Response]:
        async with httpx.AsyncClient(timeout=30) as client:
            return await asyncio.gather(*(client.post(f"{gateway_url}/v1/auth/verify", json=body) for _ in range(5)))

    responses = asyncio.run(race())
    statuses = Counter(r.status_code for r in responses)
    assert statuses[200] == 1 and statuses[401] + statuses[429] == 4, statuses


def test_base64_signature_is_accepted(gateway_url: str, account: dict[str, Any]) -> None:
    wallet: Wallet = account["wallet"]
    challenge = get_challenge(gateway_url, wallet.address)["challenge"]
    raw = bytes.fromhex(wallet.sign(challenge))
    response = verify(gateway_url, wallet, challenge, signature=base64.b64encode(raw).decode())
    assert response.status_code == 200, response.text


def test_base64_signature_with_a_header_byte_is_accepted(gateway_url: str, account: dict[str, Any]) -> None:
    wallet: Wallet = account["wallet"]
    challenge = get_challenge(gateway_url, wallet.address)["challenge"]
    raw = b"\x1f" + bytes.fromhex(wallet.sign(challenge))
    response = verify(gateway_url, wallet, challenge, signature=base64.b64encode(raw).decode())
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------- failures are generic 401s


def test_unknown_address_gets_a_challenge_but_cannot_log_in(gateway_url: str, shared_account: dict[str, Any]) -> None:
    stranger = Wallet()
    body = get_challenge(gateway_url, stranger.address)  # no enumeration: 200 for an address nobody registered
    assert body["kaspa_address"] == stranger.address
    unknown = verify(gateway_url, stranger, body["challenge"])
    assert unknown.status_code == 401
    # the failure must look exactly like a bad signature for a REAL merchant
    real: Wallet = shared_account["wallet"]
    real_challenge = get_challenge(gateway_url, real.address)["challenge"]
    bad_signature = verify(gateway_url, real, real_challenge, signature=stranger.sign(real_challenge))
    assert bad_signature.status_code == 401
    assert unknown.json() == bad_signature.json() and unknown.json()["detail"] == GENERIC_FAILURE


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


@pytest.mark.parametrize("mutation", ["nonce", "whitespace", "expiry", "garbage", "forged"])
def test_tampered_or_forged_challenges_are_401(gateway_url: str, shared_account: dict[str, Any], mutation: str) -> None:
    wallet: Wallet = shared_account["wallet"]
    challenge = get_challenge(gateway_url, wallet.address)["challenge"]
    forged = {
        "nonce": challenge.replace("Nonce: ", "Nonce: 0"),
        "whitespace": challenge + " ",
        "expiry": challenge.rsplit("Expires: ", 1)[0] + "Expires: 2999-01-01T00:00:00+00:00",
        "garbage": "not a login challenge, but long enough to pass length checks",
        "forged": f"KasGate login\nAddress: {wallet.address}\nNonce: {secrets.token_hex(16)}\nExpires: 2999-01-01T00:00:00+00:00",
    }[mutation]
    response = verify(gateway_url, wallet, forged)
    assert response.status_code == 401, response.text
    assert response.json()["detail"] == GENERIC_FAILURE


# ---------------------------------------------------------------- rotate-key


def test_rotate_key_invalidates_the_old_key_immediately(gateway_url: str, account: dict[str, Any]) -> None:
    old_key = account["api_key"]
    response = httpx.post(f"{gateway_url}/v1/merchants/me/rotate-key", headers=bearer(old_key), timeout=10)
    assert response.status_code == 200, response.text
    new_key = response.json()["api_key"]
    assert new_key != old_key and response.json()["merchant"]["id"] == account["id"]
    assert me(gateway_url, old_key).status_code == 401
    assert me(gateway_url, new_key).status_code == 200
    again = httpx.post(f"{gateway_url}/v1/merchants/me/rotate-key", headers=bearer(old_key), timeout=10)
    assert again.status_code == 401


def test_rotate_key_requires_a_valid_bearer_token(gateway_url: str) -> None:
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
    new_key = rotated.json()["api_key"]
    listing = httpx.get(f"{gateway_url}/v1/endpoints", headers=bearer(new_key), timeout=10)
    assert listing.status_code == 200 and [e["id"] for e in listing.json()] == [created.json()["id"]]


def test_malformed_bearer_values_are_rejected(gateway_url: str) -> None:
    for header in ("Bearer", "Bearer ", "Basic abc", "bearer kg_x", "Bearer " + "A" * 5000):
        response = httpx.get(f"{gateway_url}/v1/merchants/me", headers={"Authorization": header}, timeout=10)
        assert response.status_code in {401, 403}, (header[:20], response.status_code)
