"""Validation of a decoded 402 challenge. Runs before anything is signed."""

from __future__ import annotations

import httpx

from .constants import (
    HEADER_PAYMENT_REQUIRED,
    NETWORK_ADDRESS_PREFIX,
    X402_ASSET,
    X402_SCHEME,
    X402_VERSION,
)
from .exceptions import (
    FeeSplitMismatchError,
    InvalidChallengeError,
    NetworkMismatchError,
    PriceCapExceededError,
    ProtocolMismatchError,
    UnexpectedRecipientError,
)
from .protocol import PaymentRequired, compute_fee_split


def decode_challenge(response: httpx.Response) -> PaymentRequired | None:
    """Return the decoded challenge, or None when the 402 carries no PAYMENT-REQUIRED header (not an x402 server)."""
    raw = response.headers.get(HEADER_PAYMENT_REQUIRED)
    if raw is None:
        return None
    try:
        return PaymentRequired.from_header(raw)
    except ValueError as exc:
        raise InvalidChallengeError(f"PAYMENT-REQUIRED header cannot be decoded: {exc}") from exc


def validate_challenge(
    challenge: PaymentRequired,
    *,
    network: str,
    cap_sompi: int,
    min_output_sompi: int = 1,
    pinned_fee_to: str | None = None,
    expected_pay_to: str | None = None,
) -> tuple[int, int]:
    """Validate the challenge and return (merchant_sompi, fee_sompi) recomputed locally.

    Raises a subclass of InvalidChallengeError for protocol, network, fee-split or recipient problems and
    PriceCapExceededError when the total exceeds the circuit breaker. The cap is enforced on the recomputed total,
    never on a field the server could have forged independently of `amount`.
    """
    if challenge.x402Version != X402_VERSION:
        raise ProtocolMismatchError(f"unsupported x402Version {challenge.x402Version}; expected {X402_VERSION}")
    if challenge.scheme != X402_SCHEME:
        raise ProtocolMismatchError(f"unsupported scheme {challenge.scheme!r}; expected {X402_SCHEME!r}")
    if challenge.asset != X402_ASSET:
        raise ProtocolMismatchError(f"unsupported asset {challenge.asset!r}; expected {X402_ASSET!r}")
    if challenge.network != network:
        raise NetworkMismatchError(f"challenge network {challenge.network!r} differs from client network {network!r}")

    prefix = NETWORK_ADDRESS_PREFIX[network]
    for label, address in (("payTo", challenge.payTo), ("feeTo", challenge.platformFee.feeTo)):
        if not address.startswith(f"{prefix}:"):
            raise NetworkMismatchError(f"{label} must use the '{prefix}:' prefix on {network}")

    total = challenge.total_sompi
    merchant, fee = compute_fee_split(total)
    if challenge.fee_sompi != fee:
        raise FeeSplitMismatchError(
            f"platformFee.feeAmount={challenge.fee_sompi} but ceil({total} * 150 / 10000) = {fee}"
        )
    if merchant < min_output_sompi or fee < min_output_sompi:
        raise FeeSplitMismatchError(
            f"split {merchant}/{fee} puts an output below the minimum of {min_output_sompi} sompi"
        )
    if challenge.payTo == challenge.platformFee.feeTo:
        raise FeeSplitMismatchError("payTo and feeTo are identical; the split would collapse into one recipient")

    if pinned_fee_to is not None and challenge.platformFee.feeTo != pinned_fee_to.strip().lower():
        raise UnexpectedRecipientError("feeTo differs from the pinned platform treasury address")
    if expected_pay_to is not None and challenge.payTo != expected_pay_to.strip().lower():
        raise UnexpectedRecipientError("payTo differs from the expected merchant address")

    if total > cap_sompi:
        raise PriceCapExceededError(total, cap_sompi)
    return merchant, fee
