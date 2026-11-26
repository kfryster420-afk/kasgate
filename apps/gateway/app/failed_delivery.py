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
from sqlalchemy import ColumnElement, and_, exists, func, literal, or_, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.deps import get_db_session
from app.kaspa_types import payer_public_key, validate_kaspa_address
from app.ledger import credit_expiry, expire_balance_for
from app.models import (
    CREDIT_REASON_ATTEMPTS_EXHAUSTED,
    CREDIT_REASON_WINDOW_EXPIRED,
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
SCHEME_K402: Final = "k402-kaspa-utxo"


# ---------------------------------------------------------------- policy and eligibility


def upstream_failed(status_code: int | None) -> bool:
    """No upstream answer (None) or a 5xx one."""
    return status_code is None or status_code >= 500


def policy_enabled(merchant: Merchant) -> bool:
    disabled = merchant.failed_delivery_credit_disabled_at
    return disabled is None or disabled < merchant.failed_delivery_credit_enabled_at


def policy_covers(merchant: Merchant, settled_at: datetime) -> bool:
    """True when the call settled inside the merchant's latest enabled interval."""
    if settled_at < merchant.failed_delivery_credit_enabled_at:
        return False
    disabled = merchant.failed_delivery_credit_disabled_at
    return policy_enabled(merchant) or disabled is None or settled_at < disabled


def _policy_covers_sql() -> ColumnElement[bool]:
    enabled_at = Merchant.failed_delivery_credit_enabled_at
    disabled_at = Merchant.failed_delivery_credit_disabled_at
    return and_(
        Settlement.settled_at >= enabled_at,
        or_(disabled_at.is_(None), disabled_at < enabled_at, Settlement.settled_at < disabled_at),
    )


def redelivery_closed(settings: Settings, settlement: Settlement, now: datetime) -> FailedDeliveryCreditReason | None:
    """Why this settlement can no longer be redelivered, or None while a redelivery is still possible."""
    if settlement.delivery_attempts >= settings.redelivery_max_attempts or settings.redelivery_window_seconds == 0:
        return "attempts_exhausted"
    settled_at = settlement.settled_at
    if settled_at is None:
        return None
    closes = settled_at + timedelta(
        seconds=settings.redelivery_window_seconds + settings.failed_delivery_credit_grace_seconds
    )
    return "window_expired" if closes <= now else None


# ---------------------------------------------------------------- crediting


@dataclass(frozen=True)
class CreditOutcome:
    entry_id: uuid.UUID
    merchant_id: uuid.UUID
    tx_id: str
    credit_sompi: int
    balance_sompi: int
    reason: FailedDeliveryCreditReason

    def receipt(self) -> FailedDeliveryCreditReceipt:
        return FailedDeliveryCreditReceipt(
            creditId=self.entry_id,
            kind="failed_delivery_credit",
            txId=self.tx_id,
            merchantId=self.merchant_id,
            creditSompi=str(self.credit_sompi),
            balanceSompi=str(self.balance_sompi),
            reason=self.reason,
        )


def _credit_note(settlement: Settlement, reason: str) -> str:
    status = "no response" if settlement.upstream_status_code is None else f"HTTP {settlement.upstream_status_code}"
    attempts = settlement.delivery_attempts
    return f"upstream {status} after {attempts} deliver{'y' if attempts == 1 else 'ies'} ({reason})"


async def credit_failed_delivery(
    session: AsyncSession, settlement_id: uuid.UUID, *, now: datetime | None = None
) -> CreditOutcome | None:
    """Credit one failed delivery if it is eligible now; None when it is not (or was already credited). Commits.

    Raises SQLAlchemyError only for unexpected database failures (the caller rolls back and logs).
    """
    settings = get_settings()
    current = now or datetime.now(UTC)
    row = (
        await session.execute(
            select(Settlement, Merchant, Endpoint)
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
    settlement, merchant, endpoint = row
    reason = _eligible_reason(settings, settlement, merchant, current)
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
    credit = settlement.merchant_amount_sompi
    await expire_balance_for(session, merchant.id, public_key)
    new_expiry, next_expiry = await credit_expiry(session, endpoint)
    balance_id, balance = (
        await session.execute(
            pg_insert(PrepaidBalance)
            .values(
                id=uuid.uuid4(),
                merchant_id=merchant.id,
                payer_address=payer,
                payer_public_key=public_key,
                balance_sompi=credit,
                deposited_sompi=0,
                debited_sompi=0,
                credited_sompi=credit,
                expires_at=new_expiry,
            )
            .on_conflict_do_update(
                index_elements=[PrepaidBalance.merchant_id, PrepaidBalance.payer_public_key],
                set_={
                    "balance_sompi": PrepaidBalance.balance_sompi + credit,
                    "credited_sompi": PrepaidBalance.credited_sompi + credit,
                    "expires_at": next_expiry,
                    "updated_at": func.now(),
                },
            )
            .returning(PrepaidBalance.id, PrepaidBalance.balance_sompi)
        )
    ).one()
    entry = PrepaidLedgerEntry(
        id=uuid.uuid4(),
        balance_id=balance_id,
        merchant_id=merchant.id,
        payer_address=payer,
        kind=LEDGER_KIND_FAILED_DELIVERY_CREDIT,
        direction="credit",
        amount_sompi=credit,
        balance_after_sompi=int(balance),
        tx_id=settlement.tx_id,
        refund_id=None,
        note=_credit_note(settlement, reason),
        settlement_id=settlement.id,
        slug=endpoint.slug,
        credit_reason=reason,
        created_at=current,
    )
    session.add(entry)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        logger.info("failed delivery of tx %s was already credited", settlement.tx_id)
        return None
    await enqueue_event(
        session,
        merchant_id=merchant.id,
        event_type=EVENT_TYPE,
        data={
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
            "balance_sompi": str(int(balance)),
            "reason": reason,
            "upstream_status_code": settlement.upstream_status_code,
            "delivery_attempts": settlement.delivery_attempts,
            "settled_at": None if settlement.settled_at is None else settlement.settled_at.isoformat(),
        },
        source_id=str(entry.id),
    )
    await session.commit()
    logger.info(
        "credited %s sompi to %s at merchant %s for failed delivery of tx %s (%s)",
        credit,
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
        balance_sompi=int(balance),
        reason=reason,
    )


def _eligible_reason(
    settings: Settings, settlement: Settlement, merchant: Merchant, now: datetime
) -> FailedDeliveryCreditReason | None:
    if (
        settlement.status != SettlementStatus.SETTLED
        or settlement.settled_at is None
        or settlement.payment_scheme == SCHEME_K402
        or settlement.payer_address is None
        or settlement.merchant_amount_sompi <= 0
        or not upstream_failed(settlement.upstream_status_code)
        or not policy_covers(merchant, settlement.settled_at)
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


async def sweep_failed_deliveries(
    maker: async_sessionmaker[AsyncSession], *, limit: int, now: datetime | None = None
) -> int:
    """Ledger-worker pass: credit every eligible failed delivery whose redelivery window has closed. Returns how many
    were credited. Walks the candidates in (settled_at, id) order, `limit` per query, inside the lookback horizon."""
    settings = get_settings()
    current = now or datetime.now(UTC)
    cutoff = current - timedelta(
        seconds=settings.redelivery_window_seconds + settings.failed_delivery_credit_grace_seconds
    )
    horizon = current - timedelta(seconds=settings.failed_delivery_credit_lookback_seconds)
    credited = 0
    cursor: tuple[datetime, uuid.UUID] | None = None
    while True:
        query = (
            select(Settlement.id, Settlement.settled_at)
            .join(Merchant, Merchant.id == Settlement.merchant_id)
            .where(
                Settlement.status == SettlementStatus.SETTLED,
                or_(Settlement.upstream_status_code.is_(None), Settlement.upstream_status_code >= 500),
                Settlement.settled_at.is_not(None),
                Settlement.settled_at >= horizon,
                Settlement.settled_at <= cutoff,
                Settlement.payer_address.is_not(None),
                Settlement.payment_scheme != SCHEME_K402,
                _policy_covers_sql(),
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
                    if await credit_failed_delivery(session, settlement_id, now=current) is not None:
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
    """Opt in or out of failed-delivery auto-credit. Setting the current state again changes nothing."""
    if payload.enabled != policy_enabled(merchant):
        now = datetime.now(UTC)
        if payload.enabled:
            merchant.failed_delivery_credit_enabled_at = now
        else:
            merchant.failed_delivery_credit_disabled_at = now
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
        balance_after_sompi=str(entry.balance_after_sompi),
        reason=reason,
        upstream_status_code=settlement.upstream_status_code,
        delivery_attempts=settlement.delivery_attempts,
        settled_at=settlement.settled_at,
        created_at=entry.created_at,
    )
