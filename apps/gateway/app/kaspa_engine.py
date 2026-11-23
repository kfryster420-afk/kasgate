"""Kaspa RPC connection manager and dual-output UTXO payment verification engine (CONTRACT v2 / v2.1).

Pipeline used by the proxy, see ``KaspaEngine`` for signatures:

1. ``verify(signature, required)``: PURE and network-free. Safe to call before the replay lock.
2. ``await resolve_inputs(signature, tx)``: fetches every input's real UTXO entry from the node.
3. ``await settle(tx, verified, required)``: submits, then waits for finality per ``settings.settlement_mode``.

``verify_and_submit(signature, required)`` runs all three in order.

k402 ``kaspa-utxo`` (CONTRACT v2.9): ``await verify_k402(tx_id, offer)`` looks up a transaction the payer already
submitted and applies the same exact two-output rule (see ``check_k402_outputs``), without the payload or payerProof.

Verification contract (see docs/CONTRACTS.md):

* ``signature.nonce == required.nonce`` and ``tx.payload == schemas.payment_payload(nonce)``
  (``b"kasgate:v1:" + 32 hex``). The payload is covered by the transaction id, so it cannot be swapped.
* ``outputs[0]`` pays ``required.payTo`` EXACTLY ``required.merchant_amount_sompi``.
* ``outputs[1]`` pays ``required.platformFee.feeTo`` EXACTLY ``required.fee_amount_sompi``, and that fee is at least
  ``max(ceil(total * 150 / 10_000), MIN_OUTPUT_SOMPI)`` (CONTRACT v2.1).
* ``outputs[2:]`` are change only and are never counted towards the payment.

Error-code decisions:

* ``wrong_network``       network mismatch or an address prefix that does not belong to the network.
* ``invalid_payment_header`` malformed JSON/transaction, txId mismatch, nonce or payload mismatch, payer address not
                          P2PK Schnorr or not an input owner, payerProof invalid (CONTRACT v2.6), input UTXO
                          missing on the node or different from the embedded entry.
* ``underpaid``           ``outputs[0]`` pays the right script but strictly less than the merchant amount, or the
                          merchant output is below ``2 * MIN_OUTPUT_SOMPI`` (CONTRACT v2.3).
* ``invalid_fee_split``   missing ``outputs[1]``, wrong script, swapped order, ANY fee value other than the
                          exact fee (1 sompi short or over), or a merchant value above the exact amount.
* ``tx_rejected``         storage mass above the standard maximum, outputs above inputs, node rejection.
* ``rpc_unavailable``     the node is unreachable, timed out, or the tx was not accepted by the virtual chain within
                          ``settlement_timeout_seconds`` (``RpcUnavailableError``, ``retryable=True``). Re-submitting
                          the same transaction is idempotent.

Finality (``settings.settlement_mode``):

* ``accepted`` (default): the sink hash is read BEFORE submission, then ``get_virtual_chain_from_block(startHash=sink,
  includeAcceptedTransactionIds=True)`` is polled until the txId appears in an accepting chain block. Each poll is the
  full diff from the start hash, so a re-org that removes the accepting block also removes the txId from the result.
* ``mempool``: success once the node accepts the transaction into its mempool (testnet demos only).

A node reply of "already in the mempool" or "already accepted" for the SAME txId is success only because the payload
nonce was re-checked against ``required.nonce`` immediately before submission.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Final, Protocol, TypeVar

from kaspa import (
    Address,
    NetworkId,
    PublicKey,
    Resolver,
    RpcClient,
    ScriptPublicKey,
    Transaction,
    address_from_script_public_key,
    calculate_storage_mass,
    calculate_transaction_mass,
    maximum_standard_transaction_mass,
    pay_to_address_script,
    verify_message,
)

from app.config import (
    BPS_DENOMINATOR,
    NETWORK_ADDRESS_PREFIX,
    PLATFORM_FEE_BPS,
    Settings,
    get_settings,
)
from app.schemas import (
    K402UtxoOffer,
    PaymentRequired,
    PaymentSignature,
    k402_network_label,
    payer_proof_message,
    payment_payload,
)

logger = logging.getLogger("kasgate.kaspa_engine")

NETWORK_ID_BY_CONFIG: Final[dict[str, str]] = {
    "kaspa:testnet-10": "testnet-10",
    "kaspa:mainnet": "mainnet",
}
NETWORK_TYPE_BY_CONFIG: Final[dict[str, str]] = {
    "kaspa:testnet-10": "testnet",
    "kaspa:mainnet": "mainnet",
}

NATIVE_SUBNETWORK_ID: Final[str] = "0" * 40
TX_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")
HEX_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?:[0-9a-f]{2})*")
P2PK_SCHNORR_SCRIPT: Final[re.Pattern[str]] = re.compile(r"20([0-9a-f]{64})ac")
PAYER_PROOF_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{128}")
ALREADY_ACCEPTED_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"already been accepted|already accepted|was accepted by the consensus", re.IGNORECASE
)
DUPLICATE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"already in the mempool|already in mempool|already exists in the mempool|" + ALREADY_ACCEPTED_PATTERN.pattern,
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
DEFAULT_ACCEPTANCE_POLL_SECONDS: Final[float] = 0.5
HEALTH_POLL_SECONDS: Final[float] = 5.0
CONNECT_ATTEMPTS: Final[int] = 3
# CONTRACT v2.3: payments drawing from more than 16 payer addresses' UTXOs are rejected (invalid_payment_header).
MAX_LOOKUP_ADDRESSES: Final[int] = 16

T = TypeVar("T")
ScriptKey = tuple[int, str]
Outpoint = tuple[str, int]


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
class VerifiedPayment:
    tx_id: str
    payer_address: str | None  # None only for k402 kaspa-utxo, which proves no payer
    total_sompi: int
    merchant_sompi: int
    fee_sompi: int


@dataclass(frozen=True)
class NodeInputs:
    """Input UTXO entries as reported by the node, in transaction input order."""

    values: tuple[int, ...]
    scripts: tuple[ScriptKey, ...]
    storage_mass: int


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

    async def get_sink(self, request: dict[str, Any] | None = None) -> Any: ...

    async def get_virtual_chain_from_block(self, request: dict[str, Any]) -> Any: ...

    async def get_utxos_by_addresses(self, request: dict[str, Any]) -> Any: ...

    async def get_mempool_entries_by_addresses(self, request: dict[str, Any]) -> Any: ...


RpcClientFactory = Callable[[], RpcClientProtocol]


def network_id_for(network: str) -> str:
    try:
        return NETWORK_ID_BY_CONFIG[network]
    except KeyError as exc:
        raise ValueError(f"unsupported Kaspa network: {network}") from exc


def rpc_url_for(settings: Settings) -> str | None:
    """``KASPA_RPC_URL`` stripped; None (use the public Resolver) when unset, empty or whitespace only."""
    url = (settings.kaspa_rpc_url or "").strip()
    return url or None


def _script_key(address: str, *, what: str, code: str) -> ScriptKey:
    """Locking script of ``address`` as a comparable ``(version, script_hex)`` pair."""
    try:
        script = pay_to_address_script(Address(address))
    except Exception as exc:
        raise PaymentVerificationError(code, f"{what} is not a valid Kaspa address") from exc
    return int(script.version), str(script.script).lower()


def parse_script_key(value: object) -> ScriptKey | None:
    """Normalise a script public key from any wire form into ``(version, script_hex)``.

    Accepts ``{"version": int, "script": hex}`` (``Transaction.to_dict``), the RPC hex string with a 2-byte big-endian
    version prefix (``"0000" + script``, as returned by ``get_utxos_by_addresses`` and mempool entries) or an SDK
    ``ScriptPublicKey``. Returns None for anything else.
    """
    if isinstance(value, ScriptPublicKey):
        return int(value.version), str(value.script).lower()
    if isinstance(value, dict):
        version = value.get("version", 0)
        script = value.get("script")
        if isinstance(version, bool) or not isinstance(version, int) or not isinstance(script, str):
            return None
        script = script.lower()
        return (version, script) if script and HEX_PATTERN.fullmatch(script) else None
    if isinstance(value, str):
        lowered = value.lower()
        if len(lowered) <= 4 or not HEX_PATTERN.fullmatch(lowered):
            return None
        return int(lowered[:4], 16), lowered[4:]
    return None


def _has_prefix(address: str, prefix: str) -> bool:
    return address.strip().lower().startswith(f"{prefix}:")


def minimum_fee_sompi(total_sompi: int, min_output_sompi: int) -> int:
    """CONTRACT v2.1 floor for Output 1: ``max(ceil(total * 150 / 10_000), MIN_OUTPUT_SOMPI)``."""
    percentage = -(-total_sompi * PLATFORM_FEE_BPS // BPS_DENOMINATOR)
    return max(percentage, min_output_sompi)


OutputMap = dict[int, tuple[int, ScriptKey]]


def check_k402_outputs(tx_id: str, outputs: OutputMap, offer: K402UtxoOffer, min_output_sompi: int) -> VerifiedPayment:
    """CONTRACT v2.9 (k402 ``kaspa-utxo``): output 0 pays the merchant EXACTLY, output 1 pays the treasury EXACTLY.

    ``outputs`` maps output index -> ``(value, script)`` for every output of ``tx_id`` that is known: the whole
    transaction (mempool) or only the still-unspent outputs paying the merchant or the treasury (UTXO index). The
    check is by index AND by locking script: an output paying the treasury or the merchant at any other index (a
    reordered ``create_transactions`` result, a swap) is ``invalid_fee_split``. There is no payload or payerProof
    check here; unmodified k402 clients send an empty payload.
    """
    merchant_script = _script_key(offer.pay_to, what="pay_to", code="invalid_fee_split")
    fee_script = _script_key(offer.facilitator_fee.to, what="facilitator_fee.to", code="invalid_fee_split")
    if merchant_script == fee_script:
        raise PaymentVerificationError("invalid_fee_split", "the merchant and treasury outputs must be different")
    expected_merchant, expected_fee = offer.merchant_sompi, offer.fee_sompi

    def indices(script: ScriptKey) -> list[int]:
        return sorted(index for index, (_, out_script) in outputs.items() if out_script == script)

    merchant_at, fee_at = indices(merchant_script), indices(fee_script)
    fee_out = outputs.get(1)
    if fee_out is None or fee_out[1] != fee_script:
        if fee_at:
            raise PaymentVerificationError(
                "invalid_fee_split", f"the treasury output must be output 1, found at output {fee_at[0]}"
            )
        raise PaymentVerificationError("invalid_fee_split", "output 1 must pay the KasGate treasury address")
    merchant_out = outputs.get(0)
    if merchant_out is None or merchant_out[1] != merchant_script:
        if merchant_at:
            raise PaymentVerificationError(
                "invalid_fee_split", f"the merchant output must be output 0, found at output {merchant_at[0]}"
            )
        raise PaymentVerificationError("invalid_fee_split", "output 0 must pay the merchant address")
    fee_value, merchant_value = fee_out[0], merchant_out[0]
    if fee_value != expected_fee:
        raise PaymentVerificationError(
            "invalid_fee_split", f"treasury output must be exactly {expected_fee} sompi, got {fee_value}"
        )
    if merchant_value < expected_merchant:
        raise PaymentVerificationError(
            "underpaid", f"merchant output is {expected_merchant - merchant_value} sompi short of {expected_merchant}"
        )
    if merchant_value != expected_merchant:
        raise PaymentVerificationError(
            "invalid_fee_split", f"merchant output must be exactly {expected_merchant} sompi, got {merchant_value}"
        )
    total = merchant_value + fee_value
    minimum_fee = minimum_fee_sompi(total, min_output_sompi)
    if fee_value < minimum_fee:
        raise PaymentVerificationError(
            "invalid_fee_split",
            f"treasury output {fee_value} is below max(ceil(1.5%), MIN_OUTPUT_SOMPI) of the total ({minimum_fee})",
        )
    if merchant_value < 2 * min_output_sompi:
        raise PaymentVerificationError(
            "underpaid", f"merchant output {merchant_value} is below 2*MIN_OUTPUT_SOMPI ({2 * min_output_sompi})"
        )
    return VerifiedPayment(
        tx_id=tx_id, payer_address=None, total_sompi=total, merchant_sompi=merchant_value, fee_sompi=fee_value
    )


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


class KaspaEngine:
    """Owns the RPC connection and turns a PAYMENT-SIGNATURE into a settled payment.

    Public API (all raise ``PaymentVerificationError``; node problems raise its subclass ``RpcUnavailableError``):

    * ``verify(signature, required) -> tuple[VerifiedPayment, Transaction]``: pure, network-free.
    * ``async resolve_inputs(signature, tx) -> NodeInputs``: node UTXO lookup, payer attribution, mass on node values.
    * ``async settle(tx, verified, required) -> VerifiedPayment``: submit, then finality per ``settlement_mode``.
    * ``async verify_and_submit(signature, required) -> VerifiedPayment``: the three steps above in order.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client_factory: RpcClientFactory | None = None,
        connect_timeout_seconds: float | None = None,
        rpc_timeout_seconds: float | None = None,
        backoff_initial_seconds: float = DEFAULT_BACKOFF_INITIAL_SECONDS,
        backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
        acceptance_poll_seconds: float = DEFAULT_ACCEPTANCE_POLL_SECONDS,
    ) -> None:
        self._settings = settings
        self._network_id = NetworkId(network_id_for(settings.kaspa_network))
        self._network_type = NETWORK_TYPE_BY_CONFIG[settings.kaspa_network]
        self._client_factory: RpcClientFactory = client_factory or self._default_client_factory
        self._connect_timeout = (
            settings.kaspa_connect_timeout_seconds if connect_timeout_seconds is None else connect_timeout_seconds
        )
        self._rpc_timeout = settings.kaspa_rpc_timeout_seconds if rpc_timeout_seconds is None else rpc_timeout_seconds
        self._backoff_initial = backoff_initial_seconds
        self._backoff_max = backoff_max_seconds
        self._acceptance_poll = acceptance_poll_seconds
        self._client: RpcClientProtocol | None = None
        self._connect_lock = asyncio.Lock()
        self._supervisor: asyncio.Task[None] | None = None
        self._stopping = False

    # ------------------------------------------------------------------ lifecycle

    def _default_client_factory(self) -> RpcClientProtocol:
        """Fixed wRPC Borsh URL (our own kaspad, e.g. ``ws://kaspad:17210``) when set, else the public Resolver."""
        url = rpc_url_for(self._settings)
        if url is not None:
            return RpcClient(url=url, encoding="borsh", network_id=self._network_id)  # type: ignore[return-value]
        return RpcClient(resolver=Resolver(), encoding="borsh", network_id=self._network_id)  # type: ignore[return-value]

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
        """Return a connected client, connecting under a lock so concurrent callers share one attempt.

        The connect budget (``kaspa_connect_timeout_seconds``) is split over ``CONNECT_ATTEMPTS`` attempts, each
        with a FRESH client after a failure: the public Resolver sometimes hands out a node that never answers
        (seen live on testnet-10 for about half of the picks), and a new client re-resolves to another node.
        """
        async with self._connect_lock:
            client = self._client
            if client is not None and client.is_connected:
                return client
            per_attempt = self._connect_timeout / CONNECT_ATTEMPTS
            last_error = "Kaspa node connection could not be established"
            for attempt in range(CONNECT_ATTEMPTS):
                if client is None:
                    client = self._client_factory()
                self._client = client
                try:
                    await asyncio.wait_for(
                        client.connect(
                            block_async_connect=True,
                            strategy="fallback",
                            timeout_duration=max(1, int(per_attempt * 1000)),
                        ),
                        per_attempt * 1.25,
                    )
                except TimeoutError:
                    last_error = "Kaspa node connection timed out"
                except Exception as exc:
                    last_error = f"Kaspa node connection failed: {exc}"
                else:
                    if client.is_connected:
                        logger.info("connected to Kaspa node on %s", self._settings.kaspa_network)
                        return client
                    last_error = "Kaspa node connection could not be established"
                logger.warning("Kaspa connect attempt %d/%d failed: %s", attempt + 1, CONNECT_ATTEMPTS, last_error)
                self._client = None
                client = None
            raise RpcUnavailableError(last_error)

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

    # ------------------------------------------------------------------ pipeline

    async def verify_and_submit(self, signature: PaymentSignature, required: PaymentRequired) -> VerifiedPayment:
        """Full pipeline: ``verify`` -> ``resolve_inputs`` -> ``settle``."""
        verified, tx = self.verify(signature, required)
        await self.resolve_inputs(signature, tx)
        return await self.settle(tx, verified, required)

    def verify(self, signature: PaymentSignature, required: PaymentRequired) -> tuple[VerifiedPayment, Transaction]:
        """Pure, network-free verification. Returns the verified payment and the parsed, re-finalised transaction.

        Input amounts used here are the ones embedded in ``rawTx``; ``resolve_inputs`` replaces them with node data.
        """
        self._check_network(signature, required)
        tx = self._parse_transaction(signature)
        self._check_payload(tx, signature.nonce, required.nonce)
        self._check_payer_proof(signature)
        input_values, input_scripts = self._read_inputs(tx.to_dict())
        self._check_payer(signature.payerAddress, set(input_scripts))
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

    async def resolve_inputs(self, signature: PaymentSignature, tx: Transaction) -> NodeInputs:
        """Fetch each input's UTXO entry from the node and re-check payer attribution and mass on node values.

        Looks up the confirmed UTXO set (``get_utxos_by_addresses``) and unconfirmed outputs paying the same addresses
        (``get_mempool_entries_by_addresses``, receiving side). The addresses queried are ``payerAddress`` plus the
        owners of the embedded input scripts. Missing or mismatching entries raise ``invalid_payment_header``; an
        unreachable or failing node raises ``RpcUnavailableError``.
        """
        tx_dict = tx.to_dict()
        embedded_values, embedded_scripts = self._read_inputs(tx_dict)
        outpoints = self._read_outpoints(tx_dict)
        addresses = self._lookup_addresses(signature.payerAddress, embedded_scripts)

        utxo_response = await self._rpc(
            "get_utxos_by_addresses", lambda client: client.get_utxos_by_addresses({"addresses": addresses})
        )
        mempool_response = await self._rpc(
            "get_mempool_entries_by_addresses",
            lambda client: client.get_mempool_entries_by_addresses(
                {"addresses": addresses, "includeOrphanPool": False, "filterTransactionPool": False}
            ),
        )
        known = self._index_node_utxos(utxo_response, mempool_response)

        values: list[int] = []
        scripts: list[ScriptKey] = []
        for index, outpoint in enumerate(outpoints):
            entry = known.get(outpoint)
            if entry is None:
                raise PaymentVerificationError(
                    "invalid_payment_header",
                    f"input {index} spends {outpoint[0]}:{outpoint[1]}, which the node does not know as unspent",
                )
            node_value, node_script = entry
            if node_value != embedded_values[index] or node_script != embedded_scripts[index]:
                raise PaymentVerificationError(
                    "invalid_payment_header", f"input {index} UTXO entry does not match the node's entry"
                )
            values.append(node_value)
            scripts.append(node_script)

        self._check_payer(signature.payerAddress, set(scripts))
        storage_mass = self._check_mass(tx, values)
        return NodeInputs(values=tuple(values), scripts=tuple(scripts), storage_mass=storage_mass)

    async def settle(self, tx: Transaction, verified: VerifiedPayment, required: PaymentRequired) -> VerifiedPayment:
        """Submit ``tx`` and wait for finality according to ``settings.settlement_mode``.

        The payload nonce is re-checked against ``required.nonce`` first, which is what makes a duplicate
        ("already in mempool/accepted") reply safe to treat as success.
        """
        tx.finalize()
        if tx.id != verified.tx_id:
            raise PaymentVerificationError("invalid_payment_header", "transaction does not match the verified payment")
        self._check_payload(tx, required.nonce, required.nonce)
        accepted_mode = self._settings.settlement_mode == "accepted"
        start_hash = await self._sink_hash() if accepted_mode else ""
        already_accepted = await self._submit(tx, verified.tx_id)
        if accepted_mode and not already_accepted:
            await self._wait_for_acceptance(verified.tx_id, start_hash)
        return verified

    # ------------------------------------------------------------------ k402 kaspa-utxo (CONTRACT v2.9)

    async def verify_k402(self, tx_id: str, offer: K402UtxoOffer) -> VerifiedPayment:
        """Find the k402 payer's own transaction ``tx_id`` on the node and check its outputs against ``offer``.

        The payer has already submitted the transaction, so nothing is submitted here. Lookup order:

        1. ``accepted`` mode: read the sink hash FIRST (the acceptance poll starts there).
        2. Mempool entries received by the merchant or treasury address: the full transaction, so every output is
           checked by index and script. In ``accepted`` mode the virtual chain is then polled until it accepts
           ``tx_id``; if that times out, the UTXO index is consulted once (it may have been accepted at the sink).
        3. Otherwise the UTXO index (``get_utxos_by_addresses`` for the merchant and treasury addresses), which
           holds only outputs of accepted transactions.

        Raises ``payment_not_found`` (the payer should retry the same header shortly) when the transaction is not
        visible yet, or a split error from ``check_k402_outputs``. A spent merchant or treasury output that is no
        longer in the mempool or UTXO index cannot be checked and is refused, never assumed.
        """
        self._check_k402_network(offer)
        floor = self._settings.min_output_sompi
        accepted_mode = self._settings.settlement_mode == "accepted"
        addresses = sorted({offer.pay_to.lower(), offer.facilitator_fee.to.lower()})
        start_hash = await self._sink_hash() if accepted_mode else ""
        mempool_response = await self._rpc(
            "get_mempool_entries_by_addresses",
            lambda client: client.get_mempool_entries_by_addresses(
                {"addresses": addresses, "includeOrphanPool": False, "filterTransactionPool": False}
            ),
        )
        in_mempool = self._k402_mempool_outputs(mempool_response, tx_id)
        if in_mempool is not None:
            verified = check_k402_outputs(tx_id, in_mempool, offer, floor)
            if accepted_mode:
                try:
                    await self._wait_for_acceptance(tx_id, start_hash)
                except RpcUnavailableError as exc:
                    if not await self._k402_utxo_outputs(addresses, tx_id):
                        raise PaymentVerificationError(
                            "payment_not_found", f"transaction {tx_id} is not accepted yet; retry shortly"
                        ) from exc
            return verified
        accepted_outputs = await self._k402_utxo_outputs(addresses, tx_id)
        if not accepted_outputs:
            raise PaymentVerificationError(
                "payment_not_found",
                f"transaction {tx_id} pays neither the merchant nor the treasury on this node yet; retry shortly",
            )
        return check_k402_outputs(tx_id, accepted_outputs, offer, floor)

    def _check_k402_network(self, offer: K402UtxoOffer) -> None:
        settings = self._settings
        expected = k402_network_label(settings.kaspa_network)
        if offer.network != expected:
            raise PaymentVerificationError(
                "wrong_network", f"k402 offer network {offer.network} does not match gateway network {expected}"
            )
        prefix = NETWORK_ADDRESS_PREFIX[settings.kaspa_network]
        for label, address in (("pay_to", offer.pay_to), ("facilitator_fee.to", offer.facilitator_fee.to)):
            if not _has_prefix(address, prefix):
                raise PaymentVerificationError(
                    "wrong_network", f"{label} must use the '{prefix}:' prefix on {settings.kaspa_network}"
                )

    @staticmethod
    def _k402_mempool_outputs(response: object, tx_id: str) -> OutputMap | None:
        """Every output of ``tx_id`` from a ``get_mempool_entries_by_addresses`` reply, or None when it is absent."""
        for by_address in _as_list(_as_dict(response).get("entries")):
            for mempool_entry in _as_list(_as_dict(by_address).get("receiving")):
                transaction = _as_dict(_as_dict(mempool_entry).get("transaction"))
                entry_id = _as_dict(transaction.get("verboseData")).get("transactionId")
                if not isinstance(entry_id, str) or entry_id.lower() != tx_id:
                    continue
                outputs: OutputMap = {}
                for index, output in enumerate(_as_list(transaction.get("outputs"))):
                    output_dict = _as_dict(output)
                    value = output_dict.get("value")
                    script = parse_script_key(output_dict.get("scriptPublicKey"))
                    if isinstance(value, bool) or not isinstance(value, int) or script is None:
                        raise PaymentVerificationError(
                            "invalid_payment_header", f"output {index} of {tx_id} is malformed in the node's reply"
                        )
                    outputs[index] = (value, script)
                return outputs
        return None

    async def _k402_utxo_outputs(self, addresses: list[str], tx_id: str) -> OutputMap:
        """Unspent outputs of ``tx_id`` that pay one of ``addresses``, from the node's UTXO index (accepted only)."""
        response = await self._rpc(
            "get_utxos_by_addresses", lambda client: client.get_utxos_by_addresses({"addresses": addresses})
        )
        outputs: OutputMap = {}
        for entry in _as_list(_as_dict(response).get("entries")):
            entry_dict = _as_dict(entry)
            outpoint = _as_dict(entry_dict.get("outpoint"))
            utxo = _as_dict(entry_dict.get("utxoEntry"))
            entry_tx, index, amount = outpoint.get("transactionId"), outpoint.get("index"), utxo.get("amount")
            if not isinstance(entry_tx, str) or entry_tx.lower() != tx_id:
                continue
            script = parse_script_key(utxo.get("scriptPublicKey"))
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or isinstance(amount, bool)
                or not isinstance(amount, int)
                or script is None
            ):
                raise PaymentVerificationError(
                    "invalid_payment_header", f"a UTXO of {tx_id} is malformed in the node's reply"
                )
            outputs[index] = (amount, script)
        return outputs

    # ------------------------------------------------------------------ pure checks

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
            # from_dict keeps the id field it was given; finalize() recomputes it from the content (payload included).
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
    def _check_payload(tx: Transaction, signature_nonce: str, required_nonce: str) -> None:
        if signature_nonce != required_nonce:
            raise PaymentVerificationError(
                "invalid_payment_header", "PAYMENT-SIGNATURE nonce does not match the PAYMENT-REQUIRED nonce"
            )
        actual = str(tx.payload or "").lower()
        if not actual:
            raise PaymentVerificationError("invalid_payment_header", "transaction payload is missing the payment nonce")
        if actual != payment_payload(required_nonce).hex():
            raise PaymentVerificationError(
                "invalid_payment_header", "transaction payload does not bind this payment's nonce"
            )

    @staticmethod
    def _check_payer_proof(signature: PaymentSignature) -> None:
        """CONTRACT v2.6: ``payerProof`` is a Schnorr signature by the payerAddress key over
        ``payer_proof_message(nonce, txId)``, so someone who only saw the tx in the mempool cannot claim it."""
        version, script = _script_key(signature.payerAddress, what="payerAddress", code="invalid_payment_header")
        match = P2PK_SCHNORR_SCRIPT.fullmatch(script)
        if version != 0 or match is None:
            raise PaymentVerificationError(
                "invalid_payment_header", "payerAddress must be a P2PK Schnorr address (script 20 <x-only key> ac)"
            )
        proof = str(signature.payerProof).lower()
        if not PAYER_PROOF_PATTERN.fullmatch(proof):
            raise PaymentVerificationError("invalid_payment_header", "payerProof must be a 128-hex Schnorr signature")
        message = payer_proof_message(signature.nonce, signature.txId)
        try:
            valid = bool(verify_message(message, proof, PublicKey(match.group(1))))
        except Exception as exc:
            raise PaymentVerificationError("invalid_payment_header", "payerProof is not a valid signature") from exc
        if not valid:
            raise PaymentVerificationError(
                "invalid_payment_header", "payerProof is not a signature by payerAddress over this nonce and txId"
            )

    @staticmethod
    def _read_inputs(tx_dict: dict[str, Any]) -> tuple[list[int], list[ScriptKey]]:
        values: list[int] = []
        scripts: list[ScriptKey] = []
        for index, entry in enumerate(_as_list(tx_dict.get("inputs"))):
            utxo = entry.get("utxo") if isinstance(entry, dict) else None
            if not isinstance(utxo, dict):
                raise PaymentVerificationError("invalid_payment_header", f"input {index} carries no UTXO entry")
            amount = utxo.get("amount")
            script = parse_script_key(utxo.get("scriptPublicKey"))
            if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0 or script is None:
                raise PaymentVerificationError("invalid_payment_header", f"input {index} has a malformed UTXO entry")
            values.append(amount)
            scripts.append(script)
        return values, scripts

    @staticmethod
    def _read_outpoints(tx_dict: dict[str, Any]) -> list[Outpoint]:
        outpoints: list[Outpoint] = []
        for index, entry in enumerate(_as_list(tx_dict.get("inputs"))):
            previous = _as_dict(_as_dict(entry).get("previousOutpoint"))
            tx_id = previous.get("transactionId")
            out_index = previous.get("index")
            if (
                not isinstance(tx_id, str)
                or not TX_ID_PATTERN.fullmatch(tx_id.lower())
                or isinstance(out_index, bool)
                or not isinstance(out_index, int)
            ):
                raise PaymentVerificationError("invalid_payment_header", f"input {index} has a malformed outpoint")
            outpoints.append((tx_id.lower(), out_index))
        if len(set(outpoints)) != len(outpoints):
            raise PaymentVerificationError("invalid_payment_header", "transaction spends the same outpoint twice")
        return outpoints

    @staticmethod
    def _check_payer(payer_address: str, input_scripts: set[ScriptKey]) -> None:
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

        if parse_script_key(merchant_out.script_public_key) != merchant_script:
            raise PaymentVerificationError("invalid_fee_split", "output 0 must pay the merchant address")
        if parse_script_key(fee_out.script_public_key) != fee_script:
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
        minimum_fee = minimum_fee_sompi(total, floor)
        if fee_out.value < minimum_fee:
            raise PaymentVerificationError(
                "invalid_fee_split",
                f"platform fee {fee_out.value} is below max(ceil(1.5%), MIN_OUTPUT_SOMPI) of the payment total "
                f"({minimum_fee})",
            )
        merchant_floor = 2 * floor
        if merchant_out.value < merchant_floor:
            raise PaymentVerificationError(
                "underpaid",
                f"merchant output {merchant_out.value} is below 2*MIN_OUTPUT_SOMPI ({merchant_floor} sompi), "
                "the minimum merchant output",
            )
        return merchant_out.value, fee_out.value

    def _check_mass(self, tx: Transaction, input_values: list[int]) -> int:
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
        return int(storage_mass)

    # ------------------------------------------------------------------ node input lookup

    def _lookup_addresses(self, payer_address: str, scripts: Iterable[ScriptKey]) -> list[str]:
        addresses = {payer_address.strip().lower()}
        for version, script in scripts:
            try:
                # The stub types the script as the opaque `Binary` class; the binding accepts bytes.
                spk = ScriptPublicKey(version, bytes.fromhex(script))  # type: ignore[arg-type]
                owner = address_from_script_public_key(spk, self._network_type)
            except Exception as exc:
                raise PaymentVerificationError(
                    "invalid_payment_header", "an input is locked by a script with no standard address"
                ) from exc
            addresses.add(owner.to_string().lower())
        if len(addresses) > MAX_LOOKUP_ADDRESSES:
            raise PaymentVerificationError(
                "invalid_payment_header", f"payment draws from more than {MAX_LOOKUP_ADDRESSES} payer addresses' UTXOs"
            )
        return sorted(addresses)

    @staticmethod
    def _index_node_utxos(utxo_response: object, mempool_response: object) -> dict[Outpoint, tuple[int, ScriptKey]]:
        """Map outpoint -> (amount, script) from the confirmed UTXO set plus unconfirmed mempool outputs."""
        known: dict[Outpoint, tuple[int, ScriptKey]] = {}
        for entry in _as_list(_as_dict(utxo_response).get("entries")):
            entry_dict = _as_dict(entry)
            outpoint = _as_dict(entry_dict.get("outpoint"))
            utxo = _as_dict(entry_dict.get("utxoEntry"))
            tx_id, index, amount = outpoint.get("transactionId"), outpoint.get("index"), utxo.get("amount")
            script = parse_script_key(utxo.get("scriptPublicKey"))
            if isinstance(tx_id, str) and isinstance(index, int) and isinstance(amount, int) and script is not None:
                known[(tx_id.lower(), index)] = (amount, script)
        for by_address in _as_list(_as_dict(mempool_response).get("entries")):
            for mempool_entry in _as_list(_as_dict(by_address).get("receiving")):
                transaction = _as_dict(_as_dict(mempool_entry).get("transaction"))
                tx_id = _as_dict(transaction.get("verboseData")).get("transactionId")
                if not isinstance(tx_id, str):
                    continue
                for index, output in enumerate(_as_list(transaction.get("outputs"))):
                    output_dict = _as_dict(output)
                    value = output_dict.get("value")
                    script = parse_script_key(output_dict.get("scriptPublicKey"))
                    if isinstance(value, int) and script is not None:
                        known.setdefault((tx_id.lower(), index), (value, script))
        return known

    # ------------------------------------------------------------------ RPC plumbing

    async def _rpc(self, name: str, call: Callable[[RpcClientProtocol], Awaitable[T]]) -> T:
        """Run a read-only RPC call with one reconnect-and-retry. Any failure is ``RpcUnavailableError``."""
        last_error = "unknown error"
        for attempt in range(2):
            client = await self._connect_once()
            try:
                return await asyncio.wait_for(call(client), self._rpc_timeout)
            except TimeoutError:
                last_error = f"Kaspa node did not answer {name} in time"
            except Exception as exc:
                message = str(exc)
                if not self._is_transport_error(message):
                    raise RpcUnavailableError(f"Kaspa node could not serve {name}: {message}") from exc
                last_error = f"Kaspa node connection error during {name}: {message}"
            await self._drop_client()
            logger.warning("%s attempt %d failed: %s", name, attempt + 1, last_error)
        raise RpcUnavailableError(last_error)

    async def _sink_hash(self) -> str:
        response = await self._rpc("get_sink", lambda client: client.get_sink())
        sink = _as_dict(response).get("sink")
        if not isinstance(sink, str) or not TX_ID_PATTERN.fullmatch(sink.lower()):
            raise RpcUnavailableError("Kaspa node returned no sink hash")
        return sink.lower()

    async def _wait_for_acceptance(self, tx_id: str, start_hash: str) -> None:
        """Poll the virtual chain from ``start_hash`` until ``tx_id`` is accepted or the settlement timeout passes."""
        loop = asyncio.get_running_loop()
        timeout = self._settings.settlement_timeout_seconds
        deadline = loop.time() + timeout
        request = {"startHash": start_hash, "includeAcceptedTransactionIds": True, "minConfirmationCount": None}
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                client = await asyncio.wait_for(self._connect_once(), remaining)
                response = await asyncio.wait_for(
                    client.get_virtual_chain_from_block(request), min(self._rpc_timeout, remaining)
                )
            except (TimeoutError, RpcUnavailableError) as exc:
                logger.warning("virtual chain poll for %s failed: %s", tx_id, exc)
                response = None
            except Exception as exc:
                logger.warning("virtual chain poll for %s failed: %s", tx_id, exc)
                if self._is_transport_error(str(exc)):
                    await self._drop_client()
                response = None
            if response is not None and self._is_accepted(response, tx_id):
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(self._acceptance_poll, remaining))
        raise RpcUnavailableError(f"transaction {tx_id} was not accepted by the virtual chain within {timeout:g}s")

    @staticmethod
    def _is_accepted(response: object, tx_id: str) -> bool:
        for block in _as_list(_as_dict(response).get("acceptedTransactionIds")):
            ids = _as_list(_as_dict(block).get("acceptedTransactionIds"))
            if any(isinstance(item, str) and item.lower() == tx_id for item in ids):
                return True
        return False

    async def _submit(self, tx: Transaction, tx_id: str) -> bool:
        """Submit ``tx``; one reconnect-and-retry on transport failure.

        Returns True when the node reports the transaction as already accepted by consensus, False otherwise.
        A duplicate for the same txId is success; any other node rejection is ``tx_rejected``.
        """
        last_error: str = "unknown error"
        for attempt in range(2):
            try:
                client = await self._connect_once()
                await asyncio.wait_for(
                    client.submit_transaction({"transaction": tx, "allowOrphan": False}),
                    self._rpc_timeout,
                )
                return False
            except PaymentVerificationError:
                raise
            except TimeoutError:
                last_error = "Kaspa node did not answer in time"
            except Exception as exc:
                message = str(exc)
                if self._is_duplicate(message, tx_id):
                    logger.info("transaction %s already known to the node with a matching nonce", tx_id)
                    return bool(ALREADY_ACCEPTED_PATTERN.search(message))
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
