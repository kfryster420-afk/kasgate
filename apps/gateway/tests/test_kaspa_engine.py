from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from kaspa import (
    Address,
    PaymentOutput,
    PrivateKey,
    Transaction,
    UtxoEntry,
    UtxoEntryReference,
    create_transaction,
    pay_to_address_script,
    sign_transaction,
)

_TREASURY_KEY = PrivateKey("02" * 32)
os.environ.setdefault("KASGATE_TREASURY_ADDRESS", _TREASURY_KEY.to_address("testnet").to_string())
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://kasgate:kasgate@localhost:5432/kasgate")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("ENCRYPTION_KEY", "A" * 43 + "=")
os.environ.setdefault("KASPA_NETWORK", "kaspa:testnet-10")

from app.config import BPS_DENOMINATOR, PLATFORM_FEE_BPS, SOMPI_PER_KAS, Settings, get_settings  # noqa: E402
from app.kaspa_engine import (  # noqa: E402
    KaspaEngine,
    NodeInputs,
    PaymentVerificationError,
    RpcUnavailableError,
    VerifiedPayment,
    minimum_fee_sompi,
    parse_script_key,
)
from app.schemas import (  # noqa: E402
    PaymentRequired,
    PaymentSignature,
    PlatformFee,
    compute_fee_split,
    min_price_sompi,
    payment_payload,
)

PRICE = 20 * SOMPI_PER_KAS
SPLIT = compute_fee_split(PRICE)
MERCHANT_KEY = PrivateKey("01" * 32)
PAYER_KEY = PrivateKey("03" * 32)
OTHER_KEY = PrivateKey("04" * 32)
INPUT_AMOUNT = 30 * SOMPI_PER_KAS
NETWORK_FEE = 100_000
NONCE = "0123456789abcdef0123456789abcdef"
OTHER_NONCE = "fedcba9876543210fedcba9876543210"
EXPIRES_AT = datetime.now(UTC) + timedelta(hours=1)

# Outpoint -> RPC `get_utxos_by_addresses` entry for every UTXO a test transaction spends ("the node's UTXO set").
NODE_UTXOS: dict[tuple[str, int], dict[str, Any]] = {}
_OUTPOINT_COUNTER = [0]


def address_of(key: PrivateKey) -> Address:
    return key.to_address("testnet")


def build_required(price: int = PRICE, nonce: str = NONCE) -> PaymentRequired:
    """A schema-valid PAYMENT-REQUIRED whose treasury is a real address (the test treasury key)."""
    split = compute_fee_split(price)
    return PaymentRequired(
        nonce=nonce,
        expiresAt=EXPIRES_AT,
        network=get_settings().kaspa_network,
        amount=str(split.total_sompi),
        payTo=address_of(MERCHANT_KEY).to_string(),
        platformFee=PlatformFee(feeTo=address_of(_TREASURY_KEY).to_string(), feeAmount=str(split.fee_sompi)),
    )


def build_unchecked_required(amount: int, fee: int) -> PaymentRequired:
    """PAYMENT-REQUIRED with an arbitrary fee, bypassing the schema split validator."""
    return PaymentRequired.model_construct(
        x402Version=2,
        scheme="kaspa-exact-v1",
        network=get_settings().kaspa_network,
        asset="KAS",
        amount=str(amount),
        payTo=address_of(MERCHANT_KEY).to_string(),
        platformFee=PlatformFee.model_construct(feeTo=address_of(_TREASURY_KEY).to_string(), feeAmount=str(fee)),
        nonce=NONCE,
        expiresAt=EXPIRES_AT,
    )


def fee_address(required: PaymentRequired) -> Address:
    return Address(required.platformFee.feeTo)


def node_utxo_entry(address: str, tx_id: str, index: int, amount: int, script_hex: str) -> dict[str, Any]:
    """An entry exactly as `RpcClient.get_utxos_by_addresses` returns it (script = 2-byte BE version + script)."""
    return {
        "address": address,
        "outpoint": {"transactionId": tx_id, "index": index},
        "utxoEntry": {
            "amount": amount,
            "scriptPublicKey": "0000" + script_hex,
            "blockDaaScore": 1,
            "isCoinbase": False,
            "covenantId": None,
        },
    }


def signed_transaction(
    outputs: list[tuple[Address, int]],
    *,
    payer: PrivateKey = PAYER_KEY,
    input_amounts: tuple[int, ...] = (INPUT_AMOUNT,),
    payload: bytes | None = payment_payload(NONCE),
    register: bool = True,
) -> Transaction:
    """Sign a real testnet transaction spending fresh outpoints; ``register`` adds them to ``NODE_UTXOS``."""
    payer_address = address_of(payer)
    script_hex = str(pay_to_address_script(payer_address).script)
    entries: list[UtxoEntryReference] = []
    for amount in input_amounts:
        _OUTPOINT_COUNTER[0] += 1
        tx_id = f"{_OUTPOINT_COUNTER[0]:064x}"
        entry = UtxoEntry.from_dict(
            {
                "address": payer_address.to_string(),
                "outpoint": {"transactionId": tx_id, "index": 0},
                "amount": amount,
                "scriptPublicKey": {"version": 0, "script": script_hex},
                "blockDaaScore": 1,
                "isCoinbase": False,
                "covenantId": None,
            }
        )
        entries.append(UtxoEntryReference.from_dict(entry.to_dict()))
        if register:
            NODE_UTXOS[(tx_id, 0)] = node_utxo_entry(payer_address.to_string(), tx_id, 0, amount, script_hex)
    payments = [PaymentOutput(address, amount) for address, amount in outputs]
    # The SDK stub types `outputs` as the opaque `Outputs` class, but the binding accepts a list of PaymentOutput.
    unsigned = create_transaction(entries, payments, 0, payload)  # type: ignore[arg-type]
    return sign_transaction(unsigned, [payer], False)


def signature_for(
    tx: Transaction,
    *,
    tx_id: str | None = None,
    raw: str | None = None,
    payer: PrivateKey = PAYER_KEY,
    nonce: str = NONCE,
) -> PaymentSignature:
    return PaymentSignature(
        txId=tx_id or tx.id,
        rawTx=raw if raw is not None else json.dumps(tx.to_dict()),
        payerAddress=address_of(payer).to_string(),
        nonce=nonce,
    )


def standard_outputs(
    required: PaymentRequired, *, change: int = INPUT_AMOUNT - PRICE - NETWORK_FEE
) -> list[tuple[Address, int]]:
    return [
        (Address(required.payTo), required.merchant_amount_sompi),
        (fee_address(required), required.fee_amount_sompi),
        (address_of(PAYER_KEY), change),
    ]


@dataclass
class FakeClient:
    submit_results: list[BaseException | None] = field(default_factory=list)
    connect_results: list[BaseException | None] = field(default_factory=list)
    submit_delay: float = 0.0
    connect_delay: float = 0.0
    connected: bool = False
    submitted: list[Any] = field(default_factory=list)
    connect_calls: int = 0
    disconnect_calls: int = 0
    utxos: dict[tuple[str, int], dict[str, Any]] | None = None
    mempool_receiving: list[dict[str, Any]] = field(default_factory=list)
    utxo_error: BaseException | None = None
    utxo_delay: float = 0.0
    utxo_calls: list[list[str]] = field(default_factory=list)
    sink: str = "aa" * 32
    sink_error: BaseException | None = None
    accept: bool = True
    accept_after_polls: int = 0
    chain_error: BaseException | None = None
    chain_requests: list[dict[str, Any]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def connect(
        self,
        block_async_connect: bool | None = None,
        strategy: str | None = None,
        url: str | None = None,
        timeout_duration: int | None = None,
        retry_interval: int | None = None,
    ) -> None:
        self.connect_calls += 1
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        outcome = self.connect_results.pop(0) if self.connect_results else None
        if outcome is not None:
            raise outcome
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False

    async def get_utxos_by_addresses(self, request: dict[str, Any]) -> dict[str, Any]:
        self.events.append("utxos")
        self.utxo_calls.append(list(request["addresses"]))
        if self.utxo_delay:
            await asyncio.sleep(self.utxo_delay)
        if self.utxo_error is not None:
            raise self.utxo_error
        source = NODE_UTXOS if self.utxos is None else self.utxos
        wanted = set(request["addresses"])
        return {"entries": [entry for entry in source.values() if entry["address"] in wanted]}

    async def get_mempool_entries_by_addresses(self, request: dict[str, Any]) -> dict[str, Any]:
        self.events.append("mempool")
        return {
            "entries": [
                {"address": address, "sending": [], "receiving": self.mempool_receiving}
                for address in request["addresses"]
            ]
        }

    async def get_sink(self, request: dict[str, Any] | None = None) -> dict[str, str]:
        self.events.append("sink")
        if self.sink_error is not None:
            raise self.sink_error
        return {"sink": self.sink}

    async def get_virtual_chain_from_block(self, request: dict[str, Any]) -> dict[str, Any]:
        self.events.append("chain")
        self.chain_requests.append(request)
        if self.chain_error is not None:
            raise self.chain_error
        accepted: list[str] = []
        if self.accept and len(self.chain_requests) > self.accept_after_polls:
            accepted = [item["transaction"].id for item in self.submitted]
        return {
            "removedChainBlockHashes": [],
            "addedChainBlockHashes": ["bb" * 32],
            "acceptedTransactionIds": [
                {"acceptingBlockHash": "bb" * 32, "acceptedTransactionIds": ["cc" * 32, *accepted]}
            ],
        }

    async def submit_transaction(self, request: dict[str, Any]) -> dict[str, str]:
        self.events.append("submit")
        self.submitted.append(request)
        if self.submit_delay:
            await asyncio.sleep(self.submit_delay)
        outcome = self.submit_results.pop(0) if self.submit_results else None
        if outcome is not None:
            raise outcome
        return {"transactionId": request["transaction"].id}


def make_engine(
    client: FakeClient | None = None,
    settings: Settings | None = None,
    **kwargs: float,
) -> tuple[KaspaEngine, FakeClient]:
    fake = client or FakeClient()
    engine = KaspaEngine(
        settings or get_settings(),
        client_factory=lambda: fake,
        connect_timeout_seconds=kwargs.pop("connect_timeout_seconds", 1.0),
        rpc_timeout_seconds=kwargs.pop("rpc_timeout_seconds", 1.0),
        backoff_initial_seconds=kwargs.pop("backoff_initial_seconds", 0.01),
        backoff_max_seconds=kwargs.pop("backoff_max_seconds", 0.05),
        acceptance_poll_seconds=kwargs.pop("acceptance_poll_seconds", 0.01),
    )
    return engine, fake


async def expect_error(
    engine: KaspaEngine, signature: PaymentSignature, required: PaymentRequired, code: str
) -> PaymentVerificationError:
    with pytest.raises(PaymentVerificationError) as info:
        await engine.verify_and_submit(signature, required)
    assert info.value.code == code, info.value.message
    return info.value


# ----------------------------------------------------------------------------- happy path


async def test_valid_payment_is_verified_and_submitted() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    engine, client = make_engine()
    result = await engine.verify_and_submit(signature_for(tx), required)
    assert result == VerifiedPayment(
        tx_id=tx.id,
        payer_address=address_of(PAYER_KEY).to_string(),
        total_sompi=PRICE,
        merchant_sompi=SPLIT.merchant_sompi,
        fee_sompi=SPLIT.fee_sompi,
    )
    assert len(client.submitted) == 1
    assert client.submitted[0]["transaction"].id == tx.id
    assert client.submitted[0]["allowOrphan"] is False
    assert engine.is_connected()


async def test_exact_payment_without_change_is_valid() -> None:
    required = build_required()
    tx = signed_transaction(
        [(Address(required.payTo), required.merchant_amount_sompi), (fee_address(required), required.fee_amount_sompi)],
        input_amounts=(PRICE + NETWORK_FEE,),
    )
    engine, _ = make_engine()
    result = await engine.verify_and_submit(signature_for(tx), required)
    assert result.total_sompi == PRICE


async def test_multiple_inputs_and_change_are_valid() -> None:
    required = build_required()
    tx = signed_transaction(
        standard_outputs(required, change=1_400_000_000), input_amounts=(2_000_000_000, 1_400_000_000)
    )
    engine, _ = make_engine()
    result = await engine.verify_and_submit(signature_for(tx), required)
    assert result.tx_id == tx.id


# ----------------------------------------------------------------------------- fee split


async def test_missing_fee_output_is_invalid_fee_split() -> None:
    required = build_required()
    tx = signed_transaction([(Address(required.payTo), PRICE)])
    engine, client = make_engine()
    await expect_error(engine, signature_for(tx), required, "invalid_fee_split")
    assert client.submitted == []


async def test_merchant_gets_total_and_change_replaces_fee_output() -> None:
    required = build_required()
    tx = signed_transaction(
        [(Address(required.payTo), required.merchant_amount_sompi), (address_of(PAYER_KEY), required.fee_amount_sompi)]
    )
    engine, client = make_engine()
    await expect_error(engine, signature_for(tx), required, "invalid_fee_split")
    assert client.submitted == []


async def test_fee_underpaid_by_one_sompi_is_rejected() -> None:
    required = build_required()
    outputs = standard_outputs(required)
    outputs[1] = (outputs[1][0], required.fee_amount_sompi - 1)
    outputs[0] = (outputs[0][0], required.merchant_amount_sompi + 1)
    engine, client = make_engine()
    await expect_error(engine, signature_for(signed_transaction(outputs)), required, "invalid_fee_split")
    assert client.submitted == []


async def test_fee_underpaid_by_one_sompi_with_merchant_unchanged_is_rejected() -> None:
    required = build_required()
    outputs = standard_outputs(required)
    outputs[1] = (outputs[1][0], required.fee_amount_sompi - 1)
    engine, client = make_engine()
    await expect_error(engine, signature_for(signed_transaction(outputs)), required, "invalid_fee_split")
    assert client.submitted == []


async def test_fee_overpaid_is_rejected_as_inexact() -> None:
    required = build_required()
    outputs = standard_outputs(required)
    outputs[1] = (outputs[1][0], required.fee_amount_sompi + 1)
    engine, _ = make_engine()
    await expect_error(engine, signature_for(signed_transaction(outputs)), required, "invalid_fee_split")


async def test_wrong_fee_address_is_invalid_fee_split() -> None:
    required = build_required()
    outputs = standard_outputs(required)
    outputs[1] = (address_of(OTHER_KEY), required.fee_amount_sompi)
    engine, client = make_engine()
    await expect_error(engine, signature_for(signed_transaction(outputs)), required, "invalid_fee_split")
    assert client.submitted == []


async def test_swapped_outputs_are_invalid_fee_split() -> None:
    required = build_required()
    outputs = standard_outputs(required)
    outputs[0], outputs[1] = outputs[1], outputs[0]
    engine, client = make_engine()
    await expect_error(engine, signature_for(signed_transaction(outputs)), required, "invalid_fee_split")
    assert client.submitted == []


async def test_wrong_merchant_address_is_invalid_fee_split() -> None:
    required = build_required()
    outputs = standard_outputs(required)
    outputs[0] = (address_of(OTHER_KEY), required.merchant_amount_sompi)
    engine, _ = make_engine()
    await expect_error(engine, signature_for(signed_transaction(outputs)), required, "invalid_fee_split")


async def test_fee_paid_only_in_change_slot_is_rejected() -> None:
    required = build_required()
    outputs = [
        (Address(required.payTo), required.merchant_amount_sompi),
        (address_of(PAYER_KEY), 100_000_000),
        (fee_address(required), required.fee_amount_sompi),
    ]
    engine, _ = make_engine()
    await expect_error(engine, signature_for(signed_transaction(outputs)), required, "invalid_fee_split")


# ----------------------------------------------------------------------------- merchant amount


async def test_merchant_underpaid_by_one_sompi() -> None:
    required = build_required()
    outputs = standard_outputs(required)
    outputs[0] = (outputs[0][0], required.merchant_amount_sompi - 1)
    engine, client = make_engine()
    error = await expect_error(engine, signature_for(signed_transaction(outputs)), required, "underpaid")
    assert "1 sompi short" in error.message
    assert client.submitted == []


async def test_merchant_overpaid_is_rejected_as_inexact_split() -> None:
    required = build_required()
    outputs = standard_outputs(required)
    outputs[0] = (outputs[0][0], required.merchant_amount_sompi + 1)
    engine, _ = make_engine()
    await expect_error(engine, signature_for(signed_transaction(outputs)), required, "invalid_fee_split")


async def test_fee_below_raised_floor_is_rejected() -> None:
    required = build_required()
    strict = get_settings().model_copy(update={"min_output_sompi": 40_000_000})
    engine, client = make_engine(settings=strict)
    error = await expect_error(
        engine, signature_for(signed_transaction(standard_outputs(required))), required, "invalid_fee_split"
    )
    assert "MIN_OUTPUT_SOMPI" in error.message
    assert client.submitted == []


async def test_merchant_below_floor_is_underpaid() -> None:
    total = 40_000_000
    floor = 25_000_000
    required = build_unchecked_required(total, floor)
    tx = signed_transaction(
        [(Address(required.payTo), total - floor), (fee_address(required), floor)],
        input_amounts=(total + NETWORK_FEE,),
    )
    engine, client = make_engine(settings=get_settings().model_copy(update={"min_output_sompi": floor}))
    error = await expect_error(engine, signature_for(tx), required, "underpaid")
    assert "minimum output" in error.message
    assert client.submitted == []


# ----------------------------------------------------------------------------- tx id / payload


async def test_tx_id_mismatch_is_invalid_payment_header() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    engine, client = make_engine()
    await expect_error(engine, signature_for(tx, tx_id="ab" * 32), required, "invalid_payment_header")
    assert client.submitted == []


async def test_forged_id_field_does_not_hide_tampered_outputs() -> None:
    required = build_required()
    honest = signed_transaction(standard_outputs(required))
    tampered = honest.to_dict()
    tampered["outputs"][1]["value"] = required.fee_amount_sompi - 1
    tampered["outputs"][2]["value"] += 1
    engine, client = make_engine()
    await expect_error(engine, signature_for(honest, raw=json.dumps(tampered)), required, "invalid_payment_header")
    assert client.submitted == []


@pytest.mark.parametrize(
    "raw",
    ["not json", "[]", "{}", '{"id": "x"}', '"string"', '{"inputs": [{}], "outputs": []}', "123"],
)
async def test_malformed_raw_tx_is_invalid_payment_header(raw: str) -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    engine, client = make_engine()
    await expect_error(engine, signature_for(tx, raw=raw), required, "invalid_payment_header")
    assert client.submitted == []


async def test_input_without_utxo_entry_is_invalid_payment_header() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    stripped = tx.to_dict()
    for entry in stripped["inputs"]:
        entry["utxo"] = None
    engine, _ = make_engine()
    await expect_error(engine, signature_for(tx, raw=json.dumps(stripped)), required, "invalid_payment_header")


async def test_payer_address_must_own_an_input() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    engine, _ = make_engine()
    await expect_error(engine, signature_for(tx, payer=OTHER_KEY), required, "invalid_payment_header")


async def test_outputs_exceeding_inputs_are_rejected() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    forged = tx.to_dict()
    forged["inputs"][0]["utxo"]["amount"] = 500_000_000
    engine, _ = make_engine()
    await expect_error(engine, signature_for(tx, raw=json.dumps(forged)), required, "tx_rejected")


# ----------------------------------------------------------------------------- network


async def test_required_network_mismatch_is_wrong_network() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    mainnet = get_settings().model_copy(update={"kaspa_network": "kaspa:mainnet"})
    engine, client = make_engine(settings=mainnet)
    await expect_error(engine, signature_for(tx), required, "wrong_network")
    assert client.submitted == []


async def test_address_prefix_mismatch_is_wrong_network() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    mainnet_address = MERCHANT_KEY.to_address("mainnet").to_string()
    skewed = PaymentRequired.model_construct(
        x402Version=required.x402Version,
        scheme=required.scheme,
        network="kaspa:testnet-10",
        asset=required.asset,
        amount=required.amount,
        payTo=mainnet_address,
        platformFee=required.platformFee,
        nonce=NONCE,
        expiresAt=EXPIRES_AT,
    )
    engine, client = make_engine()
    await expect_error(engine, signature_for(tx), skewed, "wrong_network")
    assert client.submitted == []


async def test_payer_address_prefix_mismatch_is_wrong_network() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    signature = PaymentSignature.model_construct(
        txId=tx.id,
        rawTx=json.dumps(tx.to_dict()),
        payerAddress=PAYER_KEY.to_address("mainnet").to_string(),
        nonce=NONCE,
    )
    engine, _ = make_engine()
    await expect_error(engine, signature, required, "wrong_network")


# ----------------------------------------------------------------------------- mass


async def test_v2_1_minimum_price_without_change_is_within_standard_mass() -> None:
    price = min_price_sompi()
    assert price == 40_000_000
    required = build_required(price)
    assert required.fee_amount_sompi == 20_000_000
    assert required.merchant_amount_sompi == 20_000_000
    engine, _ = make_engine()
    exact = signed_transaction(
        [(Address(required.payTo), required.merchant_amount_sompi), (fee_address(required), required.fee_amount_sompi)],
        input_amounts=(price + NETWORK_FEE,),
    )
    result = await engine.verify_and_submit(signature_for(exact), required)
    assert (result.total_sompi, result.merchant_sompi, result.fee_sompi) == (price, 20_000_000, 20_000_000)


async def test_v2_1_minimum_price_with_two_inputs_and_change_is_within_standard_mass() -> None:
    price = min_price_sompi()
    required = build_required(price)
    change = 500_000_000
    total_in = price + change + NETWORK_FEE
    tx = signed_transaction(
        standard_outputs(required, change=change), input_amounts=(total_in // 2, total_in - total_in // 2)
    )
    engine, _ = make_engine()
    inputs = await engine.resolve_inputs(signature_for(tx), engine.verify(signature_for(tx), required)[1])
    assert inputs.storage_mass <= 100_000
    assert (await engine.verify_and_submit(signature_for(tx), required)).total_sompi == price


@pytest.mark.parametrize("change", [10_000_000, 500_000_000, 100 * SOMPI_PER_KAS])
async def test_v2_1_minimum_price_with_single_input_and_change_exceeds_standard_mass(change: int) -> None:
    """Two 20M outputs alone cost 2 * 10^12 / 2*10^7 = 100_000 storage mass (the standard maximum); with ONE input,
    any change output pushes the transaction over the limit, so the node would refuse to relay it."""
    price = min_price_sompi()
    required = build_required(price)
    tx = signed_transaction(standard_outputs(required, change=change), input_amounts=(price + change + NETWORK_FEE,))
    engine, client = make_engine()
    error = await expect_error(engine, signature_for(tx), required, "tx_rejected")
    assert "standard maximum" in error.message
    assert client.submitted == []


async def test_old_floor_minimum_price_exceeds_standard_mass() -> None:
    old_floor_price = 666_666_667
    required = build_unchecked_required(old_floor_price, ceil_fee(old_floor_price))
    tx = signed_transaction(
        [(Address(required.payTo), required.merchant_amount_sompi), (fee_address(required), required.fee_amount_sompi)],
        input_amounts=(old_floor_price + NETWORK_FEE,),
    )
    engine, client = make_engine(settings=get_settings().model_copy(update={"min_output_sompi": 10_000_000}))
    error = await expect_error(engine, signature_for(tx), required, "tx_rejected")
    assert "standard maximum" in error.message
    assert client.submitted == []


async def test_many_change_outputs_exceed_standard_mass() -> None:
    required = build_required()
    outputs = standard_outputs(required, change=10_000_000)
    outputs += [(address_of(PAYER_KEY), 10_000_000)] * 3
    tx = signed_transaction(outputs, input_amounts=(PRICE + 100_000_000,))
    engine, _ = make_engine()
    await expect_error(engine, signature_for(tx), required, "tx_rejected")


# ----------------------------------------------------------------------------- submission


async def test_already_in_mempool_for_same_tx_is_success() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    message = (
        f"RPC Server (remote error) -> Rejected transaction {tx.id}: transaction {tx.id} is already in the mempool"
    )
    engine, client = make_engine(FakeClient(submit_results=[Exception(message)]))
    result = await engine.verify_and_submit(signature_for(tx), required)
    assert result.tx_id == tx.id
    assert len(client.submitted) == 1


async def test_already_accepted_without_id_is_success() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    engine, _ = make_engine(
        FakeClient(submit_results=[Exception("transaction has already been accepted by the consensus")])
    )
    assert (await engine.verify_and_submit(signature_for(tx), required)).tx_id == tx.id


async def test_already_in_mempool_for_other_tx_is_not_success() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    other = "cd" * 32
    engine, _ = make_engine(
        FakeClient(
            submit_results=[Exception(f"Rejected transaction {other}: transaction {other} is already in the mempool")]
        )
    )
    await expect_error(engine, signature_for(tx), required, "tx_rejected")


def test_rpc_unavailable_error_contract() -> None:
    error = RpcUnavailableError("down")
    assert error.code == "rpc_unavailable"
    assert error.retryable is True
    assert isinstance(error, PaymentVerificationError)


async def test_node_rejection_is_tx_rejected() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    message = f"RPC Server (remote error) -> Rejected transaction {tx.id}: transaction {tx.id} is an orphan where orphan is disallowed"
    engine, client = make_engine(FakeClient(submit_results=[Exception(message)]))
    error = await expect_error(engine, signature_for(tx), required, "tx_rejected")
    assert "orphan" in error.message
    assert not isinstance(error, RpcUnavailableError)
    assert len(client.submitted) == 1


async def test_rpc_timeout_is_retryable_rpc_unavailable() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    engine, client = make_engine(FakeClient(submit_delay=0.5), rpc_timeout_seconds=0.05)
    error = await expect_error(engine, signature_for(tx), required, "rpc_unavailable")
    assert isinstance(error, RpcUnavailableError)
    assert error.retryable is True
    assert len(client.submitted) == 2


async def test_transport_error_reconnects_and_retries_once() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    client = FakeClient(
        submit_results=[Exception("RPC Server (remote error) -> WebSocket -> WebSocket is not connected"), None]
    )
    engine, _ = make_engine(client)
    result = await engine.verify_and_submit(signature_for(tx), required)
    assert result.tx_id == tx.id
    assert client.connect_calls == 2
    assert client.disconnect_calls == 1
    assert len(client.submitted) == 2


async def test_connect_failure_raises_unavailable() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    engine, client = make_engine(FakeClient(connect_results=[ConnectionError("refused")]))
    error = await expect_error(engine, signature_for(tx), required, "rpc_unavailable")
    assert isinstance(error, RpcUnavailableError)
    assert client.submitted == []


async def test_connect_timeout_raises_unavailable() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    engine, _ = make_engine(FakeClient(connect_delay=1.0), connect_timeout_seconds=0.05)
    error = await expect_error(engine, signature_for(tx), required, "rpc_unavailable")
    assert isinstance(error, RpcUnavailableError)
    assert "timed out" in error.message


# ----------------------------------------------------------------------------- lifecycle


async def wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


async def test_start_connects_and_stop_disconnects() -> None:
    engine, client = make_engine()
    assert not engine.is_connected()
    await engine.start()
    assert engine.is_connected()
    await engine.stop()
    assert not engine.is_connected()
    assert client.disconnect_calls == 1


async def test_supervisor_reconnects_with_backoff_after_failed_start() -> None:
    client = FakeClient(connect_results=[ConnectionError("down"), ConnectionError("down")])
    engine, _ = make_engine(client)
    await engine.start()
    assert not engine.is_connected()
    await wait_until(engine.is_connected)
    assert client.connect_calls == 3
    await engine.stop()


async def test_supervisor_restores_dropped_connection() -> None:
    engine, client = make_engine()
    await engine.start()
    client.connected = False
    await wait_until(lambda: client.connect_calls >= 2, timeout=8.0)
    assert engine.is_connected()
    await engine.stop()


def test_engine_rejects_unknown_network() -> None:
    bad = get_settings().model_copy(update={"kaspa_network": "kaspa:devnet"})
    with pytest.raises(ValueError):
        KaspaEngine(bad)


# ----------------------------------------------------------------------------- fee floor defence (CONTRACT v2.1)


def ceil_fee(total: int) -> int:
    return -(-total * PLATFORM_FEE_BPS // BPS_DENOMINATOR)


@pytest.mark.parametrize(
    ("total", "expected_fee"),
    [
        (40_000_000, 20_000_000),  # v2.1 minimum price: flat floor fee, merchant 20M
        (100_000_000, 20_000_000),  # 1.5% would be 1.5M: floor wins
        (1_333_333_333, 20_000_000),  # 19_999_999.995 -> ceil 20_000_000 == floor
        (1_333_333_334, 20_000_001),  # 20_000_000.01 -> ceil 20_000_001: percentage wins by 1 sompi
        (2_000_000_000, 30_000_000),  # fee is an exact integer
        (2_000_000_001, 30_000_001),  # 30_000_000.015 -> ceil 30_000_001 (floor division would give 30_000_000)
        (3_333_333_333, 50_000_000),
    ],
)
async def test_fee_defence_matches_contract_v2_1_at_the_edge(total: int, expected_fee: int) -> None:
    floor = get_settings().min_output_sompi
    fee = minimum_fee_sompi(total, floor)
    assert fee == expected_fee == compute_fee_split(total).fee_sompi == max(ceil_fee(total), floor)
    assert fee * BPS_DENOMINATOR >= total * PLATFORM_FEE_BPS
    engine, _ = make_engine()

    at_edge = build_unchecked_required(total, fee)
    tx = signed_transaction(
        [(Address(at_edge.payTo), total - fee), (fee_address(at_edge), fee)], input_amounts=(total + NETWORK_FEE,)
    )
    assert (await engine.verify_and_submit(signature_for(tx), at_edge)).fee_sompi == fee

    below_edge = build_unchecked_required(total, fee - 1)
    short = signed_transaction(
        [(Address(below_edge.payTo), total - fee + 1), (fee_address(below_edge), fee - 1)],
        input_amounts=(total + NETWORK_FEE,),
    )
    error = await expect_error(engine, signature_for(short), below_edge, "invalid_fee_split")
    assert "max(ceil(1.5%), MIN_OUTPUT_SOMPI)" in error.message


async def test_fee_at_percentage_but_below_floor_is_rejected() -> None:
    """A PAYMENT-REQUIRED built with the pre-v2.1 rule (fee = ceil(1.5%) only) must never be honoured."""
    total = 100_000_000
    legacy = build_unchecked_required(total, ceil_fee(total))
    tx = signed_transaction(
        [(Address(legacy.payTo), total - ceil_fee(total)), (fee_address(legacy), ceil_fee(total))],
        input_amounts=(total + 900_000_000,),
    )
    engine, client = make_engine()
    error = await expect_error(engine, signature_for(tx), legacy, "invalid_fee_split")
    assert "MIN_OUTPUT_SOMPI" in error.message
    assert client.submitted == []


# ----------------------------------------------------------------------------- settings-driven timeouts


async def test_timeouts_default_to_settings() -> None:
    settings = get_settings().model_copy(
        update={"kaspa_connect_timeout_seconds": 0.05, "kaspa_rpc_timeout_seconds": 0.05}
    )
    client = FakeClient(submit_delay=0.5)
    engine = KaspaEngine(settings, client_factory=lambda: client)
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    await expect_error(engine, signature_for(tx), required, "rpc_unavailable")

    slow_connect = KaspaEngine(settings, client_factory=lambda: FakeClient(connect_delay=1.0))
    await expect_error(slow_connect, signature_for(tx), required, "rpc_unavailable")


async def test_constructor_overrides_settings_timeouts() -> None:
    settings = get_settings().model_copy(update={"kaspa_rpc_timeout_seconds": 0.01})
    client = FakeClient(submit_delay=0.05)
    engine = KaspaEngine(settings, client_factory=lambda: client, rpc_timeout_seconds=2.0)
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    assert (await engine.verify_and_submit(signature_for(tx), required)).tx_id == tx.id


# ----------------------------------------------------------------------------- CONTRACT v2: payload / nonce binding


async def test_payload_binding_happy_path_and_payload_bytes() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    assert bytes.fromhex(tx.payload) == b"kasgate:v1:" + NONCE.encode("ascii")
    engine, client = make_engine()
    assert (await engine.verify_and_submit(signature_for(tx), required)).tx_id == tx.id
    assert len(client.submitted) == 1


async def test_missing_payload_is_invalid_payment_header() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required), payload=None)
    engine, client = make_engine()
    error = await expect_error(engine, signature_for(tx), required, "invalid_payment_header")
    assert "payload is missing" in error.message
    assert client.submitted == []


@pytest.mark.parametrize(
    "payload",
    [
        payment_payload(OTHER_NONCE),
        b"kasgate:v2:" + NONCE.encode(),
        payment_payload(NONCE) + b"x",
        payment_payload(NONCE.upper()),
        NONCE.encode(),
    ],
)
async def test_wrong_payload_is_invalid_payment_header(payload: bytes) -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required), payload=payload)
    engine, client = make_engine()
    error = await expect_error(engine, signature_for(tx), required, "invalid_payment_header")
    assert "does not bind" in error.message
    assert client.submitted == []


async def test_signature_nonce_must_match_required_nonce() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    engine, client = make_engine()
    error = await expect_error(engine, signature_for(tx, nonce=OTHER_NONCE), required, "invalid_payment_header")
    assert "nonce" in error.message
    assert client.submitted == []


async def test_tx_bound_to_another_challenge_is_rejected_even_with_matching_signature_nonce() -> None:
    """A tx paid for challenge A cannot settle challenge B, even if the header claims B's nonce."""
    tx = signed_transaction(standard_outputs(build_required(nonce=NONCE)), payload=payment_payload(NONCE))
    other_challenge = build_required(nonce=OTHER_NONCE)
    engine, client = make_engine()
    await expect_error(engine, signature_for(tx, nonce=OTHER_NONCE), other_challenge, "invalid_payment_header")
    assert client.submitted == []


def test_payload_is_covered_by_the_transaction_id() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    tampered = tx.to_dict()
    tampered["payload"] = payment_payload(OTHER_NONCE).hex()
    rebuilt = Transaction.from_dict(tampered)
    assert rebuilt.id == tx.id  # from_dict keeps the forged id field ...
    rebuilt.finalize()
    assert rebuilt.id != tx.id  # ... but the recomputed id commits to the payload


async def test_tampered_payload_with_original_tx_id_is_rejected() -> None:
    """Re-pointing a paid tx at a new challenge by editing the payload breaks the txId check."""
    paid = signed_transaction(standard_outputs(build_required()))
    tampered = paid.to_dict()
    tampered["payload"] = payment_payload(OTHER_NONCE).hex()
    second_challenge = build_required(nonce=OTHER_NONCE)
    engine, client = make_engine()
    error = await expect_error(
        engine,
        signature_for(paid, raw=json.dumps(tampered), nonce=OTHER_NONCE),
        second_challenge,
        "invalid_payment_header",
    )
    assert "txId" in error.message
    assert client.submitted == []


def test_verify_is_pure_and_never_touches_the_node() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))

    def no_network() -> FakeClient:
        raise AssertionError("verify() must not create an RPC client")

    engine = KaspaEngine(get_settings(), client_factory=no_network)
    verified, parsed = engine.verify(signature_for(tx), required)
    assert verified.tx_id == parsed.id == tx.id
    assert not engine.is_connected()


async def test_settle_rechecks_payload_against_required_nonce() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    engine, client = make_engine()
    verified, parsed = engine.verify(signature_for(tx), required)
    with pytest.raises(PaymentVerificationError) as info:
        await engine.settle(parsed, verified, build_required(nonce=OTHER_NONCE))
    assert info.value.code == "invalid_payment_header"
    assert client.submitted == []


# ----------------------------------------------------------------------------- CONTRACT v2: finality


def mode(settlement_mode: str, timeout: float = 1.0) -> Settings:
    return get_settings().model_copy(update={"settlement_mode": settlement_mode, "settlement_timeout_seconds": timeout})


async def test_default_mode_is_accepted() -> None:
    assert get_settings().settlement_mode == "accepted"


async def test_accepted_mode_reads_sink_before_submit_then_polls_virtual_chain() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    client = FakeClient(accept_after_polls=3, sink="ab" * 32)
    engine, _ = make_engine(client, settings=mode("accepted"))
    assert (await engine.verify_and_submit(signature_for(tx), required)).tx_id == tx.id
    assert client.events.index("sink") < client.events.index("submit") < client.events.index("chain")
    assert client.events.index("utxos") < client.events.index("submit")
    assert len(client.chain_requests) == 4
    assert client.chain_requests[0] == {
        "startHash": "ab" * 32,
        "includeAcceptedTransactionIds": True,
        "minConfirmationCount": None,
    }


async def test_accepted_mode_timeout_is_rpc_unavailable() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    client = FakeClient(accept=False)
    engine, _ = make_engine(client, settings=mode("accepted", timeout=0.15))
    started = asyncio.get_running_loop().time()
    error = await expect_error(engine, signature_for(tx), required, "rpc_unavailable")
    elapsed = asyncio.get_running_loop().time() - started
    assert isinstance(error, RpcUnavailableError) and error.retryable
    assert "not accepted by the virtual chain" in error.message
    assert len(client.submitted) == 1
    assert 0.1 <= elapsed < 1.0


async def test_accepted_mode_survives_transient_poll_errors() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    client = FakeClient(chain_error=Exception("WebSocket is not connected"))
    engine, _ = make_engine(client, settings=mode("accepted", timeout=2.0))

    async def heal() -> None:
        await asyncio.sleep(0.05)
        client.chain_error = None

    healer = asyncio.create_task(heal())
    assert (await engine.verify_and_submit(signature_for(tx), required)).tx_id == tx.id
    await healer
    assert client.connect_calls >= 2


async def test_accepted_mode_poll_errors_until_deadline_are_rpc_unavailable() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    client = FakeClient(chain_error=Exception("RPC Server (remote error) -> block not found"))
    engine, _ = make_engine(client, settings=mode("accepted", timeout=0.1))
    await expect_error(engine, signature_for(tx), required, "rpc_unavailable")


async def test_accepted_mode_sink_failure_is_rpc_unavailable_and_nothing_is_submitted() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    client = FakeClient(sink_error=Exception("WebSocket is not connected"))
    engine, _ = make_engine(client, settings=mode("accepted"))
    await expect_error(engine, signature_for(tx), required, "rpc_unavailable")
    assert client.submitted == []


async def test_mempool_mode_returns_after_submit_without_polling() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    client = FakeClient(accept=False)
    engine, _ = make_engine(client, settings=mode("mempool"))
    assert (await engine.verify_and_submit(signature_for(tx), required)).tx_id == tx.id
    assert "sink" not in client.events
    assert client.chain_requests == []


async def test_duplicate_in_mempool_with_matching_nonce_waits_for_acceptance() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    message = f"Rejected transaction {tx.id}: transaction {tx.id} is already in the mempool"
    client = FakeClient(submit_results=[Exception(message)], accept_after_polls=1)
    engine, _ = make_engine(client, settings=mode("accepted"))
    assert (await engine.verify_and_submit(signature_for(tx), required)).tx_id == tx.id
    assert len(client.chain_requests) == 2


async def test_duplicate_in_mempool_never_accepted_times_out() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    message = f"Rejected transaction {tx.id}: transaction {tx.id} is already in the mempool"
    client = FakeClient(submit_results=[Exception(message)], accept=False)
    engine, _ = make_engine(client, settings=mode("accepted", timeout=0.1))
    await expect_error(engine, signature_for(tx), required, "rpc_unavailable")


async def test_duplicate_already_accepted_with_matching_nonce_is_settled_without_polling() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    message = f"Rejected transaction {tx.id}: transaction {tx.id} was already accepted by the consensus"
    client = FakeClient(submit_results=[Exception(message)], accept=False)
    engine, _ = make_engine(client, settings=mode("accepted"))
    assert (await engine.verify_and_submit(signature_for(tx), required)).tx_id == tx.id
    assert client.chain_requests == []


async def test_duplicate_for_a_tx_with_another_nonce_never_reaches_the_node() -> None:
    """The 'already in mempool' shortcut cannot launder a tx bound to a different (consumed) challenge."""
    tx = signed_transaction(standard_outputs(build_required()), payload=payment_payload(OTHER_NONCE))
    message = f"Rejected transaction {tx.id}: transaction {tx.id} is already in the mempool"
    client = FakeClient(submit_results=[Exception(message)])
    engine, _ = make_engine(client)
    await expect_error(engine, signature_for(tx), build_required(), "invalid_payment_header")
    assert client.submitted == []


# ----------------------------------------------------------------------------- CONTRACT v2: real inputs


async def test_resolve_inputs_uses_node_entries() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required), input_amounts=(2_000_000_000, 1_000_000_000))
    engine, client = make_engine()
    signature = signature_for(tx)
    _, parsed = engine.verify(signature, required)
    inputs = await engine.resolve_inputs(signature, parsed)
    assert isinstance(inputs, NodeInputs)
    assert inputs.values == (2_000_000_000, 1_000_000_000)
    assert inputs.scripts[0] == parse_script_key(pay_to_address_script(address_of(PAYER_KEY)))
    assert 0 < inputs.storage_mass <= 100_000
    assert client.utxo_calls == [[address_of(PAYER_KEY).to_string()]]
    assert client.submitted == []


async def test_unknown_input_utxo_is_invalid_payment_header() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required), register=False)
    engine, client = make_engine()
    error = await expect_error(engine, signature_for(tx), required, "invalid_payment_header")
    assert "does not know as unspent" in error.message
    assert client.submitted == []


async def test_input_amount_mismatch_with_node_is_invalid_payment_header() -> None:
    """The embedded UTXO claims more value than the node holds (would hide storage mass / fake payer funds)."""
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    first_input = tx.to_dict()["inputs"][0]["previousOutpoint"]
    key = (first_input["transactionId"], first_input["index"])
    real = {**NODE_UTXOS[key], "utxoEntry": {**NODE_UTXOS[key]["utxoEntry"], "amount": INPUT_AMOUNT - 1}}
    engine, client = make_engine(FakeClient(utxos={key: real}))
    error = await expect_error(engine, signature_for(tx), required, "invalid_payment_header")
    assert "does not match the node" in error.message
    assert client.submitted == []


async def test_input_script_mismatch_with_node_is_invalid_payment_header() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    first_input = tx.to_dict()["inputs"][0]["previousOutpoint"]
    key = (first_input["transactionId"], first_input["index"])
    other_script = str(pay_to_address_script(address_of(OTHER_KEY)).script)
    forged = {
        **NODE_UTXOS[key],
        "utxoEntry": {**NODE_UTXOS[key]["utxoEntry"], "scriptPublicKey": "0000" + other_script},
    }
    engine, client = make_engine(FakeClient(utxos={key: forged}))
    await expect_error(engine, signature_for(tx), required, "invalid_payment_header")
    assert client.submitted == []


async def test_unconfirmed_mempool_output_is_a_valid_input() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required), register=False)
    previous = tx.to_dict()["inputs"][0]["previousOutpoint"]
    script = str(pay_to_address_script(address_of(PAYER_KEY)).script)
    padding = [{"value": 123, "scriptPublicKey": "0000" + script}] * previous["index"]
    parent = {
        "fee": 2000,
        "is_orphan": False,
        "transaction": {
            "outputs": [*padding, {"value": INPUT_AMOUNT, "scriptPublicKey": "0000" + script}],
            "verboseData": {"transactionId": previous["transactionId"]},
        },
    }
    engine, client = make_engine(FakeClient(utxos={}, mempool_receiving=[parent]))
    assert (await engine.verify_and_submit(signature_for(tx), required)).tx_id == tx.id
    assert "mempool" in client.events


async def test_utxo_lookup_node_down_is_rpc_unavailable() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    client = FakeClient(utxo_error=Exception("WebSocket is not connected"))
    engine, _ = make_engine(client)
    error = await expect_error(engine, signature_for(tx), required, "rpc_unavailable")
    assert isinstance(error, RpcUnavailableError) and error.retryable is True
    assert len(client.utxo_calls) == 2  # one reconnect-and-retry
    assert client.submitted == []


async def test_utxo_lookup_timeout_is_rpc_unavailable() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    client = FakeClient(utxo_delay=0.5)
    engine, _ = make_engine(client, rpc_timeout_seconds=0.05)
    await expect_error(engine, signature_for(tx), required, "rpc_unavailable")
    assert client.submitted == []


async def test_utxo_index_unavailable_is_rpc_unavailable() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    client = FakeClient(
        utxo_error=Exception("RPC Server (remote error) -> Method unavailable. Run the node with --utxoindex")
    )
    engine, _ = make_engine(client)
    await expect_error(engine, signature_for(tx), required, "rpc_unavailable")
    assert len(client.utxo_calls) == 1
    assert client.submitted == []


async def test_mass_is_recomputed_from_node_amounts() -> None:
    """Passing pure verify() with embedded values is not enough: resolve_inputs re-runs mass on node values."""
    price = min_price_sompi()
    required = build_required(price)
    change = 500_000_000
    total_in = price + change + NETWORK_FEE
    tx = signed_transaction(
        standard_outputs(required, change=change), input_amounts=(total_in // 2, total_in - total_in // 2)
    )
    engine, _ = make_engine()
    signature = signature_for(tx)
    _, parsed = engine.verify(signature, required)
    node_values = (await engine.resolve_inputs(signature, parsed)).values
    assert node_values == (total_in // 2, total_in - total_in // 2)
    with pytest.raises(PaymentVerificationError) as info:
        engine._check_mass(parsed, [sum(node_values)])
    assert info.value.code == "tx_rejected"


async def test_payer_must_own_a_node_reported_input() -> None:
    required = build_required()
    tx = signed_transaction(standard_outputs(required))
    engine, _ = make_engine()
    await expect_error(engine, signature_for(tx, payer=OTHER_KEY), required, "invalid_payment_header")


def test_parse_script_key_accepts_all_wire_forms() -> None:
    spk = pay_to_address_script(address_of(PAYER_KEY))
    expected = (0, str(spk.script).lower())
    assert parse_script_key(spk) == expected
    assert parse_script_key({"version": 0, "script": str(spk.script)}) == expected
    assert parse_script_key("0000" + str(spk.script)) == expected
    assert parse_script_key("0001ab") == (1, "ab")
    for bad in ("", "00", "zz", {"version": True, "script": "ab"}, {"script": 5}, 7, None):
        assert parse_script_key(bad) is None
