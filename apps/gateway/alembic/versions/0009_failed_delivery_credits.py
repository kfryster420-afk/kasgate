"""Failed-delivery auto-credit: the merchant share of a paid call whose upstream failed becomes prepaid credit.

Revision ID: 0009_failed_delivery_credits
Revises: 0008_prepaid_refunds
Create Date: 2026-11-26 00:00:00

* `merchants.failed_delivery_credit_enabled_at` (NOT NULL, default now()) and
  `merchants.failed_delivery_credit_disabled_at` (NULL): the opt-out policy. Existing merchants are opted in from
  the moment this migration runs, so no payment settled before the upgrade is ever credited.
* `prepaid_balances.credited_sompi` (default 0). The 0008 identity
  `balance = deposited - debited - refunded - expired` becomes
  `balance = deposited + credited - debited - refunded - expired`; existing rows stay valid (credited = 0).
* `prepaid_ledger_entries`: kind CHECK gains `failed_delivery_credit`; new nullable `settlement_id` (FK, unique),
  `slug` (indexed) and `credit_reason`; a CHECK that credit rows (and only they) carry settlement, tx_id, slug and
  reason with direction `credit`; a unique partial index on tx_id for credit rows (idempotency per tx_id).
* `settlements`: partial index `ix_settlements_undelivered` (settled, no upstream answer or a 5xx) for the sweeper.
* The webhook event type CHECK gains `prepaid.credit.failed_delivery`.

Downgrade refuses while any credit row, credited balance, credit event or subscribed webhook exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_failed_delivery_credits"
down_revision: str | None = "0008_prepaid_refunds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_EVENT_TYPE = "prepaid.credit.failed_delivery"
OLD_EVENT_TYPES_SQL = (
    "'payment.settled', 'prepaid.deposit', 'prepaid.debit', 'trial.used', 'endpoint.created', "
    "'endpoint.updated', 'prepaid.refund.requested', 'prepaid.refund.paid', 'prepaid.refund.rejected', "
    "'prepaid.refund.cancelled', 'prepaid.refund.payout_reverted', 'prepaid.credit.expired', 'webhook.test'"
)
NEW_EVENT_TYPES_SQL = (
    "'payment.settled', 'prepaid.deposit', 'prepaid.debit', 'trial.used', 'endpoint.created', "
    "'endpoint.updated', 'prepaid.refund.requested', 'prepaid.refund.paid', 'prepaid.refund.rejected', "
    "'prepaid.refund.cancelled', 'prepaid.refund.payout_reverted', 'prepaid.credit.expired', "
    "'prepaid.credit.failed_delivery', 'webhook.test'"
)
OLD_BALANCE_IDENTITY = "balance_sompi = deposited_sompi - debited_sompi - refunded_sompi - expired_sompi"
NEW_BALANCE_IDENTITY = (
    "balance_sompi = deposited_sompi + credited_sompi - debited_sompi - refunded_sompi - expired_sompi"
)
OLD_KINDS_SQL = "kind IN ('refund', 'expiry')"
NEW_KINDS_SQL = "kind IN ('refund', 'expiry', 'failed_delivery_credit')"
CREDIT_ROW_SQL = "kind = 'failed_delivery_credit'"
CREDIT_ROWS_CARRY_SETTLEMENT_SQL = (
    "(kind = 'failed_delivery_credit') = (settlement_id IS NOT NULL) "
    "AND (settlement_id IS NOT NULL) = (slug IS NOT NULL) "
    "AND (slug IS NOT NULL) = (credit_reason IS NOT NULL) "
    "AND (kind <> 'failed_delivery_credit' OR (tx_id IS NOT NULL AND direction = 'credit'))"
)
UNDELIVERED_SQL = "status = 'settled' AND (upstream_status_code IS NULL OR upstream_status_code >= 500)"
LEDGER = "prepaid_ledger_entries"


def _replace_check(table: str, name: str, sql: str) -> None:
    op.drop_constraint(op.f(name), table, type_="check")
    op.create_check_constraint(op.f(name), table, sql)


def upgrade() -> None:
    op.add_column(
        "merchants",
        sa.Column(
            "failed_delivery_credit_enabled_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.add_column(
        "merchants", sa.Column("failed_delivery_credit_disabled_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.add_column(
        "prepaid_balances",
        sa.Column("credited_sompi", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
    )
    _replace_check("prepaid_balances", "ck_prepaid_balances_balance_matches_totals", NEW_BALANCE_IDENTITY)
    op.create_check_constraint(
        op.f("ck_prepaid_balances_credited_not_negative"), "prepaid_balances", "credited_sompi >= 0"
    )

    op.add_column(LEDGER, sa.Column("settlement_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(LEDGER, sa.Column("slug", sa.String(length=64), nullable=True))
    op.add_column(LEDGER, sa.Column("credit_reason", sa.String(length=20), nullable=True))
    op.create_foreign_key(
        op.f("fk_prepaid_ledger_entries_settlement_id_settlements"),
        LEDGER,
        "settlements",
        ["settlement_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    _replace_check(LEDGER, "ck_prepaid_ledger_entries_kind_known", NEW_KINDS_SQL)
    op.create_check_constraint(
        op.f("ck_prepaid_ledger_entries_credit_rows_carry_settlement"),
        LEDGER,
        CREDIT_ROWS_CARRY_SETTLEMENT_SQL,
    )
    op.create_check_constraint(
        op.f("ck_prepaid_ledger_entries_credit_reason_known"),
        LEDGER,
        "credit_reason IN ('attempts_exhausted', 'window_expired')",
    )
    op.create_index(
        "ix_prepaid_ledger_entries_credit_tx_id",
        LEDGER,
        ["tx_id"],
        unique=True,
        postgresql_where=sa.text(CREDIT_ROW_SQL),
    )
    op.create_index("ix_prepaid_ledger_entries_settlement_id", LEDGER, ["settlement_id"], unique=True)
    op.create_index("ix_prepaid_ledger_entries_slug", LEDGER, ["slug"], unique=False)

    op.create_index(
        "ix_settlements_undelivered",
        "settlements",
        ["settled_at"],
        unique=False,
        postgresql_where=sa.text(UNDELIVERED_SQL),
    )

    _replace_check("webhook_events", "ck_webhook_events_event_type_known", f"event_type IN ({NEW_EVENT_TYPES_SQL})")


def downgrade() -> None:
    op.execute(
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM {LEDGER} WHERE {CREDIT_ROW_SQL}) "
        "OR EXISTS (SELECT 1 FROM prepaid_balances WHERE credited_sompi <> 0) "
        f"OR EXISTS (SELECT 1 FROM webhook_events WHERE event_type = '{NEW_EVENT_TYPE}') "
        f"OR EXISTS (SELECT 1 FROM webhooks WHERE '{NEW_EVENT_TYPE}' = ANY(event_types)) THEN "
        "RAISE EXCEPTION 'failed-delivery credits, credited balances or credit webhooks exist; export them before "
        "downgrading below 0009_failed_delivery_credits'; END IF; END $$"
    )
    _replace_check("webhook_events", "ck_webhook_events_event_type_known", f"event_type IN ({OLD_EVENT_TYPES_SQL})")
    op.drop_index("ix_settlements_undelivered", table_name="settlements")
    op.drop_index("ix_prepaid_ledger_entries_slug", table_name=LEDGER)
    op.drop_index("ix_prepaid_ledger_entries_settlement_id", table_name=LEDGER)
    op.drop_index("ix_prepaid_ledger_entries_credit_tx_id", table_name=LEDGER)
    op.drop_constraint(op.f("ck_prepaid_ledger_entries_credit_reason_known"), LEDGER, type_="check")
    op.drop_constraint(op.f("ck_prepaid_ledger_entries_credit_rows_carry_settlement"), LEDGER, type_="check")
    _replace_check(LEDGER, "ck_prepaid_ledger_entries_kind_known", OLD_KINDS_SQL)
    op.drop_constraint(op.f("fk_prepaid_ledger_entries_settlement_id_settlements"), LEDGER, type_="foreignkey")
    op.drop_column(LEDGER, "credit_reason")
    op.drop_column(LEDGER, "slug")
    op.drop_column(LEDGER, "settlement_id")
    op.drop_constraint(op.f("ck_prepaid_balances_credited_not_negative"), "prepaid_balances", type_="check")
    _replace_check("prepaid_balances", "ck_prepaid_balances_balance_matches_totals", OLD_BALANCE_IDENTITY)
    op.drop_column("prepaid_balances", "credited_sompi")
    op.drop_column("merchants", "failed_delivery_credit_disabled_at")
    op.drop_column("merchants", "failed_delivery_credit_enabled_at")
