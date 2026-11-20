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
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .constants import (
    BPS_DENOMINATOR,
    DEFAULT_MIN_OUTPUT_SOMPI,
    GATEWAY_MAX_HEADER_BYTES,
    GATEWAY_MAX_RAW_TX_CHARS,
    PAYMENT_PAYLOAD_PREFIX,
    PLATFORM_FEE_BPS,
    SOMPI_PER_KAS,
)

KASPA_ADDRESS_RE = re.compile(
    r"^(kaspa|kaspatest):[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{61,63}$"
)
TX_ID_RE = re.compile(r"^[0-9a-f]{64}$")
SOMPI_STR_RE = r"^[1-9][0-9]{0,18}$"
NONCE_RE = r"^[0-9a-f]{32}$"

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


def min_price_sompi(min_output_sompi: int = DEFAULT_MIN_OUTPUT_SOMPI) -> int:
    """Cheapest valid total (CONTRACT v2.3): fee = floor (flat) and merchant = 2 * floor, i.e. 3 * floor.

    Two floor-sized outputs already use the whole 100_000 standard storage mass, so a payment that must also carry
    a change output needs a merchant output of at least 2 * floor.
    """
    return 3 * min_output_sompi


def compute_fee_split(
    total_sompi: int, min_output_sompi: int = DEFAULT_MIN_OUTPUT_SOMPI
) -> tuple[int, int]:
    """Return (merchant_sompi, fee_sompi) under CONTRACT v2.1 / v2.3.

    fee = max(ceil(total * 150 / 10_000), min_output_sompi); merchant = total - fee. Raises ValueError when the
    merchant output would fall below 2 * min_output_sompi (total < 3 * min_output_sompi).
    """
    if total_sompi <= 0:
        raise ValueError("total_sompi must be positive")
    if min_output_sompi < 1:
        raise ValueError("min_output_sompi must be >= 1")
    percentage_fee = (
        total_sompi * PLATFORM_FEE_BPS + BPS_DENOMINATOR - 1
    ) // BPS_DENOMINATOR
    fee = max(percentage_fee, min_output_sompi)
    merchant = total_sompi - fee
    if merchant < 2 * min_output_sompi:
        raise ValueError(
            f"price {total_sompi} sompi leaves a merchant output below {2 * min_output_sompi} sompi "
            f"(2 * min_output_sompi); minimum price is {min_price_sompi(min_output_sompi)} sompi"
        )
    return merchant, fee


def payment_payload(nonce: str) -> bytes:
    """Exact transaction payload that binds a payment to one 402 challenge (CONTRACT v2): b'kasgate:v1:' + nonce."""
    return f"{PAYMENT_PAYLOAD_PREFIX}{nonce}".encode("ascii")


def payer_proof_message(nonce: str, tx_id: str) -> str:
    """Message the payer signs with the payerAddress key (CONTRACT v2.6): 'kasgate:v1:<nonce>:<txId>'.

    It binds a PAYMENT-SIGNATURE to whoever holds the payer key, so an observer who copies a signed transaction from
    the mempool cannot present it as their own payment.
    """
    return f"{PAYMENT_PAYLOAD_PREFIX}{nonce}:{tx_id}"


def normalise_proof(value: object) -> object:
    """Accept 128-hex Schnorr (any case), or Base64 of 64 bytes / 65 bytes (leading header byte dropped)."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if re.fullmatch(r"[0-9a-fA-F]{128}", text):
        return text.lower()
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return value
    if len(raw) == 65:
        raw = raw[1:]
    return raw.hex() if len(raw) == 64 else value


def kas_to_sompi(value: Decimal | str | float) -> int:
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
    def from_header(cls, value: str):
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
    nonce: Annotated[str, Field(pattern=NONCE_RE)]
    expiresAt: datetime

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
    rawTx: str = Field(min_length=2, max_length=GATEWAY_MAX_RAW_TX_CHARS)
    payerAddress: str
    nonce: Annotated[str, Field(pattern=NONCE_RE)]
    payerProof: Annotated[str, Field(pattern=r"^[0-9a-f]{128}$")]
    """Schnorr `sign_message(payer_proof_message(nonce, txId))` by the payerAddress key (CONTRACT v2.6)."""

    @field_validator("payerProof", mode="before")
    @classmethod
    def _proof(cls, v: object) -> object:
        return normalise_proof(v)

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
