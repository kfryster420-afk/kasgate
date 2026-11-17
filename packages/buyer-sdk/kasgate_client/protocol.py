"""x402 header models, byte-compatible with apps/gateway/app/schemas.py.

Field names, field order and JSON encoding (compact `model_dump_json`, Base64 standard alphabet, padded) match the
gateway so that `Model.to_header()` produces identical bytes on both sides. The challenge model deliberately uses
plain `str`/`int` for version, scheme, network and asset so the client can raise precise errors instead of a generic
validation failure.
"""

from __future__ import annotations

import base64
import binascii
import re
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .constants import (
    BPS_DENOMINATOR,
    GATEWAY_MAX_HEADER_BYTES,
    PLATFORM_FEE_BPS,
    SOMPI_PER_KAS,
)

KASPA_ADDRESS_RE = re.compile(r"^(kaspa|kaspatest):[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{61,63}$")
TX_ID_RE = re.compile(r"^[0-9a-f]{64}$")
SOMPI_STR_RE = r"^[1-9][0-9]{0,18}$"

SompiStr = Annotated[str, Field(pattern=SOMPI_STR_RE)]
UsdDecimal = Annotated[Decimal, Field(ge=0, max_digits=20, decimal_places=8)]


def normalize_address(value: str) -> str:
    value = value.strip().lower()
    if not KASPA_ADDRESS_RE.fullmatch(value):
        raise ValueError("invalid Kaspa address")
    return value


def normalize_tx_id(value: str) -> str:
    value = value.strip().lower()
    if not TX_ID_RE.fullmatch(value):
        raise ValueError("txId must be 64 lowercase hex characters")
    return value


def compute_fee_split(total_sompi: int) -> tuple[int, int]:
    """Return (merchant_sompi, fee_sompi): fee = ceil(total * 150 / 10_000), merchant = total - fee."""
    if total_sompi <= 0:
        raise ValueError("total_sompi must be positive")
    fee = (total_sompi * PLATFORM_FEE_BPS + BPS_DENOMINATOR - 1) // BPS_DENOMINATOR
    return total_sompi - fee, fee


def kas_to_sompi(value: Decimal | str | int | float) -> int:
    """Convert a KAS amount to sompi exactly. Rejects non-positive values and more than 8 decimal places."""
    try:
        dec = Decimal(str(value)) if not isinstance(value, Decimal) else value
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid KAS amount: {value!r}") from exc
    if not dec.is_finite() or dec <= 0:
        raise ValueError("KAS amount must be a positive finite number")
    sompi = dec * SOMPI_PER_KAS
    if sompi != sompi.to_integral_value():
        raise ValueError("KAS amount has more than 8 decimal places")
    return int(sompi)


def sompi_to_kas(sompi: int) -> str:
    return format(Decimal(sompi) / Decimal(SOMPI_PER_KAS), "f")


def encode_header(model: BaseModel) -> str:
    return base64.b64encode(model.model_dump_json().encode("utf-8")).decode("ascii")


def decode_header_bytes(value: str) -> bytes:
    if not value or len(value) > GATEWAY_MAX_HEADER_BYTES:
        raise ValueError("header missing or too large")
    try:
        return base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("header is not valid Base64") from exc


class X402Header(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    def to_header(self) -> str:
        return encode_header(self)

    @classmethod
    def from_header(cls, value: str):  # noqa: ANN206 - returns the concrete subclass
        return cls.model_validate_json(decode_header_bytes(value))


class PlatformFee(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    feeTo: str
    feeAmount: SompiStr

    @field_validator("feeTo")
    @classmethod
    def _fee_to(cls, v: str) -> str:
        return normalize_address(v)


class PaymentRequired(X402Header):
    """Decoded PAYMENT-REQUIRED header. `amount` is the TOTAL the payer sends across both outputs."""

    x402Version: int
    scheme: str
    network: str
    asset: str
    amount: SompiStr
    payTo: str
    platformFee: PlatformFee

    @field_validator("payTo")
    @classmethod
    def _pay_to(cls, v: str) -> str:
        return normalize_address(v)

    @property
    def total_sompi(self) -> int:
        return int(self.amount)

    @property
    def fee_sompi(self) -> int:
        return int(self.platformFee.feeAmount)

    @property
    def merchant_sompi(self) -> int:
        return int(self.amount) - int(self.platformFee.feeAmount)


class PaymentSignature(X402Header):
    """Body of the PAYMENT-SIGNATURE header. `rawTx` is `json.dumps(Transaction.to_dict())` of the signed tx."""

    txId: str
    rawTx: str = Field(min_length=2, max_length=100_000)
    payerAddress: str

    @field_validator("txId")
    @classmethod
    def _tx(cls, v: str) -> str:
        return normalize_tx_id(v)

    @field_validator("payerAddress")
    @classmethod
    def _payer(cls, v: str) -> str:
        return normalize_address(v)


class PaymentResponse(X402Header):
    txId: str
    settled: Literal[True] = True
    network: str
    usdValueAtSettlement: UsdDecimal

    @field_validator("txId")
    @classmethod
    def _tx(cls, v: str) -> str:
        return normalize_tx_id(v)
