"""Unit tests for USD micro-dollar -> sompi conversion, ceil rounding, the min-price floor, and the fee split."""

from decimal import Decimal

import pytest

from app.schemas import (
    MAX_SOMPI,
    compute_fee_split,
    min_price_sompi,
    usd_micros_to_sompi,
    usd_micros_to_usd,
)


def test_usd_micros_to_usd_string() -> None:
    assert usd_micros_to_usd(1) == "0.000001"
    assert usd_micros_to_usd(1_000_000) == "1"
    assert usd_micros_to_usd(1_250_000) == "1.25"
    assert usd_micros_to_usd(99) == "0.000099"


def test_exact_sompi_conversion_does_not_ceil() -> None:
    # $1 at $0.10 / KAS is exactly 10 KAS = 1_000_000_000 sompi
    assert usd_micros_to_sompi(1_000_000, Decimal("0.1")) == 1_000_000_000


def test_conversion_ceils_fractional_sompi() -> None:
    # $1 at $0.3/KAS = 3.333... KAS = 333_333_333.333... sompi, always ceiled
    assert usd_micros_to_sompi(1_000_000, Decimal("0.3")) == 333_333_334
    # $2 at $0.3/KAS = 6.666... KAS = 666_666_666.666... sompi
    assert usd_micros_to_sompi(2_000_000, Decimal("0.3")) == 666_666_667


def test_converted_price_below_the_floor_is_raised_to_min_price() -> None:
    floor = min_price_sompi()
    # 1 micro-dollar at $0.125/KAS is 800 sompi, far below 0.6 KAS
    assert usd_micros_to_sompi(1, Decimal("0.125")) == floor
    # just under the floor at this rate: $0.074999 -> 59_999_200 sompi, raised
    assert usd_micros_to_sompi(74_999, Decimal("0.125")) == floor
    # exactly 0.6 KAS: $0.075 at $0.125/KAS
    assert usd_micros_to_sompi(75_000, Decimal("0.125")) == floor


def test_floored_price_has_a_valid_fee_split() -> None:
    sompi = usd_micros_to_sompi(1, Decimal("0.125"))
    split = compute_fee_split(sompi)
    assert split.total_sompi == min_price_sompi() == 60_000_000
    assert split.fee_sompi == 20_000_000
    assert split.merchant_sompi == 40_000_000


def test_conversion_then_fee_split_above_the_floor() -> None:
    # $1 at $0.125/KAS = 8 KAS = 800_000_000 sompi; fee is the 0.2 KAS floor (1.5% would be 0.12 KAS)
    sompi = usd_micros_to_sompi(1_000_000, Decimal("0.12500000"))
    assert sompi == 800_000_000
    split = compute_fee_split(sompi)
    assert split.fee_sompi == 20_000_000
    assert split.merchant_sompi == 780_000_000


@pytest.mark.parametrize(
    "rate",
    [Decimal("0"), Decimal("-0.1"), Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")],
)
def test_invalid_rates_are_rejected(rate: Decimal) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        usd_micros_to_sompi(1_000_000, rate)


def test_non_decimal_rate_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive finite"):
        usd_micros_to_sompi(1_000_000, 0.125)  # type: ignore[arg-type]


def test_non_positive_micros_are_rejected() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        usd_micros_to_sompi(0, Decimal("0.1"))
    with pytest.raises(ValueError, match="must be positive"):
        usd_micros_to_sompi(-1, Decimal("0.1"))


def test_overflow_beyond_max_sompi_is_rejected() -> None:
    with pytest.raises(ValueError, match="MAX_SOMPI"):
        usd_micros_to_sompi(1_000_000_000_000_000, Decimal("0.00000001"))
    assert MAX_SOMPI == 999_999_999_999_999_999
