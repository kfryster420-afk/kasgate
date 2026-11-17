"""Paywalled wildcard reverse proxy: the HTTP 402 handshake for `/p/{slug}[/{path}]`.

Request lifecycle
-----------------
1. Resolve the active endpoint (404) and check the HTTP method (405).
2. Pre-flight everything that can fail without a payment (path, body size, upstream headers, upstream URL).
3. No `PAYMENT-SIGNATURE` -> 402 + `PAYMENT-REQUIRED`.
4. Decode the signature (bad header -> 402 `invalid_payment_header`).
5. Take the Redis replay lock `SET kasgate:tx:{txId} NX EX 600` BEFORE verification or forwarding. A held lock or an
   existing settlement row -> 409 `payment_replayed`.
6. `engine.verify_and_submit`. ONLY a failure here releases the lock; success keeps it.
7. Record the SETTLED settlement (unique `tx_id`; IntegrityError -> 409), then forward upstream.
8. Return the upstream response with `PAYMENT-RESPONSE` (or 502 `upstream_error`, still with `PAYMENT-RESPONSE`).
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Final, cast
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
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
DEAD_LETTER_KEY: Final = "kasgate:deadletter:settlements"
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


# ---------------------------------------------------------------- response helpers


def _error(
    status_code: int,
    error: str,
    message: str,
    *,
    required: PaymentRequired | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """x402 error response. `error` outside the X402ErrorBody vocabulary yields a plain `{error, message}` body."""
    out_headers = dict(headers or {})
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
        raise RequestRejectedError(
            503, "endpoint_misconfigured", "endpoint upstream credentials are unavailable"
        ) from exc
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


async def _release_lock(redis: Redis, tx_id: str) -> None:
    try:
        await redis.delete(lock_key(tx_id))
    except RedisError:
        logger.error("could not release replay lock for %s; it will expire in %ss", tx_id, REPLAY_LOCK_TTL_SECONDS)


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
        created_at=now,
        settled_at=now,
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
    signature_header = request.headers.get(HEADER_PAYMENT_SIGNATURE)
    if not signature_header:
        await enforce_rate_limit(redis, RATE_LIMIT_UNPAID_PROXY, f"ip:{client_ip(request)}")
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

    try:
        required = PaymentRequired.for_endpoint(endpoint.price_sompi, merchant_address)
    except ValueError as exc:
        logger.error("endpoint %s cannot be priced: %s", slug, exc)
        return _error(503, "endpoint_misconfigured", "endpoint price is not currently payable")

    if not signature_header:
        return _error(402, "payment_required", "payment is required to access this resource", required=required)
    try:
        signature = PaymentSignature.from_header(signature_header)
    except ValidationError as exc:
        description = str(exc).lower()
        code = "wrong_network" if "prefix" in description or "network" in description else "invalid_payment_header"
        return _error(402, code, f"{HEADER_PAYMENT_SIGNATURE} header is invalid", required=required)
    except ValueError:
        return _error(
            402, "invalid_payment_header", f"{HEADER_PAYMENT_SIGNATURE} header is malformed", required=required
        )

    try:
        prepared = await _prepare_upstream(request, endpoint, path)
    except RequestRejectedError as exc:
        return _error(exc.status_code, exc.error, exc.message)

    tx_id = signature.txId
    try:
        acquired = await redis.set(lock_key(tx_id), "1", nx=True, ex=REPLAY_LOCK_TTL_SECONDS)
    except RedisError:
        logger.exception("replay lock unavailable")
        return _error(503, "service_unavailable", "payment replay protection is temporarily unavailable")
    if not acquired:
        return _error(409, "payment_replayed", "this transaction is already being processed or was already used")

    try:
        already_settled = await session.scalar(select(Settlement.id).where(Settlement.tx_id == tx_id))
    except SQLAlchemyError:
        logger.exception("settlement lookup failed")
        await _release_lock(redis, tx_id)
        return _error(503, "service_unavailable", "settlement store is temporarily unavailable")
    if already_settled is not None:
        return _error(409, "payment_replayed", "this transaction was already used")

    try:
        verified = await engine.verify_and_submit(signature, required)
    except RpcUnavailableError as exc:
        await _release_lock(redis, tx_id)
        logger.warning("tx %s not settled, node unavailable: %s", tx_id, exc.message)
        return _error(503, "rpc_unavailable", exc.message, headers={"Retry-After": "5"})
    except PaymentVerificationError as exc:
        await _release_lock(redis, tx_id)
        if exc.code == "payment_replayed":
            return _error(409, "payment_replayed", exc.message)
        return _error(
            402, exc.code if exc.code in _VERIFICATION_CODES else "tx_rejected", exc.message, required=required
        )
    except Exception:
        await _release_lock(redis, tx_id)
        logger.exception("unexpected failure while verifying tx %s", tx_id)
        return _error(503, "service_unavailable", "payment verification is temporarily unavailable")

    rate = await _lookup_rate(oracle)
    settlement = _new_settlement(endpoint, merchant_address, required, verified, rate, request)
    settlement_id = settlement.id
    unrecorded_payload = _settlement_payload(settlement, endpoint.slug)
    session.add(settlement)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return _error(409, "payment_replayed", "this transaction was already used")
    except SQLAlchemyError:
        await session.rollback()
        logger.error("tx %s was accepted by the node but its settlement could not be stored", tx_id, exc_info=True)
        await _dead_letter_settlement(redis, unrecorded_payload)
        return _error(503, "service_unavailable", "payment was accepted but could not be recorded; contact support")

    usd = settlement.usd_value_at_settlement if settlement.usd_value_at_settlement is not None else Decimal(0)
    payment_response = _payment_response_header(tx_id, usd)

    try:
        result = await _call_upstream(client, method, prepared)
    except UpstreamFailedError as exc:
        logger.warning("upstream failure for settled tx %s on %s: %s", tx_id, slug, exc)
        await _record_upstream_status(session, settlement_id, None)
        return _error(
            502,
            "upstream_error",
            "the upstream API did not return a response",
            headers={HEADER_PAYMENT_RESPONSE: payment_response},
        )
    await _record_upstream_status(session, settlement_id, result.status_code)
    return _relay(result, payment_response)
