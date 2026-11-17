"""Initial schema: merchants, endpoints, settlements.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-18 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

settlement_status = postgresql.ENUM("pending", "settled", "rejected", name="settlement_status", create_type=False)


def upgrade() -> None:
    settlement_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "merchants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kaspa_address", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("api_key_hash", sa.String(length=64), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_merchants")),
    )
    op.create_index(op.f("ix_merchants_kaspa_address"), "merchants", ["kaspa_address"], unique=True)
    op.create_index(
        op.f("ix_merchants_email"),
        "merchants",
        ["email"],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL"),
    )
    op.create_index(op.f("ix_merchants_api_key_hash"), "merchants", ["api_key_hash"], unique=True)

    op.create_table(
        "endpoints",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(length=60), nullable=True),
        sa.Column("upstream_url", sa.Text(), nullable=False),
        sa.Column("upstream_headers_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("price_sompi", sa.BigInteger(), nullable=False),
        sa.Column(
            "allowed_methods",
            postgresql.ARRAY(sa.String(length=10)),
            server_default="{GET}",
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("is_public_listed", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("price_sompi > 0", name=op.f("ck_endpoints_price_positive")),
        sa.CheckConstraint("cardinality(allowed_methods) > 0", name=op.f("ck_endpoints_methods_not_empty")),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name=op.f("fk_endpoints_merchant_id_merchants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_endpoints")),
    )
    op.create_index(op.f("ix_endpoints_slug"), "endpoints", ["slug"], unique=True)
    op.create_index(op.f("ix_endpoints_merchant_id"), "endpoints", ["merchant_id"], unique=False)
    op.create_index(op.f("ix_endpoints_directory"), "endpoints", ["is_public_listed", "is_active"], unique=False)

    op.create_table(
        "settlements",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tx_id", sa.String(length=64), nullable=False),
        sa.Column("endpoint_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("network", sa.String(length=20), nullable=False),
        sa.Column("payer_address", sa.String(length=100), nullable=False),
        sa.Column("merchant_address", sa.String(length=100), nullable=False),
        sa.Column("treasury_address", sa.String(length=100), nullable=False),
        sa.Column("amount_sompi", sa.BigInteger(), nullable=False),
        sa.Column("merchant_amount_sompi", sa.BigInteger(), nullable=False),
        sa.Column("fee_amount_sompi", sa.BigInteger(), nullable=False),
        sa.Column("kas_usd_rate", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("usd_value_at_settlement", sa.Numeric(precision=20, scale=8), nullable=True),
        sa.Column("status", settlement_status, server_default="pending", nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("request_method", sa.String(length=10), nullable=False),
        sa.Column("request_path", sa.Text(), nullable=False),
        sa.Column("upstream_status_code", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount_sompi > 0", name=op.f("ck_settlements_amount_positive")),
        sa.CheckConstraint(
            "merchant_amount_sompi > 0 AND fee_amount_sompi > 0", name=op.f("ck_settlements_outputs_positive")
        ),
        sa.CheckConstraint(
            "merchant_amount_sompi + fee_amount_sompi = amount_sompi", name=op.f("ck_settlements_split_sums_to_total")
        ),
        sa.CheckConstraint("tx_id ~ '^[0-9a-f]{64}$'", name=op.f("ck_settlements_tx_id_hex64")),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["endpoints.id"],
            name=op.f("fk_settlements_endpoint_id_endpoints"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name=op.f("fk_settlements_merchant_id_merchants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_settlements")),
    )
    op.create_index(op.f("ix_settlements_tx_id"), "settlements", ["tx_id"], unique=True)
    op.create_index(op.f("ix_settlements_merchant_created"), "settlements", ["merchant_id", "created_at"], unique=False)
    op.create_index(op.f("ix_settlements_endpoint_created"), "settlements", ["endpoint_id", "created_at"], unique=False)
    op.create_index(op.f("ix_settlements_payer_address"), "settlements", ["payer_address"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_settlements_payer_address"), table_name="settlements")
    op.drop_index(op.f("ix_settlements_endpoint_created"), table_name="settlements")
    op.drop_index(op.f("ix_settlements_merchant_created"), table_name="settlements")
    op.drop_index(op.f("ix_settlements_tx_id"), table_name="settlements")
    op.drop_table("settlements")

    op.drop_index(op.f("ix_endpoints_directory"), table_name="endpoints")
    op.drop_index(op.f("ix_endpoints_merchant_id"), table_name="endpoints")
    op.drop_index(op.f("ix_endpoints_slug"), table_name="endpoints")
    op.drop_table("endpoints")

    op.drop_index(op.f("ix_merchants_api_key_hash"), table_name="merchants")
    op.drop_index(op.f("ix_merchants_email"), table_name="merchants", postgresql_where=sa.text("email IS NOT NULL"))
    op.drop_index(op.f("ix_merchants_kaspa_address"), table_name="merchants")
    op.drop_table("merchants")

    settlement_status.drop(op.get_bind(), checkfirst=True)
