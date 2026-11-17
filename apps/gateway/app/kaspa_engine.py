"""Kaspa RPC connection manager and dual-output UTXO payment verification engine.

Verification contract (see docs/CONTRACTS.md):

* ``outputs[0]`` pays ``required.payTo`` EXACTLY ``required.merchant_amount_sompi``.
* ``outputs[1]`` pays ``required.platformFee.feeTo`` EXACTLY ``required.fee_amount_sompi``.
* ``outputs[2:]`` are change only and are never counted towards the payment.

Error-code decisions:

* ``wrong_network``       network mismatch or an address prefix that does not belong to the network.
* ``invalid_payment_header`` malformed JSON/transaction, txId mismatch, payer address not an input owner.
* ``underpaid``           ``outputs[0]`` pays the right script but strictly less than the merchant amount.
* ``invalid_fee_split``   missing ``outputs[1]``, wrong script, swapped order, ANY fee value other than the
                          exact fee (1 sompi short or over), or a merchant value above the exact amount.
* ``tx_rejected``         storage mass above the standard maximum, input value mismatch, node rejection.
* ``rpc_unavailable``     the node is unreachable or timed out (``RpcUnavailableError``, ``retryable=True``);
                          nothing was settled and re-submitting the same transaction is idempotent.

Payer address: a signed Kaspa input only carries a signature, the public key lives in the UTXO being
spent. ``PaymentSignature.payerAddress`` is therefore authoritative for reporting, and it is
cross-checked against the locking scripts embedded in the transaction inputs (``inputs[].utxo``).
The node performs the real signature/UTXO validation when the transaction is submitted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, Protocol

from kaspa import (
    Address,
    NetworkId,
    Resolver,
    RpcClient,
    Transaction,
    calculate_storage_mass,
    calculate_transaction_mass,
    maximum_standard_transaction_mass,
    pay_to_address_script,
)

from app.config import (
    BPS_DENOMINATOR,
    NETWORK_ADDRESS_PREFIX,
    PLATFORM_FEE_BPS,
    Settings,
    get_settings,
)
from app.schemas import PaymentRequired, PaymentSignature

logger = logging.getLogger("kasgate.kaspa_engine")

NETWORK_ID_BY_CONFIG: Final[dict[str, str]] = {
    "kaspa:testnet-10": "testnet-10",
    "kaspa:mainnet": "mainnet",
}

NATIVE_SUBNETWORK_ID: Final[str] = "0" * 40
TX_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
DUPLICATE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"already in the mempool|already in mempool|already been accepted|already accepted|already exists in the mempool",
    re.IGNORECASE,
)
TRANSPORT_MARKERS: Final[tuple[str, ...]] = (
    "websocket",
    "not connected",
    "disconnected",
    "connection",
    "timed out",
    "timeout",
    "channel closed",
    "broken pipe",
    "eof",
)

DEFAULT_BACKOFF_INITIAL_SECONDS: Final[float] = 1.0
DEFAULT_BACKOFF_MAX_SECONDS: Final[float] = 30.0
HEALTH_POLL_SECONDS: Final[float] = 5.0


class PaymentVerificationError(Exception):
    """Raised when a payment is not acceptable. ``code`` is one of the x402 error codes."""

    code: str
    message: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class RpcUnavailableError(PaymentVerificationError):
    """The Kaspa node could not be reached or did not answer in time; the payment was NOT settled.

    ``code`` is ``rpc_unavailable``. Callers answer 503 and release the replay lock; ``retryable`` is True
    because re-submitting the same transaction is idempotent (a duplicate is treated as success).
    """

    retryable: bool = True

    def __init__(self, message: str) -> None:
        super().__init__("rpc_unavailable", message)


@dataclass(frozen=True)
class VerifiedPayment:
    tx_id: str
    payer_address: str
    total_sompi: int
    merchant_sompi: int
    fee_sompi: int


class RpcClientProtocol(Protocol):
    @property
    def is_connected(self) -> bool: ...

    async def connect(
        self,
        block_async_connect: bool | None = None,
        strategy: str | None = None,
        url: str | None = None,
        timeout_duration: int | None = None,
        retry_interval: int | None = None,
    ) -> None: ...

    async def disconnect(self) -> None: ...

    async def submit_transaction(self, request: dict[str, Any]) -> Any: ...


RpcClientFactory = Callable[[], RpcClientProtocol]


def network_id_for(network: str) -> str:
    try:
        return NETWORK_ID_BY_CONFIG[network]
    except KeyError as exc:
        raise ValueError(f"unsupported Kaspa network: {network}") from exc


def _script_key(address: str, *, what: str, code: str) -> tuple[int, str]:
    """Locking script of ``address`` as a comparable ``(version, script_hex)`` pair."""
    try:
        script = pay_to_address_script(Address(address))
    except Exception as exc:
        raise PaymentVerificationError(code, f"{what} is not a valid Kaspa address") from exc
    return int(script.version), str(script.script).lower()


def _has_prefix(address: str, prefix: str) -> bool:
    return address.strip().lower().startswith(f"{prefix}:")


class KaspaEngine:
    """Owns the RPC connection and turns a PAYMENT-SIGNATURE into a mempool-accepted payment."""

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: RpcClientFactory | None = None,
        connect_timeout_seconds: float | None = None,
        rpc_timeout_seconds: float | None = None,
        backoff_initial_seconds: float = DEFAULT_BACKOFF_INITIAL_SECONDS,
        backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
    ) -> None:
        self._settings = settings
        self._network_id = NetworkId(network_id_for(settings.kaspa_network))
        self._client_factory: RpcClientFactory = client_factory or self._default_client_factory
        self._connect_timeout = (
            settings.kaspa_connect_timeout_seconds if connect_timeout_seconds is None else connect_timeout_seconds
        )
        self._rpc_timeout = settings.kaspa_rpc_timeout_seconds if rpc_timeout_seconds is None else rpc_timeout_seconds
        self._backoff_initial = backoff_initial_seconds
        self._backoff_max = backoff_max_seconds
        self._client: RpcClientProtocol | None = None
        self._connect_lock = asyncio.Lock()
        self._supervisor: asyncio.Task[None] | None = None
        self._stopping = False

    # ------------------------------------------------------------------ lifecycle

    def _default_client_factory(self) -> RpcClientProtocol:
        network_id = self._network_id
        if self._settings.kaspa_rpc_url:
            return RpcClient(url=self._settings.kaspa_rpc_url, network_id=network_id)  # type: ignore[return-value]
        return RpcClient(resolver=Resolver(), network_id=network_id)  # type: ignore[return-value]

    async def start(self) -> None:
        """Connect (best effort) and launch the background reconnect supervisor. Async; idempotent."""
        if self._supervisor is not None and not self._supervisor.done():
            return
        self._stopping = False
        try:
            await self._connect_once()
        except RpcUnavailableError as exc:
            logger.warning("initial Kaspa RPC connect failed, supervisor will retry: %s", exc.message)
        self._supervisor = asyncio.create_task(self._supervise(), name="kaspa-engine-supervisor")

    async def stop(self) -> None:
        """Stop the supervisor and close the connection. Async; safe to call repeatedly."""
        self._stopping = True
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None:
            supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)
        client, self._client = self._client, None
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), self._rpc_timeout)
            except Exception as exc:
                logger.warning("Kaspa RPC disconnect failed: %s", exc)

    def is_connected(self) -> bool:
        client = self._client
        return client is not None and bool(client.is_connected)

    async def _connect_once(self) -> RpcClientProtocol:
        """Return a connected client, connecting under a lock so concurrent callers share one attempt."""
        async with self._connect_lock:
            client = self._client
            if client is not None and client.is_connected:
                return client
            if client is None:
                client = self._client_factory()
                self._client = client
            try:
                await asyncio.wait_for(
                    client.connect(
                        block_async_connect=True,
                        strategy="retry",
                        timeout_duration=int(self._connect_timeout * 1000),
                    ),
                    self._connect_timeout * 1.25,
                )
            except TimeoutError as exc:
                self._client = None
                raise RpcUnavailableError("Kaspa node connection timed out") from exc
            except Exception as exc:
                self._client = None
                raise RpcUnavailableError(f"Kaspa node connection failed: {exc}") from exc
            if not client.is_connected:
                self._client = None
                raise RpcUnavailableError("Kaspa node connection could not be established")
            logger.info("connected to Kaspa node on %s", self._settings.kaspa_network)
            return client

    async def _supervise(self) -> None:
        delay = self._backoff_initial
        while not self._stopping:
            if self.is_connected():
                delay = self._backoff_initial
                await asyncio.sleep(HEALTH_POLL_SECONDS)
                continue
            try:
                await self._connect_once()
                delay = self._backoff_initial
            except RpcUnavailableError as exc:
                logger.warning("Kaspa RPC reconnect failed (retry in %.1fs): %s", delay, exc.message)
                await asyncio.sleep(delay + random.uniform(0, delay / 4))
                delay = min(delay * 2, self._backoff_max)

    # ------------------------------------------------------------------ verification

    async def verify_and_submit(self, signature: PaymentSignature, required: PaymentRequired) -> VerifiedPayment:
        """Verify ``signature`` against ``required`` and submit it to the node.

        Raises ``PaymentVerificationError`` (``code`` in invalid_payment_header, underpaid,
        invalid_fee_split, wrong_network, tx_rejected). Settlement means mempool acceptance.
        """
        verified, tx = self.verify(signature, required)
        await self._submit(tx, verified.tx_id)
        return verified

    def verify(self, signature: PaymentSignature, required: PaymentRequired) -> tuple[VerifiedPayment, Transaction]:
        """Pure, network-free verification. Returns the verified payment and the parsed transaction."""
        self._check_network(signature, required)
        tx = self._parse_transaction(signature)
        tx_dict = tx.to_dict()
        input_values, input_scripts = self._read_inputs(tx_dict)
        self._check_payer(signature.payerAddress, input_scripts)
        merchant_paid, fee_paid = self._check_outputs(tx, required)
        self._check_mass(tx, input_values)
        return (
            VerifiedPayment(
                tx_id=tx.id,
                payer_address=signature.payerAddress,
                total_sompi=merchant_paid + fee_paid,
                merchant_sompi=merchant_paid,
                fee_sompi=fee_paid,
            ),
            tx,
        )

    def _check_network(self, signature: PaymentSignature, required: PaymentRequired) -> None:
        settings = self._settings
        if required.network != settings.kaspa_network:
            raise PaymentVerificationError(
                "wrong_network",
                f"payment network {required.network} does not match gateway network {settings.kaspa_network}",
            )
        prefix = NETWORK_ADDRESS_PREFIX[settings.kaspa_network]
        for label, address in (
            ("payTo", required.payTo),
            ("feeTo", required.platformFee.feeTo),
            ("payerAddress", signature.payerAddress),
        ):
            if not _has_prefix(address, prefix):
                raise PaymentVerificationError(
                    "wrong_network", f"{label} must use the '{prefix}:' prefix on {settings.kaspa_network}"
                )

    @staticmethod
    def _parse_transaction(signature: PaymentSignature) -> Transaction:
        try:
            raw = json.loads(signature.rawTx)
        except (ValueError, RecursionError) as exc:
            raise PaymentVerificationError("invalid_payment_header", "rawTx is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise PaymentVerificationError("invalid_payment_header", "rawTx must be a JSON object")
        try:
            tx = Transaction.from_dict(raw)
            # from_dict keeps the id field it was given; finalize() recomputes it from the content.
            tx.finalize()
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise PaymentVerificationError(
                "invalid_payment_header", f"rawTx is not a valid Kaspa transaction: {exc}"
            ) from exc
        if tx.id != signature.txId:
            raise PaymentVerificationError("invalid_payment_header", "txId does not match the transaction in rawTx")
        if not tx.inputs:
            raise PaymentVerificationError("invalid_payment_header", "transaction has no inputs")
        if tx.subnetwork != NATIVE_SUBNETWORK_ID or tx.gas != 0:
            raise PaymentVerificationError("invalid_payment_header", "only native-subnetwork transactions are accepted")
        return tx

    @staticmethod
    def _read_inputs(tx_dict: dict[str, Any]) -> tuple[list[int], set[tuple[int, str]]]:
        values: list[int] = []
        scripts: set[tuple[int, str]] = set()
        for index, entry in enumerate(tx_dict.get("inputs", [])):
            utxo = entry.get("utxo") if isinstance(entry, dict) else None
            if not isinstance(utxo, dict):
                raise PaymentVerificationError("invalid_payment_header", f"input {index} carries no UTXO entry")
            amount = utxo.get("amount")
            spk = utxo.get("scriptPublicKey")
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0 or not isinstance(spk, dict):
                raise PaymentVerificationError("invalid_payment_header", f"input {index} has a malformed UTXO entry")
            values.append(amount)
            scripts.add((int(spk.get("version", 0)), str(spk.get("script", "")).lower()))
        return values, scripts

    @staticmethod
    def _check_payer(payer_address: str, input_scripts: set[tuple[int, str]]) -> None:
        payer_script = _script_key(payer_address, what="payerAddress", code="invalid_payment_header")
        if payer_script not in input_scripts:
            raise PaymentVerificationError(
                "invalid_payment_header", "payerAddress does not own any input of the transaction"
            )

    def _check_outputs(self, tx: Transaction, required: PaymentRequired) -> tuple[int, int]:
        outputs = tx.outputs
        if len(outputs) < 2:
            raise PaymentVerificationError(
                "invalid_fee_split", "transaction must contain a merchant output and a platform fee output"
            )
        merchant_script = _script_key(required.payTo, what="payTo", code="invalid_fee_split")
        fee_script = _script_key(required.platformFee.feeTo, what="feeTo", code="invalid_fee_split")
        merchant_out, fee_out = outputs[0], outputs[1]
        expected_merchant = required.merchant_amount_sompi
        expected_fee = required.fee_amount_sompi
        floor = self._settings.min_output_sompi

        if (
            int(merchant_out.script_public_key.version),
            str(merchant_out.script_public_key.script).lower(),
        ) != merchant_script:
            raise PaymentVerificationError("invalid_fee_split", "output 0 must pay the merchant address")
        if (int(fee_out.script_public_key.version), str(fee_out.script_public_key.script).lower()) != fee_script:
            raise PaymentVerificationError("invalid_fee_split", "output 1 must pay the KasGate treasury address")
        if fee_out.value != expected_fee:
            raise PaymentVerificationError(
                "invalid_fee_split", f"platform fee output must be exactly {expected_fee} sompi, got {fee_out.value}"
            )
        if merchant_out.value < expected_merchant:
            raise PaymentVerificationError(
                "underpaid",
                f"merchant output is {expected_merchant - merchant_out.value} sompi short of {expected_merchant}",
            )
        if merchant_out.value != expected_merchant:
            raise PaymentVerificationError(
                "invalid_fee_split",
                f"merchant output must be exactly {expected_merchant} sompi, got {merchant_out.value}",
            )
        total = merchant_out.value + fee_out.value
        if total != int(required.amount):
            raise PaymentVerificationError(
                "underpaid", f"payment total {total} does not equal required amount {required.amount}"
            )
        minimum_fee = -(-total * PLATFORM_FEE_BPS // BPS_DENOMINATOR)
        if fee_out.value < minimum_fee:
            raise PaymentVerificationError(
                "invalid_fee_split",
                f"platform fee {fee_out.value} is below ceil(1.5%) of the payment total ({minimum_fee})",
            )
        for index, output in enumerate((merchant_out, fee_out)):
            if output.value < floor:
                raise PaymentVerificationError(
                    "underpaid", f"output {index} value {output.value} is below the minimum output of {floor} sompi"
                )
        return merchant_out.value, fee_out.value

    def _check_mass(self, tx: Transaction, input_values: list[int]) -> None:
        output_values = [int(output.value) for output in tx.outputs]
        if sum(input_values) < sum(output_values):
            raise PaymentVerificationError("tx_rejected", "transaction outputs exceed the declared input value")
        limit = maximum_standard_transaction_mass()
        try:
            storage_mass = calculate_storage_mass(self._network_id, input_values, output_values)
            total_mass = calculate_transaction_mass(self._network_id, tx)
        except Exception as exc:
            raise PaymentVerificationError(
                "invalid_payment_header", f"transaction mass could not be computed: {exc}"
            ) from exc
        if storage_mass is None:
            raise PaymentVerificationError(
                "invalid_payment_header", "storage mass is not computable for this transaction"
            )
        if storage_mass > limit or total_mass > limit:
            raise PaymentVerificationError(
                "tx_rejected", f"transaction mass {max(storage_mass, total_mass)} exceeds the standard maximum {limit}"
            )

    # ------------------------------------------------------------------ submission

    async def _submit(self, tx: Transaction, tx_id: str) -> None:
        """Submit ``tx``; one reconnect-and-retry on transport failure. Duplicate acceptance is success."""
        last_error: str = "unknown error"
        for attempt in range(2):
            try:
                client = await self._connect_once()
                await asyncio.wait_for(
                    client.submit_transaction({"transaction": tx, "allowOrphan": False}),
                    self._rpc_timeout,
                )
                return
            except PaymentVerificationError:
                raise
            except TimeoutError:
                last_error = "Kaspa node did not answer in time"
            except Exception as exc:
                message = str(exc)
                if self._is_duplicate(message, tx_id):
                    logger.info("transaction %s already known to the node; treating as settled", tx_id)
                    return
                if not self._is_transport_error(message):
                    raise PaymentVerificationError("tx_rejected", f"node rejected the transaction: {message}") from exc
                last_error = f"Kaspa node connection error: {message}"
            await self._drop_client()
            logger.warning("submit attempt %d for %s failed: %s", attempt + 1, tx_id, last_error)
        raise RpcUnavailableError(last_error)

    async def _drop_client(self) -> None:
        async with self._connect_lock:
            client, self._client = self._client, None
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), self._rpc_timeout)
            except Exception as exc:
                logger.debug("ignoring disconnect error while dropping client: %s", exc)

    @staticmethod
    def _is_duplicate(message: str, tx_id: str) -> bool:
        if not DUPLICATE_PATTERN.search(message):
            return False
        mentioned = set(TX_ID_PATTERN.findall(message.lower()))
        return not mentioned or tx_id in mentioned

    @staticmethod
    def _is_transport_error(message: str) -> bool:
        lowered = message.lower()
        if "rejected transaction" in lowered:
            return False
        return any(marker in lowered for marker in TRANSPORT_MARKERS)


engine = KaspaEngine(get_settings())
