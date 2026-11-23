"""CONTRACT USD pricing: endpoints may be priced in KAS (sompi) or USD (micro-dollars).

Revision ID: 0003_usd_pricing
Revises: 0002_payment_nonce
Create Date: 2026-11-23 00:00:00

Adds `price_currency` (KAS|USD, default KAS) and nullable `price_usd_micros`. `price_sompi` becomes nullable
so a USD-priced row does not store a misleading fixed KAS amount. Existing rows stay KAS with their sompi
price. The slug and settlements.tx_id indexes are not touched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_usd_pricing"
down_revision: str | None = "0002_payment_nonce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

price_currency = postgresql.ENUM("KAS", "USD", name="price_currency", create_type=False)


def upgrade() -> None:
    price_currency.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "endpoints",
        sa.Column("price_currency", price_currency, server_default="KAS", nullable=False),
    )
    op.add_column("endpoints", sa.Column("price_usd_micros", sa.BigInteger(), nullable=True))
    op.drop_constraint(op.f("ck_endpoints_price_positive"), "endpoints", type_="check")
    op.alter_column("endpoints", "price_sompi", existing_type=sa.BigInteger(), nullable=True)
    op.create_check_constraint(
        op.f("ck_endpoints_price_matches_currency"),
        "endpoints",
        "(price_currency = 'KAS' AND price_sompi > 0 AND price_usd_micros IS NULL) OR "
        "(price_currency = 'USD' AND price_usd_micros > 0 AND price_sompi IS NULL)",
    )


def downgrade() -> None:
    bind = op.get_bind()
    usd_rows = bind.execute(sa.text("SELECT count(*) FROM endpoints WHERE price_currency = 'USD'")).scalar_one()
    if usd_rows:
        raise RuntimeError(
            f"cannot downgrade 0003_usd_pricing: {usd_rows} endpoint(s) are priced in USD and have no sompi price"
        )
    op.drop_constraint(op.f("ck_endpoints_price_matches_currency"), "endpoints", type_="check")
    op.alter_column("endpoints", "price_sompi", existing_type=sa.BigInteger(), nullable=False)
    op.create_check_constraint(op.f("ck_endpoints_price_positive"), "endpoints", "price_sompi > 0")
    op.drop_column("endpoints", "price_usd_micros")
    op.drop_column("endpoints", "price_currency")
    price_currency.drop(bind, checkfirst=True)
