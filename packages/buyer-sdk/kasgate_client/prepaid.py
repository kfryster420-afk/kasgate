"""CONTRACT v2.11 prepaid credit: models, the debit message and the checks the SDK runs before signing anything.

The deposit is an ordinary kaspa-exact-v1 split payment (output 0 merchant, output 1 treasury, fee
max(ceil(1.5 %), 0.2 KAS), at least 0.6 KAS); the merchant share becomes credit at that merchant. Each later call
signs ONE debit challenge from POST /v1/prepaid/challenge with the depositing key. The SDK never signs a challenge
text it did not rebuild itself from its own request (domain, address, method, path, body hash) and the cap.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .exceptions import InvalidChallengeError, PriceCapExceededError
from .protocol import PaymentRequired, X402Header

HEADER_PREPAID_DEBIT = "X-KasGate-Prepaid"
HEADER_PREPAID_RECEIPT = "X-KasGate-Prepaid-Receipt"
PREPAID_SCHEME = "kasgate-prepaid-v1"
PREPAID_DEBIT_PURPOSE = "kasgate:v1:prepaid-debit"
PREPAID_EMPTY_BODY_HASH = "-"
PREPAID_RECEIPT_EXTENSION = "kasgate_prepaid_receipt"
DEPOSIT_QUOTE_PATH = "/v1/prepaid/deposits/quote"
DEPOSIT_PATH = "/v1/prepaid/deposits"
CHALLENGE_PATH = "/v1/prepaid/challenge"
BALANCE_PATH = "/v1/prepaid/balance"

_BALANCE_RE = r"^(?:0|[1-9][0-9]{0,17})$"
_SOMPI_RE = r"^[1-9][0-9]{0,17}$"
_PROXY_PATH_RE = re.compile(r"^/p/([a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9]))(?:[/?].*)?$")

BalanceStr = Annotated[str, Field(pattern=_BALANCE_RE)]
PositiveSompiStr = Annotated[str, Field(pattern=_SOMPI_RE)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class PrepaidDepositQuote(_Model):
    slug: str
    merchant_id: uuid.UUID
    payer_address: str
    deposit_sompi: PositiveSompiStr
    fee_sompi: PositiveSompiStr
    credit_sompi: PositiveSompiStr
    balance_sompi: BalanceStr
    required: PaymentRequired
    deposit_url: str


class PrepaidDepositReceipt(_Model):
    deposit_id: uuid.UUID
    tx_id: str
    slug: str
    merchant_id: uuid.UUID
    payer_address: str
    deposit_sompi: PositiveSompiStr
    fee_sompi: PositiveSompiStr
    credit_sompi: PositiveSompiStr
    balance_sompi: BalanceStr
    created_at: datetime


class PrepaidChallenge(_Model):
    nonce: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    message: str
    slug: str
    merchant_id: uuid.UUID
    payer_address: str
    method: str
    path: str
    body_hash: str
    amount_sompi: PositiveSompiStr
    kas_usd_rate: str | None = None
    balance_sompi: BalanceStr
    expires_at: datetime


class PrepaidBalance(_Model):
    merchant_id: uuid.UUID
    payer_address: str
    balance_sompi: BalanceStr
    deposited_sompi: BalanceStr
    debited_sompi: BalanceStr
    updated_at: datetime | None = None


class PrepaidDebitHeader(X402Header):
    """Body of the X-KasGate-Prepaid request header (byte-compatible with the gateway model)."""

    nonce: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    signature: Annotated[str, Field(pattern=r"^[0-9a-f]{128}$")]


class PrepaidReceipt(X402Header):
    """Body of the X-KasGate-Prepaid-Receipt response header."""

    debitId: uuid.UUID
    nonce: str
    amountSompi: PositiveSompiStr
    balanceSompi: BalanceStr
    status: Literal["debited", "refunded"]


def prepaid_body_hash(body: bytes) -> str:
    """SHA-256 hex of the raw request body, or "-" when there is none (gateway `prepaid_body_hash`)."""
    return hashlib.sha256(body).hexdigest() if body else PREPAID_EMPTY_BODY_HASH


def build_prepaid_debit_message(
    *,
    domain: str,
    address: str,
    merchant_id: uuid.UUID,
    method: str,
    path: str,
    body_hash: str,
    amount_sompi: int,
    nonce: str,
    expires_at: datetime,
) -> str:
    """The exact text signed for one debit; identical to the gateway's `build_prepaid_debit_message`."""
    return (
        "KasGate\n"
        f"Domain: {domain}\n"
        f"Purpose: {PREPAID_DEBIT_PURPOSE}\n"
        f"Address: {address}\n"
        f"Merchant: {merchant_id}\n"
        f"Method: {method}\n"
        f"Path: {path}\n"
        f"Body-Hash: {body_hash}\n"
        f"Amount: {amount_sompi}\n"
        f"Nonce: {nonce}\n"
        f"Expires: {expires_at.isoformat()}"
    )


def slug_of(path: str) -> str:
    """The endpoint slug of a /p/{slug}[/...][?...] proxy path."""
    match = _PROXY_PATH_RE.fullmatch(path)
    if match is None:
        raise InvalidChallengeError(f"{path!r} is not a KasGate proxy path (/p/<slug>...)")
    return match.group(1)


def validate_debit_challenge(
    challenge: PrepaidChallenge,
    *,
    domain: str,
    address: str,
    method: str,
    path: str,
    body_hash: str,
    cap_sompi: int,
    now: datetime | None = None,
) -> int:
    """Check a debit challenge against the request the SDK is about to send; return the amount in sompi.

    The message is rebuilt locally and must equal the server's text byte for byte, so a gateway can never get a
    signature over another path, body, payer, domain or a larger amount than the cap. Raises InvalidChallengeError
    or PriceCapExceededError; nothing is signed when it raises.
    """
    amount = int(challenge.amount_sompi)
    if amount > cap_sompi:
        raise PriceCapExceededError(amount, cap_sompi)
    if (challenge.payer_address, challenge.method, challenge.path, challenge.body_hash) != (
        address,
        method,
        path,
        body_hash,
    ):
        raise InvalidChallengeError("prepaid challenge is for another payer, method, path or body")
    if challenge.expires_at.tzinfo is None or challenge.expires_at <= (now or datetime.now(UTC)):
        raise InvalidChallengeError("prepaid challenge has already expired")
    expected = build_prepaid_debit_message(
        domain=domain,
        address=address,
        merchant_id=challenge.merchant_id,
        method=method,
        path=path,
        body_hash=body_hash,
        amount_sompi=amount,
        nonce=challenge.nonce,
        expires_at=challenge.expires_at,
    )
    if challenge.message != expected:
        raise InvalidChallengeError("prepaid challenge text does not match the request; refusing to sign it")
    return amount
