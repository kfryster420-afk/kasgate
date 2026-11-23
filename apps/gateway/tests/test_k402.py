"""CONTRACT v2.9: k402 `kaspa-utxo` offers and the X-K402-Payment ingest path, against the real engine checks.

Transactions are built the way the official `k402.wallet.HotWallet.pay` builds them (kaspa SDK `create_transactions`
with `[merchant, treasury]` outputs and change), signed, and served from a fake node's mempool or UTXO index. When the
upstream k402 checkout is importable (K402_SRC, default /workspace/kasgate-beta/x402v2-src/k402, commit 63e1eae), the
unmodified `HotWallet` and `Client` are also run end to end against the gateway.
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from kaspa import Address, PaymentOutput, PrivateKey, Transaction, create_transactions, pay_to_address_script
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.kaspa_engine import KaspaEngine, PaymentVerificationError, VerifiedPayment, check_k402_outputs
from app.models import Settlement
from app.proxy import k402_lock_key, k402_offer_key, lock_key
from app.schemas import (
    HEADER_K402_PAYMENT,
    HEADER_PAYMENT_REQUIRED,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
    K402_TAG_SPACE,
    K402FacilitatorFee,
    K402PaymentHeader,
    K402UtxoOffer,
    PaymentRequired,
    PaymentResponse,
    compute_fee_split,
    min_price_sompi,
)
from tests.conftest import (
    MERCHANT_ADDRESS,
    OTHER_ADDRESS,
    PAYER_KEY,
    PRICE_SOMPI,
    TREASURY,
    Seed,
    StubOracle,
    Upstream,
    make_signature_header,
    seed_endpoint,
)

EXPIRES = datetime.now(UTC) + timedelta(minutes=5)
INPUT_SOMPI = 30 * 100_000_000
_OUTPOINTS = [0]


# ---------------------------------------------------------------- the upstream k402 reference implementation


def _load_reference_k402() -> ModuleType | None:
    source = os.environ.get("K402_SRC", "/workspace/kasgate-beta/x402v2-src/k402")
    if not (Path(source) / "k402" / "wallet.py").is_file():
        return None
    if source not in sys.path:
        sys.path.insert(0, source)
    try:
        return importlib.import_module("k402")
    except ImportError:
        return None


K402_REF = _load_reference_k402()
needs_reference = pytest.mark.skipif(K402_REF is None, reason="k402 reference checkout not importable (set K402_SRC)")


# ---------------------------------------------------------------- fake node


def _script_hex(address: str | Address) -> str:
    return str(pay_to_address_script(Address(address) if isinstance(address, str) else address).script).lower()


@dataclass
class FakeNode:
    """Just enough of the wRPC client for `KaspaEngine.verify_k402`: mempool, UTXO index and virtual chain."""

    mempool: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # txid -> outputs
    utxos: dict[str, list[tuple[int, int, str]]] = field(default_factory=dict)  # txid -> [(index, value, script)]
    accepted: set[str] = field(default_factory=set)
    accept_on_poll: bool = True
    connected: bool = False
    calls: list[str] = field(default_factory=list)

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def connect(self, **_: Any) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def submit_transaction(self, request: dict[str, Any]) -> Any:
        raise AssertionError("the k402 path must never submit: the payer already did")

    async def get_sink(self, request: dict[str, Any] | None = None) -> dict[str, str]:
        self.calls.append("sink")
        return {"sink": "aa" * 32}

    async def get_virtual_chain_from_block(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("chain")
        if self.accept_on_poll:
            self.accepted |= set(self.mempool)
        return {
            "acceptedTransactionIds": [{"acceptingBlockHash": "bb" * 32, "acceptedTransactionIds": [*self.accepted]}]
        }

    async def get_mempool_entries_by_addresses(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("mempool")
        wanted = {_script_hex(address) for address in request["addresses"]}
        entries = []
        for address in request["addresses"]:
            receiving = [
                {"fee": 1, "isOrphan": False, "transaction": {"outputs": outputs, "verboseData": {"transactionId": tx}}}
                for tx, outputs in self.mempool.items()
                if any(str(o["scriptPublicKey"]["script"]).lower() in wanted for o in outputs)
            ]
            entries.append({"address": address, "sending": [], "receiving": receiving})
        return {"entries": entries}

    async def get_utxos_by_addresses(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("utxos")
        by_script = {_script_hex(address): address for address in request["addresses"]}
        entries = [
            {
                "address": by_script[script],
                "outpoint": {"transactionId": tx, "index": index},
                "utxoEntry": {"amount": value, "scriptPublicKey": "0000" + script, "blockDaaScore": 1},
            }
            for tx, outs in self.utxos.items()
            for index, value, script in outs
            if script in by_script
        ]
        return {"entries": entries}

    def add_to_mempool(self, tx: Transaction) -> None:
        self.mempool[tx.id] = tx.to_dict()["outputs"]

    def accept_into_utxo_index(self, tx: Transaction, *, spent: tuple[int, ...] = ()) -> None:
        outputs = tx.to_dict()["outputs"]
        self.utxos[tx.id] = [
            (index, int(o["value"]), str(o["scriptPublicKey"]["script"]).lower())
            for index, o in enumerate(outputs)
            if index not in spent
        ]
        self.mempool.pop(tx.id, None)
        self.accepted.add(tx.id)


def make_engine(node: FakeNode, **overrides: Any) -> KaspaEngine:
    settings: Settings = get_settings().model_copy(update=overrides) if overrides else get_settings()
    return KaspaEngine(
        settings,
        client_factory=lambda: node,
        connect_timeout_seconds=1.0,
        rpc_timeout_seconds=1.0,
        acceptance_poll_seconds=0.01,
    )


# ---------------------------------------------------------------- transactions, HotWallet style


def payer_entries(key: PrivateKey = PAYER_KEY, amounts: tuple[int, ...] = (INPUT_SOMPI,)) -> list[dict[str, Any]]:
    address = key.to_address("testnet")
    script = _script_hex(address)
    entries = []
    for amount in amounts:
        _OUTPOINTS[0] += 1
        entries.append(
            {
                "address": address.to_string(),
                "outpoint": {"transactionId": f"{_OUTPOINTS[0] + 0xA000:064x}", "index": 0},
                "amount": amount,
                "scriptPublicKey": {"version": 0, "script": script},
                "blockDaaScore": 1,
                "isCoinbase": False,
                "covenantId": None,
            }
        )
    return entries


def build_tx(outputs: list[tuple[str, int]], key: PrivateKey = PAYER_KEY) -> Transaction:
    """Exactly `HotWallet.pay`: `create_transactions(outputs=..., change_address=payer)`, no payload, then sign."""
    bundle = create_transactions(
        network_id="testnet-10",
        entries=payer_entries(key),
        outputs=[PaymentOutput(Address(address), amount) for address, amount in outputs],
        change_address=key.to_address("testnet"),
        priority_fee=0,
    )
    pending = bundle["transactions"][-1]
    pending.sign([key])
    tx: Transaction = pending.transaction
    return tx


def hotwallet_outputs(offer: K402UtxoOffer) -> list[tuple[str, int]]:
    return [(offer.pay_to, offer.merchant_sompi), (offer.facilitator_fee.to, offer.fee_sompi)]


def make_offer(price: int = PRICE_SOMPI, tag: int = 0, pay_to: str = MERCHANT_ADDRESS) -> K402UtxoOffer:
    return K402UtxoOffer.for_endpoint(price, pay_to, "p_0123456789abcdef", EXPIRES, tag_sompi=tag)


# ================================================================ offers


def test_offer_quotes_merchant_share_and_exact_treasury_fee() -> None:
    offer = make_offer()
    split = compute_fee_split(PRICE_SOMPI)
    wire = offer.model_dump(mode="json")
    assert wire["scheme"] == "kaspa-utxo"
    assert wire["network"] == "testnet-10"  # k402 wire name, not the kaspa: CAIP form
    assert wire["amount_sompi"] == str(split.merchant_sompi) == "1970000000"
    assert wire["pay_to"] == MERCHANT_ADDRESS
    assert wire["facilitator_fee"] == {"sompi": str(split.fee_sompi), "to": TREASURY, "by": "kasgate"}
    assert wire["expires"] == int(EXPIRES.timestamp())
    assert offer.total_sompi == PRICE_SOMPI


@pytest.mark.parametrize(
    ("price", "fee"),
    [
        (60_000_000, 20_000_000),  # minimum price: 0.2 KAS floor, 0.4 KAS merchant
        (1_333_333_333, 20_000_000),  # still on the floor
        (1_333_333_400, 20_000_001),  # 1.5 % (ceil) takes over above ~13.33 KAS
        (10_000_000_000, 150_000_000),
    ],
)
def test_offer_fee_is_max_of_one_and_a_half_percent_and_the_floor(price: int, fee: int) -> None:
    offer = make_offer(price)
    assert offer.fee_sompi == fee
    assert offer.merchant_sompi == price - fee
    assert offer.total_sompi >= min_price_sompi() == 60_000_000


def test_offer_tag_moves_the_total_and_keeps_the_split_exact() -> None:
    for tag in (1, 777_777, K402_TAG_SPACE - 1):
        offer = make_offer(PRICE_SOMPI, tag)
        split = compute_fee_split(PRICE_SOMPI + tag)
        assert offer.total_sompi == PRICE_SOMPI + tag
        assert (offer.merchant_sompi, offer.fee_sompi) == (split.merchant_sompi, split.fee_sompi)
    with pytest.raises(ValueError, match="tag space"):
        make_offer(PRICE_SOMPI, K402_TAG_SPACE)


@pytest.mark.parametrize("network", ["kaspa:testnet-10", "kaspa:mainnet", "mainnet", "testnet-11", ""])
def test_offer_rejects_any_network_label_but_testnet_10(network: str) -> None:
    wire = make_offer().model_dump(mode="json") | {"network": network}
    with pytest.raises(ValidationError):
        K402UtxoOffer.model_validate(wire)


def test_offer_rejects_a_fee_that_is_not_exact_or_a_total_below_the_minimum() -> None:
    wire = make_offer().model_dump(mode="json")
    # 30000001 would be self-consistent (fee of a 2_000_000_001 total), so the cases are off by more than rounding
    for sompi in ("29999999", "30000002", "2000"):
        with pytest.raises(ValidationError, match="facilitator_fee.sompi must be"):
            K402UtxoOffer.model_validate(wire | {"facilitator_fee": wire["facilitator_fee"] | {"sompi": sompi}})
    with pytest.raises(ValidationError):  # 0.5 KAS total: merchant output below 2 * floor
        K402UtxoOffer.model_validate(
            wire | {"amount_sompi": "30000000", "facilitator_fee": wire["facilitator_fee"] | {"sompi": "20000000"}}
        )
    with pytest.raises(ValidationError):
        K402UtxoOffer.model_validate(wire | {"extra": 1})
    with pytest.raises(ValidationError):
        K402FacilitatorFee.model_validate({"sompi": "20000000", "to": "kaspa:" + "q" * 61})


@needs_reference
def test_offer_round_trips_through_the_reference_k402_parser() -> None:
    assert K402_REF is not None
    offer = make_offer(tag=4242)
    body = {"k402": "0.2", "accepts": [{"scheme": "kaspa-exact-v1", "amount": "1"}, offer.model_dump(mode="json")]}
    parsed = K402_REF.parse_offers(body)
    assert len(parsed) == 1  # the KasGate v1 entry is skipped as an unknown scheme
    ref = parsed[0]
    assert isinstance(ref, K402_REF.UtxoOffer)
    assert (ref.network, ref.amount_sompi, ref.pay_to, ref.payment_id) == (
        "testnet-10",
        offer.amount_sompi,
        MERCHANT_ADDRESS,
        offer.payment_id,
    )
    assert ref.facilitator_fee.to == TREASURY and ref.facilitator_fee.sompi == offer.facilitator_fee.sompi
    assert ref.total_sompi == offer.total_sompi


# ================================================================ X-K402-Payment


def test_payment_header_parses_the_three_token_form() -> None:
    tx = "ab" * 32
    header = K402PaymentHeader.from_header(f"  kaspa-utxo {tx.upper()} p_0123456789abcdef ")
    assert (header.scheme, header.tx_id, header.payment_id) == ("kaspa-utxo", tx, "p_0123456789abcdef")
    assert header.to_header() == f"kaspa-utxo {tx} p_0123456789abcdef"
    if K402_REF is not None:
        assert K402_REF.format_payment_header(tx, "p_0123456789abcdef") == header.to_header()


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "kaspa-utxo",
        f"kaspa-utxo {'ab' * 32}",
        f"kaspa-session {'ab' * 32} p_0123456789abcdef",
        f"kaspa-channel {'ab' * 32} 1000 {'cd' * 64}",
        f"kaspa-utxo {'ab' * 31} p_0123456789abcdef",
        f"kaspa-utxo {'zz' * 32} p_0123456789abcdef",
        f"kaspa-utxo {'ab' * 32} p_0123",
        f"kaspa-utxo {'ab' * 32} 0123456789abcdef",
        f"kaspa-utxo {'ab' * 32} p_0123456789abcdef " + "x" * 300,
    ],
)
def test_payment_header_rejects_anything_else(value: str) -> None:
    with pytest.raises(ValueError):
        K402PaymentHeader.from_header(value)


# ================================================================ output checks (pure)


def _outputs(tx: Transaction) -> dict[int, tuple[int, tuple[int, str]]]:
    return {
        index: (int(o["value"]), (0, str(o["scriptPublicKey"]["script"]).lower()))
        for index, o in enumerate(tx.to_dict()["outputs"])
    }


def test_hotwallet_style_transaction_keeps_merchant_then_treasury_then_change() -> None:
    """kaspa 2.1.0 `create_transactions` keeps the `outputs=` order and appends change (K402_DIFF open question 1)."""
    for price in (60_000_000, PRICE_SOMPI, 1_000_000_000):
        offer = make_offer(price, tag=12_345)
        tx = build_tx(hotwallet_outputs(offer))
        outputs = tx.to_dict()["outputs"]
        assert [o["scriptPublicKey"]["script"] for o in outputs[:2]] == [
            _script_hex(MERCHANT_ADDRESS),
            _script_hex(TREASURY),
        ]
        assert [o["value"] for o in outputs[:2]] == [offer.merchant_sompi, offer.fee_sompi]
        assert len(outputs) == 3 and not tx.payload  # change last, empty payload
        assert check_k402_outputs(tx.id, _outputs(tx), offer, 20_000_000) == VerifiedPayment(
            tx_id=tx.id,
            payer_address=None,
            total_sompi=offer.total_sompi,
            merchant_sompi=offer.merchant_sompi,
            fee_sompi=offer.fee_sompi,
        )


def test_a_larger_treasury_output_does_not_reorder_outputs() -> None:
    offer = make_offer(60_000_000)
    tx = build_tx([(offer.pay_to, 20_000_000), (offer.facilitator_fee.to, 40_000_000)])
    assert [o["value"] for o in tx.to_dict()["outputs"][:2]] == [20_000_000, 40_000_000]


@pytest.mark.parametrize(
    ("outputs", "code", "needle"),
    [
        ("missing_fee", "invalid_fee_split", "output 1 must pay the KasGate treasury"),
        ("fee_short_1", "invalid_fee_split", "exactly"),
        ("fee_over_1", "invalid_fee_split", "exactly"),
        ("swapped", "invalid_fee_split", "treasury output must be output 1, found at output 0"),
        ("wrong_fee_recipient", "invalid_fee_split", "output 1 must pay the KasGate treasury"),
        ("fee_after_change", "invalid_fee_split", "treasury output must be output 1, found at output 2"),
        ("merchant_short_1", "underpaid", "1 sompi short"),
        ("merchant_over_1", "invalid_fee_split", "merchant output must be exactly"),
        ("wrong_merchant", "invalid_fee_split", "output 0 must pay the merchant"),
    ],
)
def test_split_violations_are_never_approved(outputs: str, code: str, needle: str) -> None:
    offer = make_offer()
    merchant, fee = offer.merchant_sompi, offer.fee_sompi
    payer = PAYER_KEY.to_address("testnet").to_string()
    shapes: dict[str, list[tuple[str, int]]] = {
        "missing_fee": [(MERCHANT_ADDRESS, merchant)],
        "fee_short_1": [(MERCHANT_ADDRESS, merchant), (TREASURY, fee - 1)],
        "fee_over_1": [(MERCHANT_ADDRESS, merchant), (TREASURY, fee + 1)],
        "swapped": [(TREASURY, fee), (MERCHANT_ADDRESS, merchant)],
        "wrong_fee_recipient": [(MERCHANT_ADDRESS, merchant), (OTHER_ADDRESS, fee)],
        "fee_after_change": [(MERCHANT_ADDRESS, merchant), (payer, 50_000_000), (TREASURY, fee)],
        "merchant_short_1": [(MERCHANT_ADDRESS, merchant - 1), (TREASURY, fee)],
        "merchant_over_1": [(MERCHANT_ADDRESS, merchant + 1), (TREASURY, fee)],
        "wrong_merchant": [(OTHER_ADDRESS, merchant), (TREASURY, fee)],
    }
    tx = build_tx(shapes[outputs])
    with pytest.raises(PaymentVerificationError) as info:
        check_k402_outputs(tx.id, _outputs(tx), offer, 20_000_000)
    assert info.value.code == code
    assert needle in info.value.message


def test_only_the_merchant_utxo_left_is_refused_not_assumed() -> None:
    offer = make_offer()
    tx = build_tx(hotwallet_outputs(offer))
    partial = {0: _outputs(tx)[0]}  # treasury output not visible (spent or never paid)
    with pytest.raises(PaymentVerificationError) as info:
        check_k402_outputs(tx.id, partial, offer, 20_000_000)
    assert info.value.code == "invalid_fee_split"
    with pytest.raises(PaymentVerificationError) as info:
        check_k402_outputs(tx.id, {1: _outputs(tx)[1]}, offer, 20_000_000)
    assert info.value.code == "invalid_fee_split"


# ================================================================ engine: node lookup


async def test_engine_accepts_from_the_mempool_and_waits_for_acceptance() -> None:
    offer = make_offer(tag=99)
    tx = build_tx(hotwallet_outputs(offer))
    node = FakeNode()
    node.add_to_mempool(tx)
    verified = await make_engine(node).verify_k402(tx.id, offer)
    assert verified.fee_sompi == offer.fee_sompi and verified.merchant_sompi == offer.merchant_sompi
    assert verified.payer_address is None
    assert node.calls[:2] == ["sink", "mempool"] and "chain" in node.calls  # sink read before the lookup


async def test_engine_accepts_from_the_utxo_index_after_acceptance() -> None:
    offer = make_offer()
    tx = build_tx(hotwallet_outputs(offer))
    node = FakeNode()
    node.accept_into_utxo_index(tx)
    verified = await make_engine(node).verify_k402(tx.id, offer)
    assert verified.total_sompi == offer.total_sompi
    assert node.calls == ["sink", "mempool", "utxos"]


async def test_engine_mempool_mode_does_not_wait_for_the_chain() -> None:
    offer = make_offer()
    tx = build_tx(hotwallet_outputs(offer))
    node = FakeNode()
    node.add_to_mempool(tx)
    await make_engine(node, settlement_mode="mempool").verify_k402(tx.id, offer)
    assert node.calls == ["mempool"]


async def test_engine_unknown_or_unaccepted_tx_is_payment_not_found() -> None:
    offer = make_offer()
    tx = build_tx(hotwallet_outputs(offer))
    node = FakeNode()
    with pytest.raises(PaymentVerificationError) as info:
        await make_engine(node).verify_k402(tx.id, offer)
    assert info.value.code == "payment_not_found"

    node.add_to_mempool(tx)
    node.accept_on_poll = False
    with pytest.raises(PaymentVerificationError) as info:
        await make_engine(node, settlement_timeout_seconds=0.05).verify_k402(tx.id, offer)
    assert info.value.code == "payment_not_found"


async def test_engine_rejects_a_bad_split_found_on_the_node() -> None:
    offer = make_offer()
    bad = build_tx([(MERCHANT_ADDRESS, offer.merchant_sompi), (TREASURY, offer.fee_sompi - 1)])
    node = FakeNode()
    node.add_to_mempool(bad)
    with pytest.raises(PaymentVerificationError) as info:
        await make_engine(node).verify_k402(bad.id, offer)
    assert info.value.code == "invalid_fee_split"
    assert "chain" not in node.calls  # never waits for acceptance of a bad split

    swapped = build_tx([(TREASURY, offer.fee_sompi), (MERCHANT_ADDRESS, offer.merchant_sompi)])
    node.accept_into_utxo_index(swapped)
    with pytest.raises(PaymentVerificationError) as info:
        await make_engine(node).verify_k402(swapped.id, offer)
    assert info.value.code == "invalid_fee_split"

    good = build_tx(hotwallet_outputs(offer))
    node.accept_into_utxo_index(good, spent=(1,))  # treasury output no longer visible
    with pytest.raises(PaymentVerificationError) as info:
        await make_engine(node).verify_k402(good.id, offer)
    assert info.value.code == "invalid_fee_split"


async def test_engine_refuses_an_offer_for_another_network() -> None:
    wire = make_offer().model_dump(mode="json") | {"network": "mainnet"}
    foreign = K402UtxoOffer.model_construct(
        **(wire | {"facilitator_fee": K402FacilitatorFee.model_construct(**wire["facilitator_fee"])})
    )
    with pytest.raises(PaymentVerificationError) as info:
        await make_engine(FakeNode()).verify_k402("ab" * 32, foreign)
    assert info.value.code == "wrong_network"


# ================================================================ proxy: X-K402-Payment


@pytest.fixture
def node(app: FastAPI) -> Iterator[FakeNode]:
    fake = FakeNode()
    app.state.engine = make_engine(fake)
    yield fake


async def fetch_offer(client: httpx.AsyncClient, slug: str = "weather") -> tuple[PaymentRequired, K402UtxoOffer]:
    response = await client.get(f"/p/{slug}")
    assert response.status_code == 402, response.text
    body = response.json()
    assert body["k402"] == "0.2"
    v1, k402 = body["accepts"]
    required = PaymentRequired.from_header(response.headers[HEADER_PAYMENT_REQUIRED])
    assert PaymentRequired.model_validate(v1) == required
    return required, K402UtxoOffer.model_validate(k402)


def k402_header(tx_id: str, payment_id: str) -> dict[str, str]:
    return {HEADER_K402_PAYMENT: f"kaspa-utxo {tx_id} {payment_id}"}


async def settlements(maker: async_sessionmaker[AsyncSession]) -> list[Settlement]:
    async with maker() as session:
        return list((await session.execute(select(Settlement))).scalars().all())


async def test_402_carries_a_stored_k402_offer_next_to_the_v1_terms(
    client: httpx.AsyncClient, seeded: Seed, node: FakeNode, redis: Any
) -> None:
    required, offer = await fetch_offer(client)
    assert offer.pay_to == required.payTo == MERCHANT_ADDRESS
    assert offer.facilitator_fee.to == required.platformFee.feeTo == TREASURY
    assert 0 <= offer.total_sompi - PRICE_SOMPI < K402_TAG_SPACE
    assert offer.fee_sompi == compute_fee_split(offer.total_sompi).fee_sompi
    assert offer.description == "Weather API"
    assert 0 < await redis.ttl(k402_offer_key(offer.payment_id)) <= 120
    assert offer.expires == int(required.expiresAt.timestamp())
    if K402_REF is not None:
        response = await client.get("/p/weather")
        (ref,) = K402_REF.parse_offers(response.json())
        assert ref.facilitator_fee is not None and ref.network == "testnet-10"


async def test_correctly_split_k402_payment_is_served_and_recorded(
    client: httpx.AsyncClient,
    seeded: Seed,
    node: FakeNode,
    upstream: Upstream,
    redis: Any,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _, offer = await fetch_offer(client)
    tx = build_tx(hotwallet_outputs(offer))
    node.add_to_mempool(tx)
    response = await client.get("/p/weather/today?city=hanoi", headers=k402_header(tx.id, offer.payment_id))
    assert response.status_code == 200, response.text
    assert PaymentResponse.from_header(response.headers[HEADER_PAYMENT_RESPONSE]).txId == tx.id
    forwarded = upstream.requests[-1]
    assert HEADER_K402_PAYMENT.lower() not in {name.lower() for name in forwarded.headers}
    (row,) = await settlements(sessionmaker)
    assert row.payment_scheme == "k402-kaspa-utxo" and row.k402_payment_id == offer.payment_id
    assert row.tx_id == tx.id and row.payer_address is None
    assert (row.merchant_amount_sompi, row.fee_amount_sompi) == (offer.merchant_sompi, offer.fee_sompi)
    assert row.amount_sompi == offer.total_sompi and row.treasury_address == TREASURY
    assert row.network == "kaspa:testnet-10" and len(row.payment_nonce) == 32
    assert await redis.get(k402_offer_key(offer.payment_id)) is None  # consumed
    assert await redis.get(lock_key(tx.id)) == "1"  # the shared lock stays for its TTL

    listed = await client.get("/v1/settlements", headers={"Authorization": f"Bearer {seeded.api_key}"})
    assert listed.status_code == 200, listed.text
    item = listed.json()[0] if isinstance(listed.json(), list) else listed.json()["items"][0]
    assert item["payment_scheme"] == "k402-kaspa-utxo" and item["payer_address"] is None


async def test_k402_replays_are_refused(
    client: httpx.AsyncClient,
    seeded: Seed,
    node: FakeNode,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    required, offer = await fetch_offer(client)
    tx = build_tx(hotwallet_outputs(offer))
    node.add_to_mempool(tx)
    assert (await client.get("/p/weather", headers=k402_header(tx.id, offer.payment_id))).status_code == 200
    served = len(upstream.requests)

    again = await client.get("/p/weather", headers=k402_header(tx.id, offer.payment_id))
    assert again.status_code == 409 and again.json()["error"] == "payment_replayed"

    _, other_offer = await fetch_offer(client)  # same txid under a fresh payment_id
    reused = await client.get("/p/weather", headers=k402_header(tx.id, other_offer.payment_id))
    assert reused.status_code == 409

    other_tx = build_tx(hotwallet_outputs(offer))  # a new txid under the consumed payment_id
    node.add_to_mempool(other_tx)
    burned = await client.get("/p/weather", headers=k402_header(other_tx.id, offer.payment_id))
    assert burned.status_code == 409

    # ... and through the other binding: a v1 PAYMENT-SIGNATURE citing the k402 txid
    v1 = await client.get(
        "/p/weather", headers={HEADER_PAYMENT_SIGNATURE: make_signature_header(tx.id, required.nonce)}
    )
    assert v1.status_code == 409
    assert len(upstream.requests) == served
    assert len(await settlements(sessionmaker)) == 1


async def test_k402_honours_the_shared_tx_lock_and_releases_its_own_on_failure(
    client: httpx.AsyncClient, seeded: Seed, node: FakeNode, upstream: Upstream, redis: Any
) -> None:
    _, offer = await fetch_offer(client)
    tx = build_tx(hotwallet_outputs(offer))
    node.add_to_mempool(tx)
    await redis.set(lock_key(tx.id), "1")  # e.g. the v1 path is processing this txid right now
    busy = await client.get("/p/weather", headers=k402_header(tx.id, offer.payment_id))
    assert busy.status_code == 409 and busy.json()["error"] == "payment_replayed"
    assert await redis.get(k402_lock_key(offer.payment_id)) is None
    assert upstream.requests == []
    await redis.delete(lock_key(tx.id))
    assert (await client.get("/p/weather", headers=k402_header(tx.id, offer.payment_id))).status_code == 200


async def test_k402_tx_not_visible_yet_is_a_retryable_402(
    client: httpx.AsyncClient, seeded: Seed, node: FakeNode, upstream: Upstream, redis: Any
) -> None:
    _, offer = await fetch_offer(client)
    tx = build_tx(hotwallet_outputs(offer))
    early = await client.get("/p/weather", headers=k402_header(tx.id, offer.payment_id))
    assert early.status_code == 402 and early.json()["error"] == "payment_not_found"
    assert early.json()["k402"] == "0.2"
    assert await redis.get(lock_key(tx.id)) is None and await redis.get(k402_lock_key(offer.payment_id)) is None
    node.add_to_mempool(tx)
    assert (await client.get("/p/weather", headers=k402_header(tx.id, offer.payment_id))).status_code == 200
    assert len(upstream.requests) == 1


@pytest.mark.parametrize(
    ("shape", "code"),
    [
        ("missing_fee", "invalid_fee_split"),
        ("fee_short_1", "invalid_fee_split"),
        ("swapped", "invalid_fee_split"),
        ("wrong_fee_recipient", "invalid_fee_split"),
        ("merchant_short_1", "underpaid"),
    ],
)
async def test_k402_bad_split_is_refused_and_nothing_is_served(
    client: httpx.AsyncClient,
    seeded: Seed,
    node: FakeNode,
    upstream: Upstream,
    redis: Any,
    sessionmaker: async_sessionmaker[AsyncSession],
    shape: str,
    code: str,
) -> None:
    _, offer = await fetch_offer(client)
    merchant, fee = offer.merchant_sompi, offer.fee_sompi
    outputs = {
        "missing_fee": [(MERCHANT_ADDRESS, merchant)],
        "fee_short_1": [(MERCHANT_ADDRESS, merchant), (TREASURY, fee - 1)],
        "swapped": [(TREASURY, fee), (MERCHANT_ADDRESS, merchant)],
        "wrong_fee_recipient": [(MERCHANT_ADDRESS, merchant), (OTHER_ADDRESS, fee)],
        "merchant_short_1": [(MERCHANT_ADDRESS, merchant - 1), (TREASURY, fee)],
    }[shape]
    tx = build_tx(outputs)
    node.add_to_mempool(tx)
    response = await client.get("/p/weather", headers=k402_header(tx.id, offer.payment_id))
    assert response.status_code == 402 and response.json()["error"] == code
    assert upstream.requests == [] and await settlements(sessionmaker) == []
    assert await redis.get(lock_key(tx.id)) is None
    assert await redis.get(k402_offer_key(offer.payment_id)) is not None  # not consumed by a failure


@pytest.mark.parametrize(
    "value",
    [
        "",
        "kaspa-utxo",
        f"kaspa-session {'ab' * 32} p_0123456789abcdef",
        f"kaspa-channel {'ab' * 32} 100 {'cd' * 64}",
        "kaspa-utxo not-a-txid p_0123456789abcdef",
    ],
)
async def test_k402_empty_or_malformed_header_is_invalid_payment_header(
    client: httpx.AsyncClient, seeded: Seed, node: FakeNode, upstream: Upstream, value: str
) -> None:
    response = await client.get("/p/weather", headers={HEADER_K402_PAYMENT: value})
    assert response.status_code == 402
    body = response.json()
    assert body["error"] == "invalid_payment_header" and body["k402"] == "0.2"
    assert HEADER_PAYMENT_REQUIRED in response.headers
    assert upstream.requests == [] and node.calls == []


async def test_k402_unknown_payment_id_other_slug_and_both_headers(
    client: httpx.AsyncClient,
    seeded: Seed,
    node: FakeNode,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await seed_endpoint(sessionmaker, slug="other-api", address=OTHER_ADDRESS)
    required, offer = await fetch_offer(client)
    tx = build_tx(hotwallet_outputs(offer))
    node.add_to_mempool(tx)

    unknown = await client.get("/p/weather", headers=k402_header(tx.id, "p_ffffffffffffffff"))
    assert unknown.status_code == 402 and unknown.json()["error"] == "payment_expired"

    elsewhere = await client.get("/p/other-api", headers=k402_header(tx.id, offer.payment_id))
    assert elsewhere.status_code == 402 and elsewhere.json()["error"] == "invalid_payment_header"

    both = await client.get(
        "/p/weather",
        headers={
            **k402_header(tx.id, offer.payment_id),
            HEADER_PAYMENT_SIGNATURE: make_signature_header(tx.id, required.nonce),
        },
    )
    assert both.status_code == 402 and both.json()["error"] == "invalid_payment_header"
    assert upstream.requests == []


async def test_k402_expired_offer_is_refused(
    client: httpx.AsyncClient, seeded: Seed, node: FakeNode, upstream: Upstream, redis: Any
) -> None:
    _, offer = await fetch_offer(client)
    tx = build_tx(hotwallet_outputs(offer))
    node.add_to_mempool(tx)
    await redis.delete(k402_offer_key(offer.payment_id))
    response = await client.get("/p/weather", headers=k402_header(tx.id, offer.payment_id))
    assert response.status_code == 402 and response.json()["error"] == "payment_expired"
    assert upstream.requests == []


async def test_k402_upstream_5xx_can_be_redelivered_with_the_same_header(
    client: httpx.AsyncClient, seeded: Seed, node: FakeNode, upstream: Upstream
) -> None:
    _, offer = await fetch_offer(client)
    tx = build_tx(hotwallet_outputs(offer))
    node.add_to_mempool(tx)
    upstream.handler = lambda request: httpx.Response(503, text="down")
    first = await client.get("/p/weather", headers=k402_header(tx.id, offer.payment_id))
    assert first.status_code == 503
    upstream.handler = None
    retry = await client.get("/p/weather", headers=k402_header(tx.id, offer.payment_id))
    assert retry.status_code == 200
    elsewhere = await client.get("/p/weather/other", headers=k402_header(tx.id, offer.payment_id))
    assert elsewhere.status_code == 409


# ================================================================ the unmodified reference client, end to end


@dataclass
class _NodeBackend:
    """A `k402.backend` stand-in: serves the payer's UTXOs and submits into the fake node's mempool."""

    node: FakeNode
    key: PrivateKey
    submitted: list[Transaction] = field(default_factory=list)

    async def utxos(self, address: str) -> list[dict[str, Any]]:
        return payer_entries(self.key)

    async def submit_transaction(self, pending: Any) -> str:
        tx: Transaction = pending.transaction
        self.submitted.append(tx)
        self.node.add_to_mempool(tx)
        return str(pending.id)

    async def address_received_sompi(self, address: str) -> int:
        return 0


@needs_reference
async def test_unmodified_k402_client_and_hotwallet_pay_through_the_gateway(
    app: FastAPI,
    seeded: Seed,
    node: FakeNode,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    assert K402_REF is not None
    from k402.wallet import HotWallet

    backend = _NodeBackend(node, PAYER_KEY)
    # Upstream finding: `HotWallet.__init__` calls `to_address(network)`, which kaspa 2.1.0 refuses for "testnet-10"
    # ("Invalid network type"), while `create_transactions` refuses plain "testnet" ("Missing network suffix"). So an
    # unmodified HotWallet cannot even be constructed on testnet-10; mainnet is unaffected. The wallet is therefore
    # assembled field by field here, and the UNMODIFIED `HotWallet.pay` and `Client` do the paying.
    with pytest.raises(Exception, match="Invalid network type"):
        HotWallet(PAYER_KEY.to_string(), network="testnet-10", backend=backend)  # type: ignore[arg-type]
    wallet = HotWallet.__new__(HotWallet)
    wallet._key = PAYER_KEY
    wallet.network = "testnet-10"
    wallet.address = PAYER_KEY.to_address("testnet").to_string()
    wallet.backend = backend
    wallet.priority_fee_sompi = 0
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway.test")
    k402_client = K402_REF.Client(payer=wallet, max_kas_per_call=25.0, confirm_retries=2, http=http)
    try:
        response = await k402_client.get("http://gateway.test/p/weather")
    finally:
        await http.aclose()
    assert response.status_code == 200, response.text
    (tx,) = backend.submitted
    outputs = tx.to_dict()["outputs"]
    assert outputs[0]["scriptPublicKey"]["script"] == _script_hex(MERCHANT_ADDRESS)
    assert outputs[1]["scriptPublicKey"]["script"] == _script_hex(TREASURY)
    (row,) = await settlements(sessionmaker)
    assert row.tx_id == tx.id and row.payment_scheme == "k402-kaspa-utxo"
    assert row.fee_amount_sompi == outputs[1]["value"] >= 20_000_000
    assert len(upstream.requests) == 1


# ================================================================ USD-priced endpoints (main's oracle pricing)


@pytest.mark.parametrize(
    ("usd_micros", "floored"),
    [(1_000_000, False), (1, True)],  # 8 KAS at 0.125 USD/KAS; 1 micro-USD -> the 0.6 KAS floor
)
async def test_usd_k402_offer_uses_the_same_locked_quote_as_the_v1_challenge(
    client: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    node: FakeNode,
    stub_oracle: StubOracle,
    upstream: Upstream,
    redis: Any,
    usd_micros: int,
    floored: bool,
) -> None:
    import json

    from app.models import PriceCurrency
    from app.oracle import usd_value
    from app.schemas import usd_micros_to_sompi

    await seed_endpoint(
        sessionmaker,
        slug="usd-api",
        address=OTHER_ADDRESS,
        price_currency=PriceCurrency.USD,
        price_usd_micros=usd_micros,
        price_sompi=None,
    )
    required, offer = await fetch_offer(client, "usd-api")
    locked = usd_micros_to_sompi(usd_micros, Decimal("0.12500000"))
    assert required.amount == str(locked)
    assert (locked == min_price_sompi()) is floored
    assert required.extra is not None and required.extra.kasUsdRate == "0.12500000"
    # same converted total (ceil + floor) as v1; only the documented anti-front-running tag is added
    tag = offer.total_sompi - int(required.amount)
    assert 0 <= tag < K402_TAG_SPACE
    assert offer.fee_sompi == compute_fee_split(offer.total_sompi).fee_sompi
    assert offer.merchant_sompi + offer.fee_sompi == offer.total_sompi >= min_price_sompi()
    assert offer.pay_to == OTHER_ADDRESS and offer.expires == int(required.expiresAt.timestamp())
    record = json.loads(await redis.get(k402_offer_key(offer.payment_id)))
    assert record["kasUsdRate"] == "0.12500000"

    # a live rate change after the 402 must not re-quote the offer or the settlement's USD value
    stub_oracle.rate = Decimal("0.25000000")
    tx = build_tx(hotwallet_outputs(offer))
    node.add_to_mempool(tx)
    paid = await client.get("/p/usd-api", headers=k402_header(tx.id, offer.payment_id))
    assert paid.status_code == 200, paid.text
    (row,) = await settlements(sessionmaker)
    assert row.payment_scheme == "k402-kaspa-utxo" and row.amount_sompi == offer.total_sompi
    assert row.kas_usd_rate == Decimal("0.12500000")
    assert row.usd_value_at_settlement == usd_value(offer.total_sompi, Decimal("0.12500000"))


async def test_usd_endpoint_fails_closed_with_no_k402_offer_when_the_oracle_is_down(
    client: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    node: FakeNode,
    stub_oracle: StubOracle,
    upstream: Upstream,
    redis: Any,
) -> None:
    from app.models import PriceCurrency

    await seed_endpoint(
        sessionmaker,
        slug="usd-down",
        address=OTHER_ADDRESS,
        price_currency=PriceCurrency.USD,
        price_usd_micros=1_000_000,
        price_sompi=None,
    )
    stub_oracle.rate = None
    response = await client.get("/p/usd-down")
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "price_unavailable"
    assert body.get("accepts") is None and body.get("k402") is None
    assert HEADER_PAYMENT_REQUIRED not in response.headers
    assert await redis.keys("kasgate:k402:*") == [] and await redis.keys("kasgate:nonce:*") == []

    # a k402 retry for an offer that was never issued also fails closed before any lock or node lookup
    tx = build_tx(hotwallet_outputs(make_offer()))
    node.add_to_mempool(tx)
    retry = await client.get("/p/usd-down", headers=k402_header(tx.id, "p_0123456789abcdef"))
    assert retry.status_code == 503 and retry.json()["error"] == "price_unavailable"
    assert retry.json().get("accepts") is None
    assert await redis.keys("kasgate:k402*") == [] and await redis.keys("kasgate:tx:*") == []
    assert await settlements(sessionmaker) == [] and upstream.requests == []
