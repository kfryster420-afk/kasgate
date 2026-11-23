"""Community kaspa-x402 v2 wire format (CONTRACT v2.10): scheme `exact`, binding `kaspa-exact-v2`, standard-native.

Interop target: elldeeone/kaspa-x402 v1.0.1 (commit 56221913531bcb8367a900d28ed24c62d50591a1). Field names, patterns and
the canonical JSON / hash constructions below mirror `protocol/schemas/*.schema.json`,
`protocol/packages/core/src/{stable-json,exact-authorization,headers}.ts` and `protocol/spec/kaspa-exact-v2.md` at
that commit. Every model is strict: unknown fields are rejected, except inside the transaction artifact, which
`app.exact_v2` parses with the official parser's rules.

KasGate offers this binding next to its own `kaspa-exact-v1` in one PAYMENT-REQUIRED envelope (see
`app.schemas.PaymentRequiredEnvelope`). Kyle's beta decision: the v2 entry pays the merchant the full list price
(`payTo` = merchant, `amount` = price) and KasGate takes NO platform fee on this path.
"""

import base64
import binascii
import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import KaspaNetwork
from app.kaspa_types import SompiStr, serialized_script_public_key, validate_kaspa_address, validate_tx_id

X402_V2_SCHEME: Final = "exact"
EXACT_V2_BINDING: Final = "kaspa-exact-v2"
EXACT_V2_PROFILE: Final = "standard-native"
EXACT_V2_PAYMENT_FLOW: Final = "upfront"
EXACT_V2_FINALITY: Final = "accepted"
EXACT_V2_TRANSACTION_ENCODING: Final = "kaspa-sdk-safe-json-v2.0.0"
EXACT_V2_AUTHORIZATION_VERSION: Final = "kaspa-x402-exact-request-authorization-v2"
EXACT_V2_PAYMENT_TYPE: Final = "exact-transaction"
PAYMENT_IDENTIFIER_EXTENSION: Final = "payment-identifier"

# KasGate application policy, NOT part of the binding (kaspa-exact-v2.md defines no universal floor): the single
# merchant output of a v2 payment must be at least MIN_OUTPUT_SOMPI (0.2 KAS), the size of the official interop
# vector's merchant output, so a payment with change stays well inside the standard KIP-9 storage mass.
EXACT_V2_MIN_MERCHANT_OUTPUT_SOMPI: Final = 20_000_000
# Same ceiling as the official verifier's MAX_SAFE_TRANSACTION_FEE_SOMPI (adapters.ts): the miner fee the payer adds
# on top of `amount` may not exceed 1 KAS.
EXACT_V2_MAX_MINER_FEE_SOMPI: Final = 100_000_000
EXACT_V2_MAX_ACCEPTS: Final = 16
MAX_TRANSACTION_ARTIFACT_CHARS: Final = 32_768
MAX_RESOURCE_URL_CHARS: Final = 4_096

HASH32_PATTERN: Final = r"^[0-9a-fA-F]{64}$"
SIGNATURE64_PATTERN: Final = r"^[0-9a-fA-F]{128}$"
SCRIPT_PUBLIC_KEY_PATTERN: Final = r"^0000(?:[0-9a-fA-F]{2})+$"
PAYMENT_IDENTIFIER_PATTERN: Final = r"^[A-Za-z0-9_-]+$"
# kaspa-payment-payload.schema.json: ISO-8601 Zulu with optional milliseconds.
EXPIRES_AT_PATTERN: Final = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{3})?Z$"
UINT32_MAX: Final = 4_294_967_295

# `protocol/schemas/payment-identifier.schema.json` at v1.0.1, advertised verbatim (the official client compares the
# advertised schema against this exact document before trusting it).
PAYMENT_IDENTIFIER_SCHEMA: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://kaspa-x402.org/schemas/payment-identifier.schema.json",
    "title": "Kaspa x402 Payment Identifier Extension Info",
    "type": "object",
    "required": ["required"],
    "properties": {
        "required": {"type": "boolean"},
        "id": {"type": "string", "minLength": 16, "maxLength": 128, "pattern": "^[A-Za-z0-9_-]+$"},
    },
    "additionalProperties": True,
}

Hash32 = Annotated[str, Field(pattern=HASH32_PATTERN)]
Signature64 = Annotated[str, Field(pattern=SIGNATURE64_PATTERN)]
Uint32 = Annotated[int, Field(ge=0, le=UINT32_MAX)]
PositiveUint32 = Annotated[int, Field(ge=1, le=UINT32_MAX)]
PaymentIdentifier = Annotated[str, Field(min_length=16, max_length=128, pattern=PAYMENT_IDENTIFIER_PATTERN)]


# ---------------------------------------------------------------- canonical JSON and hashes


def canonical_json(value: object) -> str:
    """`stableStringify` of @kaspa-x402/core: sorted keys, no insignificant whitespace, raw UTF-8 (no \\u escapes)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def encode_canonical_header(value: object) -> str:
    """Header encoding of @kaspa-x402/core (`headers.ts`): Base64 (standard, padded) of the canonical JSON."""
    return base64.b64encode(canonical_json(value).encode("utf-8")).decode("ascii")


def decode_header_json(value: str, max_bytes: int) -> object:
    """Base64 -> JSON for an x402 header, bounded by `max_bytes` of encoded input. Raises ValueError."""
    if not value or len(value) > max_bytes:
        raise ValueError("header missing or too large")
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("header is not valid Base64") from exc
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError("header is not valid JSON") from exc


def payment_requirements_hash(requirements: "ExactV2Requirements") -> str:
    """SHA-256 of the canonical JSON of the complete selected PaymentRequirements (kaspa-exact-v2.md, digest inputs)."""
    return sha256_hex(canonical_json(requirements.wire()))


def http_request_hash(method: str, url: str, body: str | None, requirements_hash: str) -> str:
    """The official HTTP request fingerprint (`fingerprintHttpRequest` / `fingerprintRequest`, no trusted context).

    sha256(stableStringify({method, url, body, paymentRequirementsHash})) where `body` is the request body text, or
    JSON null when the request has no body.
    """
    return sha256_hex(
        canonical_json({"method": method, "url": url, "body": body, "paymentRequirementsHash": requirements_hash})
    )


def authorization_preimage(
    *,
    network: str,
    transaction_id: str,
    payment_output_index: int,
    amount: str,
    pay_to: str,
    pay_to_script_public_key: str,
    payment_requirements_hash: str,
    request_hash: str,
    payment_identifier: str,
    input_index: int,
    expires_at: str,
    profile: str = EXACT_V2_PROFILE,
    challenge_id: str | None = None,
) -> str:
    """`exactRequestAuthorizationPreimage` (exact-authorization.ts). KasGate only verifies standard-native, where
    `challengeId` is null; `profile`/`challenge_id` exist so the official additive vector can be recomputed."""
    return canonical_json(
        {
            "scope": EXACT_V2_AUTHORIZATION_VERSION,
            "network": network,
            "profile": profile,
            "transactionId": transaction_id.lower(),
            "paymentOutputIndex": payment_output_index,
            "amount": amount,
            "payTo": pay_to,
            "payToScriptPublicKey": pay_to_script_public_key.lower(),
            "paymentRequirementsHash": payment_requirements_hash.lower(),
            "requestHash": request_hash.lower(),
            "paymentIdentifier": payment_identifier,
            "challengeId": challenge_id,
            "inputIndex": input_index,
            "expiresAt": expires_at,
        }
    )


def authorization_digest(**fields: Any) -> str:
    """SHA-256 of `authorization_preimage(**fields)`; the 32-byte message the payer signs with BIP-340 Schnorr."""
    return sha256_hex(authorization_preimage(**fields))


def parse_expires_at(value: str) -> datetime:
    """Parse an ISO-8601 Zulu timestamp (optional milliseconds) as an aware UTC datetime. Raises ValueError."""
    if not re.fullmatch(EXPIRES_AT_PATTERN, value):
        raise ValueError("expiresAt must be ISO-8601 UTC with a Z suffix")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _lower(value: object) -> object:
    return value.lower() if isinstance(value, str) else value


# ---------------------------------------------------------------- models


class V2Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    def wire(self) -> dict[str, Any]:
        """JSON-ready dict with the wire field names; absent optional fields are omitted, never sent as null."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class ExactV2Extra(V2Model):
    binding: Literal["kaspa-exact-v2"]
    profile: Literal["standard-native"]
    paymentFlow: Literal["upfront"]
    finality: Literal["accepted"]
    transactionEncoding: Literal["kaspa-sdk-safe-json-v2.0.0"]
    payToScriptPublicKey: Annotated[str, Field(pattern=SCRIPT_PUBLIC_KEY_PATTERN, max_length=1_024)]

    @field_validator("payToScriptPublicKey", mode="before")
    @classmethod
    def _normalise(cls, v: object) -> object:
        return _lower(v)


class ExactV2Requirements(V2Model):
    """One `accepts[]` entry of the kaspa-exact-v2 standard-native offer. `amount` is the ENTIRE merchant gain."""

    scheme: Literal["exact"]
    network: KaspaNetwork
    amount: SompiStr
    asset: Literal["KAS"]
    payTo: str
    maxTimeoutSeconds: PositiveUint32
    extra: ExactV2Extra

    @model_validator(mode="after")
    def _pay_to_matches_script(self) -> Self:
        validate_kaspa_address(self.payTo, self.network)
        if self.payTo != self.payTo.lower():
            raise ValueError("payTo must be a lowercase Kaspa address")
        if serialized_script_public_key(self.payTo) != self.extra.payToScriptPublicKey:
            raise ValueError("extra.payToScriptPublicKey does not match payTo")
        return self

    @classmethod
    def for_merchant(cls, *, network: KaspaNetwork, amount_sompi: int, pay_to: str, max_timeout_seconds: int) -> Self:
        if amount_sompi < EXACT_V2_MIN_MERCHANT_OUTPUT_SOMPI:
            raise ValueError(
                f"kaspa-exact-v2 amount {amount_sompi} is below the KasGate minimum merchant output "
                f"{EXACT_V2_MIN_MERCHANT_OUTPUT_SOMPI} sompi"
            )
        address = validate_kaspa_address(pay_to, network)
        return cls(
            scheme="exact",
            network=network,
            amount=str(amount_sompi),
            asset="KAS",
            payTo=address,
            maxTimeoutSeconds=max_timeout_seconds,
            extra=ExactV2Extra(
                binding="kaspa-exact-v2",
                profile="standard-native",
                paymentFlow="upfront",
                finality="accepted",
                transactionEncoding="kaspa-sdk-safe-json-v2.0.0",
                payToScriptPublicKey=serialized_script_public_key(address),
            ),
        )


class ResourceInfo(V2Model):
    url: Annotated[str, Field(min_length=1, max_length=MAX_RESOURCE_URL_CHARS)]
    description: Annotated[str, Field(max_length=500)] | None = None
    mimeType: Annotated[str, Field(max_length=200)] | None = None


class PaymentIdentifierInfo(V2Model):
    required: bool
    id: PaymentIdentifier | None = None


class PaymentIdentifierExtension(V2Model):
    info: PaymentIdentifierInfo
    schema_: dict[str, Any] = Field(alias="schema")


class PaymentExtensions(V2Model):
    payment_identifier: PaymentIdentifierExtension = Field(alias="payment-identifier")

    @classmethod
    def required_identifier(cls, identifier: str | None = None) -> Self:
        return cls.model_validate(
            {
                PAYMENT_IDENTIFIER_EXTENSION: {
                    "info": {"required": True, **({"id": identifier} if identifier else {})},
                    "schema": PAYMENT_IDENTIFIER_SCHEMA,
                }
            }
        )


class ExactV2Authorization(V2Model):
    version: Literal["kaspa-x402-exact-request-authorization-v2"]
    inputIndex: Uint32
    expiresAt: Annotated[str, Field(pattern=EXPIRES_AT_PATTERN)]
    digest: Hash32
    signature: Signature64

    @field_validator("digest", "signature", mode="before")
    @classmethod
    def _normalise(cls, v: object) -> object:
        return _lower(v)


class ExactV2TransactionPayload(V2Model):
    """`PaymentPayload.payload` for `type: exact-transaction`, profile standard-native.

    There is deliberately no `transactionId`: the schema forbids a client-authoritative id and the verifier
    recomputes it from the transaction. `challengeId` is forbidden on standard-native.
    """

    type: Literal["exact-transaction"]
    profile: Literal["standard-native"]
    payerAddress: Annotated[str, Field(max_length=100)] | None = None
    transaction: Annotated[str, Field(min_length=2, max_length=MAX_TRANSACTION_ARTIFACT_CHARS)]
    transactionEncoding: Literal["kaspa-sdk-safe-json-v2.0.0"]
    paymentOutputIndex: Uint32
    requestHash: Hash32
    authorization: ExactV2Authorization

    @field_validator("requestHash", mode="before")
    @classmethod
    def _normalise(cls, v: object) -> object:
        return _lower(v)


class ExactV2PaymentPayload(V2Model):
    """Body of a kaspa-exact-v2 PAYMENT-SIGNATURE header: `{x402Version, accepted, payload, extensions}`."""

    x402Version: Literal[2]
    accepted: ExactV2Requirements
    payload: ExactV2TransactionPayload
    extensions: PaymentExtensions | None = None

    @property
    def payment_identifier(self) -> str | None:
        return None if self.extensions is None else self.extensions.payment_identifier.info.id


class KaspaSettlementExtension(V2Model):
    """`extensions.kaspa` of a standard-native SettlementResponse.

    Field names follow `settlement-response.schema.json` and what the official server emits
    (`#buildSuccessfulExactSettlement` in direct-server.ts): `exactProfile`, `paymentOutputIndex`, `finality`,
    `requestHash`, `transactionEncoding`. The official client (`applyExactSettlement`) requires `exactProfile` to equal
    the accepted profile, `paymentOutputIndex` to equal the payload's and `requestHash` to equal the payload's. The
    prose example's `binding` / `profile` keys are not emitted by the reference server, so KasGate does not emit them.
    """

    exactProfile: Literal["standard-native"]
    paymentOutputIndex: Uint32
    finality: Literal["accepted"]
    requestHash: Hash32
    transactionEncoding: Literal["kaspa-sdk-safe-json-v2.0.0"]


class KasGateSettlementExtension(V2Model):
    """KasGate-specific receipt data, kept out of the top level (the v2 schema forbids top-level `extra`)."""

    platformFeeSompi: Literal["0"]
    usdValueAtSettlement: Annotated[Decimal, Field(ge=0, max_digits=20, decimal_places=8)]


class SettlementExtensions(V2Model):
    kaspa: KaspaSettlementExtension
    kasgate: KasGateSettlementExtension


class SettlementResponse(V2Model):
    """Body of PAYMENT-RESPONSE for a kaspa-exact-v2 payment (success only; failures are HTTP errors)."""

    success: Literal[True]
    transaction: str
    network: KaspaNetwork
    payer: str
    amount: SompiStr
    extensions: SettlementExtensions

    @field_validator("transaction")
    @classmethod
    def _tx(cls, v: str) -> str:
        return validate_tx_id(v)

    def to_header(self) -> str:
        return encode_canonical_header(self.wire())

    @classmethod
    def from_header(cls, value: str, max_bytes: int) -> Self:
        return cls.model_validate(decode_header_json(value, max_bytes))
