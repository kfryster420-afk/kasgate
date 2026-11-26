import base64
import binascii
import hashlib
import ipaddress
import re
import secrets
import uuid
from datetime import date, datetime
from decimal import ROUND_CEILING, Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    HttpUrl,
    ValidationError,
    computed_field,
    field_validator,
    model_validator,
)
from pydantic_core import InitErrorDetails, PydanticCustomError

from app.config import (
    BPS_DENOMINATOR,
    PLATFORM_FEE_BPS,
    SOMPI_PER_KAS,
    KaspaNetwork,
    get_settings,
)
from app.kaspa_types import KASPA_ADDRESS_RE as KASPA_ADDRESS_RE
from app.kaspa_types import MAX_SOMPI as MAX_SOMPI
from app.kaspa_types import SOMPI_STR_RE as SOMPI_STR_RE
from app.kaspa_types import TX_ID_RE as TX_ID_RE
from app.kaspa_types import Sompi, SompiStr, validate_kaspa_address, validate_tx_id
from app.models import (
    PREPAID_EXPIRY_DAYS_MAX,
    TRIAL_DAILY_CAP_DEFAULT,
    TRIAL_DAILY_CAP_MAX,
    TRIAL_MAX_CALLS,
    WEBHOOK_EVENT_TYPES,
    PriceCurrency,
    SettlementStatus,
)
from app.x402_v2 import (
    EXACT_V2_MAX_ACCEPTS,
    ExactV2Requirements,
    PaymentExtensions,
    ResourceInfo,
    decode_header_json,
    encode_canonical_header,
)

HEADER_PAYMENT_REQUIRED = "PAYMENT-REQUIRED"
HEADER_PAYMENT_SIGNATURE = "PAYMENT-SIGNATURE"
HEADER_PAYMENT_RESPONSE = "PAYMENT-RESPONSE"
HEADER_K402_PAYMENT = "X-K402-Payment"
# CONTRACT v2.11 prepaid credit: the signed debit on a paid call and the debit receipt on its response.
HEADER_PREPAID_DEBIT = "X-KasGate-Prepaid"
HEADER_PREPAID_RECEIPT = "X-KasGate-Prepaid-Receipt"
# CONTRACT v2.12 free trial calls: the offer on a 402, the signed trial call, and the trial state on its response.
HEADER_TRIAL_OFFER = "X-KasGate-Trial-Offer"
HEADER_TRIAL_CALL = "X-KasGate-Trial"
HEADER_TRIAL_RECEIPT = "X-KasGate-Trial-Receipt"
HEADER_TRIAL_LIMIT = "X-KasGate-Trial-Limit"
HEADER_TRIAL_REMAINING = "X-KasGate-Trial-Remaining"
# Failed-delivery auto-credit: on the failing response when the call was credited back to prepaid there and then.
HEADER_FAILED_DELIVERY_CREDIT = "X-KasGate-Credit"
# CONTRACT v2.14: the buyer's failed-delivery credit summary on the signed POST /v1/prepaid/refunds/status reply.
HEADER_CREDIT_SUMMARY = "X-KasGate-Credit-Summary"
MAX_SUMMARY_CREDITS = 10

PROXY_PATH_PREFIX = "/p"
MAX_HEADER_BYTES = 40_960
MAX_RAW_TX_CHARS = 24_000
PAYMENT_PAYLOAD_PREFIX = "kasgate:v1:"
NONCE_RE = r"^[0-9a-f]{32}$"
AUTH_PURPOSE = "Sign in to the KasGate merchant dashboard. This does not authorise any payment."
REGISTER_PURPOSE = "Register this address as a KasGate merchant. This does not authorise any payment."

K402_VERSION = "0.2"
K402_PAYMENT_ID_RE = r"^p_[0-9a-f]{16}$"
MAX_K402_HEADER_CHARS = 256
# Uniqueness tag added to the merchant output of every k402 offer: 0 .. K402_TAG_SPACE - 1 sompi (< 0.01 KAS).
# k402 transactions carry no payload nonce or payerProof, so the exact amounts are the only thing that ties a txid to
# one offer; the tag makes a bystander's own offer for the same endpoint very unlikely to match someone else's tx.
K402_TAG_SPACE = 1_000_000
K402_NETWORK_BY_CONFIG: dict[str, str] = {"kaspa:testnet-10": "testnet-10", "kaspa:mainnet": "mainnet"}
PaymentScheme = Literal["kaspa-exact-v1", "k402-kaspa-utxo", "kaspa-exact-v2"]
K402Network = Literal["testnet-10", "mainnet"]
SLUG_RE = r"^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])$"

PREPAID_SCHEME = "kasgate-prepaid-v1"
PREPAID_DEBIT_PURPOSE = "kasgate:v1:prepaid-debit"
# Body-Hash of a request without a body.
PREPAID_EMPTY_BODY_HASH = "-"
PREPAID_BODY_HASH_RE = r"^(?:[0-9a-f]{64}|-)$"
PREPAID_PATH_MAX_LENGTH = 2048
MAX_PREPAID_HEADER_CHARS = 512

TRIAL_SCHEME = "kasgate-trial-v1"
TRIAL_CALL_PURPOSE = "kasgate:v1:trial-call"

# CONTRACT v2.13 prepaid refunds.
PREPAID_REFUND_PURPOSE = "kasgate:v1:prepaid-refund"
PREPAID_REFUND_PAYLOAD_PREFIX = "kasgate:v1:refund:"
PREPAID_REFUND_REASON_MAX_LENGTH = 300
PrepaidRefundAction = Literal["request", "cancel", "status"]
PrepaidRefundStatus = Literal["requested", "paying", "payout_submitted", "paid", "rejected", "cancelled"]
PrepaidRefundPayoutStatus = Literal["pending", "confirmed", "invalid", "duplicate", "abandoned"]

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
USD_MICROS_PER_DOLLAR = 1_000_000
MAX_USD_MICROS = 1_000_000_000_000_000  # $1e9
UsdMicros = Annotated[int, Field(gt=0, le=MAX_USD_MICROS)]
UsdDecimal = Annotated[Decimal, Field(ge=0, max_digits=20, decimal_places=8)]
FreeTrialCalls = Annotated[int, Field(strict=True, ge=0, le=TRIAL_MAX_CALLS)]
TrialDailyCap = Annotated[int, Field(strict=True, ge=1, le=TRIAL_DAILY_CAP_MAX)]
PrepaidExpiryDays = Annotated[int, Field(strict=True, ge=1, le=PREPAID_EXPIRY_DAYS_MAX)]


def sompi_to_kas(sompi: int) -> str:
    return format(Decimal(sompi) / Decimal(SOMPI_PER_KAS), "f")


class FeeSplit(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    total_sompi: int
    merchant_sompi: int
    fee_sompi: int


def compute_fee_split(total_sompi: int, min_output_sompi: int | None = None) -> FeeSplit:
    """Output 1 (treasury) fee = max(ceil(total * 150 / 10_000), MIN_OUTPUT_SOMPI); Output 0 (merchant) = total - fee.

    The fee never drops below the KIP-9 output floor, so the treasury output is always relayable.
    Raises ValueError when the merchant output would fall below the floor.
    """
    floor = get_settings().min_output_sompi if min_output_sompi is None else min_output_sompi
    if total_sompi <= 0:
        raise ValueError("total_sompi must be positive")
    pct_fee = (total_sompi * PLATFORM_FEE_BPS + BPS_DENOMINATOR - 1) // BPS_DENOMINATOR
    fee = max(pct_fee, floor)
    merchant = total_sompi - fee
    if merchant < 2 * floor:
        raise ValueError(
            f"price {total_sompi} sompi leaves the merchant output below 2*MIN_OUTPUT_SOMPI={2 * floor} "
            "(needed so a payment with change stays under the KIP-9 storage-mass limit); "
            f"minimum price is {min_price_sompi(floor)} sompi"
        )
    return FeeSplit(total_sompi=total_sompi, merchant_sompi=merchant, fee_sompi=fee)


def min_price_sompi(min_output_sompi: int | None = None) -> int:
    """Cheapest valid total: fee = floor (flat) and merchant = 2*floor.

    Two outputs at the floor already use the whole 100_000 standard storage mass, so any change output
    would make the tx non-standard. A 2*floor merchant output leaves room for change >= ~0.3 KAS.
    """
    floor = get_settings().min_output_sompi if min_output_sompi is None else min_output_sompi
    return 3 * floor


def check_on_chain_price(price_sompi: int) -> None:
    """Raise ValueError unless `price_sompi` is payable on-chain (fee split valid, >= min_price_sompi())."""
    compute_fee_split(price_sompi)


def usd_micros_to_prepaid_sompi(price_usd_micros: int, kas_usd_rate: Decimal) -> int:
    """Prepaid debit for a USD price: ceil(usd / rate * SOMPI_PER_KAS), at least 1 sompi, WITHOUT the on-chain floor.

    Prepaid debits never touch the chain, so the 0.6 KAS minimum does not apply to them (CONTRACT v2.11).
    """
    if price_usd_micros <= 0:
        raise ValueError("price_usd_micros must be positive")
    if isinstance(kas_usd_rate, bool) or not isinstance(kas_usd_rate, Decimal):
        raise ValueError("kas_usd_rate must be a positive finite Decimal")
    if not kas_usd_rate.is_finite() or kas_usd_rate <= 0:
        raise ValueError("kas_usd_rate must be a positive finite Decimal")
    sompi = (
        Decimal(price_usd_micros) * Decimal(SOMPI_PER_KAS) / (Decimal(USD_MICROS_PER_DOLLAR) * kas_usd_rate)
    ).to_integral_value(rounding=ROUND_CEILING)
    result = max(int(sompi), 1)
    if result > MAX_SOMPI:
        raise ValueError(f"converted price {result} sompi exceeds MAX_SOMPI")
    return result


def usd_micros_to_usd(price_usd_micros: int) -> str:
    """USD amount as a decimal string with micro-dollar precision."""
    return format(Decimal(price_usd_micros) / Decimal(USD_MICROS_PER_DOLLAR), "f")


def usd_micros_to_sompi(price_usd_micros: int, kas_usd_rate: Decimal, min_sompi: int | None = None) -> int:
    """Convert a USD micro-dollar price to sompi: ceil(usd / rate * SOMPI_PER_KAS), then the min-price floor.

    Uses Decimal throughout. The result always passes `compute_fee_split` (raises ValueError if it cannot).
    """
    if price_usd_micros <= 0:
        raise ValueError("price_usd_micros must be positive")
    if isinstance(kas_usd_rate, bool) or not isinstance(kas_usd_rate, Decimal):
        raise ValueError("kas_usd_rate must be a positive finite Decimal")
    if not kas_usd_rate.is_finite() or kas_usd_rate <= 0:
        raise ValueError("kas_usd_rate must be a positive finite Decimal")
    sompi = (
        Decimal(price_usd_micros) * Decimal(SOMPI_PER_KAS) / (Decimal(USD_MICROS_PER_DOLLAR) * kas_usd_rate)
    ).to_integral_value(rounding=ROUND_CEILING)
    if sompi > MAX_SOMPI:
        raise ValueError(f"converted price {sompi} sompi exceeds MAX_SOMPI")
    result = max(int(sompi), min_price_sompi() if min_sompi is None else min_sompi)
    if result > MAX_SOMPI:
        raise ValueError(f"converted price {result} sompi exceeds MAX_SOMPI")
    compute_fee_split(result)
    return result


def _reject_private_host(url: HttpUrl) -> HttpUrl:
    host = (url.host or "").strip("[]").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost") or host.endswith(".internal"):
        raise ValueError("upstream_url may not target a local host")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return url
    if not ip.is_global or ip.is_multicast:
        raise ValueError("upstream_url may not target a private or reserved IP")
    return url


def normalise_signature(v: object) -> object:
    """Accept 128-hex Schnorr, or Base64 of 64 bytes / 65 bytes (leading header byte dropped)."""
    if not isinstance(v, str):
        return v
    v = v.strip()
    if re.fullmatch(r"[0-9a-fA-F]{128}", v):
        return v.lower()
    try:
        raw = base64.b64decode(v, validate=True)
    except (binascii.Error, ValueError):
        return v
    if len(raw) == 65:
        raw = raw[1:]
    return raw.hex() if len(raw) == 64 else v


def payer_proof_message(nonce: str, tx_id: str) -> str:
    """Message the payer signs with the payerAddress key; binds the PAYMENT-SIGNATURE to its submitter."""
    return f"{PAYMENT_PAYLOAD_PREFIX}{nonce}:{tx_id}"


def new_payment_nonce() -> str:
    return secrets.token_hex(16)


def payment_payload(nonce: str) -> bytes:
    """Exact bytes the payer must put in the transaction payload to bind the tx to one 402 challenge."""
    return f"{PAYMENT_PAYLOAD_PREFIX}{nonce}".encode("ascii")


def build_auth_challenge(purpose: str, domain: str, address: str, nonce: str, expires_at: datetime) -> str:
    return (
        "KasGate\n"
        f"Domain: {domain}\n"
        f"Purpose: {purpose}\n"
        f"Address: {address}\n"
        f"Nonce: {nonce}\n"
        f"Expires: {expires_at.isoformat()}"
    )


def prepaid_body_hash(body: bytes) -> str:
    """Body-Hash of a prepaid debit: SHA-256 hex of the raw request body, or "-" when there is no body."""
    return hashlib.sha256(body).hexdigest() if body else PREPAID_EMPTY_BODY_HASH


def build_prepaid_debit_message(
    *,
    domain: str,
    address: str,
    merchant_id: uuid.UUID,
    method: str,
    path: str,
    body_hash: str,
    amount_sompi: int,
    nonce: str,
    expires_at: datetime,
) -> str:
    """The exact text a buyer signs (kaspa sign_message, depositing P2PK key) to authorise ONE prepaid debit.

    Multi-line like the login challenge, but its Purpose names the prepaid debit, so it can never be confused with a
    `payerProof` or a login/registration challenge. The gateway rebuilds it from the stored challenge and the request
    it is actually serving, never from client-supplied copies.
    """
    return (
        "KasGate\n"
        f"Domain: {domain}\n"
        f"Purpose: {PREPAID_DEBIT_PURPOSE}\n"
        f"Address: {address}\n"
        f"Merchant: {merchant_id}\n"
        f"Method: {method}\n"
        f"Path: {path}\n"
        f"Body-Hash: {body_hash}\n"
        f"Amount: {amount_sompi}\n"
        f"Nonce: {nonce}\n"
        f"Expires: {expires_at.isoformat()}"
    )


def build_prepaid_refund_message(
    *,
    domain: str,
    action: str,
    address: str,
    merchant_id: uuid.UUID,
    amount_sompi: int,
    refund_id: uuid.UUID | None,
    nonce: str,
    expires_at: datetime,
) -> str:
    """The exact text a buyer signs (kaspa sign_message, the depositing P2PK key) to open or cancel ONE refund, or
    to read their own refunds with the merchant's reasons (`status`, Amount 0, Refund `-`).

    Its Purpose differs from a prepaid debit, a trial call, a payerProof and a login, so no other signature can stand
    in for it. A refund signature moves no coins: it only asks the merchant to pay `amount_sompi` back on-chain to
    `address` (the depositing address), and the gateway debits the ledger only once that payout is seen on-chain.
    """
    return (
        "KasGate\n"
        f"Domain: {domain}\n"
        f"Purpose: {PREPAID_REFUND_PURPOSE}\n"
        f"Action: {action}\n"
        f"Address: {address}\n"
        f"Merchant: {merchant_id}\n"
        f"Amount: {amount_sompi}\n"
        f"Refund: {refund_id if refund_id is not None else '-'}\n"
        f"Nonce: {nonce}\n"
        f"Expires: {expires_at.isoformat()}"
    )


def refund_payout_payload(refund_id: uuid.UUID) -> bytes:
    """Payload every refund payout transaction must carry (`kasgate:v1:refund:<refund id hex>`), in both modes.

    KasWare: `sendKaspa(payout_address, amount_sompi, {payload: "kasgate:v1:refund:<hex>"})` (the wallet hex-encodes
    the UTF-8 string, which gives exactly these bytes)."""
    return f"{PREPAID_REFUND_PAYLOAD_PREFIX}{refund_id.hex}".encode("ascii")


def build_trial_call_message(
    *,
    domain: str,
    address: str,
    merchant_id: uuid.UUID,
    slug: str,
    method: str,
    path: str,
    body_hash: str,
    nonce: str,
    expires_at: datetime,
) -> str:
    """The exact text a buyer signs (kaspa sign_message, the P2PK key of `address`) to use ONE free trial call.

    Its Purpose differs from a prepaid debit, a payerProof and a login, so no other signature can stand in for it.
    The gateway rebuilds it from the stored challenge and the request it is actually serving.
    """
    return (
        "KasGate\n"
        f"Domain: {domain}\n"
        f"Purpose: {TRIAL_CALL_PURPOSE}\n"
        f"Address: {address}\n"
        f"Merchant: {merchant_id}\n"
        f"Endpoint: {slug}\n"
        f"Method: {method}\n"
        f"Path: {path}\n"
        f"Body-Hash: {body_hash}\n"
        f"Nonce: {nonce}\n"
        f"Expires: {expires_at.isoformat()}"
    )


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReadModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- x402 headers


class X402Header(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    def to_header(self) -> str:
        return base64.b64encode(self.model_dump_json(exclude_none=True).encode("utf-8")).decode("ascii")

    @classmethod
    def from_header(cls, value: str) -> Self:
        if not value or len(value) > MAX_HEADER_BYTES:
            raise ValueError("header missing or too large")
        try:
            raw = base64.b64decode(value.strip(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("header is not valid Base64") from exc
        return cls.model_validate_json(raw)


class PlatformFee(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    feeTo: str
    feeAmount: SompiStr

    @field_validator("feeTo")
    @classmethod
    def _fee_to(cls, v: str) -> str:
        return validate_kaspa_address(v)


class PaymentRequiredExtra(StrictModel):
    """Scheme-specific quote metadata locked into the challenge. Present on USD-priced quotes only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kasUsdRate: Annotated[str, Field(pattern=r"^[0-9]+(?:\.[0-9]+)?$")]
    quotedAt: datetime

    @field_validator("kasUsdRate")
    @classmethod
    def _rate(cls, v: str) -> str:
        rate = Decimal(v)
        if not rate.is_finite() or rate <= 0:
            raise ValueError("kasUsdRate must be a positive finite decimal string")
        return format(rate, "f")


class PaymentRequired(X402Header):
    """Body of the PAYMENT-REQUIRED header. `amount` is the TOTAL the payer sends across both outputs."""

    x402Version: Literal[2] = 2
    scheme: Literal["kaspa-exact-v1"] = "kaspa-exact-v1"
    network: KaspaNetwork
    asset: Literal["KAS"] = "KAS"
    amount: SompiStr
    payTo: str
    platformFee: PlatformFee
    nonce: Annotated[str, Field(pattern=NONCE_RE)]
    expiresAt: datetime
    extra: PaymentRequiredExtra | None = None

    @field_validator("payTo")
    @classmethod
    def _pay_to(cls, v: str) -> str:
        return validate_kaspa_address(v)

    @model_validator(mode="after")
    def _check_split(self) -> Self:
        for addr in (self.payTo, self.platformFee.feeTo):
            validate_kaspa_address(addr, self.network)
        split = compute_fee_split(int(self.amount))
        if int(self.platformFee.feeAmount) != split.fee_sompi:
            raise ValueError(f"platformFee.feeAmount must be {split.fee_sompi} for amount {self.amount}")
        return self

    @classmethod
    def from_header(cls, value: str) -> Self:
        """Decode a PAYMENT-REQUIRED header into the kaspa-exact-v1 terms.

        CONTRACT v2.10 headers are a `PaymentRequiredEnvelope`; its `kaspa-exact-v1` accepts entry is returned. A flat
        pre-v2.10 header is still accepted as is.
        """
        data = decode_header_json(value, MAX_HEADER_BYTES)
        if isinstance(data, dict) and "accepts" in data:
            return cls.model_validate(PaymentRequiredEnvelope.model_validate(data).v1_terms().model_dump())
        return cls.model_validate(data)

    @property
    def merchant_amount_sompi(self) -> int:
        return int(self.amount) - int(self.platformFee.feeAmount)

    @property
    def fee_amount_sompi(self) -> int:
        return int(self.platformFee.feeAmount)

    @classmethod
    def for_endpoint(
        cls,
        price_sompi: int,
        pay_to: str,
        nonce: str,
        expires_at: datetime,
        extra: PaymentRequiredExtra | None = None,
    ) -> Self:
        settings = get_settings()
        split = compute_fee_split(price_sompi)
        return cls(
            nonce=nonce,
            expiresAt=expires_at,
            network=settings.kaspa_network,
            amount=str(split.total_sompi),
            payTo=pay_to,
            platformFee=PlatformFee(feeTo=settings.kasgate_treasury_address, feeAmount=str(split.fee_sompi)),
            extra=extra,
        )


class KasGateV1Requirements(StrictModel):
    """The `kaspa-exact-v1` entry of the CONTRACT v2.10 `accepts[]`: the v1 terms without `x402Version`.

    Official kaspa-x402 clients skip this entry (unknown scheme); KasGate clients select it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    scheme: Literal["kaspa-exact-v1"]
    network: KaspaNetwork
    asset: Literal["KAS"]
    amount: SompiStr
    payTo: str
    platformFee: PlatformFee
    nonce: Annotated[str, Field(pattern=NONCE_RE)]
    expiresAt: datetime
    extra: PaymentRequiredExtra | None = None

    @model_validator(mode="after")
    def _same_rules_as_v1(self) -> Self:
        self.as_payment_required()
        return self

    def as_payment_required(self) -> PaymentRequired:
        return PaymentRequired.model_validate({"x402Version": 2, **self.model_dump()})

    @classmethod
    def from_required(cls, required: PaymentRequired) -> Self:
        return cls.model_validate(required.model_dump(exclude={"x402Version"}))


AcceptsEntry = Annotated[ExactV2Requirements | KasGateV1Requirements, Field(discriminator="scheme")]


class PaymentRequiredEnvelope(BaseModel):
    """Body of the CONTRACT v2.10 PAYMENT-REQUIRED header: an x402 v2 envelope offering both bindings.

    * `accepts[0]` is the community kaspa-x402 `exact` / `kaspa-exact-v2` standard-native offer (merchant gets the full
      list price, no platform fee), present whenever the price is payable on that path.
    * The `kaspa-exact-v1` entry carries KasGate's own nonce-bound terms with the atomic merchant/treasury split.
    * Backward compatibility: the same v1 terms are ALSO mirrored at the top level (`scheme`, `network`, `asset`,
      `amount`, `payTo`, `platformFee`, `nonce`, `expiresAt`), so pre-v2.10 readers that look up those keys keep
      working. The envelope schema allows additional top-level properties, so official clients ignore the mirror.
    * Encoded as canonical (key-sorted) JSON then standard Base64, like @kaspa-x402/core.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    x402Version: Literal[2]
    resource: ResourceInfo
    accepts: Annotated[list[AcceptsEntry], Field(min_length=1, max_length=EXACT_V2_MAX_ACCEPTS)]
    extensions: PaymentExtensions | None = None
    error: Annotated[str, Field(max_length=500)] | None = None
    # v1 mirror (pre-v2.10 flat header fields)
    scheme: Literal["kaspa-exact-v1"]
    network: KaspaNetwork
    asset: Literal["KAS"]
    amount: SompiStr
    payTo: str
    platformFee: PlatformFee
    nonce: Annotated[str, Field(pattern=NONCE_RE)]
    expiresAt: datetime
    extra: PaymentRequiredExtra | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        v1 = [entry for entry in self.accepts if isinstance(entry, KasGateV1Requirements)]
        v2 = [entry for entry in self.accepts if isinstance(entry, ExactV2Requirements)]
        if len(v1) != 1 or len(v2) > 1:
            raise ValueError("accepts must hold exactly one kaspa-exact-v1 entry and at most one exact entry")
        if v1[0].model_dump() != self.model_dump(include=set(KasGateV1Requirements.model_fields)):
            raise ValueError("top-level kaspa-exact-v1 terms must mirror the kaspa-exact-v1 accepts entry")
        if v2 and not isinstance(self.accepts[0], ExactV2Requirements):
            raise ValueError("the exact (kaspa-exact-v2) entry must come first")
        if bool(v2) != (self.extensions is not None):
            raise ValueError("the payment-identifier extension is advertised exactly when the exact entry is")
        if v2 and (v2[0].network != v1[0].network or v2[0].payTo != v1[0].payTo):
            raise ValueError("both bindings must pay the same merchant on the same network")
        return self

    @classmethod
    def build(
        cls,
        required: PaymentRequired,
        exact: ExactV2Requirements | None,
        *,
        resource_url: str,
        description: str | None = None,
        error: str | None = None,
    ) -> Self:
        accepts: list[ExactV2Requirements | KasGateV1Requirements] = []
        if exact is not None:
            accepts.append(exact)
        accepts.append(KasGateV1Requirements.from_required(required))
        return cls(
            x402Version=2,
            resource=ResourceInfo(url=resource_url, description=description),
            accepts=accepts,
            extensions=PaymentExtensions.required_identifier() if exact is not None else None,
            error=error,
            **required.model_dump(exclude={"x402Version"}),
        )

    def v1_terms(self) -> PaymentRequired:
        return next(e for e in self.accepts if isinstance(e, KasGateV1Requirements)).as_payment_required()

    def exact_v2(self) -> ExactV2Requirements | None:
        return next((e for e in self.accepts if isinstance(e, ExactV2Requirements)), None)

    def wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)

    def to_header(self) -> str:
        return encode_canonical_header(self.wire())

    @classmethod
    def from_header(cls, value: str) -> Self:
        return cls.model_validate(decode_header_json(value, MAX_HEADER_BYTES))


class PaymentSignature(X402Header):
    """Body of the PAYMENT-SIGNATURE header. `rawTx` is the JSON-serialised signed Kaspa transaction."""

    txId: str
    rawTx: str = Field(min_length=2, max_length=MAX_RAW_TX_CHARS)
    payerAddress: str
    nonce: Annotated[str, Field(pattern=NONCE_RE)]
    payerProof: Annotated[str, Field(pattern=r"^[0-9a-f]{128}$")]
    """Schnorr sign_message(payer_proof_message(nonce, txId)) by payerAddress's key; stops mempool front-running."""

    @field_validator("payerProof", mode="before")
    @classmethod
    def _proof(cls, v: object) -> object:
        return normalise_signature(v)

    @field_validator("txId")
    @classmethod
    def _tx(cls, v: str) -> str:
        return validate_tx_id(v)

    @field_validator("payerAddress")
    @classmethod
    def _payer(cls, v: str) -> str:
        return validate_kaspa_address(v)


class PaymentResponse(X402Header):
    txId: str
    settled: Literal[True] = True
    network: KaspaNetwork
    usdValueAtSettlement: UsdDecimal

    @field_validator("txId")
    @classmethod
    def _tx(cls, v: str) -> str:
        return validate_tx_id(v)


# ---------------------------------------------------------------- k402 (kaspa-402/k402 PROTOCOL.md, scheme kaspa-utxo)


def k402_network_label(network: str) -> str:
    """k402 wire network name for a KasGate network (``kaspa:testnet-10`` -> ``testnet-10``), never the CAIP form."""
    try:
        return K402_NETWORK_BY_CONFIG[network]
    except KeyError as exc:
        raise ValueError(f"unsupported Kaspa network: {network}") from exc


def new_k402_payment_id() -> str:
    return "p_" + secrets.token_hex(8)


def new_k402_tag() -> int:
    return secrets.randbelow(K402_TAG_SPACE)


class K402FacilitatorFee(StrictModel):
    """``facilitator_fee`` of a k402 offer: the KasGate treasury output, paid as output 1 by ``HotWallet.pay``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sompi: SompiStr
    to: str
    by: Literal["kasgate"] = "kasgate"

    @field_validator("to")
    @classmethod
    def _to(cls, v: str) -> str:
        return validate_kaspa_address(v)


class K402UtxoOffer(StrictModel):
    """One ``kaspa-utxo`` entry of a k402 402 body.

    ``amount_sompi`` is the merchant output only; ``facilitator_fee.sompi`` is the exact KasGate fee for the offer total
    ``amount_sompi + facilitator_fee.sompi`` (``max(ceil(total * 1.5 %), MIN_OUTPUT_SOMPI)``), and the total is at least
    ``3 * MIN_OUTPUT_SOMPI`` (0.6 KAS).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scheme: Literal["kaspa-utxo"] = "kaspa-utxo"
    network: K402Network
    amount_sompi: SompiStr
    pay_to: str
    payment_id: Annotated[str, Field(pattern=K402_PAYMENT_ID_RE)]
    expires: Annotated[int, Field(gt=0)]
    description: Annotated[str, Field(max_length=200)] = ""
    facilitator_fee: K402FacilitatorFee

    @model_validator(mode="after")
    def _check_split(self) -> Self:
        settings = get_settings()
        expected_network = k402_network_label(settings.kaspa_network)
        if self.network != expected_network:
            raise ValueError(f"k402 network must be '{expected_network}' on this gateway")
        for address in (self.pay_to, self.facilitator_fee.to):
            validate_kaspa_address(address, settings.kaspa_network)
        split = compute_fee_split(self.total_sompi)
        if int(self.facilitator_fee.sompi) != split.fee_sompi:
            raise ValueError(f"facilitator_fee.sompi must be {split.fee_sompi} for a {self.total_sompi} sompi total")
        if self.total_sompi < min_price_sompi():
            raise ValueError(f"offer total must be at least {min_price_sompi()} sompi")
        return self

    @field_validator("pay_to")
    @classmethod
    def _pay_to(cls, v: str) -> str:
        return validate_kaspa_address(v)

    @property
    def merchant_sompi(self) -> int:
        return int(self.amount_sompi)

    @property
    def fee_sompi(self) -> int:
        return int(self.facilitator_fee.sompi)

    @property
    def total_sompi(self) -> int:
        return int(self.amount_sompi) + int(self.facilitator_fee.sompi)

    @classmethod
    def for_endpoint(
        cls,
        price_sompi: int,
        pay_to: str,
        payment_id: str,
        expires_at: datetime,
        *,
        tag_sompi: int,
        description: str = "",
    ) -> Self:
        """Offer for ``price_sompi + tag_sompi`` split exactly like a v1 challenge of that total."""
        if not 0 <= tag_sompi < K402_TAG_SPACE:
            raise ValueError("tag_sompi is outside the k402 tag space")
        settings = get_settings()
        split = compute_fee_split(price_sompi + tag_sompi)
        return cls(
            network=k402_network_label(settings.kaspa_network),
            amount_sompi=str(split.merchant_sompi),
            pay_to=pay_to,
            payment_id=payment_id,
            expires=int(expires_at.timestamp()),
            description=description[:200],
            facilitator_fee=K402FacilitatorFee(to=settings.kasgate_treasury_address, sompi=str(split.fee_sompi)),
        )


class K402PaymentHeader(BaseModel):
    """``X-K402-Payment: kaspa-utxo <txid> <payment_id>``. Only the ``kaspa-utxo`` scheme is accepted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scheme: Literal["kaspa-utxo"]
    tx_id: str
    payment_id: Annotated[str, Field(pattern=K402_PAYMENT_ID_RE)]

    @field_validator("tx_id")
    @classmethod
    def _tx(cls, v: str) -> str:
        return validate_tx_id(v)

    @classmethod
    def from_header(cls, value: str) -> Self:
        """Raises ValueError (pydantic ``ValidationError`` included) for anything but exactly three valid tokens."""
        if not value or len(value) > MAX_K402_HEADER_CHARS:
            raise ValueError(f"{HEADER_K402_PAYMENT} header missing or too large")
        parts = value.strip().split()
        if len(parts) != 3:
            raise ValueError(f"{HEADER_K402_PAYMENT} must be '<scheme> <txid> <payment_id>'")
        return cls.model_validate({"scheme": parts[0], "tx_id": parts[1], "payment_id": parts[2]})

    def to_header(self) -> str:
        return f"{self.scheme} {self.tx_id} {self.payment_id}"


class PrepaidOffer(StrictModel):
    """CONTRACT v2.11: how to pay this endpoint from a prepaid balance (deposit once on-chain, then signed debits).

    `amount` is the per-call debit in sompi (USD-priced endpoints: the current quote, without the 0.6 KAS on-chain
    floor). The deposit itself is an ordinary kaspa-exact-v1 split payment of at least `minDepositSompi`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scheme: Literal["kasgate-prepaid-v1"] = "kasgate-prepaid-v1"
    network: KaspaNetwork
    asset: Literal["KAS"] = "KAS"
    amount: SompiStr
    payTo: str
    merchantId: uuid.UUID
    prepaidOnly: bool
    minDepositSompi: SompiStr
    depositQuoteUrl: str
    depositUrl: str
    challengeUrl: str
    balanceUrl: str


# CONTRACT v2.10: a fresh-challenge 402 body lists the kaspa-exact-v1 terms, the k402 offer and the kaspa-x402
# `exact` (kaspa-exact-v2) entry; each client family skips the schemes it does not know.
PaymentOffer = Annotated[
    PaymentRequired | K402UtxoOffer | ExactV2Requirements | PrepaidOffer, Field(discriminator="scheme")
]


class X402ErrorBody(StrictModel):
    """Error body. On a 402 it is also a k402 offer body: ``k402`` is set and ``accepts`` lists every way to pay
    (the KasGate ``kaspa-exact-v1`` terms, identical to the ``PAYMENT-REQUIRED`` header, then the k402 ``kaspa-utxo``
    offer). k402 clients skip the entry whose scheme they do not know."""

    error: Literal[
        "payment_required",
        "invalid_payment_header",
        "payment_replayed",
        "payment_expired",
        "payment_not_found",
        "underpaid",
        "invalid_fee_split",
        "wrong_network",
        "tx_rejected",
        "rpc_unavailable",
        "service_unavailable",
        "rate_limited",
        "price_unavailable",
        "endpoint_not_found",
        "method_not_allowed",
        "upstream_error",
        "invalid_exact_payment",
        "insufficient_balance",
        "trial_exhausted",
        # CONTRACT v2.13 prepaid refunds
        "invalid_payer",
        "invalid_signature",
        "invalid_status",
        "challenge_expired",
        "challenge_replayed",
        "refund_too_small",
        "refund_already_open",
        "refund_not_found",
        "refund_not_cancellable",
        "refund_not_claimable",
        "refund_not_rejectable",
        "refund_closed",
        "invalid_payout",
        "payout_replayed",
    ]
    message: str
    k402: Literal["0.2"] | None = None
    accepts: list[PaymentOffer] | None = None


# ---------------------------------------------------------------- merchants


class MerchantCreate(StrictModel):
    """Registration requires a wallet signature over a challenge from POST /v1/auth/challenge?purpose=register."""

    kaspa_address: str
    display_name: str = Field(min_length=1, max_length=120)
    email: EmailStr | None = None
    challenge: str = Field(min_length=16, max_length=512)
    public_key: str = Field(pattern=r"^(?:[0-9a-f]{64}|0[23][0-9a-f]{64})$")
    signature: str = Field(pattern=r"^[0-9a-f]{128}$")

    @field_validator("public_key", mode="before")
    @classmethod
    def _lower_pub(cls, v: object) -> object:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("signature", mode="before")
    @classmethod
    def _sig(cls, v: object) -> object:
        return normalise_signature(v)

    @field_validator("kaspa_address")
    @classmethod
    def _addr(cls, v: str) -> str:
        return validate_kaspa_address(v)


class MerchantRead(ReadModel):
    id: uuid.UUID
    kaspa_address: str
    display_name: str
    email: str | None
    is_active: bool
    created_at: datetime
    prepaid_expiry_days: int | None = None
    """CONTRACT v2.13: default prepaid credit expiry in days after the latest deposit (None = never expires)."""


class MerchantUpdate(StrictModel):
    """PATCH /v1/merchants/me. Only fields present in the body change; `prepaid_expiry_days: null` = never expire."""

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    prepaid_expiry_days: PrepaidExpiryDays | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("send at least one field to change")
        if "display_name" in self.model_fields_set and self.display_name is None:
            raise ValueError("display_name cannot be null")
        return self


class MerchantCreated(BaseModel):
    merchant: MerchantRead
    api_key: str = Field(
        description="Long-lived automation key (kg_...). Shown once. 'Authorization: Bearer <api_key>'."
    )
    session_token: str = Field(
        description="Dashboard session (kgs_...) issued alongside, so the web cookie never holds the api_key."
    )
    expires_at: datetime


class SessionIssued(BaseModel):
    merchant: MerchantRead
    session_token: str = Field(description="Dashboard session (kgs_...), 7-day TTL. Does NOT rotate the api_key.")
    expires_at: datetime


class AuthChallengeRequest(StrictModel):
    kaspa_address: str
    purpose: Literal["login", "register"] = "login"

    @field_validator("kaspa_address")
    @classmethod
    def _addr(cls, v: str) -> str:
        return validate_kaspa_address(v)


class AuthChallenge(BaseModel):
    kaspa_address: str
    challenge: str = Field(description="Exact message the wallet must sign with sign_message.")
    expires_at: datetime


class AuthVerifyRequest(StrictModel):
    kaspa_address: str
    challenge: str = Field(min_length=16, max_length=512)
    public_key: str = Field(pattern=r"^(?:[0-9a-f]{64}|0[23][0-9a-f]{64})$")
    signature: str = Field(pattern=r"^[0-9a-f]{128}$")

    @field_validator("kaspa_address")
    @classmethod
    def _addr(cls, v: str) -> str:
        return validate_kaspa_address(v)

    @field_validator("public_key", mode="before")
    @classmethod
    def _lower(cls, v: object) -> object:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("signature", mode="before")
    @classmethod
    def _normalise_signature(cls, v: object) -> object:
        return normalise_signature(v)


# ---------------------------------------------------------------- endpoints


def _default_methods() -> list[HttpMethod]:
    return ["GET"]


class EndpointCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    slug: Annotated[str, Field(pattern=SLUG_RE)] | None = None
    description: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, max_length=60)
    upstream_url: HttpUrl
    upstream_headers: dict[str, str] = Field(default_factory=dict, max_length=20)
    price_currency: PriceCurrency = PriceCurrency.KAS
    price_sompi: Sompi | None = None
    price_usd_micros: UsdMicros | None = None
    allowed_methods: list[HttpMethod] = Field(default_factory=_default_methods, min_length=1, max_length=5)
    is_public_listed: bool = True
    prepaid_only: bool = False
    """CONTRACT v2.11: paid only from a prepaid balance; a KAS price may then be below the 0.6 KAS on-chain minimum."""
    free_trial_calls: FreeTrialCalls = 0
    """CONTRACT v2.12: unpaid calls each buyer key gets on this endpoint (0 = no free trial)."""
    trial_daily_cap: TrialDailyCap = TRIAL_DAILY_CAP_DEFAULT
    """CONTRACT v2.12: total free trial calls this endpoint serves per UTC day, across all buyers."""
    prepaid_expiry_days: PrepaidExpiryDays | None = None
    """CONTRACT v2.13: prepaid credit bought through this endpoint expires this many days after the latest deposit;
    None = use the merchant default (`MerchantRead.prepaid_expiry_days`)."""

    @field_validator("upstream_url")
    @classmethod
    def _url(cls, v: HttpUrl) -> HttpUrl:
        return _reject_private_host(v)

    @field_validator("allowed_methods")
    @classmethod
    def _methods(cls, v: list[str]) -> list[str]:
        return sorted(set(v))

    @model_validator(mode="after")
    def _price(self) -> Self:
        if self.price_currency is PriceCurrency.KAS:
            if self.price_sompi is None:
                raise ValueError("price_sompi is required when price_currency is KAS")
            if self.price_usd_micros is not None:
                raise ValueError("price_usd_micros must be omitted when price_currency is KAS")
            if not self.prepaid_only:
                check_on_chain_price(self.price_sompi)
        else:
            if self.price_usd_micros is None:
                raise ValueError("price_usd_micros is required when price_currency is USD")
            if self.price_sompi is not None:
                raise ValueError("price_sompi must be omitted when price_currency is USD")
        return self


class EndpointUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, max_length=60)
    upstream_url: HttpUrl | None = None
    upstream_headers: dict[str, str] | None = Field(default=None, max_length=20)
    price_currency: PriceCurrency | None = None
    price_sompi: Sompi | None = None
    price_usd_micros: UsdMicros | None = None
    allowed_methods: list[HttpMethod] | None = Field(default=None, min_length=1, max_length=5)
    is_active: bool | None = None
    is_public_listed: bool | None = None
    prepaid_only: bool | None = None
    free_trial_calls: FreeTrialCalls | None = None
    trial_daily_cap: TrialDailyCap | None = None
    prepaid_expiry_days: PrepaidExpiryDays | None = None
    """CONTRACT v2.13: send null to fall back to the merchant default."""

    @field_validator("upstream_url")
    @classmethod
    def _url(cls, v: HttpUrl | None) -> HttpUrl | None:
        return None if v is None else _reject_private_host(v)

    @field_validator("allowed_methods")
    @classmethod
    def _methods(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else sorted(set(v))

    @model_validator(mode="after")
    def _price(self) -> Self:
        if self.price_sompi is not None and self.price_usd_micros is not None:
            raise ValueError("set only one of price_sompi or price_usd_micros")
        if self.price_currency is PriceCurrency.KAS:
            if self.price_usd_micros is not None:
                raise ValueError("price_usd_micros must be omitted when price_currency is KAS")
        elif self.price_currency is PriceCurrency.USD and self.price_sompi is not None:
            raise ValueError("price_sompi must be omitted when price_currency is USD")
        # The route re-checks the merged row: a sub-minimum price is only valid on a prepaid-only endpoint.
        if self.price_sompi is not None and self.prepaid_only is not True:
            check_on_chain_price(self.price_sompi)
        return self


class EndpointRead(ReadModel):
    id: uuid.UUID
    merchant_id: uuid.UUID
    slug: str
    name: str
    description: str | None
    category: str | None
    upstream_url: str
    price_currency: PriceCurrency
    price_sompi: int | None
    price_usd_micros: int | None
    allowed_methods: list[str]
    is_active: bool
    is_public_listed: bool
    prepaid_only: bool = False
    free_trial_calls: int = 0
    trial_daily_cap: int = TRIAL_DAILY_CAP_DEFAULT
    prepaid_expiry_days: int | None = None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def price_kas(self) -> str | None:
        return None if self.price_sompi is None else sompi_to_kas(self.price_sompi)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def price_usd(self) -> str | None:
        return None if self.price_usd_micros is None else usd_micros_to_usd(self.price_usd_micros)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def proxy_url(self) -> str:
        return f"{get_settings().public_base_url}{PROXY_PATH_PREFIX}/{self.slug}"


# ---------------------------------------------------------------- OpenAPI import

OPENAPI_MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
OPENAPI_MAX_OPERATIONS = 500
OPENAPI_MAX_PATH_LENGTH = 512
OPENAPI_PREVIEW_TEXT_LENGTH = 200


class OpenApiImportSelection(StrictModel):
    """One operation to create, addressed by its stable key (method + path exactly as in the document).

    A row override is in the import's default_price_currency: price_sompi for KAS imports, price_usd_micros for
    USD imports. The request rejects an override in the other currency.
    """

    method: HttpMethod
    path: str = Field(min_length=1, max_length=OPENAPI_MAX_PATH_LENGTH)
    price_sompi: Sompi | None = Field(default=None, description="Per-operation override of default_price_sompi.")
    price_usd_micros: UsdMicros | None = Field(
        default=None, description="Per-operation override of default_price_usd_micros."
    )

    @field_validator("method", mode="before")
    @classmethod
    def _upper(cls, v: object) -> object:
        return v.strip().upper() if isinstance(v, str) else v

    @field_validator("price_sompi")
    @classmethod
    def _price(cls, v: int | None) -> int | None:
        if v is not None:
            compute_fee_split(v)
        return v


def _price_error(loc: tuple[str | int, ...], message: str) -> InitErrorDetails:
    return InitErrorDetails(type=PydanticCustomError("value_error", message), loc=loc, input=None)


class OpenApiImportRequest(StrictModel):
    """POST /v1/endpoints/import body. Exactly one of spec_text / spec_url.

    mode="preview" lists the operations and creates nothing; mode="create" creates one endpoint per selection
    in a single transaction (all-or-nothing). Prices follow EndpointCreate: default_price_currency KAS (the default)
    needs default_price_sompi and no USD fields; USD needs default_price_usd_micros and no sompi fields.
    """

    mode: Literal["preview", "create"]
    spec_text: str | None = Field(default=None, min_length=1, description="OpenAPI 3.0/3.1 document, JSON or YAML.")
    spec_url: HttpUrl | None = Field(
        default=None, description="Public http(s) URL the gateway fetches the document from."
    )
    upstream_base_url: HttpUrl
    default_price_currency: PriceCurrency = PriceCurrency.KAS
    default_price_sompi: Sompi | None = None
    default_price_usd_micros: UsdMicros | None = None
    upstream_headers: dict[str, str] = Field(default_factory=dict, max_length=20)
    category: str | None = Field(default=None, max_length=60)
    is_public_listed: bool = True
    free_trial_calls: FreeTrialCalls = 0
    """CONTRACT v2.12: free trial calls per buyer key on every imported endpoint (0 = none)."""
    selections: list[OpenApiImportSelection] | None = Field(
        default=None, min_length=1, max_length=OPENAPI_MAX_OPERATIONS
    )

    @field_validator("spec_url", "upstream_base_url")
    @classmethod
    def _url(cls, v: HttpUrl | None) -> HttpUrl | None:
        return None if v is None else _reject_private_host(v)

    @field_validator("default_price_sompi")
    @classmethod
    def _price(cls, v: int | None) -> int | None:
        if v is not None:
            compute_fee_split(v)
        return v

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if (self.spec_text is None) == (self.spec_url is None):
            raise ValueError("provide exactly one of spec_text or spec_url")
        if self.mode == "create" and not self.selections:
            raise ValueError("mode 'create' needs at least one selection")
        if self.mode == "preview" and self.selections is not None:
            raise ValueError("selections are only accepted with mode 'create'")
        if self.selections is not None:
            keys = [(item.method, item.path) for item in self.selections]
            if len(set(keys)) != len(keys):
                raise ValueError("each operation may be selected only once")
        self._check_prices()
        return self

    def _check_prices(self) -> None:
        errors: list[InitErrorDetails] = []
        if self.default_price_currency is PriceCurrency.KAS:
            if self.default_price_sompi is None:
                errors.append(
                    _price_error(("default_price_sompi",), "default_price_sompi is required when price currency is KAS")
                )
            if self.default_price_usd_micros is not None:
                errors.append(
                    _price_error(
                        ("default_price_usd_micros",),
                        "default_price_usd_micros must be omitted when default_price_currency is KAS",
                    )
                )
            wrong_field, wrong_message = "price_usd_micros", "price_usd_micros must be omitted in a KAS-priced import"
        else:
            if self.default_price_usd_micros is None:
                errors.append(
                    _price_error(
                        ("default_price_usd_micros",),
                        "default_price_usd_micros is required when default_price_currency is USD",
                    )
                )
            if self.default_price_sompi is not None:
                errors.append(
                    _price_error(
                        ("default_price_sompi",),
                        "default_price_sompi must be omitted when default_price_currency is USD",
                    )
                )
            wrong_field, wrong_message = "price_sompi", "price_sompi must be omitted in a USD-priced import"
        for index, item in enumerate(self.selections or []):
            if getattr(item, wrong_field) is not None:
                errors.append(_price_error(("selections", index, wrong_field), wrong_message))
        if errors:
            raise ValidationError.from_exception_data(type(self).__name__, errors)

    def default_price(self) -> tuple[PriceCurrency, int]:
        """(currency, amount) of the default: sompi for KAS, micro-dollars for USD."""
        if self.default_price_currency is PriceCurrency.USD:
            assert self.default_price_usd_micros is not None  # noqa: S101 - enforced by _check_prices
            return PriceCurrency.USD, self.default_price_usd_micros
        assert self.default_price_sompi is not None  # noqa: S101 - enforced by _check_prices
        return PriceCurrency.KAS, self.default_price_sompi

    def row_price(self, selection: OpenApiImportSelection) -> tuple[PriceCurrency, int]:
        """(currency, amount) for one selection: its override in the import currency, else the default."""
        currency, amount = self.default_price()
        override = selection.price_usd_micros if currency is PriceCurrency.USD else selection.price_sompi
        return currency, amount if override is None else override


class OpenApiOperationPreview(BaseModel):
    key: str = Field(description="Stable operation key: '<METHOD> <path>'.")
    method: str
    path: str
    operation_id: str | None
    summary: str | None
    path_params: list[str]
    importable: bool
    skip_reason: str | None
    slug: str | None = Field(description="Slug the endpoint would get if created now (deterministic).")
    upstream_url: str | None = Field(description="Upstream URL: base URL + the path up to the first {param} segment.")
    proxy_path: str | None = Field(description="Buyer route, e.g. /p/{slug}/{petId}; path params are the suffix.")
    proxy_url: str | None
    price_currency: PriceCurrency
    price_sompi: int | None = Field(description="Default KAS price; null when price_currency is USD.")
    price_usd_micros: int | None = Field(description="Default USD price in micro-dollars; null when KAS.")


class OpenApiImportPreview(BaseModel):
    mode: Literal["preview"] = "preview"
    openapi_version: str
    title: str | None
    api_version: str | None
    servers: list[str]
    operation_count: int
    importable_count: int
    default_price_currency: PriceCurrency
    default_price_sompi: int | None
    default_price_usd_micros: int | None
    min_price_sompi: int
    operations: list[OpenApiOperationPreview]


class OpenApiImportCreated(BaseModel):
    mode: Literal["create"] = "create"
    created_count: int
    created: list[EndpointRead]


# ---------------------------------------------------------------- analytics & tax


class SettlementRead(ReadModel):
    id: uuid.UUID
    tx_id: str
    endpoint_id: uuid.UUID
    merchant_id: uuid.UUID
    network: str
    payment_scheme: PaymentScheme
    payer_address: str | None
    merchant_address: str
    treasury_address: str | None
    """None on kaspa-exact-v2 settlements: that path has no treasury output (no platform fee during the beta)."""
    amount_sompi: int
    merchant_amount_sompi: int
    fee_amount_sompi: int
    kas_usd_rate: Decimal | None
    usd_value_at_settlement: Decimal | None
    status: SettlementStatus
    failure_reason: str | None
    request_method: str
    request_path: str
    upstream_status_code: int | None
    payment_nonce: str | None
    """Challenge nonce (kaspa-exact-v1, k402 internal); None on kaspa-exact-v2 settlements."""
    payment_identifier: str | None
    """kaspa-exact-v2 payment-identifier; None on kaspa-exact-v1 settlements."""
    delivery_attempts: int
    created_at: datetime
    settled_at: datetime | None


class DailyRevenue(BaseModel):
    day: date
    call_count: int
    gross_sompi: int
    merchant_sompi: int
    fee_sompi: int
    usd_value: Decimal


class EndpointRevenue(BaseModel):
    endpoint_id: uuid.UUID
    slug: str
    name: str
    call_count: int
    merchant_sompi: int
    usd_value: Decimal


class MerchantAnalytics(BaseModel):
    merchant_id: uuid.UUID
    network: KaspaNetwork
    period_start: datetime
    period_end: datetime
    total_calls: int
    gross_sompi: int
    merchant_sompi: int
    fee_sompi: int
    usd_value: Decimal
    daily: list[DailyRevenue]
    by_endpoint: list[EndpointRevenue]


TAX_CSV_COLUMNS: tuple[str, ...] = (
    "settled_at_utc",
    "tx_id",
    "network",
    "endpoint_slug",
    "payer_address",
    "gross_kas",
    "merchant_kas",
    "platform_fee_kas",
    "kas_usd_rate",
    "usd_value_at_settlement",
)


class TaxReportRow(BaseModel):
    settled_at_utc: datetime
    tx_id: str
    network: str
    endpoint_slug: str
    payer_address: str | None
    gross_kas: str
    merchant_kas: str
    platform_fee_kas: str
    kas_usd_rate: Decimal | None
    usd_value_at_settlement: Decimal | None

    def as_csv_row(self) -> list[str]:
        data = self.model_dump(mode="json")
        return ["" if data[c] is None else str(data[c]) for c in TAX_CSV_COLUMNS]


# ---------------------------------------------------------------- public directory


class DirectoryEntry(BaseModel):
    slug: str
    name: str
    description: str | None
    category: str | None
    merchant_display_name: str
    pay_to: str
    price_currency: PriceCurrency
    price_sompi: int | None
    price_kas: str | None
    price_usd_micros: int | None
    price_usd: str | None
    allowed_methods: list[str]
    proxy_url: str
    prepaid_only: bool = False
    """CONTRACT v2.11: payable only from a prepaid balance (kasgate-prepaid-v1); the price may be below the
    0.6 KAS on-chain minimum and the 402 carries no kaspa-exact-v1 terms."""
    free_trial_calls: int = 0
    """CONTRACT v2.12: free trial calls per buyer key (kasgate-trial-v1); 0 = none."""


class DirectoryManifest(BaseModel):
    manifest_version: Literal[1] = 1
    x402_version: Literal[2] = 2
    scheme: Literal["kaspa-exact-v1"] = "kaspa-exact-v1"
    asset: Literal["KAS"] = "KAS"
    network: KaspaNetwork
    gateway_base_url: str
    platform_fee_bps: int = PLATFORM_FEE_BPS
    min_output_sompi: int
    generated_at: datetime
    endpoints: list[DirectoryEntry]


class HealthStatus(BaseModel):
    status: Literal["ok", "degraded"]
    network: KaspaNetwork
    database: bool
    redis: bool
    kaspa_rpc: bool


# ---------------------------------------------------------------- public agent catalog (additive to /v1/directory)


class CatalogEndpoint(StrictModel):
    """Public, machine-readable listing of one merchant-listed endpoint.

    `input_schema` is reserved for a future merchant-supplied JSON Schema. The endpoint row has no such
    column today, so this field is always null. It is never populated from upstream headers or other secrets.

    The catalog does not live-convert USD prices. `price_sompi` / `price_kas` are set for KAS endpoints
    and null for USD endpoints; `price_usd_micros` / `price_usd` are the inverse. Buyers get the payable
    sompi amount from GET /v1/quote/{slug} or the 402 challenge.
    """

    slug: str
    name: str
    description: str | None
    methods: list[str]
    price_currency: PriceCurrency
    price_sompi: int | None
    price_kas: str | None
    price_usd_micros: int | None
    price_usd: str | None
    network: KaspaNetwork
    schemes: list[str]
    paid_url: str
    input_schema: dict[str, object] | None = None
    prepaid_only: bool = False
    free_trial_calls: int = 0


class CatalogPage(StrictModel):
    catalog_version: Literal[1] = 1
    x402_version: Literal[2] = 2
    scheme: Literal["kaspa-exact-v1"] = "kaspa-exact-v1"
    asset: Literal["KAS"] = "KAS"
    network: KaspaNetwork
    gateway_base_url: str
    items: list[CatalogEndpoint]
    next_cursor: str | None = None
    limit: int


# ---------------------------------------------------------------- prepaid credit (CONTRACT v2.11)

# A balance or running total in sompi as a decimal string; unlike SompiStr it may be "0".
BalanceSompiStr = Annotated[str, Field(pattern=r"^(?:0|[1-9][0-9]{0,17})$")]


class PrepaidDepositQuoteRequest(StrictModel):
    """Ask for a nonce-bound kaspa-exact-v1 challenge that funds a prepaid balance at the endpoint's merchant.

    `amount_sompi` is the deposit TOTAL (both outputs). It must pass `compute_fee_split` (>= 0.6 KAS); the credit
    posted to the balance is the merchant share (output 0), never the treasury fee (output 1).
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    slug: Annotated[str, Field(pattern=SLUG_RE)]
    payer_address: str
    amount_sompi: Sompi

    @field_validator("payer_address")
    @classmethod
    def _payer(cls, v: str) -> str:
        return validate_kaspa_address(v)

    @field_validator("amount_sompi")
    @classmethod
    def _amount(cls, v: int) -> int:
        check_on_chain_price(v)
        return v


class PrepaidDepositQuote(BaseModel):
    slug: str
    merchant_id: uuid.UUID
    payer_address: str
    deposit_sompi: SompiStr
    fee_sompi: SompiStr
    credit_sompi: SompiStr
    balance_sompi: BalanceSompiStr
    """The payer's balance at this merchant before this deposit."""
    required: PaymentRequired
    """Pay these terms (outputs 0 merchant, 1 treasury, payload kasgate:v1:<nonce>, payerProof) and POST the
    PAYMENT-SIGNATURE to deposit_url. Valid until required.expiresAt."""
    deposit_url: str


class PrepaidDepositReceipt(BaseModel):
    deposit_id: uuid.UUID
    tx_id: str
    slug: str
    merchant_id: uuid.UUID
    payer_address: str
    deposit_sompi: SompiStr
    fee_sompi: SompiStr
    credit_sompi: SompiStr
    balance_sompi: BalanceSompiStr
    created_at: datetime


class PrepaidChallengeRequest(StrictModel):
    """Ask for the debit challenge of ONE prepaid call. `path` is the exact proxy path to be called, including the
    query string if any (e.g. "/p/weather/today?city=hanoi"); `body_hash` is prepaid_body_hash(body)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    slug: Annotated[str, Field(pattern=SLUG_RE)]
    payer_address: str
    method: HttpMethod
    path: str = Field(min_length=3, max_length=PREPAID_PATH_MAX_LENGTH)
    body_hash: Annotated[str, Field(pattern=PREPAID_BODY_HASH_RE)] = PREPAID_EMPTY_BODY_HASH

    @field_validator("payer_address")
    @classmethod
    def _payer(cls, v: str) -> str:
        return validate_kaspa_address(v)

    @model_validator(mode="after")
    def _path(self) -> Self:
        prefix = f"{PROXY_PATH_PREFIX}/{self.slug}"
        rest = self.path[len(prefix) :]
        if not self.path.startswith(prefix) or (rest and rest[0] not in "/?"):
            raise ValueError(f"path must be {prefix} or start with {prefix}/ or {prefix}?")
        if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in self.path) or "#" in self.path:
            raise ValueError("path must be printable ASCII without spaces or a fragment (percent-encode it)")
        return self


class PrepaidChallenge(BaseModel):
    nonce: str
    message: str
    """Sign exactly this text with the depositing key (kaspa sign_message) and send
    X-KasGate-Prepaid: Base64(JSON{nonce, signature}) on the call."""
    slug: str
    merchant_id: uuid.UUID
    payer_address: str
    method: str
    path: str
    body_hash: str
    amount_sompi: SompiStr
    kas_usd_rate: str | None
    balance_sompi: BalanceSompiStr
    expires_at: datetime


class PrepaidDebitHeader(X402Header):
    """Body of the X-KasGate-Prepaid header."""

    nonce: Annotated[str, Field(pattern=NONCE_RE)]
    signature: Annotated[str, Field(pattern=r"^[0-9a-f]{128}$")]

    @field_validator("signature", mode="before")
    @classmethod
    def _signature(cls, v: object) -> object:
        return normalise_signature(v)

    @classmethod
    def from_header(cls, value: str) -> Self:
        if not value or len(value) > MAX_PREPAID_HEADER_CHARS:
            raise ValueError("header missing or too large")
        return super().from_header(value)


class PrepaidReceipt(X402Header):
    """Body of the X-KasGate-Prepaid-Receipt response header."""

    debitId: uuid.UUID
    nonce: str
    amountSompi: SompiStr
    balanceSompi: BalanceSompiStr
    status: Literal["debited", "refunded"]


class PrepaidBalanceRead(BaseModel):
    merchant_id: uuid.UUID
    payer_address: str
    balance_sompi: BalanceSompiStr
    deposited_sompi: BalanceSompiStr
    debited_sompi: BalanceSompiStr
    updated_at: datetime | None
    # CONTRACT v2.13
    refunded_sompi: BalanceSompiStr = "0"
    expired_sompi: BalanceSompiStr = "0"
    refund_hold_sompi: BalanceSompiStr = "0"
    """Set aside by an open refund request: still in the balance, but not spendable."""
    spendable_sompi: BalanceSompiStr = "0"
    """What calls can be paid from right now: balance - refund hold, or 0 once `expires_at` has passed."""
    expires_at: datetime | None = None
    credited_sompi: BalanceSompiStr = "0"
    """Merchant shares of failed paid calls credited back onto this balance (ledger kind failed_delivery_credit)."""
    # CONTRACT v2.14
    credit_balance_sompi: BalanceSompiStr = "0"
    """Unspent failed-delivery credit inside the balance: spent first by calls, never refundable on-chain."""
    refundable_sompi: BalanceSompiStr = "0"
    """What a refund request may take right now: spendable credit minus the unspent failed-delivery credit."""


class PrepaidDebitRead(ReadModel):
    id: uuid.UUID
    endpoint_id: uuid.UUID
    payer_address: str
    nonce: str
    amount_sompi: SompiStr
    kas_usd_rate: UsdDecimal | None
    usd_value: UsdDecimal | None
    status: Literal["debited", "refunded"]
    request_method: str
    request_path: str
    upstream_status_code: int | None
    created_at: datetime
    refunded_at: datetime | None

    @field_validator("amount_sompi", mode="before")
    @classmethod
    def _sompi(cls, v: object) -> object:
        return str(v) if isinstance(v, int) else v


class PrepaidDepositRead(ReadModel):
    id: uuid.UUID
    tx_id: str
    settlement_id: uuid.UUID
    payer_address: str
    deposit_sompi: SompiStr
    fee_sompi: SompiStr
    credit_sompi: SompiStr
    created_at: datetime

    @field_validator("deposit_sompi", "fee_sompi", "credit_sompi", mode="before")
    @classmethod
    def _sompi(cls, v: object) -> object:
        return str(v) if isinstance(v, int) else v


# ---------------------------------------------------------------- prepaid refunds and expiry (CONTRACT v2.13)


class PrepaidRefundChallengeRequest(StrictModel):
    """Ask for the text to sign to open (`request`) or cancel (`cancel`) a refund of unused prepaid credit, or to
    read this key's refunds including the merchant's reject reasons (`status`).

    `request`: `amount_sompi` is how much to get back (omit it for all spendable credit); `refund_id` must be absent.
    `cancel`: `refund_id` names the open request; `amount_sompi` must be absent.
    `status`: neither `amount_sompi` nor `refund_id`.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    slug: Annotated[str, Field(pattern=SLUG_RE)]
    payer_address: str
    action: PrepaidRefundAction = "request"
    amount_sompi: Sompi | None = None
    refund_id: Annotated[uuid.UUID, Field(strict=False)] | None = None

    @field_validator("payer_address")
    @classmethod
    def _payer(cls, v: str) -> str:
        return validate_kaspa_address(v)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.action == "request" and self.refund_id is not None:
            raise ValueError("refund_id is only used to cancel a refund")
        if self.action == "cancel":
            if self.refund_id is None:
                raise ValueError("refund_id is required to cancel a refund")
            if self.amount_sompi is not None:
                raise ValueError("amount_sompi must be omitted when cancelling a refund")
        if self.action == "status" and (self.amount_sompi is not None or self.refund_id is not None):
            raise ValueError("a status challenge takes neither amount_sompi nor refund_id")
        return self


class PrepaidRefundChallenge(BaseModel):
    nonce: str
    message: str
    """Sign exactly this text with the depositing key (kaspa sign_message) and POST {nonce, signature} to
    /v1/prepaid/refunds (request), /v1/prepaid/refunds/{refund_id}/cancel (cancel) or /v1/prepaid/refunds/status
    (status)."""
    action: PrepaidRefundAction
    slug: str
    merchant_id: uuid.UUID
    payer_address: str
    amount_sompi: BalanceSompiStr
    """The refund amount; "0" for a status challenge."""
    refund_id: uuid.UUID | None
    spendable_sompi: BalanceSompiStr
    expires_at: datetime
    refundable_sompi: BalanceSompiStr = "0"
    """CONTRACT v2.14: the most a refund may take (deposited, unspent credit; failed-delivery credit is excluded)."""


class PrepaidRefundSigned(StrictModel):
    """The buyer's signature over a refund challenge."""

    nonce: Annotated[str, Field(pattern=NONCE_RE)]
    signature: Annotated[str, Field(pattern=r"^[0-9a-f]{128}$")]

    @field_validator("signature", mode="before")
    @classmethod
    def _signature(cls, v: object) -> object:
        return normalise_signature(v)


class PrepaidRefundRead(ReadModel):
    id: uuid.UUID
    merchant_id: uuid.UUID
    payer_address: str
    amount_sompi: SompiStr
    status: PrepaidRefundStatus
    tx_id: str | None
    reason: str | None
    """The merchant's reject reason. Null in the public GET /refunds/status view; read it with a signed status."""
    last_error: str | None
    """The latest payout problem the gateway saw. Null in the public GET /refunds/status view."""
    created_at: datetime
    updated_at: datetime
    claimed_at: datetime | None
    payout_submitted_at: datetime | None
    paid_at: datetime | None
    resolved_at: datetime | None
    late_paid_from: Literal["cancelled", "rejected"] | None
    """Set when the payout was confirmed after the refund had been cancelled or rejected (it is still recorded)."""
    shortfall_sompi: BalanceSompiStr
    """For a late payout: the part of amount_sompi the balance no longer held (already spent or expired)."""

    @field_validator("amount_sompi", "shortfall_sompi", mode="before")
    @classmethod
    def _sompi(cls, v: object) -> object:
        return str(v) if isinstance(v, int) else v

    def public(self) -> "PrepaidRefundRead":
        """The unsigned public view: no merchant reason and no payout error text."""
        return self.model_copy(update={"reason": None, "last_error": None})

    @computed_field  # type: ignore[prop-decorator]
    @property
    def payout_address(self) -> str:
        """Pay exactly `amount_sompi` to this address (the depositing address). No platform fee applies."""
        return self.payer_address

    @computed_field  # type: ignore[prop-decorator]
    @property
    def payout_payload_hex(self) -> str:
        """Payload every payout transaction must carry (both modes), hex-encoded."""
        return refund_payout_payload(self.id).hex()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def payout_payload(self) -> str:
        """The same payload as text: pass it as KasWare `sendKaspa(..., {payload})`."""
        return refund_payout_payload(self.id).decode("ascii")


class PrepaidRefundPayoutRead(ReadModel):
    """One attempted payout transaction of a refund (every tx_id a merchant posted or the gateway broadcast)."""

    id: uuid.UUID
    refund_id: uuid.UUID
    tx_id: str
    method: Literal["tx_id", "transaction"]
    status: PrepaidRefundPayoutStatus
    payload_verified: bool
    check_count: int
    last_error: str | None
    next_check_at: datetime
    confirmed_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime


class PrepaidRefundPayout(StrictModel):
    """The merchant's payout of a refund: EITHER the `tx_id` of a payment already broadcast (e.g. KasWare
    `sendKaspa` with the refund payload), OR a signed `transaction` (kaspa Transaction JSON) for the gateway to
    broadcast. Both must carry the payload `kasgate:v1:refund:<refund id hex>`."""

    tx_id: str | None = None
    transaction: str | None = Field(default=None, min_length=2, max_length=MAX_RAW_TX_CHARS)

    @field_validator("tx_id")
    @classmethod
    def _tx(cls, v: str | None) -> str | None:
        return None if v is None else validate_tx_id(v)

    @model_validator(mode="after")
    def _one(self) -> Self:
        if (self.tx_id is None) == (self.transaction is None):
            raise ValueError("send exactly one of tx_id or transaction")
        return self


class PrepaidRefundReject(StrictModel):
    reason: str = Field(min_length=1, max_length=PREPAID_REFUND_REASON_MAX_LENGTH)


PrepaidLedgerKindName = Literal["refund", "expiry", "failed_delivery_credit"]
FailedDeliveryCreditReason = Literal["attempts_exhausted", "window_expired"]
DeliveryFailureName = Literal["upstream_5xx", "connect_error", "timeout", "oversized"]


class PrepaidLedgerEntryRead(ReadModel):
    id: uuid.UUID
    payer_address: str
    kind: PrepaidLedgerKindName
    direction: Literal["credit", "debit"]
    amount_sompi: BalanceSompiStr
    """Positive, except a failed-delivery credit fully clamped by the balance cap (0, see shortfall_sompi)."""
    balance_after_sompi: BalanceSompiStr
    tx_id: str | None
    refund_id: uuid.UUID | None
    note: str | None
    created_at: datetime
    # failed_delivery_credit rows only
    settlement_id: uuid.UUID | None = None
    slug: str | None = None
    credit_reason: FailedDeliveryCreditReason | None = None
    shortfall_sompi: BalanceSompiStr = "0"

    @field_validator("amount_sompi", "balance_after_sompi", "shortfall_sompi", mode="before")
    @classmethod
    def _sompi(cls, v: object) -> object:
        return str(v) if isinstance(v, int) else v


# ---------------------------------------------------------------- failed-delivery auto-credit


class FailedDeliveryCreditReceipt(X402Header):
    """Body of the X-KasGate-Credit response header: the paid call failed for good and its merchant share is now
    prepaid credit at this merchant (spend it with X-KasGate-Prepaid debits). The on-chain payment is not reversed."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    creditId: uuid.UUID
    kind: Literal["failed_delivery_credit"]
    txId: Annotated[str, Field(pattern=TX_ID_RE.pattern)]
    merchantId: uuid.UUID
    creditSompi: SompiStr
    balanceSompi: BalanceSompiStr
    reason: FailedDeliveryCreditReason
    shortfallSompi: BalanceSompiStr | None = None
    """Part of the merchant share not credited because the balance reached PREPAID_MAX_BALANCE_SOMPI."""


class BuyerCreditItem(BaseModel):
    """One failed-delivery credit in the buyer's credit summary (newest first)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    txId: Annotated[str, Field(pattern=TX_ID_RE.pattern)]
    slug: Annotated[str, Field(pattern=SLUG_RE)]
    creditSompi: BalanceSompiStr
    shortfallSompi: BalanceSompiStr
    reason: FailedDeliveryCreditReason
    createdAt: datetime


class BuyerCreditSummary(X402Header):
    """Body of the X-KasGate-Credit-Summary header on the signed POST /v1/prepaid/refunds/status reply (v2.14).

    The JSON body stays v2.13's list of refunds; this header adds the signing buyer's failed-delivery credit at
    that merchant. Credit is call-only: `refundableSompi` (deposited and unspent, minus the open hold) is what a
    refund may take; `creditBalanceSompi` can only pay for calls.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    merchantId: uuid.UUID
    creditedSompi: BalanceSompiStr
    """All failed-delivery credit ever added to this balance (the `credited_sompi` total)."""
    creditBalanceSompi: BalanceSompiStr
    """Unspent call-only credit (spent before deposited sompi; never refundable)."""
    refundableSompi: BalanceSompiStr
    creditCount: Annotated[int, Field(ge=0)]
    credits: Annotated[list[BuyerCreditItem], Field(max_length=MAX_SUMMARY_CREDITS)]
    """The newest credits, at most 10; the merchant lists all with GET /v1/prepaid/credits."""


class PrepaidCreditRead(BaseModel):
    """GET /v1/prepaid/credits: one `failed_delivery_credit` ledger entry joined with the paid call it credits."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: uuid.UUID
    kind: Literal["failed_delivery_credit"]
    tx_id: Annotated[str, Field(pattern=TX_ID_RE.pattern)]
    settlement_id: uuid.UUID
    endpoint_id: uuid.UUID
    slug: Annotated[str, Field(pattern=SLUG_RE)]
    payer_address: str
    payment_scheme: PaymentScheme
    amount_sompi: SompiStr
    """What the buyer paid on-chain for the call."""
    fee_sompi: BalanceSompiStr
    """Platform fee in output 1: already paid to the treasury and NOT reversed."""
    credit_sompi: BalanceSompiStr
    """The merchant share credited to the buyer's prepaid balance at this merchant (call-only, non-refundable)."""
    shortfall_sompi: BalanceSompiStr
    """The part of the merchant share not credited because the balance reached PREPAID_MAX_BALANCE_SOMPI."""
    balance_after_sompi: BalanceSompiStr
    reason: FailedDeliveryCreditReason
    delivery_failure: DeliveryFailureName
    upstream_status_code: Annotated[int, Field(ge=500)] | None
    delivery_attempts: Annotated[int, Field(ge=1)]
    settled_at: datetime | None
    created_at: datetime


class FailedDeliveryPolicyRead(BaseModel):
    """GET/PUT /v1/prepaid/failed-delivery-policy. The policy in force when a call SETTLES is stamped on it: while
    `enabled`, its last delivery failing for good (5xx, connection error, and a timeout if `credit_timeouts`) is
    credited to the buyer's prepaid balance. A later change never adds or drops credit for calls already paid.
    `enabled_at` / `disabled_at` are the times of the last switch on / off."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    enabled: bool
    credit_timeouts: bool
    enabled_at: datetime
    disabled_at: datetime | None


class FailedDeliveryPolicyUpdate(StrictModel):
    """Omitted fields keep their current value; at least one must be given."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)

    enabled: bool | None = None
    credit_timeouts: bool | None = None

    @model_validator(mode="after")
    def _something(self) -> Self:
        if self.enabled is None and self.credit_timeouts is None:
            raise ValueError("give enabled and/or credit_timeouts")
        return self


# ---------------------------------------------------------------- free trial calls (CONTRACT v2.12)


class TrialOffer(X402Header):
    """Body of the X-KasGate-Trial-Offer header on a 402 from an endpoint with free trial calls."""

    scheme: Literal["kasgate-trial-v1"] = "kasgate-trial-v1"
    freeTrialCalls: Annotated[int, Field(ge=1, le=TRIAL_MAX_CALLS)]
    challengeUrl: str
    statusUrl: str


class TrialChallengeRequest(PrepaidChallengeRequest):
    """Ask for the challenge of ONE free trial call: same fields and rules as a prepaid debit challenge. `path` is
    the exact proxy path to be called incl. the query string; `body_hash` is prepaid_body_hash(body)."""


class TrialChallenge(BaseModel):
    nonce: str
    message: str
    """Sign exactly this text with the key of payer_address (kaspa sign_message) and send
    X-KasGate-Trial: Base64(JSON{nonce, signature}) on the call."""
    slug: str
    merchant_id: uuid.UUID
    payer_address: str
    method: str
    path: str
    body_hash: str
    free_trial_calls: int
    calls_used: int
    remaining: int
    expires_at: datetime


class TrialCallHeader(PrepaidDebitHeader):
    """Body of the X-KasGate-Trial header: the nonce of a trial challenge and the buyer's signature over it."""


class TrialReceipt(X402Header):
    """Body of the X-KasGate-Trial-Receipt response header."""

    trialCallId: uuid.UUID
    nonce: str
    limit: int
    remaining: int
    status: Literal["used", "refunded"]


class TrialStatusRead(BaseModel):
    slug: str
    merchant_id: uuid.UUID
    payer_address: str
    free_trial_calls: int
    calls_used: int
    remaining: int
    """Trial calls this key can still use here (0 while the endpoint's daily trial cap is reached)."""


class TrialCallRead(ReadModel):
    id: uuid.UUID
    endpoint_id: uuid.UUID
    payer_address: str
    nonce: str
    status: Literal["used", "refunded"]
    request_method: str
    request_path: str
    upstream_status_code: int | None
    created_at: datetime
    refunded_at: datetime | None


# ---------------------------------------------------------------- merchant webhooks (CONTRACT v2.12)

WEBHOOK_URL_MAX_LENGTH = 2048
WEBHOOK_API_VERSION = "2.12"
WEBHOOK_SIGNATURE_HEADER = "X-KasGate-Signature"
WEBHOOK_TIMESTAMP_HEADER = "X-KasGate-Timestamp"
WEBHOOK_EVENT_ID_HEADER = "X-KasGate-Event-Id"
WEBHOOK_EVENT_TYPE_HEADER = "X-KasGate-Event-Type"
WEBHOOK_DELIVERY_ID_HEADER = "X-KasGate-Delivery-Id"
WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS = 300
WebhookEventType = Literal[
    "payment.settled",
    "prepaid.deposit",
    "prepaid.debit",
    "trial.used",
    "endpoint.created",
    "endpoint.updated",
    "prepaid.refund.requested",
    "prepaid.refund.paid",
    "prepaid.refund.rejected",
    "prepaid.refund.cancelled",
    "prepaid.refund.payout_reverted",
    "prepaid.credit.expired",
    "prepaid.credit.failed_delivery",
    "prepaid.credit.failed_delivery_shortfall",
]
WebhookDeliveryStatus = Literal["pending", "delivering", "succeeded", "dead"]
_LOCAL_HOST_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".localdomain")


def validate_webhook_url(value: str) -> str:
    """HTTPS only, no credentials or fragment, and not a local name or a non-public IP literal.

    Names are resolved again at delivery time by the SSRF guard (every resolved address must be public).
    """
    if len(value) > WEBHOOK_URL_MAX_LENGTH:
        raise ValueError(f"url must be at most {WEBHOOK_URL_MAX_LENGTH} characters")
    if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in value):
        raise ValueError("url must be printable ASCII without spaces")
    try:
        parsed = HttpUrl(value)
    except ValidationError as exc:
        raise ValueError("url is not a valid URL") from exc
    if parsed.scheme != "https":
        raise ValueError("webhook url must use https://")
    if parsed.username or parsed.password:
        raise ValueError("webhook url must not contain credentials")
    if parsed.fragment:
        raise ValueError("webhook url must not contain a fragment")
    host = (parsed.host or "").strip("[]").lower().rstrip(".")
    if not host or host in {"localhost", "localhost.localdomain"} or host.endswith(_LOCAL_HOST_SUFFIXES):
        raise ValueError("webhook url may not target a local host")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            raise ValueError("webhook url host must be a public domain name") from None
        return str(parsed)
    if not ip.is_global or ip.is_multicast:
        raise ValueError("webhook url may not target a private, loopback, link-local or reserved IP")
    return str(parsed)


def _event_types(value: list[str]) -> list[str]:
    return sorted(set(value))


class WebhookCreate(StrictModel):
    url: str = Field(min_length=12, max_length=WEBHOOK_URL_MAX_LENGTH)
    event_types: list[WebhookEventType] = Field(min_length=1, max_length=len(WEBHOOK_EVENT_TYPES))
    description: str | None = Field(default=None, max_length=200)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return validate_webhook_url(v)

    @field_validator("event_types")
    @classmethod
    def _types(cls, v: list[str]) -> list[str]:
        return _event_types(v)


class WebhookUpdate(StrictModel):
    url: str | None = Field(default=None, min_length=12, max_length=WEBHOOK_URL_MAX_LENGTH)
    event_types: list[WebhookEventType] | None = Field(default=None, min_length=1, max_length=len(WEBHOOK_EVENT_TYPES))
    description: str | None = Field(default=None, max_length=200)
    is_active: bool | None = None

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return None if v is None else validate_webhook_url(v)

    @field_validator("event_types")
    @classmethod
    def _types(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _event_types(v)


class WebhookRead(ReadModel):
    id: uuid.UUID
    url: str
    description: str | None
    event_types: list[str]
    secret_prefix: str
    """The first characters of the signing secret, so a merchant can tell secrets apart. Never the secret."""
    is_active: bool
    created_at: datetime
    updated_at: datetime


class WebhookCreated(BaseModel):
    webhook: WebhookRead
    secret: str = Field(description="HMAC-SHA256 signing secret (whsec_...). Shown once; store it now.")


class WebhookDeliveryRead(BaseModel):
    id: uuid.UUID
    webhook_id: uuid.UUID
    event_id: str
    event_type: str
    status: WebhookDeliveryStatus
    attempts: int
    next_attempt_at: datetime | None
    last_attempt_at: datetime | None
    last_status_code: int | None
    last_error: str | None
    delivered_at: datetime | None
    created_at: datetime


class WebhookEventTypes(BaseModel):
    event_types: list[str]
    api_version: str = WEBHOOK_API_VERSION
    signature_header: str = WEBHOOK_SIGNATURE_HEADER
    timestamp_header: str = WEBHOOK_TIMESTAMP_HEADER
    timestamp_tolerance_seconds: int = WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS
