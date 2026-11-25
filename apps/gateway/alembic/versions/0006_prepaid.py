"""CONTRACT v2.11: prepaid credit for prices below the on-chain minimum.

Revision ID: 0006_prepaid
Revises: 0005_exact_v2
Create Date: 2026-11-25 00:00:00

* `endpoints.prepaid_only` (default false): the endpoint is paid only from a prepaid balance, so it may be priced
  below the 0.6 KAS on-chain minimum.
* `prepaid_balances`: one running balance per (merchant, buyer x-only Schnorr key). The balance can never go negative
  and always equals deposited - debited.
* `prepaid_deposits`: the kaspa-exact-v1 settlement (merchant output 0, treasury output 1) that funded a balance.
  One deposit per settlement and per txId.
* `prepaid_debits`: one signed, single-use (unique nonce) off-chain call debit; `refunded` when the upstream failed.

KasGate holds no funds: every deposit already paid the merchant and the treasury on-chain.

Downgrade refuses while any prepaid row or prepaid-only endpoint exists: balances are buyer credit the merchant
was paid for, and dropping them silently is not acceptable.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_prepaid"
down_revision: str | None = "0005_exact_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "endpoints",
        sa.Column("prepaid_only", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )

    op.create_table(
        "prepaid_balances",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payer_address", sa.String(length=100), nullable=False),
        sa.Column("payer_public_key", sa.String(length=64), nullable=False),
        sa.Column("balance_sompi", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("deposited_sompi", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("debited_sompi", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("balance_sompi >= 0", name=op.f("ck_prepaid_balances_balance_not_negative")),
        sa.CheckConstraint(
            "deposited_sompi >= 0 AND debited_sompi >= 0", name=op.f("ck_prepaid_balances_totals_not_negative")
        ),
        sa.CheckConstraint(
            "balance_sompi = deposited_sompi - debited_sompi", name=op.f("ck_prepaid_balances_balance_matches_totals")
        ),
        sa.CheckConstraint(
            "payer_public_key ~ '^[0-9a-f]{64}$'", name=op.f("ck_prepaid_balances_payer_public_key_hex64")
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name=op.f("fk_prepaid_balances_merchant_id_merchants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prepaid_balances")),
    )
    op.create_index(
        "ix_prepaid_balances_merchant_id_payer_public_key",
        "prepaid_balances",
        ["merchant_id", "payer_public_key"],
        unique=True,
    )
    op.create_index("ix_prepaid_balances_payer_address", "prepaid_balances", ["payer_address"], unique=False)

    op.create_table(
        "prepaid_deposits",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("balance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("settlement_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tx_id", sa.String(length=64), nullable=False),
        sa.Column("payer_address", sa.String(length=100), nullable=False),
        sa.Column("deposit_sompi", sa.BigInteger(), nullable=False),
        sa.Column("fee_sompi", sa.BigInteger(), nullable=False),
        sa.Column("credit_sompi", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("tx_id ~ '^[0-9a-f]{64}$'", name=op.f("ck_prepaid_deposits_tx_id_hex64")),
        sa.CheckConstraint("credit_sompi > 0 AND fee_sompi > 0", name=op.f("ck_prepaid_deposits_amounts_positive")),
        sa.CheckConstraint(
            "credit_sompi + fee_sompi = deposit_sompi", name=op.f("ck_prepaid_deposits_split_sums_to_deposit")
        ),
        sa.ForeignKeyConstraint(
            ["balance_id"],
            ["prepaid_balances.id"],
            name=op.f("fk_prepaid_deposits_balance_id_prepaid_balances"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name=op.f("fk_prepaid_deposits_merchant_id_merchants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["settlement_id"],
            ["settlements.id"],
            name=op.f("fk_prepaid_deposits_settlement_id_settlements"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prepaid_deposits")),
    )
    op.create_index("ix_prepaid_deposits_tx_id", "prepaid_deposits", ["tx_id"], unique=True)
    op.create_index("ix_prepaid_deposits_settlement_id", "prepaid_deposits", ["settlement_id"], unique=True)
    op.create_index("ix_prepaid_deposits_balance_id", "prepaid_deposits", ["balance_id"], unique=False)
    op.create_index(
        "ix_prepaid_deposits_merchant_created", "prepaid_deposits", ["merchant_id", "created_at"], unique=False
    )

    op.create_table(
        "prepaid_debits",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("balance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("endpoint_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("nonce", sa.String(length=32), nullable=False),
        sa.Column("payer_address", sa.String(length=100), nullable=False),
        sa.Column("amount_sompi", sa.BigInteger(), nullable=False),
        sa.Column("kas_usd_rate", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("usd_value", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("status", sa.String(length=10), server_default="debited", nullable=False),
        sa.Column("request_method", sa.String(length=10), nullable=False),
        sa.Column("request_path", sa.Text(), nullable=False),
        sa.Column("body_hash", sa.String(length=64), nullable=True),
        sa.Column("upstream_status_code", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount_sompi > 0", name=op.f("ck_prepaid_debits_amount_positive")),
        sa.CheckConstraint("nonce ~ '^[0-9a-f]{32}$'", name=op.f("ck_prepaid_debits_nonce_hex32")),
        sa.CheckConstraint("body_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_prepaid_debits_body_hash_hex64")),
        sa.CheckConstraint("status IN ('debited', 'refunded')", name=op.f("ck_prepaid_debits_status_known")),
        sa.CheckConstraint(
            "(status = 'refunded') = (refunded_at IS NOT NULL)",
            name=op.f("ck_prepaid_debits_refunded_at_iff_refunded"),
        ),
        sa.ForeignKeyConstraint(
            ["balance_id"],
            ["prepaid_balances.id"],
            name=op.f("fk_prepaid_debits_balance_id_prepaid_balances"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["endpoints.id"],
            name=op.f("fk_prepaid_debits_endpoint_id_endpoints"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name=op.f("fk_prepaid_debits_merchant_id_merchants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prepaid_debits")),
    )
    op.create_index("ix_prepaid_debits_nonce", "prepaid_debits", ["nonce"], unique=True)
    op.create_index("ix_prepaid_debits_balance_id", "prepaid_debits", ["balance_id"], unique=False)
    op.create_index("ix_prepaid_debits_merchant_created", "prepaid_debits", ["merchant_id", "created_at"], unique=False)
    op.create_index("ix_prepaid_debits_endpoint_created", "prepaid_debits", ["endpoint_id", "created_at"], unique=False)


def downgrade() -> None:
    # Refuse rather than drop buyer credit the merchant was already paid for on-chain.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM prepaid_balances) OR EXISTS (SELECT 1 FROM prepaid_deposits) "
        "OR EXISTS (SELECT 1 FROM prepaid_debits) OR EXISTS (SELECT 1 FROM endpoints WHERE prepaid_only) THEN "
        "RAISE EXCEPTION 'prepaid balances, deposits, debits or prepaid-only endpoints exist; export them before "
        "downgrading below 0006_prepaid'; END IF; END $$"
    )
    op.drop_index("ix_prepaid_debits_endpoint_created", table_name="prepaid_debits")
    op.drop_index("ix_prepaid_debits_merchant_created", table_name="prepaid_debits")
    op.drop_index("ix_prepaid_debits_balance_id", table_name="prepaid_debits")
    op.drop_index("ix_prepaid_debits_nonce", table_name="prepaid_debits")
    op.drop_table("prepaid_debits")
    op.drop_index("ix_prepaid_deposits_merchant_created", table_name="prepaid_deposits")
    op.drop_index("ix_prepaid_deposits_balance_id", table_name="prepaid_deposits")
    op.drop_index("ix_prepaid_deposits_settlement_id", table_name="prepaid_deposits")
    op.drop_index("ix_prepaid_deposits_tx_id", table_name="prepaid_deposits")
    op.drop_table("prepaid_deposits")
    op.drop_index("ix_prepaid_balances_payer_address", table_name="prepaid_balances")
    op.drop_index("ix_prepaid_balances_merchant_id_payer_public_key", table_name="prepaid_balances")
    op.drop_table("prepaid_balances")
    op.drop_column("endpoints", "prepaid_only")
