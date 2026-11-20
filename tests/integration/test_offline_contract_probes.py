"""Offline probes of apps/gateway/app/schemas.py and the upstream guard in app/security.py (CONTRACT v2 / v2.1).

Every GAP that CONTRACT v2 closed (GAP-8, 9, 10) is now a plain regression test. `xfail(strict=True)` is reserved for
gaps that are still open: such a test asserts the SECURE behaviour, fails today, and turns into a hard failure the day
the gap is fixed so the marker must then be removed.
"""

from __future__ import annotations

import asyncio
import random
import socket
import threading
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app import security
from app.config import get_settings
from app.schemas import (
    MAX_HEADER_BYTES,
    MAX_RAW_TX_CHARS,
    EndpointCreate,
    MerchantCreate,
    PaymentRequired,
    PaymentSignature,
    compute_fee_split,
    min_price_sompi,
    new_payment_nonce,
)

from conftest import OFFLINE_PAYER_KEY, address_for_key

PAYER = address_for_key(OFFLINE_PAYER_KEY)
PRICE = 2_000_000_000
NONCE = "0123456789abcdef" * 2


def endpoint(url: str) -> EndpointCreate:
    return EndpointCreate(name="probe", upstream_url=url, price_sompi=PRICE)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1",
        "http://localhost",
        "http://2130706433",
        "http://0x7f.1",
        "http://127.1",
        "http://[::1]",
        "http://[::ffff:127.0.0.1]",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.1",
        "http://metadata.google.internal",
        "http://foo.localhost",
        "http://example.com@127.0.0.1",
        "http://100.64.0.1",  # GAP-9: carrier-grade NAT 100.64.0.0/10
        "http://100.127.255.254",
        "http://[fc00::1]",
        "http://[fd12:3456::1]",
        "http://0.0.0.0",
        "http://224.0.0.1",
    ],
)
def test_literal_private_upstreams_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        endpoint(url)


# ---------------------------------------------------------------- GAP-9: DNS names are guarded at request time


def _fake_dns(monkeypatch: pytest.MonkeyPatch, table: dict[str, list[str]]) -> None:
    async def fake(host: str, port: int):
        if host not in table:
            raise socket.gaierror("unknown host")
        return [
            (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in table[host]
        ]

    monkeypatch.setattr(security, "_getaddrinfo", fake)


@pytest.mark.parametrize(
    ("host", "addresses"),
    [
        ("localtest.me", ["127.0.0.1"]),
        ("127.0.0.1.nip.io", ["127.0.0.1"]),
        ("metadata.attacker.test", ["169.254.169.254"]),
        ("cgnat.attacker.test", ["100.64.0.1"]),
        ("v6.attacker.test", ["::1"]),
        ("ula.attacker.test", ["fd00::1"]),
        ("mapped.attacker.test", ["::ffff:127.0.0.1"]),
        ("nat64.attacker.test", ["64:ff9b::7f00:1"]),
        ("sixtofour.attacker.test", ["2002:7f00:1::1"]),
        ("mixed.attacker.test", ["93.184.216.34", "10.0.0.5"]),  # one private answer poisons the whole name
        ("empty.attacker.test", []),
    ],
)
async def test_hostnames_resolving_to_non_public_addresses_are_blocked_at_request_time(monkeypatch, host, addresses) -> None:
    _fake_dns(monkeypatch, {host: addresses})
    with pytest.raises(security.UpstreamBlockedError):
        await security.resolve_public_address(host, 443)


async def test_public_hostname_is_resolved_and_pinned_to_a_single_ip(monkeypatch) -> None:
    _fake_dns(monkeypatch, {"api.merchant.test": ["93.184.216.34", "93.184.216.35"]})
    assert await security.resolve_public_address("api.merchant.test", 443) == "93.184.216.34"


async def test_unresolvable_host_is_blocked_not_crashed(monkeypatch) -> None:
    _fake_dns(monkeypatch, {})
    with pytest.raises(security.UpstreamBlockedError):
        await security.resolve_public_address("nope.attacker.test", 443)


async def test_guarded_transport_never_opens_a_connection_to_a_loopback_resolution(monkeypatch) -> None:
    """End to end: a name that resolves to 127.0.0.1 (DNS rebinding) must be refused before any TCP connect."""
    hits: list[str] = []
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    server.settimeout(1.0)
    port = server.getsockname()[1]

    def accept_loop() -> None:
        try:
            conn, _ = server.accept()
            hits.append("connected")
            conn.close()
        except OSError:
            pass

    thread = threading.Thread(target=accept_loop, daemon=True)
    thread.start()
    _fake_dns(monkeypatch, {"rebind.attacker.test": ["127.0.0.1"]})
    async with security.build_upstream_client() as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(f"http://rebind.attacker.test:{port}/internal")
    await asyncio.to_thread(thread.join, 2.0)
    server.close()
    assert hits == []


@pytest.mark.parametrize(
    ("ip", "forbidden"),
    [
        ("93.184.216.34", False),
        ("8.8.8.8", False),
        ("2606:4700:4700::1111", False),
        ("127.0.0.1", True),
        ("100.64.0.1", True),
        ("192.168.1.1", True),
        ("::ffff:10.0.0.1", True),
        ("64:ff9b::a00:1", True),
        ("2001:0:4136:e378:8000:63bf:3fff:fdd2", True),  # Teredo embedding a private v4
        ("ff02::1", True),
    ],
)
def test_forbidden_ip_classification(ip: str, forbidden: bool) -> None:
    import ipaddress

    assert security.is_forbidden_ip(ipaddress.ip_address(ip)) is forbidden


# ---------------------------------------------------------------- GAP-10: bech32 checksum


def test_merchant_address_with_bad_checksum_is_rejected() -> None:
    for position in (-1, -2, -5, 10):
        chars = list(PAYER)
        index = position if position < 0 else len(PAYER.split(":")[0]) + 1 + position
        chars[index] = "q" if chars[index] != "q" else "p"
        with pytest.raises(ValueError):
            MerchantCreate(
                kaspa_address="".join(chars),
                display_name="typo",
                challenge="x" * 20,
                public_key="ab" * 32,
                signature="cd" * 64,
            )


def test_valid_address_passes_the_checksum() -> None:
    merchant = MerchantCreate(
        kaspa_address=PAYER, display_name="ok", challenge="x" * 20, public_key="ab" * 32, signature="cd" * 64
    )
    assert merchant.kaspa_address == PAYER


# ---------------------------------------------------------------- GAP-8: header and rawTx limits are consistent


def test_gateway_limits_are_the_v2_values() -> None:
    assert MAX_RAW_TX_CHARS == 24_000
    assert MAX_HEADER_BYTES == 40_960


def test_largest_legal_payment_signature_fits_in_the_header_cap() -> None:
    signature = PaymentSignature(
        txId="ab" * 32, rawTx='{"x":"' + "a" * (MAX_RAW_TX_CHARS - 10) + '"}', payerAddress=PAYER, nonce=NONCE
    )
    header = signature.to_header()
    assert len(header) <= MAX_HEADER_BYTES
    assert PaymentSignature.from_header(header).txId == "ab" * 32


def test_rawtx_one_char_over_the_limit_is_rejected() -> None:
    with pytest.raises(ValueError):
        PaymentSignature(txId="ab" * 32, rawTx="x" * (MAX_RAW_TX_CHARS + 1), payerAddress=PAYER, nonce=NONCE)


def test_header_one_byte_over_the_cap_is_rejected() -> None:
    with pytest.raises(ValueError):
        PaymentSignature.from_header("A" * (MAX_HEADER_BYTES + 1))


def test_largest_legal_header_exceeds_the_default_h11_limit_so_front_proxies_must_be_tuned() -> None:
    """Info: uvicorn's h11 default (16 KiB) and nginx's 8 KiB per header line are below the 40 KiB cap; the gateway
    Dockerfile sets --h11-max-incomplete-event-size 65536 but any reverse proxy in front needs the same treatment."""
    signature = PaymentSignature(
        txId="ab" * 32, rawTx='{"x":"' + "a" * (MAX_RAW_TX_CHARS - 10) + '"}', payerAddress=PAYER, nonce=NONCE
    )
    assert len(signature.to_header()) > 16 * 1024


# ---------------------------------------------------------------- challenge schema (nonce, expiry, v2.1 split)


def _required(price: int, **overrides) -> PaymentRequired:
    return PaymentRequired.for_endpoint(
        price, PAYER, overrides.pop("nonce", NONCE), overrides.pop("expires_at", datetime.now(UTC) + timedelta(seconds=60))
    )


def test_payment_nonce_is_32_lowercase_hex_and_unique() -> None:
    nonces = {new_payment_nonce() for _ in range(2_000)}
    assert len(nonces) == 2_000
    assert all(len(n) == 32 and n == n.lower() and int(n, 16) >= 0 for n in nonces)


@pytest.mark.parametrize("nonce", ["", "A" * 32, "g" * 32, "a" * 31, "a" * 33, " " + "a" * 31])
def test_malformed_nonce_is_rejected_by_the_schema(nonce: str) -> None:
    with pytest.raises(ValueError):
        _required(PRICE, nonce=nonce)


def test_min_price_is_the_exact_boundary_for_many_floors() -> None:
    rng = random.Random(7)
    for floor in [1, 2, 3, 99, 100, 101, 10_000_000, 20_000_000, *(rng.randint(1, 10**9) for _ in range(300))]:
        price = min_price_sompi(floor)
        assert price == 3 * floor
        split = compute_fee_split(price, min_output_sompi=floor)
        assert split.merchant_sompi >= 2 * floor and split.fee_sompi >= floor
        assert (split.merchant_sompi, split.fee_sompi) == (2 * floor, floor) or floor > 100_000_000
        with pytest.raises(ValueError):
            compute_fee_split(price - 1, min_output_sompi=floor)


def test_v2_1_fee_split_invariants_hold_for_a_dense_sweep() -> None:
    floor = get_settings().min_output_sompi
    start = min_price_sompi(floor)
    assert (floor, start) == (20_000_000, 60_000_000)
    for total in range(start, start + 20_000):
        split = compute_fee_split(total, min_output_sompi=floor)
        assert split.merchant_sompi + split.fee_sompi == total
        assert split.fee_sompi == max(-(-total * 150 // 10_000), floor)
        assert split.fee_sompi * 10_000 >= total * 150  # never under 1.5 %
        assert split.merchant_sompi >= 2 * floor


def test_v2_1_crossover_window_is_exact() -> None:
    floor = get_settings().min_output_sompi
    for total in range(1_333_333_000, 1_333_334_000):
        fee = compute_fee_split(total, min_output_sompi=floor).fee_sompi
        if total <= 1_333_333_333:
            assert fee == floor, total
        else:
            assert fee == -(-total * 150 // 10_000) > floor, total
    assert compute_fee_split(1_333_333_333, min_output_sompi=floor).fee_sompi == 20_000_000
    assert compute_fee_split(1_333_333_334, min_output_sompi=floor).fee_sompi == 20_000_001


def test_v2_1_percentage_regime_has_no_off_by_one_for_large_prices() -> None:
    rng = random.Random(21)
    floor = get_settings().min_output_sompi
    for _ in range(10_000):
        total = rng.randint(1_333_333_334, 10**15)
        fee = compute_fee_split(total, min_output_sompi=floor).fee_sompi
        assert fee == -(-total * 150 // 10_000) > floor
