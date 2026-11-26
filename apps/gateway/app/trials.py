"""Free trial calls (CONTRACT v2.12): a merchant gives each buyer key N unpaid calls per endpoint.

Routes (all under /v1/trials):

* ``POST /challenge`` (public, unpaid rate limit) -> 201 TrialChallenge: the exact text the buyer signs with the key
  of its P2PK address for ONE trial call, bound to the endpoint, method, raw path + query and body hash. Stored as
  ``kasgate:trial:challenge:{nonce}``. No trial, used up, or the endpoint's daily cap reached -> 402
  ``trial_exhausted`` (no accepts: pay the 402 already in hand).
* ``GET /status?slug=&payer_address=`` (public) -> TrialStatusRead.
* ``GET /calls`` [Bearer] -> the merchant's trial call log, newest first.

The call itself is ``/p/{slug}`` with ``X-KasGate-Trial`` (see `app.proxy._pay_with_trial`). Trial calls create no
settlement, move no coins and never carry a platform fee.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.deps import get_db_session, get_redis
from app.kaspa_types import validate_kaspa_address
from app.models import Endpoint, Merchant, TrialCall
from app.proxy import (
    StoredTrialChallenge,
    _error,
    payer_public_key,
    trial_calls_used,
    trial_challenge_key,
    trial_daily_calls,
)
from app.schemas import (
    SLUG_RE,
    TrialCallRead,
    TrialChallenge,
    TrialChallengeRequest,
    TrialStatusRead,
    X402ErrorBody,
    new_payment_nonce,
)
from app.security import RATE_LIMIT_UNPAID_PROXY, get_current_merchant, limit_by_ip

logger = logging.getLogger("kasgate.trials")

router = APIRouter(prefix="/v1/trials", tags=["trials"])

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
CurrentMerchant = Annotated[Merchant, Depends(get_current_merchant)]


async def _active_endpoint(session: AsyncSession, slug: str) -> Endpoint | None:
    return (
        await session.execute(
            select(Endpoint)
            .join(Merchant, Merchant.id == Endpoint.merchant_id)
            .where(Endpoint.slug == slug, Endpoint.is_active.is_(True), Merchant.is_active.is_(True))
        )
    ).scalar_one_or_none()


def _not_p2pk() -> JSONResponse:
    return _error(
        400,
        "invalid_payment_header",
        "payer_address must be a P2PK Schnorr address (kaspa:q...): its key signs the trial call",
    )


def _exhausted(message: str) -> JSONResponse:
    body = X402ErrorBody(error="trial_exhausted", message=message).model_dump(mode="json")
    return JSONResponse(status_code=402, content=body, headers={"Cache-Control": "no-store"})


async def _remaining(session: AsyncSession, endpoint: Endpoint, public_key: str) -> tuple[int, int]:
    """(calls used by this key, calls it can still make now: 0 while the endpoint's daily cap is reached)."""
    used = await trial_calls_used(session, endpoint.id, public_key)
    left = max(endpoint.free_trial_calls - used, 0)
    if left and await trial_daily_calls(session, endpoint.id) >= endpoint.trial_daily_cap:
        left = 0
    return used, left


@router.post(
    "/challenge",
    status_code=status.HTTP_201_CREATED,
    response_model=TrialChallenge,
    responses={402: {"description": "trial_exhausted: no trial calls left for this key today"}},
    dependencies=[Depends(limit_by_ip(RATE_LIMIT_UNPAID_PROXY))],
)
async def trial_challenge(
    payload: TrialChallengeRequest,
    session: DbSession,
    redis: Annotated[Redis, Depends(get_redis)],
) -> Response:
    settings = get_settings()
    public_key = payer_public_key(payload.payer_address)
    if public_key is None:
        return _not_p2pk()
    try:
        endpoint = await _active_endpoint(session, payload.slug)
        if endpoint is None:
            return _error(404, "endpoint_not_found", f"no active endpoint with slug '{payload.slug}'")
        if payload.method not in endpoint.allowed_methods:
            return _error(
                405,
                "method_not_allowed",
                f"{payload.method} is not allowed on this endpoint",
                headers={"Allow": ", ".join(sorted(endpoint.allowed_methods))},
            )
        if endpoint.free_trial_calls <= 0:
            return _exhausted("this endpoint offers no free trial calls")
        used, left = await _remaining(session, endpoint, public_key)
    except SQLAlchemyError:
        logger.exception("trial lookup failed")
        return _error(503, "service_unavailable", "trial store is temporarily unavailable")
    if left <= 0:
        return _exhausted("no free trial calls are left for this key on this endpoint today")
    nonce = new_payment_nonce()
    stored = StoredTrialChallenge(
        slug=endpoint.slug,
        endpoint_id=endpoint.id,
        merchant_id=endpoint.merchant_id,
        payer_address=payload.payer_address,
        payer_public_key=public_key,
        method=payload.method,
        path=payload.path,
        body_hash=payload.body_hash,
        expires_at=datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=settings.payment_max_age_seconds),
    )
    try:
        await redis.set(trial_challenge_key(nonce), stored.to_json(), ex=settings.payment_max_age_seconds)
    except RedisError:
        logger.exception("trial challenge store unavailable")
        return _error(503, "service_unavailable", "free trial calls are temporarily unavailable")
    challenge = TrialChallenge(
        nonce=nonce,
        message=stored.message(nonce),
        slug=endpoint.slug,
        merchant_id=endpoint.merchant_id,
        payer_address=payload.payer_address,
        method=payload.method,
        path=payload.path,
        body_hash=payload.body_hash,
        free_trial_calls=endpoint.free_trial_calls,
        calls_used=used,
        remaining=left,
        expires_at=stored.expires_at,
    )
    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content=challenge.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/status",
    response_model=TrialStatusRead,
    dependencies=[Depends(limit_by_ip(RATE_LIMIT_UNPAID_PROXY))],
)
async def trial_status(
    session: DbSession,
    slug: Annotated[str, Query(min_length=3, max_length=64, pattern=SLUG_RE)],
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
        endpoint = await _active_endpoint(session, slug)
        if endpoint is None:
            return _error(404, "endpoint_not_found", f"no active endpoint with slug '{slug}'")
        used, left = await _remaining(session, endpoint, public_key)
    except SQLAlchemyError:
        logger.exception("trial status lookup failed")
        return _error(503, "service_unavailable", "trial store is temporarily unavailable")
    body = TrialStatusRead(
        slug=endpoint.slug,
        merchant_id=endpoint.merchant_id,
        payer_address=address,
        free_trial_calls=endpoint.free_trial_calls,
        calls_used=used,
        remaining=left,
    )
    return JSONResponse(content=body.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.get("/calls", response_model=list[TrialCallRead])
async def list_trial_calls(
    merchant: CurrentMerchant,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TrialCallRead]:
    rows = (
        await session.execute(
            select(TrialCall)
            .where(TrialCall.merchant_id == merchant.id)
            .order_by(TrialCall.created_at.desc(), TrialCall.id)
            .limit(limit)
            .offset(offset)
        )
    ).scalars()
    return [TrialCallRead.model_validate(row) for row in rows]
