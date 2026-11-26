"""Failed-delivery auto-credit: a paid call that the upstream never served becomes prepaid credit at that merchant.

KasGate settles on-chain BEFORE it calls the upstream. When the upstream then answers 5xx or not at all (timeout,
connection error, oversized response), the buyer may redeliver the same payment inside `redelivery_window_seconds`
and below `redelivery_max_attempts`. Once neither is possible any more and the last delivery still failed, the
merchant share of that payment (output 0) is credited to the buyer's prepaid balance with the same merchant:

* `attempts_exhausted`: the last allowed delivery failed (or redelivery is switched off). Credited right away on
  the failing response, which then carries `X-KasGate-Credit`.
* `window_expired`: the window plus `failed_delivery_credit_grace_seconds` (longer than one upstream attempt, so an
  in-flight redelivery has finished) has passed. Credited by the ledger worker's sweep.

Each credit is ONE transaction: the settlement row is locked, eligibility is re-checked, overdue credit expires
first (CONTRACT v2.13), the balance gains `credited_sompi` (and the deposit expiry policy), a
`prepaid_ledger_entries` row of kind `failed_delivery_credit` is written (unique per tx_id and per settlement, so a
second attempt is a no-op) and the signed `prepaid.credit.failed_delivery` webhook is queued in the outbox.

Nothing moves on-chain and KasGate holds nothing: the merchant was paid by the original transaction and the credit
is calls the merchant owes, like any prepaid balance. The treasury fee (output 1) is never reversed. Merchants can
opt out (`PUT /v1/prepaid/failed-delivery-policy`); a policy change is never applied retroactively. Payments with no
proven payer key (k402 `kaspa-utxo`, or a kaspa-exact-v2 payer that is not a P2PK Schnorr address) and prepaid
deposits are never credited.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import ColumnElement, and_, exists, func, literal, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.deps import get_db_session
from app.kaspa_types import payer_public_key, validate_kaspa_address
from app.ledger import credit_expiry, expire_balance_for
from app.models import (
    CREDIT_POLICY_ERRORS,
    CREDIT_POLICY_ERRORS_AND_TIMEOUTS,
    CREDIT_REASON_ATTEMPTS_EXHAUSTED,
    CREDIT_REASON_WINDOW_EXPIRED,
    DELIVERY_CREDITED,
    DELIVERY_FAILED,
    FAILURE_CONNECT_ERROR,
    FAILURE_TIMEOUT,
    FAILURE_UPSTREAM_5XX,
    LEDGER_KIND_FAILED_DELIVERY_CREDIT,
    Endpoint,
    Merchant,
    PrepaidBalance,
    PrepaidDeposit,
    PrepaidLedgerEntry,
    Settlement,
    SettlementStatus,
)
from app.schemas import (
    SLUG_RE,
    DeliveryFailureName,
    FailedDeliveryCreditReason,
    FailedDeliveryCreditReceipt,
    FailedDeliveryPolicyRead,
    FailedDeliveryPolicyUpdate,
    PaymentScheme,
    PrepaidCreditRead,
)
from app.security import get_current_merchant
from app.webhooks import enqueue_event

logger = logging.getLogger("kasgate.failed_delivery")

EVENT_TYPE: Final = "prepaid.credit.failed_delivery"
SHORTFALL_EVENT_TYPE: Final = "prepaid.credit.failed_delivery_shortfall"
SCHEME_K402: Final = "k402-kaspa-utxo"


# ---------------------------------------------------------------- policy and eligibility


def policy_enabled(merchant: Merchant) -> bool:
    """On while it was never switched off, or was switched back on after the last switch off."""
    disabled = merchant.failed_delivery_credit_disabled_at
    return disabled is None or disabled < merchant.failed_delivery_credit_enabled_at


def stamped_policy(merchant: Merchant) -> str | None:
    """The value stamped on a settlement that settles now: None = this payment is never credited."""
    if not policy_enabled(merchant):
        return None
    return CREDIT_POLICY_ERRORS_AND_TIMEOUTS if merchant.failed_delivery_credit_timeouts else CREDIT_POLICY_ERRORS


async def credit_policy_for(session: AsyncSession, merchant_id: uuid.UUID) -> str | None:
    """Settle-time stamp for `settlements.failed_delivery_credit_policy` (CONTRACT v2.14)."""
    merchant = (await session.execute(select(Merchant).where(Merchant.id == merchant_id))).scalar_one()
    return stamped_policy(merchant)


def creditable_failure(settlement: Settlement) -> bool:
    """The last delivery failed in a way the policy stamped at settle time credits.

    5xx answers and connection errors are credited; timeouts only under `errors_and_timeouts`; an oversized body
    (the upstream answered, the gateway refused to relay it) never. A NULL, `pending` or `delivered` outcome is
    never a credit: only an explicitly recorded failure is.
    """
    policy = settlement.failed_delivery_credit_policy
    if policy is None or settlement.delivery_outcome != DELIVERY_FAILED:
        return False
    failure = settlement.delivery_failure
    if failure == FAILURE_UPSTREAM_5XX:
        return settlement.upstream_status_code is not None and settlement.upstream_status_code >= 500
    if failure == FAILURE_CONNECT_ERROR:
        return True
    return failure == FAILURE_TIMEOUT and policy == CREDIT_POLICY_ERRORS_AND_TIMEOUTS


def _creditable_failure_sql() -> ColumnElement[bool]:
    return and_(
        Settlement.delivery_outcome == DELIVERY_FAILED,
        Settlement.failed_delivery_credit_policy.is_not(None),
        or_(
            and_(Settlement.delivery_failure == FAILURE_UPSTREAM_5XX, Settlement.upstream_status_code >= 500),
            Settlement.delivery_failure == FAILURE_CONNECT_ERROR,
            and_(
                Settlement.delivery_failure == FAILURE_TIMEOUT,
                Settlement.failed_delivery_credit_policy == CREDIT_POLICY_ERRORS_AND_TIMEOUTS,
            ),
        ),
    )


def redelivery_closed(settings: Settings, settlement: Settlement, now: datetime) -> FailedDeliveryCreditReason | None:
    """Why this settlement can no longer be redelivered, or None while a redelivery is still possible.

    `now` is the database clock (the redelivery claim checks its window on the same clock).
    """
    if settlement.delivery_attempts >= settings.redelivery_max_attempts or settings.redelivery_window_seconds == 0:
        return "attempts_exhausted"
    settled_at = settlement.settled_at
    if settled_at is None:
        return None
    closes = settled_at + timedelta(
        seconds=settings.redelivery_window_seconds + settings.failed_delivery_credit_wait_seconds
    )
    return "window_expired" if closes <= now else None


# ---------------------------------------------------------------- crediting


@dataclass(frozen=True)
class CreditOutcome:
    entry_id: uuid.UUID
    merchant_id: uuid.UUID
    tx_id: str
    credit_sompi: int
    shortfall_sompi: int
    balance_sompi: int
    reason: FailedDeliveryCreditReason

    def receipt(self) -> FailedDeliveryCreditReceipt | None:
        """The X-KasGate-Credit body, or None when the balance cap left nothing to credit."""
        if self.credit_sompi <= 0:
            return None
        return FailedDeliveryCreditReceipt(
            creditId=self.entry_id,
            kind="failed_delivery_credit",
            txId=self.tx_id,
            merchantId=self.merchant_id,
            creditSompi=str(self.credit_sompi),
            balanceSompi=str(self.balance_sompi),
            reason=self.reason,
            shortfallSompi=str(self.shortfall_sompi) if self.shortfall_sompi else None,
        )


def _credit_note(settlement: Settlement, reason: str, shortfall: int) -> str:
    if settlement.delivery_failure == FAILURE_UPSTREAM_5XX:
        status = f"HTTP {settlement.upstream_status_code}"
    elif settlement.delivery_failure == FAILURE_TIMEOUT:
        status = "timeout"
    else:
        status = "no response"
    attempts = settlement.delivery_attempts
    note = f"upstream {status} after {attempts} deliver{'y' if attempts == 1 else 'ies'} ({reason})"
    if shortfall:
        note += f"; {shortfall} sompi not credited (prepaid balance cap)"
    return note


async def credit_failed_delivery(
    session: AsyncSession, settlement_id: uuid.UUID, *, now: datetime | None = None
) -> CreditOutcome | None:
    """Credit one failed delivery if it is eligible now; None when it is not (or was already credited). Commits.

    One transaction: lock the settlement, re-check eligibility on the database clock, expire overdue credit, clamp
    the credit to PREPAID_MAX_BALANCE_SOMPI, raise the balance (credited + call-only credit_balance), write the
    ledger row (with any shortfall), mark the settlement `credited` and queue the signed webhook(s).
    Raises SQLAlchemyError only for unexpected database failures (the caller rolls back and logs).
    """
    settings = get_settings()
    row = (
        await session.execute(
            select(Settlement, Merchant, Endpoint, func.now())
            .join(Merchant, Merchant.id == Settlement.merchant_id)
            .join(Endpoint, Endpoint.id == Settlement.endpoint_id)
            .where(Settlement.id == settlement_id)
            .with_for_update(of=Settlement)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        await session.rollback()
        return None
    settlement, merchant, endpoint, db_now = row
    reason = _eligible_reason(settings, settlement, now or db_now)
    public_key = None if settlement.payer_address is None else payer_public_key(settlement.payer_address)
    if reason is None or public_key is None or settlement.payer_address is None:
        await session.rollback()
        return None
    already = (
        await session.execute(
            select(
                exists().where(PrepaidLedgerEntry.settlement_id == settlement.id)
                | exists().where(PrepaidDeposit.settlement_id == settlement.id)
            )
        )
    ).scalar_one()
    if already:
        await session.rollback()
        return None

    payer = settlement.payer_address
    share = settlement.merchant_amount_sompi
    await expire_balance_for(session, merchant.id, public_key)
    await session.execute(
        pg_insert(PrepaidBalance)
        .values(
            id=uuid.uuid4(),
            merchant_id=merchant.id,
            payer_address=payer,
            payer_public_key=public_key,
            balance_sompi=0,
            deposited_sompi=0,
            debited_sompi=0,
        )
        .on_conflict_do_nothing(index_elements=[PrepaidBalance.merchant_id, PrepaidBalance.payer_public_key])
    )
    balance_id, current = (
        await session.execute(
            select(PrepaidBalance.id, PrepaidBalance.balance_sompi)
            .where(PrepaidBalance.merchant_id == merchant.id, PrepaidBalance.payer_public_key == public_key)
            .with_for_update()
        )
    ).one()
    # CONTRACT v2.14: credits respect the prepaid balance cap; what does not fit is recorded as the shortfall.
    credit = max(0, min(share, settings.prepaid_max_balance_sompi - int(current)))
    shortfall = share - credit
    balance = int(current)
    if credit > 0:
        _new_expiry, next_expiry = await credit_expiry(session, endpoint)
        balance = (
            await session.execute(
                update(PrepaidBalance)
                .where(PrepaidBalance.id == balance_id)
                .values(
                    balance_sompi=PrepaidBalance.balance_sompi + credit,
                    credited_sompi=PrepaidBalance.credited_sompi + credit,
                    credit_balance_sompi=PrepaidBalance.credit_balance_sompi + credit,
                    expires_at=next_expiry,
                    updated_at=func.now(),
                )
                .returning(PrepaidBalance.balance_sompi)
            )
        ).scalar_one()
    entry = PrepaidLedgerEntry(
        id=uuid.uuid4(),
        balance_id=balance_id,
        merchant_id=merchant.id,
        payer_address=payer,
        kind=LEDGER_KIND_FAILED_DELIVERY_CREDIT,
        direction="credit",
        amount_sompi=credit,
        shortfall_sompi=shortfall,
        balance_after_sompi=int(balance),
        tx_id=settlement.tx_id,
        refund_id=None,
        note=_credit_note(settlement, reason, shortfall),
        settlement_id=settlement.id,
        slug=endpoint.slug,
        credit_reason=reason,
        created_at=datetime.now(UTC),
    )
    session.add(entry)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        logger.info("failed delivery of tx %s was already credited", settlement.tx_id)
        return None
    await session.execute(
        update(Settlement).where(Settlement.id == settlement.id).values(delivery_outcome=DELIVERY_CREDITED)
    )
    base = {
        "ledger_entry_id": str(entry.id),
        "kind": LEDGER_KIND_FAILED_DELIVERY_CREDIT,
        "settlement_id": str(settlement.id),
        "tx_id": settlement.tx_id,
        "payment_scheme": settlement.payment_scheme,
        "endpoint_id": str(endpoint.id),
        "endpoint_slug": endpoint.slug,
        "payer_address": payer,
        "amount_sompi": str(settlement.amount_sompi),
        "fee_sompi": str(settlement.fee_amount_sompi),
        "credit_sompi": str(credit),
        "shortfall_sompi": str(shortfall),
        "balance_sompi": str(int(balance)),
        "reason": reason,
        "delivery_failure": settlement.delivery_failure,
        "upstream_status_code": settlement.upstream_status_code,
        "delivery_attempts": settlement.delivery_attempts,
        "settled_at": None if settlement.settled_at is None else settlement.settled_at.isoformat(),
    }
    if credit > 0:
        await enqueue_event(session, merchant_id=merchant.id, event_type=EVENT_TYPE, data=base, source_id=str(entry.id))
    if shortfall > 0:
        await enqueue_event(
            session,
            merchant_id=merchant.id,
            event_type=SHORTFALL_EVENT_TYPE,
            data={
                **base,
                "merchant_share_sompi": str(share),
                "max_balance_sompi": str(settings.prepaid_max_balance_sompi),
            },
            source_id=str(entry.id),
        )
    await session.commit()
    logger.info(
        "credited %s sompi (shortfall %s) to %s at merchant %s for failed delivery of tx %s (%s)",
        credit,
        shortfall,
        payer,
        merchant.id,
        settlement.tx_id,
        reason,
    )
    return CreditOutcome(
        entry_id=entry.id,
        merchant_id=merchant.id,
        tx_id=settlement.tx_id,
        credit_sompi=credit,
        shortfall_sompi=shortfall,
        balance_sompi=int(balance),
        reason=reason,
    )


def _eligible_reason(settings: Settings, settlement: Settlement, now: datetime) -> FailedDeliveryCreditReason | None:
    if (
        settlement.status != SettlementStatus.SETTLED
        or settlement.settled_at is None
        or settlement.payment_scheme == SCHEME_K402
        or settlement.payer_address is None
        or settlement.merchant_amount_sompi <= 0
        or not creditable_failure(settlement)
    ):
        return None
    return redelivery_closed(settings, settlement, now)


async def credit_if_exhausted(session: AsyncSession, settlement_id: uuid.UUID) -> CreditOutcome | None:
    """Request path, right after a failed delivery was recorded: credit now if no redelivery is possible any more.

    Never raises: a database failure is logged and left to the sweeper, and the buyer's response is unaffected.
    """
    try:
        return await credit_failed_delivery(session, settlement_id)
    except Exception:
        await session.rollback()
        logger.exception("could not credit failed delivery of settlement %s; the sweeper will retry", settlement_id)
        return None


async def sweep_failed_deliveries(maker: async_sessionmaker[AsyncSession], *, limit: int) -> int:
    """Ledger-worker pass: credit every eligible failed delivery whose redelivery window has closed. Returns how many
    were credited. Walks the candidates in (settled_at, id) order, `limit` per query, inside the lookback horizon.
    Times are compared on the database clock, the same clock the redelivery claim uses."""
    settings = get_settings()
    closed_after = timedelta(seconds=settings.redelivery_window_seconds + settings.failed_delivery_credit_wait_seconds)
    lookback = timedelta(seconds=settings.failed_delivery_credit_lookback_seconds)
    credited = 0
    cursor: tuple[datetime, uuid.UUID] | None = None
    while True:
        query = (
            select(Settlement.id, Settlement.settled_at)
            .where(
                Settlement.status == SettlementStatus.SETTLED,
                _creditable_failure_sql(),
                Settlement.settled_at.is_not(None),
                Settlement.settled_at >= func.now() - lookback,
                Settlement.settled_at <= func.now() - closed_after,
                Settlement.payer_address.is_not(None),
                Settlement.payment_scheme != SCHEME_K402,
                ~exists().where(PrepaidLedgerEntry.settlement_id == Settlement.id),
                ~exists().where(PrepaidDeposit.settlement_id == Settlement.id),
            )
            .order_by(Settlement.settled_at, Settlement.id)
            .limit(limit)
        )
        if cursor is not None:
            query = query.where(
                tuple_(Settlement.settled_at, Settlement.id) > tuple_(literal(cursor[0]), literal(cursor[1]))
            )
        async with maker() as session:
            rows = [(row[0], row[1]) for row in (await session.execute(query)).all()]
        for settlement_id, _settled_at in rows:
            async with maker() as session:
                try:
                    if await credit_failed_delivery(session, settlement_id) is not None:
                        credited += 1
                except SQLAlchemyError:
                    await session.rollback()
                    logger.exception("could not credit failed delivery of settlement %s", settlement_id)
        if len(rows) < limit:
            return credited
        last_id, last_settled = rows[-1]
        if last_settled is None:
            return credited
        cursor = (last_settled, last_id)


async def sweep_pass(maker: async_sessionmaker[AsyncSession], limit: int) -> int:
    """`LedgerPass` adapter so the ledger worker runs the sweep on its timer."""
    return await sweep_failed_deliveries(maker, limit=limit)


# ---------------------------------------------------------------- merchant routes

router = APIRouter(prefix="/v1/prepaid", tags=["prepaid-credits"])
DbSession = Annotated[AsyncSession, Depends(get_db_session)]
CurrentMerchant = Annotated[Merchant, Depends(get_current_merchant)]


def _policy_read(merchant: Merchant) -> FailedDeliveryPolicyRead:
    return FailedDeliveryPolicyRead(
        enabled=policy_enabled(merchant),
        credit_timeouts=merchant.failed_delivery_credit_timeouts,
        enabled_at=merchant.failed_delivery_credit_enabled_at,
        disabled_at=merchant.failed_delivery_credit_disabled_at,
    )


@router.get("/failed-delivery-policy", response_model=FailedDeliveryPolicyRead)
async def read_policy(merchant: CurrentMerchant) -> FailedDeliveryPolicyRead:
    return _policy_read(merchant)


@router.put("/failed-delivery-policy", response_model=FailedDeliveryPolicyRead)
async def update_policy(
    payload: FailedDeliveryPolicyUpdate, merchant: CurrentMerchant, session: DbSession
) -> FailedDeliveryPolicyRead:
    """Opt in or out, and choose whether timeouts are credited. Applies to calls that SETTLE afterwards only: the
    policy is stamped on each settlement, so calls already paid keep the policy they were paid under."""
    changed = False
    if payload.enabled is not None and payload.enabled != policy_enabled(merchant):
        now = datetime.now(UTC)
        if payload.enabled:
            merchant.failed_delivery_credit_enabled_at = now
        else:
            merchant.failed_delivery_credit_disabled_at = now
        changed = True
    if payload.credit_timeouts is not None and payload.credit_timeouts != merchant.failed_delivery_credit_timeouts:
        merchant.failed_delivery_credit_timeouts = payload.credit_timeouts
        changed = True
    if changed:
        session.add(merchant)
        await session.commit()
        await session.refresh(merchant)
    return _policy_read(merchant)


@router.get("/credits", response_model=list[PrepaidCreditRead])
async def list_credits(
    merchant: CurrentMerchant,
    session: DbSession,
    slug: Annotated[str | None, Query(min_length=3, max_length=64, pattern=SLUG_RE)] = None,
    payer_address: Annotated[str | None, Query(min_length=10, max_length=100)] = None,
    tx_id: Annotated[str | None, Query(pattern=r"^[0-9a-f]{64}$")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PrepaidCreditRead]:
    """This merchant's failed-delivery credits, newest first, optionally filtered by endpoint slug, buyer or tx."""
    query = (
        select(PrepaidLedgerEntry, Settlement)
        .join(Settlement, Settlement.id == PrepaidLedgerEntry.settlement_id)
        .where(
            PrepaidLedgerEntry.merchant_id == merchant.id,
            PrepaidLedgerEntry.kind == LEDGER_KIND_FAILED_DELIVERY_CREDIT,
        )
    )
    if slug is not None:
        query = query.where(PrepaidLedgerEntry.slug == slug)
    if payer_address is not None:
        try:
            address = validate_kaspa_address(payer_address)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        query = query.where(PrepaidLedgerEntry.payer_address == address)
    if tx_id is not None:
        query = query.where(PrepaidLedgerEntry.tx_id == tx_id)
    rows = (
        await session.execute(
            query.order_by(PrepaidLedgerEntry.created_at.desc(), PrepaidLedgerEntry.id).limit(limit).offset(offset)
        )
    ).all()
    return [_credit_read(entry, settlement) for entry, settlement in rows]


def _credit_read(entry: PrepaidLedgerEntry, settlement: Settlement) -> PrepaidCreditRead:
    if entry.tx_id is None or entry.slug is None or entry.settlement_id is None or entry.credit_reason is None:
        raise ValueError(f"ledger entry {entry.id} is not a complete failed-delivery credit")
    if entry.credit_reason not in (CREDIT_REASON_ATTEMPTS_EXHAUSTED, CREDIT_REASON_WINDOW_EXPIRED):
        raise ValueError(f"ledger entry {entry.id} has an unknown credit reason")
    reason: FailedDeliveryCreditReason = (
        "attempts_exhausted" if entry.credit_reason == CREDIT_REASON_ATTEMPTS_EXHAUSTED else "window_expired"
    )
    failure: DeliveryFailureName
    if settlement.delivery_failure == FAILURE_UPSTREAM_5XX:
        failure = "upstream_5xx"
    elif settlement.delivery_failure == FAILURE_TIMEOUT:
        failure = "timeout"
    elif settlement.delivery_failure == FAILURE_CONNECT_ERROR:
        failure = "connect_error"
    else:
        raise ValueError(f"settlement {settlement.id} has no creditable delivery failure")
    scheme: PaymentScheme
    if settlement.payment_scheme == "kaspa-exact-v2":
        scheme = "kaspa-exact-v2"
    elif settlement.payment_scheme == SCHEME_K402:
        scheme = "k402-kaspa-utxo"
    else:
        scheme = "kaspa-exact-v1"
    return PrepaidCreditRead(
        id=entry.id,
        kind="failed_delivery_credit",
        tx_id=entry.tx_id,
        settlement_id=entry.settlement_id,
        endpoint_id=settlement.endpoint_id,
        slug=entry.slug,
        payer_address=entry.payer_address,
        payment_scheme=scheme,
        amount_sompi=str(settlement.amount_sompi),
        fee_sompi=str(settlement.fee_amount_sompi),
        credit_sompi=str(entry.amount_sompi),
        shortfall_sompi=str(entry.shortfall_sompi),
        balance_after_sompi=str(entry.balance_after_sompi),
        reason=reason,
        delivery_failure=failure,
        upstream_status_code=settlement.upstream_status_code,
        delivery_attempts=settlement.delivery_attempts,
        settled_at=settlement.settled_at,
        created_at=entry.created_at,
    )
