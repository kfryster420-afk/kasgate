"""CONTRACT v2.12: free trial calls and merchant webhooks.

Revision ID: 0007_trials_webhooks
Revises: 0006_prepaid
Create Date: 2026-11-26 00:00:00

* `endpoints.free_trial_calls` (0..100, default 0) and `endpoints.trial_daily_cap` (1..100000, default 100).
* `trial_usages`: calls used per (endpoint, buyer x-only key); `trial_daily_counts`: trial calls per endpoint per
  UTC day; `trial_calls`: one row per signed, single-use (unique nonce) trial call.
* `webhooks`: merchant HTTPS webhooks (secret Fernet-encrypted); `webhook_events`: the outbox (unique event_id);
  `webhook_deliveries`: one row per (event, webhook) with the retry state.

Downgrade refuses while any trial call, trial-enabled endpoint or webhook exists.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_trials_webhooks"
down_revision: str | None = "0006_prepaid"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_TYPES_SQL = (
    "'payment.settled', 'prepaid.deposit', 'prepaid.debit', 'trial.used', 'endpoint.created', "
    "'endpoint.updated', 'webhook.test'"
)


def upgrade() -> None:
    op.add_column("endpoints", sa.Column("free_trial_calls", sa.Integer(), server_default=sa.text("0"), nullable=False))
    op.add_column(
        "endpoints", sa.Column("trial_daily_cap", sa.Integer(), server_default=sa.text("100"), nullable=False)
    )
    op.create_check_constraint(
        op.f("ck_endpoints_free_trial_calls_range"), "endpoints", "free_trial_calls BETWEEN 0 AND 100"
    )
    op.create_check_constraint(
        op.f("ck_endpoints_trial_daily_cap_range"), "endpoints", "trial_daily_cap BETWEEN 1 AND 100000"
    )

    op.create_table(
        "trial_usages",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("endpoint_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payer_address", sa.String(length=100), nullable=False),
        sa.Column("payer_public_key", sa.String(length=64), nullable=False),
        sa.Column("calls_used", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("calls_used >= 0", name=op.f("ck_trial_usages_calls_used_not_negative")),
        sa.CheckConstraint("payer_public_key ~ '^[0-9a-f]{64}$'", name=op.f("ck_trial_usages_payer_public_key_hex64")),
        sa.ForeignKeyConstraint(
            ["endpoint_id"], ["endpoints.id"], name=op.f("fk_trial_usages_endpoint_id_endpoints"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], name=op.f("fk_trial_usages_merchant_id_merchants"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trial_usages")),
    )
    op.create_index(
        "ix_trial_usages_endpoint_id_payer_public_key",
        "trial_usages",
        ["endpoint_id", "payer_public_key"],
        unique=True,
    )
    op.create_index("ix_trial_usages_merchant_id", "trial_usages", ["merchant_id"], unique=False)

    op.create_table(
        "trial_daily_counts",
        sa.Column("endpoint_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("calls", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint("calls >= 0", name=op.f("ck_trial_daily_counts_calls_not_negative")),
        sa.ForeignKeyConstraint(
            ["endpoint_id"],
            ["endpoints.id"],
            name=op.f("fk_trial_daily_counts_endpoint_id_endpoints"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("endpoint_id", "day", name=op.f("pk_trial_daily_counts")),
    )

    op.create_table(
        "trial_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("usage_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("endpoint_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("nonce", sa.String(length=32), nullable=False),
        sa.Column("payer_address", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=10), server_default="used", nullable=False),
        sa.Column("request_method", sa.String(length=10), nullable=False),
        sa.Column("request_path", sa.Text(), nullable=False),
        sa.Column("body_hash", sa.String(length=64), nullable=True),
        sa.Column("upstream_status_code", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("nonce ~ '^[0-9a-f]{32}$'", name=op.f("ck_trial_calls_nonce_hex32")),
        sa.CheckConstraint("body_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_trial_calls_body_hash_hex64")),
        sa.CheckConstraint("status IN ('used', 'refunded')", name=op.f("ck_trial_calls_status_known")),
        sa.CheckConstraint(
            "(status = 'refunded') = (refunded_at IS NOT NULL)", name=op.f("ck_trial_calls_refunded_at_iff_refunded")
        ),
        sa.ForeignKeyConstraint(
            ["usage_id"], ["trial_usages.id"], name=op.f("fk_trial_calls_usage_id_trial_usages"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["endpoint_id"], ["endpoints.id"], name=op.f("fk_trial_calls_endpoint_id_endpoints"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], name=op.f("fk_trial_calls_merchant_id_merchants"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trial_calls")),
    )
    op.create_index("ix_trial_calls_nonce", "trial_calls", ["nonce"], unique=True)
    op.create_index("ix_trial_calls_usage_id", "trial_calls", ["usage_id"], unique=False)
    op.create_index("ix_trial_calls_endpoint_created", "trial_calls", ["endpoint_id", "created_at"], unique=False)
    op.create_index("ix_trial_calls_merchant_created", "trial_calls", ["merchant_id", "created_at"], unique=False)

    op.create_table(
        "webhooks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=True),
        sa.Column("event_types", postgresql.ARRAY(sa.String(length=40)), nullable=False),
        sa.Column("secret_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("secret_prefix", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("cardinality(event_types) > 0", name=op.f("ck_webhooks_event_types_not_empty")),
        sa.CheckConstraint("url LIKE 'https://%'", name=op.f("ck_webhooks_url_https")),
        sa.ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], name=op.f("fk_webhooks_merchant_id_merchants"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhooks")),
    )
    op.create_index("ix_webhooks_merchant_id", "webhooks", ["merchant_id"], unique=False)

    op.create_table(
        "webhook_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("event_id ~ '^evt_[0-9a-f]{32}$'", name=op.f("ck_webhook_events_event_id_format")),
        sa.CheckConstraint(f"event_type IN ({EVENT_TYPES_SQL})", name=op.f("ck_webhook_events_event_type_known")),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name=op.f("fk_webhook_events_merchant_id_merchants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_events")),
    )
    op.create_index("ix_webhook_events_event_id", "webhook_events", ["event_id"], unique=True)
    op.create_index("ix_webhook_events_merchant_created", "webhook_events", ["merchant_id", "created_at"], unique=False)

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_pk", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("webhook_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("merchant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=12), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'delivering', 'succeeded', 'dead')", name=op.f("ck_webhook_deliveries_status_known")
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_webhook_deliveries_attempts_not_negative")),
        sa.CheckConstraint(
            "(status = 'succeeded') = (delivered_at IS NOT NULL)",
            name=op.f("ck_webhook_deliveries_delivered_at_iff_succeeded"),
        ),
        sa.ForeignKeyConstraint(
            ["event_pk"],
            ["webhook_events.id"],
            name=op.f("fk_webhook_deliveries_event_pk_webhook_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["webhook_id"], ["webhooks.id"], name=op.f("fk_webhook_deliveries_webhook_id_webhooks"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name=op.f("fk_webhook_deliveries_merchant_id_merchants"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_deliveries")),
    )
    op.create_index(
        "ix_webhook_deliveries_event_pk_webhook_id", "webhook_deliveries", ["event_pk", "webhook_id"], unique=True
    )
    op.create_index(
        "ix_webhook_deliveries_webhook_created", "webhook_deliveries", ["webhook_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_webhook_deliveries_merchant_created", "webhook_deliveries", ["merchant_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_webhook_deliveries_due",
        "webhook_deliveries",
        ["next_attempt_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('pending', 'delivering')"),
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM trial_calls) OR EXISTS (SELECT 1 FROM webhooks) "
        "OR EXISTS (SELECT 1 FROM endpoints WHERE free_trial_calls > 0) THEN "
        "RAISE EXCEPTION 'trial calls, trial-enabled endpoints or webhooks exist; export them before "
        "downgrading below 0007_trials_webhooks'; END IF; END $$"
    )
    op.drop_index("ix_webhook_deliveries_due", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_merchant_created", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_webhook_created", table_name="webhook_deliveries")
    op.drop_index("ix_webhook_deliveries_event_pk_webhook_id", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("ix_webhook_events_merchant_created", table_name="webhook_events")
    op.drop_index("ix_webhook_events_event_id", table_name="webhook_events")
    op.drop_table("webhook_events")
    op.drop_index("ix_webhooks_merchant_id", table_name="webhooks")
    op.drop_table("webhooks")
    op.drop_index("ix_trial_calls_merchant_created", table_name="trial_calls")
    op.drop_index("ix_trial_calls_endpoint_created", table_name="trial_calls")
    op.drop_index("ix_trial_calls_usage_id", table_name="trial_calls")
    op.drop_index("ix_trial_calls_nonce", table_name="trial_calls")
    op.drop_table("trial_calls")
    op.drop_table("trial_daily_counts")
    op.drop_index("ix_trial_usages_merchant_id", table_name="trial_usages")
    op.drop_index("ix_trial_usages_endpoint_id_payer_public_key", table_name="trial_usages")
    op.drop_table("trial_usages")
    op.drop_constraint(op.f("ck_endpoints_trial_daily_cap_range"), "endpoints", type_="check")
    op.drop_constraint(op.f("ck_endpoints_free_trial_calls_range"), "endpoints", type_="check")
    op.drop_column("endpoints", "trial_daily_cap")
    op.drop_column("endpoints", "free_trial_calls")
