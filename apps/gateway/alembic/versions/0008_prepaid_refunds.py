"""CONTRACT v2.13: prepaid refunds (merchant-signed payouts of unused credit) and optional credit expiry.

Revision ID: 0008_prepaid_refunds
Revises: 0007_trials_webhooks
Create Date: 2026-11-26 00:00:00

* `merchants.prepaid_expiry_days` and `endpoints.prepaid_expiry_days` (1..3650 or NULL = never).
* `prepaid_balances`: `refunded_sompi`, `expired_sompi`, `refund_hold_sompi`, `expires_at`; the balance identity
  becomes `balance = deposited - debited - refunded - expired` and the hold stays within the balance.
* `prepaid_refunds`: one row per signed buyer refund request (unique request nonce, unique payout tx_id, at most one
  open request per balance; `paying` = claimed by the merchant; late payouts of closed refunds are recorded).
* `prepaid_refund_payouts`: every attempted payout transaction (explicit unique index on tx_id, index on refund_id,
  partial index of pending re-checks).
* `prepaid_ledger_entries`: paid refunds and expiries (explicit index on tx_id, unique refund_id).
* The webhook event type CHECK gains the six v2.13 event types.

Existing balances keep `expires_at` NULL (never expire): a policy is never applied retroactively.
Downgrade refuses while any refund, ledger entry, expiring balance or expiry policy exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_prepaid_refunds"
down_revision: str | None = "0007_trials_webhooks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_EVENT_TYPES_SQL = (
    "'payment.settled', 'prepaid.deposit', 'prepaid.debit', 'trial.used', 'endpoint.created', "
    "'endpoint.updated', 'webhook.test'"
)
NEW_EVENT_TYPES_SQL = (
    "'payment.settled', 'prepaid.deposit', 'prepaid.debit', 'trial.used', 'endpoint.created', "
    "'endpoint.updated', 'prepaid.refund.requested', 'prepaid.refund.paid', 'prepaid.refund.rejected', "
    "'prepaid.refund.cancelled', 'prepaid.refund.payout_reverted', 'prepaid.credit.expired', 'webhook.test'"
)
OPEN_STATUSES_SQL = "status IN ('requested', 'paying', 'payout_submitted')"


def upgrade() -> None:
    for table in ("merchants", "endpoints"):
        op.add_column(table, sa.Column("prepaid_expiry_days", sa.Integer(), nullable=True))
        op.create_check_constraint(
            op.f(f"ck_{table}_prepaid_expiry_days_range"), table, "prepaid_expiry_days BETWEEN 1 AND 3650"
        )

    for column in ("refunded_sompi", "expired_sompi", "refund_hold_sompi"):
        op.add_column(
            "prepaid_balances", sa.Column(column, sa.BigInteger(), server_default=sa.text("0"), nullable=False)
        )
    op.add_column("prepaid_balances", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.drop_constraint(op.f("ck_prepaid_balances_balance_matches_totals"), "prepaid_balances", type_="check")
    op.drop_constraint(op.f("ck_prepaid_balances_totals_not_negative"), "prepaid_balances", type_="check")
    op.create_check_constraint(
        op.f("ck_prepaid_balances_totals_not_negative"),
        "prepaid_balances",
        "deposited_sompi >= 0 AND debited_sompi >= 0 AND refunded_sompi >= 0 AND expired_sompi >= 0",
    )
    op.create_check_constraint(
        op.f("ck_prepaid_balances_balance_matches_totals"),
        "prepaid_balances",
        "balance_sompi = deposited_sompi - debited_sompi - refunded_sompi - expired_sompi",
    )
    op.create_check_constraint(
        op.f("ck_prepaid_balances_refund_hold_within_balance"),
        "prepaid_balances",
        "refund_hold_sompi BETWEEN 0 AND balance_sompi",
    )
    op.create_index(
        "ix_prepaid_balances_expires_at",
        "prepaid_balances",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )

    op.create_table(
        "prepaid_refunds",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("balance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payer_address", sa.String(length=100), nullable=False),
        sa.Column("payer_public_key", sa.String(length=64), nullable=False),
        sa.Column("amount_sompi", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="requested", nullable=False),
        sa.Column("request_nonce", sa.String(length=32), nullable=False),
        sa.Column("tx_id", sa.String(length=64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_claimed_daa_score", sa.BigInteger(), nullable=True),
        sa.Column("hold_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("late_paid_from", sa.String(length=16), nullable=True),
        sa.Column("shortfall_sompi", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("reason", sa.String(length=300), nullable=True),
        sa.Column("last_error", sa.String(length=300), nullable=True),
        sa.Column("payout_submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("amount_sompi > 0", name=op.f("ck_prepaid_refunds_amount_positive")),
        sa.CheckConstraint(
            "shortfall_sompi BETWEEN 0 AND amount_sompi", name=op.f("ck_prepaid_refunds_shortfall_within_amount")
        ),
        sa.CheckConstraint(
            "late_paid_from IS NULL OR (late_paid_from IN ('cancelled', 'rejected') AND status = 'paid')",
            name=op.f("ck_prepaid_refunds_late_paid_from_known"),
        ),
        sa.CheckConstraint(
            "late_paid_from IS NOT NULL OR shortfall_sompi = 0",
            name=op.f("ck_prepaid_refunds_shortfall_only_when_late"),
        ),
        sa.CheckConstraint(
            "status <> 'paying' OR claimed_at IS NOT NULL", name=op.f("ck_prepaid_refunds_paying_is_claimed")
        ),
        sa.CheckConstraint(
            "claimed_at IS NULL OR first_claimed_at IS NOT NULL",
            name=op.f("ck_prepaid_refunds_claimed_has_first_claim"),
        ),
        sa.CheckConstraint(
            "hold_until IS NULL OR status = 'cancelled'", name=op.f("ck_prepaid_refunds_hold_until_only_cancelled")
        ),
        sa.CheckConstraint(
            "first_claimed_daa_score IS NULL OR (first_claimed_at IS NOT NULL AND first_claimed_daa_score >= 0)",
            name=op.f("ck_prepaid_refunds_claimed_daa_after_first_claim"),
        ),
        sa.CheckConstraint(
            "status IN ('requested', 'paying', 'payout_submitted', 'paid', 'rejected', 'cancelled')",
            name=op.f("ck_prepaid_refunds_status_known"),
        ),
        sa.CheckConstraint("tx_id ~ '^[0-9a-f]{64}$'", name=op.f("ck_prepaid_refunds_tx_id_hex64")),
        sa.CheckConstraint("request_nonce ~ '^[0-9a-f]{32}$'", name=op.f("ck_prepaid_refunds_request_nonce_hex32")),
        sa.CheckConstraint(
            "payer_public_key ~ '^[0-9a-f]{64}$'", name=op.f("ck_prepaid_refunds_payer_public_key_hex64")
        ),
        sa.CheckConstraint(
            "(status IN ('payout_submitted', 'paid')) = (tx_id IS NOT NULL)",
            name=op.f("ck_prepaid_refunds_tx_id_iff_payout"),
        ),
        sa.CheckConstraint(
            "(status = 'payout_submitted') = (payout_submitted_at IS NOT NULL)",
            name=op.f("ck_prepaid_refunds_payout_submitted_at_iff_submitted"),
        ),
        sa.CheckConstraint(
            "(status = 'paid') = (paid_at IS NOT NULL)", name=op.f("ck_prepaid_refunds_paid_at_iff_paid")
        ),
        sa.CheckConstraint(
            "(status IN ('paid', 'rejected', 'cancelled')) = (resolved_at IS NOT NULL)",
            name=op.f("ck_prepaid_refunds_resolved_at_iff_closed"),
        ),
        sa.ForeignKeyConstraint(
            ["balance_id"],
            ["prepaid_balances.id"],
            name=op.f("fk_prepaid_refunds_balance_id_prepaid_balances"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name=op.f("fk_prepaid_refunds_merchant_id_merchants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prepaid_refunds")),
    )
    op.create_index("ix_prepaid_refunds_tx_id", "prepaid_refunds", ["tx_id"], unique=True)
    op.create_index("ix_prepaid_refunds_request_nonce", "prepaid_refunds", ["request_nonce"], unique=True)
    op.create_index(
        "ix_prepaid_refunds_merchant_status_created",
        "prepaid_refunds",
        ["merchant_id", "status", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_prepaid_refunds_open_balance_id",
        "prepaid_refunds",
        ["balance_id"],
        unique=True,
        postgresql_where=sa.text(OPEN_STATUSES_SQL),
    )
    op.create_index(
        "ix_prepaid_refunds_hold_until",
        "prepaid_refunds",
        ["hold_until"],
        unique=False,
        postgresql_where=sa.text("hold_until IS NOT NULL"),
    )

    op.create_table(
        "prepaid_refund_payouts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("refund_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tx_id", sa.String(length=64), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("payload_verified", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("check_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_check_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_error", sa.String(length=300), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("tx_id ~ '^[0-9a-f]{64}$'", name=op.f("ck_prepaid_refund_payouts_tx_id_hex64")),
        sa.CheckConstraint(
            "status IN ('pending', 'confirmed', 'invalid', 'duplicate', 'abandoned')",
            name=op.f("ck_prepaid_refund_payouts_status_known"),
        ),
        sa.CheckConstraint("method IN ('tx_id', 'transaction', 'scan')", name=op.f("ck_prepaid_refund_payouts_method_known")),
        sa.CheckConstraint(
            "check_count >= 0 AND error_count >= 0", name=op.f("ck_prepaid_refund_payouts_counts_not_negative")
        ),
        sa.CheckConstraint(
            "(status = 'pending') = (resolved_at IS NULL)",
            name=op.f("ck_prepaid_refund_payouts_resolved_at_iff_resolved"),
        ),
        sa.CheckConstraint(
            "(status IN ('confirmed', 'duplicate')) = (confirmed_at IS NOT NULL)",
            name=op.f("ck_prepaid_refund_payouts_confirmed_at_iff_confirmed"),
        ),
        sa.ForeignKeyConstraint(
            ["refund_id"],
            ["prepaid_refunds.id"],
            name=op.f("fk_prepaid_refund_payouts_refund_id_prepaid_refunds"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name=op.f("fk_prepaid_refund_payouts_merchant_id_merchants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prepaid_refund_payouts")),
    )
    op.create_index("ix_prepaid_refund_payouts_tx_id", "prepaid_refund_payouts", ["tx_id"], unique=True)
    op.create_index("ix_prepaid_refund_payouts_refund_id", "prepaid_refund_payouts", ["refund_id"], unique=False)
    op.create_index(
        "ix_prepaid_refund_payouts_pending_next_check",
        "prepaid_refund_payouts",
        ["next_check_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "prepaid_ledger_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("balance_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payer_address", sa.String(length=100), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=6), nullable=False),
        sa.Column("amount_sompi", sa.BigInteger(), nullable=False),
        sa.Column("balance_after_sompi", sa.BigInteger(), nullable=False),
        sa.Column("tx_id", sa.String(length=64), nullable=True),
        sa.Column("refund_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "amount_sompi > 0 OR (kind = 'refund' AND amount_sompi = 0)",
            name=op.f("ck_prepaid_ledger_entries_amount_valid"),
        ),
        sa.CheckConstraint(
            "balance_after_sompi >= 0", name=op.f("ck_prepaid_ledger_entries_balance_after_not_negative")
        ),
        sa.CheckConstraint("kind IN ('refund', 'expiry')", name=op.f("ck_prepaid_ledger_entries_kind_known")),
        sa.CheckConstraint("direction IN ('credit', 'debit')", name=op.f("ck_prepaid_ledger_entries_direction_known")),
        sa.CheckConstraint("tx_id ~ '^[0-9a-f]{64}$'", name=op.f("ck_prepaid_ledger_entries_tx_id_hex64")),
        sa.CheckConstraint(
            "(kind = 'refund') = (refund_id IS NOT NULL AND tx_id IS NOT NULL)",
            name=op.f("ck_prepaid_ledger_entries_refund_rows_carry_payout"),
        ),
        sa.ForeignKeyConstraint(
            ["balance_id"],
            ["prepaid_balances.id"],
            name=op.f("fk_prepaid_ledger_entries_balance_id_prepaid_balances"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name=op.f("fk_prepaid_ledger_entries_merchant_id_merchants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["refund_id"],
            ["prepaid_refunds.id"],
            name=op.f("fk_prepaid_ledger_entries_refund_id_prepaid_refunds"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prepaid_ledger_entries")),
    )
    op.create_index("ix_prepaid_ledger_entries_tx_id", "prepaid_ledger_entries", ["tx_id"], unique=False)
    op.create_index("ix_prepaid_ledger_entries_balance_id", "prepaid_ledger_entries", ["balance_id"], unique=False)
    op.create_index(
        "ix_prepaid_ledger_entries_merchant_created",
        "prepaid_ledger_entries",
        ["merchant_id", "created_at"],
        unique=False,
    )
    op.create_index("ix_prepaid_ledger_entries_refund_id", "prepaid_ledger_entries", ["refund_id"], unique=True)

    op.drop_constraint(op.f("ck_webhook_events_event_type_known"), "webhook_events", type_="check")
    op.create_check_constraint(
        op.f("ck_webhook_events_event_type_known"), "webhook_events", f"event_type IN ({NEW_EVENT_TYPES_SQL})"
    )


def downgrade() -> None:
    """Also works from the first draft of this revision (15d7bd4: no `prepaid_refund_payouts`, an extra
    `ix_prepaid_refunds_payout_submitted`), so such a database can be taken back to 0007 and upgraded again."""
    op.execute(
        "DO $$ DECLARE has_payouts boolean := false; BEGIN "
        "IF to_regclass('prepaid_refund_payouts') IS NOT NULL THEN "
        "EXECUTE 'SELECT EXISTS (SELECT 1 FROM prepaid_refund_payouts)' INTO has_payouts; END IF; "
        "IF has_payouts OR EXISTS (SELECT 1 FROM prepaid_refunds) OR EXISTS (SELECT 1 FROM prepaid_ledger_entries) "
        "OR EXISTS (SELECT 1 FROM prepaid_balances WHERE expires_at IS NOT NULL OR refunded_sompi > 0 "
        "OR expired_sompi > 0 OR refund_hold_sompi > 0) "
        "OR EXISTS (SELECT 1 FROM merchants WHERE prepaid_expiry_days IS NOT NULL) "
        "OR EXISTS (SELECT 1 FROM endpoints WHERE prepaid_expiry_days IS NOT NULL) "
        "OR EXISTS (SELECT 1 FROM webhook_events WHERE event_type LIKE 'prepaid.refund.%' "
        "OR event_type = 'prepaid.credit.expired') THEN "
        "RAISE EXCEPTION 'prepaid refunds, expiries or expiry policies exist; export them before "
        "downgrading below 0008_prepaid_refunds'; END IF; END $$"
    )
    op.drop_constraint(op.f("ck_webhook_events_event_type_known"), "webhook_events", type_="check")
    op.create_check_constraint(
        op.f("ck_webhook_events_event_type_known"), "webhook_events", f"event_type IN ({OLD_EVENT_TYPES_SQL})"
    )
    op.drop_index("ix_prepaid_ledger_entries_refund_id", table_name="prepaid_ledger_entries")
    op.drop_index("ix_prepaid_ledger_entries_merchant_created", table_name="prepaid_ledger_entries")
    op.drop_index("ix_prepaid_ledger_entries_balance_id", table_name="prepaid_ledger_entries")
    op.drop_index("ix_prepaid_ledger_entries_tx_id", table_name="prepaid_ledger_entries")
    op.drop_table("prepaid_ledger_entries")
    op.execute("DROP INDEX IF EXISTS ix_prepaid_refund_payouts_pending_next_check")
    op.execute("DROP INDEX IF EXISTS ix_prepaid_refund_payouts_refund_id")
    op.execute("DROP INDEX IF EXISTS ix_prepaid_refund_payouts_tx_id")
    op.execute("DROP TABLE IF EXISTS prepaid_refund_payouts")
    op.execute("DROP INDEX IF EXISTS ix_prepaid_refunds_hold_until")
    op.execute("DROP INDEX IF EXISTS ix_prepaid_refunds_payout_submitted")
    op.drop_index("ix_prepaid_refunds_open_balance_id", table_name="prepaid_refunds")
    op.drop_index("ix_prepaid_refunds_merchant_status_created", table_name="prepaid_refunds")
    op.drop_index("ix_prepaid_refunds_request_nonce", table_name="prepaid_refunds")
    op.drop_index("ix_prepaid_refunds_tx_id", table_name="prepaid_refunds")
    op.drop_table("prepaid_refunds")
    op.drop_index("ix_prepaid_balances_expires_at", table_name="prepaid_balances")
    op.drop_constraint(op.f("ck_prepaid_balances_refund_hold_within_balance"), "prepaid_balances", type_="check")
    op.drop_constraint(op.f("ck_prepaid_balances_balance_matches_totals"), "prepaid_balances", type_="check")
    op.drop_constraint(op.f("ck_prepaid_balances_totals_not_negative"), "prepaid_balances", type_="check")
    op.create_check_constraint(
        op.f("ck_prepaid_balances_totals_not_negative"),
        "prepaid_balances",
        "deposited_sompi >= 0 AND debited_sompi >= 0",
    )
    op.create_check_constraint(
        op.f("ck_prepaid_balances_balance_matches_totals"),
        "prepaid_balances",
        "balance_sompi = deposited_sompi - debited_sompi",
    )
    op.drop_column("prepaid_balances", "expires_at")
    for column in ("refund_hold_sompi", "expired_sompi", "refunded_sompi"):
        op.drop_column("prepaid_balances", column)
    for table in ("endpoints", "merchants"):
        op.drop_constraint(op.f(f"ck_{table}_prepaid_expiry_days_range"), table, type_="check")
        op.drop_column(table, "prepaid_expiry_days")
