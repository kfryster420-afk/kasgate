"""CONTRACT v2: bind each settlement to its payment nonce and count deliveries.

Revision ID: 0002_payment_nonce
Revises: 0001_initial
Create Date: 2026-11-20 00:00:00

`settlements.payment_nonce` becomes NOT NULL with a unique index and a 32-hex check. Rows written before this
revision have no nonce, so each one is back-filled with `md5('legacy:' || id)`: 32 lowercase hex characters,
unique per row, and recognisable as pre-v2 data. `delivery_attempts` starts at 1 for every existing row.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_payment_nonce"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("settlements", sa.Column("payment_nonce", sa.String(length=32), nullable=True))
    op.execute("UPDATE settlements SET payment_nonce = md5('legacy:' || id::text) WHERE payment_nonce IS NULL")
    op.alter_column("settlements", "payment_nonce", existing_type=sa.String(length=32), nullable=False)
    op.add_column(
        "settlements",
        sa.Column("delivery_attempts", sa.Integer(), server_default="1", nullable=False),
    )
    op.create_index(op.f("ix_settlements_payment_nonce"), "settlements", ["payment_nonce"], unique=True)
    op.create_check_constraint(
        op.f("ck_settlements_payment_nonce_hex32"), "settlements", "payment_nonce ~ '^[0-9a-f]{32}$'"
    )
    op.create_check_constraint(
        op.f("ck_settlements_delivery_attempts_positive"), "settlements", "delivery_attempts >= 1"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_settlements_delivery_attempts_positive"), "settlements", type_="check")
    op.drop_constraint(op.f("ck_settlements_payment_nonce_hex32"), "settlements", type_="check")
    op.drop_index(op.f("ix_settlements_payment_nonce"), table_name="settlements")
    op.drop_column("settlements", "delivery_attempts")
    op.drop_column("settlements", "payment_nonce")
