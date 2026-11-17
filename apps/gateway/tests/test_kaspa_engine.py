from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
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
    PaymentVerificationError,
    RpcUnavailableError,
    VerifiedPayment,
)
from app.schemas import PaymentRequired, PaymentSignature, PlatformFee, compute_fee_split, min_price_sompi  # noqa: E402

PRICE = 20 * SOMPI_PER_KAS
SPLIT = compute_fee_split(PRICE)
MERCHANT_KEY = PrivateKey("01" * 32)
PAYER_KEY = PrivateKey("03" * 32)
OTHER_KEY = PrivateKey("04" * 32)
INPUT_AMOUNT = 30 * SOMPI_PER_KAS
NETWORK_FEE = 100_000


def address_of(key: PrivateKey) -> Address:
    return key.to_address("testnet")


def build_required(price: int = PRICE) -> PaymentRequired:
    """A schema-valid PAYMENT-REQUIRED whose treasury is a real address (the test treasury key)."""
    split = compute_fee_split(price)
    return PaymentRequired(
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
    )


def fee_address(required: PaymentRequired) -> Address:
    return Address(required.platformFee.feeTo)


def signed_transaction(
    outputs: list[tuple[Address, int]],
    *,
    payer: PrivateKey = PAYER_KEY,
    input_amounts: tuple[int, ...] = (INPUT_AMOUNT,),
) -> Transaction:
    payer_address = address_of(payer)
    entries: list[UtxoEntryReference] = []
    for index, amount in enumerate(input_amounts):
        entry = UtxoEntry.from_dict(
            {
                "address": payer_address.to_string(),
                "outpoint": {"transactionId": f"{index + 1:02x}" * 32, "index": index},
                "amount": amount,
                "scriptPublicKey": {"version": 0, "script": str(pay_to_address_script(payer_address).script)},
                "blockDaaScore": 1,
                "isCoinbase": False,
                "covenantId": None,
            }
        )
        entries.append(UtxoEntryReference.from_dict(entry.to_dict()))
    payments = [PaymentOutput(address, amount) for address, amount in outputs]
    # The SDK stub types `outputs` as the opaque `Outputs` class, but the binding accepts a list of PaymentOutput.
    unsigned = create_transaction(entries, payments, 0)  # type: ignore[arg-type]
    return sign_transaction(unsigned, [payer], False)


def signature_for(
    tx: Transaction,
    *,
    tx_id: str | None = None,
    raw: str | None = None,
    payer: PrivateKey = PAYER_KEY,
) -> PaymentSignature:
    return PaymentSignature(
        txId=tx_id or tx.id,
        rawTx=raw if raw is not None else json.dumps(tx.to_dict()),
        payerAddress=address_of(payer).to_string(),
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

    async def submit_transaction(self, request: dict[str, Any]) -> dict[str, str]:
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


async def test_below_min_output_is_rejected() -> None:
    required = build_required()
    strict = get_settings().model_copy(update={"min_output_sompi": 40_000_000})
    engine, client = make_engine(settings=strict)
    error = await expect_error(
        engine, signature_for(signed_transaction(standard_outputs(required))), required, "underpaid"
    )
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
    )
    engine, _ = make_engine()
    await expect_error(engine, signature, required, "wrong_network")


# ----------------------------------------------------------------------------- mass


async def test_minimum_price_at_default_floor_is_within_standard_mass() -> None:
    price = min_price_sompi()
    assert price == 1_333_333_267
    required = build_required(price)
    assert required.fee_amount_sompi == get_settings().min_output_sompi
    engine, _ = make_engine()
    exact = signed_transaction(
        [(Address(required.payTo), required.merchant_amount_sompi), (fee_address(required), required.fee_amount_sompi)],
        input_amounts=(price + NETWORK_FEE,),
    )
    assert (await engine.verify_and_submit(signature_for(exact), required)).total_sompi == price
    with_change = signed_transaction(
        standard_outputs(required, change=500_000_000), input_amounts=(price + 500_000_000 + NETWORK_FEE,)
    )
    assert (await engine.verify_and_submit(signature_for(with_change), required)).total_sompi == price


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


# ----------------------------------------------------------------------------- 1.5% ceil defence


def ceil_fee(total: int) -> int:
    return -(-total * PLATFORM_FEE_BPS // BPS_DENOMINATOR)


@pytest.mark.parametrize(
    "total",
    [
        2_000_000_000,  # fee is an exact integer: 30_000_000
        2_000_000_001,  # 30_000_000.015 -> ceil 30_000_001 (floor would be 30_000_000)
        1_333_333_267,  # minimum price: 19_999_999.005 -> 20_000_000
        3_333_333_333,
    ],
)
async def test_fee_defence_matches_contract_ceil_at_the_edge(total: int) -> None:
    fee = ceil_fee(total)
    assert fee == compute_fee_split(total).fee_sompi
    assert fee * BPS_DENOMINATOR >= total * PLATFORM_FEE_BPS
    assert (fee - 1) * BPS_DENOMINATOR < total * PLATFORM_FEE_BPS
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
    assert "ceil(1.5%)" in error.message


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
