"""k402 kaspa-utxo settlements: record the payment scheme and the k402 payment_id.

Revision ID: 0004_k402_utxo
Revises: 0003_usd_pricing
Create Date: 2026-11-23 00:00:00

Existing rows are all KasGate-native (`kaspa-exact-v1`) payments, so `payment_scheme` is back-filled through its server
default and `k402_payment_id` stays NULL. `payer_address` becomes nullable because an unmodified k402 client proves no
payer address; a check constraint keeps it required for `kaspa-exact-v1` rows. Downgrading refuses to run while any
k402 settlement exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_k402_utxo"
down_revision: str | None = "0003_usd_pricing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "settlements",
        sa.Column("payment_scheme", sa.String(length=20), server_default="kaspa-exact-v1", nullable=False),
    )
    op.add_column("settlements", sa.Column("k402_payment_id", sa.String(length=18), nullable=True))
    op.alter_column("settlements", "payer_address", existing_type=sa.String(length=100), nullable=True)
    op.create_index(op.f("ix_settlements_k402_payment_id"), "settlements", ["k402_payment_id"], unique=True)
    op.create_check_constraint(
        op.f("ck_settlements_payment_scheme_known"),
        "settlements",
        "payment_scheme IN ('kaspa-exact-v1', 'k402-kaspa-utxo')",
    )
    op.create_check_constraint(
        op.f("ck_settlements_k402_payment_id_iff_k402"),
        "settlements",
        "(payment_scheme = 'k402-kaspa-utxo') = (k402_payment_id IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_settlements_k402_payment_id_format"), "settlements", "k402_payment_id ~ '^p_[0-9a-f]{16}$'"
    )
    op.create_check_constraint(
        op.f("ck_settlements_payer_required_for_v1"),
        "settlements",
        "payment_scheme = 'k402-kaspa-utxo' OR payer_address IS NOT NULL",
    )


def downgrade() -> None:
    # Refuse rather than drop paid k402 settlements: their payer_address is NULL and cannot be restored.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM settlements WHERE payment_scheme = 'k402-kaspa-utxo') THEN "
        "RAISE EXCEPTION 'k402 settlements exist; export them before downgrading below 0004_k402_utxo'; "
        "END IF; END $$"
    )
    op.drop_constraint(op.f("ck_settlements_payer_required_for_v1"), "settlements", type_="check")
    op.drop_constraint(op.f("ck_settlements_k402_payment_id_format"), "settlements", type_="check")
    op.drop_constraint(op.f("ck_settlements_k402_payment_id_iff_k402"), "settlements", type_="check")
    op.drop_constraint(op.f("ck_settlements_payment_scheme_known"), "settlements", type_="check")
    op.drop_index(op.f("ix_settlements_k402_payment_id"), table_name="settlements")
    op.alter_column("settlements", "payer_address", existing_type=sa.String(length=100), nullable=False)
    op.drop_column("settlements", "k402_payment_id")
    op.drop_column("settlements", "payment_scheme")
