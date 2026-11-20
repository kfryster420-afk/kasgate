"""GAP-1 closed by CONTRACT v2.6: a mempool observer cannot claim someone else's signed payment.

The victim signs a valid payment and presents it with a `payerProof`. A front-runner who copied the transaction out of
the mempool has everything EXCEPT the victim's key, so whatever header they build must be refused with 402
`invalid_payment_header` by the real `KaspaEngine` (pure `verify`, plus `resolve_inputs` for the address-swap variant).
"""

from __future__ import annotations

import asyncio
import base64
import json

import kaspa
import pytest
from app.kaspa_engine import KaspaEngine, PaymentVerificationError
from app.schemas import PaymentSignature as GatewayPaymentSignature

from conftest import address_for_key
from test_offline_engine import (
    ATTACKER,
    ATTACKER_KEY,
    NONCE,
    OFFLINE_PAYER_KEY,
    OTHER_NONCE,
    FakeNodeEngine,
    exact,
    forge_tx,
    make_required,
    pp,
    utxo,
    verdict,
)
from txforge import make_payer_proof


@pytest.fixture(scope="module")
def engine() -> KaspaEngine:
    from app.config import get_settings

    return KaspaEngine(get_settings())


@pytest.fixture(scope="module")
def required():
    return make_required()


@pytest.fixture
def victim(required):
    return forge_tx(exact(required))


# ---------------------------------------------------------------- control


def test_the_victims_own_proof_is_accepted(engine, required, victim) -> None:
    assert verdict(engine, required, victim) == "accepted"


def test_base64_encodings_of_the_victims_proof_are_accepted_too(engine, required, victim) -> None:
    proof = bytes.fromhex(pp(victim.tx_id, NONCE))
    for form in (base64.b64encode(proof).decode(), base64.b64encode(b"\x00" + proof).decode()):
        assert verdict(engine, required, victim, payerProof=form) == "accepted"


# ---------------------------------------------------------------- the front-runner reuses the victim's signed tx


def test_front_runner_proves_with_their_own_key_for_the_victims_address(engine, required, victim) -> None:
    proof = make_payer_proof(ATTACKER_KEY, NONCE, victim.tx_id)
    assert verdict(engine, required, victim, payerProof=proof) == "invalid_payment_header"


def test_front_runner_claims_the_tx_as_their_own_address_with_their_own_proof(required, victim) -> None:
    """The attacker's proof is valid for THEIR address, but none of the tx's inputs is owned by that address."""
    funds = [utxo(0)]
    forged = forge_tx(exact(required), utxos=funds)
    signature = GatewayPaymentSignature(
        txId=forged.tx_id,
        rawTx=forged.raw_tx,
        payerAddress=ATTACKER,
        nonce=NONCE,
        payerProof=make_payer_proof(ATTACKER_KEY, NONCE, forged.tx_id),
    )
    engine = FakeNodeEngine(funds)

    async def attempt() -> None:
        _, tx = engine.verify(signature, required)
        await engine.resolve_inputs(signature, tx)

    with pytest.raises(PaymentVerificationError) as caught:
        asyncio.run(attempt())
    assert caught.value.code == "invalid_payment_header"
    assert "does not own any input" in str(caught.value)


def test_front_runner_cannot_swap_outputs_to_pay_themselves_and_keep_the_victims_proof(engine, required, victim) -> None:
    """Changing the tx changes its id; the victim's proof covers the old id."""
    other = forge_tx(exact(required), nonce=NONCE, utxos=[utxo(5)])
    assert other.tx_id != victim.tx_id
    stolen_proof = pp(victim.tx_id, NONCE)
    assert verdict(engine, required, other, payerProof=stolen_proof) == "invalid_payment_header"


def test_front_runner_cannot_rebind_the_victims_tx_to_their_own_challenge_nonce(engine, required, victim) -> None:
    """They fetch their own nonce, copy the victim's tx, and sign a proof for (their nonce, victim txId)."""
    proof = make_payer_proof(ATTACKER_KEY, OTHER_NONCE, victim.tx_id)
    assert verdict(engine, required, victim, nonce=OTHER_NONCE, payerProof=proof) == "invalid_payment_header"
    assert verdict(engine, required, victim, nonce=OTHER_NONCE, payerProof=pp(victim.tx_id, NONCE)) == "invalid_payment_header"


# ---------------------------------------------------------------- malformed or mismatched proofs


def test_missing_payer_proof_is_refused_by_the_schema(required, victim) -> None:
    with pytest.raises(ValueError):
        GatewayPaymentSignature(
            txId=victim.tx_id, rawTx=victim.raw_tx, payerAddress=victim.payer_address, nonce=NONCE
        )
    body = json.loads(victim.signature().model_dump_json())
    body.pop("payerProof")
    header = base64.b64encode(json.dumps(body).encode()).decode()
    with pytest.raises(ValueError):
        GatewayPaymentSignature.from_header(header)


def test_proof_for_the_wrong_nonce_is_refused(engine, required, victim) -> None:
    assert verdict(engine, required, victim, payerProof=pp(victim.tx_id, OTHER_NONCE)) == "invalid_payment_header"


def test_proof_for_the_wrong_tx_id_is_refused(engine, required, victim) -> None:
    for other_id in ("00" * 32, "ab" * 32, forge_tx(exact(required), utxos=[utxo(9)]).tx_id):
        assert verdict(engine, required, victim, payerProof=pp(other_id, NONCE)) == "invalid_payment_header"


def test_proof_over_a_different_message_shape_is_refused(engine, required, victim) -> None:
    key = kaspa.PrivateKey(OFFLINE_PAYER_KEY)
    for message in (
        f"kasgate:v1:{NONCE}",
        f"{NONCE}:{victim.tx_id}",
        f"kasgate:v1:{victim.tx_id}:{NONCE}",
        f"kasgate:v1:{NONCE}:{victim.tx_id.upper()}",
        f"kasgate:v1:{NONCE}:{victim.tx_id} ",
        "",
    ):
        proof = kaspa.sign_message(message, key)
        assert verdict(engine, required, victim, payerProof=proof) == "invalid_payment_header", message


def test_proof_with_a_flipped_bit_is_refused(engine, required, victim) -> None:
    proof = pp(victim.tx_id, NONCE)
    flipped = proof[:-1] + ("0" if proof[-1] != "0" else "1")
    assert verdict(engine, required, victim, payerProof=flipped) == "invalid_payment_header"
    assert verdict(engine, required, victim, payerProof="00" * 64) == "invalid_payment_header"


@pytest.mark.parametrize("bad", ["", "ab" * 63, "ab" * 65, "zz" * 64, "not a signature", " " * 128])
def test_malformed_proofs_never_reach_the_engine(bad: str, victim) -> None:
    with pytest.raises(ValueError):
        GatewayPaymentSignature(
            txId=victim.tx_id, rawTx=victim.raw_tx, payerAddress=victim.payer_address, nonce=NONCE, payerProof=bad
        )


def test_a_proof_signed_for_one_payment_cannot_be_reused_on_a_second_payment(engine, required, victim) -> None:
    second = forge_tx(exact(required), utxos=[utxo(1)])
    assert second.tx_id != victim.tx_id
    assert verdict(engine, required, second, payerProof=pp(victim.tx_id, NONCE)) == "invalid_payment_header"


# ---------------------------------------------------------------- the payer address must be a P2PK Schnorr address


def test_ecdsa_payer_address_is_refused(engine, required, victim) -> None:
    key = kaspa.PrivateKey(OFFLINE_PAYER_KEY)
    ecdsa = key.to_public_key().to_address_ecdsa("testnet").to_string()
    proof = make_payer_proof(OFFLINE_PAYER_KEY, NONCE, victim.tx_id)
    assert verdict(engine, required, victim, payerAddress=ecdsa, payerProof=proof) == "invalid_payment_header"


def test_script_hash_payer_address_is_refused(engine, required, victim) -> None:
    script = kaspa.pay_to_script_hash_script("51")
    p2sh = kaspa.address_from_script_public_key(script, "testnet").to_string()
    proof = make_payer_proof(OFFLINE_PAYER_KEY, NONCE, victim.tx_id)
    assert verdict(engine, required, victim, payerAddress=p2sh, payerProof=proof) == "invalid_payment_header"


def test_proof_by_the_right_key_for_a_different_schnorr_address_is_refused(engine, required, victim) -> None:
    assert verdict(engine, required, victim, payerAddress=address_for_key(ATTACKER_KEY)) == "invalid_payment_header"


# ---------------------------------------------------------------- tampering after the proof


def test_replacing_the_tx_id_after_proving_invalidates_the_payment(engine, required, victim) -> None:
    """The proof covers the txId, so presenting the same body under another id (or a mutated body) fails."""
    assert verdict(engine, required, victim, txId="cd" * 32) == "invalid_payment_header"
