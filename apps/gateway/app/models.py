import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(AsyncAttrs, DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


PAYMENT_SCHEME_KASPA_EXACT_V1 = "kaspa-exact-v1"
PAYMENT_SCHEME_K402 = "k402-kaspa-utxo"
# x402 scheme `exact` + extra.binding `kaspa-exact-v2` (community kaspa-x402, standard-native profile).
PAYMENT_SCHEME_KASPA_EXACT_V2 = "kaspa-exact-v2"
PAYMENT_SCHEME_KNOWN_SQL = "payment_scheme IN ('kaspa-exact-v1', 'k402-kaspa-utxo', 'kaspa-exact-v2')"
EXACT_V2_IDENTIFIERS_SQL = (
    "(payment_scheme = 'kaspa-exact-v2') = (payment_identifier IS NOT NULL) "
    "AND (payment_scheme = 'kaspa-exact-v2') = (request_hash IS NOT NULL)"
)
TREASURY_SPLIT_SQL = (
    "payment_scheme = 'kaspa-exact-v2' OR "
    "(fee_amount_sompi > 0 AND treasury_address IS NOT NULL AND payment_nonce IS NOT NULL)"
)
EXACT_V2_SHAPE_SQL = (
    "payment_scheme <> 'kaspa-exact-v2' OR (fee_amount_sompi = 0 AND treasury_address IS NULL "
    "AND payment_nonce IS NULL AND k402_payment_id IS NULL AND payer_address IS NOT NULL)"
)


class SettlementStatus(enum.StrEnum):
    PENDING = "pending"
    SETTLED = "settled"
    REJECTED = "rejected"


class PriceCurrency(enum.StrEnum):
    KAS = "KAS"
    USD = "USD"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Merchant(TimestampMixin, Base):
    __tablename__ = "merchants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kaspa_address: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    api_key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    endpoints: Mapped[list["Endpoint"]] = relationship(back_populates="merchant", cascade="all, delete-orphan")
    settlements: Mapped[list["Settlement"]] = relationship(back_populates="merchant")

    __table_args__ = (
        Index("ix_merchants_kaspa_address", "kaspa_address", unique=True),
        Index("ix_merchants_email", "email", unique=True, postgresql_where="email IS NOT NULL"),
        Index("ix_merchants_api_key_hash", "api_key_hash", unique=True),
    )


class Endpoint(TimestampMixin, Base):
    __tablename__ = "endpoints"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    category: Mapped[str | None] = mapped_column(String(60), nullable=True)
    upstream_url: Mapped[str] = mapped_column(Text, nullable=False)
    upstream_headers_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    price_currency: Mapped[PriceCurrency] = mapped_column(
        Enum(
            PriceCurrency,
            name="price_currency",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=PriceCurrency.KAS,
        server_default=PriceCurrency.KAS.value,
    )
    price_sompi: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    price_usd_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    allowed_methods: Mapped[list[str]] = mapped_column(
        ARRAY(String(10)), nullable=False, default=lambda: ["GET"], server_default="{GET}"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    is_public_listed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    # CONTRACT v2.11: paid only from a prepaid balance. Such an endpoint may be priced below the on-chain minimum
    # (0.6 KAS) and its 402 offers no on-chain terms. Every endpoint also accepts prepaid debits.
    prepaid_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    merchant: Mapped[Merchant] = relationship(back_populates="endpoints")
    settlements: Mapped[list["Settlement"]] = relationship(back_populates="endpoint")

    __table_args__ = (
        Index("ix_endpoints_slug", "slug", unique=True),
        Index("ix_endpoints_merchant_id", "merchant_id"),
        Index("ix_endpoints_directory", "is_public_listed", "is_active"),
        CheckConstraint(
            "(price_currency = 'KAS' AND price_sompi > 0 AND price_usd_micros IS NULL) OR "
            "(price_currency = 'USD' AND price_usd_micros > 0 AND price_sompi IS NULL)",
            name="price_matches_currency",
        ),
        CheckConstraint("cardinality(allowed_methods) > 0", name="methods_not_empty"),
    )


class Settlement(Base):
    __tablename__ = "settlements"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tx_id: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("endpoints.id", ondelete="RESTRICT"), nullable=False
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    network: Mapped[str] = mapped_column(String(20), nullable=False)
    # "kaspa-exact-v1" (PAYMENT-SIGNATURE, KasGate terms), "k402-kaspa-utxo" (X-K402-Payment) - both carry the
    # treasury output - or "kaspa-exact-v2" (PAYMENT-SIGNATURE, community kaspa-x402 x402 scheme `exact` with
    # extra.binding `kaspa-exact-v2`, CONTRACT v2.10): no treasury output, no platform fee during the beta.
    payment_scheme: Mapped[str] = mapped_column(
        String(20), nullable=False, default="kaspa-exact-v1", server_default="kaspa-exact-v1"
    )
    # NULL only for k402: an unmodified k402 client proves no payer address (no payerProof).
    payer_address: Mapped[str | None] = mapped_column(String(100), nullable=True)
    merchant_address: Mapped[str] = mapped_column(String(100), nullable=False)
    treasury_address: Mapped[str | None] = mapped_column(String(100), nullable=True)
    amount_sompi: Mapped[int] = mapped_column(BigInteger, nullable=False)
    merchant_amount_sompi: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fee_amount_sompi: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kas_usd_rate: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    usd_value_at_settlement: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    status: Mapped[SettlementStatus] = mapped_column(
        Enum(
            SettlementStatus,
            name="settlement_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=SettlementStatus.PENDING,
        server_default=SettlementStatus.PENDING.value,
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_method: Mapped[str] = mapped_column(String(10), nullable=False)
    request_path: Mapped[str] = mapped_column(Text, nullable=False)
    upstream_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_nonce: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # kaspa-exact-v2 only: the x402 payment-identifier and the gateway's request fingerprint (requestHash).
    payment_identifier: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    k402_payment_id: Mapped[str | None] = mapped_column(String(18), nullable=True)
    delivery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    endpoint: Mapped[Endpoint] = relationship(back_populates="settlements")
    merchant: Mapped[Merchant] = relationship(back_populates="settlements")

    __table_args__ = (
        Index("ix_settlements_tx_id", "tx_id", unique=True),
        Index("ix_settlements_payment_nonce", "payment_nonce", unique=True),
        Index("ix_settlements_merchant_created", "merchant_id", "created_at"),
        Index("ix_settlements_endpoint_created", "endpoint_id", "created_at"),
        Index("ix_settlements_payer_address", "payer_address"),
        Index("ix_settlements_k402_payment_id", "k402_payment_id", unique=True),
        Index("ix_settlements_payment_identifier", "payment_identifier", unique=True),
        CheckConstraint("amount_sompi > 0", name="amount_positive"),
        CheckConstraint("merchant_amount_sompi > 0 AND fee_amount_sompi >= 0", name="outputs_positive"),
        CheckConstraint("merchant_amount_sompi + fee_amount_sompi = amount_sompi", name="split_sums_to_total"),
        CheckConstraint("tx_id ~ '^[0-9a-f]{64}$'", name="tx_id_hex64"),
        CheckConstraint("payment_nonce ~ '^[0-9a-f]{32}$'", name="payment_nonce_hex32"),
        CheckConstraint("delivery_attempts >= 1", name="delivery_attempts_positive"),
        CheckConstraint(PAYMENT_SCHEME_KNOWN_SQL, name="payment_scheme_known"),
        CheckConstraint(EXACT_V2_IDENTIFIERS_SQL, name="exact_v2_identifiers"),
        CheckConstraint(TREASURY_SPLIT_SQL, name="treasury_split_unless_exact_v2"),
        CheckConstraint(EXACT_V2_SHAPE_SQL, name="exact_v2_shape"),
        CheckConstraint("payment_identifier ~ '^[A-Za-z0-9_-]{16,128}$'", name="payment_identifier_format"),
        CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="request_hash_hex64"),
        CheckConstraint(
            "(payment_scheme = 'k402-kaspa-utxo') = (k402_payment_id IS NOT NULL)", name="k402_payment_id_iff_k402"
        ),
        CheckConstraint("k402_payment_id ~ '^p_[0-9a-f]{16}$'", name="k402_payment_id_format"),
        CheckConstraint(
            "payment_scheme = 'k402-kaspa-utxo' OR payer_address IS NOT NULL", name="payer_required_for_v1"
        ),
    )


# ---------------------------------------------------------------- prepaid credit (CONTRACT v2.11)

PREPAID_DEBIT_DEBITED = "debited"
PREPAID_DEBIT_REFUNDED = "refunded"


class PrepaidBalance(TimestampMixin, Base):
    """One running prepaid credit balance per (buyer P2PK key, merchant).

    KasGate holds no funds: every deposit already paid the merchant (output 0) and the treasury (output 1) on-chain.
    The balance is an off-chain record of calls the merchant has been prepaid for. `balance_sompi` can never go
    negative and always equals `deposited_sompi - debited_sompi` (refunded debits are subtracted from `debited_sompi`).
    """

    __tablename__ = "prepaid_balances"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    payer_address: Mapped[str] = mapped_column(String(100), nullable=False)
    # x-only Schnorr key of the P2PK address that made the deposits; the only key that can authorise debits.
    payer_public_key: Mapped[str] = mapped_column(String(64), nullable=False)
    balance_sompi: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    deposited_sompi: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    debited_sompi: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")

    __table_args__ = (
        Index("ix_prepaid_balances_merchant_id_payer_public_key", "merchant_id", "payer_public_key", unique=True),
        Index("ix_prepaid_balances_payer_address", "payer_address"),
        CheckConstraint("balance_sompi >= 0", name="balance_not_negative"),
        CheckConstraint("deposited_sompi >= 0 AND debited_sompi >= 0", name="totals_not_negative"),
        CheckConstraint("balance_sompi = deposited_sompi - debited_sompi", name="balance_matches_totals"),
        CheckConstraint("payer_public_key ~ '^[0-9a-f]{64}$'", name="payer_public_key_hex64"),
    )


class PrepaidDeposit(Base):
    """A prepaid deposit: the on-chain kaspa-exact-v1 split payment (its `settlements` row) that funded a balance."""

    __tablename__ = "prepaid_deposits"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    balance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prepaid_balances.id", ondelete="RESTRICT"), nullable=False
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    settlement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("settlements.id", ondelete="RESTRICT"), nullable=False
    )
    tx_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payer_address: Mapped[str] = mapped_column(String(100), nullable=False)
    deposit_sompi: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fee_sompi: Mapped[int] = mapped_column(BigInteger, nullable=False)
    credit_sompi: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_prepaid_deposits_tx_id", "tx_id", unique=True),
        Index("ix_prepaid_deposits_settlement_id", "settlement_id", unique=True),
        Index("ix_prepaid_deposits_balance_id", "balance_id"),
        Index("ix_prepaid_deposits_merchant_created", "merchant_id", "created_at"),
        CheckConstraint("tx_id ~ '^[0-9a-f]{64}$'", name="tx_id_hex64"),
        CheckConstraint("credit_sompi > 0 AND fee_sompi > 0", name="amounts_positive"),
        CheckConstraint("credit_sompi + fee_sompi = deposit_sompi", name="split_sums_to_deposit"),
    )


class PrepaidDebit(Base):
    """One off-chain prepaid call: a signed, single-use debit of a balance. Not revenue: the deposit was."""

    __tablename__ = "prepaid_debits"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    balance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prepaid_balances.id", ondelete="RESTRICT"), nullable=False
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("endpoints.id", ondelete="RESTRICT"), nullable=False
    )
    nonce: Mapped[str] = mapped_column(String(32), nullable=False)
    payer_address: Mapped[str] = mapped_column(String(100), nullable=False)
    amount_sompi: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kas_usd_rate: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    usd_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default=PREPAID_DEBIT_DEBITED, server_default=PREPAID_DEBIT_DEBITED
    )
    request_method: Mapped[str] = mapped_column(String(10), nullable=False)
    request_path: Mapped[str] = mapped_column(Text, nullable=False)
    body_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    upstream_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_prepaid_debits_nonce", "nonce", unique=True),
        Index("ix_prepaid_debits_balance_id", "balance_id"),
        Index("ix_prepaid_debits_merchant_created", "merchant_id", "created_at"),
        Index("ix_prepaid_debits_endpoint_created", "endpoint_id", "created_at"),
        CheckConstraint("amount_sompi > 0", name="amount_positive"),
        CheckConstraint("nonce ~ '^[0-9a-f]{32}$'", name="nonce_hex32"),
        CheckConstraint("body_hash ~ '^[0-9a-f]{64}$'", name="body_hash_hex64"),
        CheckConstraint("status IN ('debited', 'refunded')", name="status_known"),
        CheckConstraint("(status = 'refunded') = (refunded_at IS NOT NULL)", name="refunded_at_iff_refunded"),
    )

