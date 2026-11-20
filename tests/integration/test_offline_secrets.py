"""The funded payer key must be loadable from a file and must never be renderable as text."""

from __future__ import annotations

import pytest

import conftest
from conftest import SecretKey, _funded_key, address_for_key

DUMMY = "0123456789abcdef" * 4


def test_secret_key_masks_every_textual_rendering() -> None:
    key = SecretKey(DUMMY)
    for rendered in (repr(key), str(key), f"{key}", f"{key!r}", "%s" % key, "{}".format(key), f"{[key]}", f"{ {'k': key} }"):
        assert DUMMY not in rendered and DUMMY[:8] not in rendered, rendered


def test_secret_key_in_assertion_messages_does_not_leak_it() -> None:
    """Never compare the key itself in an assert: pytest's str diff prints both operands. Messages are safe."""
    key = SecretKey(DUMMY)
    with pytest.raises(AssertionError) as caught:
        assert len(key) == 0, f"key was {key} / {key!r} / {[key]}"
    assert DUMMY not in str(caught.value) and DUMMY[:8] not in str(caught.value)


def test_secret_key_still_works_as_a_key() -> None:
    assert address_for_key(SecretKey(DUMMY)) == address_for_key(DUMMY)
    assert SecretKey(DUMMY) == DUMMY and len(SecretKey(DUMMY)) == 64


def test_key_is_loaded_from_the_file_env_var(tmp_path, monkeypatch) -> None:
    path = tmp_path / "buyer.key"
    path.write_text(DUMMY.upper() + "\n")
    monkeypatch.delenv("KASGATE_TEST_PAYER_KEY", raising=False)
    monkeypatch.setenv("KASGATE_TEST_PAYER_KEY_FILE", str(path))
    key = _funded_key()
    assert isinstance(key, SecretKey) and key == DUMMY


def test_env_var_wins_over_the_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "buyer.key"
    path.write_text("f" * 64)
    monkeypatch.setenv("KASGATE_TEST_PAYER_KEY", DUMMY)
    monkeypatch.setenv("KASGATE_TEST_PAYER_KEY_FILE", str(path))
    assert _funded_key() == DUMMY


@pytest.mark.parametrize("content", ["", "not-hex", "ab" * 31, "zz" * 32, DUMMY + "00"])
def test_unreadable_or_malformed_key_means_no_funded_key(tmp_path, monkeypatch, content: str) -> None:
    path = tmp_path / "buyer.key"
    path.write_text(content)
    monkeypatch.delenv("KASGATE_TEST_PAYER_KEY", raising=False)
    monkeypatch.setenv("KASGATE_TEST_PAYER_KEY_FILE", str(path))
    assert _funded_key() == ""


def test_missing_file_means_no_funded_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("KASGATE_TEST_PAYER_KEY", raising=False)
    monkeypatch.setenv("KASGATE_TEST_PAYER_KEY_FILE", str(tmp_path / "absent"))
    assert _funded_key() == ""


def test_default_key_file_path_is_the_secrets_dir() -> None:
    assert conftest.DEFAULT_PAYER_KEY_FILE == "/workspace/kg-secrets/buyer.key"


async def test_sdk_client_accepts_a_secret_key_and_hides_it(monkeypatch) -> None:
    from kasgate_client import KasGateClient

    client = KasGateClient(SecretKey(DUMMY), network="kaspa:testnet-10")
    try:
        assert DUMMY not in repr(client) and DUMMY not in str(vars(client).get("address", ""))
    finally:
        await client.aclose()
