"""Pure, network-free verifier for kaspa-x402 `exact` / `kaspa-exact-v2` standard-native payments (CONTRACT v2.10).

Mirrors the official verifier of elldeeone/kaspa-x402 v1.0.1 (`#verifyStandardNative` and
`assertStandardNativeTransactionEnvelope` in `protocol/packages/server`, `exact-authorization.ts` in core):

* transaction envelope: version 0, `lockTime` 0, native subnetwork, `gas` 0, EMPTY payload, 1..16 inputs, 1..2
  outputs, no input compute budget, `sigOpCount` 1, P2PK Schnorr funding inputs (script version 0), output script
  version 0, `covenant` null, a declared `storageMass` equal to the contextual KIP-9 storage mass;
* the transaction id is recomputed from the consensus fields (BLAKE2b-256 keyed `TransactionID`) and must equal the
  artifact `id`; the client never supplies an authoritative id;
* exactly one output pays `extra.payToScriptPublicKey`, it is `paymentOutputIndex` and its value equals `amount`
  (the ENTIRE merchant gain; KasGate takes no platform fee on this binding); the optional second output must return
  change to a script that funds the transaction (payer-controlled);
* inputs >= outputs and the miner fee is at most 1 KAS (`MAX_SAFE_TRANSACTION_FEE_SOMPI`);
* every input signature is a valid BIP-340 Schnorr signature over the input's SIGHASH_ALL digest;
* the request authorization digest is recomputed from the server's own requirements, request fingerprint and
  payment identifier, and is signed by the key of the P2PK input at `authorization.inputIndex`;
* `expiresAt` is in the future and no later than now + `maxTimeoutSeconds`.

KasGate policy on top of the binding (documented in CONTRACTS.md v2.7): signatures must use SIGHASH_ALL (`0x01`), the
same as the official client, so no third party can re-shape a signed payment; the transaction mass must be standard.

Node lookups (embedded UTXO entries must match the chain) happen afterwards in `KaspaEngine.resolve_exact_v2_inputs`.
"""

import hashlib
import json
import re
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Final

from kaspa import (
    NetworkId,
    ScriptPublicKey,
    Transaction,
    address_from_script_public_key,
    calculate_storage_mass,
    calculate_transaction_mass,
    compute_sighash,
    maximum_standard_transaction_mass,
)
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from app.bip340 import verify_schnorr
from app.payments import PaymentVerificationError, VerifiedPayment
from app.x402_v2 import (
    EXACT_V2_MAX_MINER_FEE_SOMPI,
    EXACT_V2_MIN_MERCHANT_OUTPUT_SOMPI,
    MAX_TRANSACTION_ARTIFACT_CHARS,
    UINT32_MAX,
    ExactV2PaymentPayload,
    ExactV2Requirements,
    authorization_digest,
    parse_expires_at,
    payment_requirements_hash,
)

INVALID: Final = "invalid_exact_payment"
MAX_INPUTS: Final = 16
MAX_OUTPUTS: Final = 2
MAX_PARSED_OUTPUTS: Final = 64
U64_MAX: Final = 2**64 - 1
NATIVE_SUBNETWORK_ID: Final = "0" * 40
SIGHASH_ALL: Final = 0x01
SCHNORR_SIGNATURE_PUSH: Final = 0x41
P2PK_SCHNORR_SPK: Final = re.compile(r"000020([0-9a-f]{64})ac")
TX_ID_KEY: Final = b"TransactionID"


def _u64(value: str) -> str:
    if int(value) > U64_MAX:
        raise ValueError("value exceeds u64")
    return value


U64Str = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]{0,19})$"), AfterValidator(_u64)]
Hex = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{2})*$", max_length=20_000)]
SerializedScript = Annotated[str, Field(pattern=r"^[0-9a-f]{4}(?:[0-9a-f]{2})+$", max_length=20_000)]
TxIdHex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SafeModel(BaseModel):
    """kaspa-sdk-safe-json-v2.0.0 projection. Unknown keys are ignored like the official parser; known keys are
    strictly typed (u64 values are decimal strings, small integers are JSON numbers, hex is lowercase)."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)


class SafeOutpoint(SafeModel):
    transactionId: TxIdHex
    index: Annotated[int, Field(ge=0, le=UINT32_MAX)]


class SafeUtxoEntry(SafeModel):
    amount: U64Str
    scriptPublicKey: SerializedScript


class SafeInput(SafeModel):
    previousOutpoint: SafeOutpoint
    sequence: U64Str
    sigOpCount: Annotated[int, Field(ge=0, le=255)]
    signatureScript: Hex
    utxo: SafeUtxoEntry
    computeBudget: Annotated[int, Field(ge=0, le=65_535)] | None = None


class SafeOutput(SafeModel):
    value: U64Str
    scriptPublicKey: SerializedScript
    covenant: None


class SafeTransaction(SafeModel):
    id: TxIdHex
    version: Annotated[int, Field(ge=0, le=65_535)]
    inputs: Annotated[list[SafeInput], Field(min_length=1, max_length=MAX_INPUTS)]
    outputs: Annotated[list[SafeOutput], Field(min_length=1, max_length=MAX_PARSED_OUTPUTS)]
    lockTime: U64Str
    subnetworkId: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    gas: U64Str
    payload: Hex
    storageMass: U64Str


@dataclass(frozen=True)
class ExactV2Verification:
    """Result of the pure checks; `transaction` is the finalised SDK transaction ready for submission."""

    payment: VerifiedPayment
    transaction: Transaction
    outpoints: tuple[tuple[str, int], ...]
    input_values: tuple[int, ...]
    input_scripts: tuple[str, ...]
    lookup_addresses: tuple[str, ...]
    payment_identifier: str
    request_hash: str
    payment_output_index: int


def _fail(message: str, code: str = INVALID) -> PaymentVerificationError:
    return PaymentVerificationError(code, message)


def parse_safe_transaction(text: str) -> SafeTransaction:
    if len(text) > MAX_TRANSACTION_ARTIFACT_CHARS:
        raise _fail("transaction artifact is too large")
    try:
        raw = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise _fail("transaction is not valid kaspa-sdk-safe-json-v2.0.0 JSON") from exc
    if not isinstance(raw, dict):
        raise _fail("transaction must be a JSON object")
    try:
        return SafeTransaction.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"])
        raise _fail(f"transaction field {location or '<root>'}: {first['msg']}") from exc


def _var_bytes(data: bytes) -> bytes:
    return struct.pack("<Q", len(data)) + data


def transaction_id_v0(tx: SafeTransaction) -> str:
    """Kaspa v0 transaction id: BLAKE2b-256 keyed "TransactionID" over the consensus fields, signature scripts
    excluded (rusty-kaspa `hashing::tx::id`, the `txid.preimage` of the official interop vector)."""
    out = bytearray(struct.pack("<H", tx.version))
    out += struct.pack("<Q", len(tx.inputs))
    for tx_input in tx.inputs:
        out += bytes.fromhex(tx_input.previousOutpoint.transactionId)
        out += struct.pack("<I", tx_input.previousOutpoint.index)
        out += _var_bytes(b"")
        out += struct.pack("<Q", int(tx_input.sequence))
    out += struct.pack("<Q", len(tx.outputs))
    for output in tx.outputs:
        out += struct.pack("<Q", int(output.value))
        out += struct.pack("<H", int(output.scriptPublicKey[:4], 16))
        out += _var_bytes(bytes.fromhex(output.scriptPublicKey[4:]))
    out += struct.pack("<Q", int(tx.lockTime))
    out += bytes.fromhex(tx.subnetworkId)
    out += struct.pack("<Q", int(tx.gas))
    out += _var_bytes(bytes.fromhex(tx.payload))
    return hashlib.blake2b(bytes(out), digest_size=32, key=TX_ID_KEY).hexdigest()


def _spk(serialized: str) -> dict[str, object]:
    return {"version": int(serialized[:4], 16), "script": serialized[4:]}


def _owner(serialized: str, network_type: str) -> str:
    try:
        spk = ScriptPublicKey(int(serialized[:4], 16), bytes.fromhex(serialized[4:]))  # type: ignore[arg-type]
        return address_from_script_public_key(spk, network_type).to_string().lower()
    except Exception as exc:
        raise _fail("an input is locked by a script with no standard address") from exc


def to_sdk_transaction(tx: SafeTransaction, network_type: str) -> Transaction:
    """Build the finalised `kaspa.Transaction` (with UTXO entries, needed for sighash and submission)."""
    data = {
        "id": tx.id,
        "version": tx.version,
        "inputs": [
            {
                "previousOutpoint": tx_input.previousOutpoint.model_dump(),
                "signatureScript": tx_input.signatureScript,
                "sequence": int(tx_input.sequence),
                "sigOpCount": tx_input.sigOpCount,
                "computeBudget": 0,
                "utxo": {
                    "address": _owner(tx_input.utxo.scriptPublicKey, network_type),
                    "outpoint": tx_input.previousOutpoint.model_dump(),
                    "amount": int(tx_input.utxo.amount),
                    "scriptPublicKey": _spk(tx_input.utxo.scriptPublicKey),
                    "blockDaaScore": 0,
                    "isCoinbase": False,
                    "covenantId": None,
                },
            }
            for tx_input in tx.inputs
        ],
        "outputs": [
            {"value": int(output.value), "scriptPublicKey": _spk(output.scriptPublicKey), "covenant": None}
            for output in tx.outputs
        ],
        "lockTime": int(tx.lockTime),
        "subnetworkId": tx.subnetworkId,
        "gas": int(tx.gas),
        "payload": tx.payload,
        "mass": 0,
        "storageMass": int(tx.storageMass),
    }
    try:
        sdk_tx = Transaction.from_dict(data)
        sdk_tx.finalize()
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise _fail(f"transaction is not a valid Kaspa transaction: {exc}") from exc
    return sdk_tx


def _signer_key(serialized_script: str) -> bytes:
    """x-only public key of a P2PK Schnorr script (`0000` + `20 <32-byte key> ac`)."""
    match = P2PK_SCHNORR_SPK.fullmatch(serialized_script)
    if match is None:
        raise _fail("input is not a P2PK Schnorr output (script version 0)")
    return bytes.fromhex(match.group(1))


def _check_envelope(tx: SafeTransaction) -> None:
    if tx.version != 0:
        raise _fail("standard-native requires transaction version 0")
    if tx.lockTime != "0" or tx.gas != "0":
        raise _fail("standard-native requires lockTime 0 and gas 0")
    if tx.subnetworkId != NATIVE_SUBNETWORK_ID:
        raise _fail("standard-native requires the native subnetwork")
    if tx.payload != "":
        raise _fail("standard-native requires an empty transaction payload")
    if len(tx.outputs) > MAX_OUTPUTS:
        raise _fail("standard-native allows one payment output and at most one change output")
    outpoints = set()
    for index, tx_input in enumerate(tx.inputs):
        if tx_input.computeBudget not in (None, 0):
            raise _fail(f"input {index} declares a compute budget")
        if tx_input.sigOpCount != 1:
            raise _fail(f"input {index} must have sigOpCount 1")
        if P2PK_SCHNORR_SPK.fullmatch(tx_input.utxo.scriptPublicKey) is None:
            raise _fail(f"input {index} is not a P2PK Schnorr output (script version 0)")
        key = (tx_input.previousOutpoint.transactionId, tx_input.previousOutpoint.index)
        if key in outpoints:
            raise _fail("transaction spends the same outpoint twice")
        outpoints.add(key)
    for index, output in enumerate(tx.outputs):
        if not output.scriptPublicKey.startswith("0000"):
            raise _fail(f"output {index} must use script version 0")


def _check_outputs(tx: SafeTransaction, requirement: ExactV2Requirements, payment_output_index: int) -> int:
    pay_script = requirement.extra.payToScriptPublicKey
    amount = int(requirement.amount)
    if payment_output_index >= len(tx.outputs):
        raise _fail("paymentOutputIndex is outside the transaction outputs")
    payment_output = tx.outputs[payment_output_index]
    if payment_output.scriptPublicKey != pay_script:
        raise _fail("the payment output does not pay payTo")
    if sum(1 for output in tx.outputs if output.scriptPublicKey == pay_script) != 1:
        raise _fail("exactly one output may pay payTo")
    value = int(payment_output.value)
    if value < amount:
        raise _fail(f"payment output is {amount - value} sompi short of {amount}", "underpaid")
    if value != amount:
        raise _fail(f"payment output must be exactly {amount} sompi, got {value}")
    if value < EXACT_V2_MIN_MERCHANT_OUTPUT_SOMPI:
        raise _fail(f"payment output is below the {EXACT_V2_MIN_MERCHANT_OUTPUT_SOMPI} sompi minimum", "underpaid")
    funding_scripts = {tx_input.utxo.scriptPublicKey for tx_input in tx.inputs}
    for index, output in enumerate(tx.outputs):
        if index != payment_output_index and output.scriptPublicKey not in funding_scripts:
            raise _fail(f"output {index} is neither the payment nor change to a funding script")
    return value


def _check_mass(tx: SafeTransaction, sdk_tx: Transaction, network_id: NetworkId) -> None:
    input_values = [int(tx_input.utxo.amount) for tx_input in tx.inputs]
    output_values = [int(output.value) for output in tx.outputs]
    if sum(output_values) > sum(input_values):
        raise _fail("transaction outputs exceed its inputs", "tx_rejected")
    fee = sum(input_values) - sum(output_values)
    if fee > EXACT_V2_MAX_MINER_FEE_SOMPI:
        raise _fail(f"miner fee {fee} exceeds the {EXACT_V2_MAX_MINER_FEE_SOMPI} sompi safety ceiling", "tx_rejected")
    try:
        storage_mass = calculate_storage_mass(network_id, input_values, output_values)
        total_mass = calculate_transaction_mass(network_id, sdk_tx)
    except Exception as exc:
        raise _fail(f"transaction mass could not be computed: {exc}") from exc
    if storage_mass is None:
        raise _fail("storage mass is not computable for this transaction")
    if int(tx.storageMass) != storage_mass:
        raise _fail(f"declared storageMass {tx.storageMass} differs from the contextual storage mass {storage_mass}")
    limit = maximum_standard_transaction_mass()
    if storage_mass > limit or total_mass > limit:
        raise _fail(
            f"transaction mass {max(storage_mass, total_mass)} exceeds the standard maximum {limit}", "tx_rejected"
        )


def _check_input_signatures(tx: SafeTransaction, sdk_tx: Transaction) -> None:
    for index, tx_input in enumerate(tx.inputs):
        script = bytes.fromhex(tx_input.signatureScript)
        if len(script) != 66 or script[0] != SCHNORR_SIGNATURE_PUSH or script[65] != SIGHASH_ALL:
            raise _fail(f"input {index} signature script must be one Schnorr signature with SIGHASH_ALL")
        try:
            digest = bytes.fromhex(compute_sighash(sdk_tx, index).to_hex())
        except Exception as exc:
            raise _fail(f"input {index} sighash could not be computed: {exc}") from exc
        if not verify_schnorr(digest, _signer_key(tx_input.utxo.scriptPublicKey), script[1:65]):
            raise _fail(f"input {index} signature is invalid")


def check_authorization_expiry(payment: ExactV2PaymentPayload, requirement: ExactV2Requirements, now: datetime) -> None:
    """`expiresAt` must be in the future and no later than now + `maxTimeoutSeconds` (interop-v1 `expiry` cases)."""
    expires_at = parse_expires_at(payment.payload.authorization.expiresAt)
    if expires_at <= now:
        raise _fail("expired_authorization: authorization.expiresAt has passed")
    if expires_at > now + timedelta(seconds=requirement.maxTimeoutSeconds):
        raise _fail("authorization_exceeds_max_timeout: authorization.expiresAt is beyond maxTimeoutSeconds")


def verify_exact_v2(
    payment: ExactV2PaymentPayload,
    requirement: ExactV2Requirements,
    *,
    request_hash: str,
    network_id: NetworkId,
    network_type: str,
    now: datetime,
    check_expiry: bool = True,
) -> ExactV2Verification:
    """Run every network-free check; raise `PaymentVerificationError` on the first failure.

    `requirement` is the offer the gateway would make for this request right now and `request_hash` the gateway's own
    fingerprint of the request; neither is taken from the client. `check_expiry=False` is used only to re-verify a
    payment that already settled (idempotent redelivery).
    """
    if payment.accepted.network != requirement.network:
        raise _fail(f"payment network {payment.accepted.network} is not {requirement.network}", "wrong_network")
    if payment.accepted != requirement:
        raise _fail("invalid_kaspa_x402_accepted: `accepted` is not the payment requirement offered for this request")
    payment_identifier = payment.payment_identifier
    if payment_identifier is None or payment.extensions is None:
        raise _fail("missing_kaspa_payment_identifier: extensions.payment-identifier.info.id is required")
    if not payment.extensions.payment_identifier.info.required:
        raise _fail("payment-identifier must echo required: true")
    body = payment.payload
    if body.requestHash != request_hash:
        raise _fail("requestHash does not match this request (method, URL, body and payment requirements)")

    tx = parse_safe_transaction(body.transaction)
    _check_envelope(tx)
    tx_id = transaction_id_v0(tx)
    if tx_id != tx.id:
        raise _fail("transaction id does not match the transaction content")
    merchant_sompi = _check_outputs(tx, requirement, body.paymentOutputIndex)
    sdk_tx = to_sdk_transaction(tx, network_type)
    if sdk_tx.id != tx_id:
        raise _fail("transaction id does not match the transaction content")
    _check_mass(tx, sdk_tx, network_id)
    _check_input_signatures(tx, sdk_tx)

    auth = body.authorization
    if auth.inputIndex >= len(tx.inputs):
        raise _fail("authorization.inputIndex is outside the transaction inputs")
    signer_script = tx.inputs[auth.inputIndex].utxo.scriptPublicKey
    expected_digest = authorization_digest(
        network=requirement.network,
        transaction_id=tx_id,
        payment_output_index=body.paymentOutputIndex,
        amount=requirement.amount,
        pay_to=requirement.payTo,
        pay_to_script_public_key=requirement.extra.payToScriptPublicKey,
        payment_requirements_hash=payment_requirements_hash(requirement),
        request_hash=request_hash,
        payment_identifier=payment_identifier,
        input_index=auth.inputIndex,
        expires_at=auth.expiresAt,
    )
    if auth.digest != expected_digest:
        raise _fail("authorization digest does not bind this transaction, request and payment identifier")
    signature = bytes.fromhex(auth.signature)
    if not verify_schnorr(bytes.fromhex(expected_digest), _signer_key(signer_script), signature):
        raise _fail("authorization signature is not valid for the key of authorization.inputIndex")
    payer = _owner(signer_script, network_type)
    if body.payerAddress is not None and body.payerAddress.strip().lower() != payer:
        raise _fail("payerAddress is not the owner of authorization.inputIndex")
    if check_expiry:
        check_authorization_expiry(payment, requirement, now)

    scripts = tuple(tx_input.utxo.scriptPublicKey for tx_input in tx.inputs)
    return ExactV2Verification(
        payment=VerifiedPayment(
            tx_id=tx_id, payer_address=payer, total_sompi=merchant_sompi, merchant_sompi=merchant_sompi, fee_sompi=0
        ),
        transaction=sdk_tx,
        outpoints=tuple((i.previousOutpoint.transactionId, i.previousOutpoint.index) for i in tx.inputs),
        input_values=tuple(int(i.utxo.amount) for i in tx.inputs),
        input_scripts=scripts,
        lookup_addresses=tuple(sorted({_owner(script, network_type) for script in scripts})),
        payment_identifier=payment_identifier,
        request_hash=request_hash,
        payment_output_index=body.paymentOutputIndex,
    )
