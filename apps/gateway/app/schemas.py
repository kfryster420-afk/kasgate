import base64
import binascii
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
    computed_field,
    field_validator,
    model_validator,
)

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
from app.models import PriceCurrency, SettlementStatus
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

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
USD_MICROS_PER_DOLLAR = 1_000_000
MAX_USD_MICROS = 1_000_000_000_000_000  # $1e9
UsdMicros = Annotated[int, Field(gt=0, le=MAX_USD_MICROS)]
UsdDecimal = Annotated[Decimal, Field(ge=0, max_digits=20, decimal_places=8)]


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


# CONTRACT v2.10: a fresh-challenge 402 body lists the kaspa-exact-v1 terms, the k402 offer and the kaspa-x402
# `exact` (kaspa-exact-v2) entry; each client family skips the schemes it does not know.
PaymentOffer = Annotated[PaymentRequired | K402UtxoOffer | ExactV2Requirements, Field(discriminator="scheme")]


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
            compute_fee_split(self.price_sompi)
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
        if self.price_sompi is not None:
            compute_fee_split(self.price_sompi)
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
    """One operation to create, addressed by its stable key (method + path exactly as in the document)."""

    method: HttpMethod
    path: str = Field(min_length=1, max_length=OPENAPI_MAX_PATH_LENGTH)
    price_sompi: Sompi | None = Field(default=None, description="Per-operation override of default_price_sompi.")

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


class OpenApiImportRequest(StrictModel):
    """POST /v1/endpoints/import body. Exactly one of spec_text / spec_url.

    mode="preview" lists the operations and creates nothing; mode="create" creates one endpoint per selection
    in a single transaction (all-or-nothing).
    """

    mode: Literal["preview", "create"]
    spec_text: str | None = Field(default=None, min_length=1, description="OpenAPI 3.0/3.1 document, JSON or YAML.")
    spec_url: HttpUrl | None = Field(
        default=None, description="Public http(s) URL the gateway fetches the document from."
    )
    upstream_base_url: HttpUrl
    default_price_sompi: Sompi
    upstream_headers: dict[str, str] = Field(default_factory=dict, max_length=20)
    category: str | None = Field(default=None, max_length=60)
    is_public_listed: bool = True
    selections: list[OpenApiImportSelection] | None = Field(
        default=None, min_length=1, max_length=OPENAPI_MAX_OPERATIONS
    )

    @field_validator("spec_url", "upstream_base_url")
    @classmethod
    def _url(cls, v: HttpUrl | None) -> HttpUrl | None:
        return None if v is None else _reject_private_host(v)

    @field_validator("default_price_sompi")
    @classmethod
    def _price(cls, v: int) -> int:
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
        return self


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
    price_sompi: int


class OpenApiImportPreview(BaseModel):
    mode: Literal["preview"] = "preview"
    openapi_version: str
    title: str | None
    api_version: str | None
    servers: list[str]
    operation_count: int
    importable_count: int
    default_price_sompi: int
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
