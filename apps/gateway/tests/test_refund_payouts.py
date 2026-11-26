"""CONTRACT v2.13: the real engine's refund payout checks (exact amount to the buyer, payload binding, visibility)."""

import json
import uuid
from typing import Any

import pytest
from kaspa import Address, PaymentOutput, PrivateKey, Transaction, create_transactions

from app.kaspa_engine import PaymentVerificationError, RpcUnavailableError, check_payout_outputs
from app.schemas import refund_payout_payload
from tests.conftest import MERCHANT_KEY, OTHER_ADDRESS, PAYER_ADDRESS
from tests.test_k402 import FakeNode, _script_hex, make_engine, payer_entries

AMOUNT = 25_000_000
MAINNET_ADDRESS = MERCHANT_KEY.to_address("mainnet").to_string()
REFUND_ID = uuid.UUID("0123456789abcdef0123456789abcdef")


class SubmittingNode(FakeNode):
    """A FakeNode that accepts `submit_transaction` (into its mempool) or refuses it with `submit_error`."""

    submitted: list[str]
    submit_error: Exception | None

    def __init__(self) -> None:
        super().__init__()
        self.submitted = []
        self.submit_error = None

    async def submit_transaction(self, request: dict[str, Any]) -> Any:
        if self.submit_error is not None:
            raise self.submit_error
        tx: Transaction = request["transaction"]
        self.submitted.append(tx.id)
        self.add_to_mempool(tx)
        return {"transactionId": tx.id}


class PayloadMempoolNode(SubmittingNode):
    """Mempool replies carry each transaction's payload too (kaspad's RpcTransaction does)."""

    def __init__(self) -> None:
        super().__init__()
        self.payloads: dict[str, str] = {}

    def add_to_mempool(self, tx: Transaction) -> None:
        super().add_to_mempool(tx)
        self.payloads[tx.id] = str(tx.to_dict().get("payload") or "")

    async def get_mempool_entries_by_addresses(self, request: dict[str, Any]) -> dict[str, Any]:
        reply = await super().get_mempool_entries_by_addresses(request)
        for entry in reply["entries"]:
            for received in entry["receiving"]:
                tx = received["transaction"]
                tx["payload"] = self.payloads.get(tx["verboseData"]["transactionId"], "")
        return reply


def payout_tx(
    outputs: list[tuple[str, int]], *, payload: bytes | None = None, key: PrivateKey = MERCHANT_KEY
) -> Transaction:
    """A merchant-wallet payment (no treasury output), optionally carrying the refund payload."""
    kwargs: dict[str, Any] = {} if payload is None else {"payload": payload}
    bundle = create_transactions(
        network_id="testnet-10",
        entries=payer_entries(key),
        outputs=[PaymentOutput(Address(address), amount) for address, amount in outputs],
        change_address=key.to_address("testnet"),
        priority_fee=0,
        **kwargs,
    )
    pending = bundle["transactions"][-1]
    pending.sign([key])
    tx: Transaction = pending.transaction
    return tx


def wire(tx: Transaction) -> str:
    return json.dumps(tx.to_dict())


# ---------------------------------------------------------------- pure output check


def test_output_check_requires_an_exact_output_to_the_buyer() -> None:
    buyer = (0, _script_hex(PAYER_ADDRESS))
    other = (0, _script_hex(OTHER_ADDRESS))
    tx_id = "ab" * 32
    with pytest.raises(PaymentVerificationError, match="no output to the buyer"):
        check_payout_outputs(tx_id, {}, PAYER_ADDRESS, AMOUNT)
    with pytest.raises(PaymentVerificationError, match="no output to the buyer"):
        check_payout_outputs(tx_id, {0: (AMOUNT, other)}, PAYER_ADDRESS, AMOUNT)
    with pytest.raises(PaymentVerificationError, match=r"found 24999999, 25000001"):
        check_payout_outputs(tx_id, {0: (AMOUNT - 1, buyer), 2: (AMOUNT + 1, buyer)}, PAYER_ADDRESS, AMOUNT)
    # Change back to the merchant and outputs in any position are fine; the exact buyer output is what counts.
    assert check_payout_outputs(tx_id, {0: (AMOUNT * 3, other), 1: (AMOUNT, buyer)}, PAYER_ADDRESS, AMOUNT) == 1
    with pytest.raises(PaymentVerificationError) as info:
        check_payout_outputs(tx_id, {0: (AMOUNT, buyer)}, "kaspa:nope", AMOUNT)
    assert info.value.code == "invalid_payout"


PAYLOAD = refund_payout_payload(REFUND_ID)


def bound_tx(outputs: list[tuple[str, int]], *, key: PrivateKey = MERCHANT_KEY) -> Transaction:
    """A payout carrying THIS refund's payload."""
    return payout_tx(outputs, payload=PAYLOAD, key=key)


async def test_engine_requires_the_refund_payload_and_acceptance() -> None:
    tx = bound_tx([(PAYER_ADDRESS, AMOUNT)])
    assert tx.to_dict()["payload"] == PAYLOAD.hex()
    node = PayloadMempoolNode()
    node.add_to_mempool(tx)
    await make_engine(node).verify_payout(tx.id, PAYER_ADDRESS, AMOUNT, REFUND_ID)
    assert node.calls[:2] == ["sink", "mempool"] and "chain" in node.calls

    # Mempool settlement mode does not shortcut payouts: a refund is paid only once its payout is accepted.
    node = PayloadMempoolNode()
    node.add_to_mempool(tx)
    node.accept_on_poll = False
    with pytest.raises(PaymentVerificationError) as info:
        await make_engine(node, settlement_mode="mempool", settlement_timeout_seconds=0.05).verify_payout(
            tx.id, PAYER_ADDRESS, AMOUNT, REFUND_ID
        )
    assert info.value.code == "payout_unconfirmed"


async def test_an_accepted_payout_counts_only_if_its_payload_was_seen() -> None:
    accepted = bound_tx([(PAYER_ADDRESS, AMOUNT)])
    node = PayloadMempoolNode()
    node.accept_into_utxo_index(accepted)
    with pytest.raises(PaymentVerificationError) as info:
        await make_engine(node).verify_payout(accepted.id, PAYER_ADDRESS, AMOUNT, REFUND_ID)
    assert info.value.code == "payload_unverified" and "transaction" in info.value.message
    await make_engine(node).verify_payout(accepted.id, PAYER_ADDRESS, AMOUNT, REFUND_ID, payload_checked=True)
    assert node.calls[-3:] == ["sink", "mempool", "utxos"]


async def test_engine_refuses_a_payout_that_is_not_exact_unbound_or_not_to_the_buyer() -> None:
    node = PayloadMempoolNode()
    cases = [
        (bound_tx([(PAYER_ADDRESS, AMOUNT - 1)]), f"exactly {AMOUNT}"),
        (bound_tx([(PAYER_ADDRESS, AMOUNT + 1)]), f"exactly {AMOUNT}"),
        (payout_tx([(PAYER_ADDRESS, AMOUNT)]), "payload"),  # the right amount, no payload
        (payout_tx([(PAYER_ADDRESS, AMOUNT)], payload=refund_payout_payload(uuid.uuid4())), "payload"),
        (payout_tx([(PAYER_ADDRESS, AMOUNT)], payload=b"kasgate:v1:refund:"), "payload"),
    ]
    for tx, needle in cases:
        node.add_to_mempool(tx)
        with pytest.raises(PaymentVerificationError) as info:
            await make_engine(node).verify_payout(tx.id, PAYER_ADDRESS, AMOUNT, REFUND_ID)
        assert info.value.code == "invalid_payout" and needle in info.value.message, needle
    elsewhere = bound_tx([(OTHER_ADDRESS, AMOUNT)])
    node.accept_into_utxo_index(elsewhere)
    with pytest.raises(PaymentVerificationError) as info:
        await make_engine(node).verify_payout(elsewhere.id, PAYER_ADDRESS, AMOUNT, REFUND_ID, payload_checked=True)
    assert info.value.code == "payment_not_found"  # nothing reaches the buyer
    with pytest.raises(PaymentVerificationError) as info:
        await make_engine(node).verify_payout(elsewhere.id, MAINNET_ADDRESS, AMOUNT, REFUND_ID)
    assert info.value.code == "wrong_network"


async def test_engine_payout_not_visible_or_not_accepted_yet() -> None:
    tx = bound_tx([(PAYER_ADDRESS, AMOUNT)])
    node = PayloadMempoolNode()
    with pytest.raises(PaymentVerificationError) as info:
        await make_engine(node).verify_payout(tx.id, PAYER_ADDRESS, AMOUNT, REFUND_ID)
    assert info.value.code == "payment_not_found"
    node.add_to_mempool(tx)
    node.accept_on_poll = False
    with pytest.raises(PaymentVerificationError) as info:
        await make_engine(node, settlement_timeout_seconds=0.05).verify_payout(tx.id, PAYER_ADDRESS, AMOUNT, REFUND_ID)
    assert info.value.code == "payout_unconfirmed"


async def test_payout_tx_id_checks_locally_and_sends_nothing() -> None:
    tx = bound_tx([(PAYER_ADDRESS, AMOUNT)])
    node = SubmittingNode()
    engine = make_engine(node)
    assert engine.payout_tx_id(wire(tx), PAYLOAD, PAYER_ADDRESS, AMOUNT) == tx.id
    with pytest.raises(PaymentVerificationError):
        engine.payout_tx_id(wire(payout_tx([(PAYER_ADDRESS, AMOUNT)])), PAYLOAD, PAYER_ADDRESS, AMOUNT)
    assert node.submitted == [] and node.calls == []


async def test_engine_submits_a_signed_payout_bound_to_the_refund() -> None:
    payload = refund_payout_payload(REFUND_ID)
    assert payload == b"kasgate:v1:refund:" + REFUND_ID.hex.encode()
    tx = payout_tx([(PAYER_ADDRESS, AMOUNT)], payload=payload)
    node = SubmittingNode()
    tx_id, accepted = await make_engine(node).submit_payout(wire(tx), payload, PAYER_ADDRESS, AMOUNT)
    assert (tx_id, accepted) == (tx.id, True) and node.submitted == [tx.id]

    node = SubmittingNode()
    node.accept_on_poll = False
    later = payout_tx([(PAYER_ADDRESS, AMOUNT)], payload=payload)
    tx_id, accepted = await make_engine(node, settlement_timeout_seconds=0.05).submit_payout(
        wire(later), payload, PAYER_ADDRESS, AMOUNT
    )
    assert (tx_id, accepted) == (later.id, False)


async def test_engine_refuses_unbound_or_wrong_raw_payouts_before_submitting() -> None:
    payload = refund_payout_payload(REFUND_ID)
    node = SubmittingNode()
    engine = make_engine(node)
    cases = [
        (wire(payout_tx([(PAYER_ADDRESS, AMOUNT)])), "payload"),
        (wire(payout_tx([(PAYER_ADDRESS, AMOUNT)], payload=refund_payout_payload(uuid.uuid4()))), "payload"),
        (wire(payout_tx([(PAYER_ADDRESS, AMOUNT - 1)], payload=payload)), "exactly"),
        (wire(payout_tx([(OTHER_ADDRESS, AMOUNT)], payload=payload)), "no output"),
        ("not json", "JSON"),
        ("[1, 2]", "object"),
        ('{"version": 0}', "not a valid Kaspa transaction"),
    ]
    for raw, needle in cases:
        with pytest.raises(PaymentVerificationError) as info:
            await engine.submit_payout(raw, payload, PAYER_ADDRESS, AMOUNT)
        assert info.value.code == "invalid_payout" and needle in info.value.message, raw[:40]
    assert node.submitted == []


async def test_engine_reports_node_refusals_and_outages() -> None:
    payload = refund_payout_payload(REFUND_ID)
    raw = wire(payout_tx([(PAYER_ADDRESS, AMOUNT)], payload=payload))
    node = SubmittingNode()
    node.submit_error = RuntimeError("transaction input already spent")
    with pytest.raises(PaymentVerificationError) as info:
        await make_engine(node).submit_payout(raw, payload, PAYER_ADDRESS, AMOUNT)
    assert info.value.code == "tx_rejected"
    node.submit_error = RuntimeError("websocket connection closed")
    with pytest.raises(RpcUnavailableError):
        await make_engine(node).submit_payout(raw, payload, PAYER_ADDRESS, AMOUNT)
