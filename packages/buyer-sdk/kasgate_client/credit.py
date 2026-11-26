"""Failed-delivery credit hints (CONTRACT v2.14): the `X-KasGate-Credit` response header, treated as an UNSIGNED hint.

When a paid call fails for good (the upstream answered 5xx or not at all) and redelivery is closed, the gateway turns
the merchant share of that payment into prepaid credit and announces it in `X-KasGate-Credit`
(`Base64(JSON{creditId, kind, txId, merchantId, creditSompi, balanceSompi, reason})`). The header carries no
signature, so the SDK never reports a credit as real from the header alone:

* `credit_of(response)` returns a `CreditObservation` whose status is `hint` (parsed, not checked), `invalid`
  (malformed, oversized or inconsistent with the paid call) or, after the balance check, `confirmed` / `unconfirmed`.
* `KasGateClient.confirm_credit()` (or `auto_confirm_credit=True`) reads the public `GET /v1/prepaid/balance` and only
  then says `confirmed`: the balance must belong to the hinted merchant and its cumulative `credited_sompi` must
  cover the hinted credit (and, when a baseline is known, must have grown by at least that amount).

A credit is call-only prepaid credit at one merchant: it is not refundable and it is not spend that came back, so it
never changes a spend budget. Parsing never raises.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .prepaid import PrepaidBalance

HEADER_CREDIT = "X-KasGate-Credit"
CREDIT_EXTENSION = "kasgate_credit"
# The real header is about 450 characters of Base64; anything this large is not a credit receipt.
MAX_CREDIT_HEADER_CHARS = 2048

_TX_ID = r"^[0-9a-f]{64}$"
_POSITIVE_SOMPI = r"^[1-9][0-9]{0,17}$"
_BALANCE_SOMPI = r"^(?:0|[1-9][0-9]{0,17})$"

CreditStatus = Literal["hint", "confirmed", "unconfirmed", "invalid"]
ConfirmationBasis = Literal["increase", "cumulative", "none"]
CreditReason = Literal["attempts_exhausted", "window_expired"]


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
    """What the balance check found. `confirmed` is True only when the public balance corroborates the hint."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    confirmed: bool
    basis: ConfirmationBasis
    """`increase`: credited_sompi grew by at least the hinted credit since `baseline_credited_sompi`.
    `cumulative`: no baseline was known; credited_sompi only covers the hinted credit (a weaker check).
    `none`: the check could not be completed or the balance contradicts the hint."""
    credited_sompi: int | None = None
    baseline_credited_sompi: int | None = None
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
        problem = (
            "the hinted credit differs from the merchant share of the settled payment"
        )
    elif hint.balance_sompi < hint.credit_sompi:
        problem = "the hinted balance is smaller than the hinted credit"
    if problem is not None:
        return CreditObservation(status="invalid", error=problem)
    return CreditObservation(status="hint", hint=hint)


def judge_credit(
    hint: FailedDeliveryCreditHint,
    balance: PrepaidBalance,
    baseline_credited_sompi: int | None = None,
) -> CreditConfirmation:
    """Compare a hint with the buyer's public prepaid balance at the merchant."""
    if balance.credited_sompi is None:
        return CreditConfirmation(
            confirmed=False,
            basis="none",
            detail="the gateway balance does not report credited_sompi",
        )
    credited = int(balance.credited_sompi)
    if balance.merchant_id != hint.merchantId:
        return CreditConfirmation(
            confirmed=False,
            basis="none",
            credited_sompi=credited,
            baseline_credited_sompi=baseline_credited_sompi,
            detail="the balance belongs to a different merchant than the hint",
        )
    amount = hint.credit_sompi
    if baseline_credited_sompi is not None:
        grew = credited - baseline_credited_sompi
        ok = grew >= amount
        detail = (
            f"credited_sompi grew by {grew} sompi (hint: {amount})"
            if ok
            else f"credited_sompi grew by {grew} sompi, less than the hinted {amount}"
        )
        return CreditConfirmation(
            confirmed=ok,
            basis="increase",
            credited_sompi=credited,
            baseline_credited_sompi=baseline_credited_sompi,
            detail=detail,
        )
    ok = credited >= amount
    detail = (
        f"credited_sompi is {credited} sompi, covering the hinted {amount} (no earlier balance to compare with)"
        if ok
        else f"credited_sompi is {credited} sompi, less than the hinted {amount}"
    )
    return CreditConfirmation(
        confirmed=ok,
        basis="cumulative",
        credited_sompi=credited,
        baseline_credited_sompi=None,
        detail=detail,
    )


def credit_of(response: httpx.Response) -> CreditObservation | None:
    """The credit announced by a response from `KasGateClient.request`, or None when it carried no credit header."""
    observation = response.extensions.get(CREDIT_EXTENSION)
    return observation if isinstance(observation, CreditObservation) else None
