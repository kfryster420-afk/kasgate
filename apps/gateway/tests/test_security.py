import ipaddress

import httpx
import pytest

from app.security import (
    GuardedTransport,
    UpstreamBlockedError,
    decrypt_headers,
    encrypt_headers,
    generate_api_key,
    hash_api_key,
    is_forbidden_ip,
    resolve_public_address,
    validate_upstream_headers,
)


def test_api_key_format_and_hash() -> None:
    key = generate_api_key()
    assert key.startswith("kg_")
    assert len(key) >= 3 + 43
    assert generate_api_key() != key
    digest = hash_api_key(key)
    assert len(digest) == 64
    assert digest == hash_api_key(key)
    assert digest != hash_api_key(key + "x")


def test_headers_roundtrip_and_ciphertext_hides_secret() -> None:
    token = encrypt_headers({"Authorization": "Bearer s3cr3t", "X-Api-Key": "k"})
    assert token is not None
    assert b"s3cr3t" not in token
    assert decrypt_headers(token) == {"Authorization": "Bearer s3cr3t", "X-Api-Key": "k"}


def test_empty_headers_store_nothing() -> None:
    assert encrypt_headers({}) is None
    assert decrypt_headers(None) == {}


def test_tampered_ciphertext_is_rejected() -> None:
    token = encrypt_headers({"A": "b"})
    assert token is not None
    with pytest.raises(ValueError):
        decrypt_headers(token[:-4] + b"AAAA")


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": "evil"},
        {"payment-signature": "x"},
        {"Transfer-Encoding": "chunked"},
        {"Content-Length": "1"},
        {"Bad Name": "x"},
        {"X-Ok": "line\r\nInjected: 1"},
        {"X-A": "1", "x-a": "2"},
    ],
)
def test_forbidden_upstream_headers_are_rejected(headers: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        validate_upstream_headers(headers)


def test_valid_upstream_headers_pass() -> None:
    assert validate_upstream_headers({"Authorization": "Bearer x", "X-Api-Key": "abc"}) == {
        "Authorization": "Bearer x",
        "X-Api-Key": "abc",
    }


@pytest.mark.parametrize(
    ("address", "forbidden"),
    [
        ("127.0.0.1", True),
        ("10.1.2.3", True),
        ("172.16.0.1", True),
        ("192.168.1.1", True),
        ("169.254.169.254", True),
        ("100.64.0.1", True),
        ("0.0.0.0", True),  # noqa: S104
        ("::1", True),
        ("fe80::1", True),
        ("fc00::1", True),
        ("::ffff:127.0.0.1", True),
        ("224.0.0.1", True),
        ("93.184.216.34", False),
        ("2606:4700:4700::1111", False),
    ],
)
def test_forbidden_ip_classification(address: str, forbidden: bool) -> None:
    assert is_forbidden_ip(ipaddress.ip_address(address)) is forbidden


async def test_resolver_rejects_loopback_names() -> None:
    with pytest.raises(UpstreamBlockedError):
        await resolve_public_address("localhost", 80)


async def test_resolver_rejects_literal_private_ip() -> None:
    with pytest.raises(UpstreamBlockedError):
        await resolve_public_address("10.0.0.5", 80)


async def test_guarded_transport_refuses_private_targets() -> None:
    async with httpx.AsyncClient(transport=GuardedTransport(), follow_redirects=False) as client:
        with pytest.raises(httpx.ConnectError, match="non-public"):
            await client.get("http://localhost:9/")
        with pytest.raises(httpx.ConnectError, match="non-public"):
            await client.get("http://169.254.169.254/latest/meta-data")
