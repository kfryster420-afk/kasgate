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

k402 ``kaspa-utxo`` (CONTRACT v2.9)
---------------------------------
Every 402 is also a k402 offer body: ``{"k402": "0.2", "accepts": [<kaspa-exact-v1 terms>, <kaspa-utxo offer>]}``. The
offer is stored as `kasgate:k402:{payment_id}` (slug, the offer, and an internal 32-hex settlement nonce). An unmodified
k402 client pays it itself and retries with `X-K402-Payment: kaspa-utxo <txid> <payment_id>`; the gateway then
1. loads the offer (unknown/expired -> 402 `payment_expired`; another slug -> 402 `invalid_payment_header`),
2. takes `SET kasgate:k402lock:{payment_id} NX` and the SHARED `SET kasgate:tx:{txId} NX`, so one txId can never be
   claimed through both bindings,
3. runs `engine.verify_k402` (node lookup; output 0 = merchant exact, output 1 = treasury exact, by index and script;
   no payload or payerProof). `payment_not_found` is a 402 so k402 clients retry the same header,
4. stores the settlement (unique `tx_id`, `payment_nonce` and `k402_payment_id`) and consumes the offer.

kaspa-x402 ``exact`` / ``kaspa-exact-v2`` standard-native (CONTRACT v2.10)
-----------------------------------------------------------------------
Every fresh-challenge 402 also offers the community kaspa-x402 binding. The `PAYMENT-REQUIRED` header becomes ONE
x402 v2 envelope (`PaymentRequiredEnvelope`): `accepts = [<exact kaspa-exact-v2 entry>, <kaspa-exact-v1 terms>]`, the
payment-identifier extension, `resource.url` = the public request URL, and the kaspa-exact-v1 terms mirrored at the
top level for pre-v2.10 readers. The body keeps the k402 shape and appends the exact entry to its `accepts` list. The
exact entry pays the merchant the SAME locked sompi amount as the v1 challenge (USD endpoints: the same single oracle
quote), with NO platform fee; it is stored as `kasgate:exact:{slug}:{paymentRequirementsHash}` with the locked rate.
A PAYMENT-SIGNATURE whose JSON is an x402 v2 PaymentPayload (`accepted` / `payload`) takes `_pay_with_exact_v2`:
`accepted` must be a stored offer for this slug, `requestHash` must equal the gateway's own fingerprint of method +
public URL + body + requirements, then the pure verifier, `SET kasgate:pid:{payment identifier} NX` and the SHARED
`SET kasgate:tx:{txId} NX`, node lookup, submit with `accepted` finality, a settlement row with payment_scheme
`kaspa-exact-v2` and fee 0, and a `PAYMENT-RESPONSE` SettlementResponse. Retrying the same payment identifier with
the same request and transaction is idempotent (redelivery rules below); the same identifier with another request is
a 409 conflict.

Redelivery: the same txId + nonce + slug (and the same payer, method and path) whose settlement has no upstream
status or a 5xx one, inside `redelivery_window_seconds` and below `redelivery_max_attempts` deliveries, is
forwarded again and counted in `Settlement.delivery_attempts`. Anything else for a settled txId is 409.
"""

import asyncio
import json
import logging
import math
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Final, cast
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from kaspa import PublicKey, verify_message
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
from app.exact_v2 import ExactV2Verification, check_authorization_expiry
from app.failed_delivery import credit_if_exhausted, credit_policy_for
from app.kaspa_types import payer_public_key as payer_public_key
from app.models import (
    DELIVERY_CREDITED,
    DELIVERY_DELIVERED,
    DELIVERY_FAILED,
    DELIVERY_PENDING,
    FAILURE_CONNECT_ERROR,
    FAILURE_OVERSIZED,
    FAILURE_TIMEOUT,
    FAILURE_UPSTREAM_5XX,
    PREPAID_DEBIT_DEBITED,
    PREPAID_DEBIT_REFUNDED,
    TRIAL_CALL_REFUNDED,
    TRIAL_CALL_USED,
    Endpoint,
    Merchant,
    PrepaidBalance,
    PrepaidDebit,
    PrepaidLedgerEntry,
    PriceCurrency,
    Settlement,
    SettlementStatus,
    TrialCall,
    TrialDailyCount,
    TrialUsage,
)
from app.oracle import OracleUnavailableError, usd_value
from app.payments import PaymentVerificationError, RpcUnavailableError
from app.schemas import (
    HEADER_FAILED_DELIVERY_CREDIT,
    HEADER_K402_PAYMENT,
    HEADER_PAYMENT_REQUIRED,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
    HEADER_PREPAID_DEBIT,
    HEADER_PREPAID_RECEIPT,
    HEADER_TRIAL_CALL,
    HEADER_TRIAL_LIMIT,
    HEADER_TRIAL_OFFER,
    HEADER_TRIAL_RECEIPT,
    HEADER_TRIAL_REMAINING,
    K402_VERSION,
    NONCE_RE,
    PREPAID_EMPTY_BODY_HASH,
    PROXY_PATH_PREFIX,
    K402PaymentHeader,
    K402UtxoOffer,
    PaymentRequired,
    PaymentRequiredEnvelope,
    PaymentRequiredExtra,
    PaymentResponse,
    PaymentScheme,
    PaymentSignature,
    PrepaidDebitHeader,
    PrepaidOffer,
    PrepaidReceipt,
    TrialCallHeader,
    TrialOffer,
    TrialReceipt,
    X402ErrorBody,
    build_prepaid_debit_message,
    build_trial_call_message,
    min_price_sompi,
    new_k402_payment_id,
    new_k402_tag,
    new_payment_nonce,
    prepaid_body_hash,
    usd_micros_to_prepaid_sompi,
    usd_micros_to_sompi,
)
from app.security import (
    HOP_BY_HOP_HEADERS,
    PAYMENT_HEADERS,
    RATE_LIMIT_TRIAL_CALLS,
    RATE_LIMIT_UNPAID_PROXY,
    auth_domain,
    client_ip,
    decrypt_headers,
    enforce_rate_limit,
)
from app.webhooks import enqueue_event, settlement_event_data
from app.x402_v2 import (
    EXACT_V2_FINALITY,
    EXACT_V2_PROFILE,
    EXACT_V2_TRANSACTION_ENCODING,
    ExactV2PaymentPayload,
    ExactV2Requirements,
    KasGateSettlementExtension,
    KaspaSettlementExtension,
    SettlementExtensions,
    SettlementResponse,
    canonical_json,
    decode_header_json,
    http_request_hash,
    payment_requirements_hash,
)

logger = logging.getLogger("kasgate.proxy")

REPLAY_LOCK_PREFIX: Final = "kasgate:tx:"
REPLAY_LOCK_TTL_SECONDS: Final = 600
NONCE_KEY_PREFIX: Final = "kasgate:nonce:"
NONCE_LOCK_PREFIX: Final = "kasgate:noncelock:"
K402_OFFER_PREFIX: Final = "kasgate:k402:"
K402_LOCK_PREFIX: Final = "kasgate:k402lock:"
SCHEME_V1: Final[PaymentScheme] = "kaspa-exact-v1"
SCHEME_K402: Final[PaymentScheme] = "k402-kaspa-utxo"
SCHEME_EXACT_V2: Final[PaymentScheme] = "kaspa-exact-v2"
EXACT_OFFER_PREFIX: Final = "kasgate:exact:"
PAYMENT_IDENTIFIER_LOCK_PREFIX: Final = "kasgate:pid:"
# kaspa-exact-v2 PaymentPayloads carry a whole transaction, so they get a larger header budget than v1 headers.
MAX_EXACT_V2_HEADER_BYTES: Final = 65_536
DELIVERY_LOCK_PREFIX: Final = "kasgate:delivery:"
DELIVERY_LOCK_TTL_SECONDS: Final = 60


def _delivery_lock_ttl() -> int:
    """The in-flight marker outlives one full upstream attempt (UPSTREAM_TIMEOUT_SECONDS) with a margin."""
    return max(DELIVERY_LOCK_TTL_SECONDS, math.ceil(get_settings().upstream_timeout_seconds) + 30)


DEAD_LETTER_KEY: Final = "kasgate:deadletter:settlements"
SERVICE_UNAVAILABLE_RETRY_SECONDS: Final = "5"
# CONTRACT v2.14: delivery outcomes that could not be stored (the settlement stays `pending`, never credited).
DELIVERY_OUTCOME_DEAD_LETTER_KEY: Final = "kasgate:deadletter:delivery-outcomes"
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
_EXACT_V2_VERIFICATION_CODES: Final[frozenset[str]] = _VERIFICATION_CODES | {"invalid_exact_payment"}
_K402_VERIFICATION_CODES: Final[frozenset[str]] = _VERIFICATION_CODES | {"payment_not_found"}

router = APIRouter(tags=["proxy"])


def lock_key(tx_id: str) -> str:
    return f"{REPLAY_LOCK_PREFIX}{tx_id}"


def nonce_key(nonce: str) -> str:
    return f"{NONCE_KEY_PREFIX}{nonce}"


def nonce_lock_key(nonce: str) -> str:
    return f"{NONCE_LOCK_PREFIX}{nonce}"


def k402_offer_key(payment_id: str) -> str:
    return f"{K402_OFFER_PREFIX}{payment_id}"


def k402_lock_key(payment_id: str) -> str:
    return f"{K402_LOCK_PREFIX}{payment_id}"


def exact_offer_key(slug: str, requirements_hash: str) -> str:
    return f"{EXACT_OFFER_PREFIX}{slug}:{requirements_hash}"


def payment_identifier_lock_key(payment_identifier: str) -> str:
    return f"{PAYMENT_IDENTIFIER_LOCK_PREFIX}{payment_identifier}"


def delivery_lock_key(tx_id: str) -> str:
    return f"{DELIVERY_LOCK_PREFIX}{tx_id}"


# ---------------------------------------------------------------- response helpers


def _error(
    status_code: int,
    error: str,
    message: str,
    *,
    required: PaymentRequired | None = None,
    offer: K402UtxoOffer | None = None,
    exact: ExactV2Requirements | None = None,
    envelope: PaymentRequiredEnvelope | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """x402 error response. `error` outside the X402ErrorBody vocabulary yields a plain `{error, message}` body.

    With `required`, `accepts` lists the v1 terms (also sent as `PAYMENT-REQUIRED`); with `offer` too, the body is a
    k402 offer body (`k402` set, the `kaspa-utxo` offer appended). With `exact` the kaspa-exact-v2 entry is appended
    too, and `envelope` (CONTRACT v2.10) replaces the flat v1 `PAYMENT-REQUIRED` header. Every 503 carries
    `Retry-After`; 429 callers pass the limiter's own `Retry-After`.
    """
    out_headers = dict(headers or {})
    if status_code == 503:
        out_headers.setdefault("Retry-After", SERVICE_UNAVAILABLE_RETRY_SECONDS)
    accepts: list[dict[str, object]] | None = None
    if required is not None:
        out_headers[HEADER_PAYMENT_REQUIRED] = envelope.to_header() if envelope is not None else required.to_header()
        accepts = [required.model_dump(mode="json", exclude_none=True)]
        if offer is not None:
            accepts.append(offer.model_dump(mode="json"))
        if exact is not None:
            accepts.append(exact.wire())
    try:
        content = X402ErrorBody.model_validate(
            {
                "error": error,
                "message": message,
                "k402": K402_VERSION if accepts is not None and offer is not None else None,
                "accepts": accepts,
            }
        ).model_dump(mode="json")
    except ValueError:
        content = {"error": error, "message": message}
    return JSONResponse(status_code=status_code, content=content, headers=out_headers)


def resource_url(request: Request) -> str:
    """The public URL of this request: the x402 `resource.url` and the URL of the request fingerprint.

    `PUBLIC_BASE_URL` + the raw (still percent-encoded) path + the raw query, which is what a WHATWG client calling
    the gateway at its public URL has as `new URL(...).href` without the fragment.
    """
    raw_path = request.scope.get("raw_path")
    path = raw_path.decode("latin-1").split("?", 1)[0] if isinstance(raw_path, bytes) else request.url.path
    query = request.scope.get("query_string", b"")
    query_text = query.decode("latin-1") if isinstance(query, bytes) else ""
    return f"{get_settings().public_base_url}{path}" + (f"?{query_text}" if query_text else "")


def _exact_settlement_header(settlement: Settlement, payment_output_index: int) -> str:
    usd = settlement.usd_value_at_settlement if settlement.usd_value_at_settlement is not None else Decimal(0)
    return SettlementResponse(
        success=True,
        transaction=settlement.tx_id,
        network=get_settings().kaspa_network,
        payer=settlement.payer_address or "",
        amount=str(settlement.amount_sompi),
        extensions=SettlementExtensions(
            kaspa=KaspaSettlementExtension(
                exactProfile=EXACT_V2_PROFILE,
                paymentOutputIndex=payment_output_index,
                finality=EXACT_V2_FINALITY,
                requestHash=settlement.request_hash or "",
                transactionEncoding=EXACT_V2_TRANSACTION_ENCODING,
            ),
            kasgate=KasGateSettlementExtension(platformFeeSompi="0", usdValueAtSettlement=usd.quantize(USD_QUANTUM)),
        ),
    ).to_header()


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
    """No relayable upstream answer. `failure` says why (models.DELIVERY_FAILURES); `status_code` is the real status
    when the upstream did answer but the gateway refused to relay it (oversized body)."""

    def __init__(self, message: str, *, failure: str = FAILURE_CONNECT_ERROR, status_code: int | None = None) -> None:
        super().__init__(message)
        self.failure = failure
        self.status_code = status_code


@dataclass(frozen=True)
class UpstreamResult:
    status_code: int
    headers: list[tuple[str, str]]
    body: bytes


async def _call_upstream(client: httpx.AsyncClient, method: str, prepared: PreparedUpstream) -> UpstreamResult:
    try:
        async with asyncio.timeout(get_settings().upstream_timeout_seconds):
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
                        raise UpstreamFailedError(
                            "upstream response exceeds the gateway limit",
                            failure=FAILURE_OVERSIZED,
                            status_code=response.status_code,
                        )
                    chunks.append(chunk)
            finally:
                await response.aclose()
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise UpstreamFailedError("upstream request timed out", failure=FAILURE_TIMEOUT) from exc
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
        await redis.set(delivery_lock_key(tx_id), "1", ex=_delivery_lock_ttl())
    except RedisError:
        logger.warning("could not mark delivery of %s as in flight", tx_id, exc_info=True)


async def _drop_delivery_lock(redis: Redis, tx_id: str) -> None:
    try:
        await redis.delete(delivery_lock_key(tx_id))
    except RedisError:
        logger.warning("could not clear the in-flight delivery marker for %s", tx_id, exc_info=True)


class DeliveryOutcomeError(Exception):
    """The delivery outcome could not be stored. `payload` is the JSON to reconcile from the dead-letter list."""

    def __init__(self, payload: str) -> None:
        super().__init__(payload)
        self.payload = payload


async def _write_delivery_outcome(
    session: AsyncSession, settlement_id: uuid.UUID, outcome: str, failure: str | None, status_code: int | None
) -> None:
    """CONTRACT v2.14: replace `pending` with how this delivery ended. Logs at ERROR and raises DeliveryOutcomeError
    when the store fails (the settlement then stays `pending`, which is never credited)."""
    try:
        await session.execute(
            update(Settlement)
            .where(Settlement.id == settlement_id, Settlement.delivery_outcome.is_distinct_from(DELIVERY_CREDITED))
            .values(upstream_status_code=status_code, delivery_outcome=outcome, delivery_failure=failure)
        )
        await session.commit()
    except SQLAlchemyError as exc:
        await session.rollback()
        payload = json.dumps(
            {
                "settlementId": str(settlement_id),
                "deliveryOutcome": outcome,
                "deliveryFailure": failure,
                "upstreamStatusCode": status_code,
                "at": datetime.now(UTC).isoformat(),
            },
            sort_keys=True,
        )
        logger.error("could not store the delivery outcome of settlement %s: %s", settlement_id, payload, exc_info=True)
        raise DeliveryOutcomeError(payload) from exc


async def _record_upstream_status(session: AsyncSession, settlement_id: uuid.UUID, status_code: int | None) -> None:
    """A relayed upstream answer: `delivered` below 500, `failed` (upstream_5xx) from 500.

    Raises DeliveryOutcomeError when the outcome cannot be stored."""
    if status_code is not None and status_code < 500:
        await _write_delivery_outcome(session, settlement_id, DELIVERY_DELIVERED, None, status_code)
    else:
        failure = FAILURE_CONNECT_ERROR if status_code is None else FAILURE_UPSTREAM_5XX
        await _write_delivery_outcome(session, settlement_id, DELIVERY_FAILED, failure, status_code)


async def _record_upstream_failure(
    session: AsyncSession, settlement_id: uuid.UUID, failure: str, status_code: int | None
) -> None:
    """No relayable answer (connection error, timeout, oversized body). Raises DeliveryOutcomeError."""
    await _write_delivery_outcome(session, settlement_id, DELIVERY_FAILED, failure, status_code)


async def _alert_unrecorded_outcome(redis: Redis, payload: str) -> None:
    """Alert path for a delivery outcome the database refused: the dead-letter list (watched like the settlement
    dead-letter list) plus an ERROR log line. Never raises, so the buyer still gets the upstream's answer."""
    try:
        await cast(Awaitable[int], redis.lpush(DELIVERY_OUTCOME_DEAD_LETTER_KEY, payload))
    except RedisError:
        logger.error("dead-letter queue unavailable; unrecorded delivery outcome: %s", payload, exc_info=True)
        return
    logger.error("unrecorded delivery outcome pushed to %s: %s", DELIVERY_OUTCOME_DEAD_LETTER_KEY, payload)


def _settlement_payload(settlement: Settlement, endpoint_slug: str) -> str:
    """JSON snapshot of a settlement, taken before the insert so it survives a failed transaction."""
    return json.dumps(
        {
            "txId": settlement.tx_id,
            "paymentIdentifier": settlement.payment_identifier,
            "requestHash": settlement.request_hash,
            "paymentNonce": settlement.payment_nonce,
            "paymentScheme": settlement.payment_scheme,
            "k402PaymentId": settlement.k402_payment_id,
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


def _locked_quote_rate(required: PaymentRequired) -> Decimal | None:
    """The KAS/USD rate locked into a USD-priced challenge, if any."""
    if required.extra is None:
        return None
    try:
        rate = Decimal(required.extra.kasUsdRate)
    except Exception:
        return None
    return rate if rate.is_finite() and rate > 0 else None


def _parse_locked_rate(value: object) -> Decimal | None:
    """A stored KAS/USD rate string as a positive finite Decimal, else None."""
    if not isinstance(value, str):
        return None
    try:
        rate = Decimal(value)
    except Exception:
        return None
    return rate if rate.is_finite() and rate > 0 else None


async def _lookup_rate(oracle: RateProvider, terms: "SettlementTerms") -> Decimal | None:
    """The rate locked into the paid quote (USD-priced endpoints), else the live oracle rate, else None."""
    if terms.locked_rate is not None:
        return terms.locked_rate
    try:
        return await oracle.get_rate()
    except Exception:
        logger.exception("KAS/USD rate unavailable; settlement will be stored without a USD value")
        return None


@dataclass(frozen=True)
class SettlementTerms:
    """What a settlement row needs from the paid offer, whichever binding (v1 or k402) it came through."""

    scheme: PaymentScheme
    network: str
    treasury_address: str | None  # None for kaspa-exact-v2: no treasury output
    nonce: str | None  # None for kaspa-exact-v2: bound by payment identifier and request hash instead
    k402_payment_id: str | None
    consume_key: str | None  # Redis key of the offer, consumed after the settlement is stored
    locked_rate: Decimal | None  # KAS/USD rate locked into a USD-priced quote, used for the settlement's USD value
    payment_identifier: str | None = None
    request_hash: str | None = None

    @classmethod
    def for_v1(cls, required: PaymentRequired) -> "SettlementTerms":
        return cls(
            scheme=SCHEME_V1,
            network=required.network,
            treasury_address=required.platformFee.feeTo,
            nonce=required.nonce,
            k402_payment_id=None,
            consume_key=nonce_key(required.nonce),
            locked_rate=_locked_quote_rate(required),
        )

    @classmethod
    def for_exact_v2(cls, checked: ExactV2Verification, locked_rate: Decimal | None) -> "SettlementTerms":
        # The exact offer is NOT consumed: a KAS-priced offer is shared by every 402 at that price, and the unique
        # payment identifier, txId and request hash already make each settlement single-use.
        return cls(
            scheme=SCHEME_EXACT_V2,
            network=get_settings().kaspa_network,
            treasury_address=None,
            nonce=None,
            k402_payment_id=None,
            consume_key=None,
            locked_rate=locked_rate,
            payment_identifier=checked.payment_identifier,
            request_hash=checked.request_hash,
        )

    @classmethod
    def for_k402(cls, stored: "StoredK402Offer") -> "SettlementTerms":
        return cls(
            scheme=SCHEME_K402,
            network=get_settings().kaspa_network,
            treasury_address=stored.offer.facilitator_fee.to,
            nonce=stored.nonce,
            k402_payment_id=stored.offer.payment_id,
            consume_key=k402_offer_key(stored.offer.payment_id),
            locked_rate=stored.locked_rate,
        )


def _new_settlement(
    endpoint: Endpoint,
    merchant_address: str,
    terms: SettlementTerms,
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
        network=terms.network,
        payment_scheme=terms.scheme,
        payer_address=verified.payer_address,
        merchant_address=merchant_address,
        treasury_address=terms.treasury_address,
        amount_sompi=verified.total_sompi,
        merchant_amount_sompi=verified.merchant_sompi,
        fee_amount_sompi=verified.fee_sompi,
        kas_usd_rate=rate,
        usd_value_at_settlement=usd_value(verified.total_sompi, rate) if rate is not None else None,
        status=SettlementStatus.SETTLED,
        request_method=request.method,
        request_path=request.url.path,
        payment_nonce=terms.nonce,
        k402_payment_id=terms.k402_payment_id,
        payment_identifier=terms.payment_identifier,
        request_hash=terms.request_hash,
        delivery_attempts=1,
        created_at=now,
        settled_at=now,
    )


# ---------------------------------------------------------------- payment challenges (nonces)


async def quote_endpoint_price(endpoint: Endpoint, oracle: RateProvider) -> tuple[int, PaymentRequiredExtra | None]:
    """The sompi amount to lock into a challenge, plus USD quote metadata when the endpoint is priced in USD.

    Raises OracleUnavailableError when a USD price cannot be converted because the oracle is down or stale.
    Raises ValueError when the converted (or fixed) amount cannot be split into valid outputs.
    """
    if endpoint.price_currency is PriceCurrency.KAS:
        if endpoint.price_sompi is None:
            raise ValueError("KAS-priced endpoint is missing price_sompi")
        return endpoint.price_sompi, None
    if endpoint.price_usd_micros is None:
        raise ValueError("USD-priced endpoint is missing price_usd_micros")
    try:
        rate = await oracle.get_rate()
    except OracleUnavailableError:
        raise
    except Exception as exc:
        raise OracleUnavailableError("KAS/USD rate is unavailable") from exc
    sompi = usd_micros_to_sompi(endpoint.price_usd_micros, rate)
    extra = PaymentRequiredExtra(
        kasUsdRate=format(rate, "f"),
        quotedAt=datetime.now(UTC).replace(microsecond=0),
    )
    return sompi, extra


async def issue_payment_challenge(
    redis: Redis, endpoint: Endpoint, pay_to: str, oracle: RateProvider
) -> PaymentRequired:
    """Mint a nonce-bound `PaymentRequired` and store it as `kasgate:nonce:{nonce}` for `payment_max_age_seconds`.

    The quoted sompi amount (and, for USD, the rate and quotedAt) is locked into the stored challenge and is never
    re-quoted at payment time. Raises ValueError when the price cannot be split, OracleUnavailableError when a USD
    price cannot be converted, and RedisError when the store is down.
    """
    price_sompi, extra = await quote_endpoint_price(endpoint, oracle)
    settings = get_settings()
    expires_at = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=settings.payment_max_age_seconds)
    required = PaymentRequired.for_endpoint(price_sompi, pay_to, new_payment_nonce(), expires_at, extra=extra)
    record = json.dumps(
        {"slug": endpoint.slug, "required": required.model_dump(mode="json", exclude_none=True)},
        sort_keys=True,
    )
    await redis.set(nonce_key(required.nonce), record, ex=settings.payment_max_age_seconds)
    return required


@dataclass(frozen=True)
class StoredK402Offer:
    slug: str
    nonce: str  # internal settlement nonce, never sent to the client
    offer: K402UtxoOffer
    locked_rate: Decimal | None


async def issue_payment_offers(
    redis: Redis, endpoint: Endpoint, pay_to: str, oracle: RateProvider
) -> tuple[PaymentRequired, K402UtxoOffer]:
    """The v1 challenge plus a k402 `kaspa-utxo` offer for the same endpoint, both valid for `payment_max_age_seconds`.

    The offer is built from the v1 challenge's LOCKED total (`required.amount`: for a USD-priced endpoint the same
    single oracle quote, ceil conversion and 0.6 KAS floor) plus the k402 amount tag, and stores the same locked rate.
    It is stored as `kasgate:k402:{payment_id}` with its own internal settlement nonce, so paying the v1 challenge
    and the k402 offer are independent payments. Raises like `issue_payment_challenge` (OracleUnavailableError before
    anything is stored, so a failed USD quote yields no offer at all).
    """
    settings = get_settings()
    required = await issue_payment_challenge(redis, endpoint, pay_to, oracle)
    offer = K402UtxoOffer.for_endpoint(
        int(required.amount),
        pay_to,
        new_k402_payment_id(),
        required.expiresAt,
        tag_sompi=new_k402_tag(),
        description=endpoint.name,
    )
    record = json.dumps(
        {
            "slug": endpoint.slug,
            "nonce": new_payment_nonce(),
            "offer": offer.model_dump(mode="json"),
            "kasUsdRate": required.extra.kasUsdRate if required.extra is not None else None,
        },
        sort_keys=True,
    )
    await redis.set(k402_offer_key(offer.payment_id), record, ex=settings.payment_max_age_seconds)
    return required, offer


@dataclass(frozen=True)
class StoredExactOffer:
    slug: str
    requirement: ExactV2Requirements
    locked_rate: Decimal | None


async def issue_exact_offer(redis: Redis, endpoint: Endpoint, required: PaymentRequired) -> ExactV2Requirements | None:
    """The kaspa-exact-v2 entry for a freshly minted v1 challenge, stored for `payment_max_age_seconds`.

    `amount` is the v1 challenge's LOCKED total (`required.amount`; USD endpoints: the same oracle quote), paid in full
    to the merchant: no platform fee on this binding. Stored as `kasgate:exact:{slug}:{paymentRequirementsHash}` with
    the locked rate, because a client returns `accepted` verbatim and a USD amount cannot be re-quoted at payment
    time. Returns None when the amount is below the v2 merchant-output minimum. Raises RedisError.
    """
    settings = get_settings()
    try:
        requirement = ExactV2Requirements.for_merchant(
            network=settings.kaspa_network,
            amount_sompi=int(required.amount),
            pay_to=required.payTo,
            max_timeout_seconds=settings.payment_max_age_seconds,
        )
    except ValueError:
        return None
    record = canonical_json(
        {
            "slug": endpoint.slug,
            "requirement": requirement.wire(),
            "kasUsdRate": required.extra.kasUsdRate if required.extra is not None else None,
        }
    )
    key = exact_offer_key(endpoint.slug, payment_requirements_hash(requirement))
    await redis.set(key, record, ex=settings.payment_max_age_seconds)
    return requirement


async def _load_exact_offer(redis: Redis, slug: str, accepted: ExactV2Requirements) -> StoredExactOffer | None:
    """The stored offer matching the client's `accepted` for this slug, or None when missing, expired or unreadable."""
    raw = await redis.get(exact_offer_key(slug, payment_requirements_hash(accepted)))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        requirement = ExactV2Requirements.model_validate(data["requirement"])
        stored_slug, raw_rate = data["slug"], data.get("kasUsdRate")
    except (ValueError, KeyError, TypeError, AttributeError):
        logger.error("stored kaspa-exact-v2 offer for %s is unreadable", slug, exc_info=True)
        return None
    locked_rate = _parse_locked_rate(raw_rate)
    if stored_slug != slug or requirement != accepted or (raw_rate is not None and locked_rate is None):
        return None
    return StoredExactOffer(slug=slug, requirement=requirement, locked_rate=locked_rate)


async def _load_k402_offer(redis: Redis, payment_id: str) -> StoredK402Offer | None:
    """The offer stored for `payment_id`, or None when it is missing, expired or unreadable."""
    raw = await redis.get(k402_offer_key(payment_id))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        slug, nonce = data["slug"], data["nonce"]
        offer = K402UtxoOffer.model_validate(data["offer"])
        raw_rate = data.get("kasUsdRate")
    except (ValueError, KeyError, TypeError, AttributeError):
        logger.error("stored k402 offer %s is unreadable", payment_id, exc_info=True)
        return None
    if (
        not isinstance(slug, str)
        or not isinstance(nonce, str)
        or re.fullmatch(NONCE_RE, nonce) is None
        or offer.payment_id != payment_id
        or offer.expires <= int(datetime.now(UTC).timestamp())
    ):
        return None
    locked_rate = _parse_locked_rate(raw_rate)
    if raw_rate is not None and locked_rate is None:
        logger.error("stored k402 offer %s has an unreadable locked rate", payment_id)
        return None
    return StoredK402Offer(slug=slug, nonce=nonce, offer=offer, locked_rate=locked_rate)


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
    oracle: RateProvider,
    error: str,
    message: str,
    *,
    limited: bool = True,
) -> JSONResponse:
    """A 402 carrying a FRESH `PAYMENT-REQUIRED` (new nonce) and a fresh k402 offer in the body.

    Every new challenge costs one unpaid-rate-limit hit.
    """
    if limited:
        try:
            await enforce_rate_limit(redis, RATE_LIMIT_UNPAID_PROXY, f"ip:{client_ip(request)}")
        except HTTPException as exc:
            return _limit_error(exc)
    try:
        required, offer = await issue_payment_offers(redis, endpoint, merchant_address, oracle)
        exact = await issue_exact_offer(redis, endpoint, required)
    except OracleUnavailableError as exc:
        logger.warning("USD price unavailable for %s: %s", endpoint.slug, exc)
        return _error(503, "price_unavailable", "KAS/USD price is temporarily unavailable")
    except ValueError as exc:
        logger.error("endpoint %s cannot be priced: %s", endpoint.slug, exc)
        return _error(503, "service_unavailable", "endpoint price is not currently payable")
    except RedisError:
        logger.exception("payment challenge store unavailable")
        return _error(503, "service_unavailable", "payment challenges are temporarily unavailable")
    envelope = PaymentRequiredEnvelope.build(
        required,
        exact,
        resource_url=resource_url(request),
        description=endpoint.name[:500] if endpoint.name else None,
        error=error,
    )
    return _error(402, error, message, required=required, offer=offer, exact=exact, envelope=envelope)


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
    redis: Redis,
    client: httpx.AsyncClient,
    method: str,
    prepared: PreparedUpstream,
    settlement_id: uuid.UUID,
    tx_id: str,
    payment_response: str,
) -> Response:
    """Call the upstream for a settled payment, record the delivery outcome and credit a final failure (v2.14).

    The settlement is already `pending` (written with it, or by the redelivery claim). A credit is only attempted
    after the `failed` outcome was stored; if the store fails the outcome is dead-lettered and nothing is credited.
    """
    try:
        result = await _call_upstream(client, method, prepared)
    except UpstreamFailedError as exc:
        logger.warning("upstream failure for settled tx %s: %s", tx_id, exc)
        headers = {HEADER_PAYMENT_RESPONSE: payment_response}
        try:
            await _record_upstream_failure(session, settlement_id, exc.failure, exc.status_code)
        except DeliveryOutcomeError as err:
            await _alert_unrecorded_outcome(redis, err.payload)
        else:
            credit = await credit_if_exhausted(session, settlement_id)
            receipt = None if credit is None else credit.receipt()
            if receipt is not None:
                headers[HEADER_FAILED_DELIVERY_CREDIT] = receipt.to_header()
        if exc.failure == FAILURE_OVERSIZED:
            return _error(
                502,
                "upstream_response_too_large",
                "the upstream answered with a body larger than the gateway relays",
                headers=headers,
            )
        return _error(502, "upstream_error", "the upstream API did not return a response", headers=headers)
    recorded = True
    try:
        await _record_upstream_status(session, settlement_id, result.status_code)
    except DeliveryOutcomeError as err:
        recorded = False
        await _alert_unrecorded_outcome(redis, err.payload)
    response = _relay(result, payment_response)
    if recorded and result.status_code >= 500:
        credit = await credit_if_exhausted(session, settlement_id)
        receipt = None if credit is None else credit.receipt()
        if receipt is not None:
            response.headers[HEADER_FAILED_DELIVERY_CREDIT] = receipt.to_header()
    return response


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
    return (
        settlement.payment_scheme == SCHEME_V1
        and settlement.tx_id == signature.txId
        and settlement.payment_nonce == signature.nonce
        and settlement.payer_address == signature.payerAddress
        and _redelivery_window_open(settings, settlement, endpoint, request)
    )


def _k402_redelivery_allowed(
    settings: Settings, settlement: Settlement, payment: K402PaymentHeader, endpoint: Endpoint, request: Request
) -> bool:
    """k402: same txId + payment_id + slug + method + path, under the same window and attempt cap as v1.

    k402 proves no payer, so the `payment_id` (sent only to the client that asked for the offer) stands in for it.
    """
    return (
        settlement.payment_scheme == SCHEME_K402
        and settlement.tx_id == payment.tx_id
        and settlement.k402_payment_id == payment.payment_id
        and _redelivery_window_open(settings, settlement, endpoint, request)
    )


def _redelivery_window_open(settings: Settings, settlement: Settlement, endpoint: Endpoint, request: Request) -> bool:
    if settlement.status != SettlementStatus.SETTLED or settlement.settled_at is None:
        return False
    age = datetime.now(UTC) - settlement.settled_at
    return (
        settlement.endpoint_id == endpoint.id
        and settlement.request_method == request.method
        and settlement.request_path == request.url.path
        and (settlement.upstream_status_code is None or settlement.upstream_status_code >= 500)
        and age < timedelta(seconds=settings.redelivery_window_seconds)
        and settlement.delivery_attempts < settings.redelivery_max_attempts
    )


async def _claim_redelivery(
    session: AsyncSession, settlement_id: uuid.UUID, max_attempts: int, window_seconds: int
) -> bool:
    """Atomically count one more delivery and mark it `pending`; False when the attempt cap, the window (checked on
    the database clock, like the credit sweep) or a credit already closed redelivery."""
    result = await session.execute(
        update(Settlement)
        .where(
            Settlement.id == settlement_id,
            Settlement.status == SettlementStatus.SETTLED,
            Settlement.delivery_attempts < max_attempts,
            or_(Settlement.upstream_status_code.is_(None), Settlement.upstream_status_code >= 500),
            Settlement.settled_at > func.now() - timedelta(seconds=window_seconds),
            # A failed delivery that was already credited to prepaid can never be delivered as well. The outcome
            # check is on the row itself, so it is re-evaluated after a concurrent credit's row lock is released.
            Settlement.delivery_outcome.is_distinct_from(DELIVERY_CREDITED),
            ~exists().where(PrepaidLedgerEntry.settlement_id == Settlement.id),
        )
        .values(
            delivery_attempts=Settlement.delivery_attempts + 1, delivery_outcome=DELIVERY_PENDING, delivery_failure=None
        )
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
    payment_response: str | None = None,
) -> Response:
    settings = get_settings()
    tx_id = settlement.tx_id
    try:
        idle = await redis.set(delivery_lock_key(tx_id), "1", nx=True, ex=_delivery_lock_ttl())
    except RedisError:
        logger.exception("delivery marker unavailable")
        return _error(503, "service_unavailable", "payment replay protection is temporarily unavailable")
    if not idle:
        return _error(409, "payment_replayed", "this transaction is already being delivered")
    try:
        try:
            claimed = await _claim_redelivery(
                session, settlement.id, settings.redelivery_max_attempts, settings.redelivery_window_seconds
            )
        except SQLAlchemyError:
            await session.rollback()
            logger.exception("redelivery claim failed")
            return _error(503, "service_unavailable", "settlement store is temporarily unavailable")
        if not claimed:
            return _error(409, "payment_replayed", "this transaction was already used")
        if payment_response is None:
            usd = settlement.usd_value_at_settlement if settlement.usd_value_at_settlement is not None else Decimal(0)
            payment_response = _payment_response_header(tx_id, usd)
        return await _forward_and_record(
            session, redis, client, method, prepared, settlement.id, tx_id, payment_response
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


async def _find_k402_settlement(session: AsyncSession, tx_id: str, payment_id: str) -> Settlement | None:
    """The settlement that already uses this txId (through either binding) or this k402 payment_id."""
    rows = (
        (
            await session.execute(
                select(Settlement).where(or_(Settlement.tx_id == tx_id, Settlement.k402_payment_id == payment_id))
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
    terms: SettlementTerms,
    verified: VerifiedPaymentLike,
    method: str,
    prepared: PreparedUpstream,
    payment_response: Callable[[Settlement], str] | None = None,
) -> Response:
    tx_id = verified.tx_id
    rate = await _lookup_rate(oracle, terms)
    settlement = _new_settlement(endpoint, merchant_address, terms, verified, rate, request)
    # CONTRACT v2.14: the delivery is `pending` from the moment the settlement exists, before the upstream call.
    settlement.delivery_outcome = DELIVERY_PENDING
    settlement_id = settlement.id
    unrecorded_payload = _settlement_payload(settlement, endpoint.slug)
    try:
        # CONTRACT v2.14: stamp the merchant's failed-delivery credit policy as of settle time.
        settlement.failed_delivery_credit_policy = await credit_policy_for(session, endpoint.merchant_id)
        session.add(settlement)
        await session.flush()
        # CONTRACT v2.12: the payment.settled outbox event commits with the settlement, in a savepoint that can
        # never fail this transaction (enqueue_event swallows and logs its own errors).
        await enqueue_event(
            session,
            merchant_id=endpoint.merchant_id,
            event_type="payment.settled",
            data=settlement_event_data(settlement, endpoint.slug),
            source_id=str(settlement_id),
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        logger.error("tx %s (nonce %s) conflicts with an existing settlement", tx_id, terms.nonce, exc_info=True)
        return _error(409, "payment_replayed", "this transaction or payment nonce was already used")
    except SQLAlchemyError:
        await session.rollback()
        logger.error("tx %s was accepted by the node but its settlement could not be stored", tx_id, exc_info=True)
        await _dead_letter_settlement(redis, unrecorded_payload)
        return _error(503, "service_unavailable", "payment was accepted but could not be recorded; contact support")

    if terms.consume_key is not None:
        try:
            await redis.getdel(terms.consume_key)
        except RedisError:
            logger.warning("could not consume %s; the settlement's unique keys still block reuse", terms.consume_key)

    if payment_response is not None:
        header = payment_response(settlement)
    else:
        usd = settlement.usd_value_at_settlement if settlement.usd_value_at_settlement is not None else Decimal(0)
        header = _payment_response_header(tx_id, usd)
    return await _forward_and_record(session, redis, client, method, prepared, settlement_id, tx_id, header)


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
    k402_header = request.headers.get(HEADER_K402_PAYMENT)
    prepaid_header = request.headers.get(HEADER_PREPAID_DEBIT)
    trial_header = request.headers.get(HEADER_TRIAL_CALL)
    if not signature_header and k402_header is None and prepaid_header is None and trial_header is None:
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

    # CONTRACT v2.12: a signed free trial call; never combined with any payment header.
    if trial_header is not None:
        if signature_header or k402_header is not None or prepaid_header is not None:
            return await trial_payment_error(
                request,
                redis,
                endpoint,
                merchant_address,
                oracle,
                "invalid_payment_header",
                f"send {HEADER_TRIAL_CALL} alone, not together with a payment header",
            )
        return await _pay_with_trial(
            request, session, redis, client, oracle, endpoint, merchant_address, method, path, trial_header
        )
    # CONTRACT v2.11: a signed prepaid debit, accepted on every endpoint; never combined with an on-chain payment.
    if prepaid_header is not None:
        if signature_header or k402_header is not None:
            return await prepaid_only_error(
                endpoint,
                merchant_address,
                oracle,
                "invalid_payment_header",
                f"send {HEADER_PREPAID_DEBIT} alone, not together with an on-chain payment header",
            )
        return await _pay_with_prepaid(
            request, session, redis, client, oracle, endpoint, merchant_address, method, path, prepaid_header
        )
    # A prepaid-only endpoint has no on-chain terms: no nonce, k402 offer or exact offer is ever minted for it, and
    # an on-chain payment header (e.g. for an offer issued before the merchant switched it) is refused unpaid.
    if endpoint.prepaid_only:
        if signature_header or k402_header is not None:
            return await prepaid_only_error(
                endpoint,
                merchant_address,
                oracle,
                "invalid_payment_header",
                "this endpoint is paid from a prepaid balance only",
            )
        return with_trial_offer(
            await prepaid_only_error(
                endpoint,
                merchant_address,
                oracle,
                "payment_required",
                "this endpoint is paid from a prepaid balance only",
            ),
            endpoint,
        )

    if k402_header is not None:
        if signature_header:
            return await _challenge_error(
                request,
                redis,
                endpoint,
                merchant_address,
                oracle,
                "invalid_payment_header",
                f"send either {HEADER_PAYMENT_SIGNATURE} or {HEADER_K402_PAYMENT}, not both",
            )
        return await _pay_with_k402(
            request, session, redis, client, engine, oracle, endpoint, merchant_address, method, path, k402_header
        )

    if not signature_header:
        return with_trial_offer(
            await _challenge_error(
                request,
                redis,
                endpoint,
                merchant_address,
                oracle,
                "payment_required",
                "payment is required to access this resource",
                limited=False,
            ),
            endpoint,
        )
    exact_v2_data = _exact_v2_header_data(signature_header)
    if exact_v2_data is not None:
        return await _pay_with_exact_v2(
            request, session, redis, client, engine, oracle, endpoint, merchant_address, path, exact_v2_data
        )
    try:
        signature = PaymentSignature.from_header(signature_header)
    except ValidationError as exc:
        description = str(exc).lower()
        code = "wrong_network" if "prefix" in description or "network" in description else "invalid_payment_header"
        return await _challenge_error(
            request, redis, endpoint, merchant_address, oracle, code, f"{HEADER_PAYMENT_SIGNATURE} header is invalid"
        )
    except ValueError:
        return await _challenge_error(
            request,
            redis,
            endpoint,
            merchant_address,
            oracle,
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
            oracle,
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
            oracle,
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
            request,
            session,
            redis,
            client,
            oracle,
            endpoint,
            merchant_address,
            SettlementTerms.for_v1(required),
            verified,
            method,
            prepared,
        )
    finally:
        await _drop_delivery_lock(redis, tx_id)


async def _pay_with_k402(
    request: Request,
    session: AsyncSession,
    redis: Redis,
    client: httpx.AsyncClient,
    engine: PaymentVerifier,
    oracle: RateProvider,
    endpoint: Endpoint,
    merchant_address: str,
    method: str,
    path: str,
    header_value: str,
) -> Response:
    """CONTRACT v2.9: a paid call carrying `X-K402-Payment: kaspa-utxo <txid> <payment_id>`."""
    settings = get_settings()
    try:
        payment = K402PaymentHeader.from_header(header_value)
    except ValueError:
        return await _challenge_error(
            request,
            redis,
            endpoint,
            merchant_address,
            oracle,
            "invalid_payment_header",
            f"{HEADER_K402_PAYMENT} must be 'kaspa-utxo <64-hex txid> <payment_id>'",
        )
    try:
        prepared = await _prepare_upstream(request, endpoint, path)
    except RequestRejectedError as exc:
        return _error(exc.status_code, exc.error, exc.message)

    tx_id, payment_id = payment.tx_id, payment.payment_id
    try:
        stored = await _load_k402_offer(redis, payment_id)
    except RedisError:
        logger.exception("k402 offer store unavailable")
        return _error(503, "service_unavailable", "payment replay protection is temporarily unavailable")
    try:
        existing = await _find_k402_settlement(session, tx_id, payment_id)
    except SQLAlchemyError:
        logger.exception("settlement lookup failed")
        return _error(503, "service_unavailable", "settlement store is temporarily unavailable")
    if existing is not None:
        if _k402_redelivery_allowed(settings, existing, payment, endpoint, request):
            return await _redeliver(session, redis, client, existing, method, prepared)
        return _error(409, "payment_replayed", "this transaction or payment_id was already used")

    if stored is None:
        return await _challenge_error(
            request,
            redis,
            endpoint,
            merchant_address,
            oracle,
            "payment_expired",
            "the k402 offer is unknown or has expired; pay the fresh offer",
        )
    offer = stored.offer
    if stored.slug != endpoint.slug:
        return await _challenge_error(
            request,
            redis,
            endpoint,
            merchant_address,
            oracle,
            "invalid_payment_header",
            "the payment_id was issued for a different endpoint",
        )

    # payment_id claim first, then the SHARED txId lock, so a txId cannot be claimed through both bindings at once and
    # a refused txId lock never releases a lock owned by another request.
    tx_locked = False
    try:
        if await redis.set(k402_lock_key(payment_id), tx_id, nx=True, ex=REPLAY_LOCK_TTL_SECONDS):
            tx_locked = bool(await redis.set(lock_key(tx_id), "1", nx=True, ex=REPLAY_LOCK_TTL_SECONDS))
            if not tx_locked:
                await redis.delete(k402_lock_key(payment_id))
    except RedisError:
        logger.exception("replay lock unavailable")
        return _error(503, "service_unavailable", "payment replay protection is temporarily unavailable")
    if not tx_locked:
        return _error(409, "payment_replayed", "this transaction or payment_id is already being processed")

    try:
        verified = await engine.verify_k402(tx_id, offer)
    except PaymentVerificationError as exc:
        await _release_k402_locks(redis, tx_id, payment_id)
        if isinstance(exc, RpcUnavailableError):
            logger.warning("k402 payment not verified, node unavailable: %s", exc.message)
            return _error(503, "rpc_unavailable", exc.message, headers={"Retry-After": "5"})
        code = exc.code if exc.code in _K402_VERIFICATION_CODES else "tx_rejected"
        return await _challenge_error(request, redis, endpoint, merchant_address, oracle, code, exc.message)
    except Exception:
        await _release_k402_locks(redis, tx_id, payment_id)
        logger.exception("unexpected failure while verifying k402 tx %s", tx_id)
        return _error(503, "service_unavailable", "payment verification is temporarily unavailable")

    await _hold_delivery_lock(redis, tx_id)
    try:
        return await _settle_and_deliver(
            request,
            session,
            redis,
            client,
            oracle,
            endpoint,
            merchant_address,
            SettlementTerms.for_k402(stored),
            verified,
            method,
            prepared,
        )
    finally:
        await _drop_delivery_lock(redis, tx_id)


async def _release_k402_locks(redis: Redis, tx_id: str, payment_id: str) -> None:
    """Drop the shared txId lock and the payment_id claim after a FAILED k402 verification so the payer can retry."""
    try:
        await redis.delete(lock_key(tx_id), k402_lock_key(payment_id))
    except RedisError:
        logger.error("could not release replay lock for %s; it will expire in %ss", tx_id, REPLAY_LOCK_TTL_SECONDS)


# ---------------------------------------------------------------- kaspa-exact-v2 (CONTRACT v2.10)


def _exact_v2_header_data(header: str) -> dict[str, object] | None:
    """The decoded header when it is an x402 v2 PaymentPayload (has `accepted` or `payload`), else None (v1)."""
    try:
        data = decode_header_json(header, MAX_EXACT_V2_HEADER_BYTES)
    except ValueError:
        return None
    if isinstance(data, dict) and ("accepted" in data or "payload" in data):
        return data
    return None


def _request_body_text(body: bytes) -> str | None:
    """The body as the official client fingerprints it: the text it sent, JSON null when there is none."""
    if not body:
        return None
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PaymentVerificationError(
            "invalid_exact_payment", "kaspa-exact-v2 requests must have no body or a UTF-8 text body"
        ) from exc


async def _find_exact_settlement(session: AsyncSession, tx_id: str, payment_identifier: str) -> Settlement | None:
    """The settlement that already uses this txId (any binding) or this payment identifier; the txId match wins."""
    rows = (
        (
            await session.execute(
                select(Settlement).where(
                    or_(Settlement.tx_id == tx_id, Settlement.payment_identifier == payment_identifier)
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        if row.tx_id == tx_id:
            return row
    return rows[0] if rows else None


def _exact_redelivery_allowed(
    settings: Settings,
    settlement: Settlement,
    checked: ExactV2Verification,
    endpoint: Endpoint,
    request: Request,
) -> bool:
    """payment-identifier idempotency: the same identifier, transaction, request fingerprint, payer, endpoint, method
    and path, with no upstream result or a 5xx, inside the redelivery window and below the attempt cap. The request
    was re-verified in full (authorization signature included) before this is consulted."""
    if settlement.status != SettlementStatus.SETTLED or settlement.settled_at is None:
        return False
    age = datetime.now(UTC) - settlement.settled_at
    return (
        settlement.payment_scheme == SCHEME_EXACT_V2
        and settlement.tx_id == checked.payment.tx_id
        and settlement.payment_identifier == checked.payment_identifier
        and settlement.request_hash == checked.request_hash
        and settlement.payer_address == checked.payment.payer_address
        and settlement.endpoint_id == endpoint.id
        and settlement.request_method == request.method
        and settlement.request_path == request.url.path
        and (settlement.upstream_status_code is None or settlement.upstream_status_code >= 500)
        and age < timedelta(seconds=settings.redelivery_window_seconds)
        and settlement.delivery_attempts < settings.redelivery_max_attempts
    )


async def _release_exact_locks(redis: Redis, tx_id: str, payment_identifier: str) -> None:
    try:
        await redis.delete(lock_key(tx_id), payment_identifier_lock_key(payment_identifier))
    except RedisError:
        logger.error("could not release replay lock for %s; it will expire in %ss", tx_id, REPLAY_LOCK_TTL_SECONDS)


async def _exact_failure(
    request: Request,
    redis: Redis,
    endpoint: Endpoint,
    merchant_address: str,
    oracle: RateProvider,
    exc: PaymentVerificationError,
) -> JSONResponse:
    if isinstance(exc, RpcUnavailableError):
        logger.warning("kaspa-exact-v2 payment not settled, node unavailable: %s", exc.message)
        return _error(503, "rpc_unavailable", exc.message, headers={"Retry-After": "5"})
    if exc.code == "payment_replayed":
        return _error(409, "payment_replayed", exc.message)
    code = exc.code if exc.code in _EXACT_V2_VERIFICATION_CODES else "tx_rejected"
    return await _challenge_error(request, redis, endpoint, merchant_address, oracle, code, exc.message)


async def _unknown_exact_offer(
    request: Request, redis: Redis, endpoint: Endpoint, merchant_address: str, oracle: RateProvider
) -> JSONResponse:
    """`accepted` is not a live offer for this slug (never issued, altered, or expired): 402 with fresh offers."""
    return await _challenge_error(
        request,
        redis,
        endpoint,
        merchant_address,
        oracle,
        "payment_expired",
        "invalid_kaspa_x402_accepted: `accepted` is not a current kaspa-exact-v2 offer for this resource "
        "(unknown, altered or expired); pay the fresh offer",
    )


async def _pay_with_exact_v2(
    request: Request,
    session: AsyncSession,
    redis: Redis,
    client: httpx.AsyncClient,
    engine: PaymentVerifier,
    oracle: RateProvider,
    endpoint: Endpoint,
    merchant_address: str,
    path: str,
    data: dict[str, object],
) -> Response:
    """CONTRACT v2.10: a paid call whose PAYMENT-SIGNATURE is a kaspa-x402 `exact` / `kaspa-exact-v2` PaymentPayload.

    `accepted` must be an offer this gateway issued for this slug (stored with its LOCKED amount and rate); a settled
    payment may also be recognised by its own `accepted` for idempotent redelivery after the offer has expired. Every
    402 here carries fresh offers (rate limited like any new challenge).
    """
    settings = get_settings()
    method = request.method.upper()
    try:
        payment = ExactV2PaymentPayload.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"])
        return await _challenge_error(
            request,
            redis,
            endpoint,
            merchant_address,
            oracle,
            "invalid_payment_header",
            f"{HEADER_PAYMENT_SIGNATURE} is not a valid kaspa-exact-v2 PaymentPayload ({location}: {first['msg']})",
        )
    try:
        prepared = await _prepare_upstream(request, endpoint, path)
    except RequestRejectedError as exc:
        return _error(exc.status_code, exc.error, exc.message)

    try:
        stored = await _load_exact_offer(redis, endpoint.slug, payment.accepted)
    except RedisError:
        logger.exception("kaspa-exact-v2 offer store unavailable")
        return _error(503, "service_unavailable", "payment replay protection is temporarily unavailable")
    # Without a stored offer the client's own `accepted` is only used to recognise an already-settled payment: the
    # request hash binds it, and nothing is settled against it.
    requirement = stored.requirement if stored is not None else payment.accepted

    try:
        request_hash = http_request_hash(
            method, resource_url(request), _request_body_text(prepared.body), payment_requirements_hash(requirement)
        )
        # Pure checks first, without expiry so an idempotent retry of a settled payment can still be recognised.
        checked = engine.verify_exact_v2(payment, requirement, request_hash=request_hash, check_expiry=False)
    except PaymentVerificationError as exc:
        if stored is None and not isinstance(exc, RpcUnavailableError):
            return await _unknown_exact_offer(request, redis, endpoint, merchant_address, oracle)
        return await _exact_failure(request, redis, endpoint, merchant_address, oracle, exc)
    except Exception:
        logger.exception("unexpected failure while checking a kaspa-exact-v2 payment")
        return _error(503, "service_unavailable", "payment verification is temporarily unavailable")
    tx_id = checked.payment.tx_id
    payment_identifier = checked.payment_identifier

    try:
        existing = await _find_exact_settlement(session, tx_id, payment_identifier)
    except SQLAlchemyError:
        logger.exception("settlement lookup failed")
        return _error(503, "service_unavailable", "settlement store is temporarily unavailable")
    if existing is not None:
        if _exact_redelivery_allowed(settings, existing, checked, endpoint, request):
            header = _exact_settlement_header(existing, checked.payment_output_index)
            return await _redeliver(session, redis, client, existing, method, prepared, header)
        if existing.payment_identifier == payment_identifier and existing.request_hash != request_hash:
            message = "kaspa_payment_identifier_conflict: this payment identifier was used for a different request"
        else:
            message = "this transaction or payment identifier was already used"
        return _error(409, "payment_replayed", message)

    if stored is None:
        return await _unknown_exact_offer(request, redis, endpoint, merchant_address, oracle)
    try:
        check_authorization_expiry(payment, requirement, datetime.now(UTC))
    except PaymentVerificationError as exc:
        return await _exact_failure(request, redis, endpoint, merchant_address, oracle, exc)

    # payment-identifier claim first, then the replay lock SHARED with kaspa-exact-v1 and k402 (`kasgate:tx:{txId}`),
    # so a refused txId lock never releases a lock owned by another request.
    tx_locked = False
    try:
        if await redis.set(payment_identifier_lock_key(payment_identifier), tx_id, nx=True, ex=REPLAY_LOCK_TTL_SECONDS):
            tx_locked = bool(await redis.set(lock_key(tx_id), "1", nx=True, ex=REPLAY_LOCK_TTL_SECONDS))
            if not tx_locked:
                await redis.delete(payment_identifier_lock_key(payment_identifier))
    except RedisError:
        logger.exception("replay lock unavailable")
        return _error(503, "service_unavailable", "payment replay protection is temporarily unavailable")
    if not tx_locked:
        return _error(409, "payment_replayed", "this transaction or payment identifier is already being processed")

    try:
        await engine.resolve_exact_v2_inputs(checked)
        verified = await engine.settle_exact_v2(checked)
    except PaymentVerificationError as exc:
        await _release_exact_locks(redis, tx_id, payment_identifier)
        return await _exact_failure(request, redis, endpoint, merchant_address, oracle, exc)
    except Exception:
        await _release_exact_locks(redis, tx_id, payment_identifier)
        logger.exception("unexpected failure while settling kaspa-exact-v2 tx %s", tx_id)
        return _error(503, "service_unavailable", "payment verification is temporarily unavailable")

    output_index = checked.payment_output_index
    await _hold_delivery_lock(redis, tx_id)
    try:
        return await _settle_and_deliver(
            request,
            session,
            redis,
            client,
            oracle,
            endpoint,
            merchant_address,
            SettlementTerms.for_exact_v2(checked, stored.locked_rate),
            verified,
            method,
            prepared,
            lambda row: _exact_settlement_header(row, output_index),
        )
    finally:
        await _drop_delivery_lock(redis, tx_id)


# ---------------------------------------------------------------- prepaid credit (CONTRACT v2.11)
#
# A buyer deposits once with an ordinary kaspa-exact-v1 split payment (output 0 merchant, output 1 treasury, fee
# max(1.5 %, 0.2 KAS), total >= 0.6 KAS; app/prepaid.py) and is credited the merchant share. Later calls carry
# `X-KasGate-Prepaid: Base64(JSON{nonce, signature})`, where `signature` signs the debit challenge minted by
# POST /v1/prepaid/challenge (build_prepaid_debit_message: domain, payer, merchant, method, path + query, body hash,
# amount, nonce, expiry). The gateway rebuilds that message from the stored challenge AND the request it is actually
# serving, takes `kasgate:prepaid:noncelock:{nonce}`, then debits the balance with a conditional UPDATE (never below
# zero) and inserts the debit (unique nonce) in ONE transaction, consumes the challenge and forwards. No response or
# a 5xx from the upstream refunds the debit. Nothing in this path touches the chain.

PREPAID_DEPOSIT_PREFIX: Final = "kasgate:prepaid:deposit:"
PREPAID_CHALLENGE_PREFIX: Final = "kasgate:prepaid:challenge:"
PREPAID_NONCE_LOCK_PREFIX: Final = "kasgate:prepaid:noncelock:"
PREPAID_REFUND_DEAD_LETTER_KEY: Final = "kasgate:deadletter:prepaid-refunds"


def prepaid_deposit_key(nonce: str) -> str:
    return f"{PREPAID_DEPOSIT_PREFIX}{nonce}"


def prepaid_challenge_key(nonce: str) -> str:
    return f"{PREPAID_CHALLENGE_PREFIX}{nonce}"


def prepaid_nonce_lock_key(nonce: str) -> str:
    return f"{PREPAID_NONCE_LOCK_PREFIX}{nonce}"


def verify_prepaid_signature(message: str, signature: str, public_key: str) -> bool:
    try:
        return bool(verify_message(message, signature, PublicKey(public_key)))
    except Exception:
        return False


def request_path_and_query(request: Request) -> str:
    """The raw (percent-encoded) request path plus `?query`: the `Path` a prepaid debit is bound to."""
    raw_path = request.scope.get("raw_path")
    path = raw_path.decode("latin-1").split("?", 1)[0] if isinstance(raw_path, bytes) else request.url.path
    query = request.scope.get("query_string", b"")
    query_text = query.decode("latin-1") if isinstance(query, bytes) else ""
    return path + (f"?{query_text}" if query_text else "")


async def prepaid_price(endpoint: Endpoint, oracle: RateProvider) -> tuple[int, Decimal | None]:
    """Per-call prepaid debit in sompi and the KAS/USD rate it was quoted at (USD-priced endpoints only).

    KAS-priced: price_sompi, whatever its size. USD-priced: ceil conversion WITHOUT the 0.6 KAS on-chain floor.
    Raises OracleUnavailableError (USD, oracle down) or ValueError (unpriceable row).
    """
    if endpoint.price_currency is PriceCurrency.KAS:
        if endpoint.price_sompi is None or endpoint.price_sompi <= 0:
            raise ValueError("KAS-priced endpoint is missing price_sompi")
        return endpoint.price_sompi, None
    if endpoint.price_usd_micros is None:
        raise ValueError("USD-priced endpoint is missing price_usd_micros")
    try:
        rate = await oracle.get_rate()
    except OracleUnavailableError:
        raise
    except Exception as exc:
        raise OracleUnavailableError("KAS/USD rate is unavailable") from exc
    return usd_micros_to_prepaid_sompi(endpoint.price_usd_micros, rate), rate


def build_prepaid_offer(endpoint: Endpoint, merchant_address: str, amount_sompi: int) -> PrepaidOffer:
    settings = get_settings()
    base = settings.public_base_url
    return PrepaidOffer(
        network=settings.kaspa_network,
        amount=str(amount_sompi),
        payTo=merchant_address,
        merchantId=endpoint.merchant_id,
        prepaidOnly=endpoint.prepaid_only,
        minDepositSompi=str(min_price_sompi()),
        depositQuoteUrl=f"{base}/v1/prepaid/deposits/quote",
        depositUrl=f"{base}/v1/prepaid/deposits",
        challengeUrl=f"{base}/v1/prepaid/challenge",
        balanceUrl=f"{base}/v1/prepaid/balance?slug={endpoint.slug}",
    )


async def prepaid_only_error(
    endpoint: Endpoint,
    merchant_address: str,
    oracle: RateProvider,
    error: str,
    message: str,
    *,
    status_code: int = 402,
    amount_sompi: int | None = None,
) -> JSONResponse:
    """An x402 error whose `accepts` is the prepaid offer only. No on-chain challenge, nonce or Redis key is minted.

    Used for every 402 of a prepaid-only endpoint and for prepaid debit failures. A USD price that cannot be
    quoted right now is 503 price_unavailable, exactly like the on-chain path.
    """
    if amount_sompi is None:
        try:
            amount_sompi, _rate = await prepaid_price(endpoint, oracle)
        except OracleUnavailableError as exc:
            logger.warning("USD price unavailable for %s: %s", endpoint.slug, exc)
            return _error(503, "price_unavailable", "KAS/USD price is temporarily unavailable")
        except ValueError as exc:
            logger.error("endpoint %s cannot be priced: %s", endpoint.slug, exc)
            return _error(503, "service_unavailable", "endpoint price is not currently payable")
    offer = build_prepaid_offer(endpoint, merchant_address, amount_sompi)
    content = X402ErrorBody.model_validate(
        {"error": error, "message": message, "accepts": [offer.model_dump(mode="json")]}
    ).model_dump(mode="json")
    return JSONResponse(status_code=status_code, content=content)


@dataclass(frozen=True)
class StoredPrepaidChallenge:
    slug: str
    merchant_id: uuid.UUID
    payer_address: str
    payer_public_key: str
    method: str
    path: str
    body_hash: str
    amount_sompi: int
    kas_usd_rate: Decimal | None
    expires_at: datetime

    def message(self, nonce: str) -> str:
        return build_prepaid_debit_message(
            domain=auth_domain(),
            address=self.payer_address,
            merchant_id=self.merchant_id,
            method=self.method,
            path=self.path,
            body_hash=self.body_hash,
            amount_sompi=self.amount_sompi,
            nonce=nonce,
            expires_at=self.expires_at,
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "slug": self.slug,
                "merchantId": str(self.merchant_id),
                "payerAddress": self.payer_address,
                "payerPublicKey": self.payer_public_key,
                "method": self.method,
                "path": self.path,
                "bodyHash": self.body_hash,
                "amountSompi": str(self.amount_sompi),
                "kasUsdRate": None if self.kas_usd_rate is None else format(self.kas_usd_rate, "f"),
                "expiresAt": self.expires_at.isoformat(),
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "StoredPrepaidChallenge":
        data = json.loads(raw)
        rate = data["kasUsdRate"]
        amount = int(data["amountSompi"])
        expires_at = datetime.fromisoformat(data["expiresAt"])
        public_key = data["payerPublicKey"]
        if amount <= 0 or expires_at.tzinfo is None or not re.fullmatch(r"[0-9a-f]{64}", public_key):
            raise ValueError("stored prepaid challenge is malformed")
        return cls(
            slug=str(data["slug"]),
            merchant_id=uuid.UUID(data["merchantId"]),
            payer_address=str(data["payerAddress"]),
            payer_public_key=public_key,
            method=str(data["method"]),
            path=str(data["path"]),
            body_hash=str(data["bodyHash"]),
            amount_sompi=amount,
            kas_usd_rate=None if rate is None else Decimal(rate),
            expires_at=expires_at,
        )


async def _load_prepaid_challenge(redis: Redis, nonce: str) -> StoredPrepaidChallenge | None:
    raw = await redis.get(prepaid_challenge_key(nonce))
    if raw is None:
        return None
    try:
        return StoredPrepaidChallenge.from_json(raw)
    except (ValueError, KeyError, TypeError, ArithmeticError):
        logger.error("stored prepaid challenge %s is unreadable", nonce, exc_info=True)
        return None


def _prepaid_receipt_header(debit_id: uuid.UUID, nonce: str, amount: int, balance: int, status: str) -> str:
    return PrepaidReceipt(
        debitId=debit_id,
        nonce=nonce,
        amountSompi=str(amount),
        balanceSompi=str(balance),
        status="refunded" if status == PREPAID_DEBIT_REFUNDED else "debited",
    ).to_header()


async def _debit_balance(
    session: AsyncSession,
    endpoint: Endpoint,
    stored: StoredPrepaidChallenge,
    nonce: str,
    request: Request,
) -> tuple[uuid.UUID, int] | None:
    """Atomically take `amount` from the balance and insert the debit row. None = no balance or not enough.

    The conditional UPDATE serialises concurrent debits on the balance row and can never take it below zero (the
    ck_prepaid_balances_balance_not_negative constraint is the backstop). Raises IntegrityError when the nonce was
    already debited, SQLAlchemyError when the store fails; the caller rolls back.
    """
    amount = stored.amount_sompi
    # CONTRACT v2.14: failed-delivery credit is spent before deposited credit. Lock the row first so the part paid
    # from credit (recorded on the debit, and given back to credit if the debit is refunded) is exact.
    credit_before = (
        await session.execute(
            select(PrepaidBalance.credit_balance_sompi)
            .where(
                PrepaidBalance.merchant_id == endpoint.merchant_id,
                PrepaidBalance.payer_public_key == stored.payer_public_key,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    from_credit = min(amount, credit_before or 0)
    row = (
        await session.execute(
            update(PrepaidBalance)
            .where(
                PrepaidBalance.merchant_id == endpoint.merchant_id,
                PrepaidBalance.payer_public_key == stored.payer_public_key,
                # CONTRACT v2.13: only spendable credit pays calls (an open refund's hold and expired credit do not).
                PrepaidBalance.balance_sompi - PrepaidBalance.refund_hold_sompi >= amount,
                or_(PrepaidBalance.expires_at.is_(None), PrepaidBalance.expires_at > func.now()),
            )
            .values(
                balance_sompi=PrepaidBalance.balance_sompi - amount,
                debited_sompi=PrepaidBalance.debited_sompi + amount,
                credit_balance_sompi=PrepaidBalance.credit_balance_sompi - from_credit,
                updated_at=func.now(),
            )
            .returning(PrepaidBalance.id, PrepaidBalance.balance_sompi)
        )
    ).one_or_none()
    if row is None:
        # Nothing was written. Commit (not rollback) so the caller's loaded endpoint is not expired.
        await session.commit()
        return None
    balance_id, balance = row
    debit_id = uuid.uuid4()
    session.add(
        PrepaidDebit(
            id=debit_id,
            balance_id=balance_id,
            merchant_id=endpoint.merchant_id,
            endpoint_id=endpoint.id,
            nonce=nonce,
            payer_address=stored.payer_address,
            amount_sompi=amount,
            credit_sompi=from_credit,
            kas_usd_rate=stored.kas_usd_rate,
            usd_value=None if stored.kas_usd_rate is None else usd_value(amount, stored.kas_usd_rate),
            status=PREPAID_DEBIT_DEBITED,
            request_method=request.method.upper(),
            request_path=stored.path,
            body_hash=None if stored.body_hash == PREPAID_EMPTY_BODY_HASH else stored.body_hash,
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()
    await enqueue_event(
        session,
        merchant_id=endpoint.merchant_id,
        event_type="prepaid.debit",
        data={
            "debit_id": str(debit_id),
            "endpoint_id": str(endpoint.id),
            "endpoint_slug": endpoint.slug,
            "payer_address": stored.payer_address,
            "amount_sompi": str(amount),
            "balance_sompi": str(int(balance)),
            "kas_usd_rate": None if stored.kas_usd_rate is None else format(stored.kas_usd_rate, "f"),
            "request_method": request.method.upper(),
            "request_path": stored.path,
        },
        source_id=str(debit_id),
    )
    await session.commit()
    return debit_id, int(balance)


async def refund_prepaid_debit(
    session: AsyncSession, redis: Redis, debit_id: uuid.UUID, status_code: int | None
) -> int | None:
    """Return a debit to its balance (status refunded) in one transaction; the new balance, or None if it failed.

    Only a `debited` row is refunded, so a refund can never run twice. A store failure is dead-lettered to Redis
    for reconciliation.
    """
    try:
        row = (
            await session.execute(
                update(PrepaidDebit)
                .where(PrepaidDebit.id == debit_id, PrepaidDebit.status == PREPAID_DEBIT_DEBITED)
                .values(status=PREPAID_DEBIT_REFUNDED, refunded_at=func.now(), upstream_status_code=status_code)
                .returning(PrepaidDebit.balance_id, PrepaidDebit.amount_sompi, PrepaidDebit.credit_sompi)
            )
        ).one_or_none()
        if row is None:
            await session.rollback()
            return None
        balance_id, amount, from_credit = row
        balance = (
            await session.execute(
                update(PrepaidBalance)
                .where(PrepaidBalance.id == balance_id)
                .values(
                    balance_sompi=PrepaidBalance.balance_sompi + amount,
                    debited_sompi=PrepaidBalance.debited_sompi - amount,
                    # CONTRACT v2.14: the part paid from failed-delivery credit goes back to credit, never to deposit.
                    credit_balance_sompi=PrepaidBalance.credit_balance_sompi + from_credit,
                    updated_at=func.now(),
                )
                .returning(PrepaidBalance.balance_sompi)
            )
        ).scalar_one()
        await session.commit()
        return int(balance)
    except SQLAlchemyError:
        await session.rollback()
        logger.error("could not refund prepaid debit %s", debit_id, exc_info=True)
        payload = json.dumps({"debitId": str(debit_id), "upstreamStatusCode": status_code}, sort_keys=True)
        try:
            await cast(Awaitable[int], redis.lpush(PREPAID_REFUND_DEAD_LETTER_KEY, payload))
        except RedisError:
            logger.error("dead-letter queue unavailable; unrefunded prepaid debit: %s", payload, exc_info=True)
        return None


async def _record_debit_status(session: AsyncSession, debit_id: uuid.UUID, status_code: int) -> None:
    try:
        await session.execute(
            update(PrepaidDebit).where(PrepaidDebit.id == debit_id).values(upstream_status_code=status_code)
        )
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("could not store upstream status for prepaid debit %s", debit_id)


async def _pay_with_prepaid(
    request: Request,
    session: AsyncSession,
    redis: Redis,
    client: httpx.AsyncClient,
    oracle: RateProvider,
    endpoint: Endpoint,
    merchant_address: str,
    method: str,
    path: str,
    header: str,
) -> Response:
    async def failure(error: str, message: str) -> JSONResponse:
        return await prepaid_only_error(endpoint, merchant_address, oracle, error, message)

    try:
        debit = PrepaidDebitHeader.from_header(header)
    except (ValidationError, ValueError):
        return await failure("invalid_payment_header", f"{HEADER_PREPAID_DEBIT} header is malformed")
    nonce = debit.nonce

    try:
        prepared = await _prepare_upstream(request, endpoint, path)
    except RequestRejectedError as exc:
        return _error(exc.status_code, exc.error, exc.message)

    try:
        stored = await _load_prepaid_challenge(redis, nonce)
    except RedisError:
        logger.exception("prepaid challenge store unavailable")
        return _error(503, "service_unavailable", "prepaid debits are temporarily unavailable")
    try:
        used = (await session.execute(select(PrepaidDebit.id).where(PrepaidDebit.nonce == nonce))).first()
    except SQLAlchemyError:
        logger.exception("prepaid debit lookup failed")
        return _error(503, "service_unavailable", "prepaid store is temporarily unavailable")
    if used is not None:
        return _error(409, "payment_replayed", "this prepaid debit nonce was already used")
    if stored is None or stored.expires_at <= datetime.now(UTC):
        return await failure("payment_expired", "the prepaid challenge is unknown or has expired; request a new one")

    # Bind to the request actually being served: never trust client-supplied copies of these fields.
    actual_path = request_path_and_query(request)
    actual_body_hash = prepaid_body_hash(prepared.body)
    if (
        stored.slug != endpoint.slug
        or stored.merchant_id != endpoint.merchant_id
        or stored.method != method
        or stored.path != actual_path
        or stored.body_hash != actual_body_hash
    ):
        return await failure(
            "invalid_payment_header", "the prepaid challenge was issued for another endpoint, method, path or body"
        )
    if payer_public_key(stored.payer_address) != stored.payer_public_key or not verify_prepaid_signature(
        stored.message(nonce), debit.signature, stored.payer_public_key
    ):
        return await failure(
            "invalid_payment_header", "signature is not a signature by the depositing key over this debit challenge"
        )

    try:
        claimed = bool(await redis.set(prepaid_nonce_lock_key(nonce), "1", nx=True, ex=REPLAY_LOCK_TTL_SECONDS))
    except RedisError:
        logger.exception("prepaid replay lock unavailable")
        return _error(503, "service_unavailable", "prepaid replay protection is temporarily unavailable")
    if not claimed:
        return _error(409, "payment_replayed", "this prepaid debit is already being processed")

    try:
        debited = await _debit_balance(session, endpoint, stored, nonce, request)
    except IntegrityError:
        await session.rollback()
        return _error(409, "payment_replayed", "this prepaid debit nonce was already used")
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("prepaid debit could not be stored")
        await _release_prepaid_lock(redis, nonce)
        return _error(503, "service_unavailable", "prepaid store is temporarily unavailable")
    if debited is None:
        await _release_prepaid_lock(redis, nonce)
        return await prepaid_only_error(
            endpoint,
            merchant_address,
            oracle,
            "insufficient_balance",
            "the prepaid balance does not cover this call; deposit more first",
            amount_sompi=stored.amount_sompi,
        )
    debit_id, balance = debited
    try:
        await redis.getdel(prepaid_challenge_key(nonce))
    except RedisError:
        logger.warning("could not consume prepaid challenge %s; the unique nonce still blocks reuse", nonce)

    try:
        result = await _call_upstream(client, method, prepared)
    except UpstreamFailedError as exc:
        logger.warning("upstream failure for prepaid debit %s: %s", debit_id, exc)
        refunded = await refund_prepaid_debit(session, redis, debit_id, None)
        receipt = _prepaid_receipt_header(
            debit_id,
            nonce,
            stored.amount_sompi,
            balance if refunded is None else refunded,
            PREPAID_DEBIT_DEBITED if refunded is None else PREPAID_DEBIT_REFUNDED,
        )
        return _error(
            502,
            "upstream_error",
            "the upstream API did not return a response"
            + ("; the debit was refunded" if refunded is not None else "; the refund is pending reconciliation"),
            headers={HEADER_PREPAID_RECEIPT: receipt},
        )
    if result.status_code >= 500:
        refunded = await refund_prepaid_debit(session, redis, debit_id, result.status_code)
        status = PREPAID_DEBIT_DEBITED if refunded is None else PREPAID_DEBIT_REFUNDED
        balance = balance if refunded is None else refunded
    else:
        await _record_debit_status(session, debit_id, result.status_code)
        status = PREPAID_DEBIT_DEBITED
    response = Response(content=result.body, status_code=result.status_code)
    for name, value in result.headers:
        response.raw_headers.append((name.lower().encode("latin-1"), value.encode("latin-1")))
    response.headers[HEADER_PREPAID_RECEIPT] = _prepaid_receipt_header(
        debit_id, nonce, stored.amount_sompi, balance, status
    )
    return response


async def _release_prepaid_lock(redis: Redis, nonce: str) -> None:
    try:
        await redis.delete(prepaid_nonce_lock_key(nonce))
    except RedisError:
        logger.error("could not release prepaid lock for %s; it expires in %ss", nonce, REPLAY_LOCK_TTL_SECONDS)


# ---------------------------------------------------------------- free trial calls (CONTRACT v2.12)

TRIAL_CHALLENGE_PREFIX: Final = "kasgate:trial:challenge:"
TRIAL_NONCE_LOCK_PREFIX: Final = "kasgate:trial:noncelock:"
TRIAL_REFUND_DEAD_LETTER_KEY: Final = "kasgate:deadletter:trial-refunds"


def trial_challenge_key(nonce: str) -> str:
    return f"{TRIAL_CHALLENGE_PREFIX}{nonce}"


def trial_nonce_lock_key(nonce: str) -> str:
    return f"{TRIAL_NONCE_LOCK_PREFIX}{nonce}"


def trial_offer_header(endpoint: Endpoint) -> str | None:
    """The X-KasGate-Trial-Offer value for a 402 from `endpoint`, or None when it has no free trial calls."""
    if endpoint.free_trial_calls <= 0:
        return None
    base = get_settings().public_base_url
    return TrialOffer(
        freeTrialCalls=endpoint.free_trial_calls,
        challengeUrl=f"{base}/v1/trials/challenge",
        statusUrl=f"{base}/v1/trials/status?slug={endpoint.slug}",
    ).to_header()


def with_trial_offer(response: Response, endpoint: Endpoint) -> Response:
    """Advertise the endpoint's free trial on a 402 (no-op for any other status or an endpoint without trials)."""
    if response.status_code == 402:
        offer = trial_offer_header(endpoint)
        if offer is not None:
            response.headers[HEADER_TRIAL_OFFER] = offer
    return response


@dataclass(frozen=True)
class StoredTrialChallenge:
    slug: str
    endpoint_id: uuid.UUID
    merchant_id: uuid.UUID
    payer_address: str
    payer_public_key: str
    method: str
    path: str
    body_hash: str
    expires_at: datetime

    def message(self, nonce: str) -> str:
        return build_trial_call_message(
            domain=auth_domain(),
            address=self.payer_address,
            merchant_id=self.merchant_id,
            slug=self.slug,
            method=self.method,
            path=self.path,
            body_hash=self.body_hash,
            nonce=nonce,
            expires_at=self.expires_at,
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "slug": self.slug,
                "endpointId": str(self.endpoint_id),
                "merchantId": str(self.merchant_id),
                "payerAddress": self.payer_address,
                "payerPublicKey": self.payer_public_key,
                "method": self.method,
                "path": self.path,
                "bodyHash": self.body_hash,
                "expiresAt": self.expires_at.isoformat(),
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str | bytes) -> "StoredTrialChallenge":
        data = json.loads(raw)
        expires_at = datetime.fromisoformat(data["expiresAt"])
        public_key = data["payerPublicKey"]
        if expires_at.tzinfo is None or not re.fullmatch(r"[0-9a-f]{64}", public_key):
            raise ValueError("stored trial challenge is malformed")
        return cls(
            slug=str(data["slug"]),
            endpoint_id=uuid.UUID(data["endpointId"]),
            merchant_id=uuid.UUID(data["merchantId"]),
            payer_address=str(data["payerAddress"]),
            payer_public_key=public_key,
            method=str(data["method"]),
            path=str(data["path"]),
            body_hash=str(data["bodyHash"]),
            expires_at=expires_at,
        )


async def _load_trial_challenge(redis: Redis, nonce: str) -> StoredTrialChallenge | None:
    raw = await redis.get(trial_challenge_key(nonce))
    if raw is None:
        return None
    try:
        return StoredTrialChallenge.from_json(raw)
    except (ValueError, KeyError, TypeError):
        logger.error("stored trial challenge %s is unreadable", nonce, exc_info=True)
        return None


def utc_today() -> date:
    return datetime.now(UTC).date()


async def trial_daily_calls(session: AsyncSession, endpoint_id: uuid.UUID) -> int:
    calls = (
        await session.execute(
            select(TrialDailyCount.calls).where(
                TrialDailyCount.endpoint_id == endpoint_id, TrialDailyCount.day == utc_today()
            )
        )
    ).scalar_one_or_none()
    return 0 if calls is None else int(calls)


async def trial_calls_used(session: AsyncSession, endpoint_id: uuid.UUID, public_key: str) -> int:
    used = (
        await session.execute(
            select(TrialUsage.calls_used).where(
                TrialUsage.endpoint_id == endpoint_id, TrialUsage.payer_public_key == public_key
            )
        )
    ).scalar_one_or_none()
    return 0 if used is None else int(used)


@dataclass(frozen=True)
class TrialUse:
    call_id: uuid.UUID
    calls_used: int
    limit: int

    @property
    def remaining(self) -> int:
        return max(self.limit - self.calls_used, 0)


async def _use_trial(
    session: AsyncSession, endpoint: Endpoint, stored: StoredTrialChallenge, nonce: str, request: Request
) -> TrialUse | None:
    """ONE transaction: per-key count (conditional UPDATE under the row lock), the endpoint's daily cap (conditional
    upsert), the trial_calls row (unique nonce) and the trial.used outbox event. None = per-key or daily limit
    reached; nothing is written then. Raises IntegrityError for a used nonce, SQLAlchemyError when the store fails.
    """
    limit = endpoint.free_trial_calls
    cap = endpoint.trial_daily_cap
    if limit <= 0:
        return None
    await session.execute(
        pg_insert(TrialUsage)
        .values(
            id=uuid.uuid4(),
            endpoint_id=endpoint.id,
            merchant_id=endpoint.merchant_id,
            payer_address=stored.payer_address,
            payer_public_key=stored.payer_public_key,
            calls_used=0,
        )
        .on_conflict_do_nothing(index_elements=[TrialUsage.endpoint_id, TrialUsage.payer_public_key])
    )
    usage = (
        await session.execute(
            update(TrialUsage)
            .where(
                TrialUsage.endpoint_id == endpoint.id,
                TrialUsage.payer_public_key == stored.payer_public_key,
                TrialUsage.calls_used < limit,
            )
            .values(calls_used=TrialUsage.calls_used + 1, updated_at=func.now())
            .returning(TrialUsage.id, TrialUsage.calls_used)
        )
    ).one_or_none()
    if usage is None:
        await _undo_trial(session, endpoint)
        return None
    usage_id, calls_used = usage
    today_insert = pg_insert(TrialDailyCount).values(endpoint_id=endpoint.id, day=utc_today(), calls=1)
    daily = (
        await session.execute(
            today_insert.on_conflict_do_update(
                index_elements=[TrialDailyCount.endpoint_id, TrialDailyCount.day],
                set_={"calls": TrialDailyCount.calls + 1},
                where=TrialDailyCount.calls < cap,
            ).returning(TrialDailyCount.calls)
        )
    ).scalar_one_or_none()
    if daily is None:
        await _undo_trial(session, endpoint)
        return None
    call_id = uuid.uuid4()
    session.add(
        TrialCall(
            id=call_id,
            usage_id=usage_id,
            endpoint_id=endpoint.id,
            merchant_id=endpoint.merchant_id,
            nonce=nonce,
            payer_address=stored.payer_address,
            status=TRIAL_CALL_USED,
            request_method=request.method.upper(),
            request_path=stored.path,
            body_hash=None if stored.body_hash == PREPAID_EMPTY_BODY_HASH else stored.body_hash,
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()
    await enqueue_event(
        session,
        merchant_id=endpoint.merchant_id,
        event_type="trial.used",
        data={
            "trial_call_id": str(call_id),
            "endpoint_id": str(endpoint.id),
            "endpoint_slug": endpoint.slug,
            "payer_address": stored.payer_address,
            "calls_used": int(calls_used),
            "free_trial_calls": limit,
            "remaining": max(limit - int(calls_used), 0),
            "request_method": request.method.upper(),
            "request_path": stored.path,
        },
        source_id=str(call_id),
    )
    await session.commit()
    return TrialUse(call_id=call_id, calls_used=int(calls_used), limit=limit)


async def _undo_trial(session: AsyncSession, endpoint: Endpoint) -> None:
    """Discard a partly written trial use, then reload the endpoint (a rollback expires every loaded row)."""
    await session.rollback()
    await session.refresh(endpoint)


async def refund_trial_call(session: AsyncSession, redis: Redis, call_id: uuid.UUID, status_code: int | None) -> bool:
    """Give an unanswered (no response or 5xx) trial call back to the buyer key. Only a `used` row is refunded, so a
    refund can never run twice. The endpoint's daily counter is not refunded. Failures are dead-lettered."""
    try:
        usage_id = (
            await session.execute(
                update(TrialCall)
                .where(TrialCall.id == call_id, TrialCall.status == TRIAL_CALL_USED)
                .values(status=TRIAL_CALL_REFUNDED, refunded_at=func.now(), upstream_status_code=status_code)
                .returning(TrialCall.usage_id)
            )
        ).scalar_one_or_none()
        if usage_id is None:
            await session.rollback()
            return False
        await session.execute(
            update(TrialUsage)
            .where(TrialUsage.id == usage_id, TrialUsage.calls_used > 0)
            .values(calls_used=TrialUsage.calls_used - 1, updated_at=func.now())
        )
        await session.commit()
        return True
    except SQLAlchemyError:
        await session.rollback()
        logger.error("could not refund trial call %s", call_id, exc_info=True)
        payload = json.dumps({"trialCallId": str(call_id), "upstreamStatusCode": status_code}, sort_keys=True)
        try:
            await cast(Awaitable[int], redis.lpush(TRIAL_REFUND_DEAD_LETTER_KEY, payload))
        except RedisError:
            logger.error("dead-letter queue unavailable; unrefunded trial call: %s", payload, exc_info=True)
        return False


async def _record_trial_status(session: AsyncSession, call_id: uuid.UUID, status_code: int) -> None:
    try:
        await session.execute(update(TrialCall).where(TrialCall.id == call_id).values(upstream_status_code=status_code))
        await session.commit()
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("could not store upstream status for trial call %s", call_id)


def _trial_state_headers(use: TrialUse, nonce: str, status: str) -> dict[str, str]:
    remaining = use.remaining if status == TRIAL_CALL_USED else min(use.remaining + 1, use.limit)
    return {
        HEADER_TRIAL_LIMIT: str(use.limit),
        HEADER_TRIAL_REMAINING: str(remaining),
        HEADER_TRIAL_RECEIPT: TrialReceipt(
            trialCallId=use.call_id,
            nonce=nonce,
            limit=use.limit,
            remaining=remaining,
            status="refunded" if status == TRIAL_CALL_REFUNDED else "used",
        ).to_header(),
    }


async def trial_payment_error(
    request: Request,
    redis: Redis,
    endpoint: Endpoint,
    merchant_address: str,
    oracle: RateProvider,
    error: str,
    message: str,
    *,
    remaining: int | None = None,
) -> Response:
    """A failed trial call answers with the endpoint's normal 402 (fresh on-chain challenge, or the prepaid offer on
    a prepaid-only endpoint) so the buyer can pay straight away."""
    if endpoint.prepaid_only:
        response: Response = await prepaid_only_error(endpoint, merchant_address, oracle, error, message)
    else:
        response = await _challenge_error(request, redis, endpoint, merchant_address, oracle, error, message)
    if remaining is not None:
        response.headers[HEADER_TRIAL_REMAINING] = str(remaining)
    return with_trial_offer(response, endpoint) if remaining is None or remaining > 0 else response


async def _pay_with_trial(
    request: Request,
    session: AsyncSession,
    redis: Redis,
    client: httpx.AsyncClient,
    oracle: RateProvider,
    endpoint: Endpoint,
    merchant_address: str,
    method: str,
    path: str,
    header: str,
) -> Response:
    async def failure(error: str, message: str, remaining: int | None = None) -> Response:
        return await trial_payment_error(
            request, redis, endpoint, merchant_address, oracle, error, message, remaining=remaining
        )

    try:
        trial = TrialCallHeader.from_header(header)
    except (ValidationError, ValueError):
        return await failure("invalid_payment_header", f"{HEADER_TRIAL_CALL} header is malformed")
    nonce = trial.nonce

    try:
        prepared = await _prepare_upstream(request, endpoint, path)
    except RequestRejectedError as exc:
        return _error(exc.status_code, exc.error, exc.message)

    try:
        await enforce_rate_limit(redis, RATE_LIMIT_TRIAL_CALLS, f"ip:{client_ip(request)}")
    except HTTPException as exc:
        return _limit_error(exc)

    try:
        stored = await _load_trial_challenge(redis, nonce)
    except RedisError:
        logger.exception("trial challenge store unavailable")
        return _error(503, "service_unavailable", "free trial calls are temporarily unavailable")
    try:
        used = (await session.execute(select(TrialCall.id).where(TrialCall.nonce == nonce))).first()
    except SQLAlchemyError:
        logger.exception("trial call lookup failed")
        return _error(503, "service_unavailable", "trial store is temporarily unavailable")
    if used is not None:
        return _error(409, "payment_replayed", "this trial nonce was already used")
    if stored is None or stored.expires_at <= datetime.now(UTC):
        return await failure("payment_expired", "the trial challenge is unknown or has expired; request a new one")

    actual_path = request_path_and_query(request)
    actual_body_hash = prepaid_body_hash(prepared.body)
    if (
        stored.slug != endpoint.slug
        or stored.endpoint_id != endpoint.id
        or stored.merchant_id != endpoint.merchant_id
        or stored.method != method
        or stored.path != actual_path
        or stored.body_hash != actual_body_hash
    ):
        return await failure(
            "invalid_payment_header", "the trial challenge was issued for another endpoint, method, path or body"
        )
    if payer_public_key(stored.payer_address) != stored.payer_public_key or not verify_prepaid_signature(
        stored.message(nonce), trial.signature, stored.payer_public_key
    ):
        return await failure("invalid_payment_header", "signature is not a signature by the payer key over this trial")

    try:
        claimed = bool(await redis.set(trial_nonce_lock_key(nonce), "1", nx=True, ex=REPLAY_LOCK_TTL_SECONDS))
    except RedisError:
        logger.exception("trial replay lock unavailable")
        return _error(503, "service_unavailable", "trial replay protection is temporarily unavailable")
    if not claimed:
        return _error(409, "payment_replayed", "this trial call is already being processed")

    try:
        use = await _use_trial(session, endpoint, stored, nonce, request)
    except IntegrityError:
        await session.rollback()
        return _error(409, "payment_replayed", "this trial nonce was already used")
    except SQLAlchemyError:
        await session.rollback()
        logger.exception("trial call could not be stored")
        await _release_trial_lock(redis, nonce)
        return _error(503, "service_unavailable", "trial store is temporarily unavailable")
    if use is None:
        await _release_trial_lock(redis, nonce)
        return await failure(
            "trial_exhausted", "no free trial calls are left for this key on this endpoint today; pay instead", 0
        )
    try:
        await redis.getdel(trial_challenge_key(nonce))
    except RedisError:
        logger.warning("could not consume trial challenge %s; the unique nonce still blocks reuse", nonce)

    try:
        result = await _call_upstream(client, method, prepared)
    except UpstreamFailedError as exc:
        logger.warning("upstream failure for trial call %s: %s", use.call_id, exc)
        refunded = await refund_trial_call(session, redis, use.call_id, None)
        state = TRIAL_CALL_REFUNDED if refunded else TRIAL_CALL_USED
        return _error(
            502,
            "upstream_error",
            "the upstream API did not return a response"
            + ("; the trial call was given back" if refunded else "; the trial call is pending reconciliation"),
            headers=_trial_state_headers(use, nonce, state),
        )
    if result.status_code >= 500:
        refunded = await refund_trial_call(session, redis, use.call_id, result.status_code)
        state = TRIAL_CALL_REFUNDED if refunded else TRIAL_CALL_USED
    else:
        await _record_trial_status(session, use.call_id, result.status_code)
        state = TRIAL_CALL_USED
    response = Response(content=result.body, status_code=result.status_code)
    for name, value in result.headers:
        response.raw_headers.append((name.lower().encode("latin-1"), value.encode("latin-1")))
    for name, value in _trial_state_headers(use, nonce, state).items():
        response.headers[name] = value
    return response


async def _release_trial_lock(redis: Redis, nonce: str) -> None:
    try:
        await redis.delete(trial_nonce_lock_key(nonce))
    except RedisError:
        logger.error("could not release trial lock for %s; it expires in %ss", nonce, REPLAY_LOCK_TTL_SECONDS)
