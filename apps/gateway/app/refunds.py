"""Prepaid refunds (CONTRACT v2.13): buyers get unused credit back through a merchant-signed on-chain payout.

KasGate holds no keys and no coins. The deposit already paid the merchant on-chain, so a refund is an ordinary payment
FROM the merchant's own wallet TO the depositing address: no platform fee and no treasury output (money being
returned is never taxed). The gateway only keeps the record:

1. Buyer: ``POST /v1/prepaid/refunds/challenge`` (action ``request``, optional ``amount_sompi`` = all refundable credit
   when omitted; the unsigned reply does not include spendable or refundable totals) -> a single-use challenge; sign
   it with the depositing key; ``POST /v1/prepaid/refunds`` -> 201 refund ``requested``. The amount moves into the
   balance's ``refund_hold_sompi`` (still owed, no longer spendable). At most one open request per balance; at least
   ``MIN_OUTPUT_SOMPI`` (a smaller payout output is not standard on Kaspa).
2. Merchant [Bearer]: ``POST /v1/prepaid/refunds/{id}/claim`` (-> ``paying``) BEFORE broadcasting, so the buyer can
   no longer cancel; then ``POST /v1/prepaid/refunds/{id}/payout`` with the ``tx_id`` of a payment they broadcast
   (KasWare ``sendKaspa(payout_address, amount, {payload})``) or a signed ``transaction`` for the gateway to
   broadcast. BOTH must carry the payload ``kasgate:v1:refund:<id hex>`` and pay the depositing address EXACTLY the
   refund amount. The refund is ``payout_submitted`` (recorded BEFORE verification or broadcast) and becomes ``paid``
   only once the payout is accepted (the ledger is debited only now, see ``app.ledger``); not confirmed yet -> 202
   and the ledger worker checks again. Every attempted tx_id is kept in ``prepaid_refund_payouts``.
3. Merchant may ``reject`` a ``requested``/``paying`` refund with a reason; the buyer may ``cancel`` a ``requested``
   one (or a ``paying`` one whose claim is older than ``prepaid_refund_claim_ttl_seconds``) with a signed challenge,
   never while an attempted payout is pending. Both release the hold. A payout that lands anyway is still recorded.
4. ``GET /v1/prepaid/refunds/status`` is buyer-signed (same headers as the balance read, Purpose
   ``kasgate:v1:refunds-status-read``) and still withholds reasons and payout errors; unsigned or invalid -> 401
   ``invalid_balance_signature``. ``POST /v1/prepaid/refunds/status`` with a signed ``status`` challenge returns the
   full rows (a JSON array, unchanged). Failed-delivery credit is
   read with ``POST /v1/prepaid/credits/challenge`` then a signed ``POST /v1/prepaid/credits/status`` (action
   ``credits``; optional ``before`` / ``limit``, a fresh challenge per page); a refunds ``status`` challenge cannot
   be used there, and the reverse is also refused.

Every step queues a webhook event (``prepaid.refund.requested|paid|rejected|cancelled|payout_reverted``).
"""

import binascii
import json
import logging
import re
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import ColumnElement, and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.deps import PaymentVerifier, get_db_session, get_engine, get_redis
from app.kaspa_types import validate_kaspa_address
from app.ledger import (
    PaidRefund,
    check_attempt,
    claim_lapsed,
    finalize_payout,
    mark_attempt_invalid,
    note_attempt_transient,
    note_attempt_unseen,
    refund_event_data,
    refundable_covers,
    refundable_sompi,
)
from app.models import (
    CREDIT_REASON_ATTEMPTS_EXHAUSTED,
    LEDGER_KIND_FAILED_DELIVERY_CREDIT,
    PAYOUT_ATTEMPT_ABANDONED,
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
    PREPAID_REFUND_STATUSES,
    Endpoint,
    Merchant,
    PrepaidBalance,
    PrepaidDeposit,
    PrepaidLedgerEntry,
    PrepaidRefund,
    PrepaidRefundPayout,
    Settlement,
)
from app.payments import PaymentVerificationError, PayoutSighting, RpcUnavailableError
from app.prepaid import buyer_read_signed
from app.proxy import REPLAY_LOCK_TTL_SECONDS, _error, payer_public_key, verify_prepaid_signature
from app.schemas import (
    CREDIT_STATUS_ACTION,
    DEFAULT_CREDIT_STATUS_LIMIT,
    HEADER_BALANCE_SIGNATURE,
    HEADER_BALANCE_TIMESTAMP,
    SLUG_RE,
    BuyerCreditStatus,
    BuyerCreditStatusItem,
    PrepaidCreditStatusChallenge,
    PrepaidCreditStatusChallengeRequest,
    PrepaidCreditStatusRequest,
    PrepaidLedgerEntryRead,
    PrepaidRefundChallenge,
    PrepaidRefundChallengeRequest,
    PrepaidRefundPayoutRead,
    PrepaidRefundRead,
    PrepaidRefundReject,
    PrepaidRefundSigned,
    build_prepaid_refund_message,
    build_prepaid_refunds_read_message,
    new_payment_nonce,
    refund_payout_payload,
)
from app.schemas import (
    PrepaidRefundPayout as PrepaidRefundPayoutBody,
)
from app.security import (
    RATE_LIMIT_PREPAID_REFUND,
    RATE_LIMIT_REFUND_PAYOUT,
    RATE_LIMIT_UNPAID_PROXY,
    auth_domain,
    enforce_rate_limit,
    get_current_merchant,
    limit_by_ip,
)
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


CREDIT_CURSOR_VERSION: Final = "v1"


def encode_credit_status_cursor(
    *, created_at: datetime, credit_id: uuid.UUID, merchant_id: uuid.UUID, public_key: str
) -> str:
    """Opaque keyset cursor for credits/status (created_at, id), bound to this merchant and depositing key."""
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    payload = "|".join(
        (
            CREDIT_CURSOR_VERSION,
            created_at.astimezone(UTC).isoformat(),
            str(credit_id),
            str(merchant_id),
            public_key,
        )
    )
    return urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_credit_status_cursor(token: str) -> tuple[datetime, uuid.UUID, uuid.UUID, str] | None:
    """Parse a cursor from encode_credit_status_cursor, or None if it is malformed."""
    if not token.isascii() or not token or len(token) > 512:
        return None
    try:
        padded = token + ("=" * (-len(token) % 4))
        raw = urlsafe_b64decode(padded)
        version, stamp, credit_id, merchant_id, public_key = raw.decode("ascii").split("|")
        if version != CREDIT_CURSOR_VERSION or not _HEX64.fullmatch(public_key):
            return None
        created_at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            return None
        # astimezone(UTC) raises OverflowError (an ArithmeticError, not ValueError) when the UTC instant
        # falls outside year 1..9999, e.g. 9999-12-31T23:59:59-01:00. Any parse/convert failure is invalid_cursor.
        return created_at.astimezone(UTC), uuid.UUID(credit_id), uuid.UUID(merchant_id), public_key
    except (ValueError, OverflowError, ArithmeticError, TypeError, OSError, UnicodeDecodeError, binascii.Error):
        return None


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
            action not in ("request", "cancel", "status", CREDIT_STATUS_ACTION)
            or amount < 0
            or (action in ("status", CREDIT_STATUS_ACTION)) != (amount == 0)
            or expires_at.tzinfo is None
            or not _HEX64.fullmatch(public_key)
            or (action == "cancel") != (refund_id is not None)
            or (action == CREDIT_STATUS_ACTION and refund_id is not None)
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


def _claim_has_lapsed(refund: PrepaidRefund, now: datetime | None = None) -> bool:
    """Python twin of `ledger.claim_lapsed`: TTL since the last claim, or the total window since the first."""
    settings = get_settings()
    moment = now or datetime.now(UTC)
    claimed, first = refund.claimed_at, refund.first_claimed_at
    if claimed is None or first is None:
        return True
    return claimed <= moment - timedelta(seconds=settings.prepaid_refund_claim_ttl_seconds) or first <= moment - (
        timedelta(seconds=settings.prepaid_refund_claim_max_seconds)
    )


def _pending_attempt_exists() -> ColumnElement[bool]:
    return (
        select(PrepaidRefundPayout.id)
        .where(PrepaidRefundPayout.refund_id == PrepaidRefund.id, PrepaidRefundPayout.status == PAYOUT_ATTEMPT_PENDING)
        .exists()
    )


def _cancellable() -> ColumnElement[bool]:
    """SQL condition: the buyer may cancel (not claimed, or the claim lapsed; no attempted payout pending)."""
    return and_(
        or_(PrepaidRefund.status == PREPAID_REFUND_REQUESTED, claim_lapsed()),
        ~_pending_attempt_exists(),
    )


async def _cancel_blocker(session: AsyncSession, refund: PrepaidRefund) -> str | None:
    """Why the buyer cannot cancel `refund` right now, or None."""
    pending = (
        await session.execute(
            select(func.count())
            .select_from(PrepaidRefundPayout)
            .where(PrepaidRefundPayout.refund_id == refund.id, PrepaidRefundPayout.status == PAYOUT_ATTEMPT_PENDING)
        )
    ).scalar_one()
    if pending:
        return "a payout the merchant sent for this refund is still being confirmed on-chain"
    if refund.status == PREPAID_REFUND_PAYING:
        if _claim_has_lapsed(refund):
            return None
        return "the merchant is paying this refund right now"
    if refund.status != PREPAID_REFUND_REQUESTED:
        return "the merchant already submitted a payout for this refund"
    return None


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
    redis: Redis,
    signed: PrepaidRefundSigned,
    action: str,
    refund_id: uuid.UUID | None = None,
    precheck: Callable[[StoredRefundChallenge], Awaitable[JSONResponse | None]] | None = None,
) -> tuple[StoredRefundChallenge | None, JSONResponse | None]:
    """Load, verify and claim (single use) a signed refund challenge. Returns (challenge, None) or (None, error).

    Everything that can be checked without spending the nonce (action, the refund it names, the signature, and
    `precheck` once the signature is valid) is checked BEFORE the nonce lock is taken, so a request to the wrong
    refund, or one refused by `precheck`, does not burn a valid challenge.
    """
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
    if refund_id is not None and stored.refund_id != refund_id:
        return None, _error(400, "invalid_signature", "this challenge cancels another refund")
    if payer_public_key(stored.payer_address) != stored.payer_public_key or not verify_prepaid_signature(
        stored.message(signed.nonce), signed.signature, stored.payer_public_key
    ):
        return None, _error(400, "invalid_signature", "signature is not by the depositing key over this challenge")
    if precheck is not None:
        refused = await precheck(stored)
        if refused is not None:
            return None, refused
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
    # CONTRACT v2.14: failed-delivery credit is call-only, so a refund may take only deposited, unspent credit.
    # spendable/refundable totals stay off this unsigned response so credit cannot be derived from it.
    refundable = refundable_sompi(balance)
    if payload.action == "status":
        if balance is None:
            return _error(404, "refund_not_found", "this key has no prepaid balance at this merchant")
        amount, refund_id = 0, None
    elif payload.action == "request":
        if open_refund is not None:
            return _error(409, "refund_already_open", f"refund {open_refund.id} is still open for this balance")
        amount = payload.amount_sompi if payload.amount_sompi is not None else refundable
        if amount < settings.min_output_sompi:
            return _error(
                422,
                "refund_too_small",
                f"a refund must be at least {settings.min_output_sompi} sompi (the smallest standard Kaspa output); "
                f"refundable credit is {refundable} sompi (failed-delivery credit is spendable on calls only)",
            )
        if amount > refundable:
            return _error(
                409,
                "insufficient_balance",
                f"the refundable prepaid credit ({refundable} sompi) does not cover a {amount} sompi refund "
                "(failed-delivery credit is spendable on calls only)",
            )
        refund_id = None
    else:
        if open_refund is None or open_refund.id != payload.refund_id:
            return _error(404, "refund_not_found", "no open refund with this id for this key at this merchant")
        try:
            blocker = await _cancel_blocker(session, open_refund)
        except SQLAlchemyError:
            logger.exception("refund challenge lookup failed")
            return _store_unavailable()
        if blocker is not None:
            return _error(409, "refund_not_cancellable", blocker)
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
        expires_at=stored.expires_at,
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=challenge.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


@router.post(
    "/refunds",
    status_code=status.HTTP_201_CREATED,
    response_model=PrepaidRefundRead,
    dependencies=[Depends(limit_by_ip(RATE_LIMIT_PREPAID_REFUND))],
)
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
                    refundable_covers(amount),
                )
                .values(refund_hold_sompi=PrepaidBalance.refund_hold_sompi + amount, updated_at=func.now())
                .returning(PrepaidBalance.id, PrepaidBalance.balance_sompi)
            )
        ).one_or_none()
        if held is None:
            await session.rollback()
            await _consume_challenge(redis, signed.nonce)
            return _error(
                409, "insufficient_balance", "the refundable prepaid credit no longer covers this refund; ask again"
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


@router.post(
    "/refunds/{refund_id}/cancel",
    response_model=PrepaidRefundRead,
    dependencies=[Depends(limit_by_ip(RATE_LIMIT_PREPAID_REFUND))],
)
async def cancel_refund(
    refund_id: uuid.UUID, signed: PrepaidRefundSigned, session: DbSession, redis: RedisDep, engine: EngineDep
) -> Response:
    """The buyer withdraws a `requested` refund (signed `cancel` challenge); the hold goes back to spendable.

    Refused (409 `refund_not_cancellable`) once the merchant claimed it (`paying`, until the claim lapses), submitted
    a payout, or while any attempted payout is still pending. For a refund the merchant ever claimed, the node is
    asked first (mempool and UTXO index of the payout address) for a transaction that may be its payout: if one is
    found it is recorded as an attempt (method `scan`) and the cancel is refused; if the node cannot be asked, 503.
    Such a cancel keeps the amount held for `prepaid_refund_cancel_cooldown_seconds` (`hold_until`) before it is
    spendable again, so a payout broadcast just before the claim lapsed is still covered.
    """

    async def payout_in_flight(stored: StoredRefundChallenge) -> JSONResponse | None:
        return await _payout_in_flight(session, engine, stored, refund_id)

    stored, failure = await _claim_signed(redis, signed, "cancel", refund_id, payout_in_flight)
    if failure is not None or stored is None:
        return failure or _store_unavailable()
    settings = get_settings()
    now = datetime.now(UTC)
    cooldown_until = now + timedelta(seconds=settings.prepaid_refund_cancel_cooldown_seconds)
    try:
        refund = (
            await session.execute(
                update(PrepaidRefund)
                .where(
                    PrepaidRefund.id == refund_id,
                    PrepaidRefund.payer_public_key == stored.payer_public_key,
                    _cancellable(),
                )
                .values(
                    status=PREPAID_REFUND_CANCELLED,
                    resolved_at=now,
                    claimed_at=None,
                    hold_until=(
                        case((PrepaidRefund.first_claimed_at.is_not(None), cooldown_until), else_=None)
                        if settings.prepaid_refund_cancel_cooldown_seconds > 0
                        else None
                    ),
                    updated_at=func.now(),
                )
                .returning(PrepaidRefund)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if refund is None:
            await session.rollback()
            await _consume_challenge(redis, signed.nonce)
            current = (
                await session.execute(
                    select(PrepaidRefund).where(
                        PrepaidRefund.id == refund_id, PrepaidRefund.payer_public_key == stored.payer_public_key
                    )
                )
            ).scalar_one_or_none()
            blocker = None if current is None else await _cancel_blocker(session, current)
            return _error(409, "refund_not_cancellable", blocker or "this refund is no longer waiting for a payout")
        if refund.hold_until is None:
            balance = await _release_hold(session, refund)
        else:
            balance = int(
                (
                    await session.execute(
                        select(PrepaidBalance.balance_sompi).where(PrepaidBalance.id == refund.balance_id)
                    )
                ).scalar_one()
            )
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


async def _payout_in_flight(
    session: AsyncSession, engine: PaymentVerifier, stored: StoredRefundChallenge, refund_id: uuid.UUID
) -> JSONResponse | None:
    """Before cancelling a refund the merchant ever claimed: refuse (409) when the node shows a transaction that
    may be its payout, recording each new one as a `scan` attempt; 503 when the node cannot be asked. None to go on.
    Never claimed, already blocked or not found: None (the cancel UPDATE decides)."""
    try:
        refund = (
            await session.execute(
                select(PrepaidRefund).where(
                    PrepaidRefund.id == refund_id, PrepaidRefund.payer_public_key == stored.payer_public_key
                )
            )
        ).scalar_one_or_none()
        if refund is None or refund.first_claimed_at is None or await _cancel_blocker(session, refund) is not None:
            await session.commit()
            return None
        address, amount, min_daa = refund.payer_address, refund.amount_sompi, refund.first_claimed_daa_score or 0
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("refund cancel lookup failed")
        return _store_unavailable()
    try:
        sightings = await engine.find_refund_payouts(address, amount, refund_id, min_daa_score=min_daa)
    except PaymentVerificationError as exc:
        logger.warning("refund %s: cannot check the node for a payout before a cancel: %s", refund_id, exc.message)
        return _error(
            503,
            "service_unavailable",
            "the merchant had claimed this refund and the node cannot be checked for its payout right now; try again",
        )
    fresh: list[PayoutSighting] = []
    try:
        for sighting in sightings:
            known = (
                await session.execute(select(PrepaidRefundPayout.id).where(PrepaidRefundPayout.tx_id == sighting.tx_id))
            ).first()
            if known is None and not await _is_kasgate_payment(session, sighting.tx_id):
                fresh.append(sighting)
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("refund cancel lookup failed")
        return _store_unavailable()
    recorded: list[str] = []
    for sighting in fresh[: get_settings().prepaid_payout_max_pending_attempts]:
        current = (
            await session.execute(
                select(PrepaidRefund).where(PrepaidRefund.id == refund_id).execution_options(populate_existing=True)
            )
        ).scalar_one()
        attempt, failure = await _record_attempt(
            session, current, sighting.tx_id, "scan", payload_verified=sighting.payload_seen
        )
        if failure is not None or attempt is None:
            break
        recorded.append(sighting.tx_id)
    if not recorded:
        return None
    logger.warning("refund %s: cancel refused, possible payout(s) on-chain: %s", refund_id, ", ".join(recorded))
    return _error(
        409,
        "refund_not_cancellable",
        "a payout of this refund may already be on its way to you (transaction "
        + ", ".join(recorded)
        + "); it is being confirmed and will be recorded as paid if it is this refund's payout",
    )


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
    request: Request,
    session: DbSession,
    slug: Annotated[str, Query(min_length=3, max_length=64, pattern=SLUG_RE)],
    payer_address: Annotated[str, Query(min_length=10, max_length=100)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> Response:
    """This key's refunds at the merchant behind `slug`, newest first. Buyer-signed (same headers as the
    balance read, Purpose `kasgate:v1:refunds-status-read`); unsigned or invalid is 401.

    The merchant's `reason` and the payout `last_error` are withheld (null) here; POST /refunds/status with a
    signed `status` challenge returns them to the buyer.
    """
    try:
        address = validate_kaspa_address(payer_address)
    except ValueError as exc:
        return _error(422, "invalid_payer", str(exc))
    public_key = payer_public_key(address)
    if public_key is None:
        return _error(400, "invalid_payer", "payer_address must be a P2PK Schnorr address (kaspa:q...)")
    signed = buyer_read_signed(
        request,
        address=address,
        slug=slug,
        public_key=public_key,
        build_message=build_prepaid_refunds_read_message,
        required=True,
        kind="refunds",
    )
    if isinstance(signed, JSONResponse):
        return signed
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
        content=[PrepaidRefundRead.model_validate(row).public().model_dump(mode="json") for row in rows],
        headers={"Cache-Control": "no-store", "Vary": f"{HEADER_BALANCE_TIMESTAMP}, {HEADER_BALANCE_SIGNATURE}"},
    )


@router.post(
    "/refunds/status",
    response_model=list[PrepaidRefundRead],
    dependencies=[Depends(limit_by_ip(RATE_LIMIT_PREPAID_REFUND))],
)
async def signed_refund_status(signed: PrepaidRefundSigned, session: DbSession, redis: RedisDep) -> Response:
    """The buyer's own refunds at one merchant WITH the merchant's reasons, from a signed `status` challenge."""
    stored, failure = await _claim_signed(redis, signed, "status")
    if failure is not None or stored is None:
        return failure or _store_unavailable()
    try:
        rows = list(
            (
                await session.execute(
                    select(PrepaidRefund)
                    .where(
                        PrepaidRefund.merchant_id == stored.merchant_id,
                        PrepaidRefund.payer_public_key == stored.payer_public_key,
                    )
                    .order_by(PrepaidRefund.created_at.desc(), PrepaidRefund.id)
                    .limit(50)
                )
            ).scalars()
        )
    except SQLAlchemyError:
        logger.exception("signed refund status lookup failed")
        return _store_unavailable()
    await _consume_challenge(redis, signed.nonce)
    return JSONResponse(
        content=[PrepaidRefundRead.model_validate(row).model_dump(mode="json") for row in rows],
        headers={"Cache-Control": "no-store"},
    )


@router.post(
    "/credits/challenge",
    status_code=status.HTTP_201_CREATED,
    response_model=PrepaidCreditStatusChallenge,
    dependencies=[Depends(limit_by_ip(RATE_LIMIT_UNPAID_PROXY))],
)
async def credit_status_challenge(
    payload: PrepaidCreditStatusChallengeRequest, session: DbSession, redis: RedisDep
) -> Response:
    """The text to sign to read this key's failed-delivery credit at the merchant behind `slug` (action `credits`).

    Shares the refund challenge store, nonce lock, message layout (`Purpose: kasgate:v1:prepaid-refund`) and
    single-use consume path with refunds/status; the action string is `credits`, so the two challenges cannot be
    replayed across endpoints. A missing balance is still a valid challenge (the status reply is zeros).
    """
    settings = get_settings()
    public_key = payer_public_key(payload.payer_address)
    if public_key is None:
        return _error(
            400,
            "invalid_payer",
            "payer_address must be a P2PK Schnorr address (kaspa:q...): only its key can sign credit-status requests",
        )
    try:
        endpoint = await _endpoint_merchant(session, payload.slug)
        if endpoint is None:
            return _error(404, "endpoint_not_found", f"no active endpoint with slug '{payload.slug}'")
    except SQLAlchemyError:
        logger.exception("credit status challenge lookup failed")
        return _store_unavailable()
    nonce = new_payment_nonce()
    stored = StoredRefundChallenge(
        action=CREDIT_STATUS_ACTION,
        slug=endpoint.slug,
        merchant_id=endpoint.merchant_id,
        payer_address=payload.payer_address,
        payer_public_key=public_key,
        amount_sompi=0,
        refund_id=None,
        expires_at=datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=settings.payment_max_age_seconds),
    )
    try:
        await redis.set(refund_challenge_key(nonce), stored.to_json(), ex=settings.payment_max_age_seconds)
    except RedisError:
        logger.exception("refund challenge store unavailable")
        return _error(503, "service_unavailable", "prepaid refunds are temporarily unavailable")
    challenge = PrepaidCreditStatusChallenge(
        nonce=nonce,
        message=stored.message(nonce),
        action=CREDIT_STATUS_ACTION,
        slug=endpoint.slug,
        merchant_id=endpoint.merchant_id,
        payer_address=payload.payer_address,
        amount_sompi="0",
        refund_id=None,
        expires_at=stored.expires_at,
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=challenge.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


@router.post(
    "/credits/status",
    response_model=BuyerCreditStatus,
    dependencies=[Depends(limit_by_ip(RATE_LIMIT_PREPAID_REFUND))],
)
async def signed_credit_status(payload: PrepaidCreditStatusRequest, session: DbSession, redis: RedisDep) -> Response:
    """This key's failed-delivery credit at one merchant, from a signed `credits` challenge.

    Each request consumes the challenge, so every page needs a fresh one. `before` is checked after the
    signature and before the nonce lock, so a malformed or foreign cursor does not burn a valid challenge.
    """
    cursor_at: datetime | None = None
    cursor_id: uuid.UUID | None = None

    async def precheck(stored: StoredRefundChallenge) -> JSONResponse | None:
        nonlocal cursor_at, cursor_id
        if payload.before is None:
            return None
        parsed = decode_credit_status_cursor(payload.before)
        if parsed is None:
            return _error(400, "invalid_cursor", "before is not a valid credits cursor")
        created_at, credit_id, merchant_id, public_key = parsed
        if merchant_id != stored.merchant_id or public_key != stored.payer_public_key:
            return _error(400, "invalid_cursor", "this cursor is not for this buyer")
        cursor_at, cursor_id = created_at, credit_id
        return None

    stored, failure = await _claim_signed(redis, payload, CREDIT_STATUS_ACTION, precheck=precheck)
    if failure is not None or stored is None:
        return failure or _store_unavailable()
    try:
        summary = await _credit_summary(
            session,
            stored.merchant_id,
            stored.payer_public_key,
            limit=payload.limit,
            before_created_at=cursor_at,
            before_id=cursor_id,
        )
    except SQLAlchemyError:
        logger.exception("signed credit status lookup failed")
        return _store_unavailable()
    await _consume_challenge(redis, payload.nonce)
    return JSONResponse(
        content=summary.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


async def _credit_summary(
    session: AsyncSession,
    merchant_id: uuid.UUID,
    public_key: str,
    *,
    limit: int = DEFAULT_CREDIT_STATUS_LIMIT,
    before_created_at: datetime | None = None,
    before_id: uuid.UUID | None = None,
) -> BuyerCreditStatus:
    """CONTRACT v2.14: the signing buyer's failed-delivery credit at this merchant (credits/status)."""
    balance = await _balance_of(session, merchant_id, public_key)
    rows: list[PrepaidLedgerEntry] = []
    count = 0
    next_cursor: str | None = None
    if balance is not None:
        is_credit = (PrepaidLedgerEntry.balance_id == balance.id) & (
            PrepaidLedgerEntry.kind == LEDGER_KIND_FAILED_DELIVERY_CREDIT
        )
        count = int(
            (await session.execute(select(func.count()).select_from(PrepaidLedgerEntry).where(is_credit))).scalar_one()
        )
        filters: list[ColumnElement[bool]] = [is_credit]
        if before_created_at is not None and before_id is not None:
            filters.append(
                or_(
                    PrepaidLedgerEntry.created_at < before_created_at,
                    and_(PrepaidLedgerEntry.created_at == before_created_at, PrepaidLedgerEntry.id < before_id),
                )
            )
        page = list(
            (
                await session.execute(
                    select(PrepaidLedgerEntry)
                    .where(*filters)
                    .order_by(PrepaidLedgerEntry.created_at.desc(), PrepaidLedgerEntry.id.desc())
                    .limit(limit + 1)
                )
            ).scalars()
        )
        if len(page) > limit:
            last = page[limit - 1]
            next_cursor = encode_credit_status_cursor(
                created_at=last.created_at,
                credit_id=last.id,
                merchant_id=merchant_id,
                public_key=public_key,
            )
            page = page[:limit]
        rows = page
    credits = [
        BuyerCreditStatusItem(
            credit_id=row.id,
            tx_id=row.tx_id or "",
            slug=row.slug or "",
            credit_sompi=str(row.amount_sompi),
            shortfall_sompi=str(row.shortfall_sompi),
            reason="attempts_exhausted" if row.credit_reason == CREDIT_REASON_ATTEMPTS_EXHAUSTED else "window_expired",
            created_at=row.created_at,
        )
        for row in rows
    ]
    return BuyerCreditStatus(
        merchant_id=merchant_id,
        credited_sompi=str(0 if balance is None else balance.credited_sompi),
        credit_balance_sompi=str(0 if balance is None else balance.credit_balance_sompi),
        refundable_sompi=str(refundable_sompi(balance)),
        credit_count=count,
        credits=credits,
        next_cursor=next_cursor,
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


async def _is_kasgate_payment(session: AsyncSession, tx_id: str) -> bool:
    settled = (await session.execute(select(Settlement.id).where(Settlement.tx_id == tx_id))).first()
    deposited = (await session.execute(select(PrepaidDeposit.id).where(PrepaidDeposit.tx_id == tx_id))).first()
    return settled is not None or deposited is not None


async def _pending_tx_id_attempts(session: AsyncSession, refund_id: uuid.UUID) -> int:
    """Pending attempts that count toward the per-refund cap: bare `tx_id` posts only.

    A signed `transaction` is checked locally (payload and outputs) so it cannot be used to flood the worker, and a
    `scan` attempt is recorded by the gateway itself; neither counts toward the cap (or is refused by it).
    """
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(PrepaidRefundPayout)
                .where(
                    PrepaidRefundPayout.refund_id == refund_id,
                    PrepaidRefundPayout.status == PAYOUT_ATTEMPT_PENDING,
                    PrepaidRefundPayout.method == "tx_id",
                )
            )
        ).scalar_one()
    )


def _too_many_attempts() -> JSONResponse:
    limit = get_settings().prepaid_payout_max_pending_attempts
    return _error(
        409,
        "too_many_attempts",
        f"this refund already has {limit} unconfirmed tx_id payout attempts; post a signed transaction "
        "(`transaction`) or wait until they are confirmed or given up",
    )


async def _record_attempt(
    session: AsyncSession, refund: PrepaidRefund, tx_id: str, method: str, *, payload_verified: bool
) -> tuple[PrepaidRefundPayout | None, JSONResponse | None]:
    """Keep `tx_id` as an attempted payout of `refund` and, if the refund is open, move it to `payout_submitted`
    (tx_id = this attempt) in the SAME transaction, before anything is verified or broadcast. Commits.

    A tx_id already attempted for ANOTHER refund is refused (`payout_replayed`); re-posting one of this refund's
    attempts reuses it (an earlier attempt is never overwritten: a corrected tx_id is a new attempt and both are
    re-checked). An `abandoned` attempt is never revived by a bare tx_id (409 `payout_abandoned`); only a signed
    `transaction` proof may revive it (or an `invalid` one), and it keeps its original `created_at`, so the
    abandonment clock is never reset. At most `prepaid_payout_max_pending_attempts` pending `tx_id` attempts
    (409 `too_many_attempts`); signed `transaction` proofs and `scan` attempts do not count toward that cap.
    """
    refund_id, merchant_id = refund.id, refund.merchant_id
    # Lock order attempt -> refund, as in finalize_payout and the worker helpers; the refund lock serializes the
    # pending-attempt cap.
    existing = (
        await session.execute(
            select(PrepaidRefundPayout)
            .where(PrepaidRefundPayout.tx_id == tx_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    await session.execute(select(PrepaidRefund.id).where(PrepaidRefund.id == refund_id).with_for_update())
    if existing is not None:
        if existing.refund_id != refund_id:
            await session.rollback()
            return None, _error(409, "payout_replayed", "this transaction is already a payout of another refund")
        if existing.status == PAYOUT_ATTEMPT_INVALID and method != "transaction":
            proven = existing.last_error or "this transaction is not a valid payout of this refund"
            await session.rollback()
            return None, _error(422, "invalid_payout", proven)
        if existing.status == PAYOUT_ATTEMPT_ABANDONED and method != "transaction":
            await session.rollback()
            return None, _error(
                409,
                "payout_abandoned",
                "this transaction was given up after it stayed unconfirmed too long; post the signed transaction "
                "(`transaction`) to prove and re-broadcast it",
            )
        if existing.status in (PAYOUT_ATTEMPT_INVALID, PAYOUT_ATTEMPT_ABANDONED):
            # Only a signed `transaction` reaches here; it is exempt from the tx_id cap.
            existing.status, existing.resolved_at = PAYOUT_ATTEMPT_PENDING, None
        if payload_verified:
            existing.payload_verified = True
        existing.next_check_at = existing.updated_at = datetime.now(UTC)
        attempt = existing
    else:
        if method == "tx_id" and (
            await _pending_tx_id_attempts(session, refund_id) >= get_settings().prepaid_payout_max_pending_attempts
        ):
            await session.rollback()
            return None, _too_many_attempts()
        attempt = PrepaidRefundPayout(
            id=uuid.uuid4(),
            refund_id=refund_id,
            merchant_id=merchant_id,
            tx_id=tx_id,
            method=method,
            status=PAYOUT_ATTEMPT_PENDING,
            payload_verified=payload_verified,
            check_count=0,
            error_count=0,
            next_check_at=datetime.now(UTC),
        )
        session.add(attempt)
    try:
        await session.flush()
        if attempt.status == PAYOUT_ATTEMPT_PENDING:
            await session.execute(
                update(PrepaidRefund)
                .where(PrepaidRefund.id == refund_id, PrepaidRefund.status.in_(PREPAID_REFUND_OPEN_STATUSES))
                .values(
                    status=PREPAID_REFUND_PAYOUT_SUBMITTED,
                    tx_id=tx_id,
                    payout_submitted_at=func.now(),
                    last_error=None,
                    updated_at=func.now(),
                )
            )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return None, _error(409, "payout_replayed", "this transaction is already a payout of another refund")
    return attempt, None


async def _payout_response(session: AsyncSession, merchant_id: uuid.UUID, refund_id: uuid.UUID) -> JSONResponse:
    """202 with the refund as it is now (payout recorded, confirmation pending)."""
    current = await _owned_refund(session, merchant_id, refund_id)
    await session.commit()
    if current is None:
        return _error(404, "refund_not_found", "refund not found")
    return _refund_json(current, status.HTTP_202_ACCEPTED)


async def _paid_or_conflict(
    session: AsyncSession, merchant_id: uuid.UUID, refund_id: uuid.UUID, attempt_id: uuid.UUID, paid: PaidRefund | None
) -> JSONResponse:
    if paid is not None:
        return _refund_json(paid.refund)
    attempt = (
        await session.execute(select(PrepaidRefundPayout).where(PrepaidRefundPayout.id == attempt_id))
    ).scalar_one_or_none()
    if attempt is not None and attempt.status == PAYOUT_ATTEMPT_DUPLICATE:
        await session.commit()
        return _error(409, "refund_closed", "this refund was already paid by another transaction")
    return await _payout_response(session, merchant_id, refund_id)


@router.post(
    "/refunds/{refund_id}/claim",
    response_model=PrepaidRefundRead,
)
async def claim_refund(
    refund_id: uuid.UUID, merchant: CurrentMerchant, session: DbSession, engine: EngineDep
) -> Response:
    """The merchant is about to pay: `requested` -> `paying`, which the buyer can no longer cancel.

    Call it BEFORE broadcasting (e.g. right before KasWare `sendKaspa`) and broadcast only well before the returned
    `claim_expires_at`; repeat it to renew the claim. It lapses after `prepaid_refund_claim_ttl_seconds` without a
    posted payout, and renewals stop at `first_claimed_at` + `prepaid_refund_claim_max_seconds` (409
    `refund_not_claimable`; the merchant can still pay and post the payout, or reject).
    """
    merchant_id = merchant.id
    window_start = datetime.now(UTC) - timedelta(seconds=get_settings().prepaid_refund_claim_max_seconds)
    daa: int | None = None
    try:
        daa = await engine.virtual_daa_score()
    except PaymentVerificationError as exc:
        logger.warning("refund %s: could not read the virtual DAA at claim: %s", refund_id, exc.message)
    refund = (
        await session.execute(
            update(PrepaidRefund)
            .where(
                PrepaidRefund.id == refund_id,
                PrepaidRefund.merchant_id == merchant_id,
                PrepaidRefund.status.in_((PREPAID_REFUND_REQUESTED, PREPAID_REFUND_PAYING)),
                or_(PrepaidRefund.first_claimed_at.is_(None), PrepaidRefund.first_claimed_at > window_start),
            )
            .values(
                status=PREPAID_REFUND_PAYING,
                claimed_at=func.now(),
                first_claimed_at=func.coalesce(PrepaidRefund.first_claimed_at, func.now()),
                # Written only with the first first_claimed_at: a null score from a node outage stays null
                # (the pre-cancel UTXO filter is skipped). A renewal must never backfill a later virtual DAA.
                first_claimed_daa_score=case(
                    (PrepaidRefund.first_claimed_at.is_(None), daa), else_=PrepaidRefund.first_claimed_daa_score
                ),
                updated_at=func.now(),
            )
            .returning(PrepaidRefund)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if refund is None:
        await session.rollback()
        existing = await _owned_refund(session, merchant_id, refund_id)
        if existing is None:
            return _error(404, "refund_not_found", "refund not found")
        if existing.status in (PREPAID_REFUND_REQUESTED, PREPAID_REFUND_PAYING):
            return _error(
                409,
                "refund_not_claimable",
                "the claim window of this refund is used up (first claimed at "
                f"{existing.first_claimed_at.isoformat() if existing.first_claimed_at else 'unknown'}); pay it and "
                "post the payout, or reject it",
            )
        return _error(409, "refund_not_claimable", f"only a requested refund can be claimed (it is {existing.status})")
    await session.commit()
    return _refund_json(refund)


@router.get("/refunds/{refund_id}/payouts", response_model=list[PrepaidRefundPayoutRead])
async def list_refund_payouts(refund_id: uuid.UUID, merchant: CurrentMerchant, session: DbSession) -> Response:
    """Every payout transaction attempted for this refund, oldest first, with its confirmation state."""
    merchant_id = merchant.id
    if await _owned_refund(session, merchant_id, refund_id) is None:
        return _error(404, "refund_not_found", "refund not found")
    rows = (
        await session.execute(
            select(PrepaidRefundPayout)
            .where(PrepaidRefundPayout.refund_id == refund_id, PrepaidRefundPayout.merchant_id == merchant_id)
            .order_by(PrepaidRefundPayout.created_at, PrepaidRefundPayout.id)
        )
    ).scalars()
    return JSONResponse(content=[PrepaidRefundPayoutRead.model_validate(row).model_dump(mode="json") for row in rows])


@router.post(
    "/refunds/{refund_id}/payout",
    response_model=PrepaidRefundRead,
    responses={202: {"description": "payout recorded; not confirmed on-chain yet, re-checked automatically"}},
)
async def submit_refund_payout(
    refund_id: uuid.UUID,
    payload: PrepaidRefundPayoutBody,
    request: Request,
    merchant: CurrentMerchant,
    session: DbSession,
    engine: EngineDep,
    redis: RedisDep,
) -> Response:
    """Record the merchant's payout of a refund. The ledger is debited only once it is accepted on-chain.

    The attempt is stored and the refund moved to `payout_submitted` BEFORE the node is asked (tx_id mode) or the
    transaction is broadcast (transaction mode), so a buyer cancel cannot race the payout. In tx_id mode a payout of
    a refund that was already cancelled or rejected is still accepted and recorded as a late payout. Rate limited
    per merchant (`rl_refund_payout_per_merchant_per_minute`); at most `prepaid_payout_max_pending_attempts` pending
    attempts per refund.
    """
    merchant_id = merchant.id
    await enforce_rate_limit(redis, RATE_LIMIT_REFUND_PAYOUT, str(merchant_id))
    refund = await _owned_refund(session, merchant_id, refund_id)
    if refund is None:
        return _error(404, "refund_not_found", "refund not found")
    address, amount, current_status, paid_tx = refund.payer_address, refund.amount_sompi, refund.status, refund.tx_id
    payout_payload = refund_payout_payload(refund_id)

    if payload.tx_id is not None:
        tx_id = payload.tx_id
        if current_status == PREPAID_REFUND_PAID:
            await session.commit()
            if paid_tx == tx_id:
                return _refund_json(refund)
            return _error(409, "refund_closed", "this refund is already paid")
        if await _is_kasgate_payment(session, tx_id):
            await session.commit()
            return _error(
                422, "invalid_payout", "this transaction is a KasGate payment or deposit, not a refund payout"
            )
        attempt, failure = await _record_attempt(session, refund, tx_id, "tx_id", payload_verified=False)
        if failure is not None or attempt is None:
            return failure or _store_unavailable()
        result = await check_attempt(session, engine, attempt, address, amount, may_revert=False)
        if result.outcome == "invalid":
            return _error(422, result.code or "invalid_payout", result.message or "not a payout of this refund")
        if result.outcome == "paid":
            logger.info("refund %s paid by %s (%s)", refund_id, tx_id, request.url.path)
        return await _paid_or_conflict(session, merchant_id, refund_id, attempt.id, result.paid)

    transaction = payload.transaction
    if transaction is None:
        await session.commit()
        return _error(422, "invalid_payout", "send exactly one of tx_id or transaction")
    if current_status not in PREPAID_REFUND_OPEN_STATUSES:
        await session.commit()
        return _error(409, "refund_closed", f"this refund is already {current_status}")
    try:
        tx_id = engine.payout_tx_id(transaction, payout_payload, address, amount)
    except PaymentVerificationError as exc:
        await session.commit()
        return _error(422, exc.code, exc.message)
    attempt, failure = await _record_attempt(session, refund, tx_id, "transaction", payload_verified=True)
    if failure is not None or attempt is None:
        return failure or _store_unavailable()
    try:
        submitted_id, accepted = await engine.submit_payout(transaction, payout_payload, address, amount)
    except RpcUnavailableError as exc:
        await note_attempt_transient(session, attempt.id, f"broadcast not confirmed: {exc.message}")
        return await _payout_response(session, merchant_id, refund_id)
    except PaymentVerificationError as exc:
        await mark_attempt_invalid(session, attempt.id, f"{exc.code}: {exc.message}")
        return _error(422, exc.code, exc.message)
    if submitted_id != tx_id:
        logger.error("refund %s: broadcast id %s differs from the recorded id %s", refund_id, submitted_id, tx_id)
    if not accepted:
        await note_attempt_unseen(
            session, attempt.id, "broadcast; waiting for the node to accept it", payload_seen=True, may_revert=False
        )
        return await _payout_response(session, merchant_id, refund_id)
    try:
        paid = await finalize_payout(session, attempt.id)
    except SQLAlchemyError:
        await session.rollback()
        logger.error("refund %s was paid on-chain by %s but could not be recorded", refund_id, tx_id, exc_info=True)
        return _store_unavailable()
    if paid is not None:
        logger.info("refund %s paid by %s (%s)", refund_id, tx_id, request.url.path)
    return await _paid_or_conflict(session, merchant_id, refund_id, attempt.id, paid)


@router.post("/refunds/{refund_id}/reject", response_model=PrepaidRefundRead)
async def reject_refund(
    refund_id: uuid.UUID, payload: PrepaidRefundReject, merchant: CurrentMerchant, session: DbSession
) -> Response:
    """Decline a `requested` or `paying` refund with a reason the buyer can read (signed status); the hold is
    released back to spendable. Refused while an attempted payout is pending (it may still land)."""
    merchant_id = merchant.id
    now = datetime.now(UTC)
    refund = (
        await session.execute(
            update(PrepaidRefund)
            .where(
                PrepaidRefund.id == refund_id,
                PrepaidRefund.merchant_id == merchant_id,
                PrepaidRefund.status.in_((PREPAID_REFUND_REQUESTED, PREPAID_REFUND_PAYING)),
                ~_pending_attempt_exists(),
            )
            .values(
                status=PREPAID_REFUND_REJECTED,
                reason=payload.reason,
                resolved_at=now,
                claimed_at=None,
                updated_at=func.now(),
            )
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
            409,
            "refund_not_rejectable",
            f"only a requested refund with no pending payout can be rejected (it is {existing.status})",
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
    """Refund payouts, expiries and failed-delivery credits on this merchant's prepaid balances, newest first."""
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
