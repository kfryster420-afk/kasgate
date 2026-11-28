"""Failed-delivery credit hints (CONTRACT v2.14): the `X-KasGate-Credit` response header, treated as an UNSIGNED hint.

When a paid call fails for good (the upstream answered 5xx or not at all) and redelivery is closed, the gateway turns
the merchant share of that payment into prepaid credit and announces it in `X-KasGate-Credit`
(`Base64(JSON{creditId, kind, txId, merchantId, creditSompi, balanceSompi, reason})`). The header carries no
signature, so the SDK never reports a credit as real from the header alone:

* `credit_of(response)` returns a `CreditObservation` whose status is `hint` (parsed, not checked), `invalid`
  (malformed, oversized or inconsistent with the paid call) or, after the balance check, `confirmed` / `unconfirmed`.
* `KasGateClient.confirm_credit()` (or `auto_confirm_credit=True`) asks the gateway for the buyer's SIGNED credit
  status (`POST /v1/prepaid/credits/challenge`, sign the text with the depositing key, `POST /v1/prepaid/credits/status`,
  one fresh challenge per page) and only then says `confirmed`: the status must belong to the hinted merchant and list
  a credit whose `credit_id` is the hinted `creditId`, with the hinted `txId`, amount and reason (at the slug that was
  called). Only when the paged walk could not see every credit (page budget spent, or `credit_count` still above what
  was listed) does a caller-supplied `baseline_credited_sompi` allow a weaker `increase` confirmation.

A credit is call-only prepaid credit at one merchant: it is not refundable and it is not spend that came back, so it
never changes a spend budget. Parsing never raises.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from .exceptions import InvalidChallengeError
from .prepaid import build_prepaid_refund_message

HEADER_CREDIT = "X-KasGate-Credit"
CREDIT_EXTENSION = "kasgate_credit"
# The real header is about 450 characters of Base64; anything this large is not a credit receipt.
MAX_CREDIT_HEADER_CHARS = 2048

_TX_ID = r"^[0-9a-f]{64}$"
_POSITIVE_SOMPI = r"^[1-9][0-9]{0,17}$"
_BALANCE_SOMPI = r"^(?:0|[1-9][0-9]{0,17})$"

# The buyer's signed credit status (same challenge store, message layout and Purpose as refunds; Action: credits).
CREDIT_CHALLENGE_PATH = "/v1/prepaid/credits/challenge"
CREDIT_STATUS_PATH = "/v1/prepaid/credits/status"
CREDITS_ACTION = "credits"
DEFAULT_CREDIT_STATUS_LIMIT = 10
"""The gateway's page size when a status request names no `limit`."""
MAX_STATUS_CREDITS = 100
"""The largest `limit` (and so the most `credits` on one page) the gateway serves."""
MAX_CREDIT_CURSOR_CHARS = 512
"""The gateway refuses longer `before` cursors (400 invalid_cursor); a longer `next_cursor` is not a gateway cursor."""
DEFAULT_CREDIT_PAGE_BUDGET = 10
"""Signed status pages one confirmation may read (restart and re-walk included): 1000 credits at 100 per page, well
inside the gateway's default 30 status calls per minute."""
MAX_CREDIT_PAGE_BUDGET = 100

CreditStatus = Literal["hint", "confirmed", "unconfirmed", "invalid"]
ConfirmationBasis = Literal["credit_id", "increase", "none"]
CreditReason = Literal["attempts_exhausted", "window_expired"]
_NONCE = r"^[0-9a-f]{32}$"
_SLUG = r"^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])$"
_SOMPI_RE = re.compile(_BALANCE_SOMPI)


def _sompi(value: object) -> int:
    """A gateway sompi value: a decimal STRING in canonical form, parsed to int (numbers and odd forms are refused)."""
    if not isinstance(value, str) or _SOMPI_RE.fullmatch(value) is None:
        raise ValueError("sompi must be a canonical decimal string")
    return int(value)


Sompi = Annotated[int, BeforeValidator(_sompi)]


class FailedDeliveryCreditHint(BaseModel):
    """The decoded `X-KasGate-Credit` header. Unsigned: a hint, never proof."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    creditId: uuid.UUID
    kind: Literal["failed_delivery_credit"]
    txId: Annotated[str, Field(pattern=_TX_ID)]
    merchantId: uuid.UUID
    creditSompi: Annotated[str, Field(pattern=_POSITIVE_SOMPI)]
    balanceSompi: Annotated[str, Field(pattern=_BALANCE_SOMPI)]
    reason: CreditReason

    @property
    def credit_sompi(self) -> int:
        return int(self.creditSompi)

    @property
    def balance_sompi(self) -> int:
        return int(self.balanceSompi)


class CreditConfirmation(BaseModel):
    """What the signed credit status found. `confirmed` is True only when the gateway's own list corroborates the hint."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    confirmed: bool
    basis: ConfirmationBasis
    """`credit_id`: the signed status lists the hinted creditId with the hinted txId, amount, reason and slug.
    `increase`: the paged walk could not see every credit (page budget spent or the count never matched), so
    `credited_sompi` grew by at least the hinted credit since the caller's `baseline_credited_sompi` (a weaker check).
    `none`: the check could not be completed or the status contradicts the hint."""
    credited_sompi: int | None = None
    baseline_credited_sompi: int | None = None
    http_status: int | None = None
    """The gateway's HTTP status when the check failed because of an error answer."""
    error_code: str | None = None
    """The gateway's `error` code (for example `rate_limited`) when the check failed because of an error answer."""
    pages_read: int | None = None
    """Signed status pages read for this check (each one a fresh challenge), when any reply was read."""
    detail: str


class CreditObservation(BaseModel):
    """The credit announced by one response and how far it has been verified."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: CreditStatus
    hint: FailedDeliveryCreditHint | None = None
    error: str | None = None
    confirmation: CreditConfirmation | None = None

    @property
    def confirmed(self) -> bool:
        return self.status == "confirmed"


def parse_credit_header(
    value: str | None,
) -> tuple[FailedDeliveryCreditHint | None, str | None]:
    """Decode a header value into `(hint, None)` or `(None, reason)`. Never raises; `(None, None)` = no header."""
    if value is None:
        return None, None
    if len(value) > MAX_CREDIT_HEADER_CHARS:
        return (
            None,
            f"X-KasGate-Credit is larger than {MAX_CREDIT_HEADER_CHARS} characters and was ignored",
        )
    try:
        raw = base64.b64decode(value.strip(), validate=True)
        return FailedDeliveryCreditHint.model_validate_json(raw), None
    except (binascii.Error, ValueError, RecursionError):
        return (
            None,
            "X-KasGate-Credit is not a valid Base64 credit receipt and was ignored",
        )


def observe_credit(
    value: str | None,
    *,
    status_code: int,
    tx_id: str | None = None,
    merchant_sompi: int | None = None,
) -> CreditObservation | None:
    """The observation for a response's header value, or None when the response carries no header.

    Besides decoding, the hint must fit the call it arrived on: only a 5xx answer is a failed delivery, the txId
    must be the payment that was settled for this call and the credit must equal its merchant share (output 0).
    A hint that does not fit is `invalid` and can never be confirmed.
    """
    hint, error = parse_credit_header(value)
    if value is None:
        return None
    if hint is None:
        return CreditObservation(status="invalid", error=error)
    problem: str | None = None
    if status_code < 500:
        problem = f"a credit hint arrived on an HTTP {status_code} answer; only failed (5xx) deliveries are credited"
    elif tx_id is not None and hint.txId != tx_id:
        problem = "the credit hint names a different payment than the one settled for this call"
    elif merchant_sompi is not None and hint.credit_sompi != merchant_sompi:
        problem = "the hinted credit differs from the merchant share of the settled payment"
    elif hint.balance_sompi < hint.credit_sompi:
        problem = "the hinted balance is smaller than the hinted credit"
    if problem is not None:
        return CreditObservation(status="invalid", error=problem)
    return CreditObservation(status="hint", hint=hint)


class CreditStatusChallenge(BaseModel):
    """`POST /v1/prepaid/credits/challenge` reply: sign `message` with the depositing key (strict, no extra fields)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    nonce: Annotated[str, Field(pattern=_NONCE)]
    message: str
    action: Literal["credits"]
    slug: Annotated[str, Field(pattern=_SLUG)]
    merchant_id: uuid.UUID
    payer_address: str
    amount_sompi: Literal["0"]
    refund_id: None
    spendable_sompi: Sompi | None = None
    """Informational only for `credits` (not signed, not a hold); the gateway may drop it."""
    refundable_sompi: Sompi | None = None
    """Informational only for `credits` (not signed, not a hold); the signed status body is authoritative."""
    expires_at: datetime


class CreditStatusRequest(BaseModel):
    """`POST /v1/prepaid/credits/status` body this SDK sends: the signed challenge plus an optional page (strict)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    nonce: Annotated[str, Field(pattern=_NONCE)]
    signature: Annotated[str, Field(pattern=r"^[0-9a-f]{128}$")]
    before: Annotated[str, Field(min_length=1, max_length=MAX_CREDIT_CURSOR_CHARS)] | None = None
    """The previous page's `next_cursor`; omitted for the newest page."""
    limit: Annotated[int, Field(ge=1, le=MAX_STATUS_CREDITS)] | None = None
    """Page size 1..100; omitted means the gateway default (10)."""

    def body(self) -> dict[str, str | int]:
        """The JSON body, without the fields left unset."""
        return self.model_dump(exclude_none=True)


class BuyerCreditItem(BaseModel):
    """One failed-delivery credit in the signed status (newest first). Sompi strings are parsed to int."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    credit_id: uuid.UUID
    """The ledger entry id; the same value as `creditId` in X-KasGate-Credit."""
    tx_id: Annotated[str, Field(pattern=_TX_ID)]
    slug: Annotated[str, Field(pattern=_SLUG)]
    credit_sompi: Sompi
    shortfall_sompi: Sompi
    reason: CreditReason
    created_at: datetime


class BuyerCreditStatus(BaseModel):
    """`POST /v1/prepaid/credits/status` reply: this key's failed-delivery credit at one merchant (CONTRACT v2.14).

    Strict: unknown fields, JSON numbers where the gateway sends decimal strings, or more than 100 credits are
    refused. Call-only credit (`credit_balance_sompi`) is never refundable; `refundable_sompi` is what a refund may
    take. `credit_count` is the number of credits across all pages; `credits` is one page, newest first, and
    `next_cursor` (null on the last page) is passed as `before` with a NEW challenge to read the next older page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    merchant_id: uuid.UUID
    credited_sompi: Sompi
    credit_balance_sompi: Sompi
    refundable_sompi: Sompi
    credit_count: Annotated[int, Field(strict=True, ge=0)]
    credits: Annotated[list[BuyerCreditItem], Field(max_length=MAX_STATUS_CREDITS)]
    next_cursor: Annotated[str, Field(min_length=1, max_length=MAX_CREDIT_CURSOR_CHARS)] | None

    @model_validator(mode="after")
    def _count_covers_list(self) -> BuyerCreditStatus:
        if self.credit_count < len(self.credits):
            raise ValueError("credit_count is smaller than the number of credits listed")
        if len({item.credit_id for item in self.credits}) != len(self.credits):
            raise ValueError("a credit_id is listed twice")
        if self.next_cursor is not None and not self.credits:
            raise ValueError("next_cursor on a page without credits")
        return self


def validate_credit_challenge(
    challenge: CreditStatusChallenge,
    *,
    domain: str,
    address: str,
    slug: str,
    now: datetime | None = None,
) -> None:
    """Check a credits challenge against what the SDK asked for before anything is signed.

    The text is rebuilt locally (this payer, this domain, `Action: credits`, amount 0, no refund) and must equal the
    gateway's byte for byte, so a gateway cannot get a refund-request or cancel signature out of this call.
    Raises InvalidChallengeError; nothing is signed when it raises."""
    if (challenge.payer_address, challenge.slug) != (address, slug):
        raise InvalidChallengeError("credits challenge is for another payer or endpoint")
    if challenge.expires_at.tzinfo is None or challenge.expires_at <= (now or datetime.now(UTC)):
        raise InvalidChallengeError("credits challenge has already expired")
    expected = build_prepaid_refund_message(
        domain=domain,
        action=CREDITS_ACTION,
        address=address,
        merchant_id=challenge.merchant_id,
        amount_sompi=0,
        refund_id=None,
        nonce=challenge.nonce,
        expires_at=challenge.expires_at,
    )
    if challenge.message != expected:
        raise InvalidChallengeError("credits challenge text does not match the request; refusing to sign it")


def judge_credit(
    hint: FailedDeliveryCreditHint,
    status: BuyerCreditStatus,
    *,
    slug: str | None = None,
    baseline_credited_sompi: int | None = None,
    credits: Sequence[BuyerCreditItem] | None = None,
    complete: bool | None = None,
    pages_read: int | None = None,
) -> CreditConfirmation:
    """Compare a hint with the buyer's signed credit status.

    `status` is the newest page (its lifetime `credited_sompi` and `credit_count`); `credits` are all the items read
    across pages (default: `status.credits`) and `complete` says whether they are every credit there is (default: this
    page is the last and `credit_count` does not exceed what it lists).

    Confirmed (`credit_id`) only when the status is for the hinted merchant and lists the hinted `creditId` with the
    hinted txId, amount and reason (and slug `slug`, the endpoint that was called, when given). The hinted creditId
    with any other detail, or the hinted txId under another credit_id, contradicts the hint. When nothing matches and
    the walk was complete, the hint is simply not a credit. When it was not complete, a `baseline_credited_sompi`
    (read before the call) allows a weaker `increase` confirmation; without it the hint stays unconfirmed.
    """
    credited = status.credited_sompi
    amount = hint.credit_sompi
    items = status.credits if credits is None else credits
    if complete is None:
        complete = status.next_cursor is None and status.credit_count <= len(items)

    def no(detail: str) -> CreditConfirmation:
        return CreditConfirmation(
            confirmed=False,
            basis="none",
            credited_sompi=credited,
            baseline_credited_sompi=baseline_credited_sompi,
            pages_read=pages_read,
            detail=detail,
        )

    if status.merchant_id != hint.merchantId:
        return no("the credit status belongs to a different merchant than the hint")
    item = next((entry for entry in items if entry.credit_id == hint.creditId), None)
    if item is not None:
        if item.tx_id != hint.txId:
            return no("the gateway lists this credit_id for another payment than the hinted txId")
        if item.credit_sompi != amount:
            return no(
                f"the gateway lists this payment with a credit of {item.credit_sompi} sompi, not the hinted {amount}"
            )
        if item.reason != hint.reason:
            return no(f"the gateway lists this payment with reason {item.reason}, not the hinted {hint.reason}")
        if slug is not None and item.slug != slug:
            return no(f"the gateway lists this payment at endpoint {item.slug}, not {slug}")
        return CreditConfirmation(
            confirmed=True,
            basis="credit_id",
            credited_sompi=credited,
            baseline_credited_sompi=baseline_credited_sompi,
            pages_read=pages_read,
            detail=f"the signed credit status lists this credit_id and payment with a credit of {amount} sompi",
        )
    if any(entry.tx_id == hint.txId for entry in items):
        return no("the gateway lists this payment under another credit_id than the hinted creditId")
    if complete:
        return no("the signed credit status lists no credit for this payment")
    seen = len(items)
    if baseline_credited_sompi is None:
        return no(
            f"this payment is not among the {seen} of {status.credit_count} credits read and no earlier "
            "credited_sompi was given to compare with"
        )
    grew = credited - baseline_credited_sompi
    if grew >= amount:
        return CreditConfirmation(
            confirmed=True,
            basis="increase",
            credited_sompi=credited,
            baseline_credited_sompi=baseline_credited_sompi,
            pages_read=pages_read,
            detail=(
                f"this payment is not among the {seen} of {status.credit_count} credits read; credited_sompi grew by "
                f"{grew} sompi since the baseline (hint: {amount})"
            ),
        )
    return no(f"credited_sompi grew by {grew} sompi since the baseline, less than the hinted {amount}")


def credit_of(response: httpx.Response) -> CreditObservation | None:
    """The credit announced by a response from `KasGateClient.request`, or None when it carried no credit header."""
    observation = response.extensions.get(CREDIT_EXTENSION)
    return observation if isinstance(observation, CreditObservation) else None
