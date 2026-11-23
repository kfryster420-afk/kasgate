"""KaspaEngine kaspa-exact-v2 methods: node UTXO lookup, submission and `accepted` finality (CONTRACT v2.10)."""

from datetime import UTC, datetime

import pytest
from kaspa import PrivateKey

from app.exact_v2 import ExactV2Verification
from app.kaspa_engine import KaspaEngine, PaymentVerificationError, RpcUnavailableError
from app.x402_v2 import ExactV2PaymentPayload
from tests.exact_v2_support import ExactPaymentSpec, Funding, address_of, build_payment, request_hash_for
from tests.test_exact_v2 import URL, requirement
from tests.test_kaspa_engine import FakeClient, make_engine, mode, node_utxo_entry

PAYER = PrivateKey("03" * 32)
FUNDING_AMOUNT = 2_500_000_000


def payment_spec() -> ExactPaymentSpec:
    return ExactPaymentSpec(
        requirement=requirement(), url=URL, fundings=[Funding(amount=FUNDING_AMOUNT, key=PAYER, tx_id="70" * 32)]
    )


def node_entry(spec: ExactPaymentSpec, amount: int = FUNDING_AMOUNT) -> dict[str, object]:
    built = build_payment(spec)
    script = built.artifact["inputs"][0]["utxo"]["scriptPublicKey"][4:]
    return node_utxo_entry(address_of(PAYER), "70" * 32, 0, amount, script)


def verified(engine: KaspaEngine, spec: ExactPaymentSpec) -> ExactV2Verification:
    built = build_payment(spec)
    return engine.verify_exact_v2(
        ExactV2PaymentPayload.model_validate(built.payload), spec.requirement, request_hash=request_hash_for(spec)
    )


async def test_resolve_settle_submits_and_waits_for_acceptance() -> None:
    spec = payment_spec()
    client = FakeClient(utxos={("70" * 32, 0): node_entry(spec)}, accept_after_polls=2)
    engine, _ = make_engine(client)
    check = verified(engine, spec)
    await engine.resolve_exact_v2_inputs(check)
    assert client.utxo_calls == [[address_of(PAYER)]]
    payment = await engine.settle_exact_v2(check)
    assert payment.fee_sompi == 0 and payment.merchant_sompi == payment.total_sompi
    assert [item["transaction"].id for item in client.submitted] == [check.payment.tx_id]
    assert client.events.index("sink") < client.events.index("submit") < client.events.index("chain")
    assert len(client.chain_requests) == 3


async def test_v2_always_waits_for_acceptance_even_in_mempool_mode() -> None:
    spec = payment_spec()
    client = FakeClient(utxos={("70" * 32, 0): node_entry(spec)}, accept=False)
    engine, _ = make_engine(client, settings=mode("mempool", timeout=0.1))
    check = verified(engine, spec)
    with pytest.raises(RpcUnavailableError, match="not accepted"):
        await engine.settle_exact_v2(check)
    assert client.chain_requests  # polled the virtual chain although settlement_mode is "mempool"


async def test_missing_node_utxo_is_rejected_before_submission() -> None:
    spec = payment_spec()
    client = FakeClient(utxos={})
    engine, _ = make_engine(client)
    with pytest.raises(PaymentVerificationError) as info:
        await engine.resolve_exact_v2_inputs(verified(engine, spec))
    assert info.value.code == "invalid_exact_payment"
    assert "does not know as unspent" in info.value.message
    assert client.submitted == []


async def test_embedded_utxo_amount_must_match_the_chain() -> None:
    spec = payment_spec()
    client = FakeClient(utxos={("70" * 32, 0): node_entry(spec, amount=FUNDING_AMOUNT + 1)})
    engine, _ = make_engine(client)
    with pytest.raises(PaymentVerificationError, match="does not match the node"):
        await engine.resolve_exact_v2_inputs(verified(engine, spec))


async def test_node_rejection_is_tx_rejected_and_duplicate_is_success() -> None:
    spec = payment_spec()
    engine, client = make_engine(FakeClient(submit_results=[Exception("Rejected transaction: insufficient fee")]))
    with pytest.raises(PaymentVerificationError) as info:
        await engine.settle_exact_v2(verified(engine, spec))
    assert info.value.code == "tx_rejected"

    check = verified(engine, spec)
    tx_id = check.payment.tx_id
    duplicate = Exception(f"Rejected transaction {tx_id}: transaction {tx_id} has already been accepted")
    engine, client = make_engine(FakeClient(submit_results=[duplicate]))
    assert (await engine.settle_exact_v2(verified(engine, spec))).tx_id == tx_id
    assert client.chain_requests == []


async def test_unreachable_node_is_rpc_unavailable() -> None:
    spec = payment_spec()
    engine, _ = make_engine(FakeClient(utxo_error=Exception("websocket disconnected")))
    with pytest.raises(RpcUnavailableError):
        await engine.resolve_exact_v2_inputs(verified(engine, spec))


def test_requirement_on_another_network_is_wrong_network() -> None:
    spec = payment_spec()
    engine, _ = make_engine()
    built = build_payment(spec)
    other = spec.requirement.model_copy(update={"network": "kaspa:mainnet"})
    with pytest.raises(PaymentVerificationError) as info:
        engine.verify_exact_v2(
            ExactV2PaymentPayload.model_validate(built.payload), other, request_hash=built.request_hash
        )
    assert info.value.code == "wrong_network"


def test_verify_is_pure_and_can_skip_expiry_for_settled_retries() -> None:
    spec = payment_spec()
    engine, client = make_engine()
    built = build_payment(spec)
    payment = ExactV2PaymentPayload.model_validate(built.payload)
    later = datetime(2999, 1, 1, tzinfo=UTC)
    with pytest.raises(PaymentVerificationError, match="expired_authorization"):
        engine.verify_exact_v2(payment, spec.requirement, request_hash=built.request_hash, now=later)
    check = engine.verify_exact_v2(
        payment, spec.requirement, request_hash=built.request_hash, now=later, check_expiry=False
    )
    assert check.payment.tx_id == built.tx_id
    assert client.events == [] and client.connect_calls == 0
