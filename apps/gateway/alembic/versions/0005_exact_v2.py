"""CONTRACT v2.10: record community kaspa-x402 `exact` / `kaspa-exact-v2` settlements.

Revision ID: 0005_exact_v2
Revises: 0004_k402_utxo
Create Date: 2026-11-23 00:00:00

* `payment_scheme` gains the value 'kaspa-exact-v2' (x402 scheme `exact` with extra.binding `kaspa-exact-v2`).
* kaspa-exact-v2 rows have no challenge nonce and no treasury output, so `payment_nonce` and `treasury_address`
  become nullable. They carry the x402 `payment-identifier` (unique) and the gateway's request fingerprint instead.
* `ck_settlements_outputs_positive` now allows a zero platform fee. `ck_settlements_treasury_split_unless_exact_v2`
  keeps the v1 / k402 rule (fee > 0, treasury and nonce present) and `ck_settlements_exact_v2_shape` pins v2 rows to
  fee = 0 with no treasury, no nonce, no k402 payment id and a payer address.

Downgrade refuses to run while kaspa-exact-v2 rows exist: they cannot be represented in the 0004 schema, and
dropping paid settlements silently is not acceptable.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_exact_v2"
down_revision: str | None = "0004_k402_utxo"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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


def upgrade() -> None:
    op.add_column("settlements", sa.Column("payment_identifier", sa.String(length=128), nullable=True))
    op.add_column("settlements", sa.Column("request_hash", sa.String(length=64), nullable=True))
    op.alter_column("settlements", "payment_nonce", existing_type=sa.String(length=32), nullable=True)
    op.alter_column("settlements", "treasury_address", existing_type=sa.String(length=100), nullable=True)
    op.create_index(op.f("ix_settlements_payment_identifier"), "settlements", ["payment_identifier"], unique=True)
    op.drop_constraint(op.f("ck_settlements_payment_scheme_known"), "settlements", type_="check")
    op.create_check_constraint(
        op.f("ck_settlements_payment_scheme_known"),
        "settlements",
        "payment_scheme IN ('kaspa-exact-v1', 'k402-kaspa-utxo', 'kaspa-exact-v2')",
    )
    op.drop_constraint(op.f("ck_settlements_outputs_positive"), "settlements", type_="check")
    op.create_check_constraint(
        op.f("ck_settlements_outputs_positive"), "settlements", "merchant_amount_sompi > 0 AND fee_amount_sompi >= 0"
    )
    op.create_check_constraint(op.f("ck_settlements_exact_v2_identifiers"), "settlements", EXACT_V2_IDENTIFIERS_SQL)
    op.create_check_constraint(op.f("ck_settlements_treasury_split_unless_exact_v2"), "settlements", TREASURY_SPLIT_SQL)
    op.create_check_constraint(op.f("ck_settlements_exact_v2_shape"), "settlements", EXACT_V2_SHAPE_SQL)
    op.create_check_constraint(
        op.f("ck_settlements_payment_identifier_format"),
        "settlements",
        "payment_identifier ~ '^[A-Za-z0-9_-]{16,128}$'",
    )
    op.create_check_constraint(
        op.f("ck_settlements_request_hash_hex64"), "settlements", "request_hash ~ '^[0-9a-f]{64}$'"
    )


def downgrade() -> None:
    # Refuse rather than drop paid kaspa-exact-v2 settlements: they have no nonce or treasury to restore.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM settlements WHERE payment_scheme = 'kaspa-exact-v2') THEN "
        "RAISE EXCEPTION 'kaspa-exact-v2 settlements exist; export them before downgrading below 0005_exact_v2'; "
        "END IF; END $$"
    )
    op.drop_constraint(op.f("ck_settlements_request_hash_hex64"), "settlements", type_="check")
    op.drop_constraint(op.f("ck_settlements_payment_identifier_format"), "settlements", type_="check")
    op.drop_constraint(op.f("ck_settlements_exact_v2_shape"), "settlements", type_="check")
    op.drop_constraint(op.f("ck_settlements_treasury_split_unless_exact_v2"), "settlements", type_="check")
    op.drop_constraint(op.f("ck_settlements_exact_v2_identifiers"), "settlements", type_="check")
    op.drop_constraint(op.f("ck_settlements_outputs_positive"), "settlements", type_="check")
    op.create_check_constraint(
        op.f("ck_settlements_outputs_positive"), "settlements", "merchant_amount_sompi > 0 AND fee_amount_sompi > 0"
    )
    op.drop_constraint(op.f("ck_settlements_payment_scheme_known"), "settlements", type_="check")
    op.create_check_constraint(
        op.f("ck_settlements_payment_scheme_known"),
        "settlements",
        "payment_scheme IN ('kaspa-exact-v1', 'k402-kaspa-utxo')",
    )
    op.drop_index(op.f("ix_settlements_payment_identifier"), table_name="settlements")
    op.alter_column("settlements", "treasury_address", existing_type=sa.String(length=100), nullable=False)
    op.alter_column("settlements", "payment_nonce", existing_type=sa.String(length=32), nullable=False)
    op.drop_column("settlements", "request_hash")
    op.drop_column("settlements", "payment_identifier")
