"""Payment verification results and errors shared by the kaspa-exact-v1 engine and the kaspa-exact-v2 verifier."""

from dataclasses import dataclass


class PaymentVerificationError(Exception):
    """Raised when a payment is not acceptable. ``code`` is one of the x402 error codes."""

    code: str
    message: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class RpcUnavailableError(PaymentVerificationError):
    """The Kaspa node could not be reached, did not answer in time, or did not confirm acceptance in time.

    ``code`` is ``rpc_unavailable``. Callers answer 503, release the replay lock and keep the nonce; ``retryable`` is
    True because re-submitting the same transaction is idempotent (a duplicate with a matching nonce is success).
    """

    retryable: bool = True

    def __init__(self, message: str) -> None:
        super().__init__("rpc_unavailable", message)


@dataclass(frozen=True)
class PayoutSighting:
    """A transaction the node shows paying a refund's payout address that may be that refund's payout (CONTRACT
    v2.13 cancel check). `payload_seen`: seen in the mempool carrying the refund payload (proven); False for an
    accepted output of exactly the refund amount whose payload the UTXO index cannot show (possible payout)."""

    tx_id: str
    payload_seen: bool


@dataclass(frozen=True)
class VerifiedPayment:
    tx_id: str
    payer_address: str | None  # None only for k402 kaspa-utxo, which proves no payer
    total_sompi: int
    merchant_sompi: int
    fee_sompi: int
