"""Red-team of the gateway's payment verifier with NO services running.

Transactions are built with the SDK (valid case) and with txforge (malicious cases) from synthetic UTXOs and fed to
the gateway's own `KaspaEngine.verify`, the same code the proxy calls before submitting to the node.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import kaspa
import pytest
from app.config import get_settings
from app.kaspa_engine import KaspaEngine, PaymentVerificationError, RpcUnavailableError
from app.schemas import PaymentRequired as GatewayPaymentRequired
from app.schemas import PaymentSignature as GatewayPaymentSignature
from app.schemas import compute_fee_split, min_price_sompi, payment_payload
from kasgate_client.transaction import build_payment_transaction

from conftest import OFFLINE_PAYER_KEY, address_for_key
from txforge import forge_exact, forge_payment, tamper_raw_tx

KAS = 100_000_000
MERCHANT_KEY = "c7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfef"
ATTACKER_KEY = "e7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfef"
MERCHANT = address_for_key(MERCHANT_KEY)
ATTACKER = address_for_key(ATTACKER_KEY)
PAYER = address_for_key(OFFLINE_PAYER_KEY)
TREASURY = get_settings().kasgate_treasury_address
NETWORK = "kaspa:testnet-10"
PRICE = 2_000_000_000
FLOOR = get_settings().min_output_sompi
MIN_PRICE = min_price_sompi(FLOOR)  # CONTRACT v2.1: 40_000_000
NONCE = "0123456789abcdef" * 2
OTHER_NONCE = "fedcba9876543210" * 2
CROSSOVER = (MIN_PRICE, 1_333_333_333, 1_333_333_334, 1_333_333_335)


def make_required(price: int = PRICE, pay_to: str = MERCHANT, nonce: str = NONCE) -> GatewayPaymentRequired:
    return GatewayPaymentRequired.for_endpoint(price, pay_to, nonce, datetime.now(UTC) + timedelta(seconds=120))


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
    return make_required()


def forge_tx(outputs, key: str = OFFLINE_PAYER_KEY, utxos=None, **kwargs):
    kwargs.setdefault("nonce", NONCE)
    return forge_payment(private_key_hex=key, network=NETWORK, utxos=utxos or [utxo(0)], outputs=outputs, **kwargs)


def verdict(engine: KaspaEngine, required, forged, **overrides) -> str:
    """Return the error code the engine produces for `forged`, or 'accepted'."""
    signature = GatewayPaymentSignature.model_validate(
        {
            "txId": forged.tx_id,
            "rawTx": forged.raw_tx,
            "payerAddress": forged.payer_address,
            "nonce": NONCE,
            **overrides,
        }
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
        payload=payment_payload(required.nonce),
    )
    signature = GatewayPaymentSignature(
        txId=built.tx_id, rawTx=built.raw_tx, payerAddress=built.payer_address, nonce=required.nonce
    )
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
    signature = GatewayPaymentSignature(txId=fake_id, rawTx=raw, payerAddress=forged.payer_address, nonce=NONCE)
    with pytest.raises(PaymentVerificationError) as caught:
        engine.verify(signature, required)
    assert caught.value.code == "invalid_payment_header"


def test_tx_id_replaced_by_real_id_but_other_body_is_rejected(engine, required) -> None:
    """A tx whose body changed (extra output) but still claims the original id must not verify."""
    forged = forge_tx(exact(required))

    def add_output(data: dict) -> None:
        data["outputs"].insert(0, data["outputs"][-1])

    raw = tamper_raw_tx(forged, add_output)
    signature = GatewayPaymentSignature(txId=forged.tx_id, rawTx=raw, payerAddress=forged.payer_address, nonce=NONCE)
    with pytest.raises(PaymentVerificationError):
        engine.verify(signature, required)


def test_payer_address_must_own_an_input(engine, required) -> None:
    forged = forge_tx(exact(required))
    assert verdict(engine, required, forged, payerAddress=ATTACKER) == "invalid_payment_header"


def test_mainnet_payer_address_is_refused_by_the_schema() -> None:
    forged = forge_tx(exact(make_required()))
    with pytest.raises(ValueError):
        GatewayPaymentSignature(
            txId=forged.tx_id,
            rawTx=forged.raw_tx,
            payerAddress="kaspa:" + forged.payer_address.split(":", 1)[1],
            nonce=NONCE,
        )


def test_challenge_for_wrong_network_is_wrong_network(engine, required) -> None:
    forged = forge_tx(exact(required))
    signature = GatewayPaymentSignature(txId=forged.tx_id, rawTx=forged.raw_tx, payerAddress=forged.payer_address, nonce=NONCE)
    mainnet = required.model_copy(update={"network": "kaspa:mainnet"})
    with pytest.raises(PaymentVerificationError) as caught:
        engine.verify(signature, mainnet)
    assert caught.value.code == "wrong_network"


@pytest.mark.parametrize(
    "raw",
    ["null", "[]", "{}", '{"id": 1}', "x" * 10, json.dumps({"inputs": [], "outputs": []})],
)
def test_garbage_raw_tx_is_an_invalid_header_not_a_crash(engine, required, raw) -> None:
    signature = GatewayPaymentSignature(txId="ab" * 32, rawTx=raw, payerAddress=PAYER, nonce=NONCE)
    with pytest.raises(PaymentVerificationError) as caught:
        engine.verify(signature, required)
    assert caught.value.code == "invalid_payment_header"


def test_unsigned_transaction_passes_the_offline_verifier_and_relies_on_the_node() -> None:
    """Documents GAP-3: verify() never checks signature scripts; only node submission does."""
    required = make_required()
    engine = KaspaEngine(get_settings())
    forged = forge_tx(exact(required))

    def strip_signatures(data: dict) -> None:
        for entry in data["inputs"]:
            entry["signatureScript"] = ""

    raw = tamper_raw_tx(forged, strip_signatures)
    signature = GatewayPaymentSignature(txId=forged.tx_id, rawTx=raw, payerAddress=forged.payer_address, nonce=NONCE)
    verified, _ = engine.verify(signature, required)
    assert verified.tx_id == forged.tx_id


def test_inflated_embedded_utxo_amount_passes_offline_and_relies_on_the_node() -> None:
    """Documents GAP-4: inputs[].utxo is attacker-supplied and used for the mass check and payer attribution."""
    required = make_required()
    engine = KaspaEngine(get_settings())
    forged = forge_tx(exact(required), utxos=[utxo(0, 25 * KAS)])

    def inflate(data: dict) -> None:
        for entry in data["inputs"]:
            entry["utxo"]["amount"] = 10**15

    raw = tamper_raw_tx(forged, inflate)
    signature = GatewayPaymentSignature(txId=forged.tx_id, rawTx=raw, payerAddress=forged.payer_address, nonce=NONCE)
    verified, _ = engine.verify(signature, required)
    assert verified.payer_address == forged.payer_address



# ---------------------------------------------------------------- CONTRACT v2: nonce binding through the tx payload


def test_missing_payload_is_rejected(engine, required) -> None:
    forged = forge_tx(exact(required), payload=b"")
    assert forged.payload == b""
    assert verdict(engine, required, forged) == "invalid_payment_header"


@pytest.mark.parametrize(
    "payload",
    [
        payment_payload(OTHER_NONCE),  # bound to another challenge
        payment_payload(NONCE) + b"x",  # trailing byte
        payment_payload(NONCE)[:-1],  # truncated
        payment_payload(NONCE).upper(),  # case change
        NONCE.encode(),  # nonce without the prefix
        b"kasgate:v0:" + NONCE.encode(),  # wrong prefix version
        b"\x00" + payment_payload(NONCE),  # leading NUL
    ],
)
def test_wrong_payload_is_rejected(engine, required, payload) -> None:
    forged = forge_tx(exact(required), payload=payload)
    assert verdict(engine, required, forged) == "invalid_payment_header"


def test_header_nonce_must_match_the_challenge_nonce(engine, required) -> None:
    forged = forge_tx(exact(required))  # tx payload is bound to NONCE
    assert verdict(engine, required, forged, nonce=OTHER_NONCE) == "invalid_payment_header"


def test_tx_forged_for_another_nonce_cannot_satisfy_this_challenge(engine, required) -> None:
    forged = forge_tx(exact(required), nonce=OTHER_NONCE)
    assert verdict(engine, required, forged) == "invalid_payment_header"  # header says NONCE, payload says OTHER
    assert verdict(engine, required, forged, nonce=OTHER_NONCE) == "invalid_payment_header"  # challenge is for NONCE


def test_payload_cannot_be_swapped_after_signing(engine, required) -> None:
    """The payload is covered by the tx id: rewriting it makes the claimed id (and the signatures) invalid."""
    forged = forge_tx(exact(required), nonce=OTHER_NONCE)
    raw = tamper_raw_tx(forged, lambda d: d.update(payload=payment_payload(NONCE).hex()))
    assert verdict(engine, required, forged, rawTx=raw) == "invalid_payment_header"


def test_payload_swap_with_recomputed_id_still_fails_at_the_node_not_in_verify(engine, required) -> None:
    """Documents the layering: a payload rewrite plus recomputed id passes verify() (it does not check signature
    scripts, see GAP-3) and is stopped only by node signature validation, which sighash-covers the payload."""
    forged = forge_tx(exact(required), nonce=OTHER_NONCE)
    data = json.loads(forged.raw_tx)
    data["payload"] = payment_payload(NONCE).hex()
    rewritten = kaspa.Transaction.from_dict(data)
    new_id = str(rewritten.finalize())
    assert new_id != forged.tx_id
    signature = GatewayPaymentSignature(
        txId=new_id, rawTx=json.dumps(rewritten.to_dict()), payerAddress=forged.payer_address, nonce=NONCE
    )
    verified, _ = engine.verify(signature, required)  # accepted offline
    assert verified.tx_id == new_id


# ---------------------------------------------------------------- CONTRACT v2.1: fee floor boundary on the real engine


def test_gateway_boundary_values() -> None:
    assert FLOOR == 20_000_000 and MIN_PRICE == 60_000_000 == 3 * FLOOR  # CONTRACT v2.3
    for total in (1, 39_999_999, 40_000_000, 59_999_999):
        with pytest.raises(ValueError):
            compute_fee_split(total, min_output_sompi=FLOOR)
    expected = {
        60_000_000: (40_000_000, 20_000_000),
        1_333_333_332: (1_313_333_332, 20_000_000),
        1_333_333_333: (1_313_333_333, 20_000_000),
        1_333_333_334: (1_313_333_333, 20_000_001),
        1_333_333_335: (1_313_333_334, 20_000_001),
    }
    for total, (merchant, fee) in expected.items():
        split = compute_fee_split(total, min_output_sompi=FLOOR)
        assert (split.merchant_sompi, split.fee_sompi) == (merchant, fee), total


def test_below_minimum_price_cannot_even_be_challenged() -> None:
    for price in (1, FLOOR, 39_999_999, 40_000_000, MIN_PRICE - 1):
        with pytest.raises(ValueError):
            make_required(price)


@pytest.mark.parametrize("price", CROSSOVER)
def test_exact_outputs_are_accepted_at_every_boundary_price(engine, price) -> None:
    required = make_required(price)
    assert verdict(engine, required, forge_tx(exact(required))) == "accepted"
    verified, _ = engine.verify(
        GatewayPaymentSignature.model_validate(
            {
                "txId": (f := forge_tx(exact(required))).tx_id,
                "rawTx": f.raw_tx,
                "payerAddress": f.payer_address,
                "nonce": NONCE,
            }
        ),
        required,
    )
    assert verified.total_sompi == price
    assert verified.fee_sompi == max(-(-price * 150 // 10_000), FLOOR)


@pytest.mark.parametrize("price", CROSSOVER)
def test_one_sompi_off_is_rejected_at_every_boundary_price(engine, price) -> None:
    required = make_required(price)
    merchant, fee = required.merchant_amount_sompi, required.fee_amount_sompi
    cases = {
        "merchant+1": ([(MERCHANT, merchant + 1), (required.platformFee.feeTo, fee)], "invalid_fee_split"),
        "fee+1": ([(MERCHANT, merchant), (required.platformFee.feeTo, fee + 1)], "invalid_fee_split"),
        "merchant-1": ([(MERCHANT, merchant - 1), (required.platformFee.feeTo, fee)], "underpaid"),
    }
    for label, (outputs, code) in cases.items():
        assert verdict(engine, required, forge_tx(outputs)) == code, (price, label)


@pytest.mark.parametrize("fee", [900_000, 1_000_000, 1])
def test_far_below_floor_outputs_are_unrelayable_and_cannot_even_be_forged(fee: int) -> None:
    """Pre-v2.1 clients paid ceil(1.5%) to the treasury. KIP-9 storage mass makes such a transaction unrelayable:
    kaspa.Generator refuses to build it, so no payer can be charged for one."""
    required = make_required(MIN_PRICE)
    with pytest.raises(Exception, match="Storage mass"):
        forge_tx([(MERCHANT, MIN_PRICE - fee), (required.platformFee.feeTo, fee)])


@pytest.mark.parametrize(
    ("merchant_delta", "fee_delta", "code"),
    [
        (0, -1, "invalid_fee_split"),  # treasury one sompi under the floor
        (-1, 0, "underpaid"),  # merchant one sompi under 2*floor
        (-1, +1, "invalid_fee_split"),
        (+1, -1, "invalid_fee_split"),
        (-20_000_000, 0, "underpaid"),  # the v2.1 split (20M merchant): below the v2.3 merchant minimum
    ],
)
def test_one_sompi_off_the_minimum_price_split_is_rejected_by_the_engine(engine, merchant_delta, fee_delta, code) -> None:
    required = make_required(MIN_PRICE)
    forged = forge_tx(
        [
            (MERCHANT, required.merchant_amount_sompi + merchant_delta),
            (required.platformFee.feeTo, required.fee_amount_sompi + fee_delta),
        ]
    )
    assert verdict(engine, required, forged) == code


# ---------------------------------------------------------------- CONTRACT v2.3: storage mass with change, 16 addresses


def _change_forge(required, change: int, *, key: str = OFFLINE_PAYER_KEY, address: str = PAYER):
    total = required.merchant_amount_sompi + required.fee_amount_sompi
    fee = 300_000
    return forge_exact(
        private_keys=[key],
        network=NETWORK,
        utxos=[utxo(0, total + change + fee)],
        outputs=[(MERCHANT, required.merchant_amount_sompi), (required.platformFee.feeTo, required.fee_amount_sompi), (address, change)],
        payer_address=address,
        nonce=NONCE,
    )


def test_minimum_price_payment_with_comfortable_change_is_accepted(engine) -> None:
    required = make_required(MIN_PRICE)
    assert verdict(engine, required, _change_forge(required, 30_000_000)) == "accepted"
    assert verdict(engine, required, _change_forge(required, 100 * KAS)) == "accepted"


def test_minimum_price_payment_with_small_change_is_a_non_standard_transaction(engine) -> None:
    """CONTRACT v2.3: at the minimum price the outputs use most of the 100_000 storage mass, so change below ~0.3 KAS
    makes the transaction non-standard. The SDK never builds one; the engine must refuse one a hand-rolled client sends."""
    required = make_required(MIN_PRICE)
    for change in (100_000, 1_000_000, 5_000_000, 10_000_000):
        assert verdict(engine, required, _change_forge(required, change)) == "tx_rejected", change


def test_change_threshold_matches_the_contract() -> None:
    """The SDK's 30_000_000 minimum change is on the safe side of the engine's mass limit at the minimum price."""
    required = make_required(MIN_PRICE)
    accepted = [c for c in range(10_000_000, 40_000_001, 1_000_000) if verdict(FakeNodeEngine([]), required, _change_forge(required, c)) == "accepted"]
    assert accepted, "no change value in 10M..40M was accepted"
    assert min(accepted) <= 30_000_000, min(accepted)
    assert 27_000_000 not in accepted  # measured threshold at the minimum price: 27_477_812 sompi


def _addresses(count: int) -> tuple[list[str], list[str]]:
    keys = [f"{i + 1:064x}" for i in range(count)]
    return keys, [address_for_key(k) for k in keys]


def _multi_address_forge(required, count: int):
    keys, addresses = _addresses(count)
    funds = [utxo(i, 2 * KAS, address) for i, address in enumerate(addresses)]
    total = required.merchant_amount_sompi + required.fee_amount_sompi
    forged = forge_exact(
        private_keys=keys,
        network=NETWORK,
        utxos=funds,
        outputs=[
            (required.payTo, required.merchant_amount_sompi),
            (required.platformFee.feeTo, required.fee_amount_sompi),
            (addresses[0], sum(f["amount"] for f in funds) - total - 1_000_000),
        ],
        payer_address=addresses[0],
        nonce=NONCE,
    )
    return forged, funds


async def test_payment_from_sixteen_addresses_is_the_accepted_maximum() -> None:
    required = make_required(PRICE)
    forged, funds = _multi_address_forge(required, 16)
    signature = _signature_for(forged)
    engine = FakeNodeEngine(funds)
    _, tx = engine.verify(signature, required)
    resolved = await engine.resolve_inputs(signature, tx)
    assert len(resolved.values) == 16


async def test_payment_from_seventeen_addresses_is_rejected() -> None:
    required = make_required(PRICE)
    forged, funds = _multi_address_forge(required, 17)
    assert len(forged.tx.inputs) == 17
    signature = _signature_for(forged)
    engine = FakeNodeEngine(funds)
    _, tx = engine.verify(signature, required)  # the pure pass cannot see it: only the node lookup enforces the cap
    with pytest.raises(PaymentVerificationError) as caught:
        await engine.resolve_inputs(signature, tx)
    assert caught.value.code == "invalid_payment_header" and "16" in str(caught.value)


# ---------------------------------------------------------------- CONTRACT v2: node-verified inputs (GAP-4 fix)


class FakeNodeEngine(KaspaEngine):
    """KaspaEngine whose RPC layer answers from a fixed UTXO set instead of a node."""

    def __init__(self, entries: list[dict], *, down: bool = False) -> None:
        super().__init__(get_settings())
        self._entries = entries
        self._down = down

    async def _rpc(self, name, call):  # type: ignore[override]
        if self._down:
            raise RpcUnavailableError("node unreachable")
        if name == "get_utxos_by_addresses":
            return {
                "entries": [
                    {
                        "address": e["address"],
                        "outpoint": e["outpoint"],
                        "utxoEntry": {
                            "amount": e["amount"],
                            "scriptPublicKey": "0000" + e["scriptPublicKey"]["script"],
                            "blockDaaScore": 1,
                            "isCoinbase": False,
                        },
                    }
                    for e in self._entries
                ]
            }
        if name == "get_mempool_entries_by_addresses":
            return {"entries": []}
        raise AssertionError(f"unexpected rpc {name}")


def _signature_for(forged, nonce: str = NONCE) -> GatewayPaymentSignature:
    return GatewayPaymentSignature(
        txId=forged.tx_id, rawTx=forged.raw_tx, payerAddress=forged.payer_address, nonce=nonce
    )


async def test_inputs_matching_the_node_are_accepted(required) -> None:
    funds = [utxo(0)]
    forged = forge_tx(exact(required), utxos=funds)
    signature = _signature_for(forged)
    engine = FakeNodeEngine(funds)
    _, tx = engine.verify(signature, required)
    inputs = await engine.resolve_inputs(signature, tx)
    assert inputs.values == (500 * KAS,)


async def test_inflated_embedded_utxo_amount_is_caught_by_the_node_lookup(required) -> None:
    funds = [utxo(0, 25 * KAS)]
    forged = forge_tx(exact(required), utxos=funds)

    def inflate(data: dict) -> None:
        for entry in data["inputs"]:
            entry["utxo"]["amount"] = 10**15

    signature = GatewayPaymentSignature(
        txId=forged.tx_id,
        rawTx=tamper_raw_tx(forged, inflate),
        payerAddress=forged.payer_address,
        nonce=NONCE,
    )
    engine = FakeNodeEngine(funds)
    _, tx = engine.verify(signature, required)  # the offline pass cannot see the lie (GAP-3/4 layering)
    with pytest.raises(PaymentVerificationError) as caught:
        await engine.resolve_inputs(signature, tx)
    assert caught.value.code == "invalid_payment_header"


async def test_input_unknown_to_the_node_is_rejected(required) -> None:
    funds = [utxo(0)]
    forged = forge_tx(exact(required), utxos=funds)
    signature = _signature_for(forged)
    engine = FakeNodeEngine([utxo(7)])  # the node knows a different outpoint
    _, tx = engine.verify(signature, required)
    with pytest.raises(PaymentVerificationError) as caught:
        await engine.resolve_inputs(signature, tx)
    assert caught.value.code == "invalid_payment_header"


async def test_node_outage_during_input_lookup_is_retryable_not_a_verdict(required) -> None:
    funds = [utxo(0)]
    forged = forge_tx(exact(required), utxos=funds)
    signature = _signature_for(forged)
    engine = FakeNodeEngine(funds, down=True)
    _, tx = engine.verify(signature, required)
    with pytest.raises(RpcUnavailableError):
        await engine.resolve_inputs(signature, tx)
