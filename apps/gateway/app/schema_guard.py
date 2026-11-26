"""Refuse to run on a database migrated with an earlier draft of revision 0008_prepaid_refunds.

Revision 0008 was revised in place before release (CONTRACT v2.13 refund hardening). A database migrated with an
earlier draft (15d7bd4 or 706855a) is stamped `0008_prepaid_refunds` too, so `alembic upgrade head` would not touch
it and the gateway would fail at runtime on refund claims, payouts and cancels. Both Alembic (`alembic/env.py`, on
upgrade or stamp) and the app lifespan call `stale_0008_problem` and stop with `STALE_0008_MESSAGE`.
"""

import logging
from typing import Final

from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)

REVISION_0008: Final = "0008_prepaid_refunds"
# Columns of prepaid_refunds that only the final 0008 creates (the earliest draft lacks all of them).
FINAL_0008_REFUND_COLUMNS: Final = frozenset({"claimed_at", "first_claimed_at", "hold_until", "late_paid_from"})

STALE_0008_MESSAGE: Final = (
    "this database was migrated with an earlier draft of revision 0008_prepaid_refunds (CONTRACT v2.13, before "
    "release) and lacks the final refund schema (prepaid_refund_payouts and/or prepaid_refunds.first_claimed_at, "
    "hold_until). `alembic upgrade head` cannot fix it because the revision id is the same. Take it back to 0007 "
    "and upgrade again: `alembic downgrade 0007_trials_webhooks && alembic upgrade head` (the downgrade refuses "
    "while refund or expiry data exists; on a testnet/dev database, export what you need and rebuild it). See "
    "docs/RUNBOOK.md, 'Migration 0008 revised in place'."
)


def stale_0008_problem(connection: Connection) -> str | None:
    """`STALE_0008_MESSAGE` when the schema is an earlier draft of 0008, else None (also before 0008 is applied)."""
    inspector = inspect(connection)
    if not inspector.has_table("prepaid_refunds"):
        return None
    columns = {column["name"] for column in inspector.get_columns("prepaid_refunds")}
    if not inspector.has_table("prepaid_refund_payouts") or not columns >= FINAL_0008_REFUND_COLUMNS:
        return STALE_0008_MESSAGE
    return None


async def check_database_schema(engine: AsyncEngine) -> None:
    """App startup: raise RuntimeError on an earlier-draft 0008 schema. An unreachable database is only logged
    (the gateway starts and reports it on /healthz, as before)."""
    try:
        async with engine.connect() as connection:
            problem = await connection.run_sync(stale_0008_problem)
    except (OSError, SQLAlchemyError):
        logger.warning("could not check the database schema at startup", exc_info=True)
        return
    if problem is not None:
        raise RuntimeError(problem)
