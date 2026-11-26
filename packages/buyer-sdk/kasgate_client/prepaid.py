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
HEADER_BALANCE_TIMESTAMP = "X-KasGate-Balance-Timestamp"
HEADER_BALANCE_SIGNATURE = "X-KasGate-Balance-Signature"
PREPAID_SCHEME = "kasgate-prepaid-v1"
PREPAID_DEBIT_PURPOSE = "kasgate:v1:prepaid-debit"
PREPAID_BALANCE_READ_PURPOSE = "kasgate:v1:prepaid-balance-read"
PREPAID_REFUNDS_READ_PURPOSE = "kasgate:v1:refunds-status-read"
PREPAID_EMPTY_BODY_HASH = "-"
PREPAID_RECEIPT_EXTENSION = "kasgate_prepaid_receipt"
DEPOSIT_QUOTE_PATH = "/v1/prepaid/deposits/quote"
DEPOSIT_PATH = "/v1/prepaid/deposits"
CHALLENGE_PATH = "/v1/prepaid/challenge"
BALANCE_PATH = "/v1/prepaid/balance"
# CONTRACT v2.13 refunds of unused credit (paid back on-chain by the merchant, no platform fee).
PREPAID_REFUND_PURPOSE = "kasgate:v1:prepaid-refund"
REFUND_CHALLENGE_PATH = "/v1/prepaid/refunds/challenge"
REFUNDS_PATH = "/v1/prepaid/refunds"
REFUND_STATUS_PATH = "/v1/prepaid/refunds/status"

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
    """`GET /v1/prepaid/balance`. The unsigned public read returns every field, with only `spendable_sompi` and
    `expires_at` set and the rest null (including `merchant_id`, `payer_address`, `balance_sompi` and `updated_at`).
    A buyer-signed read (X-KasGate-Balance-Timestamp + X-KasGate-Balance-Signature, which
    `KasGateClient.prepaid_balance` sends by default) fills the rest. Every field other than spendable/expiry is
    optional so that shape parses; read `spendable_sompi` for what calls can pay, never `balance_sompi`."""

    merchant_id: uuid.UUID | None = None
    payer_address: str | None = None
    balance_sompi: BalanceStr | None = None
    """Signed reads only."""
    deposited_sompi: BalanceStr | None = None
    debited_sompi: BalanceStr | None = None
    updated_at: datetime | None = None
    """Signed reads only."""
    refunded_sompi: BalanceStr | None = None
    expired_sompi: BalanceStr | None = None
    refund_hold_sompi: BalanceStr | None = None
    spendable_sompi: BalanceStr | None = None
    """What calls can be paid from now (balance minus an open refund's hold, 0 once expired). Public."""
    expires_at: datetime | None = None
    # CONTRACT v2.14 (absent from older gateways, hence None): cumulative failed-delivery credit at this merchant.
    credited_sompi: BalanceStr | None = None


RefundAction = Literal["request", "cancel", "status"]
RefundStatus = Literal[
    "requested", "paying", "payout_submitted", "paid", "rejected", "cancelled"
]


class PrepaidRefundChallenge(_Model):
    nonce: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    message: str
    action: RefundAction
    slug: str
    merchant_id: uuid.UUID
    payer_address: str
    amount_sompi: BalanceStr
    """The refund amount; "0" for a `status` challenge. The unsigned challenge does not report spendable or refundable totals."""
    refund_id: uuid.UUID | None = None
    expires_at: datetime


class PrepaidRefund(_Model):
    """A refund request of unused prepaid credit. `paid` means the merchant's payout `tx_id` was accepted on-chain.

    `paying`: the merchant claimed it and is paying (it can no longer be cancelled until `claim_expires_at`). The
    GET list is buyer-signed (same headers as the balance read, Purpose `kasgate:v1:refunds-status-read`) and still
    hides `reason`, `last_error` and the claim (`paying` reads as `requested`, claim times null);
    `prepaid_refunds(..., signed=True)` (a POST status challenge) returns them. `hold_until`: a refund cancelled after a claim keeps its
    amount set aside until then (a payout may still land). `late_paid_from` is set when the payout landed after the
    refund was cancelled or rejected (`shortfall_sompi`: credit already gone by then)."""

    id: uuid.UUID
    merchant_id: uuid.UUID
    payer_address: str
    amount_sompi: PositiveSompiStr
    status: RefundStatus
    tx_id: str | None = None
    reason: str | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime
    payout_submitted_at: datetime | None = None
    paid_at: datetime | None = None
    resolved_at: datetime | None = None
    payout_address: str
    payout_payload_hex: str
    claimed_at: datetime | None = None
    first_claimed_at: datetime | None = None
    claim_expires_at: datetime | None = None
    hold_until: datetime | None = None
    late_paid_from: Literal["cancelled", "rejected"] | None = None
    shortfall_sompi: BalanceStr = "0"


def build_prepaid_signed_read_message(
    *, domain: str, address: str, slug: str, timestamp: int, purpose: str
) -> str:
    """The timestamped read text (domain, address, slug, unix seconds) used by GET /v1/prepaid/balance and
    GET /v1/prepaid/refunds/status. Only `purpose` differs between the two, so a signature cannot be reused
    across them."""
    return (
        "KasGate\n"
        f"Domain: {domain}\n"
        f"Purpose: {purpose}\n"
        f"Address: {address}\n"
        f"Slug: {slug}\n"
        f"Timestamp: {timestamp}"
    )


def build_prepaid_balance_read_message(
    *, domain: str, address: str, slug: str, timestamp: int
) -> str:
    """The text a buyer signs for a signed read of its own prepaid balance (byte-identical to the gateway's
    `build_prepaid_balance_read_message` at fix/earlier-features a96a16a). Purpose
    `kasgate:v1:prepaid-balance-read`; the signature authorises nothing but this read."""
    return build_prepaid_signed_read_message(
        domain=domain,
        address=address,
        slug=slug,
        timestamp=timestamp,
        purpose=PREPAID_BALANCE_READ_PURPOSE,
    )


def build_prepaid_refunds_read_message(
    *, domain: str, address: str, slug: str, timestamp: int
) -> str:
    """The text a buyer signs for GET /v1/prepaid/refunds/status (byte-identical to the gateway's
    `build_prepaid_refunds_read_message` at a96a16a). Same shape as the balance-read text; only the Purpose
    (`kasgate:v1:refunds-status-read`) differs."""
    return build_prepaid_signed_read_message(
        domain=domain,
        address=address,
        slug=slug,
        timestamp=timestamp,
        purpose=PREPAID_REFUNDS_READ_PURPOSE,
    )


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


def build_prepaid_refund_message(
    *,
    domain: str,
    action: str,
    address: str,
    merchant_id: uuid.UUID,
    amount_sompi: int,
    refund_id: uuid.UUID | None,
    nonce: str,
    expires_at: datetime,
) -> str:
    """The exact text signed to open or cancel one refund, or to read one's own refunds (`status`, amount 0);
    identical to the gateway's builder (CONTRACT v2.13)."""
    return (
        "KasGate\n"
        f"Domain: {domain}\n"
        f"Purpose: {PREPAID_REFUND_PURPOSE}\n"
        f"Action: {action}\n"
        f"Address: {address}\n"
        f"Merchant: {merchant_id}\n"
        f"Amount: {amount_sompi}\n"
        f"Refund: {refund_id if refund_id is not None else '-'}\n"
        f"Nonce: {nonce}\n"
        f"Expires: {expires_at.isoformat()}"
    )


def validate_refund_challenge(
    challenge: PrepaidRefundChallenge,
    *,
    domain: str,
    address: str,
    slug: str,
    action: RefundAction,
    amount_sompi: int | None,
    refund_id: uuid.UUID | None,
    now: datetime | None = None,
) -> int:
    """Check a refund challenge against what the SDK asked for; return the amount it names.

    A refund signature moves no coins, but the SDK still signs only text it rebuilt itself: this payer, this
    domain, the requested action and refund, and the exact amount asked for (or, when no amount was given, the
    amount the challenge names — the gateway's refundable default). Spendable and refundable totals are not on the
    unsigned challenge; the gateway enforces the refundable cap when the request is submitted. Raises
    InvalidChallengeError; nothing is signed when it raises.
    """
    amount = int(challenge.amount_sompi)
    if (
        challenge.payer_address,
        challenge.slug,
        challenge.action,
        challenge.refund_id,
    ) != (
        address,
        slug,
        action,
        refund_id,
    ):
        raise InvalidChallengeError(
            "refund challenge is for another payer, endpoint, action or refund"
        )
    if action == "request":
        if amount_sompi is not None and amount != amount_sompi:
            raise InvalidChallengeError(
                f"refund challenge names {amount} sompi, not the {amount_sompi} requested"
            )
        if amount <= 0:
            raise InvalidChallengeError("refund challenge names no amount")
    if action == "status" and amount != 0:
        raise InvalidChallengeError("a status challenge must name amount 0")
    if action == "cancel" and amount <= 0:
        raise InvalidChallengeError("refund challenge names no amount")
    if challenge.expires_at.tzinfo is None or challenge.expires_at <= (
        now or datetime.now(UTC)
    ):
        raise InvalidChallengeError("refund challenge has already expired")
    expected = build_prepaid_refund_message(
        domain=domain,
        action=action,
        address=address,
        merchant_id=challenge.merchant_id,
        amount_sompi=amount,
        refund_id=refund_id,
        nonce=challenge.nonce,
        expires_at=challenge.expires_at,
    )
    if challenge.message != expected:
        raise InvalidChallengeError(
            "refund challenge text does not match the request; refusing to sign it"
        )
    return amount


def slug_of(path: str) -> str:
    """The endpoint slug of a /p/{slug}[/...][?...] proxy path."""
    match = _PROXY_PATH_RE.fullmatch(path)
    if match is None:
        raise InvalidChallengeError(
            f"{path!r} is not a KasGate proxy path (/p/<slug>...)"
        )
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
    if (
        challenge.payer_address,
        challenge.method,
        challenge.path,
        challenge.body_hash,
    ) != (
        address,
        method,
        path,
        body_hash,
    ):
        raise InvalidChallengeError(
            "prepaid challenge is for another payer, method, path or body"
        )
    if challenge.expires_at.tzinfo is None or challenge.expires_at <= (
        now or datetime.now(UTC)
    ):
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
        raise InvalidChallengeError(
            "prepaid challenge text does not match the request; refusing to sign it"
        )
    return amount
