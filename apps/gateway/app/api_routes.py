"""Merchant-facing REST API (`/v1/*`) and the public directory/quote routes."""

import base64
import binascii
import csv
import hashlib
import io
import json
import logging
import re
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import Date, cast, desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import X402_SCHEME, get_settings
from app.deps import RateProvider, get_db_session, get_oracle, get_redis, get_sessionmaker
from app.models import Endpoint, Merchant, PriceCurrency, Settlement, SettlementStatus
from app.oracle import OracleUnavailableError
from app.proxy import issue_payment_challenge
from app.schemas import (
    PROXY_PATH_PREFIX,
    TAX_CSV_COLUMNS,
    AuthChallenge,
    AuthChallengeRequest,
    AuthVerifyRequest,
    CatalogEndpoint,
    CatalogPage,
    DailyRevenue,
    DirectoryEntry,
    DirectoryManifest,
    EndpointCreate,
    EndpointRead,
    EndpointRevenue,
    EndpointUpdate,
    MerchantAnalytics,
    MerchantCreate,
    MerchantCreated,
    MerchantRead,
    PaymentRequired,
    SessionIssued,
    SettlementRead,
    TaxReportRow,
    X402ErrorBody,
    sompi_to_kas,
    usd_micros_to_usd,
)
from app.security import (
    AUTH_CHALLENGE_TTL_SECONDS,
    RATE_LIMIT_AUTH_CHALLENGE,
    RATE_LIMIT_AUTH_VERIFY,
    RATE_LIMIT_CREATE_MERCHANT,
    RATE_LIMIT_UNPAID_PROXY,
    SESSION_TOKEN_PREFIX,
    auth_challenge_key,
    consume_wallet_proof,
    create_session,
    encrypt_headers,
    generate_api_key,
    get_bearer_token,
    get_current_merchant,
    hash_api_key,
    limit_by_ip,
    new_auth_challenge,
    revoke_all_sessions,
    revoke_session,
    validate_upstream_headers,
)

logger = logging.getLogger("kasgate.api")

router = APIRouter(prefix="/v1")

DIRECTORY_CACHE_KEY = "kasgate:directory:v1"
DIRECTORY_CACHE_TTL_SECONDS = 30
DIRECTORY_MAX_ENTRIES = 1000
DEFAULT_ANALYTICS_WINDOW_DAYS = 30
MAX_SLUG_ATTEMPTS = 8
SLUG_BASE_MAX_LENGTH = 55
CSV_CHUNK_ROWS = 500

_NON_SLUG_CHARS = re.compile(r"[^a-z0-9]+")

CurrentMerchant = Annotated[Merchant, Depends(get_current_merchant)]
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


# ---------------------------------------------------------------- helpers


def slugify(value: str) -> str:
    return _NON_SLUG_CHARS.sub("-", value.lower()).strip("-")


def generate_slug(name: str) -> str:
    base = slugify(name)[:SLUG_BASE_MAX_LENGTH].strip("-") or "endpoint"
    return f"{base}-{secrets.token_hex(3)}"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def resolve_period(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    period_end = _as_utc(end) if end is not None else datetime.now(UTC)
    period_start = _as_utc(start) if start is not None else period_end - timedelta(days=DEFAULT_ANALYTICS_WINDOW_DAYS)
    if period_start >= period_end:
        raise HTTPException(status_code=422, detail="start must be before end")
    return period_start, period_end


def _headers_or_422(headers: dict[str, str]) -> bytes | None:
    try:
        return encrypt_headers(validate_upstream_headers(headers))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _owned_endpoint(session: AsyncSession, merchant: Merchant, endpoint_id: uuid.UUID) -> Endpoint:
    endpoint = (
        await session.execute(select(Endpoint).where(Endpoint.id == endpoint_id, Endpoint.merchant_id == merchant.id))
    ).scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="endpoint not found")
    return endpoint


# ---------------------------------------------------------------- merchants


async def _issue_session(redis: Redis, merchant_id: uuid.UUID) -> tuple[str, datetime]:
    try:
        return await create_session(redis, merchant_id)
    except RedisError as exc:
        logger.exception("could not store session")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="sessions are temporarily unavailable",
            headers={"Retry-After": "5"},
        ) from exc


async def _rotate_api_key(session: AsyncSession, redis: Redis, merchant: Merchant) -> MerchantCreated:
    """Revoke ALL sessions, replace the API key (the old one stops working at once), return it with a new session."""
    api_key = generate_api_key()
    try:
        await revoke_all_sessions(redis, merchant.id)  # every old session dies before the new key and session exist
    except RedisError as exc:
        logger.exception("could not revoke sessions")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="sessions are temporarily unavailable",
            headers={"Retry-After": "5"},
        ) from exc
    token, expires_at = await _issue_session(redis, merchant.id)
    merchant.api_key_hash = hash_api_key(api_key)
    await session.commit()
    await session.refresh(merchant)
    return MerchantCreated(
        merchant=MerchantRead.model_validate(merchant), api_key=api_key, session_token=token, expires_at=expires_at
    )


REGISTRATION_FAILED_DETAIL = "registration failed: invalid challenge or signature"


@router.post(
    "/merchants",
    status_code=status.HTTP_201_CREATED,
    response_model=MerchantCreated,
    tags=["merchants"],
    dependencies=[Depends(limit_by_ip(RATE_LIMIT_CREATE_MERCHANT))],
)
async def create_merchant(
    payload: MerchantCreate, session: DbSession, redis: Annotated[Redis, Depends(get_redis)]
) -> MerchantCreated:
    # The wallet proof comes first: only the owner of the address can learn whether it is already registered.
    proven = await consume_wallet_proof(
        redis,
        purpose="register",
        address=payload.kaspa_address,
        challenge=payload.challenge,
        public_key=payload.public_key,
        signature=payload.signature,
    )
    if not proven:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=REGISTRATION_FAILED_DETAIL)
    email = str(payload.email).lower() if payload.email is not None else None
    if await session.scalar(select(Merchant.id).where(Merchant.kaspa_address == payload.kaspa_address)) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="a merchant with this Kaspa address already exists"
        )
    if email is not None and await session.scalar(select(Merchant.id).where(Merchant.email == email)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="a merchant with this email already exists")
    api_key = generate_api_key()
    merchant_id = uuid.uuid4()
    # The session goes in first: once the merchant row is committed the one-time api_key must reach the caller.
    token, expires_at = await _issue_session(redis, merchant_id)
    merchant = Merchant(
        id=merchant_id,
        kaspa_address=payload.kaspa_address,
        display_name=payload.display_name,
        email=email,
        api_key_hash=hash_api_key(api_key),
    )
    session.add(merchant)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a merchant with this Kaspa address or email already exists",
        ) from exc
    await session.refresh(merchant)
    return MerchantCreated(
        merchant=MerchantRead.model_validate(merchant), api_key=api_key, session_token=token, expires_at=expires_at
    )


@router.get("/merchants/me", response_model=MerchantRead, tags=["merchants"])
async def read_current_merchant(merchant: CurrentMerchant) -> MerchantRead:
    return MerchantRead.model_validate(merchant)


@router.post("/merchants/me/rotate-key", response_model=MerchantCreated, tags=["merchants"])
async def rotate_key(
    merchant: CurrentMerchant, session: DbSession, redis: Annotated[Redis, Depends(get_redis)]
) -> MerchantCreated:
    return await _rotate_api_key(session, redis, merchant)


# ---------------------------------------------------------------- wallet-signature login


LOGIN_FAILED_DETAIL = "login failed: invalid address, challenge or signature"


def _login_failed() -> HTTPException:
    """The single response for every way a login can fail, so callers cannot tell the causes apart."""
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=LOGIN_FAILED_DETAIL)


@router.post(
    "/auth/challenge",
    response_model=AuthChallenge,
    tags=["auth"],
    dependencies=[Depends(limit_by_ip(RATE_LIMIT_AUTH_CHALLENGE))],
)
async def auth_challenge(payload: AuthChallengeRequest, redis: Annotated[Redis, Depends(get_redis)]) -> AuthChallenge:
    """Always 200, even for unknown addresses, so it cannot be used to find out who is registered."""
    challenge = new_auth_challenge(payload.purpose, payload.kaspa_address)
    try:
        await redis.set(
            auth_challenge_key(challenge.purpose, challenge.address, challenge.nonce),
            challenge.message,
            ex=AUTH_CHALLENGE_TTL_SECONDS,
        )
    except RedisError as exc:
        logger.exception("could not store auth challenge")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="login is temporarily unavailable"
        ) from exc
    return AuthChallenge(kaspa_address=challenge.address, challenge=challenge.message, expires_at=challenge.expires_at)


@router.post(
    "/auth/verify",
    response_model=SessionIssued,
    tags=["auth"],
    dependencies=[Depends(limit_by_ip(RATE_LIMIT_AUTH_VERIFY))],
)
async def auth_verify(
    payload: AuthVerifyRequest, session: DbSession, redis: Annotated[Redis, Depends(get_redis)]
) -> SessionIssued:
    """Exchange a signed `login` challenge for a `kgs_` dashboard session. The API key is NOT rotated."""
    # Every check runs (and the challenge is always consumed) regardless of earlier failures, so the work done
    # and the response are identical for unknown addresses, bad challenges and bad signatures.
    proven = await consume_wallet_proof(
        redis,
        purpose="login",
        address=payload.kaspa_address,
        challenge=payload.challenge,
        public_key=payload.public_key,
        signature=payload.signature,
    )
    merchant = (
        await session.execute(select(Merchant).where(Merchant.kaspa_address == payload.kaspa_address))
    ).scalar_one_or_none()
    if merchant is None or not proven:
        raise _login_failed()
    if not merchant.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="merchant account is disabled")
    try:
        token, expires_at = await create_session(redis, merchant.id)
    except RedisError as exc:
        logger.exception("could not store session")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="login is temporarily unavailable"
        ) from exc
    return SessionIssued(merchant=MerchantRead.model_validate(merchant), session_token=token, expires_at=expires_at)


@router.delete(
    "/auth/session",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["auth"],
    responses={400: {"description": "the caller used an API key, not a dashboard session"}},
)
async def auth_logout(
    merchant: CurrentMerchant,
    token: Annotated[str, Depends(get_bearer_token)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> Response:
    """Log out the `kgs_` session in the Authorization header; other sessions and the API key are untouched.

    An API key is not a session, so there is nothing to log out: that is a 400 pointing at rotate-key, which is how
    a `kg_` key is revoked.
    """
    if not token.startswith(SESSION_TOKEN_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "logout needs a dashboard session (kgs_...); revoke an API key with POST /v1/merchants/me/rotate-key"
            ),
        )
    try:
        await revoke_session(redis, merchant.id, token)
    except RedisError as exc:
        logger.exception("could not revoke session")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="sessions are temporarily unavailable",
            headers={"Retry-After": "5"},
        ) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- endpoints


@router.post("/endpoints", status_code=status.HTTP_201_CREATED, response_model=EndpointRead, tags=["endpoints"])
async def create_endpoint(payload: EndpointCreate, merchant: CurrentMerchant, session: DbSession) -> EndpointRead:
    encrypted = _headers_or_422(payload.upstream_headers)
    explicit_slug = payload.slug
    for _ in range(1 if explicit_slug else MAX_SLUG_ATTEMPTS):
        endpoint = Endpoint(
            merchant_id=merchant.id,
            slug=explicit_slug or generate_slug(payload.name),
            name=payload.name,
            description=payload.description,
            category=payload.category,
            upstream_url=str(payload.upstream_url),
            upstream_headers_encrypted=encrypted,
            price_currency=payload.price_currency,
            price_sompi=payload.price_sompi if payload.price_currency is PriceCurrency.KAS else None,
            price_usd_micros=payload.price_usd_micros if payload.price_currency is PriceCurrency.USD else None,
            allowed_methods=list(payload.allowed_methods),
            is_public_listed=payload.is_public_listed,
        )
        session.add(endpoint)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            if explicit_slug:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="slug is already taken") from exc
            continue
        await session.refresh(endpoint)
        return EndpointRead.model_validate(endpoint)
    if explicit_slug:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="slug is already taken")
    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="could not allocate a unique slug")


@router.get("/endpoints", response_model=list[EndpointRead], tags=["endpoints"])
async def list_endpoints(merchant: CurrentMerchant, session: DbSession) -> list[EndpointRead]:
    rows = (
        await session.execute(
            select(Endpoint).where(Endpoint.merchant_id == merchant.id).order_by(Endpoint.created_at, Endpoint.id)
        )
    ).scalars()
    return [EndpointRead.model_validate(row) for row in rows]


@router.get("/endpoints/{endpoint_id}", response_model=EndpointRead, tags=["endpoints"])
async def read_endpoint(endpoint_id: uuid.UUID, merchant: CurrentMerchant, session: DbSession) -> EndpointRead:
    return EndpointRead.model_validate(await _owned_endpoint(session, merchant, endpoint_id))


_NON_NULLABLE_UPDATE_FIELDS = frozenset(
    {"name", "upstream_url", "upstream_headers", "allowed_methods", "is_active", "is_public_listed"}
)
_PRICE_UPDATE_FIELDS = frozenset({"price_currency", "price_sompi", "price_usd_micros"})


def _apply_price_update(endpoint: Endpoint, changes: dict[str, Any]) -> None:
    """Merge price fields into `endpoint` so the row always satisfies `price_matches_currency`."""
    if "price_currency" in changes and changes["price_currency"] is None:
        raise HTTPException(status_code=422, detail="fields cannot be null: price_currency")
    currency = changes.get("price_currency", endpoint.price_currency)
    if currency is PriceCurrency.KAS:
        sompi = changes.get("price_sompi", endpoint.price_sompi)
        if sompi is None:
            raise HTTPException(status_code=422, detail="price_sompi is required when price_currency is KAS")
        endpoint.price_currency = PriceCurrency.KAS
        endpoint.price_sompi = sompi
        endpoint.price_usd_micros = None
    else:
        micros = changes.get("price_usd_micros", endpoint.price_usd_micros)
        if micros is None:
            raise HTTPException(status_code=422, detail="price_usd_micros is required when price_currency is USD")
        endpoint.price_currency = PriceCurrency.USD
        endpoint.price_usd_micros = micros
        endpoint.price_sompi = None
    for field in _PRICE_UPDATE_FIELDS:
        changes.pop(field, None)


@router.patch("/endpoints/{endpoint_id}", response_model=EndpointRead, tags=["endpoints"])
async def update_endpoint(
    endpoint_id: uuid.UUID, payload: EndpointUpdate, merchant: CurrentMerchant, session: DbSession
) -> EndpointRead:
    endpoint = await _owned_endpoint(session, merchant, endpoint_id)
    changes: dict[str, Any] = payload.model_dump(exclude_unset=True)
    nulled = sorted(name for name in _NON_NULLABLE_UPDATE_FIELDS if name in changes and changes[name] is None)
    if nulled:
        raise HTTPException(status_code=422, detail=f"fields cannot be null: {', '.join(nulled)}")
    if changes.keys() & _PRICE_UPDATE_FIELDS:
        _apply_price_update(endpoint, changes)
    if "upstream_headers" in changes:
        endpoint.upstream_headers_encrypted = _headers_or_422(changes.pop("upstream_headers"))
    if "upstream_url" in changes:
        endpoint.upstream_url = str(payload.upstream_url)
        changes.pop("upstream_url")
    for field, value in changes.items():
        setattr(endpoint, field, value)
    await session.commit()
    await session.refresh(endpoint)
    return EndpointRead.model_validate(endpoint)


@router.delete("/endpoints/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["endpoints"])
async def delete_endpoint(endpoint_id: uuid.UUID, merchant: CurrentMerchant, session: DbSession) -> Response:
    endpoint = await _owned_endpoint(session, merchant, endpoint_id)
    endpoint.is_active = False
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------- settlements & analytics


@router.get("/settlements", response_model=list[SettlementRead], tags=["settlements"])
async def list_settlements(
    merchant: CurrentMerchant,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[SettlementRead]:
    rows = (
        await session.execute(
            select(Settlement)
            .where(Settlement.merchant_id == merchant.id)
            .order_by(desc(Settlement.created_at), desc(Settlement.id))
            .limit(limit)
            .offset(offset)
        )
    ).scalars()
    return [SettlementRead.model_validate(row) for row in rows]


@router.get("/analytics", response_model=MerchantAnalytics, tags=["analytics"])
async def analytics(
    merchant: CurrentMerchant,
    session: DbSession,
    start: datetime | None = None,
    end: datetime | None = None,
) -> MerchantAnalytics:
    period_start, period_end = resolve_period(start, end)
    in_period = (
        Settlement.merchant_id == merchant.id,
        Settlement.status == SettlementStatus.SETTLED,
        Settlement.settled_at >= period_start,
        Settlement.settled_at < period_end,
    )
    zero = Decimal(0)
    totals = (
        await session.execute(
            select(
                func.count(Settlement.id),
                func.coalesce(func.sum(Settlement.amount_sompi), 0),
                func.coalesce(func.sum(Settlement.merchant_amount_sompi), 0),
                func.coalesce(func.sum(Settlement.fee_amount_sompi), 0),
                func.coalesce(func.sum(Settlement.usd_value_at_settlement), zero),
            ).where(*in_period)
        )
    ).one()
    day = cast(func.timezone("UTC", Settlement.settled_at), Date).label("day")
    daily_rows = (
        await session.execute(
            select(
                day,
                func.count(Settlement.id),
                func.coalesce(func.sum(Settlement.amount_sompi), 0),
                func.coalesce(func.sum(Settlement.merchant_amount_sompi), 0),
                func.coalesce(func.sum(Settlement.fee_amount_sompi), 0),
                func.coalesce(func.sum(Settlement.usd_value_at_settlement), zero),
            )
            .where(*in_period)
            .group_by(day)
            .order_by(day)
        )
    ).all()
    endpoint_rows = (
        await session.execute(
            select(
                Endpoint.id,
                Endpoint.slug,
                Endpoint.name,
                func.count(Settlement.id),
                func.coalesce(func.sum(Settlement.merchant_amount_sompi), 0),
                func.coalesce(func.sum(Settlement.usd_value_at_settlement), zero),
            )
            .join(Endpoint, Endpoint.id == Settlement.endpoint_id)
            .where(*in_period)
            .group_by(Endpoint.id, Endpoint.slug, Endpoint.name)
            .order_by(desc(func.sum(Settlement.merchant_amount_sompi)), Endpoint.slug)
        )
    ).all()
    return MerchantAnalytics(
        merchant_id=merchant.id,
        network=get_settings().kaspa_network,
        period_start=period_start,
        period_end=period_end,
        total_calls=int(totals[0]),
        gross_sompi=int(totals[1]),
        merchant_sompi=int(totals[2]),
        fee_sompi=int(totals[3]),
        usd_value=Decimal(totals[4]),
        daily=[
            DailyRevenue(
                day=row[0],
                call_count=int(row[1]),
                gross_sompi=int(row[2]),
                merchant_sompi=int(row[3]),
                fee_sompi=int(row[4]),
                usd_value=Decimal(row[5]),
            )
            for row in daily_rows
        ],
        by_endpoint=[
            EndpointRevenue(
                endpoint_id=row[0],
                slug=row[1],
                name=row[2],
                call_count=int(row[3]),
                merchant_sompi=int(row[4]),
                usd_value=Decimal(row[5]),
            )
            for row in endpoint_rows
        ],
    )


def _csv_line(values: list[str]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\r\n").writerow(values)
    return buffer.getvalue()


async def _tax_csv_stream(
    maker: async_sessionmaker[AsyncSession],
    merchant_id: uuid.UUID,
    period_start: datetime,
    period_end: datetime,
) -> AsyncIterator[str]:
    yield _csv_line(list(TAX_CSV_COLUMNS))
    statement = (
        select(Settlement, Endpoint.slug)
        .join(Endpoint, Endpoint.id == Settlement.endpoint_id)
        .where(
            Settlement.merchant_id == merchant_id,
            Settlement.status == SettlementStatus.SETTLED,
            Settlement.settled_at >= period_start,
            Settlement.settled_at < period_end,
        )
        .order_by(Settlement.settled_at, Settlement.id)
        .execution_options(yield_per=CSV_CHUNK_ROWS)
    )
    async with maker() as session:
        result = await session.stream(statement)
        pending: list[str] = []
        async for settlement, slug in result:
            settled_at = settlement.settled_at if settlement.settled_at is not None else settlement.created_at
            row = TaxReportRow(
                settled_at_utc=_as_utc(settled_at),
                tx_id=settlement.tx_id,
                network=settlement.network,
                endpoint_slug=slug,
                payer_address=settlement.payer_address,
                gross_kas=sompi_to_kas(settlement.amount_sompi),
                merchant_kas=sompi_to_kas(settlement.merchant_amount_sompi),
                platform_fee_kas=sompi_to_kas(settlement.fee_amount_sompi),
                kas_usd_rate=settlement.kas_usd_rate,
                usd_value_at_settlement=settlement.usd_value_at_settlement,
            )
            pending.append(_csv_line(row.as_csv_row()))
            if len(pending) >= CSV_CHUNK_ROWS:
                yield "".join(pending)
                pending = []
        if pending:
            yield "".join(pending)


@router.get("/tax-report.csv", response_class=StreamingResponse, tags=["analytics"])
async def tax_report(
    merchant: CurrentMerchant,
    maker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    start: datetime | None = None,
    end: datetime | None = None,
) -> StreamingResponse:
    period_start, period_end = resolve_period(start, end)
    filename = f"kasgate-tax-report-{period_start:%Y%m%d}-{period_end:%Y%m%d}.csv"
    return StreamingResponse(
        _tax_csv_stream(maker, merchant.id, period_start, period_end),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------- public: directory & quote


async def _build_directory(session: AsyncSession) -> DirectoryManifest:
    settings = get_settings()
    rows = (
        await session.execute(
            select(Endpoint, Merchant.display_name, Merchant.kaspa_address)
            .join(Merchant, Merchant.id == Endpoint.merchant_id)
            .where(
                Endpoint.is_active.is_(True),
                Endpoint.is_public_listed.is_(True),
                Merchant.is_active.is_(True),
            )
            .order_by(Endpoint.created_at, Endpoint.id)
            .limit(DIRECTORY_MAX_ENTRIES)
        )
    ).all()
    entries = [
        DirectoryEntry(
            slug=endpoint.slug,
            name=endpoint.name,
            description=endpoint.description,
            category=endpoint.category,
            merchant_display_name=display_name,
            pay_to=pay_to,
            price_currency=endpoint.price_currency,
            price_sompi=endpoint.price_sompi,
            price_kas=None if endpoint.price_sompi is None else sompi_to_kas(endpoint.price_sompi),
            price_usd_micros=endpoint.price_usd_micros,
            price_usd=None if endpoint.price_usd_micros is None else usd_micros_to_usd(endpoint.price_usd_micros),
            allowed_methods=list(endpoint.allowed_methods),
            proxy_url=f"{settings.public_base_url}{PROXY_PATH_PREFIX}/{endpoint.slug}",
        )
        for endpoint, display_name, pay_to in rows
    ]
    return DirectoryManifest(
        network=settings.kaspa_network,
        gateway_base_url=settings.public_base_url,
        min_output_sompi=settings.min_output_sompi,
        generated_at=datetime.now(UTC),
        endpoints=entries,
    )


@router.get("/directory", response_model=DirectoryManifest, tags=["public"])
async def directory(session: DbSession, redis: Annotated[Redis, Depends(get_redis)]) -> Response:
    headers = {"Cache-Control": f"public, max-age={DIRECTORY_CACHE_TTL_SECONDS}"}
    try:
        cached = await redis.get(DIRECTORY_CACHE_KEY)
    except RedisError:
        logger.warning("directory cache read failed", exc_info=True)
        cached = None
    if cached:
        return Response(content=cached, media_type="application/json", headers=headers)
    manifest = await _build_directory(session)
    body = manifest.model_dump_json()
    try:
        await redis.set(DIRECTORY_CACHE_KEY, body, ex=DIRECTORY_CACHE_TTL_SECONDS)
    except RedisError:
        logger.warning("directory cache write failed", exc_info=True)
    return Response(content=body, media_type="application/json", headers=headers)


@router.get(
    "/quote/{slug}",
    response_model=PaymentRequired,
    responses={404: {"model": X402ErrorBody}, 503: {"model": X402ErrorBody}},
    tags=["public"],
    dependencies=[Depends(limit_by_ip(RATE_LIMIT_UNPAID_PROXY))],
)
async def quote(
    slug: str,
    session: DbSession,
    redis: Annotated[Redis, Depends(get_redis)],
    oracle: Annotated[RateProvider, Depends(get_oracle)],
) -> Response:
    """A payable quote: the same nonce-bound challenge `/p/{slug}` answers with, valid for `payment_max_age_seconds`."""
    row = (
        await session.execute(
            select(Endpoint, Merchant.kaspa_address)
            .join(Merchant, Merchant.id == Endpoint.merchant_id)
            .where(Endpoint.slug == slug, Endpoint.is_active.is_(True), Merchant.is_active.is_(True))
        )
    ).one_or_none()
    if row is None:
        body = X402ErrorBody(error="endpoint_not_found", message=f"no active endpoint with slug '{slug}'")
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content=body.model_dump(mode="json"))
    endpoint, pay_to = row
    try:
        required = await issue_payment_challenge(redis, endpoint, pay_to, oracle)
    except OracleUnavailableError as exc:
        logger.warning("USD price unavailable for quote %s: %s", slug, exc)
        body = X402ErrorBody(error="price_unavailable", message="KAS/USD price is temporarily unavailable")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=body.model_dump(mode="json"),
            headers={"Retry-After": "5"},
        )
    except ValueError as exc:
        logger.error("endpoint %s has an unquotable price: %s", slug, exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "endpoint price is not currently payable"},
        )
    except RedisError:
        logger.exception("quote store unavailable")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "quotes are temporarily unavailable"},
            headers={"Retry-After": "5"},
        )
    return JSONResponse(
        content=required.model_dump(mode="json", exclude_none=True), headers={"Cache-Control": "no-store"}
    )


# ---------------------------------------------------------------- public agent catalog

CATALOG_CACHE_KEY = "kasgate:catalog:v1"
CATALOG_CACHE_TTL_SECONDS = DIRECTORY_CACHE_TTL_SECONDS
CATALOG_MAX_ENTRIES = DIRECTORY_MAX_ENTRIES
CATALOG_DEFAULT_LIMIT = 50
CATALOG_MAX_LIMIT = 200
CATALOG_QUERY_MAX_LENGTH = 120

well_known_router = APIRouter(tags=["public"])


def encode_catalog_cursor(created_at: datetime, endpoint_id: uuid.UUID) -> str:
    stamp = created_at.astimezone(UTC).isoformat()
    raw = f"{stamp}|{endpoint_id}".encode()
    return base64_urlsafe(raw)


def decode_catalog_cursor(value: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64_urlsafe_decode(value).decode("ascii")
        stamp, id_s = raw.split("|", 1)
        created_at = datetime.fromisoformat(stamp)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return created_at.astimezone(UTC), uuid.UUID(id_s)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=422, detail="invalid cursor") from exc


def base64_urlsafe(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def base64_urlsafe_decode(value: str) -> bytes:
    padded = value + "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def catalog_if_none_match_hits(header: str | None, etag: str) -> bool:
    if header is None or not header.strip():
        return False
    raw = header.strip()
    if raw == "*":
        return True
    token = etag.strip().strip('"')
    for part in raw.split(","):
        item = part.strip()
        if item.startswith("W/"):
            item = item[2:].strip()
        if item.strip('"') == token:
            return True
    return False


def _endpoint_input_schema(endpoint: Endpoint) -> dict[str, object] | None:
    # The endpoints table has no input_schema column; getattr keeps this additive if one is added later.
    value = getattr(endpoint, "input_schema", None)
    return value if isinstance(value, dict) else None


def _catalog_item(endpoint: Endpoint, *, network: str, paid_url: str) -> CatalogEndpoint:
    return CatalogEndpoint(
        slug=endpoint.slug,
        name=endpoint.name,
        description=endpoint.description,
        methods=list(endpoint.allowed_methods),
        price_currency=endpoint.price_currency,
        price_sompi=endpoint.price_sompi,
        price_kas=None if endpoint.price_sompi is None else sompi_to_kas(endpoint.price_sompi),
        price_usd_micros=endpoint.price_usd_micros,
        price_usd=None if endpoint.price_usd_micros is None else usd_micros_to_usd(endpoint.price_usd_micros),
        network=network,
        schemes=[X402_SCHEME],
        paid_url=paid_url,
        input_schema=_endpoint_input_schema(endpoint),
    )


async def _listed_catalog_rows(session: AsyncSession) -> list[tuple[Endpoint, str]]:
    """Return (endpoint, cursor) for every active, publicly listed endpoint of an active merchant."""
    rows = (
        await session.execute(
            select(Endpoint)
            .join(Merchant, Merchant.id == Endpoint.merchant_id)
            .where(
                Endpoint.is_active.is_(True),
                Endpoint.is_public_listed.is_(True),
                Merchant.is_active.is_(True),
            )
            .order_by(Endpoint.created_at, Endpoint.id)
            .limit(CATALOG_MAX_ENTRIES)
        )
    ).scalars().all()
    return [(endpoint, encode_catalog_cursor(endpoint.created_at, endpoint.id)) for endpoint in rows]


async def _catalog_snapshot(session: AsyncSession, redis: Redis) -> dict[str, Any]:
    try:
        cached = await redis.get(CATALOG_CACHE_KEY)
    except RedisError:
        logger.warning("catalog cache read failed", exc_info=True)
        cached = None
    if cached:
        try:
            payload = json.loads(cached)
            if isinstance(payload, dict) and "etag" in payload and "entries" in payload:
                return payload
        except ValueError:
            logger.warning("catalog cache contained invalid JSON", exc_info=True)
    settings = get_settings()
    packed = await _listed_catalog_rows(session)
    entries: list[dict[str, Any]] = []
    public_items: list[dict[str, Any]] = []
    for endpoint, cursor in packed:
        item = _catalog_item(
            endpoint,
            network=settings.kaspa_network,
            paid_url=f"{settings.public_base_url}{PROXY_PATH_PREFIX}/{endpoint.slug}",
        )
        public = item.model_dump(mode="json")
        public_items.append(public)
        entries.append({"cursor": cursor, "item": public})
    digest = hashlib.sha256(json.dumps(public_items, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    payload = {
        "etag": f'"{digest}"',
        "network": settings.kaspa_network,
        "gateway_base_url": settings.public_base_url,
        "entries": entries,
    }
    try:
        await redis.set(CATALOG_CACHE_KEY, json.dumps(payload), ex=CATALOG_CACHE_TTL_SECONDS)
    except RedisError:
        logger.warning("catalog cache write failed", exc_info=True)
    return payload


def _catalog_headers(etag: str) -> dict[str, str]:
    return {
        "Cache-Control": f"public, max-age={CATALOG_CACHE_TTL_SECONDS}",
        "ETag": etag,
    }


def _paginate_catalog(
    snapshot: dict[str, Any], *, limit: int, cursor: str | None, query: str
) -> CatalogPage:
    entries: list[dict[str, Any]] = list(snapshot["entries"])
    if query:
        needle = query.casefold()

        def _hit(entry: dict[str, Any]) -> bool:
            item = entry["item"]
            haystacks = [item.get("slug") or "", item.get("name") or "", item.get("description") or ""]
            return any(needle in str(text).casefold() for text in haystacks)

        entries = [entry for entry in entries if _hit(entry)]
    if cursor is not None:
        # Validate even when the snapshot is empty so a junk cursor never looks like "no more pages".
        decode_catalog_cursor(cursor)
        start = 0
        for index, entry in enumerate(entries):
            if entry["cursor"] == cursor:
                start = index + 1
                break
        else:
            # Cursor is well-formed but not in this snapshot (expired, filtered out, or from another query).
            start = len(entries)
        entries = entries[start:]
    page = entries[:limit]
    next_cursor = page[-1]["cursor"] if len(entries) > limit else None
    items = [CatalogEndpoint.model_validate(entry["item"]) for entry in page]
    return CatalogPage(
        network=snapshot["network"],
        gateway_base_url=snapshot["gateway_base_url"],
        items=items,
        next_cursor=next_cursor,
        limit=limit,
    )


async def build_catalog_response(
    session: AsyncSession,
    redis: Redis,
    request: Request,
    *,
    limit: int,
    cursor: str | None,
    q: str | None,
) -> Response:
    query = (q or "").strip()
    if len(query) > CATALOG_QUERY_MAX_LENGTH:
        raise HTTPException(status_code=422, detail=f"q must be at most {CATALOG_QUERY_MAX_LENGTH} characters")
    snapshot = await _catalog_snapshot(session, redis)
    page = _paginate_catalog(snapshot, limit=limit, cursor=cursor, query=query)
    body = page.model_dump_json()
    etag = f'"{hashlib.sha256(body.encode()).hexdigest()}"'
    headers = _catalog_headers(etag)
    if catalog_if_none_match_hits(request.headers.get("if-none-match"), etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


@router.get("/catalog", response_model=CatalogPage, tags=["public"])
async def catalog(
    request: Request,
    session: DbSession,
    redis: Annotated[Redis, Depends(get_redis)],
    limit: int = Query(default=CATALOG_DEFAULT_LIMIT, ge=1, le=CATALOG_MAX_LIMIT),
    cursor: str | None = Query(default=None, min_length=1, max_length=200),
    q: str | None = Query(default=None, max_length=CATALOG_QUERY_MAX_LENGTH),
) -> Response:
    """Public, paginated catalog of merchant-listed endpoints. Cacheable; never includes secrets or upstream URLs."""
    return await build_catalog_response(session, redis, request, limit=limit, cursor=cursor, q=q)


@well_known_router.get("/.well-known/x402", response_model=CatalogPage)
async def well_known_x402(
    request: Request,
    session: DbSession,
    redis: Annotated[Redis, Depends(get_redis)],
    limit: int = Query(default=CATALOG_DEFAULT_LIMIT, ge=1, le=CATALOG_MAX_LIMIT),
    cursor: str | None = Query(default=None, min_length=1, max_length=200),
    q: str | None = Query(default=None, max_length=CATALOG_QUERY_MAX_LENGTH),
) -> Response:
    """Same catalog as GET /v1/catalog, at the x402 well-known path."""
    return await build_catalog_response(session, redis, request, limit=limit, cursor=cursor, q=q)


@router.get("/catalog/{slug}", response_model=CatalogEndpoint, tags=["public"])
async def catalog_item(
    slug: str, request: Request, session: DbSession, redis: Annotated[Redis, Depends(get_redis)]
) -> Response:
    snapshot = await _catalog_snapshot(session, redis)
    match = next((entry["item"] for entry in snapshot["entries"] if entry["item"]["slug"] == slug), None)
    if match is None:
        body = X402ErrorBody(error="endpoint_not_found", message=f"no listed endpoint with slug '{slug}'")
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content=body.model_dump(mode="json"))
    item_digest = hashlib.sha256(json.dumps(match, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    item_etag = f'"{item_digest}"'
    item_headers = _catalog_headers(item_etag)
    if catalog_if_none_match_hits(request.headers.get("if-none-match"), item_etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=item_headers)
    return Response(
        content=CatalogEndpoint.model_validate(match).model_dump_json(),
        media_type="application/json",
        headers=item_headers,
    )


def render_llms_txt() -> str:
    settings = get_settings()
    base = settings.public_base_url
    network = settings.kaspa_network
    return (
        "# KasGate\n"
        "\n"
        "KasGate is an x402 v2 pay-per-call gateway on Kaspa. An agent calls a paid URL; without payment the\n"
        "gateway answers HTTP 402 and a PAYMENT-REQUIRED header. The agent builds and signs one Kaspa transaction\n"
        "and retries with PAYMENT-SIGNATURE. A successful call returns the upstream response plus PAYMENT-RESPONSE.\n"
        "\n"
        f"This deployment is on {network}.\n"
        + (
            "It is the Kaspa testnet-10 beta (address prefix kaspatest:). "
            "Mainnet is not launched. Do not send mainnet funds.\n"
            if network == "kaspa:testnet-10"
            else ""
        )
        + "\n"
        "## x402 v2 headers\n"
        "\n"
        "All three headers are Base64(JSON) using the standard alphabet, padded.\n"
        "\n"
        "- PAYMENT-REQUIRED: {x402Version: 2, scheme: \"kaspa-exact-v1\", network, asset: \"KAS\",\n"
        "  amount (decimal sompi string), payTo, platformFee {feeTo, feeAmount}, nonce, expiresAt}.\n"
        "- PAYMENT-SIGNATURE: {txId, rawTx, payerAddress, nonce, payerProof}. The signed transaction payload\n"
        "  must be kasgate:v1:<nonce>. payerProof is a Schnorr signature over kasgate:v1:<nonce>:<txId>.\n"
        "- PAYMENT-RESPONSE: {txId, settled: true, network, usdValueAtSettlement}.\n"
        "\n"
        "Scheme accepted: kaspa-exact-v1. Paid path prefix: /p/{slug} and /p/{slug}/{path}.\n"
        "\n"
        "## Fee split and price floor\n"
        "\n"
        "Every payment is a single non-custodial Kaspa transaction:\n"
        "- output 0 is the merchant (payTo): the price minus the platform fee (98.5% once the fee is the 1.5% rate).\n"
        "- output 1 is the platform treasury (feeTo): max(1.5% of the price, 0.2 KAS).\n"
        "- outputs 2+ may only be change back to the buyer.\n"
        "\n"
        "The minimum endpoint price is 0.6 KAS (60,000,000 sompi). At that price the merchant receives 0.4 KAS and\n"
        "the treasury receives the 0.2 KAS floor. Funds never pass through KasGate custody.\n"
        "\n"
        "## Catalog\n"
        "\n"
        "A public machine-readable catalog of endpoints the merchant marked listed (active merchants only):\n"
        f"- {base}/.well-known/x402\n"
        f"- {base}/v1/catalog\n"
        "\n"
        "Query parameters: limit (1-200, default 50), cursor (opaque, from next_cursor), q (optional substring\n"
        "filter on slug, name, or description). Responses include Cache-Control and ETag; send If-None-Match for 304.\n"
        "Each item has slug, name, description, methods, price_currency, price_sompi, price_kas, "
        "price_usd_micros, price_usd, network, schemes, paid_url, and input_schema. "
        "USD prices are not live-converted in the catalog; call GET /v1/quote/{slug} "
        "or the paid URL for the locked sompi amount.\n"
        "(null when the merchant has not published a schema). The catalog never includes API keys, emails, upstream\n"
        "URLs, or other merchant-private fields.\n"
        "\n"
        f"Inspect one listed endpoint: GET {base}/v1/catalog/{{slug}}.\n"
        f"Quote without paying (mints a one-time nonce): GET {base}/v1/quote/{{slug}}.\n"
        "\n"
        "## Buyer SDK and MCP\n"
        "\n"
        "Python SDK (kasgate-client): KasGateClient.discover(gateway_url) and KasGateSyncClient.discover fetch the\n"
        "catalog (read-only, follows pagination). Payments go through KasGateClient / KasGateSyncClient, which honour\n"
        "the max_price_sompi circuit breaker and never sign a challenge above that cap. Catalog entries priced above\n"
        "the cap are still listed and can be filtered with DiscoverResult.payable.\n"
        "\n"
        "MCP server (kasgate-mcp, stdio): search_catalog, inspect_endpoint, get_budget, call_with_budget, plus the\n"
        "original list_directory, quote_endpoint, and call_paid_endpoint tools. call_with_budget reserves the quoted\n"
        "price before signing, enforces a per-call maximum and a session spend cap (sompi), releases the reservation\n"
        "on failure, and never logs buyer keys. get_budget reports spent, reserved, and remaining sompi.\n"
        "\n"
        "Protocol details: docs/CONTRACTS.md in the KasGate repository (https://github.com/kfryster420-afk/kasgate).\n"
    )


@well_known_router.get("/llms.txt")
async def llms_txt() -> PlainTextResponse:
    return PlainTextResponse(
        render_llms_txt(),
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": f"public, max-age={CATALOG_CACHE_TTL_SECONDS}"},
    )


__all__ = ["router", "well_known_router", "render_llms_txt"]
