"""Merchant-facing REST API (`/v1/*`) and the public directory/quote routes."""

import csv
import hmac
import io
import logging
import re
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import Date, cast, desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.deps import get_db_session, get_redis, get_sessionmaker
from app.models import Endpoint, Merchant, Settlement, SettlementStatus
from app.schemas import (
    PROXY_PATH_PREFIX,
    TAX_CSV_COLUMNS,
    AuthChallenge,
    AuthChallengeRequest,
    AuthVerifyRequest,
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
    SettlementRead,
    TaxReportRow,
    X402ErrorBody,
    sompi_to_kas,
)
from app.security import (
    AUTH_CHALLENGE_TTL_SECONDS,
    RATE_LIMIT_AUTH_CHALLENGE,
    RATE_LIMIT_AUTH_VERIFY,
    RATE_LIMIT_CREATE_MERCHANT,
    challenge_key,
    encrypt_headers,
    generate_api_key,
    get_current_merchant,
    hash_api_key,
    limit_by_ip,
    limit_challenge_by_address,
    new_login_challenge,
    parse_login_challenge,
    validate_upstream_headers,
    verify_wallet_signature,
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


async def _rotate_api_key(session: AsyncSession, merchant: Merchant) -> MerchantCreated:
    api_key = generate_api_key()
    merchant.api_key_hash = hash_api_key(api_key)
    await session.commit()
    await session.refresh(merchant)
    return MerchantCreated(merchant=MerchantRead.model_validate(merchant), api_key=api_key)


@router.post(
    "/merchants",
    status_code=status.HTTP_201_CREATED,
    response_model=MerchantCreated,
    tags=["merchants"],
    dependencies=[Depends(limit_by_ip(RATE_LIMIT_CREATE_MERCHANT))],
)
async def create_merchant(payload: MerchantCreate, session: DbSession) -> MerchantCreated:
    email = str(payload.email).lower() if payload.email is not None else None
    if await session.scalar(select(Merchant.id).where(Merchant.kaspa_address == payload.kaspa_address)) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="a merchant with this Kaspa address already exists"
        )
    if email is not None and await session.scalar(select(Merchant.id).where(Merchant.email == email)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="a merchant with this email already exists")
    api_key = generate_api_key()
    merchant = Merchant(
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
    return MerchantCreated(merchant=MerchantRead.model_validate(merchant), api_key=api_key)


@router.get("/merchants/me", response_model=MerchantRead, tags=["merchants"])
async def read_current_merchant(merchant: CurrentMerchant) -> MerchantRead:
    return MerchantRead.model_validate(merchant)


@router.post("/merchants/me/rotate-key", response_model=MerchantCreated, tags=["merchants"])
async def rotate_key(merchant: CurrentMerchant, session: DbSession) -> MerchantCreated:
    return await _rotate_api_key(session, merchant)


# ---------------------------------------------------------------- wallet-signature login / key recovery


LOGIN_FAILED_DETAIL = "login failed: invalid address, challenge or signature"


def _login_failed() -> HTTPException:
    """The single response for every way a login can fail, so callers cannot tell the causes apart."""
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=LOGIN_FAILED_DETAIL)


@router.post(
    "/auth/challenge",
    response_model=AuthChallenge,
    tags=["auth"],
    dependencies=[
        Depends(limit_by_ip(RATE_LIMIT_AUTH_CHALLENGE)),
        Depends(limit_challenge_by_address),
    ],
)
async def auth_challenge(payload: AuthChallengeRequest, redis: Annotated[Redis, Depends(get_redis)]) -> AuthChallenge:
    challenge, message = new_login_challenge(payload.kaspa_address)
    try:
        await redis.set(challenge_key(challenge.address, challenge.nonce), message, ex=AUTH_CHALLENGE_TTL_SECONDS)
    except RedisError as exc:
        logger.exception("could not store login challenge")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="login is temporarily unavailable"
        ) from exc
    return AuthChallenge(kaspa_address=challenge.address, challenge=message, expires_at=challenge.expires_at)


@router.post(
    "/auth/verify",
    response_model=MerchantCreated,
    tags=["auth"],
    dependencies=[Depends(limit_by_ip(RATE_LIMIT_AUTH_VERIFY))],
)
async def auth_verify(
    payload: AuthVerifyRequest, session: DbSession, redis: Annotated[Redis, Depends(get_redis)]
) -> MerchantCreated:
    # Every check runs (and the challenge is always consumed) regardless of earlier failures, so the work done
    # and the response are identical for unknown addresses, bad challenges and bad signatures.
    challenge_ok = False
    try:
        challenge = parse_login_challenge(payload.challenge)
    except ValueError:
        challenge = None
    if challenge is not None:
        try:
            stored = await redis.getdel(challenge_key(challenge.address, challenge.nonce))
        except RedisError as exc:
            logger.exception("could not consume login challenge")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="login is temporarily unavailable"
            ) from exc
        challenge_ok = (
            stored is not None
            and hmac.compare_digest(str(stored), payload.challenge)
            and challenge.address == payload.kaspa_address
            and challenge.expires_at > datetime.now(UTC)
        )
    merchant = (
        await session.execute(select(Merchant).where(Merchant.kaspa_address == payload.kaspa_address))
    ).scalar_one_or_none()
    signature_ok = await verify_wallet_signature(
        payload.challenge, payload.signature, payload.public_key, payload.kaspa_address
    )
    if merchant is None or not challenge_ok or not signature_ok:
        raise _login_failed()
    if not merchant.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="merchant account is disabled")
    return await _rotate_api_key(session, merchant)


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
            price_sompi=payload.price_sompi,
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
    {"name", "upstream_url", "upstream_headers", "price_sompi", "allowed_methods", "is_active", "is_public_listed"}
)


@router.patch("/endpoints/{endpoint_id}", response_model=EndpointRead, tags=["endpoints"])
async def update_endpoint(
    endpoint_id: uuid.UUID, payload: EndpointUpdate, merchant: CurrentMerchant, session: DbSession
) -> EndpointRead:
    endpoint = await _owned_endpoint(session, merchant, endpoint_id)
    changes: dict[str, Any] = payload.model_dump(exclude_unset=True)
    nulled = sorted(name for name in _NON_NULLABLE_UPDATE_FIELDS if name in changes and changes[name] is None)
    if nulled:
        raise HTTPException(status_code=422, detail=f"fields cannot be null: {', '.join(nulled)}")
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
            price_sompi=endpoint.price_sompi,
            price_kas=sompi_to_kas(endpoint.price_sompi),
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
    responses={404: {"model": X402ErrorBody}},
    tags=["public"],
)
async def quote(slug: str, session: DbSession) -> Response:
    row = (
        await session.execute(
            select(Endpoint.price_sompi, Merchant.kaspa_address)
            .join(Merchant, Merchant.id == Endpoint.merchant_id)
            .where(Endpoint.slug == slug, Endpoint.is_active.is_(True), Merchant.is_active.is_(True))
        )
    ).one_or_none()
    if row is None:
        body = X402ErrorBody(error="endpoint_not_found", message=f"no active endpoint with slug '{slug}'")
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content=body.model_dump(mode="json"))
    try:
        required = PaymentRequired.for_endpoint(row[0], row[1])
    except ValueError as exc:
        logger.error("endpoint %s has an unquotable price: %s", slug, exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "endpoint price is not currently payable"},
        )
    return JSONResponse(content=required.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


__all__ = ["router"]
