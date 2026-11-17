"""Red-team of the gateway's payment verifier with NO services running.

Transactions are built with the SDK (valid case) and with txforge (malicious cases) from synthetic UTXOs and fed to
the gateway's own `KaspaEngine.verify`, the same code the proxy calls before submitting to the node.
"""

from __future__ import annotations

import json

import kaspa
import pytest
from app.config import get_settings
from app.kaspa_engine import KaspaEngine, PaymentVerificationError
from app.schemas import PaymentRequired as GatewayPaymentRequired
from app.schemas import PaymentSignature as GatewayPaymentSignature
from kasgate_client.transaction import build_payment_transaction

from conftest import OFFLINE_PAYER_KEY, address_for_key
from txforge import forge_payment, tamper_raw_tx

KAS = 100_000_000
MERCHANT_KEY = "c7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfef"
ATTACKER_KEY = "e7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfef"
MERCHANT = address_for_key(MERCHANT_KEY)
ATTACKER = address_for_key(ATTACKER_KEY)
PAYER = address_for_key(OFFLINE_PAYER_KEY)
TREASURY = get_settings().kasgate_treasury_address
NETWORK = "kaspa:testnet-10"
PRICE = 2_000_000_000


def utxo(index: int, amount: int = 500 * KAS, address: str = PAYER) -> dict:
    script = kaspa.pay_to_address_script(kaspa.Address(address)).script
    return {
        "address": address,
        "outpoint": {"transactionId": f"{0xAB:02x}" * 31 + f"{index:02x}", "index": 0},
        "amount": amount,
        "scriptPublicKey": {"version": 0, "script": script},
        "blockDaaScore": 1000,
        "isCoinbase": False,
        "covenantId": None,
    }


@pytest.fixture(scope="module")
def engine() -> KaspaEngine:
    return KaspaEngine(get_settings())


@pytest.fixture(scope="module")
def required() -> GatewayPaymentRequired:
    return GatewayPaymentRequired.for_endpoint(PRICE, MERCHANT)


def forge_tx(outputs, key: str = OFFLINE_PAYER_KEY, utxos=None):
    return forge_payment(private_key_hex=key, network=NETWORK, utxos=utxos or [utxo(0)], outputs=outputs)


def verdict(engine: KaspaEngine, required, forged, **overrides) -> str:
    """Return the error code the engine produces for `forged`, or 'accepted'."""
    signature = GatewayPaymentSignature.model_validate(
        {"txId": forged.tx_id, "rawTx": forged.raw_tx, "payerAddress": forged.payer_address, **overrides}
    )
    try:
        engine.verify(signature, required)
    except PaymentVerificationError as exc:
        return exc.code
    return "accepted"


def exact(required: GatewayPaymentRequired) -> list[tuple[str, int]]:
    return [(required.payTo, required.merchant_amount_sompi), (required.platformFee.feeTo, required.fee_amount_sompi)]


def test_sdk_built_payment_is_accepted_by_the_gateway_engine(engine, required) -> None:
    built = build_payment_transaction(
        private_key=kaspa.PrivateKey(OFFLINE_PAYER_KEY),
        network=NETWORK,
        utxo_entries=[utxo(0)],
        pay_to=required.payTo,
        fee_to=required.platformFee.feeTo,
        merchant_sompi=required.merchant_amount_sompi,
        fee_sompi=required.fee_amount_sompi,
        max_network_fee_sompi=5_000_000,
    )
    signature = GatewayPaymentSignature(txId=built.tx_id, rawTx=built.raw_tx, payerAddress=built.payer_address)
    verified, tx = engine.verify(signature, required)
    assert verified.tx_id == built.tx_id == tx.id
    assert (verified.merchant_sompi, verified.fee_sompi, verified.total_sompi) == (
        required.merchant_amount_sompi,
        required.fee_amount_sompi,
        PRICE,
    )
    assert verified.payer_address == PAYER


def test_forged_payment_with_exact_outputs_is_accepted_control(engine, required) -> None:
    assert verdict(engine, required, forge_tx(exact(required))) == "accepted"


@pytest.mark.parametrize(
    ("label", "expected", "build"),
    [
        ("fee short by 1 sompi", "invalid_fee_split", lambda r: [(r.payTo, r.merchant_amount_sompi), (r.platformFee.feeTo, r.fee_amount_sompi - 1)]),
        ("fee output omitted (change takes slot 1)", "invalid_fee_split", lambda r: [(r.payTo, r.merchant_amount_sompi + r.fee_amount_sompi)]),
        ("fee over by 1 sompi", "invalid_fee_split", lambda r: [(r.payTo, r.merchant_amount_sompi), (r.platformFee.feeTo, r.fee_amount_sompi + 1)]),
        ("swapped output order", "invalid_fee_split", lambda r: [(r.platformFee.feeTo, r.fee_amount_sompi), (r.payTo, r.merchant_amount_sompi)]),
        ("wrong treasury address", "invalid_fee_split", lambda r: [(r.payTo, r.merchant_amount_sompi), (ATTACKER, r.fee_amount_sompi)]),
        ("merchant paid to someone else", "invalid_fee_split", lambda r: [(ATTACKER, r.merchant_amount_sompi), (r.platformFee.feeTo, r.fee_amount_sompi)]),
        ("merchant short by 1 sompi", "underpaid", lambda r: [(r.payTo, r.merchant_amount_sompi - 1), (r.platformFee.feeTo, r.fee_amount_sompi)]),
        ("merchant over by 1 sompi", "invalid_fee_split", lambda r: [(r.payTo, r.merchant_amount_sompi + 1), (r.platformFee.feeTo, r.fee_amount_sompi)]),
        ("merchant short by half", "underpaid", lambda r: [(r.payTo, r.merchant_amount_sompi // 2), (r.platformFee.feeTo, r.fee_amount_sompi)]),
        ("fee and merchant both paid to treasury", "invalid_fee_split", lambda r: [(r.platformFee.feeTo, r.merchant_amount_sompi), (r.platformFee.feeTo, r.fee_amount_sompi)]),
    ],
)
def test_tampered_outputs_are_rejected(engine, required, label, expected, build) -> None:
    assert verdict(engine, required, forge_tx(build(required))) == expected, label


def test_forged_tx_id_is_rejected(engine, required) -> None:
    forged = forge_tx(exact(required))
    assert verdict(engine, required, forged, txId="ab" * 32) == "invalid_payment_header"


def test_forged_id_field_inside_raw_tx_cannot_pass_as_the_claimed_tx_id(engine, required) -> None:
    """Transaction.from_dict() keeps whatever `id` the JSON carries; the verifier must recompute it."""
    forged = forge_tx(exact(required))
    fake_id = "cd" * 32
    raw = tamper_raw_tx(forged, lambda d: d.update(id=fake_id))
    assert kaspa.Transaction.from_dict(json.loads(raw)).id == fake_id  # SDK trap this test guards against
    signature = GatewayPaymentSignature(txId=fake_id, rawTx=raw, payerAddress=forged.payer_address)
    with pytest.raises(PaymentVerificationError) as caught:
        engine.verify(signature, required)
    assert caught.value.code == "invalid_payment_header"


def test_tx_id_replaced_by_real_id_but_other_body_is_rejected(engine, required) -> None:
    """A tx whose body changed (extra output) but still claims the original id must not verify."""
    forged = forge_tx(exact(required))

    def add_output(data: dict) -> None:
        data["outputs"].insert(0, data["outputs"][-1])

    raw = tamper_raw_tx(forged, add_output)
    signature = GatewayPaymentSignature(txId=forged.tx_id, rawTx=raw, payerAddress=forged.payer_address)
    with pytest.raises(PaymentVerificationError):
        engine.verify(signature, required)


def test_payer_address_must_own_an_input(engine, required) -> None:
    forged = forge_tx(exact(required))
    assert verdict(engine, required, forged, payerAddress=ATTACKER) == "invalid_payment_header"


def test_mainnet_payer_address_is_refused_by_the_schema() -> None:
    forged = forge_tx(exact(GatewayPaymentRequired.for_endpoint(PRICE, MERCHANT)))
    with pytest.raises(ValueError):
        GatewayPaymentSignature(
            txId=forged.tx_id, rawTx=forged.raw_tx, payerAddress="kaspa:" + forged.payer_address.split(":", 1)[1]
        )


def test_challenge_for_wrong_network_is_wrong_network(engine, required) -> None:
    forged = forge_tx(exact(required))
    signature = GatewayPaymentSignature(txId=forged.tx_id, rawTx=forged.raw_tx, payerAddress=forged.payer_address)
    mainnet = required.model_copy(update={"network": "kaspa:mainnet"})
    with pytest.raises(PaymentVerificationError) as caught:
        engine.verify(signature, mainnet)
    assert caught.value.code == "wrong_network"


@pytest.mark.parametrize(
    "raw",
    ["null", "[]", "{}", '{"id": 1}', "x" * 10, json.dumps({"inputs": [], "outputs": []})],
)
def test_garbage_raw_tx_is_an_invalid_header_not_a_crash(engine, required, raw) -> None:
    signature = GatewayPaymentSignature(txId="ab" * 32, rawTx=raw, payerAddress=PAYER)
    with pytest.raises(PaymentVerificationError) as caught:
        engine.verify(signature, required)
    assert caught.value.code == "invalid_payment_header"


def test_unsigned_transaction_passes_the_offline_verifier_and_relies_on_the_node() -> None:
    """Documents GAP-3: verify() never checks signature scripts; only node submission does."""
    required = GatewayPaymentRequired.for_endpoint(PRICE, MERCHANT)
    engine = KaspaEngine(get_settings())
    forged = forge_tx(exact(required))

    def strip_signatures(data: dict) -> None:
        for entry in data["inputs"]:
            entry["signatureScript"] = ""

    raw = tamper_raw_tx(forged, strip_signatures)
    signature = GatewayPaymentSignature(txId=forged.tx_id, rawTx=raw, payerAddress=forged.payer_address)
    verified, _ = engine.verify(signature, required)
    assert verified.tx_id == forged.tx_id


def test_inflated_embedded_utxo_amount_passes_offline_and_relies_on_the_node() -> None:
    """Documents GAP-4: inputs[].utxo is attacker-supplied and used for the mass check and payer attribution."""
    required = GatewayPaymentRequired.for_endpoint(PRICE, MERCHANT)
    engine = KaspaEngine(get_settings())
    forged = forge_tx(exact(required), utxos=[utxo(0, 25 * KAS)])

    def inflate(data: dict) -> None:
        for entry in data["inputs"]:
            entry["utxo"]["amount"] = 10**15

    raw = tamper_raw_tx(forged, inflate)
    signature = GatewayPaymentSignature(txId=forged.tx_id, rawTx=raw, payerAddress=forged.payer_address)
    verified, _ = engine.verify(signature, required)
    assert verified.payer_address == forged.payer_address

