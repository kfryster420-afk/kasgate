"""Prepaid ledger movements that are not deposits or call debits (CONTRACT v2.13), and the worker that runs them.

* Refund payouts: `finalize_payout` is the ONLY place a refund leaves a balance. It runs once an attempted payout
  transaction (merchant -> buyer, no platform fee, no treasury output, payload `kasgate:v1:refund:<id>`) is accepted
  on-chain, in one transaction: the attempt is `confirmed`, the refund row becomes `paid`, the hold and the balance
  drop by the amount, `refunded_sompi` grows, a `prepaid_ledger_entries` row records the payout `tx_id`, and the
  `prepaid.refund.paid` event is queued. A payout that lands after a cancel/reject is still recorded (re-debit).
* Every attempted payout tx_id is a `prepaid_refund_payouts` row. Only a definite `invalid_payout` resolves one as
  invalid; node errors are retried with backoff; an unseen attempt is re-checked until it is abandoned.
* Expiry: a balance whose `expires_at` has passed loses its spendable credit (balance minus any open refund hold,
  which stays payable). `expired_sompi` grows, a ledger row and a `prepaid.credit.expired` event are written.
* Cooldown holds: a refund the merchant had claimed and the buyer then cancelled keeps its amount held until
  `hold_until` (`prepaid_refund_cancel_cooldown_seconds`); `release_cooldown_holds` then makes it spendable.
* `LedgerWorker` runs all of these on a timer: it expires due balances, releases due cooldown holds and re-checks
  pending payout attempts.

KasGate holds no coins in any of these steps; they only change the off-chain record of what the merchant owes.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy import ColumnElement, and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.deps import PaymentVerifier
from app.models import (
    LEDGER_KIND_EXPIRY,
    LEDGER_KIND_REFUND,
    PAYOUT_ATTEMPT_ABANDONED,
    PAYOUT_ATTEMPT_CONFIRMED,
    PAYOUT_ATTEMPT_DUPLICATE,
    PAYOUT_ATTEMPT_INVALID,
    PAYOUT_ATTEMPT_PENDING,
    PREPAID_REFUND_CANCELLED,
    PREPAID_REFUND_OPEN_STATUSES,
    PREPAID_REFUND_PAID,
    PREPAID_REFUND_PAYING,
    PREPAID_REFUND_PAYOUT_SUBMITTED,
    PREPAID_REFUND_REJECTED,
    PREPAID_REFUND_REQUESTED,
    PrepaidBalance,
    PrepaidLedgerEntry,
    PrepaidRefund,
    PrepaidRefundPayout,
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


def claim_live(now: datetime | None = None) -> ColumnElement[bool]:
    """SQL: the refund is `paying` and its claim has not lapsed (claimed less than the claim TTL ago, and within the
    total claim window counted from `first_claimed_at`)."""
    settings = get_settings()
    moment = now or datetime.now(UTC)
    return and_(
        PrepaidRefund.status == PREPAID_REFUND_PAYING,
        PrepaidRefund.claimed_at > moment - timedelta(seconds=settings.prepaid_refund_claim_ttl_seconds),
        PrepaidRefund.first_claimed_at > moment - timedelta(seconds=settings.prepaid_refund_claim_max_seconds),
    )


def claim_lapsed(now: datetime | None = None) -> ColumnElement[bool]:
    """SQL: the refund is `paying` but its claim lapsed (TTL since the last claim, or the total claim window)."""
    settings = get_settings()
    moment = now or datetime.now(UTC)
    return and_(
        PrepaidRefund.status == PREPAID_REFUND_PAYING,
        or_(
            PrepaidRefund.claimed_at <= moment - timedelta(seconds=settings.prepaid_refund_claim_ttl_seconds),
            PrepaidRefund.first_claimed_at <= moment - timedelta(seconds=settings.prepaid_refund_claim_max_seconds),
        ),
    )


def refund_event_data(refund: PrepaidRefund, balance_sompi: int | None = None) -> dict[str, str | None]:
    data: dict[str, str | None] = {
        "refund_id": str(refund.id),
        "payer_address": refund.payer_address,
        "amount_sompi": str(refund.amount_sompi),
        "status": refund.status,
        "tx_id": refund.tx_id,
        "reason": refund.reason,
        "late_paid_from": refund.late_paid_from,
        "shortfall_sompi": str(refund.shortfall_sompi or 0),
        "hold_until": refund.hold_until.isoformat() if refund.hold_until is not None else None,
    }
    if balance_sompi is not None:
        data["balance_sompi"] = str(balance_sompi)
    return data


@dataclass(frozen=True)
class PaidRefund:
    refund: PrepaidRefund
    balance_sompi: int
    debited_sompi: int


async def _locked_attempt(session: AsyncSession, attempt_id: uuid.UUID) -> PrepaidRefundPayout | None:
    return (
        await session.execute(
            select(PrepaidRefundPayout)
            .where(PrepaidRefundPayout.id == attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _locked_refund(session: AsyncSession, refund_id: uuid.UUID) -> PrepaidRefund:
    return (
        await session.execute(
            select(PrepaidRefund)
            .where(PrepaidRefund.id == refund_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _supersede_open_refund(
    session: AsyncSession,
    paid_late: PrepaidRefund,
    tx_id: str,
    balance_row: PrepaidBalance,
    amount: int,
    now: datetime,
) -> int:
    """A late payout needs credit that a NEWER open refund of the same balance is holding: that refund (if the
    merchant has not started paying it) is closed as `rejected` with a reason and its hold released, so the buyer is
    not refunded twice. A newer refund the merchant is paying (`payout_submitted`, any pending attempt, or a `paying`
    claim that has not lapsed) is never touched: the difference becomes the late payout's shortfall instead.
    Returns the hold released (0 when nothing had to be closed or it could not be)."""
    if balance_row.balance_sompi - balance_row.refund_hold_sompi >= amount or balance_row.refund_hold_sompi == 0:
        return 0
    newer = (
        await session.execute(
            select(PrepaidRefund)
            .where(
                PrepaidRefund.balance_id == paid_late.balance_id,
                PrepaidRefund.id != paid_late.id,
                PrepaidRefund.status.in_(PREPAID_REFUND_OPEN_STATUSES),
                PrepaidRefund.status != PREPAID_REFUND_PAYOUT_SUBMITTED,
                ~claim_live(now),
                ~select(PrepaidRefundPayout.id)
                .where(
                    PrepaidRefundPayout.refund_id == PrepaidRefund.id,
                    PrepaidRefundPayout.status == PAYOUT_ATTEMPT_PENDING,
                )
                .exists(),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if newer is None:
        return 0
    newer.status = PREPAID_REFUND_REJECTED
    newer.reason = clip(
        f"closed by KasGate: the earlier refund {paid_late.id} was paid on-chain after all ({tx_id}); "
        "ask again for any credit left"
    )
    newer.resolved_at = now
    newer.claimed_at = None
    newer.updated_at = now
    await session.flush()
    await enqueue_event(
        session,
        merchant_id=newer.merchant_id,
        event_type="prepaid.refund.rejected",
        data=refund_event_data(newer, balance_row.balance_sompi),
        source_id=str(newer.id),
    )
    return newer.amount_sompi


async def finalize_payout(session: AsyncSession, attempt_id: uuid.UUID) -> PaidRefund | None:
    """Record a payout attempt the node confirmed (right outputs, right payload, accepted). Commits.

    * Refund still open: it becomes `paid`, the hold and the balance drop by the amount, `refunded_sompi` grows.
    * Refund already cancelled or rejected (the buyer cancelled, or the merchant rejected, while the coins were on
      their way): the coins reached the buyer anyway, so it is still recorded as `paid` with `late_paid_from`, and
      the balance is re-debited by up to the amount; a newer open refund of the same balance that is not being
      paid yet is closed (`rejected`, with a reason) so its hold can be taken; `shortfall_sompi` is what could not
      be debited (already spent or expired). The credit never stays spendable or refundable twice.
    * Refund already paid by this tx_id: idempotent (returns it). Paid by ANOTHER tx_id: the attempt is marked
      `duplicate` (the merchant paid twice; nothing moves on the ledger) and None is returned.

    Each path writes a ledger entry and the `prepaid.refund.paid` event once. Returns None when the attempt is no
    longer pending.
    """
    now = datetime.now(UTC)
    attempt = await _locked_attempt(session, attempt_id)
    if attempt is None or attempt.status not in (PAYOUT_ATTEMPT_PENDING, PAYOUT_ATTEMPT_CONFIRMED):
        await session.rollback()
        return None
    refund = await _locked_refund(session, attempt.refund_id)
    balance_row = (
        await session.execute(
            select(PrepaidBalance)
            .where(PrepaidBalance.id == refund.balance_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if refund.status == PREPAID_REFUND_PAID:
        if refund.tx_id == attempt.tx_id:
            if attempt.status == PAYOUT_ATTEMPT_PENDING:
                attempt.status, attempt.confirmed_at, attempt.resolved_at = PAYOUT_ATTEMPT_CONFIRMED, now, now
                attempt.updated_at = now
            await session.commit()
            return PaidRefund(refund=refund, balance_sompi=balance_row.balance_sompi, debited_sompi=0)
        attempt.status, attempt.confirmed_at, attempt.resolved_at = PAYOUT_ATTEMPT_DUPLICATE, now, now
        attempt.last_error = clip(f"refund was already paid by {refund.tx_id}; this transaction paid the buyer again")
        attempt.updated_at = now
        await session.commit()
        logger.warning("refund %s paid twice: %s after %s", refund.id, attempt.tx_id, refund.tx_id)
        return None
    if attempt.status != PAYOUT_ATTEMPT_PENDING:
        await session.rollback()
        return None
    amount = refund.amount_sompi
    late_from: str | None = None
    if refund.status in PREPAID_REFUND_OPEN_STATUSES:
        debit, hold_release = amount, amount
        note = "refund paid on-chain by the merchant"
    elif refund.hold_until is not None:
        # Cancelled after a claim and still in its cooldown: its own hold still covers the amount.
        late_from = refund.status
        debit, hold_release = amount, amount
        note = clip(f"late payout of a {late_from} refund within its cooldown hold: debited {amount} sompi")
    else:
        late_from = refund.status
        hold_release = await _supersede_open_refund(session, refund, attempt.tx_id, balance_row, amount, now)
        debit = min(amount, max(0, balance_row.balance_sompi - (balance_row.refund_hold_sompi - hold_release)))
        note = clip(
            f"late payout of a {late_from} refund: re-debited {debit} sompi"
            + (f", {amount - debit} sompi already spent or expired" if debit < amount else "")
        )
    balance = int(
        (
            await session.execute(
                update(PrepaidBalance)
                .where(PrepaidBalance.id == refund.balance_id)
                .values(
                    balance_sompi=PrepaidBalance.balance_sompi - debit,
                    refund_hold_sompi=PrepaidBalance.refund_hold_sompi - hold_release,
                    refunded_sompi=PrepaidBalance.refunded_sompi + debit,
                    updated_at=func.now(),
                )
                .returning(PrepaidBalance.balance_sompi)
            )
        ).scalar_one()
    )
    refund.status = PREPAID_REFUND_PAID
    refund.tx_id = attempt.tx_id
    refund.paid_at = now
    refund.resolved_at = now
    refund.payout_submitted_at = None
    refund.last_error = None
    refund.late_paid_from = late_from
    refund.shortfall_sompi = amount - debit
    refund.hold_until = None
    refund.updated_at = now
    attempt.status, attempt.confirmed_at, attempt.resolved_at = PAYOUT_ATTEMPT_CONFIRMED, now, now
    attempt.last_error = None
    attempt.updated_at = now
    session.add(
        PrepaidLedgerEntry(
            id=uuid.uuid4(),
            balance_id=refund.balance_id,
            merchant_id=refund.merchant_id,
            payer_address=refund.payer_address,
            kind=LEDGER_KIND_REFUND,
            direction="debit",
            amount_sompi=debit,
            balance_after_sompi=balance,
            tx_id=attempt.tx_id,
            refund_id=refund.id,
            note=note,
            created_at=now,
        )
    )
    await session.flush()
    await enqueue_event(
        session,
        merchant_id=refund.merchant_id,
        event_type="prepaid.refund.paid",
        data=refund_event_data(refund, balance),
        source_id=str(refund.id),
    )
    await session.commit()
    if late_from is not None:
        logger.warning("refund %s was %s but its payout %s landed; recorded late", refund.id, late_from, refund.tx_id)
    return PaidRefund(refund=refund, balance_sompi=balance, debited_sompi=debit)


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


async def _pending_attempts_besides(session: AsyncSession, refund_id: uuid.UUID, attempt_id: uuid.UUID) -> list[str]:
    return list(
        (
            await session.execute(
                select(PrepaidRefundPayout.tx_id)
                .where(
                    PrepaidRefundPayout.refund_id == refund_id,
                    PrepaidRefundPayout.status == PAYOUT_ATTEMPT_PENDING,
                    PrepaidRefundPayout.id != attempt_id,
                )
                .order_by(PrepaidRefundPayout.created_at.desc())
            )
        ).scalars()
    )


async def revert_payout(session: AsyncSession, refund_id: uuid.UUID, error: str) -> bool:
    """A `payout_submitted` refund goes back to `requested` (the hold stays; the merchant may pay again) and the
    `prepaid.refund.payout_reverted` event is queued. Attempted payouts stay on record and, while `pending`, keep
    being re-checked; the buyer cannot cancel until none is pending. Inside the caller's transaction (no commit)."""
    refund = (
        await session.execute(
            update(PrepaidRefund)
            .where(PrepaidRefund.id == refund_id, PrepaidRefund.status == PREPAID_REFUND_PAYOUT_SUBMITTED)
            .values(
                status=PREPAID_REFUND_REQUESTED,
                tx_id=None,
                payout_submitted_at=None,
                claimed_at=None,
                last_error=clip(error),
                updated_at=func.now(),
            )
            .returning(PrepaidRefund)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if refund is None:
        return False
    row = refund
    pending = await session.execute(
        select(PrepaidRefundPayout.tx_id).where(
            PrepaidRefundPayout.refund_id == refund_id, PrepaidRefundPayout.status == PAYOUT_ATTEMPT_PENDING
        )
    )
    await session.flush()
    await enqueue_event(
        session,
        merchant_id=row.merchant_id,
        event_type="prepaid.refund.payout_reverted",
        data={**refund_event_data(row), "pending_tx_ids": ",".join(pending.scalars()) or None, "error": clip(error)},
        source_id=f"{row.id}:{uuid.uuid4().hex}",
    )
    return True


async def mark_attempt_invalid(session: AsyncSession, attempt_id: uuid.UUID, error: str) -> bool:
    """The node proved this attempt is not the refund's payout (`invalid_payout`). It is resolved `invalid`; the
    refund goes back to `requested` (with `payout_reverted`) unless another attempt is still pending. Commits.
    Returns True when the refund was reverted."""
    attempt = await _locked_attempt(session, attempt_id)
    if attempt is None or attempt.status != PAYOUT_ATTEMPT_PENDING:
        await session.rollback()
        return False
    now = datetime.now(UTC)
    attempt.status, attempt.resolved_at, attempt.last_error = PAYOUT_ATTEMPT_INVALID, now, clip(error)
    attempt.check_count += 1
    attempt.updated_at = now
    await session.flush()
    refund = await _locked_refund(session, attempt.refund_id)
    reverted = False
    if refund.status == PREPAID_REFUND_PAYOUT_SUBMITTED:
        others = await _pending_attempts_besides(session, refund.id, attempt.id)
        if others:
            if refund.tx_id == attempt.tx_id:
                refund.tx_id = others[0]
            refund.last_error = clip(error)
            refund.updated_at = now
        else:
            reverted = await revert_payout(session, refund.id, error)
    await session.commit()
    return reverted


def unseen_backoff(created_at: datetime, now: datetime) -> timedelta:
    """Delay before re-checking an attempt that is not visible/accepted yet, growing with the attempt's age."""
    settings = get_settings()
    age = max(0.0, (now - created_at).total_seconds())
    return timedelta(
        seconds=min(age * settings.prepaid_payout_unseen_backoff_fraction, settings.prepaid_payout_backoff_max_seconds)
    )


async def note_attempt_unseen(
    session: AsyncSession, attempt_id: uuid.UUID, error: str, *, payload_seen: bool, may_revert: bool
) -> str:
    """The attempt is not confirmed yet (not visible, not accepted, or payload not proven). Commits.

    Re-checked after an age-based backoff: attempt age x `prepaid_payout_unseen_backoff_fraction`, capped at
    `prepaid_payout_backoff_max_seconds` (a fresh payout is re-checked at once, a days-old one rarely). With
    `may_revert` (the ledger worker): an attempt older than `prepaid_payout_attempt_max_seconds` is `abandoned`,
    and a refund `payout_submitted` for longer than `prepaid_payout_pending_max_seconds` reverts to `requested`
    while this attempt stays pending. Returns "pending", "reverted" or "abandoned".
    """
    settings = get_settings()
    now = datetime.now(UTC)
    attempt = await _locked_attempt(session, attempt_id)
    if attempt is None or attempt.status != PAYOUT_ATTEMPT_PENDING:
        await session.rollback()
        return "resolved"
    attempt.check_count += 1
    attempt.error_count = 0
    attempt.last_error = clip(error)
    attempt.next_check_at = now + unseen_backoff(attempt.created_at, now)
    attempt.updated_at = now
    if payload_seen:
        attempt.payload_verified = True
    outcome = "pending"
    refund = await _locked_refund(session, attempt.refund_id)
    if may_revert:
        if attempt.created_at <= now - timedelta(seconds=settings.prepaid_payout_attempt_max_seconds):
            attempt.status, attempt.resolved_at = PAYOUT_ATTEMPT_ABANDONED, now
            outcome = "abandoned"
            if refund.status == PREPAID_REFUND_PAYOUT_SUBMITTED and not await _pending_attempts_besides(
                session, refund.id, attempt.id
            ):
                await revert_payout(session, refund.id, f"payout {attempt.tx_id} abandoned: {error}")
        else:
            cutoff = now - timedelta(seconds=settings.prepaid_payout_pending_max_seconds)
            submitted = refund.payout_submitted_at
            if refund.status == PREPAID_REFUND_PAYOUT_SUBMITTED and submitted is not None and submitted <= cutoff:
                await session.flush()
                if await revert_payout(session, refund.id, f"{error} (not confirmed in time; pay again or wait)"):
                    outcome = "reverted"
    if refund.status == PREPAID_REFUND_PAYOUT_SUBMITTED and outcome == "pending":
        refund.last_error = clip(error)
        refund.updated_at = now
    await session.commit()
    return outcome


async def note_attempt_transient(session: AsyncSession, attempt_id: uuid.UUID, error: str) -> None:
    """A node error that proves nothing (outage, malformed reply): never revert; retry with backoff. Commits."""
    settings = get_settings()
    attempt = await _locked_attempt(session, attempt_id)
    if attempt is None or attempt.status != PAYOUT_ATTEMPT_PENDING:
        await session.rollback()
        return
    attempt.check_count += 1
    attempt.error_count += 1
    delay = min(
        settings.prepaid_payout_backoff_base_seconds * 2 ** min(attempt.error_count - 1, 30),
        settings.prepaid_payout_backoff_max_seconds,
    )
    now = datetime.now(UTC)
    attempt.next_check_at = now + timedelta(seconds=delay)
    attempt.last_error = clip(error)
    attempt.updated_at = now
    await session.execute(
        update(PrepaidRefund)
        .where(PrepaidRefund.id == attempt.refund_id, PrepaidRefund.status == PREPAID_REFUND_PAYOUT_SUBMITTED)
        .values(last_error=clip(error), updated_at=now)
    )
    await session.commit()


DEFINITE_INVALID_CODES: Final = frozenset({"invalid_payout", "wrong_network"})
UNSEEN_CODES: Final = frozenset({"payment_not_found", "payout_unconfirmed", "payload_unverified"})


@dataclass(frozen=True)
class PayoutCheck:
    """Outcome of one verification of an attempt: `paid`, `pending`, `invalid` or `transient` (+ the error)."""

    outcome: str
    code: str | None = None
    message: str | None = None
    paid: PaidRefund | None = None


async def check_attempt(
    session: AsyncSession,
    engine: PaymentVerifier,
    attempt: PrepaidRefundPayout,
    to_address: str,
    amount_sompi: int,
    *,
    may_revert: bool,
) -> PayoutCheck:
    """Verify one attempted payout on the node and record the result (the helpers commit `session`).

    Only a definite `invalid_payout`/`wrong_network` resolves the attempt as invalid; not visible / not accepted /
    payload not proven keeps it pending; anything else (outage, malformed node reply) is retried with backoff.
    """
    try:
        await engine.verify_payout(
            attempt.tx_id, to_address, amount_sompi, attempt.refund_id, payload_checked=attempt.payload_verified
        )
    except RpcUnavailableError as exc:
        await note_attempt_transient(session, attempt.id, f"node unavailable: {exc.message}")
        return PayoutCheck("transient", "rpc_unavailable", exc.message)
    except PaymentVerificationError as exc:
        text = f"{exc.code}: {exc.message}"
        if exc.code in DEFINITE_INVALID_CODES:
            await mark_attempt_invalid(session, attempt.id, text)
            return PayoutCheck("invalid", exc.code, exc.message)
        if exc.code in UNSEEN_CODES:
            await note_attempt_unseen(
                session, attempt.id, text, payload_seen=exc.code == "payout_unconfirmed", may_revert=may_revert
            )
            return PayoutCheck("pending", exc.code, exc.message)
        await note_attempt_transient(session, attempt.id, text)
        return PayoutCheck("transient", exc.code, exc.message)
    paid = await finalize_payout(session, attempt.id)
    return PayoutCheck("paid" if paid is not None else "pending", paid=paid)


async def recheck_payout_attempts(maker: async_sessionmaker[AsyncSession], engine: PaymentVerifier, limit: int) -> int:
    """Re-check every pending attempted payout that is due, even after its refund went back to `requested` or was
    cancelled meanwhile; returns how many became paid."""
    async with maker() as session:
        due = list(
            (
                await session.execute(
                    select(PrepaidRefundPayout, PrepaidRefund.payer_address, PrepaidRefund.amount_sompi)
                    .join(PrepaidRefund, PrepaidRefund.id == PrepaidRefundPayout.refund_id)
                    .where(
                        PrepaidRefundPayout.status == PAYOUT_ATTEMPT_PENDING,
                        PrepaidRefundPayout.next_check_at <= func.now(),
                    )
                    .order_by(PrepaidRefundPayout.next_check_at)
                    .limit(limit)
                )
            ).all()
        )
    paid = 0
    for attempt, address, amount in due:
        async with maker() as session:
            result = await check_attempt(session, engine, attempt, address, amount, may_revert=True)
        if result.paid is not None:
            paid += 1
    return paid


async def release_cooldown_holds(session: AsyncSession, limit: int) -> int:
    """Release the hold of cancelled refunds whose cooldown (`hold_until`) has passed: the amount becomes spendable
    again. Commits; safe on several replicas (SKIP LOCKED). Returns how many holds were released."""
    due = list(
        (
            await session.execute(
                select(PrepaidRefund)
                .where(
                    PrepaidRefund.status == PREPAID_REFUND_CANCELLED,
                    PrepaidRefund.hold_until.is_not(None),
                    PrepaidRefund.hold_until <= func.now(),
                )
                .order_by(PrepaidRefund.hold_until)
                .limit(limit)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    now = datetime.now(UTC)
    for refund in due:
        await session.execute(
            update(PrepaidBalance)
            .where(PrepaidBalance.id == refund.balance_id)
            .values(refund_hold_sompi=PrepaidBalance.refund_hold_sompi - refund.amount_sompi, updated_at=func.now())
        )
        refund.hold_until = None
        refund.updated_at = now
    await session.commit()
    return len(due)


async def run_ledger_pass(maker: async_sessionmaker[AsyncSession], engine: PaymentVerifier) -> int:
    """One worker pass: expire due balances, release due cooldown holds, then re-check submitted payouts. Returns
    the rows it changed."""
    limit = get_settings().ledger_batch_size
    async with maker() as session:
        changed = await expire_due_balances(session, limit)
    async with maker() as session:
        changed += await release_cooldown_holds(session, limit)
    changed += await recheck_payout_attempts(maker, engine, limit)
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
