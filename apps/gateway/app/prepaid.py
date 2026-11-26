"""Prepaid credit (CONTRACT v2.11): deposit once on-chain, then pay calls priced below 0.6 KAS with signed debits.

Routes (all under /v1/prepaid):

* ``POST /deposits/quote`` (public, unpaid rate limit) -> 201 PrepaidDepositQuote: a nonce-bound kaspa-exact-v1
  challenge for a deposit TOTAL of at least 0.6 KAS, split by ``compute_fee_split`` exactly like a v1 payment
  (output 0 merchant, output 1 treasury, fee max(ceil(1.5 %), 0.2 KAS)). Stored as ``kasgate:prepaid:deposit:{nonce}``
  (its own namespace: a deposit challenge can never pay a /p call and a /p challenge can never fund a deposit).
* ``POST /deposits`` with ``PAYMENT-SIGNATURE`` -> 201 PrepaidDepositReceipt + ``PAYMENT-RESPONSE``. Same order as
  the v1 proxy: parse, load the stored deposit challenge, existing settlement (idempotent receipt or 409), pure
  ``engine.verify``, ``SET kasgate:noncelock:{nonce} NX`` then the SHARED ``SET kasgate:tx:{txId} NX``,
  ``resolve_inputs`` + ``settle``, then ONE transaction: settlement row (kaspa-exact-v1, fee > 0, treasury), balance
  upsert (credit = merchant share only) and the prepaid_deposits row (unique tx_id and settlement_id).
* ``POST /challenge`` (public, unpaid rate limit) -> 201 PrepaidChallenge: the exact text to sign for ONE debit.
* ``GET /balance?slug=&payer_address=`` (public) -> PrepaidBalanceRead.
* ``GET /balances``, ``GET /deposits``, ``GET /debits`` [Bearer] -> the merchant's own prepaid ledger.

KasGate never holds the coins: the merchant and the treasury were paid on-chain by the deposit. Unused credit is a
merchant obligation, never a KasGate liability; there is no withdrawal route.
"""

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.deps import PaymentVerifier, RateProvider, get_db_session, get_engine, get_oracle, get_redis
from app.kaspa_types import validate_kaspa_address
from app.ledger import credit_expiry, expire_balance_for, refundable_sompi, spendable_sompi
from app.models import Endpoint, Merchant, PrepaidBalance, PrepaidDebit, PrepaidDeposit, Settlement
from app.oracle import OracleUnavailableError
from app.payments import PaymentVerificationError
from app.proxy import (
    REPLAY_LOCK_TTL_SECONDS,
    SettlementTerms,
    StoredPrepaidChallenge,
    _dead_letter_settlement,
    _error,
    _find_settlement,
    _lookup_rate,
    _new_settlement,
    _payment_response_header,
    _release_payment_locks,
    _settlement_payload,
    _verification_failure,
    lock_key,
    nonce_lock_key,
    payer_public_key,
    prepaid_challenge_key,
    prepaid_deposit_key,
    prepaid_only_error,
    prepaid_price,
)
from app.schemas import (
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
    PaymentRequired,
    PaymentSignature,
    PrepaidBalanceRead,
    PrepaidChallenge,
    PrepaidChallengeRequest,
    PrepaidDebitRead,
    PrepaidDepositQuote,
    PrepaidDepositQuoteRequest,
    PrepaidDepositRead,
    PrepaidDepositReceipt,
    compute_fee_split,
    new_payment_nonce,
)
from app.security import RATE_LIMIT_UNPAID_PROXY, get_current_merchant, limit_by_ip
from app.webhooks import enqueue_event

logger = logging.getLogger("kasgate.prepaid")

router = APIRouter(prefix="/v1/prepaid", tags=["prepaid"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
CurrentMerchant = Annotated[Merchant, Depends(get_current_merchant)]
DEPOSIT_PATH = "/v1/prepaid/deposits"


# ---------------------------------------------------------------- helpers


async def _active_endpoint(session: AsyncSession, slug: str) -> tuple[Endpoint, str] | None:
    row = (
        await session.execute(
            select(Endpoint, Merchant.kaspa_address)
            .join(Merchant, Merchant.id == Endpoint.merchant_id)
            .where(Endpoint.slug == slug, Endpoint.is_active.is_(True), Merchant.is_active.is_(True))
        )
    ).one_or_none()
    return None if row is None else (row[0], row[1])


def _not_found(slug: str) -> JSONResponse:
    return _error(404, "endpoint_not_found", f"no active endpoint with slug '{slug}'")


def _not_p2pk() -> JSONResponse:
    return _error(
        400,
        "invalid_payment_header",
        "payer_address must be a P2PK Schnorr address (kaspa:q...): only its key can sign prepaid debits",
    )


async def _balance_of(session: AsyncSession, merchant_id: uuid.UUID, public_key: str) -> PrepaidBalance | None:
    return (
        await session.execute(
            select(PrepaidBalance).where(
                PrepaidBalance.merchant_id == merchant_id, PrepaidBalance.payer_public_key == public_key
            )
        )
    ).scalar_one_or_none()


def _store_unavailable() -> JSONResponse:
    return _error(503, "service_unavailable", "prepaid store is temporarily unavailable")


def _receipt(deposit: PrepaidDeposit, slug: str, balance: int) -> PrepaidDepositReceipt:
    return PrepaidDepositReceipt(
        deposit_id=deposit.id,
        tx_id=deposit.tx_id,
        slug=slug,
        merchant_id=deposit.merchant_id,
        payer_address=deposit.payer_address,
        deposit_sompi=str(deposit.deposit_sompi),
        fee_sompi=str(deposit.fee_sompi),
        credit_sompi=str(deposit.credit_sompi),
        balance_sompi=str(balance),
        created_at=deposit.created_at,
    )


def _balance_read(merchant_id: uuid.UUID, payer_address: str, balance: PrepaidBalance | None) -> PrepaidBalanceRead:
    return PrepaidBalanceRead(
        merchant_id=merchant_id,
        payer_address=payer_address if balance is None else balance.payer_address,
        balance_sompi=str(0 if balance is None else balance.balance_sompi),
        deposited_sompi=str(0 if balance is None else balance.deposited_sompi),
        debited_sompi=str(0 if balance is None else balance.debited_sompi),
        updated_at=None if balance is None else balance.updated_at,
        refunded_sompi=str(0 if balance is None else balance.refunded_sompi),
        expired_sompi=str(0 if balance is None else balance.expired_sompi),
        refund_hold_sompi=str(0 if balance is None else balance.refund_hold_sompi),
        spendable_sompi=str(spendable_sompi(balance)),
        expires_at=None if balance is None else balance.expires_at,
        credited_sompi=str(0 if balance is None else balance.credited_sompi),
        credit_balance_sompi=str(0 if balance is None else balance.credit_balance_sompi),
        refundable_sompi=str(refundable_sompi(balance)),
    )


# ---------------------------------------------------------------- deposits


@router.post(
    "/deposits/quote",
    status_code=status.HTTP_201_CREATED,
    response_model=PrepaidDepositQuote,
    dependencies=[Depends(limit_by_ip(RATE_LIMIT_UNPAID_PROXY))],
)
async def quote_deposit(
    payload: PrepaidDepositQuoteRequest,
    session: DbSession,
    redis: Annotated[Redis, Depends(get_redis)],
) -> Response:
    settings = get_settings()
    public_key = payer_public_key(payload.payer_address)
    if public_key is None:
        return _not_p2pk()
    try:
        found = await _active_endpoint(session, payload.slug)
        if found is None:
            return _not_found(payload.slug)
        endpoint, merchant_address = found
        balance = await _balance_of(session, endpoint.merchant_id, public_key)
    except SQLAlchemyError:
        logger.exception("prepaid balance lookup failed")
        return _store_unavailable()
    current = 0 if balance is None else balance.balance_sompi
    split = compute_fee_split(payload.amount_sompi)
    if current + split.merchant_sompi > settings.prepaid_max_balance_sompi:
        raise HTTPException(
            status_code=422,
            detail=(
                f"this deposit would take the prepaid balance above the {settings.prepaid_max_balance_sompi} sompi "
                f"cap (current balance {current} sompi)"
            ),
        )
    expires_at = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=settings.payment_max_age_seconds)
    required = PaymentRequired.for_endpoint(payload.amount_sompi, merchant_address, new_payment_nonce(), expires_at)
    record = json.dumps(
        {
            "slug": endpoint.slug,
            "payerAddress": payload.payer_address,
            "required": required.model_dump(mode="json", exclude_none=True),
        },
        sort_keys=True,
    )
    try:
        await redis.set(prepaid_deposit_key(required.nonce), record, ex=settings.payment_max_age_seconds)
    except RedisError:
        logger.exception("prepaid deposit challenge store unavailable")
        return _error(503, "service_unavailable", "prepaid deposits are temporarily unavailable")
    quote = PrepaidDepositQuote(
        slug=endpoint.slug,
        merchant_id=endpoint.merchant_id,
        payer_address=payload.payer_address,
        deposit_sompi=str(split.total_sompi),
        fee_sompi=str(split.fee_sompi),
        credit_sompi=str(split.merchant_sompi),
        balance_sompi=str(current),
        required=required,
        deposit_url=f"{settings.public_base_url}{DEPOSIT_PATH}",
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=quote.model_dump(mode="json", exclude_none=True),
        headers={"Cache-Control": "no-store"},
    )


async def _load_deposit_challenge(redis: Redis, nonce: str) -> tuple[str, str, PaymentRequired] | None:
    raw = await redis.get(prepaid_deposit_key(nonce))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        slug = data["slug"]
        payer = data["payerAddress"]
        required = PaymentRequired.model_validate(data["required"])
    except (ValueError, KeyError, TypeError):
        logger.error("stored deposit challenge %s is unreadable", nonce, exc_info=True)
        return None
    if (
        not isinstance(slug, str)
        or not isinstance(payer, str)
        or required.nonce != nonce
        or required.expiresAt <= datetime.now(UTC)
    ):
        return None
    return slug, payer, required


async def _existing_deposit_response(
    session: AsyncSession, existing: Settlement, signature: PaymentSignature
) -> Response:
    """A deposit already settled: the same tx + nonce + payer gets its receipt again (idempotent), else 409."""
    deposit = (
        await session.execute(select(PrepaidDeposit).where(PrepaidDeposit.settlement_id == existing.id))
    ).scalar_one_or_none()
    if (
        deposit is None
        or existing.tx_id != signature.txId
        or existing.payment_nonce != signature.nonce
        or deposit.payer_address != signature.payerAddress
    ):
        return _error(409, "payment_replayed", "this transaction or payment nonce was already used")
    balance = (
        await session.execute(select(PrepaidBalance.balance_sompi).where(PrepaidBalance.id == deposit.balance_id))
    ).scalar_one()
    slug = (await session.execute(select(Endpoint.slug).where(Endpoint.id == existing.endpoint_id))).scalar_one()
    usd = existing.usd_value_at_settlement
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=_receipt(deposit, slug, int(balance)).model_dump(mode="json"),
        headers={
            HEADER_PAYMENT_RESPONSE: _payment_response_header(existing.tx_id, usd if usd is not None else Decimal(0))
        },
    )


@router.post(
    "/deposits",
    status_code=status.HTTP_201_CREATED,
    response_model=PrepaidDepositReceipt,
    responses={402: {"description": "payment missing, invalid or expired"}, 409: {"description": "replayed"}},
)
async def deposit(
    request: Request,
    session: DbSession,
    redis: Annotated[Redis, Depends(get_redis)],
    engine: Annotated[PaymentVerifier, Depends(get_engine)],
    oracle: Annotated[RateProvider, Depends(get_oracle)],
) -> Response:
    header = request.headers.get(HEADER_PAYMENT_SIGNATURE)
    if not header:
        return _error(
            402, "payment_required", f"send the paid deposit as {HEADER_PAYMENT_SIGNATURE}; quote it at /deposits/quote"
        )
    try:
        signature = PaymentSignature.from_header(header)
    except ValidationError as exc:
        description = str(exc).lower()
        code = "wrong_network" if "prefix" in description or "network" in description else "invalid_payment_header"
        return _error(402, code, f"{HEADER_PAYMENT_SIGNATURE} header is invalid")
    except ValueError:
        return _error(402, "invalid_payment_header", f"{HEADER_PAYMENT_SIGNATURE} header is malformed")
    tx_id, nonce = signature.txId, signature.nonce

    try:
        challenge = await _load_deposit_challenge(redis, nonce)
    except RedisError:
        logger.exception("prepaid deposit challenge store unavailable")
        return _error(503, "service_unavailable", "payment replay protection is temporarily unavailable")
    try:
        existing = await _find_settlement(session, tx_id, nonce)
        if existing is not None:
            return await _existing_deposit_response(session, existing, signature)
    except SQLAlchemyError:
        logger.exception("settlement lookup failed")
        return _store_unavailable()
    if challenge is None:
        return _error(402, "payment_expired", "the deposit quote is unknown or has expired; request a new quote")
    slug, quoted_payer, required = challenge
    if signature.payerAddress != quoted_payer:
        return _error(
            402,
            "invalid_payment_header",
            "payerAddress is not the address the deposit was quoted for",
            required=required,
        )
    public_key = payer_public_key(signature.payerAddress)
    if public_key is None:
        return _error(402, "invalid_payment_header", "payerAddress must be a P2PK Schnorr address", required=required)
    try:
        found = await _active_endpoint(session, slug)
    except SQLAlchemyError:
        logger.exception("endpoint lookup failed")
        return _store_unavailable()
    if found is None:
        return _not_found(slug)
    endpoint, merchant_address = found
    if required.payTo != merchant_address:
        return _error(402, "payment_expired", "the merchant's payout address changed; request a new quote")

    # Pure checks first (outputs 0 merchant / 1 treasury exact, payload nonce, payerProof): nothing reaches a lock.
    try:
        pre_verified, tx = engine.verify(signature, required)
    except PaymentVerificationError as exc:
        return _verification_failure(exc, required)
    except Exception:
        logger.exception("unexpected failure while verifying deposit tx %s", tx_id)
        return _error(503, "service_unavailable", "payment verification is temporarily unavailable")

    # Nonce claim, then the SHARED replay lock: one txId can never fund a deposit and also pay a call.
    tx_locked = False
    try:
        if await redis.set(nonce_lock_key(nonce), tx_id, nx=True, ex=REPLAY_LOCK_TTL_SECONDS):
            tx_locked = bool(await redis.set(lock_key(tx_id), "1", nx=True, ex=REPLAY_LOCK_TTL_SECONDS))
            if not tx_locked:
                await redis.delete(nonce_lock_key(nonce))
    except RedisError:
        logger.exception("replay lock unavailable")
        return _error(503, "service_unavailable", "payment replay protection is temporarily unavailable")
    if not tx_locked:
        return _error(409, "payment_replayed", "this transaction or payment nonce is already being processed")

    try:
        await engine.resolve_inputs(signature, tx)
        verified = await engine.settle(tx, pre_verified, required)
    except PaymentVerificationError as exc:
        await _release_payment_locks(redis, tx_id, nonce)
        return _verification_failure(exc, required)
    except Exception:
        await _release_payment_locks(redis, tx_id, nonce)
        logger.exception("unexpected failure while settling deposit tx %s", tx_id)
        return _error(503, "service_unavailable", "payment verification is temporarily unavailable")

    terms = SettlementTerms.for_v1(required)
    rate = await _lookup_rate(oracle, terms)
    settlement = _new_settlement(endpoint, merchant_address, terms, verified, rate, request)
    unrecorded = _settlement_payload(settlement, endpoint.slug)
    credit = verified.merchant_sompi
    try:
        session.add(settlement)
        await session.flush()
        # CONTRACT v2.13: overdue credit expires before new credit lands, and the new credit gets the policy expiry.
        await expire_balance_for(session, endpoint.merchant_id, public_key)
        new_expiry, next_expiry = await credit_expiry(session, endpoint)
        balance_id, balance = (
            await session.execute(
                pg_insert(PrepaidBalance)
                .values(
                    id=uuid.uuid4(),
                    merchant_id=endpoint.merchant_id,
                    payer_address=signature.payerAddress,
                    payer_public_key=public_key,
                    balance_sompi=credit,
                    deposited_sompi=credit,
                    debited_sompi=0,
                    expires_at=new_expiry,
                )
                .on_conflict_do_update(
                    index_elements=[PrepaidBalance.merchant_id, PrepaidBalance.payer_public_key],
                    set_={
                        "balance_sompi": PrepaidBalance.balance_sompi + credit,
                        "deposited_sompi": PrepaidBalance.deposited_sompi + credit,
                        "expires_at": next_expiry,
                        "updated_at": func.now(),
                    },
                )
                .returning(PrepaidBalance.id, PrepaidBalance.balance_sompi)
            )
        ).one()
        record = PrepaidDeposit(
            id=uuid.uuid4(),
            balance_id=balance_id,
            merchant_id=endpoint.merchant_id,
            settlement_id=settlement.id,
            tx_id=verified.tx_id,
            payer_address=signature.payerAddress,
            deposit_sompi=verified.total_sompi,
            fee_sompi=verified.fee_sompi,
            credit_sompi=credit,
            created_at=datetime.now(UTC),
        )
        session.add(record)
        await session.flush()
        await enqueue_event(
            session,
            merchant_id=endpoint.merchant_id,
            event_type="prepaid.deposit",
            data={
                "deposit_id": str(record.id),
                "settlement_id": str(settlement.id),
                "tx_id": verified.tx_id,
                "endpoint_slug": endpoint.slug,
                "payer_address": signature.payerAddress,
                "deposit_sompi": str(verified.total_sompi),
                "fee_sompi": str(verified.fee_sompi),
                "credit_sompi": str(credit),
                "balance_sompi": str(int(balance)),
            },
            source_id=str(record.id),
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        logger.error("deposit tx %s (nonce %s) conflicts with an existing settlement", tx_id, nonce, exc_info=True)
        return _error(409, "payment_replayed", "this transaction or payment nonce was already used")
    except SQLAlchemyError:
        await session.rollback()
        logger.error("deposit tx %s was accepted by the node but could not be recorded", tx_id, exc_info=True)
        await _dead_letter_settlement(redis, json.dumps({"prepaidDeposit": json.loads(unrecorded)}, sort_keys=True))
        return _error(503, "service_unavailable", "payment was accepted but could not be recorded; contact support")

    try:
        await redis.getdel(prepaid_deposit_key(nonce))
    except RedisError:
        logger.warning("could not consume deposit challenge %s; unique keys still block reuse", nonce)
    usd = settlement.usd_value_at_settlement
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=_receipt(record, endpoint.slug, int(balance)).model_dump(mode="json"),
        headers={
            HEADER_PAYMENT_RESPONSE: _payment_response_header(verified.tx_id, usd if usd is not None else Decimal(0))
        },
    )


# ---------------------------------------------------------------- debit challenges and balances


@router.post(
    "/challenge",
    status_code=status.HTTP_201_CREATED,
    response_model=PrepaidChallenge,
    dependencies=[Depends(limit_by_ip(RATE_LIMIT_UNPAID_PROXY))],
)
async def debit_challenge(
    payload: PrepaidChallengeRequest,
    session: DbSession,
    redis: Annotated[Redis, Depends(get_redis)],
    oracle: Annotated[RateProvider, Depends(get_oracle)],
) -> Response:
    settings = get_settings()
    public_key = payer_public_key(payload.payer_address)
    if public_key is None:
        return _not_p2pk()
    try:
        found = await _active_endpoint(session, payload.slug)
    except SQLAlchemyError:
        logger.exception("endpoint lookup failed")
        return _store_unavailable()
    if found is None:
        return _not_found(payload.slug)
    endpoint, merchant_address = found
    if payload.method not in endpoint.allowed_methods:
        return _error(
            405,
            "method_not_allowed",
            f"{payload.method} is not allowed on this endpoint",
            headers={"Allow": ", ".join(sorted(endpoint.allowed_methods))},
        )
    try:
        amount, rate = await prepaid_price(endpoint, oracle)
    except OracleUnavailableError as exc:
        logger.warning("USD price unavailable for prepaid challenge %s: %s", endpoint.slug, exc)
        return _error(503, "price_unavailable", "KAS/USD price is temporarily unavailable")
    except ValueError as exc:
        logger.error("endpoint %s cannot be priced: %s", endpoint.slug, exc)
        return _error(503, "service_unavailable", "endpoint price is not currently payable")
    try:
        balance = await _balance_of(session, endpoint.merchant_id, public_key)
    except SQLAlchemyError:
        logger.exception("prepaid balance lookup failed")
        return _store_unavailable()
    current = spendable_sompi(balance)
    if current < amount:
        return await prepaid_only_error(
            endpoint,
            merchant_address,
            oracle,
            "insufficient_balance",
            f"the spendable prepaid balance ({current} sompi) does not cover this call ({amount} sompi); deposit first",
            amount_sompi=amount,
        )
    nonce = new_payment_nonce()
    stored = StoredPrepaidChallenge(
        slug=endpoint.slug,
        merchant_id=endpoint.merchant_id,
        payer_address=payload.payer_address,
        payer_public_key=public_key,
        method=payload.method,
        path=payload.path,
        body_hash=payload.body_hash,
        amount_sompi=amount,
        kas_usd_rate=rate,
        expires_at=datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=settings.payment_max_age_seconds),
    )
    try:
        await redis.set(prepaid_challenge_key(nonce), stored.to_json(), ex=settings.payment_max_age_seconds)
    except RedisError:
        logger.exception("prepaid challenge store unavailable")
        return _error(503, "service_unavailable", "prepaid debits are temporarily unavailable")
    challenge = PrepaidChallenge(
        nonce=nonce,
        message=stored.message(nonce),
        slug=endpoint.slug,
        merchant_id=endpoint.merchant_id,
        payer_address=payload.payer_address,
        method=payload.method,
        path=payload.path,
        body_hash=payload.body_hash,
        amount_sompi=str(amount),
        kas_usd_rate=None if rate is None else format(rate, "f"),
        balance_sompi=str(current),
        expires_at=stored.expires_at,
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=challenge.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/balance",
    response_model=PrepaidBalanceRead,
    dependencies=[Depends(limit_by_ip(RATE_LIMIT_UNPAID_PROXY))],
)
async def read_balance(
    session: DbSession,
    slug: Annotated[str, Query(min_length=3, max_length=64, pattern=r"^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])$")],
    payer_address: Annotated[str, Query(min_length=10, max_length=100)],
) -> Response:
    try:
        address = validate_kaspa_address(payer_address)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    public_key = payer_public_key(address)
    if public_key is None:
        return _not_p2pk()
    try:
        found = await _active_endpoint(session, slug)
        if found is None:
            return _not_found(slug)
        endpoint, _merchant_address = found
        balance = await _balance_of(session, endpoint.merchant_id, public_key)
    except SQLAlchemyError:
        logger.exception("prepaid balance lookup failed")
        return _store_unavailable()
    return JSONResponse(
        content=_balance_read(endpoint.merchant_id, address, balance).model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------- merchant ledger


@router.get("/balances", response_model=list[PrepaidBalanceRead])
async def list_balances(
    merchant: CurrentMerchant,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PrepaidBalanceRead]:
    rows = (
        await session.execute(
            select(PrepaidBalance)
            .where(PrepaidBalance.merchant_id == merchant.id)
            .order_by(PrepaidBalance.updated_at.desc(), PrepaidBalance.id)
            .limit(limit)
            .offset(offset)
        )
    ).scalars()
    return [_balance_read(merchant.id, row.payer_address, row) for row in rows]


@router.get("/deposits", response_model=list[PrepaidDepositRead])
async def list_deposits(
    merchant: CurrentMerchant,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PrepaidDepositRead]:
    rows = (
        await session.execute(
            select(PrepaidDeposit)
            .where(PrepaidDeposit.merchant_id == merchant.id)
            .order_by(PrepaidDeposit.created_at.desc(), PrepaidDeposit.id)
            .limit(limit)
            .offset(offset)
        )
    ).scalars()
    return [PrepaidDepositRead.model_validate(row) for row in rows]


@router.get("/debits", response_model=list[PrepaidDebitRead])
async def list_debits(
    merchant: CurrentMerchant,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PrepaidDebitRead]:
    rows = (
        await session.execute(
            select(PrepaidDebit)
            .where(PrepaidDebit.merchant_id == merchant.id)
            .order_by(PrepaidDebit.created_at.desc(), PrepaidDebit.id)
            .limit(limit)
            .offset(offset)
        )
    ).scalars()
    return [PrepaidDebitRead.model_validate(row) for row in rows]
