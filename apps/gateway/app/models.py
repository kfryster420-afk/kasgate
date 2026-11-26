import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
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
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
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


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


# Failed-delivery auto-credit (CONTRACT v2.14). A /p settlement records how its LAST delivery ended:
# `pending` is written with the settlement (and again by every redelivery claim) BEFORE the upstream is called,
# `delivered` / `failed` after it answered. `credited` is terminal: the failure became prepaid credit. NULL = no
# delivery outcome (prepaid deposits, rows from before 0009): never credited.
DELIVERY_PENDING = "pending"
DELIVERY_DELIVERED = "delivered"
DELIVERY_FAILED = "failed"
DELIVERY_CREDITED = "credited"
DELIVERY_OUTCOMES: tuple[str, ...] = (DELIVERY_PENDING, DELIVERY_DELIVERED, DELIVERY_FAILED, DELIVERY_CREDITED)
# Why a delivery failed. Only `upstream_5xx` and `connect_error` are credited by default; `timeout` unless the
# merchant excluded timeouts; `oversized` (the upstream answered, the gateway refused to relay it) never.
FAILURE_UPSTREAM_5XX = "upstream_5xx"
FAILURE_CONNECT_ERROR = "connect_error"
FAILURE_TIMEOUT = "timeout"
FAILURE_OVERSIZED = "oversized"
DELIVERY_FAILURES: tuple[str, ...] = (FAILURE_UPSTREAM_5XX, FAILURE_CONNECT_ERROR, FAILURE_TIMEOUT, FAILURE_OVERSIZED)
# The merchant's failed-delivery credit policy, stamped on the settlement when it settles (NULL = not covered).
CREDIT_POLICY_ERRORS = "errors"
CREDIT_POLICY_ERRORS_AND_TIMEOUTS = "errors_and_timeouts"
CREDIT_POLICIES: tuple[str, ...] = (CREDIT_POLICY_ERRORS, CREDIT_POLICY_ERRORS_AND_TIMEOUTS)
DELIVERY_FAILURE_IFF_FAILED_SQL = "(delivery_failure IS NOT NULL) = (delivery_outcome IN ('failed', 'credited'))"
UNDELIVERED_SETTLEMENT_SQL = (
    "status = 'settled' AND delivery_outcome = 'failed' AND failed_delivery_credit_policy IS NOT NULL"
)
STALE_PENDING_SETTLEMENT_SQL = "status = 'settled' AND delivery_outcome = 'pending' AND pending_alerted_at IS NULL"
EXACT_V2_SHAPE_SQL = (
    "payment_scheme <> 'kaspa-exact-v2' OR (fee_amount_sompi = 0 AND treasury_address IS NULL "
    "AND payment_nonce IS NULL AND k402_payment_id IS NULL AND payer_address IS NOT NULL)"
)


# CONTRACT v2.12 free trial limits (also enforced by Pydantic and by DB CHECKs).
TRIAL_MAX_CALLS = 100
TRIAL_DAILY_CAP_DEFAULT = 100
TRIAL_DAILY_CAP_MAX = 100_000

# CONTRACT v2.13 prepaid credit expiry: days after the latest deposit (None = credit never expires).
PREPAID_EXPIRY_DAYS_MAX = 3650


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
    # CONTRACT v2.13: default prepaid credit expiry for this merchant's buyers (None = never expires).
    prepaid_expiry_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Failed-delivery auto-credit policy (opt-out, CONTRACT v2.14). On while `failed_delivery_credit_disabled_at` is
    # NULL or older than `failed_delivery_credit_enabled_at` (the times of the last switch). The policy in force is
    # stamped on each settlement when it settles (`settlements.failed_delivery_credit_policy`), so a later switch
    # never adds or drops credit for calls already paid.
    failed_delivery_credit_enabled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    failed_delivery_credit_disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # False = upstream timeouts are not credited (5xx answers and connection errors still are).
    failed_delivery_credit_timeouts: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    endpoints: Mapped[list["Endpoint"]] = relationship(back_populates="merchant", cascade="all, delete-orphan")
    settlements: Mapped[list["Settlement"]] = relationship(back_populates="merchant")

    __table_args__ = (
        Index("ix_merchants_kaspa_address", "kaspa_address", unique=True),
        Index("ix_merchants_email", "email", unique=True, postgresql_where="email IS NOT NULL"),
        Index("ix_merchants_api_key_hash", "api_key_hash", unique=True),
        CheckConstraint(
            f"prepaid_expiry_days BETWEEN 1 AND {PREPAID_EXPIRY_DAYS_MAX}", name="prepaid_expiry_days_range"
        ),
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
    # CONTRACT v2.12: unpaid calls each buyer key gets (0 = no trial) and the endpoint's total trial calls per UTC day.
    free_trial_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    trial_daily_cap: Mapped[int] = mapped_column(
        Integer, nullable=False, default=TRIAL_DAILY_CAP_DEFAULT, server_default=str(TRIAL_DAILY_CAP_DEFAULT)
    )
    # CONTRACT v2.13: prepaid credit expiry for deposits made through this endpoint; None = the merchant default.
    prepaid_expiry_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

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
        CheckConstraint(f"free_trial_calls BETWEEN 0 AND {TRIAL_MAX_CALLS}", name="free_trial_calls_range"),
        CheckConstraint(f"trial_daily_cap BETWEEN 1 AND {TRIAL_DAILY_CAP_MAX}", name="trial_daily_cap_range"),
        CheckConstraint(
            f"prepaid_expiry_days BETWEEN 1 AND {PREPAID_EXPIRY_DAYS_MAX}", name="prepaid_expiry_days_range"
        ),
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
    # CONTRACT v2.14: outcome of the last delivery (see DELIVERY_OUTCOMES), why it failed, the merchant's
    # failed-delivery credit policy at settle time (NULL = this payment is never credited), and when a still-
    # `pending` row was first alerted after the redelivery window plus grace.
    delivery_outcome: Mapped[str | None] = mapped_column(String(10), nullable=True)
    delivery_failure: Mapped[str | None] = mapped_column(String(16), nullable=True)
    failed_delivery_credit_policy: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Set once when the ledger sweep finds this row still `pending` after the redelivery window plus grace
    # (a crash or a failed outcome write). The settlement is not credited; operators alert on the ERROR log.
    pending_alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
        # Failed-delivery auto-credit sweeper: covered settlements whose last delivery failed.
        Index("ix_settlements_undelivered", "settled_at", postgresql_where=UNDELIVERED_SETTLEMENT_SQL),
        # Sweep of settlements still `pending` after the redelivery window plus grace (crash / lost outcome write).
        Index("ix_settlements_stale_pending", "settled_at", postgresql_where=STALE_PENDING_SETTLEMENT_SQL),
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
        CheckConstraint(f"delivery_outcome IN ({_sql_list(DELIVERY_OUTCOMES)})", name="delivery_outcome_known"),
        CheckConstraint(f"delivery_failure IN ({_sql_list(DELIVERY_FAILURES)})", name="delivery_failure_known"),
        CheckConstraint(DELIVERY_FAILURE_IFF_FAILED_SQL, name="delivery_failure_iff_failed"),
        CheckConstraint(f"failed_delivery_credit_policy IN ({_sql_list(CREDIT_POLICIES)})", name="credit_policy_known"),
    )


# ---------------------------------------------------------------- prepaid credit (CONTRACT v2.11)

PREPAID_DEBIT_DEBITED = "debited"
PREPAID_DEBIT_REFUNDED = "refunded"
BALANCE_IDENTITY_SQL = (
    "balance_sompi = deposited_sompi + credited_sompi - debited_sompi - refunded_sompi - expired_sompi"
)
# CONTRACT v2.14: `credit_balance_sompi` is the part of the balance that came from failed-delivery credits and is
# still unspent. Calls spend it first; it is never refundable on-chain; expiry clears it. It never exceeds the
# credit ever granted nor what is spendable (balance minus the refund hold), so refundable = spendable - it.
CREDIT_BALANCE_SQL = "credit_balance_sompi BETWEEN 0 AND LEAST(credited_sompi, balance_sompi - refund_hold_sompi)"
FAILED_DELIVERY_CREDIT_ROW_SQL = "kind = 'failed_delivery_credit'"
# v2.13 (0008) allows a 0-sompi `refund` row (a late payout that found nothing left to debit); v2.14 adds a 0-sompi
# failed-delivery credit row fully clamped by the balance cap (its shortfall > 0).
LEDGER_AMOUNT_SQL = (
    "amount_sompi > 0 OR (kind = 'refund' AND amount_sompi = 0) "
    "OR (kind = 'failed_delivery_credit' AND amount_sompi = 0 AND shortfall_sompi > 0)"
)
LEDGER_SHORTFALL_SQL = "shortfall_sompi >= 0 AND (shortfall_sompi = 0 OR kind = 'failed_delivery_credit')"
# Credit rows, and only they, carry settlement_id + slug + credit_reason; they also carry tx_id and are credits.
CREDIT_ROWS_CARRY_SETTLEMENT_SQL = (
    "(kind = 'failed_delivery_credit') = (settlement_id IS NOT NULL) "
    "AND (settlement_id IS NOT NULL) = (slug IS NOT NULL) "
    "AND (slug IS NOT NULL) = (credit_reason IS NOT NULL) "
    "AND (kind <> 'failed_delivery_credit' OR (tx_id IS NOT NULL AND direction = 'credit'))"
)


class PrepaidBalance(TimestampMixin, Base):
    """One running prepaid credit balance per (buyer P2PK key, merchant).

    KasGate holds no funds: every deposit already paid the merchant (output 0) and the treasury (output 1) on-chain.
    The balance is an off-chain record of calls the merchant has been prepaid for. `balance_sompi` can never go
    negative and always equals `deposited_sompi + credited_sompi - debited_sompi - refunded_sompi - expired_sompi`
    (refunded debits are subtracted from `debited_sompi`; `credited_sompi` is the merchant share of failed on-chain
    deliveries credited back, see `prepaid_ledger_entries` kind `failed_delivery_credit`).
    CONTRACT v2.13: `refund_hold_sompi` is the part of the balance an open refund request has set aside (it cannot
    be spent); it leaves the balance as `refunded_sompi` only once the merchant's payout transaction is seen on-chain.
    Spendable credit is `balance_sompi - refund_hold_sompi`, and none while `expires_at` has passed.
    CONTRACT v2.14: `credit_balance_sompi` is the unspent failed-delivery credit inside the balance. Debits spend it
    before deposited funds, refunds can never take it (refundable = spendable - credit_balance) and expiry clears it.
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
    refunded_sompi: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    expired_sompi: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    refund_hold_sompi: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    credited_sompi: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    credit_balance_sompi: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_prepaid_balances_merchant_id_payer_public_key", "merchant_id", "payer_public_key", unique=True),
        Index("ix_prepaid_balances_payer_address", "payer_address"),
        Index("ix_prepaid_balances_expires_at", "expires_at", postgresql_where="expires_at IS NOT NULL"),
        CheckConstraint("balance_sompi >= 0", name="balance_not_negative"),
        CheckConstraint(
            "deposited_sompi >= 0 AND debited_sompi >= 0 AND refunded_sompi >= 0 AND expired_sompi >= 0",
            name="totals_not_negative",
        ),
        CheckConstraint(BALANCE_IDENTITY_SQL, name="balance_matches_totals"),
        CheckConstraint("credited_sompi >= 0", name="credited_not_negative"),
        CheckConstraint(CREDIT_BALANCE_SQL, name="credit_balance_within_spendable"),
        CheckConstraint("refund_hold_sompi BETWEEN 0 AND balance_sompi", name="refund_hold_within_balance"),
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
    # CONTRACT v2.14: the part of `amount_sompi` paid from failed-delivery credit (spent first); a refunded debit
    # gives exactly this part back to `credit_balance_sompi`, so a failed call never turns credit into deposit.
    credit_sompi: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
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
        CheckConstraint("credit_sompi BETWEEN 0 AND amount_sompi", name="credit_within_amount"),
    )


# ---------------------------------------------------------------- prepaid refunds and expiry (CONTRACT v2.13)

PREPAID_REFUND_REQUESTED = "requested"
PREPAID_REFUND_PAYING = "paying"
PREPAID_REFUND_PAYOUT_SUBMITTED = "payout_submitted"
PREPAID_REFUND_PAID = "paid"
PREPAID_REFUND_REJECTED = "rejected"
PREPAID_REFUND_CANCELLED = "cancelled"
PREPAID_REFUND_OPEN_STATUSES: tuple[str, ...] = (
    PREPAID_REFUND_REQUESTED,
    PREPAID_REFUND_PAYING,
    PREPAID_REFUND_PAYOUT_SUBMITTED,
)
PREPAID_REFUND_STATUSES: tuple[str, ...] = (
    *PREPAID_REFUND_OPEN_STATUSES,
    PREPAID_REFUND_PAID,
    PREPAID_REFUND_REJECTED,
    PREPAID_REFUND_CANCELLED,
)
LEDGER_KIND_REFUND = "refund"
LEDGER_KIND_EXPIRY = "expiry"
LEDGER_KIND_FAILED_DELIVERY_CREDIT = "failed_delivery_credit"
LEDGER_KINDS: tuple[str, ...] = (LEDGER_KIND_REFUND, LEDGER_KIND_EXPIRY, LEDGER_KIND_FAILED_DELIVERY_CREDIT)
CREDIT_REASON_ATTEMPTS_EXHAUSTED = "attempts_exhausted"
CREDIT_REASON_WINDOW_EXPIRED = "window_expired"
CREDIT_REASONS: tuple[str, ...] = (CREDIT_REASON_ATTEMPTS_EXHAUSTED, CREDIT_REASON_WINDOW_EXPIRED)
PAYOUT_ATTEMPT_PENDING = "pending"
PAYOUT_ATTEMPT_CONFIRMED = "confirmed"
PAYOUT_ATTEMPT_INVALID = "invalid"
PAYOUT_ATTEMPT_DUPLICATE = "duplicate"
PAYOUT_ATTEMPT_ABANDONED = "abandoned"
PAYOUT_ATTEMPT_STATUSES: tuple[str, ...] = (
    PAYOUT_ATTEMPT_PENDING,
    PAYOUT_ATTEMPT_CONFIRMED,
    PAYOUT_ATTEMPT_INVALID,
    PAYOUT_ATTEMPT_DUPLICATE,
    PAYOUT_ATTEMPT_ABANDONED,
)
# `scan`: found by the gateway itself on the node (mempool or UTXO index) when the buyer tried to cancel.
PAYOUT_METHODS: tuple[str, ...] = ("tx_id", "transaction", "scan")


class PrepaidRefund(TimestampMixin, Base):
    """A buyer's signed request to get unused prepaid credit back, and the merchant's on-chain payout of it.

    KasGate never moves the coins: the merchant pays `amount_sompi` from their own wallet to `payer_address` (an
    ordinary payment with no platform fee and no treasury output). Opening a request moves the amount into the
    balance's `refund_hold_sompi`; the ledger is debited only when the payout `tx_id` is seen on-chain (status
    `paid`). A rejected or cancelled request releases the hold. At most one request per balance is open at a time.

    `paying` is the merchant's claim (before broadcasting): the buyer cannot cancel it. Every attempted payout
    transaction is kept in `prepaid_refund_payouts`; `tx_id` here is the latest attempt while `payout_submitted` and
    the confirmed payout once `paid`. A payout confirmed for a refund that was already cancelled or rejected is still
    recorded (`late_paid_from`), debiting what the balance still holds (`shortfall_sompi` is what it could not).
    """

    __tablename__ = "prepaid_refunds"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    balance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prepaid_balances.id", ondelete="RESTRICT"), nullable=False
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    payer_address: Mapped[str] = mapped_column(String(100), nullable=False)
    payer_public_key: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_sompi: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PREPAID_REFUND_REQUESTED, server_default=PREPAID_REFUND_REQUESTED
    )
    # The single-use nonce of the signed buyer request that opened this refund.
    request_nonce: Mapped[str] = mapped_column(String(32), nullable=False)
    # The merchant's payout transaction (latest attempt while payout_submitted, the confirmed one once paid).
    tx_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The first claim: claims cannot be renewed past first_claimed_at + prepaid_refund_claim_max_seconds. Kept after
    # the claim ends, so a cancel knows the merchant may have broadcast a payout.
    first_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # A cancelled refund the merchant had claimed keeps its amount in the balance's refund hold until this time.
    hold_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when a payout was confirmed after the refund had been cancelled or rejected.
    late_paid_from: Mapped[str | None] = mapped_column(String(16), nullable=True)
    shortfall_sompi: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    payout_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_prepaid_refunds_tx_id", "tx_id", unique=True),
        Index("ix_prepaid_refunds_request_nonce", "request_nonce", unique=True),
        Index("ix_prepaid_refunds_merchant_status_created", "merchant_id", "status", "created_at"),
        Index("ix_prepaid_refunds_hold_until", "hold_until", postgresql_where="hold_until IS NOT NULL"),
        Index(
            "ix_prepaid_refunds_open_balance_id",
            "balance_id",
            unique=True,
            postgresql_where=f"status IN ({_sql_list(PREPAID_REFUND_OPEN_STATUSES)})",
        ),
        CheckConstraint("amount_sompi > 0", name="amount_positive"),
        CheckConstraint("shortfall_sompi BETWEEN 0 AND amount_sompi", name="shortfall_within_amount"),
        CheckConstraint(
            "late_paid_from IS NULL OR (late_paid_from IN ('cancelled', 'rejected') AND status = 'paid')",
            name="late_paid_from_known",
        ),
        CheckConstraint("late_paid_from IS NOT NULL OR shortfall_sompi = 0", name="shortfall_only_when_late"),
        CheckConstraint("status <> 'paying' OR claimed_at IS NOT NULL", name="paying_is_claimed"),
        CheckConstraint(
            "claimed_at IS NULL OR first_claimed_at IS NOT NULL",
            name="claimed_has_first_claim",
        ),
        CheckConstraint("hold_until IS NULL OR status = 'cancelled'", name="hold_until_only_cancelled"),
        CheckConstraint(f"status IN ({_sql_list(PREPAID_REFUND_STATUSES)})", name="status_known"),
        CheckConstraint("tx_id ~ '^[0-9a-f]{64}$'", name="tx_id_hex64"),
        CheckConstraint("request_nonce ~ '^[0-9a-f]{32}$'", name="request_nonce_hex32"),
        CheckConstraint("payer_public_key ~ '^[0-9a-f]{64}$'", name="payer_public_key_hex64"),
        CheckConstraint("(status IN ('payout_submitted', 'paid')) = (tx_id IS NOT NULL)", name="tx_id_iff_payout"),
        CheckConstraint(
            "(status = 'payout_submitted') = (payout_submitted_at IS NOT NULL)",
            name="payout_submitted_at_iff_submitted",
        ),
        CheckConstraint("(status = 'paid') = (paid_at IS NOT NULL)", name="paid_at_iff_paid"),
        CheckConstraint(
            "(status IN ('paid', 'rejected', 'cancelled')) = (resolved_at IS NOT NULL)", name="resolved_at_iff_closed"
        ),
    )


class PrepaidLedgerEntry(Base):
    """One movement of a prepaid balance that is neither a deposit nor a call debit: a paid refund, an expiry, or a
    failed-delivery credit.

    `amount_sompi` is positive, except for a late payout (of an already cancelled or rejected refund) that found
    nothing left to debit, recorded with 0; `direction` says which way it moved the balance. Refund rows carry the
    merchant's payout `tx_id` and `refund_id`. `failed_delivery_credit` rows (direction `credit`) carry the paid
    call's on-chain `tx_id`, its `settlement_id`, the endpoint `slug` and the `credit_reason`; their amount is the
    merchant share of that payment (the treasury fee in output 1 is never reversed). At most one credit row exists
    per tx_id and per settlement (unique partial indexes), which makes crediting idempotent.
    """

    __tablename__ = "prepaid_ledger_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    balance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prepaid_balances.id", ondelete="RESTRICT"), nullable=False
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    payer_address: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(6), nullable=False)
    amount_sompi: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after_sompi: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tx_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    refund_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prepaid_refunds.id", ondelete="RESTRICT"), nullable=True
    )
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    settlement_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("settlements.id", ondelete="RESTRICT"), nullable=True
    )
    slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    credit_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Credit rows: the part of the merchant share NOT credited because the balance reached PREPAID_MAX_BALANCE_SOMPI.
    shortfall_sompi: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_prepaid_ledger_entries_tx_id", "tx_id"),
        Index(
            "ix_prepaid_ledger_entries_credit_tx_id",
            "tx_id",
            unique=True,
            postgresql_where=FAILED_DELIVERY_CREDIT_ROW_SQL,
        ),
        Index("ix_prepaid_ledger_entries_settlement_id", "settlement_id", unique=True),
        Index("ix_prepaid_ledger_entries_slug", "slug"),
        Index("ix_prepaid_ledger_entries_balance_id", "balance_id"),
        Index("ix_prepaid_ledger_entries_merchant_created", "merchant_id", "created_at"),
        Index("ix_prepaid_ledger_entries_refund_id", "refund_id", unique=True),
        CheckConstraint(LEDGER_AMOUNT_SQL, name="amount_valid"),
        CheckConstraint(LEDGER_SHORTFALL_SQL, name="shortfall_only_on_credit_rows"),
        CheckConstraint("balance_after_sompi >= 0", name="balance_after_not_negative"),
        CheckConstraint(f"kind IN ({_sql_list(LEDGER_KINDS)})", name="kind_known"),
        CheckConstraint("direction IN ('credit', 'debit')", name="direction_known"),
        CheckConstraint("tx_id ~ '^[0-9a-f]{64}$'", name="tx_id_hex64"),
        CheckConstraint(
            "(kind = 'refund') = (refund_id IS NOT NULL AND tx_id IS NOT NULL)", name="refund_rows_carry_payout"
        ),
        CheckConstraint(
            CREDIT_ROWS_CARRY_SETTLEMENT_SQL,
            name="credit_rows_carry_settlement",
        ),
        CheckConstraint(f"credit_reason IN ({_sql_list(CREDIT_REASONS)})", name="credit_reason_known"),
    )


class PrepaidRefundPayout(TimestampMixin, Base):
    """Every payout transaction a merchant posted (or the gateway broadcast) for a refund, kept for good.

    A tx_id can belong to one refund only (unique index). `pending` attempts are re-checked by the ledger worker,
    even after the refund went back to `requested`, until confirmed, proven invalid, or `abandoned` after
    `prepaid_payout_attempt_max_seconds`. `payload_verified` records that the `kasgate:v1:refund:<id>` payload was
    seen (mempool entry or the signed transaction itself), which a later UTXO-index sighting relies on.
    """

    __tablename__ = "prepaid_refund_payouts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    refund_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prepaid_refunds.id", ondelete="RESTRICT"), nullable=False
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False
    )
    tx_id: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=PAYOUT_ATTEMPT_PENDING, server_default=PAYOUT_ATTEMPT_PENDING
    )
    payload_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    check_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_check_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_prepaid_refund_payouts_tx_id", "tx_id", unique=True),
        Index("ix_prepaid_refund_payouts_refund_id", "refund_id"),
        Index(
            "ix_prepaid_refund_payouts_pending_next_check",
            "next_check_at",
            postgresql_where=f"status = '{PAYOUT_ATTEMPT_PENDING}'",
        ),
        CheckConstraint("tx_id ~ '^[0-9a-f]{64}$'", name="tx_id_hex64"),
        CheckConstraint(f"status IN ({_sql_list(PAYOUT_ATTEMPT_STATUSES)})", name="status_known"),
        CheckConstraint(f"method IN ({_sql_list(PAYOUT_METHODS)})", name="method_known"),
        CheckConstraint("check_count >= 0 AND error_count >= 0", name="counts_not_negative"),
        CheckConstraint("(status = 'pending') = (resolved_at IS NULL)", name="resolved_at_iff_resolved"),
        CheckConstraint(
            "(status IN ('confirmed', 'duplicate')) = (confirmed_at IS NOT NULL)", name="confirmed_at_iff_confirmed"
        ),
    )


# ---------------------------------------------------------------- free trial calls (CONTRACT v2.12)

TRIAL_CALL_USED = "used"
TRIAL_CALL_REFUNDED = "refunded"


class TrialUsage(TimestampMixin, Base):
    """How many free trial calls one buyer key has used on one endpoint. `calls_used` never exceeds the endpoint's
    `free_trial_calls` at the moment of each call (conditional UPDATE under the row lock)."""

    __tablename__ = "trial_usages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )
    payer_address: Mapped[str] = mapped_column(String(100), nullable=False)
    payer_public_key: Mapped[str] = mapped_column(String(64), nullable=False)
    calls_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (
        Index("ix_trial_usages_endpoint_id_payer_public_key", "endpoint_id", "payer_public_key", unique=True),
        Index("ix_trial_usages_merchant_id", "merchant_id"),
        CheckConstraint("calls_used >= 0", name="calls_used_not_negative"),
        CheckConstraint("payer_public_key ~ '^[0-9a-f]{64}$'", name="payer_public_key_hex64"),
    )


class TrialDailyCount(Base):
    """Trial calls one endpoint served on one UTC day (the anti-abuse daily cap)."""

    __tablename__ = "trial_daily_counts"

    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("endpoints.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    __table_args__ = (CheckConstraint("calls >= 0", name="calls_not_negative"),)


class TrialCall(Base):
    """One free trial call: signed by the buyer key over a single-use nonce. No settlement, no fee."""

    __tablename__ = "trial_calls"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    usage_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trial_usages.id", ondelete="CASCADE"), nullable=False
    )
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("endpoints.id", ondelete="CASCADE"), nullable=False
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )
    nonce: Mapped[str] = mapped_column(String(32), nullable=False)
    payer_address: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default=TRIAL_CALL_USED, server_default=TRIAL_CALL_USED
    )
    request_method: Mapped[str] = mapped_column(String(10), nullable=False)
    request_path: Mapped[str] = mapped_column(Text, nullable=False)
    body_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    upstream_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_trial_calls_nonce", "nonce", unique=True),
        Index("ix_trial_calls_usage_id", "usage_id"),
        Index("ix_trial_calls_endpoint_created", "endpoint_id", "created_at"),
        Index("ix_trial_calls_merchant_created", "merchant_id", "created_at"),
        CheckConstraint("nonce ~ '^[0-9a-f]{32}$'", name="nonce_hex32"),
        CheckConstraint("body_hash ~ '^[0-9a-f]{64}$'", name="body_hash_hex64"),
        CheckConstraint("status IN ('used', 'refunded')", name="status_known"),
        CheckConstraint("(status = 'refunded') = (refunded_at IS NOT NULL)", name="refunded_at_iff_refunded"),
    )


# ---------------------------------------------------------------- merchant webhooks (CONTRACT v2.12)

WEBHOOK_DELIVERY_PENDING = "pending"
WEBHOOK_DELIVERY_DELIVERING = "delivering"
WEBHOOK_DELIVERY_SUCCEEDED = "succeeded"
WEBHOOK_DELIVERY_DEAD = "dead"
WEBHOOK_EVENT_TYPES: tuple[str, ...] = (
    "payment.settled",
    "prepaid.deposit",
    "prepaid.debit",
    "trial.used",
    "endpoint.created",
    "endpoint.updated",
    # CONTRACT v2.13
    "prepaid.refund.requested",
    "prepaid.refund.paid",
    "prepaid.refund.rejected",
    "prepaid.refund.cancelled",
    "prepaid.refund.payout_reverted",
    "prepaid.credit.expired",
    "prepaid.credit.failed_delivery",
    "prepaid.credit.failed_delivery_shortfall",
)
WEBHOOK_TEST_EVENT = "webhook.test"
WEBHOOK_EVENT_TYPES_SQL = ", ".join(f"'{name}'" for name in (*WEBHOOK_EVENT_TYPES, WEBHOOK_TEST_EVENT))


class Webhook(TimestampMixin, Base):
    """A merchant's HTTPS webhook. The HMAC secret is stored Fernet-encrypted and is never returned after creation."""

    __tablename__ = "webhooks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(String(200), nullable=True)
    event_types: Mapped[list[str]] = mapped_column(ARRAY(String(40)), nullable=False)
    secret_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    secret_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")

    __table_args__ = (
        Index("ix_webhooks_merchant_id", "merchant_id"),
        CheckConstraint("cardinality(event_types) > 0", name="event_types_not_empty"),
        CheckConstraint("url LIKE 'https://%'", name="url_https"),
    )


class WebhookEvent(Base):
    """One merchant event in the outbox. `event_id` is the idempotency key receivers dedupe on."""

    __tablename__ = "webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_webhook_events_event_id", "event_id", unique=True),
        Index("ix_webhook_events_merchant_created", "merchant_id", "created_at"),
        CheckConstraint("event_id ~ '^evt_[0-9a-f]{32}$'", name="event_id_format"),
        CheckConstraint(f"event_type IN ({WEBHOOK_EVENT_TYPES_SQL})", name="event_type_known"),
    )


class WebhookDelivery(Base):
    """One (event, webhook) delivery with its retry state: pending -> delivering -> succeeded | pending | dead."""

    __tablename__ = "webhook_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_pk: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("webhook_events.id", ondelete="CASCADE"), nullable=False
    )
    webhook_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("webhooks.id", ondelete="CASCADE"), nullable=False
    )
    merchant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("merchants.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, default=WEBHOOK_DELIVERY_PENDING, server_default=WEBHOOK_DELIVERY_PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_webhook_deliveries_event_pk_webhook_id", "event_pk", "webhook_id", unique=True),
        Index("ix_webhook_deliveries_webhook_created", "webhook_id", "created_at"),
        Index("ix_webhook_deliveries_merchant_created", "merchant_id", "created_at"),
        Index(
            "ix_webhook_deliveries_due",
            "next_attempt_at",
            postgresql_where="status IN ('pending', 'delivering')",
        ),
        CheckConstraint("status IN ('pending', 'delivering', 'succeeded', 'dead')", name="status_known"),
        CheckConstraint("attempts >= 0", name="attempts_not_negative"),
        CheckConstraint("(status = 'succeeded') = (delivered_at IS NOT NULL)", name="delivered_at_iff_succeeded"),
    )
