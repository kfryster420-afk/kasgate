"""Paywalled wildcard reverse proxy: the HTTP 402 handshake for `/p/{slug}[/{path}]`.

Request lifecycle (CONTRACT v2)
-------------------------------
1. Resolve the active endpoint (404) and check the HTTP method (405).
2. Pre-flight everything that can fail without a payment (path, body size, upstream headers, upstream URL).
3. No `PAYMENT-SIGNATURE` -> issue a challenge: a fresh payment nonce, `PaymentRequired` bound to it and stored in Redis
   as `kasgate:nonce:{nonce}` (JSON of the challenge plus the endpoint slug, EX `payment_max_age_seconds`), then 402.
4. Decode the signature (bad header -> 402 `invalid_payment_header` / `wrong_network`, with a fresh challenge).
5. Load `required` FROM the stored nonce, never recompute it. A missing or expired nonce -> 402 `payment_expired` with a
   fresh `PAYMENT-REQUIRED`. A settlement that already exists for the txId may be re-delivered (see below) or is 409.
6. `engine.verify()` (pure, network-free) runs BEFORE any lock, so garbage never touches the replay locks.
7. Take the Redis replay lock `SET kasgate:tx:{txId} NX EX 600` BEFORE `engine.resolve_inputs` / `engine.settle` and
   before anything is forwarded. A held lock -> 409 `payment_replayed`. ONLY a verification failure releases it;
   success keeps it.
8. `engine.resolve_inputs` then `engine.settle` (submit + finality), then INSERT the SETTLED settlement (unique `tx_id`
   and `payment_nonce`; an IntegrityError is 409), then consume the nonce (GETDEL), then forward upstream.
9. Return the upstream response with `PAYMENT-RESPONSE` (or 502 `upstream_error`, still with `PAYMENT-RESPONSE`).

Redelivery: the same txId + nonce + slug (and the same payer, method and path) whose settlement has no upstream
status or a 5xx one, inside `redelivery_window_seconds` and below `redelivery_max_attempts` deliveries, is
forwarded again and counted in `Settlement.delivery_attempts`. Anything else for a settled txId is 409.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Final, cast
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.deps import (
    PaymentVerifier,
    RateProvider,
    VerifiedPaymentLike,
    get_db_session,
    get_engine,
    get_http_client,
    get_oracle,
    get_redis,
)
from app.kaspa_engine import PaymentVerificationError, RpcUnavailableError
from app.models import Endpoint, Merchant, Settlement, SettlementStatus
from app.oracle import usd_value
from app.schemas import (
    HEADER_PAYMENT_REQUIRED,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
    PROXY_PATH_PREFIX,
    PaymentRequired,
    PaymentResponse,
    PaymentSignature,
    X402ErrorBody,
    new_payment_nonce,
)
from app.security import (
    HOP_BY_HOP_HEADERS,
    PAYMENT_HEADERS,
    RATE_LIMIT_UNPAID_PROXY,
    client_ip,
    decrypt_headers,
    enforce_rate_limit,
)

logger = logging.getLogger("kasgate.proxy")

REPLAY_LOCK_PREFIX: Final = "kasgate:tx:"
REPLAY_LOCK_TTL_SECONDS: Final = 600
NONCE_KEY_PREFIX: Final = "kasgate:nonce:"
NONCE_LOCK_PREFIX: Final = "kasgate:noncelock:"
DELIVERY_LOCK_PREFIX: Final = "kasgate:delivery:"
DELIVERY_LOCK_TTL_SECONDS: Final = 60
DEAD_LETTER_KEY: Final = "kasgate:deadletter:settlements"
SERVICE_UNAVAILABLE_RETRY_SECONDS: Final = "5"
UPSTREAM_TIMEOUT_SECONDS: Final = 30.0
MAX_REQUEST_BODY_BYTES: Final = 10 * 1024 * 1024
MAX_RESPONSE_BODY_BYTES: Final = 32 * 1024 * 1024
USD_QUANTUM: Final = Decimal("0.00000001")
PROXIED_METHODS: Final = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

# Request headers that are never copied to the upstream: identity of the payer, framing, and hop-by-hop fields.
_DROPPED_REQUEST_HEADERS: Final[frozenset[str]] = (
    HOP_BY_HOP_HEADERS
    | PAYMENT_HEADERS
    | frozenset(
        {
            "host",
            "content-length",
            "accept-encoding",
            "cookie",
            "forwarded",
            "x-forwarded-for",
            "x-forwarded-host",
            "x-forwarded-proto",
            "x-forwarded-port",
            "x-real-ip",
        }
    )
)
# Upstream response headers that are never returned to the agent.
_DROPPED_RESPONSE_HEADERS: Final[frozenset[str]] = (
    HOP_BY_HOP_HEADERS
    | PAYMENT_HEADERS
    | frozenset({"content-length", "content-encoding", "set-cookie", "set-cookie2"})
)

_VERIFICATION_CODES: Final[frozenset[str]] = frozenset(
    {"invalid_payment_header", "underpaid", "invalid_fee_split", "wrong_network", "tx_rejected"}
)

router = APIRouter(tags=["proxy"])


def lock_key(tx_id: str) -> str:
    return f"{REPLAY_LOCK_PREFIX}{tx_id}"


def nonce_key(nonce: str) -> str:
    return f"{NONCE_KEY_PREFIX}{nonce}"


def nonce_lock_key(nonce: str) -> str:
    return f"{NONCE_LOCK_PREFIX}{nonce}"


def delivery_lock_key(tx_id: str) -> str:
    return f"{DELIVERY_LOCK_PREFIX}{tx_id}"


# ---------------------------------------------------------------- response helpers


def _error(
    status_code: int,
    error: str,
    message: str,
    *,
    required: PaymentRequired | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """x402 error response. `error` outside the X402ErrorBody vocabulary yields a plain `{error, message}` body.

    Every 503 carries `Retry-After`; 429 callers pass the limiter's own `Retry-After`.
    """
    out_headers = dict(headers or {})
    if status_code == 503:
        out_headers.setdefault("Retry-After", SERVICE_UNAVAILABLE_RETRY_SECONDS)
    if required is not None:
        out_headers[HEADER_PAYMENT_REQUIRED] = required.to_header()
    try:
        content = X402ErrorBody.model_validate(
            {"error": error, "message": message, "accepts": required.model_dump(mode="json") if required else None}
        ).model_dump(mode="json")
    except ValueError:
        content = {"error": error, "message": message}
    return JSONResponse(status_code=status_code, content=content, headers=out_headers)


def _payment_response_header(verified_tx_id: str, usd: Decimal) -> str:
    return PaymentResponse(
        txId=verified_tx_id,
        network=get_settings().kaspa_network,
        usdValueAtSettlement=usd.quantize(USD_QUANTUM),
    ).to_header()


# ---------------------------------------------------------------- request preparation


@dataclass(frozen=True)
class PreparedUpstream:
    url: httpx.URL
    headers: list[tuple[str, str]]
    body: bytes


class RequestRejectedError(Exception):
    def __init__(self, status_code: int, error: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error = error
        self.message = message


def build_upstream_url(upstream_url: str, path_suffix: str, query: str) -> httpx.URL:
    """Append `path_suffix` to the endpoint's upstream URL path and merge the query string.

    The scheme and authority always come from the stored upstream URL, never from the request.
    """
    segments = path_suffix.split("/") if path_suffix else []
    if any(segment in {".", ".."} for segment in segments) or "\x00" in path_suffix or "\\" in path_suffix:
        raise RequestRejectedError(400, "bad_request", "path traversal segments are not allowed")
    base = httpx.URL(upstream_url)
    base_path = base.path if base.path else "/"
    if path_suffix:
        joined = base_path.rstrip("/") + "/" + quote(path_suffix.lstrip("/"), safe="/:@!$&'()*+,;=-._~%")
    else:
        joined = base_path
    queries = [part for part in (base.query.decode("ascii"), query) if part]
    raw_path = joined + ("?" + "&".join(queries) if queries else "")
    return httpx.URL(scheme=base.scheme, host=base.host, port=base.port, raw_path=raw_path.encode("ascii"))


def build_upstream_headers(
    incoming: Iterable[tuple[str, str]], merchant_headers: dict[str, str]
) -> list[tuple[str, str]]:
    connection_tokens = {
        token.strip().lower()
        for name, value in incoming
        if name.lower() == "connection"
        for token in value.split(",")
        if token.strip()
    }
    overridden = {name.lower() for name in merchant_headers}
    forwarded = [
        (name, value)
        for name, value in incoming
        if name.lower() not in _DROPPED_REQUEST_HEADERS
        and name.lower() not in connection_tokens
        and name.lower() not in overridden
    ]
    return forwarded + list(merchant_headers.items())


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_REQUEST_BODY_BYTES:
        raise RequestRejectedError(413, "payload_too_large", "request body exceeds the gateway limit")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_REQUEST_BODY_BYTES:
            raise RequestRejectedError(413, "payload_too_large", "request body exceeds the gateway limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def _prepare_upstream(request: Request, endpoint: Endpoint, path_suffix: str) -> PreparedUpstream:
    url = build_upstream_url(endpoint.upstream_url, path_suffix, request.url.query)
    try:
        merchant_headers = decrypt_headers(endpoint.upstream_headers_encrypted)
    except ValueError as exc:
        logger.error("endpoint %s has unreadable upstream headers: %s", endpoint.slug, exc)
        raise RequestRejectedError(503, "service_unavailable", "endpoint upstream credentials are unavailable") from exc
    body = await _read_body(request)
    headers = build_upstream_headers(request.headers.items(), merchant_headers)
    return PreparedUpstream(url=url, headers=headers, body=body)


# ---------------------------------------------------------------- upstream call


class UpstreamFailedError(Exception):
    pass


@dataclass(frozen=True)
class UpstreamResult:
    status_code: int
    headers: list[tuple[str, str]]
    body: bytes


async def _call_upstream(client: httpx.AsyncClient, method: str, prepared: PreparedUpstream) -> UpstreamResult:
    try:
        async with asyncio.timeout(UPSTREAM_TIMEOUT_SECONDS):
            request = client.build_request(
                method, prepared.url, headers=prepared.headers, content=prepared.body or None
            )
            response = await client.send(request, stream=True, follow_redirects=False)
            try:
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BODY_BYTES:
                        raise UpstreamFailedError("upstream response exceeds the gateway limit")
                    chunks.append(chunk)
            finally:
                await response.aclose()
    except TimeoutError as exc:
        raise UpstreamFailedError("upstream request timed out") from exc
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise UpstreamFailedError(f"upstream request failed: {type(exc).__name__}") from exc
    return UpstreamResult(
        status_code=response.status_code,
        headers=[
            (name, value)
            for name, value in response.headers.multi_items()
            if name.lower() not in _DROPPED_RESPONSE_HEADERS
        ],
        body=b"".join(chunks),
    )


def _relay(result: UpstreamResult, payment_response: str) -> Response:
    response = Response(content=result.body, status_code=result.status_code)
    for name, value in result.headers:
        response.raw_headers.append((name.lower().encode("latin-1"), value.encode("latin-1")))
    response.headers[HEADER_PAYMENT_RESPONSE] = payment_response
    return response


# ---------------------------------------------------------------- lock + persistence helpers


async def _release_payment_locks(redis: Redis, tx_id: str, nonce: str) -> None:
    """Drop the replay lock and the nonce claim after a FAILED verification so the payer can retry."""
    try:
        await redis.delete(lock_key(tx_id), nonce_lock_key(nonce))
    except RedisError:
        logger.error("could not release replay lock for %s; it will expire in %ss", tx_id, REPLAY_LOCK_TTL_SECONDS)


async def _hold_delivery_lock(redis: Redis, tx_id: str) -> None:
    """Mark a delivery as in flight so a redelivery cannot run concurrently with it (best effort)."""
    try:
        await redis.set(delivery_lock_key(tx_id), "1", ex=DELIVERY_LOCK_TTL_SECONDS)
    except RedisError:
        logger.warning("could not mark delivery of %s as in flight", tx_id, exc_info=True)


async def _drop_delivery_lock(redis: Redis, tx_id: str) -> None:
    try:
        await redis.delete(delivery_lock_key(tx_id))
    except RedisError:
        logger.warning("could not clear the in-flight delivery marker for %s", tx_id, exc_info=True)


async def _record_upstream_status(session: AsyncSession, settlement_id: uuid.UUID, status_code: int | None) -> None:
    try:
        await session.execute(
            update(Settlement).where(Settlement.id == settlement_id).values(upstream_status_code=status_code)
        )
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("could not store upstream status for settlement %s", settlement_id)


def _settlement_payload(settlement: Settlement, endpoint_slug: str) -> str:
    """JSON snapshot of a settlement, taken before the insert so it survives a failed transaction."""
    return json.dumps(
        {
            "txId": settlement.tx_id,
            "paymentNonce": settlement.payment_nonce,
            "endpointId": str(settlement.endpoint_id),
            "endpointSlug": endpoint_slug,
            "merchantId": str(settlement.merchant_id),
            "network": settlement.network,
            "payerAddress": settlement.payer_address,
            "merchantAddress": settlement.merchant_address,
            "treasuryAddress": settlement.treasury_address,
            "amountSompi": settlement.amount_sompi,
            "merchantAmountSompi": settlement.merchant_amount_sompi,
            "platformFeeSompi": settlement.fee_amount_sompi,
            "kasUsdRate": None if settlement.kas_usd_rate is None else str(settlement.kas_usd_rate),
            "usdValueAtSettlement": (
                None if settlement.usd_value_at_settlement is None else str(settlement.usd_value_at_settlement)
            ),
            "requestMethod": settlement.request_method,
            "requestPath": settlement.request_path,
            "settledAt": settlement.settled_at.isoformat() if settlement.settled_at is not None else None,
            "recordedAt": datetime.now(UTC).isoformat(),
        },
        sort_keys=True,
    )


async def _dead_letter_settlement(redis: Redis, payload: str) -> None:
    """Persist a paid-but-unrecorded call to Redis so it can be reconciled; log it if Redis fails too."""
    try:
        await cast(Awaitable[int], redis.lpush(DEAD_LETTER_KEY, payload))
    except RedisError:
        logger.error("dead-letter queue unavailable; unrecorded settlement payload: %s", payload, exc_info=True)
        return
    logger.error("unrecorded settlement pushed to %s: %s", DEAD_LETTER_KEY, payload)


async def _lookup_rate(oracle: RateProvider) -> Decimal | None:
    try:
        return await oracle.get_rate()
    except Exception:
        logger.exception("KAS/USD rate unavailable; settlement will be stored without a USD value")
        return None


def _new_settlement(
    endpoint: Endpoint,
    merchant_address: str,
    required: PaymentRequired,
    verified: VerifiedPaymentLike,
    rate: Decimal | None,
    request: Request,
) -> Settlement:
    now = datetime.now(UTC)
    return Settlement(
        id=uuid.uuid4(),
        tx_id=verified.tx_id,
        endpoint_id=endpoint.id,
        merchant_id=endpoint.merchant_id,
        network=required.network,
        payer_address=verified.payer_address,
        merchant_address=merchant_address,
        treasury_address=required.platformFee.feeTo,
        amount_sompi=verified.total_sompi,
        merchant_amount_sompi=verified.merchant_sompi,
        fee_amount_sompi=verified.fee_sompi,
        kas_usd_rate=rate,
        usd_value_at_settlement=usd_value(verified.total_sompi, rate) if rate is not None else None,
        status=SettlementStatus.SETTLED,
        request_method=request.method,
        request_path=request.url.path,
        payment_nonce=required.nonce,
        delivery_attempts=1,
        created_at=now,
        settled_at=now,
    )


# ---------------------------------------------------------------- payment challenges (nonces)


async def issue_payment_challenge(redis: Redis, slug: str, price_sompi: int, pay_to: str) -> PaymentRequired:
    """Mint a nonce-bound `PaymentRequired` and store it as `kasgate:nonce:{nonce}` for `payment_max_age_seconds`.

    Raises ValueError when the price cannot be split into valid outputs and RedisError when the store is down.
    """
    settings = get_settings()
    expires_at = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=settings.payment_max_age_seconds)
    required = PaymentRequired.for_endpoint(price_sompi, pay_to, new_payment_nonce(), expires_at)
    record = json.dumps({"slug": slug, "required": required.model_dump(mode="json")}, sort_keys=True)
    await redis.set(nonce_key(required.nonce), record, ex=settings.payment_max_age_seconds)
    return required


async def _load_challenge(redis: Redis, nonce: str) -> tuple[str, PaymentRequired] | None:
    """The (slug, challenge) stored for `nonce`, or None when it is missing, expired or unreadable."""
    raw = await redis.get(nonce_key(nonce))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        slug = data["slug"]
        required = PaymentRequired.model_validate(data["required"])
    except (ValueError, KeyError, TypeError):
        logger.error("stored challenge for nonce %s is unreadable", nonce, exc_info=True)
        return None
    if not isinstance(slug, str) or required.nonce != nonce or required.expiresAt <= datetime.now(UTC):
        return None
    return slug, required


def _limit_error(exc: HTTPException) -> JSONResponse:
    return _error(
        exc.status_code,
        "rate_limited" if exc.status_code == 429 else "service_unavailable",
        str(exc.detail),
        headers=dict(exc.headers or {}),
    )


async def _challenge_error(
    request: Request,
    redis: Redis,
    endpoint: Endpoint,
    merchant_address: str,
    error: str,
    message: str,
    *,
    limited: bool = True,
) -> JSONResponse:
    """A 402 carrying a FRESH `PAYMENT-REQUIRED` (new nonce). Every new challenge costs one unpaid-rate-limit hit."""
    if limited:
        try:
            await enforce_rate_limit(redis, RATE_LIMIT_UNPAID_PROXY, f"ip:{client_ip(request)}")
        except HTTPException as exc:
            return _limit_error(exc)
    try:
        required = await issue_payment_challenge(redis, endpoint.slug, endpoint.price_sompi, merchant_address)
    except ValueError as exc:
        logger.error("endpoint %s cannot be priced: %s", endpoint.slug, exc)
        return _error(503, "service_unavailable", "endpoint price is not currently payable")
    except RedisError:
        logger.exception("payment challenge store unavailable")
        return _error(503, "service_unavailable", "payment challenges are temporarily unavailable")
    return _error(402, error, message, required=required)


def _verification_failure(exc: PaymentVerificationError, required: PaymentRequired) -> JSONResponse:
    if isinstance(exc, RpcUnavailableError):
        logger.warning("payment not settled, node unavailable: %s", exc.message)
        return _error(503, "rpc_unavailable", exc.message, headers={"Retry-After": "5"})
    if exc.code == "payment_replayed":
        return _error(409, "payment_replayed", exc.message)
    return _error(402, exc.code if exc.code in _VERIFICATION_CODES else "tx_rejected", exc.message, required=required)


# ---------------------------------------------------------------- delivery & redelivery


async def _forward_and_record(
    session: AsyncSession,
    client: httpx.AsyncClient,
    method: str,
    prepared: PreparedUpstream,
    settlement_id: uuid.UUID,
    tx_id: str,
    payment_response: str,
) -> Response:
    try:
        result = await _call_upstream(client, method, prepared)
    except UpstreamFailedError as exc:
        logger.warning("upstream failure for settled tx %s: %s", tx_id, exc)
        await _record_upstream_status(session, settlement_id, None)
        return _error(
            502,
            "upstream_error",
            "the upstream API did not return a response",
            headers={HEADER_PAYMENT_RESPONSE: payment_response},
        )
    await _record_upstream_status(session, settlement_id, result.status_code)
    return _relay(result, payment_response)


def _redelivery_allowed(
    settings: Settings,
    settlement: Settlement,
    signature: PaymentSignature,
    endpoint: Endpoint,
    request: Request,
) -> bool:
    """CONTRACT v2: same txId + nonce + slug, no upstream result or a 5xx, inside the window, below the attempt cap.

    The payer, method and path must also match the original paid call: a txId and its nonce are public on-chain, so
    these checks stop a bystander from replaying someone else's payment against a different resource.
    """
    if settlement.status != SettlementStatus.SETTLED or settlement.settled_at is None:
        return False
    age = datetime.now(UTC) - settlement.settled_at
    return (
        settlement.tx_id == signature.txId
        and settlement.payment_nonce == signature.nonce
        and settlement.endpoint_id == endpoint.id
        and settlement.payer_address == signature.payerAddress
        and settlement.request_method == request.method
        and settlement.request_path == request.url.path
        and (settlement.upstream_status_code is None or settlement.upstream_status_code >= 500)
        and age < timedelta(seconds=settings.redelivery_window_seconds)
        and settlement.delivery_attempts < settings.redelivery_max_attempts
    )


async def _claim_redelivery(session: AsyncSession, settlement_id: uuid.UUID, max_attempts: int) -> bool:
    """Atomically count one more delivery; False when a concurrent request already used the last attempt."""
    result = await session.execute(
        update(Settlement)
        .where(
            Settlement.id == settlement_id,
            Settlement.status == SettlementStatus.SETTLED,
            Settlement.delivery_attempts < max_attempts,
            or_(Settlement.upstream_status_code.is_(None), Settlement.upstream_status_code >= 500),
        )
        .values(delivery_attempts=Settlement.delivery_attempts + 1)
        .returning(Settlement.id)
    )
    claimed = result.scalar_one_or_none() is not None
    await session.commit()
    return claimed


async def _redeliver(
    session: AsyncSession,
    redis: Redis,
    client: httpx.AsyncClient,
    settlement: Settlement,
    method: str,
    prepared: PreparedUpstream,
) -> Response:
    settings = get_settings()
    tx_id = settlement.tx_id
    try:
        idle = await redis.set(delivery_lock_key(tx_id), "1", nx=True, ex=DELIVERY_LOCK_TTL_SECONDS)
    except RedisError:
        logger.exception("delivery marker unavailable")
        return _error(503, "service_unavailable", "payment replay protection is temporarily unavailable")
    if not idle:
        return _error(409, "payment_replayed", "this transaction is already being delivered")
    try:
        try:
            claimed = await _claim_redelivery(session, settlement.id, settings.redelivery_max_attempts)
        except SQLAlchemyError:
            await session.rollback()
            logger.exception("redelivery claim failed")
            return _error(503, "service_unavailable", "settlement store is temporarily unavailable")
        if not claimed:
            return _error(409, "payment_replayed", "this transaction was already used")
        usd = settlement.usd_value_at_settlement if settlement.usd_value_at_settlement is not None else Decimal(0)
        return await _forward_and_record(
            session, client, method, prepared, settlement.id, tx_id, _payment_response_header(tx_id, usd)
        )
    finally:
        await _drop_delivery_lock(redis, tx_id)


async def _find_settlement(session: AsyncSession, tx_id: str, nonce: str) -> Settlement | None:
    """The settlement that already uses this txId or this nonce (the txId match wins when both exist)."""
    rows = (
        (
            await session.execute(
                select(Settlement).where(or_(Settlement.tx_id == tx_id, Settlement.payment_nonce == nonce))
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        if row.tx_id == tx_id:
            return row
    return rows[0] if rows else None


async def _settle_and_deliver(
    request: Request,
    session: AsyncSession,
    redis: Redis,
    client: httpx.AsyncClient,
    oracle: RateProvider,
    endpoint: Endpoint,
    merchant_address: str,
    required: PaymentRequired,
    verified: VerifiedPaymentLike,
    method: str,
    prepared: PreparedUpstream,
) -> Response:
    tx_id = verified.tx_id
    rate = await _lookup_rate(oracle)
    settlement = _new_settlement(endpoint, merchant_address, required, verified, rate, request)
    settlement_id = settlement.id
    unrecorded_payload = _settlement_payload(settlement, endpoint.slug)
    session.add(settlement)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        logger.error("tx %s (nonce %s) conflicts with an existing settlement", tx_id, required.nonce, exc_info=True)
        return _error(409, "payment_replayed", "this transaction or payment nonce was already used")
    except SQLAlchemyError:
        await session.rollback()
        logger.error("tx %s was accepted by the node but its settlement could not be stored", tx_id, exc_info=True)
        await _dead_letter_settlement(redis, unrecorded_payload)
        return _error(503, "service_unavailable", "payment was accepted but could not be recorded; contact support")

    try:
        await redis.getdel(nonce_key(required.nonce))
    except RedisError:
        logger.warning("could not consume nonce %s; the settlement's unique nonce still blocks reuse", required.nonce)

    usd = settlement.usd_value_at_settlement if settlement.usd_value_at_settlement is not None else Decimal(0)
    return await _forward_and_record(
        session, client, method, prepared, settlement_id, tx_id, _payment_response_header(tx_id, usd)
    )


# ---------------------------------------------------------------- the route


@router.api_route(f"{PROXY_PATH_PREFIX}/{{slug}}", methods=PROXIED_METHODS, include_in_schema=False)
@router.api_route(f"{PROXY_PATH_PREFIX}/{{slug}}/{{path:path}}", methods=PROXIED_METHODS, include_in_schema=False)
async def paywalled_proxy(
    request: Request,
    slug: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis: Annotated[Redis, Depends(get_redis)],
    client: Annotated[httpx.AsyncClient, Depends(get_http_client)],
    engine: Annotated[PaymentVerifier, Depends(get_engine)],
    oracle: Annotated[RateProvider, Depends(get_oracle)],
    path: str = "",
) -> Response:
    settings = get_settings()
    signature_header = request.headers.get(HEADER_PAYMENT_SIGNATURE)
    if not signature_header:
        try:
            await enforce_rate_limit(redis, RATE_LIMIT_UNPAID_PROXY, f"ip:{client_ip(request)}")
        except HTTPException as exc:
            return _limit_error(exc)
    row = (
        await session.execute(
            select(Endpoint, Merchant.kaspa_address)
            .join(Merchant, Merchant.id == Endpoint.merchant_id)
            .where(Endpoint.slug == slug, Endpoint.is_active.is_(True), Merchant.is_active.is_(True))
        )
    ).one_or_none()
    if row is None:
        return _error(404, "endpoint_not_found", f"no active endpoint with slug '{slug}'")
    endpoint, merchant_address = row
    method = request.method.upper()
    if method not in endpoint.allowed_methods:
        return _error(
            405,
            "method_not_allowed",
            f"{method} is not allowed on this endpoint",
            headers={"Allow": ", ".join(sorted(endpoint.allowed_methods))},
        )

    if not signature_header:
        return await _challenge_error(
            request,
            redis,
            endpoint,
            merchant_address,
            "payment_required",
            "payment is required to access this resource",
            limited=False,
        )
    try:
        signature = PaymentSignature.from_header(signature_header)
    except ValidationError as exc:
        description = str(exc).lower()
        code = "wrong_network" if "prefix" in description or "network" in description else "invalid_payment_header"
        return await _challenge_error(
            request, redis, endpoint, merchant_address, code, f"{HEADER_PAYMENT_SIGNATURE} header is invalid"
        )
    except ValueError:
        return await _challenge_error(
            request,
            redis,
            endpoint,
            merchant_address,
            "invalid_payment_header",
            f"{HEADER_PAYMENT_SIGNATURE} header is malformed",
        )

    try:
        prepared = await _prepare_upstream(request, endpoint, path)
    except RequestRejectedError as exc:
        return _error(exc.status_code, exc.error, exc.message)

    tx_id = signature.txId
    nonce = signature.nonce
    try:
        challenge = await _load_challenge(redis, nonce)
    except RedisError:
        logger.exception("payment challenge store unavailable")
        return _error(503, "service_unavailable", "payment replay protection is temporarily unavailable")
    try:
        existing = await _find_settlement(session, tx_id, nonce)
    except SQLAlchemyError:
        logger.exception("settlement lookup failed")
        return _error(503, "service_unavailable", "settlement store is temporarily unavailable")
    if existing is not None:
        if _redelivery_allowed(settings, existing, signature, endpoint, request):
            return await _redeliver(session, redis, client, existing, method, prepared)
        return _error(409, "payment_replayed", "this transaction or payment nonce was already used")

    if challenge is None:
        return await _challenge_error(
            request,
            redis,
            endpoint,
            merchant_address,
            "payment_expired",
            "the payment challenge is unknown or has expired; pay the fresh challenge",
        )
    challenge_slug, required = challenge
    if challenge_slug != endpoint.slug:
        return await _challenge_error(
            request,
            redis,
            endpoint,
            merchant_address,
            "invalid_payment_header",
            "the payment nonce was issued for a different endpoint",
        )

    # Pure checks first: a malformed or mispaying transaction never reaches a lock or the node.
    try:
        pre_verified, tx = engine.verify(signature, required)
    except PaymentVerificationError as exc:
        return _verification_failure(exc, required)
    except Exception:
        logger.exception("unexpected failure while checking tx %s", tx_id)
        return _error(503, "service_unavailable", "payment verification is temporarily unavailable")

    # The replay lock is taken BEFORE anything that talks to the node (resolve_inputs, settle) and before anything
    # is forwarded. The nonce claim comes first, so a refused tx lock never releases a lock owned by another request.
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

    # Only a failure in here releases the locks; success keeps them for the rest of their TTL.
    try:
        await engine.resolve_inputs(signature, tx)
        verified = await engine.settle(tx, pre_verified, required)
    except PaymentVerificationError as exc:
        await _release_payment_locks(redis, tx_id, nonce)
        return _verification_failure(exc, required)
    except Exception:
        await _release_payment_locks(redis, tx_id, nonce)
        logger.exception("unexpected failure while verifying tx %s", tx_id)
        return _error(503, "service_unavailable", "payment verification is temporarily unavailable")

    await _hold_delivery_lock(redis, tx_id)
    try:
        return await _settle_and_deliver(
            request, session, redis, client, oracle, endpoint, merchant_address, required, verified, method, prepared
        )
    finally:
        await _drop_delivery_lock(redis, tx_id)
