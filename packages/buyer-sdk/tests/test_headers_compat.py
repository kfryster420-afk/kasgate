"""Byte-compatibility of the SDK header models with apps/gateway/app/schemas.py."""

from __future__ import annotations

import json
import random

import pytest
from app import schemas as gw
from conftest import (
    FLOOR,
    MERCHANT,
    MIN_PRICE,
    NONCE,
    OTHER,
    PAYER,
    PRICE,
    TREASURY,
    challenge,
    future,
)

import kasgate_client as sdk

TX_ID = "ab" * 32


@pytest.mark.parametrize(
    "price",
    [
        MIN_PRICE,
        1_333_333_333,
        1_333_333_334,
        1_333_333_335,
        PRICE,
        2_000_000_000,
        123_456_789_012,
    ],
)
def test_payment_required_identical_bytes_and_cross_decode(price: int) -> None:
    gateway_model = gw.PaymentRequired.for_endpoint(price, MERCHANT, NONCE, future())
    header = gateway_model.to_header()

    decoded = sdk.PaymentRequired.from_header(header)
    assert decoded.total_sompi == price
    assert decoded.merchant_sompi == gateway_model.merchant_amount_sompi
    assert decoded.fee_sompi == gateway_model.fee_amount_sompi
    assert decoded.payTo == MERCHANT and decoded.platformFee.feeTo == TREASURY
    assert decoded.nonce == NONCE and decoded.expiresAt == gateway_model.expiresAt

    assert decoded.to_header() == header
    assert gw.PaymentRequired.from_header(decoded.to_header()) == gateway_model


PROOF = "ab" * 64


def test_payment_signature_identical_bytes_both_directions() -> None:
    raw_tx = json.dumps({"id": TX_ID, "inputs": [], "outputs": []})
    sdk_model = sdk.PaymentSignature(
        txId=TX_ID, rawTx=raw_tx, payerAddress=PAYER, nonce=NONCE, payerProof=PROOF
    )
    gateway_model = gw.PaymentSignature(
        txId=TX_ID, rawTx=raw_tx, payerAddress=PAYER, nonce=NONCE, payerProof=PROOF
    )

    assert sdk_model.to_header() == gateway_model.to_header()
    assert gw.PaymentSignature.from_header(sdk_model.to_header()).rawTx == raw_tx
    assert sdk.PaymentSignature.from_header(gateway_model.to_header()).txId == TX_ID
    assert sdk.PaymentSignature.from_header(gateway_model.to_header()).nonce == NONCE
    assert (
        sdk.PaymentSignature.from_header(gateway_model.to_header()).payerProof == PROOF
    )
    assert gw.PaymentSignature.from_header(sdk_model.to_header()).payerProof == PROOF


def test_payment_response_identical_bytes_both_directions() -> None:
    gateway_model = gw.PaymentResponse(
        txId=TX_ID, network="kaspa:testnet-10", usdValueAtSettlement="12.50000000"
    )
    sdk_model = sdk.PaymentResponse(
        txId=TX_ID, network="kaspa:testnet-10", usdValueAtSettlement="12.50000000"
    )

    assert sdk_model.to_header() == gateway_model.to_header()
    parsed = sdk.PaymentResponse.from_header(gateway_model.to_header())
    assert parsed.settled is True and parsed.txId == TX_ID
    assert (
        gw.PaymentResponse.from_header(sdk_model.to_header()).network
        == "kaspa:testnet-10"
    )


def test_header_alphabet_is_standard_padded_base64() -> None:
    header = gw.PaymentRequired.for_endpoint(
        PRICE, MERCHANT, NONCE, future()
    ).to_header()
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

    payload = json.loads(
        gw.PaymentRequired.for_endpoint(
            PRICE, MERCHANT, NONCE, future()
        ).model_dump_json()
    )
    payload["extra"] = 1
    header = base64.b64encode(json.dumps(payload).encode()).decode()
    with pytest.raises(ValueError):
        sdk.PaymentRequired.from_header(header)
    with pytest.raises(ValueError):
        gw.PaymentRequired.from_header(header)


def test_signature_rejects_mainnet_payer_like_the_gateway() -> None:
    mainnet_payer = "kaspa:" + PAYER.split(":", 1)[1]
    raw_tx = "{}"
    sdk_model = sdk.PaymentSignature(
        txId=TX_ID,
        rawTx=raw_tx,
        payerAddress=mainnet_payer,
        nonce=NONCE,
        payerProof=PROOF,
    )
    with pytest.raises(ValueError):
        gw.PaymentSignature.from_header(sdk_model.to_header())


@pytest.mark.parametrize(
    "total",
    [
        MIN_PRICE,
        MIN_PRICE + 1,
        100_000_000,
        1_333_333_332,
        1_333_333_333,
        1_333_333_334,
        1_333_333_335,
        2**62,
    ],
)
def test_fee_math_matches_gateway(total: int) -> None:
    merchant, fee = sdk.compute_fee_split(total, FLOOR)
    gateway_split = gw.compute_fee_split(total, min_output_sompi=FLOOR)
    assert (merchant, fee) == (gateway_split.merchant_sompi, gateway_split.fee_sompi)
    assert merchant + fee == total
    assert merchant >= 2 * FLOOR and fee >= FLOOR
    assert fee * 10_000 >= total * 150  # fee never drops below the 1.5% share
    assert fee == max(-(-total * 150 // 10_000), FLOOR)


def test_fee_math_random_sweep_matches_gateway() -> None:
    rng = random.Random(402)
    for _ in range(5_000):
        total = rng.randint(MIN_PRICE, 10**15)
        floor = rng.choice([1, 1_000, FLOOR, 50_000_000])
        try:
            expected = gw.compute_fee_split(total, min_output_sompi=floor)
        except ValueError:
            with pytest.raises(ValueError):
                sdk.compute_fee_split(total, floor)
            continue
        assert sdk.compute_fee_split(total, floor) == (
            expected.merchant_sompi,
            expected.fee_sompi,
        )


def test_below_minimum_price_is_refused_by_both_sides() -> None:
    for total in (
        1,
        20_000_000,
        39_999_999,
        40_000_000,
        59_999_998,
        59_999_999,
        MIN_PRICE - 1,
    ):
        with pytest.raises(ValueError):
            sdk.compute_fee_split(total, FLOOR)
        with pytest.raises(ValueError):
            gw.compute_fee_split(total, min_output_sompi=FLOOR)
    with pytest.raises(ValueError):
        sdk.compute_fee_split(0)


def test_exact_minimum_price_splits_40_20() -> None:
    """CONTRACT v2.3: 60_000_000 sompi -> treasury 20_000_000 (flat floor), merchant 40_000_000 (= 2 * floor)."""
    assert (
        gw.min_price_sompi(FLOOR)
        == MIN_PRICE
        == 60_000_000
        == sdk.min_price_sompi(FLOOR)
        == 3 * FLOOR
    )
    assert sdk.compute_fee_split(MIN_PRICE, FLOOR) == (40_000_000, 20_000_000)
    split = gw.compute_fee_split(MIN_PRICE, min_output_sompi=FLOOR)
    assert (split.merchant_sompi, split.fee_sompi) == (40_000_000, 20_000_000)
    assert sdk.DEFAULT_MIN_OUTPUT_SOMPI == 20_000_000


def test_v2_1_minimum_price_of_40_000_000_is_now_refused_by_both_sides() -> None:
    for total in (39_999_999, 40_000_000, 59_999_999):
        with pytest.raises(ValueError):
            sdk.compute_fee_split(total, FLOOR)
        with pytest.raises(ValueError):
            gw.compute_fee_split(total, min_output_sompi=FLOOR)


@pytest.mark.parametrize(
    ("total", "merchant", "fee"),
    [
        (
            1_333_333_332,
            1_313_333_332,
            20_000_000,
        ),  # 1.5% = 19_999_999.98 -> ceil 20_000_000: floor == percentage
        (
            1_333_333_333,
            1_313_333_333,
            20_000_000,
        ),  # 1.5% = 19_999_999.995 -> ceil 20_000_000: last flat-fee price
        (
            1_333_333_334,
            1_313_333_333,
            20_000_001,
        ),  # 1.5% = 20_000_000.01 -> ceil 20_000_001: first percentage fee
        (1_333_333_335, 1_313_333_334, 20_000_001),
    ],
)
def test_crossover_boundary_matches_gateway(
    total: int, merchant: int, fee: int
) -> None:
    assert sdk.compute_fee_split(total, FLOOR) == (merchant, fee)
    split = gw.compute_fee_split(total, min_output_sompi=FLOOR)
    assert (split.merchant_sompi, split.fee_sompi) == (merchant, fee)
    header = gw.PaymentRequired.for_endpoint(
        total, MERCHANT, NONCE, future()
    ).to_header()
    assert sdk.PaymentRequired.from_header(header).fee_sompi == fee


def test_flat_fee_regime_ends_exactly_at_1_333_333_334() -> None:
    last_flat = max(
        t
        for t in range(1_333_333_300, 1_333_333_400)
        if sdk.compute_fee_split(t, FLOOR)[1] == FLOOR
    )
    assert last_flat == 1_333_333_333
    assert sdk.compute_fee_split(last_flat + 1, FLOOR)[1] == FLOOR + 1


def test_gateway_and_sdk_floor_default_agree() -> None:
    from app.config import DEFAULT_MIN_OUTPUT_SOMPI as gateway_default

    assert gateway_default == sdk.DEFAULT_MIN_OUTPUT_SOMPI == FLOOR


def test_payment_payload_matches_gateway() -> None:
    assert (
        sdk.payment_payload(NONCE)
        == gw.payment_payload(NONCE)
        == b"kasgate:v1:" + NONCE.encode()
    )


def test_limits_match_gateway_v2() -> None:
    from kasgate_client import constants

    assert constants.GATEWAY_MAX_HEADER_BYTES == gw.MAX_HEADER_BYTES == 40_960
    assert constants.GATEWAY_MAX_RAW_TX_CHARS == gw.MAX_RAW_TX_CHARS == 24_000
    assert constants.PAYMENT_PAYLOAD_PREFIX == gw.PAYMENT_PAYLOAD_PREFIX
    assert constants.DEFAULT_MAX_SIGNATURE_HEADER_BYTES <= gw.MAX_HEADER_BYTES


def test_gateway_enforces_the_flat_fee_so_old_percentage_only_challenges_are_invalid() -> (
    None
):
    old_fee = -(
        -MIN_PRICE * 150 // 10_000
    )  # 900_000: what v2 (pre-2.1) would have asked for
    forged = challenge(MIN_PRICE, fee_amount=old_fee)
    with pytest.raises(ValueError):
        gw.PaymentRequired.from_header(forged.to_header())


def test_gateway_accepts_every_challenge_the_sdk_validates() -> None:
    for price in (MIN_PRICE, 1_333_333_334, PRICE):
        header = challenge(price, pay_to=MERCHANT, fee_to=TREASURY).to_header()
        assert gw.PaymentRequired.from_header(header).amount == str(price)
    other = challenge(PRICE, pay_to=OTHER, fee_to=TREASURY).to_header()
    assert gw.PaymentRequired.from_header(other).payTo == OTHER


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", 100_000_000),
        ("6.66666601", 666_666_601),
        (0.5, 50_000_000),
        ("0.00000001", 1),
    ],
)
def test_kas_to_sompi_exact(value: object, expected: int) -> None:
    assert sdk.kas_to_sompi(value) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["0", "-1", "0.000000001", "abc", "nan", "inf", 0])
def test_kas_to_sompi_rejects_bad_values(value: object) -> None:
    with pytest.raises(ValueError):
        sdk.kas_to_sompi(value)  # type: ignore[arg-type]


# ---------------------------------------------------------------- CONTRACT v2.6: payerProof


@pytest.mark.parametrize("nonce", [NONCE, "00" * 16, "ff" * 16])
def test_payer_proof_message_matches_the_gateway_byte_for_byte(nonce: str) -> None:
    for tx_id in (TX_ID, "00" * 32, "0f" * 32):
        assert sdk.payer_proof_message(nonce, tx_id) == gw.payer_proof_message(
            nonce, tx_id
        )
        assert sdk.payer_proof_message(nonce, tx_id) == f"kasgate:v1:{nonce}:{tx_id}"


@pytest.mark.parametrize(
    "bad", ["", "ab" * 63, "ab" * 65, "zz" * 64, "AB" * 63 + "A", None, 5]
)
def test_proof_values_rejected_by_the_gateway_are_rejected_by_the_sdk(bad) -> None:
    fields = {"txId": TX_ID, "rawTx": "{}", "payerAddress": PAYER, "nonce": NONCE}
    with pytest.raises(ValueError):
        gw.PaymentSignature(payerProof=bad, **fields)
    with pytest.raises(ValueError):
        sdk.PaymentSignature(payerProof=bad, **fields)


def test_proof_is_required() -> None:
    fields = {"txId": TX_ID, "rawTx": "{}", "payerAddress": PAYER, "nonce": NONCE}
    with pytest.raises(ValueError):
        gw.PaymentSignature(**fields)
    with pytest.raises(ValueError):
        sdk.PaymentSignature(**fields)


def test_proof_encodings_normalise_like_the_gateway() -> None:
    import base64

    raw = bytes(range(64))
    fields = {"txId": TX_ID, "rawTx": "{}", "payerAddress": PAYER, "nonce": NONCE}
    for form in (
        raw.hex(),
        raw.hex().upper(),
        base64.b64encode(raw).decode(),
        base64.b64encode(b"\x00" + raw).decode(),
    ):
        assert (
            sdk.PaymentSignature(payerProof=form, **fields).payerProof
            == gw.PaymentSignature(payerProof=form, **fields).payerProof
            == raw.hex()
        )
