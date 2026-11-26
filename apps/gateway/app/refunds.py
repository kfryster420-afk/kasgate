"""Prepaid refunds (CONTRACT v2.13): buyers get unused credit back through a merchant-signed on-chain payout.

KasGate holds no keys and no coins. The deposit already paid the merchant on-chain, so a refund is an ordinary payment
FROM the merchant's own wallet TO the depositing address: no platform fee and no treasury output (money being
returned is never taxed). The gateway only keeps the record:

1. Buyer: ``POST /v1/prepaid/refunds/challenge`` (action ``request``, optional ``amount_sompi`` = all spendable credit
   when omitted) -> a single-use challenge; sign it with the depositing key; ``POST /v1/prepaid/refunds`` ->
   201 refund ``requested``. The amount moves into the balance's ``refund_hold_sompi`` (still owed, no longer
   spendable). At most one open request per balance; at least ``MIN_OUTPUT_SOMPI`` (a smaller payout output is not
   standard on Kaspa).
2. Merchant [Bearer]: ``POST /v1/prepaid/refunds/{id}/payout`` with the ``tx_id`` of a payment they broadcast (the
   dashboard uses KasWare ``sendKaspa``) or a signed ``transaction`` for the gateway to broadcast (it must carry the
   payload ``kasgate:v1:refund:<id hex>``). The node must show an output paying the depositing address EXACTLY the
   refund amount. Seen on-chain -> ``paid`` (the ledger is debited only now, see ``app.ledger``); not visible yet ->
   202 ``payout_submitted`` and the ledger worker checks again.
3. Merchant may ``reject`` a ``requested`` refund with a reason; the buyer may ``cancel`` it with a signed challenge.
   Both release the hold.

Every step queues a webhook event (``prepaid.refund.requested|paid|rejected|cancelled``).
"""

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.deps import PaymentVerifier, get_db_session, get_engine, get_redis
from app.kaspa_types import validate_kaspa_address
from app.ledger import clip, finalize_refund_paid, refund_event_data, spendable_sompi
from app.models import (
    PREPAID_REFUND_CANCELLED,
    PREPAID_REFUND_OPEN_STATUSES,
    PREPAID_REFUND_PAYOUT_SUBMITTED,
    PREPAID_REFUND_REJECTED,
    PREPAID_REFUND_REQUESTED,
    PREPAID_REFUND_STATUSES,
    Endpoint,
    Merchant,
    PrepaidBalance,
    PrepaidDeposit,
    PrepaidLedgerEntry,
    PrepaidRefund,
    Settlement,
)
from app.payments import PaymentVerificationError, RpcUnavailableError
from app.proxy import REPLAY_LOCK_TTL_SECONDS, _error, payer_public_key, verify_prepaid_signature
from app.schemas import (
    SLUG_RE,
    PrepaidLedgerEntryRead,
    PrepaidRefundChallenge,
    PrepaidRefundChallengeRequest,
    PrepaidRefundPayout,
    PrepaidRefundRead,
    PrepaidRefundReject,
    PrepaidRefundSigned,
    build_prepaid_refund_message,
    new_payment_nonce,
    refund_payout_payload,
)
from app.security import RATE_LIMIT_UNPAID_PROXY, auth_domain, get_current_merchant, limit_by_ip
from app.webhooks import enqueue_event

logger = logging.getLogger("kasgate.refunds")

router = APIRouter(prefix="/v1/prepaid", tags=["prepaid-refunds"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
CurrentMerchant = Annotated[Merchant, Depends(get_current_merchant)]
RedisDep = Annotated[Redis, Depends(get_redis)]
EngineDep = Annotated[PaymentVerifier, Depends(get_engine)]

REFUND_CHALLENGE_PREFIX: Final = "kasgate:prepaid:refund:challenge:"
REFUND_NONCE_LOCK_PREFIX: Final = "kasgate:prepaid:refund:noncelock:"
_HEX64: Final = re.compile(r"[0-9a-f]{64}")


def refund_challenge_key(nonce: str) -> str:
    return f"{REFUND_CHALLENGE_PREFIX}{nonce}"


def refund_nonce_lock_key(nonce: str) -> str:
    return f"{REFUND_NONCE_LOCK_PREFIX}{nonce}"


@dataclass(frozen=True)
class StoredRefundChallenge:
    action: str
    slug: str
    merchant_id: uuid.UUID
    payer_address: str
    payer_public_key: str
    amount_sompi: int
    refund_id: uuid.UUID | None
    expires_at: datetime

    def message(self, nonce: str) -> str:
        return build_prepaid_refund_message(
            domain=auth_domain(),
            action=self.action,
            address=self.payer_address,
            merchant_id=self.merchant_id,
            amount_sompi=self.amount_sompi,
            refund_id=self.refund_id,
            nonce=nonce,
            expires_at=self.expires_at,
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "action": self.action,
                "slug": self.slug,
                "merchantId": str(self.merchant_id),
                "payerAddress": self.payer_address,
                "payerPublicKey": self.payer_public_key,
                "amountSompi": str(self.amount_sompi),
                "refundId": None if self.refund_id is None else str(self.refund_id),
                "expiresAt": self.expires_at.isoformat(),
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "StoredRefundChallenge":
        data = json.loads(raw)
        action = str(data["action"])
        amount = int(data["amountSompi"])
        expires_at = datetime.fromisoformat(data["expiresAt"])
        public_key = str(data["payerPublicKey"])
        refund_id = data["refundId"]
        if (
            action not in ("request", "cancel")
            or amount <= 0
            or expires_at.tzinfo is None
            or not _HEX64.fullmatch(public_key)
            or (action == "cancel") != (refund_id is not None)
        ):
            raise ValueError("stored refund challenge is malformed")
        return cls(
            action=action,
            slug=str(data["slug"]),
            merchant_id=uuid.UUID(data["merchantId"]),
            payer_address=str(data["payerAddress"]),
            payer_public_key=public_key,
            amount_sompi=amount,
            refund_id=None if refund_id is None else uuid.UUID(refund_id),
            expires_at=expires_at,
        )


# ---------------------------------------------------------------- helpers


def _refund_json(refund: PrepaidRefund, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=PrepaidRefundRead.model_validate(refund).model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


def _store_unavailable() -> JSONResponse:
    return _error(503, "service_unavailable", "prepaid store is temporarily unavailable")


async def _endpoint_merchant(session: AsyncSession, slug: str) -> Endpoint | None:
    return (
        await session.execute(
            select(Endpoint)
            .join(Merchant, Merchant.id == Endpoint.merchant_id)
            .where(Endpoint.slug == slug, Endpoint.is_active.is_(True), Merchant.is_active.is_(True))
        )
    ).scalar_one_or_none()


async def _balance_of(session: AsyncSession, merchant_id: uuid.UUID, public_key: str) -> PrepaidBalance | None:
    return (
        await session.execute(
            select(PrepaidBalance).where(
                PrepaidBalance.merchant_id == merchant_id, PrepaidBalance.payer_public_key == public_key
            )
        )
    ).scalar_one_or_none()


async def _open_refund(session: AsyncSession, balance_id: uuid.UUID) -> PrepaidRefund | None:
    return (
        await session.execute(
            select(PrepaidRefund).where(
                PrepaidRefund.balance_id == balance_id, PrepaidRefund.status.in_(PREPAID_REFUND_OPEN_STATUSES)
            )
        )
    ).scalar_one_or_none()


async def _load_challenge(redis: Redis, nonce: str) -> StoredRefundChallenge | None:
    raw = await redis.get(refund_challenge_key(nonce))
    if raw is None:
        return None
    try:
        stored = StoredRefundChallenge.from_json(raw)
    except (ValueError, KeyError, TypeError, ArithmeticError):
        logger.error("stored refund challenge %s is unreadable", nonce, exc_info=True)
        return None
    return None if stored.expires_at <= datetime.now(UTC) else stored


async def _claim_signed(
    redis: Redis, signed: PrepaidRefundSigned, action: str
) -> tuple[StoredRefundChallenge | None, JSONResponse | None]:
    """Load, verify and claim (single use) a signed refund challenge. Returns (challenge, None) or (None, error)."""
    try:
        stored = await _load_challenge(redis, signed.nonce)
    except RedisError:
        logger.exception("refund challenge store unavailable")
        return None, _error(503, "service_unavailable", "prepaid refunds are temporarily unavailable")
    if stored is None:
        return None, _error(
            410, "challenge_expired", "the refund challenge is unknown or has expired; request a new one"
        )
    if stored.action != action:
        return None, _error(400, "invalid_signature", f"this challenge is for action '{stored.action}', not '{action}'")
    if payer_public_key(stored.payer_address) != stored.payer_public_key or not verify_prepaid_signature(
        stored.message(signed.nonce), signed.signature, stored.payer_public_key
    ):
        return None, _error(400, "invalid_signature", "signature is not by the depositing key over this challenge")
    try:
        claimed = bool(await redis.set(refund_nonce_lock_key(signed.nonce), "1", nx=True, ex=REPLAY_LOCK_TTL_SECONDS))
    except RedisError:
        logger.exception("refund replay lock unavailable")
        return None, _error(503, "service_unavailable", "prepaid refunds are temporarily unavailable")
    if not claimed:
        return None, _error(409, "challenge_replayed", "this refund challenge was already used")
    return stored, None


async def _consume_challenge(redis: Redis, nonce: str) -> None:
    try:
        await redis.getdel(refund_challenge_key(nonce))
    except RedisError:
        logger.warning("could not consume refund challenge %s; the nonce lock and unique keys still block reuse", nonce)


# ---------------------------------------------------------------- buyer routes


@router.post(
    "/refunds/challenge",
    status_code=status.HTTP_201_CREATED,
    response_model=PrepaidRefundChallenge,
    dependencies=[Depends(limit_by_ip(RATE_LIMIT_UNPAID_PROXY))],
)
async def refund_challenge(payload: PrepaidRefundChallengeRequest, session: DbSession, redis: RedisDep) -> Response:
    """The text to sign to open or cancel a refund of this key's unused credit at the merchant behind `slug`."""
    settings = get_settings()
    public_key = payer_public_key(payload.payer_address)
    if public_key is None:
        return _error(
            400,
            "invalid_payer",
            "payer_address must be a P2PK Schnorr address (kaspa:q...): only its key can sign refund requests",
        )
    try:
        endpoint = await _endpoint_merchant(session, payload.slug)
        if endpoint is None:
            return _error(404, "endpoint_not_found", f"no active endpoint with slug '{payload.slug}'")
        balance = await _balance_of(session, endpoint.merchant_id, public_key)
        open_refund = None if balance is None else await _open_refund(session, balance.id)
    except SQLAlchemyError:
        logger.exception("refund challenge lookup failed")
        return _store_unavailable()
    available = spendable_sompi(balance)
    if payload.action == "request":
        if open_refund is not None:
            return _error(409, "refund_already_open", f"refund {open_refund.id} is still open for this balance")
        amount = payload.amount_sompi if payload.amount_sompi is not None else available
        if amount < settings.min_output_sompi:
            return _error(
                422,
                "refund_too_small",
                f"a refund must be at least {settings.min_output_sompi} sompi (the smallest standard Kaspa output); "
                f"spendable credit is {available} sompi",
            )
        if amount > available:
            return _error(
                409,
                "insufficient_balance",
                f"the spendable prepaid credit ({available} sompi) does not cover a {amount} sompi refund",
            )
        refund_id = None
    else:
        if open_refund is None or open_refund.id != payload.refund_id:
            return _error(404, "refund_not_found", "no open refund with this id for this key at this merchant")
        if open_refund.status != PREPAID_REFUND_REQUESTED:
            return _error(409, "refund_not_cancellable", "the merchant already submitted a payout for this refund")
        amount = open_refund.amount_sompi
        refund_id = open_refund.id
    nonce = new_payment_nonce()
    stored = StoredRefundChallenge(
        action=payload.action,
        slug=endpoint.slug,
        merchant_id=endpoint.merchant_id,
        payer_address=payload.payer_address,
        payer_public_key=public_key,
        amount_sompi=amount,
        refund_id=refund_id,
        expires_at=datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=settings.payment_max_age_seconds),
    )
    try:
        await redis.set(refund_challenge_key(nonce), stored.to_json(), ex=settings.payment_max_age_seconds)
    except RedisError:
        logger.exception("refund challenge store unavailable")
        return _error(503, "service_unavailable", "prepaid refunds are temporarily unavailable")
    challenge = PrepaidRefundChallenge(
        nonce=nonce,
        message=stored.message(nonce),
        action=payload.action,
        slug=endpoint.slug,
        merchant_id=endpoint.merchant_id,
        payer_address=payload.payer_address,
        amount_sompi=str(amount),
        refund_id=refund_id,
        spendable_sompi=str(available),
        expires_at=stored.expires_at,
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=challenge.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


@router.post("/refunds", status_code=status.HTTP_201_CREATED, response_model=PrepaidRefundRead)
async def create_refund(signed: PrepaidRefundSigned, session: DbSession, redis: RedisDep) -> Response:
    """Open a refund request from a signed `request` challenge. Holds the amount; debits nothing yet."""
    stored, failure = await _claim_signed(redis, signed, "request")
    if failure is not None or stored is None:
        return failure or _store_unavailable()
    amount = stored.amount_sompi
    try:
        held = (
            await session.execute(
                update(PrepaidBalance)
                .where(
                    PrepaidBalance.merchant_id == stored.merchant_id,
                    PrepaidBalance.payer_public_key == stored.payer_public_key,
                    PrepaidBalance.balance_sompi - PrepaidBalance.refund_hold_sompi >= amount,
                    or_(PrepaidBalance.expires_at.is_(None), PrepaidBalance.expires_at > func.now()),
                )
                .values(refund_hold_sompi=PrepaidBalance.refund_hold_sompi + amount, updated_at=func.now())
                .returning(PrepaidBalance.id, PrepaidBalance.balance_sompi)
            )
        ).one_or_none()
        if held is None:
            await session.rollback()
            await _consume_challenge(redis, signed.nonce)
            return _error(
                409, "insufficient_balance", "the spendable prepaid credit no longer covers this refund; ask again"
            )
        balance_id, balance = held
        refund = PrepaidRefund(
            id=uuid.uuid4(),
            balance_id=balance_id,
            merchant_id=stored.merchant_id,
            payer_address=stored.payer_address,
            payer_public_key=stored.payer_public_key,
            amount_sompi=amount,
            status=PREPAID_REFUND_REQUESTED,
            request_nonce=signed.nonce,
        )
        session.add(refund)
        await session.flush()
        await enqueue_event(
            session,
            merchant_id=stored.merchant_id,
            event_type="prepaid.refund.requested",
            data={**refund_event_data(refund, int(balance)), "endpoint_slug": stored.slug},
            source_id=str(refund.id),
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        await _consume_challenge(redis, signed.nonce)
        return _error(409, "refund_already_open", "another refund is already open for this balance")
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("refund request could not be stored")
        return _store_unavailable()
    await _consume_challenge(redis, signed.nonce)
    await session.refresh(refund)
    return _refund_json(refund, status.HTTP_201_CREATED)


@router.post("/refunds/{refund_id}/cancel", response_model=PrepaidRefundRead)
async def cancel_refund(
    refund_id: uuid.UUID, signed: PrepaidRefundSigned, session: DbSession, redis: RedisDep
) -> Response:
    """The buyer withdraws a `requested` refund (signed `cancel` challenge); the hold goes back to spendable."""
    stored, failure = await _claim_signed(redis, signed, "cancel")
    if failure is not None or stored is None:
        return failure or _store_unavailable()
    if stored.refund_id != refund_id:
        return _error(400, "invalid_signature", "this challenge cancels another refund")
    now = datetime.now(UTC)
    try:
        refund = (
            await session.execute(
                update(PrepaidRefund)
                .where(
                    PrepaidRefund.id == refund_id,
                    PrepaidRefund.payer_public_key == stored.payer_public_key,
                    PrepaidRefund.status == PREPAID_REFUND_REQUESTED,
                )
                .values(status=PREPAID_REFUND_CANCELLED, resolved_at=now, updated_at=func.now())
                .returning(PrepaidRefund)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if refund is None:
            await session.rollback()
            await _consume_challenge(redis, signed.nonce)
            return _error(409, "refund_not_cancellable", "this refund is no longer waiting for a payout")
        balance = await _release_hold(session, refund)
        await enqueue_event(
            session,
            merchant_id=refund.merchant_id,
            event_type="prepaid.refund.cancelled",
            data=refund_event_data(refund, balance),
            source_id=str(refund.id),
        )
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("refund cancel could not be stored")
        return _store_unavailable()
    await _consume_challenge(redis, signed.nonce)
    return _refund_json(refund)


async def _release_hold(session: AsyncSession, refund: PrepaidRefund) -> int:
    return int(
        (
            await session.execute(
                update(PrepaidBalance)
                .where(PrepaidBalance.id == refund.balance_id)
                .values(refund_hold_sompi=PrepaidBalance.refund_hold_sompi - refund.amount_sompi, updated_at=func.now())
                .returning(PrepaidBalance.balance_sompi)
            )
        ).scalar_one()
    )


@router.get(
    "/refunds/status",
    response_model=list[PrepaidRefundRead],
    dependencies=[Depends(limit_by_ip(RATE_LIMIT_UNPAID_PROXY))],
)
async def refund_status(
    session: DbSession,
    slug: Annotated[str, Query(min_length=3, max_length=64, pattern=SLUG_RE)],
    payer_address: Annotated[str, Query(min_length=10, max_length=100)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> Response:
    """Public: this key's refunds at the merchant behind `slug`, newest first (like GET /balance, no signature)."""
    try:
        address = validate_kaspa_address(payer_address)
    except ValueError as exc:
        return _error(422, "invalid_payer", str(exc))
    public_key = payer_public_key(address)
    if public_key is None:
        return _error(400, "invalid_payer", "payer_address must be a P2PK Schnorr address (kaspa:q...)")
    try:
        endpoint = await _endpoint_merchant(session, slug)
        if endpoint is None:
            return _error(404, "endpoint_not_found", f"no active endpoint with slug '{slug}'")
        rows = list(
            (
                await session.execute(
                    select(PrepaidRefund)
                    .where(
                        PrepaidRefund.merchant_id == endpoint.merchant_id,
                        PrepaidRefund.payer_public_key == public_key,
                    )
                    .order_by(PrepaidRefund.created_at.desc(), PrepaidRefund.id)
                    .limit(limit)
                )
            ).scalars()
        )
    except SQLAlchemyError:
        logger.exception("refund status lookup failed")
        return _store_unavailable()
    return JSONResponse(
        content=[PrepaidRefundRead.model_validate(row).model_dump(mode="json") for row in rows],
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------- merchant routes [Bearer]


async def _owned_refund(session: AsyncSession, merchant_id: uuid.UUID, refund_id: uuid.UUID) -> PrepaidRefund | None:
    return (
        await session.execute(
            select(PrepaidRefund)
            .where(PrepaidRefund.id == refund_id, PrepaidRefund.merchant_id == merchant_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


@router.get("/refunds", response_model=list[PrepaidRefundRead])
async def list_refunds(
    merchant: CurrentMerchant,
    session: DbSession,
    status_filter: Annotated[str | None, Query(alias="status", pattern=r"^[a-z_]{4,16}$")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Response:
    if status_filter is not None and status_filter not in PREPAID_REFUND_STATUSES:
        return _error(422, "invalid_status", f"status must be one of {', '.join(PREPAID_REFUND_STATUSES)}")
    query = select(PrepaidRefund).where(PrepaidRefund.merchant_id == merchant.id)
    if status_filter is not None:
        query = query.where(PrepaidRefund.status == status_filter)
    rows = (
        await session.execute(
            query.order_by(PrepaidRefund.created_at.desc(), PrepaidRefund.id).limit(limit).offset(offset)
        )
    ).scalars()
    return JSONResponse(content=[PrepaidRefundRead.model_validate(row).model_dump(mode="json") for row in rows])


async def _mark_submitted(session: AsyncSession, refund_id: uuid.UUID, tx_id: str, note: str) -> PrepaidRefund | None:
    refund = (
        await session.execute(
            update(PrepaidRefund)
            .where(PrepaidRefund.id == refund_id, PrepaidRefund.status.in_(PREPAID_REFUND_OPEN_STATUSES))
            .values(
                status=PREPAID_REFUND_PAYOUT_SUBMITTED,
                tx_id=tx_id,
                payout_submitted_at=func.now(),
                last_error=clip(note),
                updated_at=func.now(),
            )
            .returning(PrepaidRefund)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    await session.commit()
    return refund


async def _is_kasgate_payment(session: AsyncSession, tx_id: str) -> bool:
    settled = (await session.execute(select(Settlement.id).where(Settlement.tx_id == tx_id))).first()
    deposited = (await session.execute(select(PrepaidDeposit.id).where(PrepaidDeposit.tx_id == tx_id))).first()
    return settled is not None or deposited is not None


async def _tx_id_taken(session: AsyncSession, refund_id: uuid.UUID, tx_id: str) -> bool:
    other = (
        await session.execute(
            select(PrepaidRefund.id).where(PrepaidRefund.tx_id == tx_id, PrepaidRefund.id != refund_id)
        )
    ).first()
    return other is not None


@router.post(
    "/refunds/{refund_id}/payout",
    response_model=PrepaidRefundRead,
    responses={202: {"description": "payout recorded; not visible on-chain yet, re-checked automatically"}},
)
async def submit_refund_payout(
    refund_id: uuid.UUID,
    payload: PrepaidRefundPayout,
    request: Request,
    merchant: CurrentMerchant,
    session: DbSession,
    engine: EngineDep,
) -> Response:
    """Record the merchant's payout of an open refund. The ledger is debited only once it is seen on-chain."""
    merchant_id = merchant.id
    refund = await _owned_refund(session, merchant_id, refund_id)
    if refund is None:
        return _error(404, "refund_not_found", "refund not found")
    if refund.status not in PREPAID_REFUND_OPEN_STATUSES:
        return _error(409, "refund_closed", f"this refund is already {refund.status}")
    address, amount = refund.payer_address, refund.amount_sompi
    await session.commit()

    if payload.tx_id is not None:
        tx_id = payload.tx_id
        if await _is_kasgate_payment(session, tx_id):
            return _error(
                422, "invalid_payout", "this transaction is a KasGate payment or deposit, not a refund payout"
            )
        if await _tx_id_taken(session, refund_id, tx_id):
            return _error(409, "payout_replayed", "this transaction already paid another refund")
        await session.commit()
        try:
            await engine.verify_payout(tx_id, address, amount)
        except RpcUnavailableError as exc:
            return await _pending(session, refund_id, tx_id, f"node unavailable: {exc.message}")
        except PaymentVerificationError as exc:
            if exc.code == "payment_not_found":
                return await _pending(session, refund_id, tx_id, exc.message)
            return _error(422, exc.code, exc.message)
    else:
        transaction = payload.transaction
        if transaction is None:
            return _error(422, "invalid_payout", "send exactly one of tx_id or transaction")
        try:
            tx_id, accepted = await engine.submit_payout(transaction, refund_payout_payload(refund_id), address, amount)
        except RpcUnavailableError as exc:
            return _error(503, "rpc_unavailable", f"the payout could not be broadcast or confirmed: {exc.message}")
        except PaymentVerificationError as exc:
            return _error(422, exc.code, exc.message)
        if await _tx_id_taken(session, refund_id, tx_id):
            return _error(409, "payout_replayed", "this transaction already paid another refund")
        await session.commit()
        if not accepted:
            return await _pending(session, refund_id, tx_id, "broadcast; waiting for the node to accept it")
    try:
        paid = await finalize_refund_paid(session, refund_id, tx_id)
    except IntegrityError:
        await session.rollback()
        return _error(409, "payout_replayed", "this transaction already paid another refund")
    except SQLAlchemyError:
        await session.rollback()
        logger.error("refund %s was paid on-chain by %s but could not be recorded", refund_id, tx_id, exc_info=True)
        return _store_unavailable()
    if paid is None:
        current = await _owned_refund(session, merchant_id, refund_id)
        if current is None:
            return _error(404, "refund_not_found", "refund not found")
        return _error(409, "refund_closed", f"this refund is already {current.status}")
    logger.info("refund %s paid by %s (%s)", refund_id, tx_id, request.url.path)
    return _refund_json(paid.refund)


async def _pending(session: AsyncSession, refund_id: uuid.UUID, tx_id: str, note: str) -> JSONResponse:
    try:
        refund = await _mark_submitted(session, refund_id, tx_id, note)
    except IntegrityError:
        await session.rollback()
        return _error(409, "payout_replayed", "this transaction already paid another refund")
    if refund is None:
        return _error(409, "refund_closed", "this refund is no longer open")
    return _refund_json(refund, status.HTTP_202_ACCEPTED)


@router.post("/refunds/{refund_id}/reject", response_model=PrepaidRefundRead)
async def reject_refund(
    refund_id: uuid.UUID, payload: PrepaidRefundReject, merchant: CurrentMerchant, session: DbSession
) -> Response:
    """Decline a `requested` refund with a reason the buyer can read; the hold is released back to spendable."""
    merchant_id = merchant.id
    now = datetime.now(UTC)
    refund = (
        await session.execute(
            update(PrepaidRefund)
            .where(
                PrepaidRefund.id == refund_id,
                PrepaidRefund.merchant_id == merchant.id,
                PrepaidRefund.status == PREPAID_REFUND_REQUESTED,
            )
            .values(status=PREPAID_REFUND_REJECTED, reason=payload.reason, resolved_at=now, updated_at=func.now())
            .returning(PrepaidRefund)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if refund is None:
        await session.rollback()
        existing = await _owned_refund(session, merchant_id, refund_id)
        if existing is None:
            return _error(404, "refund_not_found", "refund not found")
        return _error(
            409, "refund_not_rejectable", f"only a requested refund can be rejected (it is {existing.status})"
        )
    balance = await _release_hold(session, refund)
    await enqueue_event(
        session,
        merchant_id=merchant.id,
        event_type="prepaid.refund.rejected",
        data=refund_event_data(refund, balance),
        source_id=str(refund.id),
    )
    await session.commit()
    return _refund_json(refund)


@router.get("/ledger", response_model=list[PrepaidLedgerEntryRead])
async def list_ledger(
    merchant: CurrentMerchant,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PrepaidLedgerEntryRead]:
    """Refund payouts and expiries on this merchant's prepaid balances, newest first."""
    rows = (
        await session.execute(
            select(PrepaidLedgerEntry)
            .where(PrepaidLedgerEntry.merchant_id == merchant.id)
            .order_by(PrepaidLedgerEntry.created_at.desc(), PrepaidLedgerEntry.id)
            .limit(limit)
            .offset(offset)
        )
    ).scalars()
    return [PrepaidLedgerEntryRead.model_validate(row) for row in rows]
