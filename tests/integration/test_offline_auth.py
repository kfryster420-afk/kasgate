"""Wallet login / registration primitives of the gateway (app.security / app.schemas), exercised without services.

CONTRACT v2: challenges are domain- and purpose-bound (`build_auth_challenge`), single use, and `register` proofs are
required to create a merchant.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import kaspa
import pytest
from app.config import get_settings
from app.schemas import (
    AUTH_PURPOSE,
    REGISTER_PURPOSE,
    AuthChallengeRequest,
    AuthVerifyRequest,
    MerchantCreate,
    build_auth_challenge,
)
from app.security import (
    AUTH_CHALLENGE_TTL_SECONDS,
    _check_wallet_signature,
    auth_challenge_key,
    auth_domain,
    consume_wallet_proof,
    new_auth_challenge,
    parse_auth_challenge,
)

from conftest import OFFLINE_PAYER_KEY, address_for_key

OTHER_KEY = "e7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfef"
KEY = kaspa.PrivateKey(OFFLINE_PAYER_KEY)
ADDRESS = address_for_key(OFFLINE_PAYER_KEY)
OTHER_ADDRESS = address_for_key(OTHER_KEY)
PUBLIC_KEY = KEY.to_public_key().to_string()
X_ONLY = KEY.to_public_key().to_x_only_public_key().to_string()


def signed(message: str, key: kaspa.PrivateKey = KEY) -> str:
    return kaspa.sign_message(message, key)


def challenge(purpose: str = "login", address: str = ADDRESS, now: datetime | None = None):
    return new_auth_challenge(purpose, address, now)  # type: ignore[arg-type]


class FakeRedis:
    """The one Redis call the proof check makes (GETDEL), over a dict."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def store(self, data) -> None:
        self.data[auth_challenge_key(data.purpose, data.address, data.nonce)] = data.message

    async def getdel(self, key: str):
        return self.data.pop(key, None)


# ---------------------------------------------------------------- challenge format


def test_challenge_has_the_documented_shape_and_binds_domain_purpose_and_address() -> None:
    data = challenge("login")
    lines = data.message.split("\n")
    assert lines[0] == "KasGate"
    assert lines[1] == f"Domain: {auth_domain()}"
    assert lines[2] == f"Purpose: {AUTH_PURPOSE}"
    assert lines[3] == f"Address: {ADDRESS}"
    assert lines[4].startswith("Nonce: ") and len(lines[4]) == len("Nonce: ") + 32
    assert datetime.fromisoformat(lines[5].removeprefix("Expires: ")) == data.expires_at
    assert data.expires_at - datetime.now(UTC) <= timedelta(seconds=AUTH_CHALLENGE_TTL_SECONDS)
    assert parse_auth_challenge(data.message) == data


def test_message_equals_the_contract_builder() -> None:
    data = challenge("register")
    assert data.message == build_auth_challenge(REGISTER_PURPOSE, auth_domain(), ADDRESS, data.nonce, data.expires_at)


def test_purposes_produce_different_messages_that_cannot_be_swapped() -> None:
    login, register = challenge("login"), challenge("register")
    assert AUTH_PURPOSE != REGISTER_PURPOSE
    assert f"Purpose: {AUTH_PURPOSE}" in login.message and f"Purpose: {REGISTER_PURPOSE}" in register.message
    assert "does not authorise any payment" in login.message and "does not authorise any payment" in register.message


def test_domain_is_the_lowercased_netloc_of_public_base_url() -> None:
    """CONTRACT v2.2: host, plus :port only when PUBLIC_BASE_URL has one. Exactly one form."""
    from urllib.parse import urlsplit

    assert auth_domain() == urlsplit(get_settings().public_base_url).netloc.lower()


def test_nonces_are_unique() -> None:
    assert len({challenge().nonce for _ in range(500)}) == 500


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.replace("\n", "\r\n"),
        lambda m: m + "\n",
        lambda m: " " + m,
        lambda m: m.replace("KasGate", "kasgate", 1),
        lambda m: m.replace("Nonce: ", "Nonce:  "),
        lambda m: m.split("\nNonce")[0],
        lambda m: m.replace("Domain: ", "Domain: evil.example.", 1),  # phishing site's domain
        lambda m: m.replace("Purpose: ", "Purpose: Also ", 1),
        lambda m: m.rsplit("\n", 1)[0] + "\nExpires: 2999-01-01T00:00:00",
        lambda m: m.rsplit("\n", 1)[0] + "\nExpires: 2999-01-01 00:00:00+00:00",
        lambda m: m.rsplit("\n", 1)[0] + "\nExpires: " + (datetime.now(UTC) + timedelta(days=3650)).isoformat() + "Z",
        lambda m: m.upper(),
        lambda m: "\n".join(m.split("\n")[:5] + ["Extra: line"] + m.split("\n")[5:]),
    ],
)
def test_non_canonical_challenges_are_rejected(mutate) -> None:
    with pytest.raises(ValueError):
        parse_auth_challenge(mutate(challenge().message))


def test_challenge_for_another_domain_does_not_parse_here() -> None:
    data = challenge()
    foreign = build_auth_challenge(AUTH_PURPOSE, "evil.example", ADDRESS, data.nonce, data.expires_at)
    with pytest.raises(ValueError):
        parse_auth_challenge(foreign)


def test_expired_challenge_still_parses_so_expiry_is_checked_by_the_consumer() -> None:
    data = challenge(now=datetime.now(UTC) - timedelta(days=1))
    assert parse_auth_challenge(data.message).expires_at < datetime.now(UTC)


# ---------------------------------------------------------------- signature checks


@pytest.mark.parametrize("public_key", [PUBLIC_KEY, X_ONLY])
def test_signature_by_the_address_owner_verifies_with_either_public_key_form(public_key: str) -> None:
    message = challenge().message
    assert _check_wallet_signature(message, signed(message), public_key, ADDRESS)


def test_signature_from_another_key_is_rejected() -> None:
    other = kaspa.PrivateKey(OTHER_KEY)
    message = challenge().message
    assert not _check_wallet_signature(message, signed(message, other), PUBLIC_KEY, ADDRESS)
    assert not _check_wallet_signature(message, signed(message, other), other.to_public_key().to_string(), ADDRESS)


def test_signature_over_a_different_message_is_rejected() -> None:
    message, other_message = challenge().message, challenge().message
    assert not _check_wallet_signature(message, signed(other_message), PUBLIC_KEY, ADDRESS)
    assert not _check_wallet_signature(message + " ", signed(message), PUBLIC_KEY, ADDRESS)


def test_mainnet_derived_address_never_matches_on_testnet() -> None:
    message = challenge().message
    mainnet = KEY.to_address("mainnet").to_string()
    assert not _check_wallet_signature(message, signed(message), PUBLIC_KEY, mainnet)


def test_corrupt_inputs_return_false_instead_of_raising() -> None:
    message = challenge().message
    assert not _check_wallet_signature(message, "00" * 64, PUBLIC_KEY, ADDRESS)
    assert not _check_wallet_signature(message, "zz", PUBLIC_KEY, ADDRESS)
    assert not _check_wallet_signature(message, signed(message), "00" * 33, ADDRESS)


# ---------------------------------------------------------------- consume_wallet_proof: single use, purpose, expiry


async def prove(redis: FakeRedis, data, purpose: str = "login", address: str = ADDRESS, **overrides) -> bool:
    args = {
        "purpose": purpose,
        "address": address,
        "challenge": data.message,
        "public_key": PUBLIC_KEY,
        "signature": signed(data.message),
    }
    args.update(overrides)
    return await consume_wallet_proof(redis, **args)  # type: ignore[arg-type]


async def test_valid_login_proof_is_accepted_exactly_once() -> None:
    redis, data = FakeRedis(), challenge("login")
    redis.store(data)
    assert await prove(redis, data)
    assert not await prove(redis, data)  # replay of the same signed challenge


async def test_valid_register_proof_is_accepted_exactly_once() -> None:
    redis, data = FakeRedis(), challenge("register")
    redis.store(data)
    assert await prove(redis, data, "register")
    assert not await prove(redis, data, "register")


async def test_register_challenge_cannot_be_used_to_log_in_and_vice_versa() -> None:
    redis = FakeRedis()
    register, login = challenge("register"), challenge("login")
    redis.store(register)
    redis.store(login)
    assert not await prove(redis, register, "login")
    assert not await prove(redis, login, "register")


async def test_a_failed_attempt_still_burns_the_challenge() -> None:
    redis, data = FakeRedis(), challenge()
    redis.store(data)
    other = kaspa.PrivateKey(OTHER_KEY)
    assert not await prove(redis, data, signature=signed(data.message, other))
    assert not await prove(redis, data)  # the legitimate signature no longer works either: single use


async def test_unknown_challenge_is_rejected() -> None:
    assert not await prove(FakeRedis(), challenge())


async def test_expired_challenge_is_rejected_even_if_stored() -> None:
    redis, data = FakeRedis(), challenge(now=datetime.now(UTC) - timedelta(seconds=AUTH_CHALLENGE_TTL_SECONDS + 5))
    redis.store(data)
    assert not await prove(redis, data)


async def test_challenge_issued_for_another_address_cannot_be_claimed() -> None:
    redis, data = FakeRedis(), challenge("login", OTHER_ADDRESS)
    redis.store(data)
    assert not await prove(redis, data, address=OTHER_ADDRESS)  # signed by KEY, not the owner of OTHER_ADDRESS
    redis.store(data)
    assert not await prove(redis, data, address=ADDRESS)  # claiming someone else's challenge for our address


async def test_tampered_stored_message_is_rejected() -> None:
    redis, data = FakeRedis(), challenge()
    redis.data[auth_challenge_key(data.purpose, data.address, data.nonce)] = data.message + " "
    assert not await prove(redis, data)


async def test_garbage_challenge_text_is_rejected_without_error() -> None:
    redis = FakeRedis()
    data = challenge()
    redis.store(data)
    assert not await prove(redis, data, challenge="not a challenge at all, but long enough")


# ---------------------------------------------------------------- schemas


def request(signature: str) -> AuthVerifyRequest:
    return AuthVerifyRequest(
        kaspa_address=ADDRESS, challenge=challenge().message, public_key=PUBLIC_KEY, signature=signature
    )


def test_hex_signature_is_accepted_and_lowercased() -> None:
    sig = signed("x" * 40)
    assert request(sig.upper()).signature == sig


def test_base64_64_byte_signature_is_normalised_to_hex() -> None:
    sig = signed("y" * 40)
    assert request(base64.b64encode(bytes.fromhex(sig)).decode()).signature == sig


def test_base64_65_byte_signature_drops_the_header_byte() -> None:
    sig = signed("z" * 40)
    assert request(base64.b64encode(b"\x1f" + bytes.fromhex(sig)).decode()).signature == sig


@pytest.mark.parametrize("bad", ["", "00", "g" * 128, "a" * 127, "a" * 129, base64.b64encode(b"x" * 63).decode()])
def test_malformed_signatures_fail_schema_validation(bad: str) -> None:
    with pytest.raises(ValueError):
        request(bad)


def test_mainnet_address_cannot_request_a_challenge_on_testnet() -> None:
    with pytest.raises(ValueError):
        AuthChallengeRequest(kaspa_address=KEY.to_address("mainnet").to_string())


def test_challenge_request_purpose_defaults_to_login_and_rejects_unknown_values() -> None:
    assert AuthChallengeRequest(kaspa_address=ADDRESS).purpose == "login"
    assert AuthChallengeRequest(kaspa_address=ADDRESS, purpose="register").purpose == "register"
    with pytest.raises(ValueError):
        AuthChallengeRequest(kaspa_address=ADDRESS, purpose="admin")  # type: ignore[arg-type]


def test_merchant_registration_requires_a_wallet_proof() -> None:
    with pytest.raises(ValueError):
        MerchantCreate(kaspa_address=ADDRESS, display_name="no proof")  # type: ignore[call-arg]
    data = challenge("register")
    sig = signed(data.message)
    ok = MerchantCreate(
        kaspa_address=ADDRESS,
        display_name="proven",
        challenge=data.message,
        public_key=PUBLIC_KEY,
        signature=base64.b64encode(bytes.fromhex(sig)).decode(),
    )
    assert ok.signature == sig
