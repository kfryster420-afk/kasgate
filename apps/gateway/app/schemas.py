import base64
import binascii
import ipaddress
import re
import secrets
import uuid
from datetime import date, datetime
from decimal import ROUND_CEILING, Decimal
from typing import Annotated, Literal, Self

from kaspa import Address
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
    NETWORK_ADDRESS_PREFIX,
    PLATFORM_FEE_BPS,
    SOMPI_PER_KAS,
    KaspaNetwork,
    get_settings,
)
from app.models import PriceCurrency, SettlementStatus

HEADER_PAYMENT_REQUIRED = "PAYMENT-REQUIRED"
HEADER_PAYMENT_SIGNATURE = "PAYMENT-SIGNATURE"
HEADER_PAYMENT_RESPONSE = "PAYMENT-RESPONSE"

PROXY_PATH_PREFIX = "/p"
MAX_HEADER_BYTES = 40_960
MAX_RAW_TX_CHARS = 24_000
PAYMENT_PAYLOAD_PREFIX = "kasgate:v1:"
NONCE_RE = r"^[0-9a-f]{32}$"
AUTH_PURPOSE = "Sign in to the KasGate merchant dashboard. This does not authorise any payment."
REGISTER_PURPOSE = "Register this address as a KasGate merchant. This does not authorise any payment."

KASPA_ADDRESS_RE = re.compile(r"^(kaspa|kaspatest):[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{61,63}$")
TX_ID_RE = re.compile(r"^[0-9a-f]{64}$")
SLUG_RE = r"^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])$"
SOMPI_STR_RE = r"^[1-9][0-9]{0,17}$"

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
MAX_SOMPI = 999_999_999_999_999_999  # 18 digits, same cap as SompiStr
USD_MICROS_PER_DOLLAR = 1_000_000
MAX_USD_MICROS = 1_000_000_000_000_000  # $1e9
Sompi = Annotated[int, Field(gt=0, le=MAX_SOMPI)]
SompiStr = Annotated[str, Field(pattern=SOMPI_STR_RE)]
UsdMicros = Annotated[int, Field(gt=0, le=MAX_USD_MICROS)]
UsdDecimal = Annotated[Decimal, Field(ge=0, max_digits=20, decimal_places=8)]


def sompi_to_kas(sompi: int) -> str:
    return format(Decimal(sompi) / Decimal(SOMPI_PER_KAS), "f")


def validate_kaspa_address(value: str, network: str | None = None) -> str:
    value = value.strip().lower()
    if not KASPA_ADDRESS_RE.fullmatch(value):
        raise ValueError("invalid Kaspa address")
    if not Address.validate(value):
        raise ValueError("invalid Kaspa address checksum")
    target = network or get_settings().kaspa_network
    prefix = NETWORK_ADDRESS_PREFIX[target]
    if not value.startswith(f"{prefix}:"):
        raise ValueError(f"address must use the '{prefix}:' prefix on {target}")
    return value


def validate_tx_id(value: str) -> str:
    value = value.strip().lower()
    if not TX_ID_RE.fullmatch(value):
        raise ValueError("txId must be 64 lowercase hex characters")
    return value


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


class X402ErrorBody(StrictModel):
    error: Literal[
        "payment_required",
        "invalid_payment_header",
        "payment_replayed",
        "payment_expired",
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
    ]
    message: str
    accepts: PaymentRequired | None = None


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


# ---------------------------------------------------------------- analytics & tax


class SettlementRead(ReadModel):
    id: uuid.UUID
    tx_id: str
    endpoint_id: uuid.UUID
    merchant_id: uuid.UUID
    network: str
    payer_address: str
    merchant_address: str
    treasury_address: str
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
    payment_nonce: str
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
    payer_address: str
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
