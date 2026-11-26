"""Merchant webhooks (CONTRACT v2.12): a durable outbox, HMAC-signed deliveries, retries and the merchant routes.

Writing events (`enqueue_event`) happens inside the caller's transaction, in a SAVEPOINT, after the caller's own rows
are flushed: the event commits exactly when the payment/debit/trial/endpoint row commits, and any webhook-side
failure only rolls back the savepoint, so it can never fail or block the payment path. Nothing in here makes a
network call on the request path except the merchant's own test-fire.

Delivery (`WebhookDispatcher`) claims due rows with `FOR UPDATE SKIP LOCKED` and a lease, so several gateway
replicas can run it at once and a crashed worker's rows are picked up again after the lease. Every request is a
`POST` of the canonical JSON envelope with `X-KasGate-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, "<t>.<body>")>`;
2xx is success, anything else is retried with exponential backoff and jitter, then marked `dead` after
`webhook_max_attempts`. The merchant can redeliver any non-delivering row.

Outbound requests go through the SSRF-guarded transport (public addresses only, resolved at connect time), with no
redirects, a total timeout and a response-size cap. Secrets are Fernet-encrypted at rest and never logged.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final

import httpx
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.deps import get_db_session, get_sessionmaker
from app.models import (
    WEBHOOK_DELIVERY_DEAD,
    WEBHOOK_DELIVERY_DELIVERING,
    WEBHOOK_DELIVERY_PENDING,
    WEBHOOK_DELIVERY_SUCCEEDED,
    WEBHOOK_EVENT_TYPES,
    WEBHOOK_TEST_EVENT,
    Endpoint,
    Merchant,
    Settlement,
    Webhook,
    WebhookDelivery,
    WebhookEvent,
)
from app.schemas import (
    WEBHOOK_API_VERSION,
    WEBHOOK_DELIVERY_ID_HEADER,
    WEBHOOK_EVENT_ID_HEADER,
    WEBHOOK_EVENT_TYPE_HEADER,
    WEBHOOK_SIGNATURE_HEADER,
    WEBHOOK_TIMESTAMP_HEADER,
    WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS,
    WebhookCreate,
    WebhookCreated,
    WebhookDeliveryRead,
    WebhookDeliveryStatus,
    WebhookEventTypes,
    WebhookRead,
    WebhookUpdate,
)
from app.security import get_current_merchant, get_fernet

logger = logging.getLogger("kasgate.webhooks")

SECRET_PREFIX: Final = "whsec_"  # noqa: S105 - a secret-type prefix, not a credential
SECRET_DISPLAY_CHARS: Final = 10
MAX_WEBHOOK_BODY_BYTES: Final = 64 * 1024
MAX_WEBHOOK_RESPONSE_BYTES: Final = 64 * 1024
MAX_ERROR_CHARS: Final = 500
USER_AGENT: Final = f"KasGate-Webhooks/{WEBHOOK_API_VERSION}"
_RANDOM = secrets.SystemRandom()


# ---------------------------------------------------------------- signing


def generate_webhook_secret() -> str:
    return f"{SECRET_PREFIX}{secrets.token_urlsafe(32)}"


def encrypt_secret(secret: str) -> bytes:
    return get_fernet().encrypt(secret.encode("utf-8"))


def decrypt_secret(token: bytes) -> str:
    try:
        return get_fernet().decrypt(bytes(token)).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("webhook secret cannot be decrypted with the configured ENCRYPTION_KEY") from exc


def compute_signature(secret: str, timestamp: int, body: bytes) -> str:
    """Hex HMAC-SHA256 over `"<timestamp>.<raw body>"` keyed with the whole secret string (UTF-8)."""
    signed = str(timestamp).encode("ascii") + b"." + body
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def signature_header(secret: str, timestamp: int, body: bytes) -> str:
    return f"t={timestamp},v1={compute_signature(secret, timestamp, body)}"


def verify_signature(
    secret: str,
    header: str,
    body: bytes,
    *,
    now: int | None = None,
    tolerance_seconds: int = WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS,
) -> bool:
    """Receiver-side check (documented for merchants, used by the tests): constant-time compare of any `v1=` value
    and a timestamp within `tolerance_seconds` of now (replay protection)."""
    timestamp: int | None = None
    candidates: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t" and value.isdigit():
            timestamp = int(value)
        elif key == "v1" and value:
            candidates.append(value)
    if timestamp is None or not candidates:
        return False
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        return False
    expected = compute_signature(secret, timestamp, body)
    return any(hmac.compare_digest(expected, candidate) for candidate in candidates)


def canonical_body(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def event_id_for(event_type: str, source_id: str | None) -> str:
    """`evt_` + 32 hex. Deterministic for a (type, source) pair, so emitting the same event twice is a no-op and a
    receiver sees one id however often it is delivered; random when there is no natural source (test events)."""
    if source_id is None:
        return f"evt_{uuid.uuid4().hex}"
    return "evt_" + hashlib.sha256(f"{event_type}:{source_id}".encode()).hexdigest()[:32]


# ---------------------------------------------------------------- event payloads


def _sompi(value: int | None) -> str | None:
    return None if value is None else str(value)


def _decimal(value: object) -> str | None:
    return None if value is None else format(value, "f")


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def settlement_event_data(settlement: Settlement, endpoint_slug: str) -> dict[str, Any]:
    return {
        "settlement_id": str(settlement.id),
        "tx_id": settlement.tx_id,
        "payment_scheme": settlement.payment_scheme,
        "network": settlement.network,
        "endpoint_id": str(settlement.endpoint_id),
        "endpoint_slug": endpoint_slug,
        "payer_address": settlement.payer_address,
        "merchant_address": settlement.merchant_address,
        "amount_sompi": _sompi(settlement.amount_sompi),
        "merchant_amount_sompi": _sompi(settlement.merchant_amount_sompi),
        "fee_sompi": _sompi(settlement.fee_amount_sompi),
        "kas_usd_rate": _decimal(settlement.kas_usd_rate),
        "usd_value": _decimal(settlement.usd_value_at_settlement),
        "request_method": settlement.request_method,
        "request_path": settlement.request_path,
        "settled_at": _iso(settlement.settled_at),
    }


def endpoint_event_data(endpoint: Endpoint, changed: list[str] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "endpoint_id": str(endpoint.id),
        "slug": endpoint.slug,
        "name": endpoint.name,
        "price_currency": endpoint.price_currency.value,
        "price_sompi": _sompi(endpoint.price_sompi),
        "price_usd_micros": _sompi(endpoint.price_usd_micros),
        "allowed_methods": list(endpoint.allowed_methods),
        "is_active": endpoint.is_active,
        "is_public_listed": endpoint.is_public_listed,
        "prepaid_only": endpoint.prepaid_only,
        "free_trial_calls": endpoint.free_trial_calls,
    }
    if changed is not None:
        data["changed_fields"] = sorted(changed)
    return data


# ---------------------------------------------------------------- the outbox


async def enqueue_event(
    session: AsyncSession,
    *,
    merchant_id: uuid.UUID,
    event_type: str,
    data: Mapping[str, Any],
    source_id: str | None,
    only_webhook_id: uuid.UUID | None = None,
) -> str | None:
    """Write one event and its deliveries in a SAVEPOINT of the caller's transaction; never raises.

    The caller must have FLUSHED its own rows first (so their errors surface to the caller, not here) and commits
    afterwards. Returns the event id, or None when no active webhook subscribes to `event_type` or writing failed.
    """
    try:
        async with session.begin_nested():
            if only_webhook_id is not None:
                hooks_query = select(Webhook.id).where(
                    Webhook.id == only_webhook_id, Webhook.merchant_id == merchant_id
                )
            else:
                hooks_query = select(Webhook.id).where(
                    Webhook.merchant_id == merchant_id,
                    Webhook.is_active.is_(True),
                    Webhook.event_types.contains([event_type]),
                )
            hook_ids = list((await session.execute(hooks_query)).scalars())
            if not hook_ids:
                return None
            event_id = event_id_for(event_type, source_id)
            envelope = {
                "id": event_id,
                "type": event_type,
                "api_version": WEBHOOK_API_VERSION,
                "created_at": datetime.now(UTC).isoformat(),
                "merchant_id": str(merchant_id),
                "data": dict(data),
            }
            if len(canonical_body(envelope)) > MAX_WEBHOOK_BODY_BYTES:
                logger.error(
                    "%s event for merchant %s exceeds the webhook body limit; not queued", event_type, merchant_id
                )
                return None
            event_pk = (
                await session.execute(
                    pg_insert(WebhookEvent)
                    .values(
                        id=uuid.uuid4(),
                        event_id=event_id,
                        merchant_id=merchant_id,
                        event_type=event_type,
                        payload=envelope,
                    )
                    .on_conflict_do_nothing(index_elements=["event_id"])
                    .returning(WebhookEvent.id)
                )
            ).scalar_one_or_none()
            if event_pk is None:
                return event_id
            await session.execute(
                pg_insert(WebhookDelivery),
                [
                    {
                        "id": uuid.uuid4(),
                        "event_pk": event_pk,
                        "webhook_id": hook_id,
                        "merchant_id": merchant_id,
                        "status": WEBHOOK_DELIVERY_PENDING,
                        "attempts": 0,
                    }
                    for hook_id in hook_ids
                ],
            )
            return event_id
    except Exception:
        logger.exception("could not queue %s webhook event for merchant %s", event_type, merchant_id)
        return None


# ---------------------------------------------------------------- delivery


@dataclass(frozen=True)
class AttemptResult:
    ok: bool
    status_code: int | None
    error: str | None
    retry: bool = True


@dataclass(frozen=True)
class ClaimedDelivery:
    id: uuid.UUID
    attempts: int
    webhook_id: uuid.UUID
    url: str
    secret_encrypted: bytes
    webhook_active: bool
    event_id: str
    event_type: str
    payload: dict[str, Any]


def backoff_seconds(attempts: int, settings: Settings, jitter: float | None = None) -> float:
    """Delay before retry number `attempts + 1`: base * 2^(attempts - 1), capped, times a 0.9..1.1 jitter."""
    exponent = max(attempts - 1, 0)
    delay = min(settings.webhook_backoff_base_seconds * (2**exponent), settings.webhook_backoff_max_seconds)
    factor = 0.9 + 0.2 * (_RANDOM.random() if jitter is None else jitter)
    return float(delay * factor)


def _clip(message: str) -> str:
    return message[:MAX_ERROR_CHARS]


async def send_webhook(
    client: httpx.AsyncClient,
    *,
    url: str,
    secret: str,
    delivery_id: uuid.UUID,
    event_id: str,
    event_type: str,
    body: bytes,
    timeout_seconds: float,
    timestamp: int | None = None,
) -> AttemptResult:
    """POST one signed delivery. Never raises; never follows redirects; reads at most 64 KiB of the response."""
    stamp = int(time.time()) if timestamp is None else timestamp
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        WEBHOOK_EVENT_ID_HEADER: event_id,
        WEBHOOK_EVENT_TYPE_HEADER: event_type,
        WEBHOOK_DELIVERY_ID_HEADER: str(delivery_id),
        WEBHOOK_TIMESTAMP_HEADER: str(stamp),
        WEBHOOK_SIGNATURE_HEADER: signature_header(secret, stamp, body),
    }
    try:
        async with asyncio.timeout(timeout_seconds):
            request = client.build_request("POST", url, headers=headers, content=body)
            response = await client.send(request, stream=True, follow_redirects=False)
            try:
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_WEBHOOK_RESPONSE_BYTES:
                        break
            finally:
                await response.aclose()
    except TimeoutError:
        return AttemptResult(ok=False, status_code=None, error=f"timed out after {timeout_seconds:g}s")
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return AttemptResult(ok=False, status_code=None, error=_clip(f"{type(exc).__name__}: {exc}"))
    code = response.status_code
    if 200 <= code < 300:
        return AttemptResult(ok=True, status_code=code, error=None)
    if 300 <= code < 400:
        return AttemptResult(ok=False, status_code=code, error="redirects are not followed; use the final URL")
    return AttemptResult(ok=False, status_code=code, error=f"receiver answered HTTP {code}")


async def claim_due(
    session: AsyncSession, settings: Settings, *, delivery_id: uuid.UUID | None = None
) -> list[ClaimedDelivery]:
    """Lease due deliveries (or one given pending delivery): status delivering, attempts + 1, locked_until set."""
    now = datetime.now(UTC)
    if delivery_id is not None:
        candidates = (
            select(WebhookDelivery.id)
            .where(WebhookDelivery.id == delivery_id, WebhookDelivery.status == WEBHOOK_DELIVERY_PENDING)
            .with_for_update(skip_locked=True)
        )
    else:
        candidates = (
            select(WebhookDelivery.id)
            .where(
                or_(
                    and_(
                        WebhookDelivery.status == WEBHOOK_DELIVERY_PENDING,
                        WebhookDelivery.next_attempt_at <= now,
                    ),
                    and_(
                        WebhookDelivery.status == WEBHOOK_DELIVERY_DELIVERING,
                        WebhookDelivery.locked_until < now,
                    ),
                )
            )
            .order_by(WebhookDelivery.next_attempt_at)
            .limit(settings.webhook_batch_size)
            .with_for_update(skip_locked=True)
        )
    claimed = (
        await session.execute(
            update(WebhookDelivery)
            .where(WebhookDelivery.id.in_(candidates.scalar_subquery()))
            .values(
                status=WEBHOOK_DELIVERY_DELIVERING,
                attempts=WebhookDelivery.attempts + 1,
                locked_until=now + timedelta(seconds=settings.webhook_lease_seconds),
                last_attempt_at=now,
            )
            .returning(
                WebhookDelivery.id, WebhookDelivery.attempts, WebhookDelivery.webhook_id, WebhookDelivery.event_pk
            )
        )
    ).all()
    if not claimed:
        await session.commit()
        return []
    event_pks = {row.event_pk for row in claimed}
    hook_ids = {row.webhook_id for row in claimed}
    events = {
        row.id: row
        for row in (await session.execute(select(WebhookEvent).where(WebhookEvent.id.in_(event_pks)))).scalars()
    }
    hooks = {row.id: row for row in (await session.execute(select(Webhook).where(Webhook.id.in_(hook_ids)))).scalars()}
    await session.commit()
    result: list[ClaimedDelivery] = []
    for row in claimed:
        event = events[row.event_pk]
        hook = hooks[row.webhook_id]
        result.append(
            ClaimedDelivery(
                id=row.id,
                attempts=row.attempts,
                webhook_id=hook.id,
                url=hook.url,
                secret_encrypted=bytes(hook.secret_encrypted),
                webhook_active=hook.is_active or event.event_type == WEBHOOK_TEST_EVENT,
                event_id=event.event_id,
                event_type=event.event_type,
                payload=dict(event.payload),
            )
        )
    return result


async def record_result(
    session: AsyncSession, claimed: ClaimedDelivery, result: AttemptResult, settings: Settings
) -> str:
    """Store the outcome of one attempt; only the lease holder (same attempts count, still delivering) may write."""
    now = datetime.now(UTC)
    values: dict[str, Any] = {"last_status_code": result.status_code, "locked_until": None}
    if result.ok:
        values.update(status=WEBHOOK_DELIVERY_SUCCEEDED, delivered_at=now, last_error=None)
    elif not result.retry or claimed.attempts >= settings.webhook_max_attempts:
        values.update(status=WEBHOOK_DELIVERY_DEAD, last_error=result.error)
    else:
        values.update(
            status=WEBHOOK_DELIVERY_PENDING,
            last_error=result.error,
            next_attempt_at=now + timedelta(seconds=backoff_seconds(claimed.attempts, settings)),
        )
    await session.execute(
        update(WebhookDelivery)
        .where(
            WebhookDelivery.id == claimed.id,
            WebhookDelivery.status == WEBHOOK_DELIVERY_DELIVERING,
            WebhookDelivery.attempts == claimed.attempts,
        )
        .values(**values)
    )
    await session.commit()
    return str(values["status"])


async def attempt(client: httpx.AsyncClient, claimed: ClaimedDelivery, settings: Settings) -> AttemptResult:
    if not claimed.webhook_active:
        return AttemptResult(ok=False, status_code=None, error="webhook is disabled", retry=False)
    try:
        secret = decrypt_secret(claimed.secret_encrypted)
    except ValueError as exc:
        return AttemptResult(ok=False, status_code=None, error=str(exc), retry=False)
    return await send_webhook(
        client,
        url=claimed.url,
        secret=secret,
        delivery_id=claimed.id,
        event_id=claimed.event_id,
        event_type=claimed.event_type,
        body=canonical_body(claimed.payload),
        timeout_seconds=settings.webhook_timeout_seconds,
    )


async def deliver_due(maker: async_sessionmaker[AsyncSession], client: httpx.AsyncClient) -> int:
    """One dispatcher pass: claim a batch, deliver it with bounded concurrency, record every result."""
    settings = get_settings()
    async with maker() as session:
        batch = await claim_due(session, settings)
    if not batch:
        return 0
    gate = asyncio.Semaphore(settings.webhook_concurrency)

    async def run(item: ClaimedDelivery) -> None:
        async with gate:
            result = await attempt(client, item, settings)
        try:
            async with maker() as session:
                await record_result(session, item, result, settings)
        except Exception:
            logger.exception("could not record webhook delivery %s; its lease will expire and it is retried", item.id)

    await asyncio.gather(*(run(item) for item in batch))
    return len(batch)


async def deliver_one_now(
    maker: async_sessionmaker[AsyncSession], client: httpx.AsyncClient, delivery_id: uuid.UUID
) -> None:
    """Claim and attempt one pending delivery immediately (test-fire). Its retries follow the normal schedule."""
    settings = get_settings()
    async with maker() as session:
        batch = await claim_due(session, settings, delivery_id=delivery_id)
    for item in batch:
        result = await attempt(client, item, settings)
        async with maker() as session:
            await record_result(session, item, result, settings)


class WebhookDispatcher:
    """Background outbox worker started by the app lifespan."""

    def __init__(self, maker: async_sessionmaker[AsyncSession], client: httpx.AsyncClient) -> None:
        self._maker = maker
        self._client = client
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    def wake(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="kasgate-webhook-dispatcher")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                logger.info("webhook dispatcher stopped")
            self._task = None

    async def _run(self) -> None:
        interval = get_settings().webhook_poll_interval_seconds
        while not self._stopping:
            try:
                handled = await deliver_due(self._maker, self._client)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("webhook dispatcher pass failed")
                handled = 0
            if handled:
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except TimeoutError:
                continue


def wake_dispatcher(request: Request) -> None:
    dispatcher = getattr(request.app.state, "webhook_dispatcher", None)
    if isinstance(dispatcher, WebhookDispatcher):
        dispatcher.wake()


def get_webhook_client(request: Request) -> httpx.AsyncClient:
    client: httpx.AsyncClient = request.app.state.webhook_client
    return client


# ---------------------------------------------------------------- merchant routes

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])
CurrentMerchant = Annotated[Merchant, Depends(get_current_merchant)]
DbSession = Annotated[AsyncSession, Depends(get_db_session)]


async def _owned_webhook(session: AsyncSession, merchant: Merchant, webhook_id: uuid.UUID) -> Webhook:
    hook = (
        await session.execute(select(Webhook).where(Webhook.id == webhook_id, Webhook.merchant_id == merchant.id))
    ).scalar_one_or_none()
    if hook is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="webhook not found")
    return hook


def _secret_prefix(secret: str) -> str:
    return secret[:SECRET_DISPLAY_CHARS]


def _delivery_read(delivery: WebhookDelivery, event: WebhookEvent) -> WebhookDeliveryRead:
    pending = delivery.status in (WEBHOOK_DELIVERY_PENDING, WEBHOOK_DELIVERY_DELIVERING)
    return WebhookDeliveryRead(
        id=delivery.id,
        webhook_id=delivery.webhook_id,
        event_id=event.event_id,
        event_type=event.event_type,
        status=delivery.status,
        attempts=delivery.attempts,
        next_attempt_at=delivery.next_attempt_at if pending else None,
        last_attempt_at=delivery.last_attempt_at,
        last_status_code=delivery.last_status_code,
        last_error=delivery.last_error,
        delivered_at=delivery.delivered_at,
        created_at=delivery.created_at,
    )


async def _read_delivery(session: AsyncSession, delivery_id: uuid.UUID) -> WebhookDeliveryRead:
    row = (
        await session.execute(
            select(WebhookDelivery, WebhookEvent)
            .join(WebhookEvent, WebhookEvent.id == WebhookDelivery.event_pk)
            .where(WebhookDelivery.id == delivery_id)
            .execution_options(populate_existing=True)
        )
    ).one()
    return _delivery_read(row[0], row[1])


@router.get("/event-types", response_model=WebhookEventTypes)
async def event_types(merchant: CurrentMerchant) -> WebhookEventTypes:
    return WebhookEventTypes(event_types=list(WEBHOOK_EVENT_TYPES))


@router.post("", status_code=status.HTTP_201_CREATED, response_model=WebhookCreated)
async def create_webhook(payload: WebhookCreate, merchant: CurrentMerchant, session: DbSession) -> WebhookCreated:
    settings = get_settings()
    count = (
        await session.execute(select(func.count()).select_from(Webhook).where(Webhook.merchant_id == merchant.id))
    ).scalar_one()
    if count >= settings.webhook_max_per_merchant:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a merchant can register at most {settings.webhook_max_per_merchant} webhooks",
        )
    secret = generate_webhook_secret()
    hook = Webhook(
        merchant_id=merchant.id,
        url=payload.url,
        description=payload.description,
        event_types=list(payload.event_types),
        secret_encrypted=encrypt_secret(secret),
        secret_prefix=_secret_prefix(secret),
        is_active=True,
    )
    session.add(hook)
    await session.commit()
    await session.refresh(hook)
    return WebhookCreated(webhook=WebhookRead.model_validate(hook), secret=secret)


@router.get("", response_model=list[WebhookRead])
async def list_webhooks(merchant: CurrentMerchant, session: DbSession) -> list[WebhookRead]:
    rows = (
        await session.execute(
            select(Webhook).where(Webhook.merchant_id == merchant.id).order_by(Webhook.created_at, Webhook.id)
        )
    ).scalars()
    return [WebhookRead.model_validate(row) for row in rows]


@router.get("/{webhook_id}", response_model=WebhookRead)
async def read_webhook(webhook_id: uuid.UUID, merchant: CurrentMerchant, session: DbSession) -> WebhookRead:
    return WebhookRead.model_validate(await _owned_webhook(session, merchant, webhook_id))


@router.patch("/{webhook_id}", response_model=WebhookRead)
async def update_webhook(
    webhook_id: uuid.UUID, payload: WebhookUpdate, merchant: CurrentMerchant, session: DbSession
) -> WebhookRead:
    hook = await _owned_webhook(session, merchant, webhook_id)
    changes = payload.model_dump(exclude_unset=True)
    nulled = sorted(name for name in ("url", "event_types", "is_active") if name in changes and changes[name] is None)
    if nulled:
        raise HTTPException(status_code=422, detail=f"fields cannot be null: {', '.join(nulled)}")
    for field, value in changes.items():
        setattr(hook, field, value)
    await session.commit()
    await session.refresh(hook)
    return WebhookRead.model_validate(hook)


@router.delete("/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(webhook_id: uuid.UUID, merchant: CurrentMerchant, session: DbSession) -> Response:
    hook = await _owned_webhook(session, merchant, webhook_id)
    await session.delete(hook)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{webhook_id}/rotate-secret", response_model=WebhookCreated)
async def rotate_secret(webhook_id: uuid.UUID, merchant: CurrentMerchant, session: DbSession) -> WebhookCreated:
    hook = await _owned_webhook(session, merchant, webhook_id)
    secret = generate_webhook_secret()
    hook.secret_encrypted = encrypt_secret(secret)
    hook.secret_prefix = _secret_prefix(secret)
    await session.commit()
    await session.refresh(hook)
    return WebhookCreated(webhook=WebhookRead.model_validate(hook), secret=secret)


@router.post("/{webhook_id}/test", response_model=WebhookDeliveryRead)
async def test_webhook(
    webhook_id: uuid.UUID,
    merchant: CurrentMerchant,
    session: DbSession,
    maker: Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)],
    client: Annotated[httpx.AsyncClient, Depends(get_webhook_client)],
) -> WebhookDeliveryRead:
    """Queue a `webhook.test` event for this webhook only and attempt it once right away."""
    hook = await _owned_webhook(session, merchant, webhook_id)
    event_id = await enqueue_event(
        session,
        merchant_id=merchant.id,
        event_type=WEBHOOK_TEST_EVENT,
        data={"webhook_id": str(hook.id), "message": "KasGate test event"},
        source_id=None,
        only_webhook_id=hook.id,
    )
    if event_id is None:
        await session.rollback()
        raise HTTPException(status_code=503, detail="could not queue the test event", headers={"Retry-After": "5"})
    delivery_id = (
        await session.execute(
            select(WebhookDelivery.id)
            .join(WebhookEvent, WebhookEvent.id == WebhookDelivery.event_pk)
            .where(WebhookEvent.event_id == event_id, WebhookDelivery.webhook_id == hook.id)
        )
    ).scalar_one()
    await session.commit()
    await deliver_one_now(maker, client, delivery_id)
    return await _read_delivery(session, delivery_id)


@router.get("/{webhook_id}/deliveries", response_model=list[WebhookDeliveryRead])
async def list_deliveries(
    webhook_id: uuid.UUID,
    merchant: CurrentMerchant,
    session: DbSession,
    status_filter: Annotated[WebhookDeliveryStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[WebhookDeliveryRead]:
    hook = await _owned_webhook(session, merchant, webhook_id)
    query = (
        select(WebhookDelivery, WebhookEvent)
        .join(WebhookEvent, WebhookEvent.id == WebhookDelivery.event_pk)
        .where(WebhookDelivery.webhook_id == hook.id)
    )
    if status_filter is not None:
        query = query.where(WebhookDelivery.status == status_filter)
    rows = (
        await session.execute(
            query.order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id).limit(limit).offset(offset)
        )
    ).all()
    return [_delivery_read(delivery, event) for delivery, event in rows]


@router.post(
    "/{webhook_id}/deliveries/{delivery_id}/redeliver",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WebhookDeliveryRead,
)
async def redeliver(
    webhook_id: uuid.UUID,
    delivery_id: uuid.UUID,
    request: Request,
    merchant: CurrentMerchant,
    session: DbSession,
) -> WebhookDeliveryRead:
    """Put a delivery back in the queue, due now, with a fresh attempt budget. Same event id and body."""
    hook = await _owned_webhook(session, merchant, webhook_id)
    current = (
        await session.execute(
            select(WebhookDelivery.status).where(
                WebhookDelivery.id == delivery_id, WebhookDelivery.webhook_id == hook.id
            )
        )
    ).scalar_one_or_none()
    if current is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="delivery not found")
    reset = (
        await session.execute(
            update(WebhookDelivery)
            .where(
                WebhookDelivery.id == delivery_id,
                WebhookDelivery.webhook_id == hook.id,
                WebhookDelivery.status != WEBHOOK_DELIVERY_DELIVERING,
            )
            .values(
                status=WEBHOOK_DELIVERY_PENDING,
                attempts=0,
                next_attempt_at=func.now(),
                locked_until=None,
                delivered_at=None,
            )
            .returning(WebhookDelivery.id)
        )
    ).scalar_one_or_none()
    await session.commit()
    if reset is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="this delivery is being attempted right now")
    wake_dispatcher(request)
    return await _read_delivery(session, delivery_id)
