"""Prepaid ledger movements that are not deposits or call debits (CONTRACT v2.13), and the worker that runs them.

* Refund payouts: `finalize_refund_paid` is the ONLY place a refund leaves a balance. It runs once the merchant's
  payout transaction (merchant -> buyer, no platform fee, no treasury output) has been seen on-chain, in one
  transaction: the refund row becomes `paid`, the hold and the balance drop by the amount, `refunded_sompi` grows, a
  `prepaid_ledger_entries` row records the payout `tx_id`, and the `prepaid.refund.paid` event is queued.
* Expiry: a balance whose `expires_at` has passed loses its spendable credit (balance minus any open refund hold,
  which stays payable). `expired_sompi` grows, a ledger row and a `prepaid.credit.expired` event are written.
* `LedgerWorker` runs both on a timer: it expires due balances and re-checks payouts the node could not see yet.

KasGate holds no coins in any of these steps; they only change the off-chain record of what the merchant owes.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.deps import PaymentVerifier
from app.models import (
    LEDGER_KIND_EXPIRY,
    LEDGER_KIND_REFUND,
    PREPAID_REFUND_OPEN_STATUSES,
    PREPAID_REFUND_PAID,
    PREPAID_REFUND_PAYOUT_SUBMITTED,
    PREPAID_REFUND_REQUESTED,
    PrepaidBalance,
    PrepaidLedgerEntry,
    PrepaidRefund,
)
from app.payments import PaymentVerificationError, RpcUnavailableError
from app.webhooks import enqueue_event

logger = logging.getLogger("kasgate.ledger")

MAX_ERROR_CHARS: Final = 300


def clip(message: str) -> str:
    return message if len(message) <= MAX_ERROR_CHARS else message[: MAX_ERROR_CHARS - 1] + "…"


def spendable_sompi(balance: PrepaidBalance | None, now: datetime | None = None) -> int:
    """Credit calls can be paid from right now: balance minus the open refund hold, none once expired."""
    if balance is None:
        return 0
    if balance.expires_at is not None and balance.expires_at <= (now or datetime.now(UTC)):
        return 0
    return max(0, balance.balance_sompi - balance.refund_hold_sompi)


def refund_event_data(refund: PrepaidRefund, balance_sompi: int | None = None) -> dict[str, str | None]:
    data: dict[str, str | None] = {
        "refund_id": str(refund.id),
        "payer_address": refund.payer_address,
        "amount_sompi": str(refund.amount_sompi),
        "status": refund.status,
        "tx_id": refund.tx_id,
        "reason": refund.reason,
    }
    if balance_sompi is not None:
        data["balance_sompi"] = str(balance_sompi)
    return data


@dataclass(frozen=True)
class PaidRefund:
    refund: PrepaidRefund
    balance_sompi: int


async def finalize_refund_paid(session: AsyncSession, refund_id: uuid.UUID, tx_id: str) -> PaidRefund | None:
    """Mark an OPEN refund paid by `tx_id` and debit its balance; None when it was not open any more.

    Commits. Raises IntegrityError (the caller rolls back) when `tx_id` already paid another refund.
    """
    now = datetime.now(UTC)
    refund = (
        await session.execute(
            update(PrepaidRefund)
            .where(PrepaidRefund.id == refund_id, PrepaidRefund.status.in_(PREPAID_REFUND_OPEN_STATUSES))
            .values(
                status=PREPAID_REFUND_PAID,
                tx_id=tx_id,
                paid_at=now,
                resolved_at=now,
                payout_submitted_at=None,
                last_error=None,
                updated_at=func.now(),
            )
            .returning(PrepaidRefund)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if refund is None:
        await session.rollback()
        return None
    amount = refund.amount_sompi
    balance = (
        await session.execute(
            update(PrepaidBalance)
            .where(PrepaidBalance.id == refund.balance_id)
            .values(
                balance_sompi=PrepaidBalance.balance_sompi - amount,
                refund_hold_sompi=PrepaidBalance.refund_hold_sompi - amount,
                refunded_sompi=PrepaidBalance.refunded_sompi + amount,
                updated_at=func.now(),
            )
            .returning(PrepaidBalance.balance_sompi)
        )
    ).scalar_one()
    session.add(
        PrepaidLedgerEntry(
            id=uuid.uuid4(),
            balance_id=refund.balance_id,
            merchant_id=refund.merchant_id,
            payer_address=refund.payer_address,
            kind=LEDGER_KIND_REFUND,
            direction="debit",
            amount_sompi=amount,
            balance_after_sompi=int(balance),
            tx_id=tx_id,
            refund_id=refund.id,
            note="refund paid on-chain by the merchant",
            created_at=now,
        )
    )
    await session.flush()
    await enqueue_event(
        session,
        merchant_id=refund.merchant_id,
        event_type="prepaid.refund.paid",
        data=refund_event_data(refund, int(balance)),
        source_id=str(refund.id),
    )
    await session.commit()
    return PaidRefund(refund=refund, balance_sompi=int(balance))


async def expire_balance(session: AsyncSession, balance_id: uuid.UUID) -> int:
    """Expire the spendable credit of one balance if its `expires_at` has passed; return the sompi expired.

    Runs inside the caller's transaction (the caller commits). The conditional UPDATE re-checks the expiry and the
    spendable amount under the row lock, so concurrent workers or a concurrent deposit never expire twice.
    """
    row = (
        await session.execute(
            select(PrepaidBalance)
            .where(
                PrepaidBalance.id == balance_id,
                PrepaidBalance.expires_at.is_not(None),
                PrepaidBalance.expires_at <= func.now(),
                PrepaidBalance.balance_sompi > PrepaidBalance.refund_hold_sompi,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        return 0
    amount = row.balance_sompi - row.refund_hold_sompi
    balance = (
        await session.execute(
            update(PrepaidBalance)
            .where(PrepaidBalance.id == balance_id)
            .values(
                balance_sompi=PrepaidBalance.balance_sompi - amount,
                expired_sompi=PrepaidBalance.expired_sompi + amount,
                updated_at=func.now(),
            )
            .returning(PrepaidBalance.balance_sompi)
        )
    ).scalar_one()
    entry_id = uuid.uuid4()
    expired_at = row.expires_at
    session.add(
        PrepaidLedgerEntry(
            id=entry_id,
            balance_id=balance_id,
            merchant_id=row.merchant_id,
            payer_address=row.payer_address,
            kind=LEDGER_KIND_EXPIRY,
            direction="debit",
            amount_sompi=amount,
            balance_after_sompi=int(balance),
            tx_id=None,
            refund_id=None,
            note=None if expired_at is None else f"credit expired at {expired_at.isoformat()}",
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()
    await enqueue_event(
        session,
        merchant_id=row.merchant_id,
        event_type="prepaid.credit.expired",
        data={
            "ledger_entry_id": str(entry_id),
            "payer_address": row.payer_address,
            "expired_sompi": str(amount),
            "balance_sompi": str(int(balance)),
            "refund_hold_sompi": str(row.refund_hold_sompi),
            "expired_at": None if expired_at is None else expired_at.isoformat(),
        },
        source_id=str(entry_id),
    )
    return amount


async def expire_balance_for(session: AsyncSession, merchant_id: uuid.UUID, public_key: str) -> int:
    """`expire_balance` for the (merchant, buyer key) balance, if it exists. Inside the caller's transaction."""
    balance_id = (
        await session.execute(
            select(PrepaidBalance.id).where(
                PrepaidBalance.merchant_id == merchant_id, PrepaidBalance.payer_public_key == public_key
            )
        )
    ).scalar_one_or_none()
    return 0 if balance_id is None else await expire_balance(session, balance_id)


async def expire_due_balances(session: AsyncSession, limit: int) -> int:
    """Expire up to `limit` due balances (FOR UPDATE SKIP LOCKED, so replicas share the work). Commits."""
    ids = list(
        (
            await session.execute(
                select(PrepaidBalance.id)
                .where(
                    PrepaidBalance.expires_at.is_not(None),
                    PrepaidBalance.expires_at <= func.now(),
                    PrepaidBalance.balance_sompi > PrepaidBalance.refund_hold_sompi,
                )
                .order_by(PrepaidBalance.expires_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).scalars()
    )
    expired = 0
    for balance_id in ids:
        if await expire_balance(session, balance_id):
            expired += 1
    await session.commit()
    return expired


async def revert_payout(session: AsyncSession, refund_id: uuid.UUID, error: str) -> bool:
    """A submitted payout that cannot be confirmed goes back to `requested` (the hold stays). Commits."""
    reverted = (
        await session.execute(
            update(PrepaidRefund)
            .where(PrepaidRefund.id == refund_id, PrepaidRefund.status == PREPAID_REFUND_PAYOUT_SUBMITTED)
            .values(
                status=PREPAID_REFUND_REQUESTED,
                tx_id=None,
                payout_submitted_at=None,
                last_error=clip(error),
                updated_at=func.now(),
            )
            .returning(PrepaidRefund.id)
        )
    ).scalar_one_or_none()
    await session.commit()
    return reverted is not None


async def recheck_submitted_payouts(
    maker: async_sessionmaker[AsyncSession], engine: PaymentVerifier, limit: int
) -> int:
    """Look again for payouts the node could not see when they were submitted; returns how many became paid."""
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(seconds=settings.prepaid_payout_pending_max_seconds)
    async with maker() as session:
        pending = list(
            (
                await session.execute(
                    select(PrepaidRefund)
                    .where(and_(PrepaidRefund.status == PREPAID_REFUND_PAYOUT_SUBMITTED))
                    .order_by(PrepaidRefund.payout_submitted_at)
                    .limit(limit)
                )
            ).scalars()
        )
    paid = 0
    for refund in pending:
        tx_id = refund.tx_id
        if tx_id is None:
            continue
        try:
            await engine.verify_payout(tx_id, refund.payer_address, refund.amount_sompi)
        except RpcUnavailableError as exc:
            logger.warning("payout %s for refund %s not re-checked: %s", tx_id, refund.id, exc.message)
            continue
        except PaymentVerificationError as exc:
            submitted = refund.payout_submitted_at
            if exc.code == "payment_not_found" and submitted is not None and submitted > cutoff:
                continue
            async with maker() as session:
                await revert_payout(session, refund.id, f"{exc.code}: {exc.message}")
            continue
        async with maker() as session:
            if await finalize_refund_paid(session, refund.id, tx_id) is not None:
                paid += 1
    return paid


async def run_ledger_pass(maker: async_sessionmaker[AsyncSession], engine: PaymentVerifier) -> int:
    """One worker pass: expire due balances, then re-check submitted payouts. Returns the rows it changed."""
    limit = get_settings().ledger_batch_size
    async with maker() as session:
        changed = await expire_due_balances(session, limit)
    changed += await recheck_submitted_payouts(maker, engine, limit)
    return changed


class LedgerWorker:
    """Background prepaid-ledger worker started by the app lifespan (safe to run on several replicas)."""

    def __init__(self, maker: async_sessionmaker[AsyncSession], engine: PaymentVerifier) -> None:
        self._maker = maker
        self._engine = engine
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="kasgate-ledger-worker")

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                logger.info("ledger worker stopped")
            self._task = None

    async def _run(self) -> None:
        interval = get_settings().ledger_poll_interval_seconds
        while not self._stopping:
            try:
                await run_ledger_pass(self._maker, self._engine)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ledger worker pass failed")
            await asyncio.sleep(interval)
