"""Typed exceptions. Every error the SDK raises on purpose derives from KasGateError."""

from __future__ import annotations

from typing import Any

import httpx


class KasGateError(Exception):
    """Base class for all SDK errors."""


class ConfigurationError(KasGateError):
    """Invalid constructor or call arguments (bad key, bad network, bad cap, unreplayable body)."""


class InvalidChallengeError(KasGateError):
    """The 402 challenge is missing, undecodable, or internally inconsistent. Nothing was signed."""


class ProtocolMismatchError(InvalidChallengeError):
    """x402Version, scheme or asset is not the one this SDK implements."""


class NetworkMismatchError(InvalidChallengeError):
    """The challenge network or an address prefix differs from the client's network."""


class FeeSplitMismatchError(InvalidChallengeError):
    """platformFee.feeAmount differs from max(ceil(amount * 150 / 10_000), min_output_sompi), or an output would be
    below the min_output_sompi floor."""


class ChallengeExpiredError(InvalidChallengeError):
    """The challenge's expiresAt has passed (or is too close to pay in time). Nothing was signed."""


class UnexpectedRecipientError(InvalidChallengeError):
    """payTo or feeTo differs from the address the caller pinned."""


class PriceCapExceededError(KasGateError):
    """The quoted price exceeds the client-side circuit breaker. Nothing was signed."""

    def __init__(self, amount_sompi: int, cap_sompi: int) -> None:
        super().__init__(
            f"quoted price {amount_sompi} sompi exceeds the spend cap of {cap_sompi} sompi"
        )
        self.amount_sompi = amount_sompi
        self.cap_sompi = cap_sompi


class InsufficientFundsError(KasGateError):
    """The payer address cannot fund price plus network fee."""


class DustChangeError(InsufficientFundsError):
    """Every UTXO of the payer was used and the change would still be in (0, 30_000_000) sompi. CONTRACT v2.3 forbids
    small change (storage mass), and the SDK never donates it to the miner fee."""


class TransactionBuildError(KasGateError):
    """The payment transaction could not be built safely (fragmented UTXOs, fee too high, wrong output layout)."""


class NetworkFeeTooHighError(TransactionBuildError):
    """The miner fee for the transaction exceeds max_network_fee_sompi."""

    def __init__(self, fee_sompi: int, cap_sompi: int) -> None:
        super().__init__(
            f"network fee {fee_sompi} sompi exceeds max_network_fee_sompi={cap_sompi}"
        )
        self.fee_sompi = fee_sompi
        self.cap_sompi = cap_sompi


class RpcError(KasGateError):
    """Kaspa node RPC failure while fetching UTXOs."""


class PaymentRejectedError(KasGateError):
    """The gateway answered the paid retry with an error (usually 402). The tx was not accepted as payment."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        response: httpx.Response | None = None,
    ) -> None:
        super().__init__(f"payment rejected ({status_code} {code}): {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.response = response


class PaymentReplayedError(PaymentRejectedError):
    """409 payment_replayed: the txId was already presented."""


class PaymentExpiredError(PaymentRejectedError):
    """402 payment_expired: the gateway no longer knows the challenge nonce. The SDK re-challenges once before
    raising this, and a transaction is never broadcast for an expired nonce."""


class PaymentReceiptError(KasGateError):
    """The paid retry succeeded but PAYMENT-RESPONSE is missing, invalid or does not match the tx we signed.

    The funds may have been spent; `response` carries whatever the server returned and `tx_id` the id we signed.
    """

    def __init__(
        self,
        message: str,
        response: httpx.Response | None = None,
        tx_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.response = response
        self.tx_id = tx_id


def error_details(response: httpx.Response) -> tuple[str, str]:
    """Extract (code, message) from an X402ErrorBody JSON body, tolerating anything else."""
    try:
        body: Any = response.json()
    except ValueError:
        return "unknown", response.text[:200]
    if isinstance(body, dict) and isinstance(body.get("detail"), dict):
        body = body["detail"]
    if isinstance(body, dict):
        return str(body.get("error", "unknown")), str(body.get("message", ""))[:500]
    return "unknown", str(body)[:200]
