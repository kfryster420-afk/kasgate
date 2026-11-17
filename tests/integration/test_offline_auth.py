"""Wallet-login primitives of the gateway (app.security / app.schemas), exercised without services."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import kaspa
import pytest
from app.schemas import AuthVerifyRequest
from app.security import (
    AUTH_CHALLENGE_TTL_SECONDS,
    _check_wallet_signature,
    format_login_challenge,
    new_login_challenge,
    parse_login_challenge,
)
from conftest import OFFLINE_PAYER_KEY, address_for_key

OTHER_KEY = "e7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfef"
KEY = kaspa.PrivateKey(OFFLINE_PAYER_KEY)
ADDRESS = address_for_key(OFFLINE_PAYER_KEY)
PUBLIC_KEY = KEY.to_public_key().to_string()
X_ONLY = KEY.to_public_key().to_x_only_public_key().to_string()


def signed(message: str, key: kaspa.PrivateKey = KEY) -> str:
    return kaspa.sign_message(message, key)


def test_challenge_has_the_documented_shape() -> None:
    challenge, message = new_login_challenge(ADDRESS)
    lines = message.split("\n")
    assert lines[0] == "KasGate login" and lines[1] == f"Address: {ADDRESS}"
    assert lines[2].startswith("Nonce: ") and len(lines[2]) == len("Nonce: ") + 32
    assert datetime.fromisoformat(lines[3].removeprefix("Expires: ")) == challenge.expires_at
    assert challenge.expires_at - datetime.now(UTC) <= timedelta(seconds=AUTH_CHALLENGE_TTL_SECONDS)
    assert parse_login_challenge(message) == challenge


def test_nonces_are_unique() -> None:
    assert len({new_login_challenge(ADDRESS)[0].nonce for _ in range(500)}) == 500


@pytest.mark.parametrize("public_key", [PUBLIC_KEY, X_ONLY])
def test_signature_by_the_address_owner_verifies_with_either_public_key_form(public_key: str) -> None:
    _, message = new_login_challenge(ADDRESS)
    assert _check_wallet_signature(message, signed(message), public_key, ADDRESS)


def test_signature_from_another_key_is_rejected() -> None:
    other = kaspa.PrivateKey(OTHER_KEY)
    _, message = new_login_challenge(ADDRESS)
    assert not _check_wallet_signature(message, signed(message, other), PUBLIC_KEY, ADDRESS)
    assert not _check_wallet_signature(message, signed(message, other), other.to_public_key().to_string(), ADDRESS)


def test_public_key_that_does_not_derive_the_claimed_address_is_rejected() -> None:
    other = kaspa.PrivateKey(OTHER_KEY)
    _, message = new_login_challenge(ADDRESS)
    assert not _check_wallet_signature(message, signed(message, other), other.to_public_key().to_string(), ADDRESS)


def test_signature_over_a_different_message_is_rejected() -> None:
    _, message = new_login_challenge(ADDRESS)
    _, other_message = new_login_challenge(ADDRESS)
    assert not _check_wallet_signature(message, signed(other_message), PUBLIC_KEY, ADDRESS)
    assert not _check_wallet_signature(message + " ", signed(message), PUBLIC_KEY, ADDRESS)


def test_mainnet_derived_address_never_matches_on_testnet() -> None:
    _, message = new_login_challenge(ADDRESS)
    mainnet = KEY.to_address("mainnet").to_string()
    assert not _check_wallet_signature(message, signed(message), PUBLIC_KEY, mainnet)


def test_corrupt_inputs_return_false_instead_of_raising() -> None:
    _, message = new_login_challenge(ADDRESS)
    assert not _check_wallet_signature(message, "00" * 64, PUBLIC_KEY, ADDRESS)
    assert not _check_wallet_signature(message, "zz", PUBLIC_KEY, ADDRESS)
    assert not _check_wallet_signature(message, signed(message), "00" * 33, ADDRESS)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.replace("\n", "\r\n"),
        lambda m: m + "\n",
        lambda m: " " + m,
        lambda m: m.replace("KasGate login", "kasgate login"),
        lambda m: m.replace("Nonce: ", "Nonce:  "),
        lambda m: m.split("\nNonce")[0],
        lambda m: m.rsplit("\n", 1)[0] + "\nExpires: 2999-01-01T00:00:00",
        lambda m: m.rsplit("\n", 1)[0] + "\nExpires: 2999-01-01 00:00:00+00:00",
        lambda m: m.rsplit("\n", 1)[0] + "\nExpires: " + (datetime.now(UTC) + timedelta(days=3650)).isoformat() + "Z",
        lambda m: m.replace("Nonce: ", "Nonce: ").upper(),
    ],
)
def test_non_canonical_challenges_are_rejected(mutate) -> None:
    _, message = new_login_challenge(ADDRESS)
    with pytest.raises(ValueError):
        parse_login_challenge(mutate(message))


def test_expired_challenge_still_parses_so_expiry_must_be_checked_by_the_caller() -> None:
    challenge, _ = new_login_challenge(ADDRESS, now=datetime.now(UTC) - timedelta(days=1))
    parsed = parse_login_challenge(format_login_challenge(challenge))
    assert parsed.expires_at < datetime.now(UTC)


def request(signature: str) -> AuthVerifyRequest:
    _, message = new_login_challenge(ADDRESS)
    return AuthVerifyRequest(kaspa_address=ADDRESS, challenge=message, public_key=PUBLIC_KEY, signature=signature)


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


def test_mainnet_address_cannot_request_a_login_on_testnet() -> None:
    from app.schemas import AuthChallengeRequest

    with pytest.raises(ValueError):
        AuthChallengeRequest(kaspa_address=KEY.to_address("mainnet").to_string())


@pytest.mark.xfail(strict=True, reason="GAP-12: the login challenge carries no origin/domain or purpose binding (phishable)")
def test_challenge_binds_the_signature_to_an_origin() -> None:
    _, message = new_login_challenge(ADDRESS)
    assert any(line.lower().startswith(("domain:", "origin:", "uri:")) for line in message.split("\n"))
