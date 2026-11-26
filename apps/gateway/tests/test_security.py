import ipaddress
import socket
from typing import Any

import httpcore
import httpx
import pytest

from app import security
from app.security import (
    GuardedTransport,
    UpstreamBlockedError,
    _GuardedNetworkBackend,
    build_upstream_client,
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
        ("::ffff:10.0.0.1", True),
        ("64:ff9b::7f00:1", True),  # NAT64 of 127.0.0.1
        ("64:ff9b::a00:1", True),  # NAT64 of 10.0.0.1
        ("2002:7f00:1::", True),  # 6to4 of 127.0.0.1
        ("2002:c0a8:101::", True),  # 6to4 of 192.168.1.1
        ("64:ff9b::5db8:d822", False),  # NAT64 of 93.184.216.34
        ("100.127.255.255", True),
        ("100.128.0.1", False),
        ("192.0.0.1", True),
        ("198.18.0.1", True),
        ("240.0.0.1", True),
        ("224.0.0.1", True),
        ("93.184.216.34", False),
        ("2606:4700:4700::1111", False),
        ("::7f00:1", True),  # IPv4-compatible 127.0.0.1 (::/96, deprecated)
        ("::a00:1", True),  # IPv4-compatible 10.0.0.1
        ("::808:808", True),  # IPv4-compatible even of a public v4: the whole ::/96 block is refused
        ("fec0::1", True),  # site-local (fec0::/10, deprecated)
        ("feff:ffff::1", True),  # top of fec0::/10
        ("ff00::1", True),  # multicast, just past fec0::/10
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


# ---------------------------------------------------------------- SSRF guard with a mocked resolver


def fake_dns(monkeypatch: pytest.MonkeyPatch, table: dict[str, list[str]]) -> list[tuple[str, int]]:
    """Replace the resolver seam with a table of host -> addresses; returns the log of lookups."""
    lookups: list[tuple[str, int]] = []

    async def getaddrinfo(host: str, port: int) -> list[tuple[Any, Any, Any, Any, tuple[Any, ...]]]:
        lookups.append((host, port))
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "unknown host")
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, port, 0, 0) if ":" in address else (address, port),
            )
            for address in table[host]
        ]

    monkeypatch.setattr(security, "_getaddrinfo", getaddrinfo)
    return lookups


@pytest.mark.parametrize(
    ("host", "answer"),
    [
        ("10.0.0.5.nip.io", "10.0.0.5"),
        ("127.0.0.1.nip.io", "127.0.0.1"),
        ("169-254-169-254.nip.io", "169.254.169.254"),
        ("localtest.me", "127.0.0.1"),
        ("sub.localtest.me", "127.0.0.1"),
        ("cgnat.example.com", "100.64.0.9"),
        ("v6loop.example.com", "::1"),
        ("ula.example.com", "fd00::1"),
        ("mapped.example.com", "::ffff:192.168.0.1"),
        ("nat64.example.com", "64:ff9b::7f00:1"),
        ("sixtofour.example.com", "2002:a00:1::"),
        ("zero.example.com", "0.0.0.0"),  # noqa: S104
        ("multicast.example.com", "224.0.0.251"),
    ],
)
async def test_names_resolving_to_non_global_addresses_are_refused(
    monkeypatch: pytest.MonkeyPatch, host: str, answer: str
) -> None:
    fake_dns(monkeypatch, {host: [answer]})
    with pytest.raises(UpstreamBlockedError, match="non-public"):
        await resolve_public_address(host, 443)


async def test_one_private_answer_among_public_ones_refuses_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_dns(monkeypatch, {"mixed.example.com": ["93.184.216.34", "10.0.0.1"]})
    with pytest.raises(UpstreamBlockedError):
        await resolve_public_address("mixed.example.com", 443)


async def test_public_names_resolve_and_the_first_address_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    lookups = fake_dns(monkeypatch, {"api.example.com": ["93.184.216.34", "2606:4700:4700::1111"]})
    assert await resolve_public_address("api.example.com", 443) == "93.184.216.34"
    assert lookups == [("api.example.com", 443)]


async def test_unresolvable_and_empty_answers_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_dns(monkeypatch, {"empty.example.com": []})
    with pytest.raises(UpstreamBlockedError, match="cannot resolve"):
        await resolve_public_address("missing.example.com", 443)
    with pytest.raises(UpstreamBlockedError, match="no addresses"):
        await resolve_public_address("empty.example.com", 443)


async def test_literal_ips_outside_is_global_are_refused_without_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    lookups = fake_dns(monkeypatch, {})
    for literal in ("100.64.0.1", "127.0.0.1", "[::1]", "169.254.169.254"):
        with pytest.raises(UpstreamBlockedError):
            await resolve_public_address(literal, 80)
    assert all(host.strip("[]") in {"100.64.0.1", "127.0.0.1", "::1", "169.254.169.254"} for host, _ in lookups)


class _RecordingBackend:
    def __init__(self) -> None:
        self.connected: list[tuple[str, int]] = []

    async def connect_tcp(self, host: str, port: int, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        self.connected.append((host, port))
        raise httpcore.ConnectError("recorded; no real connection in tests")


async def test_the_connection_is_pinned_to_the_validated_ip_not_the_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_dns(monkeypatch, {"api.example.com": ["93.184.216.34"]})
    backend = _GuardedNetworkBackend()
    recording = _RecordingBackend()
    monkeypatch.setattr(backend, "_inner", recording)
    with pytest.raises(httpcore.ConnectError, match="recorded"):
        await backend.connect_tcp("api.example.com", 8443)
    assert recording.connected == [("93.184.216.34", 8443)]


async def test_dns_is_resolved_on_every_connection_so_rebinding_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])

    async def getaddrinfo(host: str, port: int) -> list[tuple[Any, Any, Any, Any, tuple[Any, ...]]]:
        address = next(answers)[0]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(security, "_getaddrinfo", getaddrinfo)
    backend = _GuardedNetworkBackend()
    recording = _RecordingBackend()
    monkeypatch.setattr(backend, "_inner", recording)
    with pytest.raises(httpcore.ConnectError, match="recorded"):
        await backend.connect_tcp("rebind.example.com", 443)
    with pytest.raises(httpcore.ConnectError, match="non-public"):
        await backend.connect_tcp("rebind.example.com", 443)
    assert recording.connected == [("93.184.216.34", 443)]


async def test_client_refuses_a_name_that_resolves_to_loopback_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_dns(monkeypatch, {"localtest.me": ["127.0.0.1"], "nip.example.com": ["100.64.1.1"]})
    async with build_upstream_client() as client:
        for url in ("http://localtest.me:9/x", "https://nip.example.com/x"):
            with pytest.raises(httpx.ConnectError, match="non-public"):
                await client.get(url)


async def test_unix_sockets_are_refused() -> None:
    with pytest.raises(httpcore.ConnectError):
        await _GuardedNetworkBackend().connect_unix_socket("/var/run/docker.sock")


def test_upstream_client_never_follows_redirects_and_ignores_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.internal:3128")
    client = build_upstream_client()
    assert client.follow_redirects is False
    assert client.trust_env is False
    assert isinstance(client._transport, GuardedTransport)
