"""CONTRACT v2.12 free trial calls and merchant webhook signature checks.

Free trials: a 402 from an endpoint with free trial calls carries ``X-KasGate-Trial-Offer``. The buyer asks
``POST /v1/trials/challenge`` for ONE call, rebuilds the challenge text from its own request (domain, payer, slug,
method, path + query, body hash), signs it with the key of its P2PK address and sends it as ``X-KasGate-Trial``.
A trial moves no coins and is never charged a fee; once the trial is used up the gateway answers with its normal 402.

Webhooks: `verify_webhook_signature` is the receiver-side check merchants run on every delivery.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .exceptions import InvalidChallengeError
from .protocol import X402Header

HEADER_TRIAL_OFFER = "X-KasGate-Trial-Offer"
HEADER_TRIAL_CALL = "X-KasGate-Trial"
HEADER_TRIAL_RECEIPT = "X-KasGate-Trial-Receipt"
HEADER_TRIAL_LIMIT = "X-KasGate-Trial-Limit"
HEADER_TRIAL_REMAINING = "X-KasGate-Trial-Remaining"
TRIAL_SCHEME = "kasgate-trial-v1"
TRIAL_CALL_PURPOSE = "kasgate:v1:trial-call"
TRIAL_CHALLENGE_PATH = "/v1/trials/challenge"
TRIAL_STATUS_PATH = "/v1/trials/status"
TRIAL_RECEIPT_EXTENSION = "kasgate_trial_receipt"

WEBHOOK_SIGNATURE_HEADER = "X-KasGate-Signature"
WEBHOOK_TIMESTAMP_HEADER = "X-KasGate-Timestamp"
WEBHOOK_EVENT_ID_HEADER = "X-KasGate-Event-Id"
WEBHOOK_EVENT_TYPE_HEADER = "X-KasGate-Event-Type"
WEBHOOK_DELIVERY_ID_HEADER = "X-KasGate-Delivery-Id"
WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS = 300


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class TrialOffer(X402Header):
    """Body of the X-KasGate-Trial-Offer header on a 402."""

    scheme: Literal["kasgate-trial-v1"] = "kasgate-trial-v1"
    freeTrialCalls: Annotated[int, Field(ge=1, le=100)]
    challengeUrl: str
    statusUrl: str


class TrialChallenge(_Model):
    nonce: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    message: str
    slug: str
    merchant_id: uuid.UUID
    payer_address: str
    method: str
    path: str
    body_hash: str
    free_trial_calls: Annotated[int, Field(ge=0)]
    calls_used: Annotated[int, Field(ge=0)]
    remaining: Annotated[int, Field(ge=0)]
    expires_at: datetime


class TrialCallHeader(X402Header):
    """Body of the X-KasGate-Trial request header (byte-compatible with the gateway model)."""

    nonce: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    signature: Annotated[str, Field(pattern=r"^[0-9a-f]{128}$")]


class TrialReceipt(X402Header):
    """Body of the X-KasGate-Trial-Receipt response header."""

    trialCallId: uuid.UUID
    nonce: str
    limit: Annotated[int, Field(ge=0)]
    remaining: Annotated[int, Field(ge=0)]
    status: Literal["used", "refunded"]


class TrialStatus(_Model):
    slug: str
    merchant_id: uuid.UUID
    payer_address: str
    free_trial_calls: Annotated[int, Field(ge=0)]
    calls_used: Annotated[int, Field(ge=0)]
    remaining: Annotated[int, Field(ge=0)]


def build_trial_call_message(
    *,
    domain: str,
    address: str,
    merchant_id: uuid.UUID,
    slug: str,
    method: str,
    path: str,
    body_hash: str,
    nonce: str,
    expires_at: datetime,
) -> str:
    """The exact text signed for one trial call; identical to the gateway's `build_trial_call_message`."""
    return (
        "KasGate\n"
        f"Domain: {domain}\n"
        f"Purpose: {TRIAL_CALL_PURPOSE}\n"
        f"Address: {address}\n"
        f"Merchant: {merchant_id}\n"
        f"Endpoint: {slug}\n"
        f"Method: {method}\n"
        f"Path: {path}\n"
        f"Body-Hash: {body_hash}\n"
        f"Nonce: {nonce}\n"
        f"Expires: {expires_at.isoformat()}"
    )


def validate_trial_challenge(
    challenge: TrialChallenge,
    *,
    domain: str,
    address: str,
    slug: str,
    method: str,
    path: str,
    body_hash: str,
    now: datetime | None = None,
) -> None:
    """Check a trial challenge against the request the SDK is about to send; raise InvalidChallengeError otherwise.

    The text is rebuilt locally and must equal the server's byte for byte, so the signature can only ever authorise
    this one request (it carries no amount: a trial never moves coins).
    """
    if (challenge.payer_address, challenge.slug, challenge.method, challenge.path, challenge.body_hash) != (
        address,
        slug,
        method,
        path,
        body_hash,
    ):
        raise InvalidChallengeError("trial challenge is for another payer, endpoint, method, path or body")
    if challenge.expires_at.tzinfo is None or challenge.expires_at <= (now or datetime.now(UTC)):
        raise InvalidChallengeError("trial challenge has already expired")
    expected = build_trial_call_message(
        domain=domain,
        address=address,
        merchant_id=challenge.merchant_id,
        slug=slug,
        method=method,
        path=path,
        body_hash=body_hash,
        nonce=challenge.nonce,
        expires_at=challenge.expires_at,
    )
    if challenge.message != expected:
        raise InvalidChallengeError("trial challenge text does not match the request; refusing to sign it")


def compute_webhook_signature(secret: str, timestamp: int, body: bytes) -> str:
    """Hex HMAC-SHA256 of `"<timestamp>.<raw body>"` keyed with the webhook secret (`whsec_...`, UTF-8)."""
    return hmac.new(secret.encode("utf-8"), str(timestamp).encode("ascii") + b"." + body, hashlib.sha256).hexdigest()


def verify_webhook_signature(
    secret: str,
    signature_header: str,
    body: bytes,
    *,
    tolerance_seconds: int = WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS,
    now: int | None = None,
) -> bool:
    """True when `X-KasGate-Signature` (`t=<unix>,v1=<hex>`) signs exactly `body` (the RAW request bytes, before any
    JSON parsing) with `secret` and its timestamp is within `tolerance_seconds` of now. Constant-time compare;
    reject the delivery when this is False and dedupe accepted ones on `X-KasGate-Event-Id`."""
    timestamp: int | None = None
    candidates: list[str] = []
    for part in signature_header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t" and value.isdigit():
            timestamp = int(value)
        elif key == "v1" and value:
            candidates.append(value)
    if timestamp is None or not candidates:
        return False
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        return False
    expected = compute_webhook_signature(secret, timestamp, body)
    return any(hmac.compare_digest(expected, candidate) for candidate in candidates)
