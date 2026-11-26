"""Failed-delivery auto-credit: the merchant share of a paid call whose upstream failed becomes prepaid credit.

Revision ID: 0009_failed_delivery_credits
Revises: 0008_prepaid_refunds
Create Date: 2026-11-26 00:00:00

* `merchants.failed_delivery_credit_enabled_at` (NOT NULL, default now()), `failed_delivery_credit_disabled_at`
  (NULL): times of the last switch of the opt-out policy; `failed_delivery_credit_timeouts` (default true).
* `settlements`: `delivery_outcome` (pending|delivered|failed|credited), `delivery_failure`
  (upstream_5xx|connect_error|timeout|oversized, set iff failed/credited), `failed_delivery_credit_policy`
  (errors|errors_and_timeouts, the merchant policy stamped at settle time) and `pending_alerted_at` (set once when
  a row is still `pending` after the redelivery window plus grace). Existing rows stay NULL, so no payment
  settled before the upgrade is ever credited. Partial indexes `ix_settlements_undelivered` (failed, covered) and
  `ix_settlements_stale_pending` (pending, not yet alerted) for the sweeper.
* `prepaid_balances.credited_sompi` (default 0). The 0008 identity
  `balance = deposited - debited - refunded - expired` becomes
  `balance = deposited + credited - debited - refunded - expired`; existing rows stay valid (credited = 0).
  `credit_balance_sompi` (default 0): unspent call-only credit, `0 <= it <= LEAST(credited, balance - hold)`.
* `prepaid_debits.credit_sompi` (default 0, `0 <= it <= amount`): the part of a debit paid from credit.
* `prepaid_ledger_entries`: kind CHECK gains `failed_delivery_credit`; new nullable `settlement_id` (FK, unique),
  `slug` (indexed) and `credit_reason`; `shortfall_sompi` (default 0, credit rows only); a CHECK that credit rows
  (and only they) carry settlement, tx_id, slug and reason with direction `credit`; the amount CHECK allows 0 only
  on a credit row fully clamped by the balance cap; a unique partial index on tx_id for credit rows.
* The webhook event type CHECK gains `prepaid.credit.failed_delivery` and
  `prepaid.credit.failed_delivery_shortfall`.

Downgrade refuses while any credit row, credited balance, credit-paid debit, credit event or subscribed webhook
exists.
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
SHORTFALL_EVENT_TYPE = "prepaid.credit.failed_delivery_shortfall"
OLD_EVENT_TYPES_SQL = (
    "'payment.settled', 'prepaid.deposit', 'prepaid.debit', 'trial.used', 'endpoint.created', "
    "'endpoint.updated', 'prepaid.refund.requested', 'prepaid.refund.paid', 'prepaid.refund.rejected', "
    "'prepaid.refund.cancelled', 'prepaid.refund.payout_reverted', 'prepaid.credit.expired', 'webhook.test'"
)
NEW_EVENT_TYPES_SQL = (
    "'payment.settled', 'prepaid.deposit', 'prepaid.debit', 'trial.used', 'endpoint.created', "
    "'endpoint.updated', 'prepaid.refund.requested', 'prepaid.refund.paid', 'prepaid.refund.rejected', "
    "'prepaid.refund.cancelled', 'prepaid.refund.payout_reverted', 'prepaid.credit.expired', "
    "'prepaid.credit.failed_delivery', 'prepaid.credit.failed_delivery_shortfall', 'webhook.test'"
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
UNDELIVERED_SQL = "status = 'settled' AND delivery_outcome = 'failed' AND failed_delivery_credit_policy IS NOT NULL"
STALE_PENDING_SQL = "status = 'settled' AND delivery_outcome = 'pending' AND pending_alerted_at IS NULL"
CREDIT_BALANCE_SQL = "credit_balance_sompi BETWEEN 0 AND LEAST(credited_sompi, balance_sompi - refund_hold_sompi)"
# 0008's check exactly (a 0-sompi `refund` row is a late payout shortfall), plus a fully clamped credit row.
OLD_AMOUNT_SQL = "amount_sompi > 0 OR (kind = 'refund' AND amount_sompi = 0)"
NEW_AMOUNT_SQL = (
    "amount_sompi > 0 OR (kind = 'refund' AND amount_sompi = 0) "
    "OR (kind = 'failed_delivery_credit' AND amount_sompi = 0 AND shortfall_sompi > 0)"
)
SHORTFALL_SQL = "shortfall_sompi >= 0 AND (shortfall_sompi = 0 OR kind = 'failed_delivery_credit')"
SETTLEMENT_CHECKS = (
    ("ck_settlements_delivery_outcome_known", "delivery_outcome IN ('pending', 'delivered', 'failed', 'credited')"),
    (
        "ck_settlements_delivery_failure_known",
        "delivery_failure IN ('upstream_5xx', 'connect_error', 'timeout', 'oversized')",
    ),
    (
        "ck_settlements_delivery_failure_iff_failed",
        "(delivery_failure IS NOT NULL) = (delivery_outcome IN ('failed', 'credited'))",
    ),
    ("ck_settlements_credit_policy_known", "failed_delivery_credit_policy IN ('errors', 'errors_and_timeouts')"),
)
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
        "merchants",
        sa.Column("failed_delivery_credit_timeouts", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )

    op.add_column("settlements", sa.Column("delivery_outcome", sa.String(length=10), nullable=True))
    op.add_column("settlements", sa.Column("delivery_failure", sa.String(length=16), nullable=True))
    op.add_column("settlements", sa.Column("failed_delivery_credit_policy", sa.String(length=20), nullable=True))
    op.add_column("settlements", sa.Column("pending_alerted_at", sa.DateTime(timezone=True), nullable=True))
    for name, sql in SETTLEMENT_CHECKS:
        op.create_check_constraint(op.f(name), "settlements", sql)

    op.add_column(
        "prepaid_balances",
        sa.Column("credited_sompi", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
    )
    _replace_check("prepaid_balances", "ck_prepaid_balances_balance_matches_totals", NEW_BALANCE_IDENTITY)
    op.create_check_constraint(
        op.f("ck_prepaid_balances_credited_not_negative"), "prepaid_balances", "credited_sompi >= 0"
    )
    op.add_column(
        "prepaid_balances",
        sa.Column("credit_balance_sompi", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_prepaid_balances_credit_balance_within_spendable"), "prepaid_balances", CREDIT_BALANCE_SQL
    )
    op.add_column(
        "prepaid_debits", sa.Column("credit_sompi", sa.BigInteger(), server_default=sa.text("0"), nullable=False)
    )
    op.create_check_constraint(
        op.f("ck_prepaid_debits_credit_within_amount"), "prepaid_debits", "credit_sompi BETWEEN 0 AND amount_sompi"
    )

    op.add_column(LEDGER, sa.Column("settlement_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(LEDGER, sa.Column("slug", sa.String(length=64), nullable=True))
    op.add_column(LEDGER, sa.Column("credit_reason", sa.String(length=20), nullable=True))
    op.add_column(LEDGER, sa.Column("shortfall_sompi", sa.BigInteger(), server_default=sa.text("0"), nullable=False))
    _replace_check(LEDGER, "ck_prepaid_ledger_entries_amount_valid", NEW_AMOUNT_SQL)
    op.create_check_constraint(op.f("ck_prepaid_ledger_entries_shortfall_only_on_credit_rows"), LEDGER, SHORTFALL_SQL)
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
    op.create_index(
        "ix_settlements_stale_pending",
        "settlements",
        ["settled_at"],
        unique=False,
        postgresql_where=sa.text(STALE_PENDING_SQL),
    )

    _replace_check("webhook_events", "ck_webhook_events_event_type_known", f"event_type IN ({NEW_EVENT_TYPES_SQL})")


def downgrade() -> None:
    op.execute(
        f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM {LEDGER} WHERE {CREDIT_ROW_SQL}) "
        "OR EXISTS (SELECT 1 FROM prepaid_balances WHERE credited_sompi <> 0 OR credit_balance_sompi <> 0) "
        "OR EXISTS (SELECT 1 FROM prepaid_debits WHERE credit_sompi <> 0) "
        f"OR EXISTS (SELECT 1 FROM webhook_events WHERE event_type IN ('{NEW_EVENT_TYPE}', '{SHORTFALL_EVENT_TYPE}')) "
        f"OR EXISTS (SELECT 1 FROM webhooks WHERE '{NEW_EVENT_TYPE}' = ANY(event_types) "
        f"OR '{SHORTFALL_EVENT_TYPE}' = ANY(event_types)) THEN "
        "RAISE EXCEPTION 'failed-delivery credits, credited balances or credit webhooks exist; export them before "
        "downgrading below 0009_failed_delivery_credits'; END IF; END $$"
    )
    _replace_check("webhook_events", "ck_webhook_events_event_type_known", f"event_type IN ({OLD_EVENT_TYPES_SQL})")
    op.drop_index("ix_settlements_stale_pending", table_name="settlements")
    op.drop_index("ix_settlements_undelivered", table_name="settlements")
    op.drop_index("ix_prepaid_ledger_entries_slug", table_name=LEDGER)
    op.drop_index("ix_prepaid_ledger_entries_settlement_id", table_name=LEDGER)
    op.drop_index("ix_prepaid_ledger_entries_credit_tx_id", table_name=LEDGER)
    op.drop_constraint(op.f("ck_prepaid_ledger_entries_credit_reason_known"), LEDGER, type_="check")
    op.drop_constraint(op.f("ck_prepaid_ledger_entries_credit_rows_carry_settlement"), LEDGER, type_="check")
    _replace_check(LEDGER, "ck_prepaid_ledger_entries_kind_known", OLD_KINDS_SQL)
    op.drop_constraint(op.f("fk_prepaid_ledger_entries_settlement_id_settlements"), LEDGER, type_="foreignkey")
    op.drop_constraint(op.f("ck_prepaid_ledger_entries_shortfall_only_on_credit_rows"), LEDGER, type_="check")
    _replace_check(LEDGER, "ck_prepaid_ledger_entries_amount_valid", OLD_AMOUNT_SQL)
    op.drop_column(LEDGER, "shortfall_sompi")
    op.drop_column(LEDGER, "credit_reason")
    op.drop_column(LEDGER, "slug")
    op.drop_column(LEDGER, "settlement_id")
    op.drop_constraint(op.f("ck_prepaid_debits_credit_within_amount"), "prepaid_debits", type_="check")
    op.drop_column("prepaid_debits", "credit_sompi")
    op.drop_constraint(
        op.f("ck_prepaid_balances_credit_balance_within_spendable"), "prepaid_balances", type_="check"
    )
    op.drop_column("prepaid_balances", "credit_balance_sompi")
    op.drop_constraint(op.f("ck_prepaid_balances_credited_not_negative"), "prepaid_balances", type_="check")
    _replace_check("prepaid_balances", "ck_prepaid_balances_balance_matches_totals", OLD_BALANCE_IDENTITY)
    op.drop_column("prepaid_balances", "credited_sompi")
    for name, _sql in reversed(SETTLEMENT_CHECKS):
        op.drop_constraint(op.f(name), "settlements", type_="check")
    op.drop_column("settlements", "pending_alerted_at")
    op.drop_column("settlements", "failed_delivery_credit_policy")
    op.drop_column("settlements", "delivery_failure")
    op.drop_column("settlements", "delivery_outcome")
    op.drop_column("merchants", "failed_delivery_credit_timeouts")
    op.drop_column("merchants", "failed_delivery_credit_disabled_at")
    op.drop_column("merchants", "failed_delivery_credit_enabled_at")
