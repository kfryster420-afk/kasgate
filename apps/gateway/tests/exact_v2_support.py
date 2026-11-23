"""Builders for signed kaspa-exact-v2 standard-native payments, independent of the gateway verifier.

Transactions are produced in the kaspa-sdk-safe-json-v2.0.0 projection the official client sends; input signatures use
the Kaspa SDK's sighash + `sign_script_hash`, the authorization uses the official digest construction.
"""

import base64
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from kaspa import (
    NetworkId,
    PrivateKey,
    calculate_storage_mass,
    compute_sighash,
    pay_to_address_script,
    sign_script_hash,
)

from app.exact_v2 import SafeTransaction, to_sdk_transaction, transaction_id_v0
from app.x402_v2 import (
    ExactV2Requirements,
    PaymentExtensions,
    authorization_digest,
    canonical_json,
    http_request_hash,
    payment_requirements_hash,
)

NETWORK_ID = NetworkId("testnet-10")
FUNDING_TX = "70" * 32


def script_of(key: PrivateKey) -> str:
    script = pay_to_address_script(key.to_address("testnet"))
    return f"{int(script.version):04x}{str(script.script).lower()}"


def address_of(key: PrivateKey) -> str:
    return key.to_address("testnet").to_string()


def iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


@dataclass
class Funding:
    amount: int
    key: PrivateKey
    tx_id: str = FUNDING_TX
    index: int = 0


@dataclass
class ExactPaymentSpec:
    """Everything that shapes one payment; tests mutate a field to produce a specific rejection."""

    requirement: ExactV2Requirements
    url: str
    method: str = "GET"
    body: str | None = None
    fundings: list[Funding] = field(default_factory=list)
    outputs: list[tuple[str, int]] | None = None  # (serialized script, value); default merchant + change
    fee: int = 10_000
    payload: str = ""
    payment_identifier: str = "pay_0123456789abcdef0123"
    authorization_key: PrivateKey | None = None
    authorization_input: int = 0
    payment_output_index: int = 0
    expires_in: timedelta = timedelta(seconds=60)
    expires_at: str | None = None  # authorization expiresAt chosen by an official client; else now + expires_in
    sighash_byte: int = 0x01
    storage_mass_delta: int = 0
    request_hash: str | None = None
    payer_address: str | None = None
    artifact_overrides: dict[str, Any] = field(default_factory=dict)
    accepted_override: dict[str, Any] | None = None
    tamper_input_signature: bool = False
    tamper_authorization_signature: bool = False
    digest_override: str | None = None


@dataclass(frozen=True)
class BuiltPayment:
    payload: dict[str, Any]
    artifact: dict[str, Any]
    tx_id: str
    request_hash: str

    def header(self) -> str:
        return base64.b64encode(canonical_json(self.payload).encode()).decode()


def request_hash_for(spec: ExactPaymentSpec) -> str:
    return http_request_hash(spec.method, spec.url, spec.body, payment_requirements_hash(spec.requirement))


def build_payment(spec: ExactPaymentSpec) -> BuiltPayment:
    requirement = spec.requirement
    amount = int(requirement.amount)
    fundings = spec.fundings or [Funding(amount=amount + 500_000_000, key=PrivateKey("03" * 32))]
    total_in = sum(f.amount for f in fundings)
    outputs = spec.outputs
    if outputs is None:
        outputs = [(requirement.extra.payToScriptPublicKey, amount)]
        change = total_in - amount - spec.fee
        if change > 0:
            outputs.append((script_of(fundings[0].key), change))
    artifact: dict[str, Any] = {
        "id": "",
        "version": 0,
        "inputs": [
            {
                "previousOutpoint": {"transactionId": f.tx_id, "index": f.index},
                "sequence": "18446744073709551615",
                "sigOpCount": 1,
                "signatureScript": "",
                "utxo": {"amount": str(f.amount), "scriptPublicKey": script_of(f.key)},
            }
            for f in fundings
        ],
        "outputs": [{"value": str(value), "scriptPublicKey": spk, "covenant": None} for spk, value in outputs],
        "lockTime": "0",
        "subnetworkId": "0" * 40,
        "gas": "0",
        "payload": spec.payload,
        "storageMass": "0",
    }
    mass = calculate_storage_mass(NETWORK_ID, [f.amount for f in fundings], [value for _, value in outputs])
    artifact["storageMass"] = str((mass or 0) + spec.storage_mass_delta)
    artifact.update({k: v for k, v in spec.artifact_overrides.items() if k != "id"})
    unsigned = SafeTransaction.model_validate({**artifact, "id": "0" * 64})
    tx_id = transaction_id_v0(unsigned)
    artifact["id"] = spec.artifact_overrides.get("id", tx_id)
    sdk_tx = to_sdk_transaction(SafeTransaction.model_validate({**artifact, "id": tx_id}), "testnet")
    for index, funding in enumerate(fundings):
        digest = compute_sighash(sdk_tx, index).to_hex()
        signature = sign_script_hash(digest, funding.key)[2:130]
        if spec.tamper_input_signature:
            signature = ("0" if signature[0] != "0" else "1") + signature[1:]
        artifact["inputs"][index]["signatureScript"] = f"41{signature}{spec.sighash_byte:02x}"

    request_hash = spec.request_hash or request_hash_for(spec)
    expires_at = spec.expires_at or iso_z(datetime.now(UTC) + spec.expires_in)
    digest = spec.digest_override or authorization_digest(
        network=requirement.network,
        transaction_id=tx_id,
        payment_output_index=spec.payment_output_index,
        amount=requirement.amount,
        pay_to=requirement.payTo,
        pay_to_script_public_key=requirement.extra.payToScriptPublicKey,
        payment_requirements_hash=payment_requirements_hash(requirement),
        request_hash=request_hash,
        payment_identifier=spec.payment_identifier,
        input_index=spec.authorization_input,
        expires_at=expires_at,
    )
    auth_key = spec.authorization_key or fundings[min(spec.authorization_input, len(fundings) - 1)].key
    auth_signature = sign_script_hash(digest, auth_key)[2:130]
    if spec.tamper_authorization_signature:
        auth_signature = ("0" if auth_signature[0] != "0" else "1") + auth_signature[1:]
    payload: dict[str, Any] = {
        "x402Version": 2,
        "accepted": spec.accepted_override if spec.accepted_override is not None else requirement.wire(),
        "payload": {
            "type": "exact-transaction",
            "profile": "standard-native",
            "transaction": json.dumps(artifact, separators=(",", ":")),
            "transactionEncoding": "kaspa-sdk-safe-json-v2.0.0",
            "paymentOutputIndex": spec.payment_output_index,
            "requestHash": request_hash,
            "authorization": {
                "version": "kaspa-x402-exact-request-authorization-v2",
                "inputIndex": spec.authorization_input,
                "expiresAt": expires_at,
                "digest": digest,
                "signature": auth_signature,
            },
        },
        "extensions": PaymentExtensions.required_identifier(spec.payment_identifier).wire(),
    }
    if spec.payer_address is not None:
        payload["payload"]["payerAddress"] = spec.payer_address
    return BuiltPayment(payload=payload, artifact=artifact, tx_id=tx_id, request_hash=request_hash)
