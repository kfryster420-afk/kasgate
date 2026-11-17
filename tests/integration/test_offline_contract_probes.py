"""Offline probes of apps/gateway/app/schemas.py. `xfail(strict=True)` tests assert the SECURE behaviour: they fail
today (documenting a gap) and turn into hard failures the day the gap is fixed, so the marker must then be removed.
"""

from __future__ import annotations

import random

import pytest
from app.config import get_settings
from app.schemas import (
    MAX_HEADER_BYTES,
    EndpointCreate,
    MerchantCreate,
    PaymentSignature,
    compute_fee_split,
    min_price_sompi,
)

from conftest import OFFLINE_PAYER_KEY, address_for_key

PAYER = address_for_key(OFFLINE_PAYER_KEY)
PRICE = 2_000_000_000


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
    ],
)
def test_literal_private_upstreams_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        endpoint(url)


@pytest.mark.xfail(strict=True, reason="GAP-9: hostnames are not resolved; DNS names pointing at 127.0.0.1 pass")
@pytest.mark.parametrize("url", ["http://localtest.me", "http://127.0.0.1.nip.io"])
def test_dns_names_resolving_to_loopback_are_rejected(url: str) -> None:
    with pytest.raises(ValueError):
        endpoint(url)


@pytest.mark.xfail(strict=True, reason="GAP-9: carrier-grade NAT 100.64.0.0/10 is not treated as private")
def test_cgnat_upstream_is_rejected() -> None:
    with pytest.raises(ValueError):
        endpoint("http://100.64.0.1")


@pytest.mark.xfail(strict=True, reason="GAP-10: addresses are validated by charset/length only, not the bech32 checksum")
def test_merchant_address_with_bad_checksum_is_rejected() -> None:
    bad = PAYER[:-1] + ("q" if PAYER[-1] != "q" else "p")
    with pytest.raises(ValueError):
        MerchantCreate(kaspa_address=bad, display_name="typo")


@pytest.mark.xfail(strict=True, reason="GAP-8: PaymentSignature.rawTx allows 100k chars but the header cap is 131072 bytes")
def test_largest_legal_payment_signature_fits_in_the_header_cap() -> None:
    signature = PaymentSignature(txId="ab" * 32, rawTx='{"x":"' + "a" * 99_990 + '"}', payerAddress=PAYER)
    assert PaymentSignature.from_header(signature.to_header()).txId == "ab" * 32


def test_header_cap_is_not_what_real_servers_accept() -> None:
    """Info: MAX_HEADER_BYTES (128 KiB) exceeds uvicorn/h11 (16 KiB) and nginx (8 KiB per line) defaults."""
    assert MAX_HEADER_BYTES > 16 * 1024


def test_min_price_is_the_exact_boundary_for_many_floors() -> None:
    rng = random.Random(7)
    for floor in [1, 2, 3, 99, 100, 101, 10_000_000, 20_000_000, *(rng.randint(1, 10**9) for _ in range(300))]:
        price = min_price_sompi(floor)
        compute_fee_split(price, min_output_sompi=floor)
        with pytest.raises(ValueError):
            compute_fee_split(price - 1, min_output_sompi=floor)


def test_fee_split_invariants_hold_for_a_dense_sweep() -> None:
    floor = get_settings().min_output_sompi
    start = min_price_sompi(floor)
    for total in range(start, start + 20_000):
        split = compute_fee_split(total, min_output_sompi=floor)
        assert split.merchant_sompi + split.fee_sompi == total
        assert split.fee_sompi * 10_000 >= total * 150  # never under 1.5 %
        assert split.fee_sompi * 10_000 < total * 150 + 10_000  # over by less than one sompi of fee
