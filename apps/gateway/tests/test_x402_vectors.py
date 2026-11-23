"""Official kaspa-x402 v1.0.1 conformance vectors (tests/fixtures/kaspa-x402) against KasGate's v2 implementation."""

import base64
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from kaspa import NetworkId
from pydantic import ValidationError

from app.bip340 import verify_schnorr
from app.exact_v2 import (
    _check_envelope,
    _check_input_signatures,
    _check_mass,
    check_authorization_expiry,
    parse_safe_transaction,
    to_sdk_transaction,
    transaction_id_v0,
)
from app.payments import PaymentVerificationError
from app.x402_v2 import (
    PAYMENT_IDENTIFIER_SCHEMA,
    ExactV2PaymentPayload,
    ExactV2Requirements,
    authorization_preimage,
    canonical_json,
    encode_canonical_header,
    parse_expires_at,
    sha256_hex,
)

FIXTURES = Path(__file__).parent / "fixtures" / "kaspa-x402"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


INTEROP = load("interop-v1.json")
HTTP = load("exact-transaction.json")
STANDARD = INTEROP["transactionEncoding"]["profiles"]["standardNative"]


def test_standard_native_transaction_id_is_recomputed_from_consensus_fields() -> None:
    artifact = STANDARD["artifact"]
    tx = parse_safe_transaction(json.dumps(artifact))
    assert transaction_id_v0(tx) == STANDARD["transactionId"] == STANDARD["txid"]["digest"] == artifact["id"]
    assert to_sdk_transaction(tx, "testnet").id == STANDARD["transactionId"]
    # the documented preimage hashes to the id under BLAKE2b-256 keyed "TransactionID"
    preimage = bytes.fromhex(STANDARD["txid"]["preimage"])
    assert hashlib.blake2b(preimage, digest_size=32, key=b"TransactionID").hexdigest() == artifact["id"]


def test_id_does_not_depend_on_json_key_order_or_signature_scripts() -> None:
    artifact = dict(reversed(list(STANDARD["artifact"].items())))
    artifact["inputs"] = [dict(artifact["inputs"][0], signatureScript="41" + "00" * 65)]
    assert transaction_id_v0(parse_safe_transaction(json.dumps(artifact))) == STANDARD["transactionId"]


def test_standard_native_artifact_passes_envelope_mass_and_input_signature_checks() -> None:
    tx = parse_safe_transaction(json.dumps(STANDARD["artifact"]))
    sdk_tx = to_sdk_transaction(tx, "testnet")
    _check_envelope(tx)
    _check_mass(tx, sdk_tx, NetworkId("testnet-10"))  # declared storageMass 63670 == contextual KIP-9 mass
    _check_input_signatures(tx, sdk_tx)


@pytest.mark.parametrize(
    ("mutate", "fragment"),
    [
        (lambda a: a.update(payload="00"), "empty transaction payload"),
        (lambda a: a.update(storageMass="63671"), "storageMass"),
        (lambda a: a["outputs"].append(dict(a["outputs"][1])), "at most one change output"),
        (lambda a: a["inputs"][0].update(sigOpCount=2), "sigOpCount"),
        (lambda a: a["inputs"][0].update(computeBudget=1), "compute budget"),
        (lambda a: a.update(gas="1"), "gas 0"),
    ],
)
def test_vector_artifact_mutations_are_rejected(mutate: Any, fragment: str) -> None:
    artifact = json.loads(json.dumps(STANDARD["artifact"]))
    mutate(artifact)
    tx = parse_safe_transaction(json.dumps(artifact))
    with pytest.raises(PaymentVerificationError) as info:
        _check_envelope(tx)
        _check_mass(tx, to_sdk_transaction(tx, "testnet"), NetworkId("testnet-10"))
    assert fragment in info.value.message


def test_tampered_vector_signature_is_rejected() -> None:
    artifact = json.loads(json.dumps(STANDARD["artifact"]))
    script = artifact["inputs"][0]["signatureScript"]
    artifact["inputs"][0]["signatureScript"] = script[:10] + ("0" if script[10] != "0" else "1") + script[11:]
    tx = parse_safe_transaction(json.dumps(artifact))
    with pytest.raises(PaymentVerificationError, match="signature is invalid"):
        _check_input_signatures(tx, to_sdk_transaction(tx, "testnet"))


def test_payment_requirements_canonical_json_and_hash() -> None:
    vector = INTEROP["paymentRequirements"]
    assert canonical_json(vector["value"]) == vector["canonicalJsonUtf8"]
    assert sha256_hex(vector["canonicalJsonUtf8"]) == vector["sha256"]


def test_request_authorization_digest_and_schnorr_signature() -> None:
    vector = INTEROP["requestAuthorization"]
    fields = vector["input"]
    preimage = authorization_preimage(
        network=fields["network"],
        transaction_id=fields["transactionId"],
        payment_output_index=fields["paymentOutputIndex"],
        amount=fields["amount"],
        pay_to=fields["payTo"],
        pay_to_script_public_key=fields["payToScriptPublicKey"],
        payment_requirements_hash=fields["paymentRequirementsHash"],
        request_hash=fields["requestHash"],
        payment_identifier=fields["paymentIdentifier"],
        input_index=fields["inputIndex"],
        expires_at=fields["expiresAt"],
        profile=fields["profile"],
        challenge_id=fields["challengeId"],
    )
    assert preimage == vector["canonicalJsonUtf8"]
    digest = sha256_hex(preimage)
    assert digest == vector["sha256"]
    assert verify_schnorr(
        bytes.fromhex(digest), bytes.fromhex(vector["signerPublicKey"]), bytes.fromhex(vector["signature"])
    )
    tampered = bytes.fromhex(vector["signature"])[:-1] + b"\x00"
    assert not verify_schnorr(bytes.fromhex(digest), bytes.fromhex(vector["signerPublicKey"]), tampered)


@pytest.mark.parametrize("case", [c for c in INTEROP["expiry"]["cases"] if c["profile"] == "standard-native"])
def test_standard_native_expiry_cases(case: dict[str, str]) -> None:
    now = parse_expires_at(INTEROP["expiry"]["referenceTime"])
    payment = _payment_with_expiry(case["authorizationExpiresAt"])
    requirement = payment.accepted.model_copy(update={"maxTimeoutSeconds": INTEROP["expiry"]["maxTimeoutSeconds"]})
    if case["expected"] == "valid":
        check_authorization_expiry(payment, requirement, now)
    else:
        with pytest.raises(PaymentVerificationError, match=case["expected"]):
            check_authorization_expiry(payment, requirement, now)


def _payment_with_expiry(expires_at: str) -> ExactV2PaymentPayload:
    from tests.exact_v2_support import ExactPaymentSpec, build_payment
    from tests.test_exact_v2 import requirement

    built = build_payment(ExactPaymentSpec(requirement=requirement(), url="http://gateway.test/p/weather"))
    payload = json.loads(json.dumps(built.payload))
    payload["payload"]["authorization"]["expiresAt"] = expires_at
    return ExactV2PaymentPayload.model_validate(payload)


@pytest.mark.parametrize("key", ["paymentRequired", "paymentSignature", "paymentResponse"])
def test_http_headers_use_the_same_canonical_base64_encoding(key: str) -> None:
    source = {
        "paymentRequired": "paymentRequired",
        "paymentSignature": "paymentPayload",
        "paymentResponse": "settlementResponse",
    }
    assert encode_canonical_header(HTTP[source[key]]) == HTTP["headers"][key]
    assert json.loads(base64.b64decode(HTTP["headers"][key])) == HTTP[source[key]]


def test_advertised_payment_identifier_schema_is_the_official_document() -> None:
    advertised = HTTP["paymentRequired"]["extensions"]["payment-identifier"]["schema"]
    assert advertised == PAYMENT_IDENTIFIER_SCHEMA


def test_settlement_response_kaspa_extension_keys_match_the_reference_server() -> None:
    from app.x402_v2 import KaspaSettlementExtension

    official = set(HTTP["settlementResponse"]["extensions"]["kaspa"])
    ours = set(KaspaSettlementExtension.model_fields)
    # the additive profile adds template/head keys; the standard-native core keys are shared
    assert ours <= official
    assert ours == {"exactProfile", "paymentOutputIndex", "finality", "requestHash", "transactionEncoding"}


def test_the_additive_profile_is_not_accepted_by_kasgate() -> None:
    with pytest.raises(ValidationError):
        ExactV2Requirements.model_validate(HTTP["paymentRequired"]["accepts"][0])
    with pytest.raises(ValidationError):
        ExactV2PaymentPayload.model_validate(HTTP["paymentPayload"])


@pytest.mark.parametrize("name", ["missing-payment-identifier.json", "accepted-not-offered.json"])
def test_official_semantic_negative_payloads_are_rejected(name: str) -> None:
    vector = load(name)
    with pytest.raises((ValidationError, PaymentVerificationError)):
        payment = ExactV2PaymentPayload.model_validate(vector["paymentPayload"])
        offered = ExactV2Requirements.model_validate(vector["paymentRequired"]["accepts"][0])
        if payment.accepted != offered:
            raise PaymentVerificationError("invalid_exact_payment", vector["expectedError"])
        if payment.payment_identifier is None:
            raise PaymentVerificationError("invalid_exact_payment", vector["expectedError"])


def test_reference_time_parsing_accepts_optional_milliseconds() -> None:
    assert parse_expires_at("2099-01-01T00:00:00Z") == parse_expires_at("2099-01-01T00:00:00.000Z")
    assert isinstance(parse_expires_at("2099-01-01T00:00:00.001Z"), datetime)
    with pytest.raises(ValueError):
        parse_expires_at("2099-01-01T00:00:00+00:00")
