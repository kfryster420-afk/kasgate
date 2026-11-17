"""Byte-compatibility of the SDK header models with apps/gateway/app/schemas.py."""

from __future__ import annotations

import json
import random

import kasgate_client as sdk
import pytest
from app import schemas as gw
from conftest import MERCHANT, OTHER, PAYER, PRICE, TREASURY, challenge

TX_ID = "ab" * 32


@pytest.mark.parametrize("price", [1_333_333_267, PRICE, 2_000_000_000, 123_456_789_012])
def test_payment_required_identical_bytes_and_cross_decode(price: int) -> None:
    gateway_model = gw.PaymentRequired.for_endpoint(price, MERCHANT)
    header = gateway_model.to_header()

    decoded = sdk.PaymentRequired.from_header(header)
    assert decoded.total_sompi == price
    assert decoded.merchant_sompi == gateway_model.merchant_amount_sompi
    assert decoded.fee_sompi == gateway_model.fee_amount_sompi
    assert decoded.payTo == MERCHANT and decoded.platformFee.feeTo == TREASURY

    assert decoded.to_header() == header
    assert gw.PaymentRequired.from_header(decoded.to_header()) == gateway_model


def test_payment_signature_identical_bytes_both_directions() -> None:
    raw_tx = json.dumps({"id": TX_ID, "inputs": [], "outputs": []})
    sdk_model = sdk.PaymentSignature(txId=TX_ID, rawTx=raw_tx, payerAddress=PAYER)
    gateway_model = gw.PaymentSignature(txId=TX_ID, rawTx=raw_tx, payerAddress=PAYER)

    assert sdk_model.to_header() == gateway_model.to_header()
    assert gw.PaymentSignature.from_header(sdk_model.to_header()).rawTx == raw_tx
    assert sdk.PaymentSignature.from_header(gateway_model.to_header()).txId == TX_ID


def test_payment_response_identical_bytes_both_directions() -> None:
    gateway_model = gw.PaymentResponse(txId=TX_ID, network="kaspa:testnet-10", usdValueAtSettlement="12.50000000")
    sdk_model = sdk.PaymentResponse(txId=TX_ID, network="kaspa:testnet-10", usdValueAtSettlement="12.50000000")

    assert sdk_model.to_header() == gateway_model.to_header()
    parsed = sdk.PaymentResponse.from_header(gateway_model.to_header())
    assert parsed.settled is True and parsed.txId == TX_ID
    assert gw.PaymentResponse.from_header(sdk_model.to_header()).network == "kaspa:testnet-10"


def test_header_alphabet_is_standard_padded_base64() -> None:
    header = gw.PaymentRequired.for_endpoint(PRICE, MERCHANT).to_header()
    assert header.isascii() and "-" not in header and "_" not in header
    assert len(header) % 4 == 0


def test_sdk_rejects_what_the_gateway_rejects() -> None:
    for bad in ("", "not base64!!", "e30=", "bnVsbA=="):
        with pytest.raises(ValueError):
            sdk.PaymentRequired.from_header(bad)
        with pytest.raises(ValueError):
            gw.PaymentRequired.from_header(bad)
    with pytest.raises(ValueError):
        sdk.PaymentRequired.from_header("A" * (gw.MAX_HEADER_BYTES + 4))


def test_unknown_fields_are_rejected_like_the_gateway() -> None:
    import base64

    payload = json.loads(gw.PaymentRequired.for_endpoint(PRICE, MERCHANT).model_dump_json())
    payload["extra"] = 1
    header = base64.b64encode(json.dumps(payload).encode()).decode()
    with pytest.raises(ValueError):
        sdk.PaymentRequired.from_header(header)
    with pytest.raises(ValueError):
        gw.PaymentRequired.from_header(header)


def test_signature_rejects_mainnet_payer_like_the_gateway() -> None:
    mainnet_payer = "kaspa:" + PAYER.split(":", 1)[1]
    raw_tx = "{}"
    sdk_model = sdk.PaymentSignature(txId=TX_ID, rawTx=raw_tx, payerAddress=mainnet_payer)
    with pytest.raises(ValueError):
        gw.PaymentSignature.from_header(sdk_model.to_header())


@pytest.mark.parametrize("total", [2, 99, 100, 101, 6_666, 6_667, 10_000, 9_999_999, 1_333_333_267, 2**62])
def test_fee_math_matches_gateway(total: int) -> None:
    merchant, fee = sdk.compute_fee_split(total)
    gateway_split = gw.compute_fee_split(total, min_output_sompi=1)
    assert (merchant, fee) == (gateway_split.merchant_sompi, gateway_split.fee_sompi)
    assert merchant + fee == total
    assert fee * 10_000 >= total * 150 > (fee - 1) * 10_000


def test_fee_math_random_sweep_matches_gateway() -> None:
    rng = random.Random(402)
    for _ in range(5_000):
        total = rng.randint(1, 10**15)
        merchant, fee = sdk.compute_fee_split(total)
        split = gw.compute_fee_split(total, min_output_sompi=1)
        assert (merchant, fee) == (split.merchant_sompi, split.fee_sompi)


def test_minimum_price_boundary_agrees_with_gateway_floor() -> None:
    floor = gw.min_price_sompi(20_000_000)
    assert floor == 1_333_333_267 == gw.min_price_sompi()
    merchant, fee = sdk.compute_fee_split(floor)
    assert merchant >= 20_000_000 and fee >= 20_000_000
    assert sdk.DEFAULT_MIN_OUTPUT_SOMPI == 20_000_000
    with pytest.raises(ValueError):
        gw.compute_fee_split(floor - 1, min_output_sompi=20_000_000)


def test_gateway_accepts_every_challenge_the_sdk_validates() -> None:
    header = challenge(PRICE, pay_to=MERCHANT, fee_to=TREASURY).to_header()
    assert gw.PaymentRequired.from_header(header).amount == str(PRICE)
    other = challenge(PRICE, pay_to=OTHER, fee_to=TREASURY).to_header()
    assert gw.PaymentRequired.from_header(other).payTo == OTHER


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", 100_000_000), ("6.66666601", 666_666_601), (0.5, 50_000_000), ("0.00000001", 1)],
)
def test_kas_to_sompi_exact(value: object, expected: int) -> None:
    assert sdk.kas_to_sompi(value) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["0", "-1", "0.000000001", "abc", "nan", "inf", 0])
def test_kas_to_sompi_rejects_bad_values(value: object) -> None:
    with pytest.raises(ValueError):
        sdk.kas_to_sompi(value)  # type: ignore[arg-type]
