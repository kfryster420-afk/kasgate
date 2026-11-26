"""Orphaned in-flight prepaid debits: refund the ones whose request died before the upstream status was recorded.

A prepaid call debits first (`prepaid_debits.status = 'debited'`, `upstream_status_code IS NULL`) and records the
upstream status afterwards. If the worker is killed in between, the row stays "in flight" for ever: the buyer's
money is held and, because in-flight debits count against the prepaid cap, headroom is held too. This sweeper
refunds such rows once they are older than PREPAID_DEBIT_ORPHAN_AGE_SECONDS (validated to stay well above one upstream
attempt), by the same refund the proxy applies to an upstream 5xx (`refund_prepaid_debit`: row -> refunded, amount
(and its failed-delivery-credit part) back on the balance, no clamp).

Safe on several replicas and against live requests: every refund is ONE conditional UPDATE guarded by
`status = 'debited' AND upstream_status_code IS NULL AND created_at < now() - age` (database clock), so a row that
finished, was refunded by another sweeper, or is still young is simply not matched. No migration; the candidate scan
is a bounded read of the existing `prepaid_debits` table (see the RUNBOOK for an optional partial index).
"""

import asyncio
import logging
import uuid
from datetime import timedelta

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.models import PREPAID_DEBIT_DEBITED, PrepaidDebit
from app.proxy import refund_prepaid_debit

logger = logging.getLogger(__name__)


async def sweep_orphaned_debits(
    maker: async_sessionmaker[AsyncSession], redis: Redis, *, age_seconds: float, batch_size: int
) -> int:
    """One pass: refund up to `batch_size` orphaned debits older than `age_seconds`; returns how many were refunded."""
    async with maker() as session:
        ids: list[uuid.UUID] = list(
            (
                await session.execute(
                    select(PrepaidDebit.id)
                    .where(
                        PrepaidDebit.status == PREPAID_DEBIT_DEBITED,
                        PrepaidDebit.upstream_status_code.is_(None),
                        PrepaidDebit.created_at < func.now() - timedelta(seconds=age_seconds),
                    )
                    .order_by(PrepaidDebit.created_at)
                    .limit(batch_size)
                )
            ).scalars()
        )
    refunded = 0
    for debit_id in ids:
        async with maker() as session:
            if await refund_prepaid_debit(session, redis, debit_id, None, orphaned_for_seconds=age_seconds) is not None:
                refunded += 1
                logger.warning("refunded orphaned in-flight prepaid debit %s", debit_id)
    return refunded


class DebitSweeper:
    """Background task started by the app lifespan; cancelled cleanly on shutdown."""

    def __init__(self, maker: async_sessionmaker[AsyncSession], redis: Redis) -> None:
        self._maker = maker
        self._redis = redis
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="kasgate-debit-sweeper")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                logger.info("debit sweeper stopped")
            self._task = None

    async def _run(self) -> None:
        settings = get_settings()
        while True:
            try:
                await sweep_orphaned_debits(
                    self._maker,
                    self._redis,
                    age_seconds=settings.prepaid_debit_orphan_age_seconds,
                    batch_size=settings.prepaid_debit_sweeper_batch_size,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("debit sweeper pass failed")
            await asyncio.sleep(settings.prepaid_debit_sweeper_interval_seconds)
