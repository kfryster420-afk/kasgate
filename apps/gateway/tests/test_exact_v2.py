"""kaspa-exact-v2 standard-native pure verifier (app.exact_v2): the happy path and every rejection rule."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from kaspa import NetworkId, PrivateKey

from app.exact_v2 import ExactV2Verification, verify_exact_v2
from app.payments import PaymentVerificationError
from app.x402_v2 import ExactV2PaymentPayload, ExactV2Requirements
from tests.conftest import MERCHANT_ADDRESS, PAYER_ADDRESS, PRICE_SOMPI, TREASURY
from tests.exact_v2_support import ExactPaymentSpec, Funding, build_payment, request_hash_for, script_of

URL = "http://gateway.test/p/weather"
PAYER = PrivateKey("03" * 32)
OTHER = PrivateKey("05" * 32)


def requirement(amount: int = PRICE_SOMPI, pay_to: str = MERCHANT_ADDRESS) -> ExactV2Requirements:
    return ExactV2Requirements.for_merchant(
        network="kaspa:testnet-10", amount_sompi=amount, pay_to=pay_to, max_timeout_seconds=120
    )


def spec(**overrides: object) -> ExactPaymentSpec:
    base = ExactPaymentSpec(requirement=requirement(), url=URL)
    for name, value in overrides.items():
        setattr(base, name, value)
    return base


def run(
    s: ExactPaymentSpec, *, offered: ExactV2Requirements | None = None, request_hash: str | None = None
) -> ExactV2Verification:
    built = build_payment(s)
    return verify_exact_v2(
        ExactV2PaymentPayload.model_validate(built.payload),
        offered or s.requirement,
        request_hash=request_hash or request_hash_for(s),
        network_id=NetworkId("testnet-10"),
        network_type="testnet",
        now=datetime.now(UTC),
    )


def rejects(s: ExactPaymentSpec, code: str, fragment: str, **kwargs: object) -> None:
    with pytest.raises(PaymentVerificationError) as info:
        run(s, **kwargs)  # type: ignore[arg-type]
    assert info.value.code == code, info.value.message
    assert fragment in info.value.message


def test_a_valid_standard_native_payment_verifies_with_zero_platform_fee() -> None:
    s = spec()
    built = build_payment(s)
    result = run(s)
    assert result.payment.tx_id == built.tx_id
    assert result.payment.payer_address == PAYER_ADDRESS
    assert result.payment.total_sompi == result.payment.merchant_sompi == PRICE_SOMPI
    assert result.payment.fee_sompi == 0
    assert result.transaction.id == built.tx_id
    assert result.payment_identifier == s.payment_identifier
    assert result.lookup_addresses == (PAYER_ADDRESS,)


def test_payment_without_change_output_verifies() -> None:
    s = spec(fundings=[Funding(amount=PRICE_SOMPI + 10_000, key=PAYER)])
    assert len(build_payment(s).artifact["outputs"]) == 1
    run(s)


def test_multiple_inputs_from_two_payers_and_authorization_by_the_second() -> None:
    s = spec(
        fundings=[Funding(amount=PRICE_SOMPI, key=PAYER, index=0), Funding(amount=300_000_000, key=OTHER, index=1)],
        authorization_input=1,
    )
    result = run(s)
    assert result.payment.payer_address == OTHER.to_address("testnet").to_string()
    assert len(result.lookup_addresses) == 2


@pytest.mark.parametrize(
    ("mutate", "code", "fragment"),
    [
        # KasGate v1 shape on the v2 path: a treasury output is neither the payment nor payer change
        (
            lambda s: setattr(
                s,
                "outputs",
                [(s.requirement.extra.payToScriptPublicKey, PRICE_SOMPI), (script_of_address(TREASURY), 30_000_000)],
            ),
            "invalid_exact_payment",
            "neither the payment nor change",
        ),
        (
            lambda s: setattr(
                s,
                "outputs",
                [
                    (s.requirement.extra.payToScriptPublicKey, PRICE_SOMPI),
                    (script_of(PAYER), 100_000_000),
                    (script_of(PAYER), 100_000_000),
                ],
            ),
            "invalid_exact_payment",
            "at most one change output",
        ),
        (lambda s: setattr(s, "payload", "6b61736761746531"), "invalid_exact_payment", "empty transaction payload"),
        (
            lambda s: setattr(s, "outputs", [(s.requirement.extra.payToScriptPublicKey, PRICE_SOMPI - 1)]),
            "underpaid",
            "1 sompi short",
        ),
        (
            lambda s: setattr(s, "outputs", [(s.requirement.extra.payToScriptPublicKey, PRICE_SOMPI + 1)]),
            "invalid_exact_payment",
            "must be exactly",
        ),
        (
            lambda s: setattr(s, "outputs", [(script_of(OTHER), PRICE_SOMPI)]),
            "invalid_exact_payment",
            "does not pay payTo",
        ),
        (
            lambda s: setattr(
                s,
                "outputs",
                [
                    (s.requirement.extra.payToScriptPublicKey, PRICE_SOMPI),
                    (s.requirement.extra.payToScriptPublicKey, 100_000_000),
                ],
            ),
            "invalid_exact_payment",
            "exactly one output",
        ),
        (lambda s: setattr(s, "payment_output_index", 1), "invalid_exact_payment", "does not pay payTo"),
        (lambda s: setattr(s, "tamper_input_signature", True), "invalid_exact_payment", "signature is invalid"),
        (lambda s: setattr(s, "sighash_byte", 0x81), "invalid_exact_payment", "SIGHASH_ALL"),
        (
            lambda s: setattr(s, "tamper_authorization_signature", True),
            "invalid_exact_payment",
            "authorization signature",
        ),
        (lambda s: setattr(s, "authorization_key", OTHER), "invalid_exact_payment", "authorization signature"),
        (lambda s: setattr(s, "digest_override", "ab" * 32), "invalid_exact_payment", "authorization digest"),
        (lambda s: setattr(s, "authorization_input", 3), "invalid_exact_payment", "inputIndex"),
        (lambda s: setattr(s, "storage_mass_delta", 1), "invalid_exact_payment", "storageMass"),
        (lambda s: setattr(s, "artifact_overrides", {"id": "cd" * 32}), "invalid_exact_payment", "transaction id"),
        (lambda s: setattr(s, "artifact_overrides", {"lockTime": "1"}), "invalid_exact_payment", "lockTime"),
        (lambda s: setattr(s, "artifact_overrides", {"version": 1}), "invalid_exact_payment", "version 0"),
        (lambda s: setattr(s, "fee", 200_000_000), "tx_rejected", "safety ceiling"),
        (lambda s: setattr(s, "expires_in", timedelta(seconds=-1)), "invalid_exact_payment", "expired_authorization"),
        (
            lambda s: setattr(s, "expires_in", timedelta(seconds=600)),
            "invalid_exact_payment",
            "authorization_exceeds_max_timeout",
        ),
        (lambda s: setattr(s, "payer_address", MERCHANT_ADDRESS), "invalid_exact_payment", "payerAddress"),
        (lambda s: setattr(s, "request_hash", "ef" * 32), "invalid_exact_payment", "requestHash"),
    ],
)
def test_rejections(mutate: Callable[[ExactPaymentSpec], None], code: str, fragment: str) -> None:
    s = spec()
    mutate(s)
    rejects(s, code, fragment)


def script_of_address(address: str) -> str:
    from app.kaspa_types import serialized_script_public_key

    return serialized_script_public_key(address)


def test_accepted_must_be_the_offer_for_this_request() -> None:
    s = spec(accepted_override=requirement(amount=PRICE_SOMPI - 1).wire())
    rejects(s, "invalid_exact_payment", "invalid_kaspa_x402_accepted")


def test_wrong_payto_in_the_offer_is_rejected() -> None:
    other_merchant = requirement(pay_to=PAYER_ADDRESS)
    rejects(spec(), "invalid_exact_payment", "invalid_kaspa_x402_accepted", offered=other_merchant)


def test_request_fingerprint_binds_method_url_and_body() -> None:
    s = spec(method="POST", body='{"q":"weather"}')
    run(s)
    other = request_hash_for(spec(method="POST", body='{"q":"other"}'))
    rejects(s, "invalid_exact_payment", "requestHash", request_hash=other)


def test_missing_payment_identifier_is_rejected() -> None:
    s = spec()
    built = build_payment(s)
    payload = dict(built.payload)
    payload.pop("extensions")
    with pytest.raises(PaymentVerificationError) as info:
        verify_exact_v2(
            ExactV2PaymentPayload.model_validate(payload),
            s.requirement,
            request_hash=request_hash_for(s),
            network_id=NetworkId("testnet-10"),
            network_type="testnet",
            now=datetime.now(UTC),
        )
    assert "missing_kaspa_payment_identifier" in info.value.message


def test_unknown_artifact_keys_are_ignored_like_the_official_parser_but_known_keys_are_strict() -> None:
    s = spec()
    built = build_payment(s)
    artifact = dict(built.artifact, futureField={"x": 1})
    payload = json.loads(json.dumps(built.payload))
    payload["payload"]["transaction"] = json.dumps(artifact)
    verify_exact_v2(
        ExactV2PaymentPayload.model_validate(payload),
        s.requirement,
        request_hash=request_hash_for(s),
        network_id=NetworkId("testnet-10"),
        network_type="testnet",
        now=datetime.now(UTC),
    )
    artifact["outputs"] = [dict(artifact["outputs"][0], value=int(artifact["outputs"][0]["value"]))]
    payload["payload"]["transaction"] = json.dumps(artifact)
    with pytest.raises(PaymentVerificationError) as info:
        verify_exact_v2(
            ExactV2PaymentPayload.model_validate(payload),
            s.requirement,
            request_hash=request_hash_for(s),
            network_id=NetworkId("testnet-10"),
            network_type="testnet",
            now=datetime.now(UTC),
        )
    assert "outputs.0.value" in info.value.message


def test_the_payload_model_rejects_a_client_supplied_transaction_id_and_other_profiles() -> None:
    built = build_payment(spec())
    with_id = json.loads(json.dumps(built.payload))
    with_id["payload"]["transactionId"] = built.tx_id
    with pytest.raises(ValueError, match="transactionId"):
        ExactV2PaymentPayload.model_validate(with_id)
    additive = json.loads(json.dumps(built.payload))
    additive["payload"]["profile"] = "additive"
    with pytest.raises(ValueError, match="profile"):
        ExactV2PaymentPayload.model_validate(additive)


def test_below_minimum_amount_is_never_offered() -> None:
    with pytest.raises(ValueError, match="minimum merchant output"):
        requirement(amount=19_999_999)
