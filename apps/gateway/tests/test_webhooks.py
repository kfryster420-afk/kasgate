"""CONTRACT v2.12 merchant webhooks: signed deliveries, durable outbox, retries, dead letters, SSRF guard."""

import asyncio
import json
import logging
import socket
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import security, webhooks
from app.config import get_settings
from app.models import Settlement, Webhook, WebhookDelivery, WebhookEvent
from app.schemas import (
    WEBHOOK_DELIVERY_ID_HEADER,
    WEBHOOK_EVENT_ID_HEADER,
    WEBHOOK_EVENT_TYPE_HEADER,
    WEBHOOK_SIGNATURE_HEADER,
    WEBHOOK_TIMESTAMP_HEADER,
    WebhookCreated,
    WebhookDeliveryRead,
    validate_webhook_url,
)
from app.security import build_webhook_client
from app.webhooks import (
    WebhookDispatcher,
    WebhookSecrets,
    backoff_seconds,
    claim_due,
    compute_signature,
    decrypt_secret,
    decrypt_secrets,
    deliver_due,
    encrypt_secret,
    encrypt_secrets,
    enqueue_event,
    record_result,
    send_webhook,
    signature_header,
    verify_signature,
)
from tests.conftest import (
    OTHER_ADDRESS,
    Seed,
    WebhookReceiver,
    bearer,
    paid_headers,
    seed_endpoint,
)

HOOK_URL = "https://hooks.example.com/kasgate"

# ---------------------------------------------------------------- helpers


async def create_hook(
    client: httpx.AsyncClient, seed: Seed, event_types: list[str] | None = None, url: str = HOOK_URL
) -> WebhookCreated:
    response = await client.post(
        "/v1/webhooks",
        json={"url": url, "event_types": event_types or ["payment.settled"], "description": "orders"},
        headers=bearer(seed.api_key),
    )
    assert response.status_code == 201, response.text
    return WebhookCreated.model_validate(response.json())


async def deliveries(maker: async_sessionmaker[AsyncSession]) -> list[WebhookDelivery]:
    async with maker() as session:
        return list((await session.execute(select(WebhookDelivery).order_by(WebhookDelivery.created_at))).scalars())


async def events(maker: async_sessionmaker[AsyncSession]) -> list[WebhookEvent]:
    async with maker() as session:
        return list((await session.execute(select(WebhookEvent).order_by(WebhookEvent.created_at))).scalars())


async def make_due(maker: async_sessionmaker[AsyncSession]) -> None:
    async with maker() as session:
        await session.execute(
            update(WebhookDelivery)
            .where(WebhookDelivery.status == "pending")
            .values(next_attempt_at=func.now() - timedelta(seconds=1))
        )
        await session.commit()


def webhook_client_for(receiver: WebhookReceiver) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(receiver), follow_redirects=False)


async def pay(client: httpx.AsyncClient) -> httpx.Response:
    response = await client.get("/p/weather", headers=await paid_headers(client))
    assert response.status_code == 200, response.text
    return response


# ---------------------------------------------------------------- signatures


def test_signature_round_trip_and_forgeries() -> None:
    secret = "whsec_" + "a" * 43
    body = b'{"id":"evt_1","type":"payment.settled"}'
    now = 1_800_000_000
    header = signature_header(secret, now, body)
    assert header == f"t={now},v1={compute_signature(secret, now, body)}"
    assert verify_signature(secret, header, body, now=now)
    assert verify_signature(secret, header, body, now=now + 300)
    # stale or future timestamps (replay protection)
    assert not verify_signature(secret, header, body, now=now + 301)
    assert not verify_signature(secret, header, body, now=now - 301)
    # tampered body, wrong secret, re-stamped header, truncated / garbage headers
    assert not verify_signature(secret, header, body + b" ", now=now)
    assert not verify_signature("whsec_" + "b" * 43, header, body, now=now)
    restamped = header.replace(f"t={now}", f"t={now + 10}")
    assert not verify_signature(secret, restamped, body, now=now + 10)
    for bad in ("", "t=abc,v1=00", f"t={now}", "v1=" + compute_signature(secret, now, body), "garbage"):
        assert not verify_signature(secret, bad, body, now=now)
    # several v1 values (secret rotation on the receiver side) pass when one matches
    assert verify_signature(secret, f"t={now},v1={'0' * 64},v1={compute_signature(secret, now, body)}", body, now=now)



@pytest.mark.parametrize(
    "timestamp",
    ["\u00b2", "1\u00b2", "\u0661\u0662", "\uff11", "9" * 5000, "+1", " 1", "-1"],
)
def test_signature_header_with_a_non_ascii_or_absurd_timestamp_is_rejected_not_crashed(timestamp: str) -> None:
    """str.isdigit() is True for '²', Arabic-Indic and full-width digits; int() then raised (or hit the 4300-digit
    limit). Each must simply fail verification."""
    secret = "whsec_" + "a" * 43
    body = b"{}"
    assert verify_signature(secret, f"t={timestamp},v1={'0' * 64}", body, now=1_800_000_000) is False


def test_signature_header_with_a_non_ascii_v1_is_rejected_not_crashed() -> None:
    secret = "whsec_" + "a" * 43
    now = 1_800_000_000
    assert verify_signature(secret, f"t={now},v1=\u00e9\u00e9", b"{}", now=now) is False

# ---------------------------------------------------------------- CRUD + secret handling


async def test_create_shows_secret_once_and_stores_it_encrypted(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    created = await create_hook(client, seeded, ["payment.settled", "trial.used", "payment.settled"])
    assert created.secret.startswith("whsec_")
    assert len(created.secret) >= 40
    assert created.webhook.secret_prefix == created.secret[:10]
    assert created.webhook.event_types == ["payment.settled", "trial.used"]
    listed = await client.get("/v1/webhooks", headers=bearer(seeded.api_key))
    assert created.secret not in listed.text
    one = await client.get(f"/v1/webhooks/{created.webhook.id}", headers=bearer(seeded.api_key))
    assert created.secret not in one.text and "secret_encrypted" not in one.text
    async with sessionmaker() as session:
        hook = (await session.execute(select(Webhook))).scalar_one()
    assert created.secret.encode() not in bytes(hook.secret_encrypted)
    assert decrypt_secret(bytes(hook.secret_encrypted)) == created.secret
    # rotation shows a new secret once
    rotated = await client.post(f"/v1/webhooks/{created.webhook.id}/rotate-secret", headers=bearer(seeded.api_key))
    assert rotated.status_code == 200
    assert rotated.json()["secret"] != created.secret


async def test_rotation_keeps_the_previous_secret_signing_for_the_overlap(
    client: httpx.AsyncClient,
    seeded: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    webhook_receiver: WebhookReceiver,
) -> None:
    created = await create_hook(client, seeded, ["trial.used"])
    before = datetime.now(UTC)
    response = await client.post(f"/v1/webhooks/{created.webhook.id}/rotate-secret", headers=bearer(seeded.api_key))
    assert response.status_code == 200, response.text
    rotated = WebhookCreated.model_validate(response.json())
    assert rotated.previous_secret_expires_at is not None
    assert abs((rotated.previous_secret_expires_at - before).total_seconds() - 86_400) < 5
    assert rotated.webhook.secret_prefix == rotated.secret[:10]
    async with sessionmaker() as session:
        stored = bytes((await session.execute(select(Webhook.secret_encrypted))).scalar_one())
    assert decrypt_secret(stored) == rotated.secret
    assert created.secret.encode() not in stored and rotated.secret.encode() not in stored

    fired = await client.post(f"/v1/webhooks/{created.webhook.id}/test", headers=bearer(seeded.api_key))
    assert fired.json()["status"] == "succeeded"
    [request] = webhook_receiver.requests
    header = request.headers[WEBHOOK_SIGNATURE_HEADER]
    stamp = int(request.headers[WEBHOOK_TIMESTAMP_HEADER])
    assert header == (
        f"t={stamp},v1={compute_signature(rotated.secret, stamp, request.content)}"
        f",v1={compute_signature(created.secret, stamp, request.content)}"
    )
    assert verify_signature(rotated.secret, header, request.content)
    assert verify_signature(created.secret, header, request.content)  # receivers not yet switched keep working

    again = WebhookCreated.model_validate(
        (await client.post(f"/v1/webhooks/{created.webhook.id}/rotate-secret", headers=bearer(seeded.api_key))).json()
    )
    await client.post(f"/v1/webhooks/{created.webhook.id}/test", headers=bearer(seeded.api_key))
    header = webhook_receiver.requests[-1].headers[WEBHOOK_SIGNATURE_HEADER]
    body = webhook_receiver.requests[-1].content
    assert header.count("v1=") == 2
    assert verify_signature(again.secret, header, body) and verify_signature(rotated.secret, header, body)
    assert not verify_signature(created.secret, header, body)  # only the most recently replaced secret overlaps


async def test_rotation_with_no_overlap_signs_with_the_new_secret_only(
    client: httpx.AsyncClient, seeded: Seed, webhook_receiver: WebhookReceiver, override_settings: Any
) -> None:
    override_settings(webhook_secret_overlap_seconds=0)
    created = await create_hook(client, seeded, ["trial.used"])
    rotated = (await client.post(f"/v1/webhooks/{created.webhook.id}/rotate-secret", headers=bearer(seeded.api_key)))
    assert rotated.json()["previous_secret_expires_at"] is None
    await client.post(f"/v1/webhooks/{created.webhook.id}/test", headers=bearer(seeded.api_key))
    [request] = webhook_receiver.requests
    header = request.headers[WEBHOOK_SIGNATURE_HEADER]
    assert header.count("v1=") == 1
    assert verify_signature(rotated.json()["secret"], header, request.content)
    assert not verify_signature(created.secret, header, request.content)


def test_previous_secret_stops_signing_when_the_overlap_ends() -> None:
    stored = WebhookSecrets(current="whsec_new", previous="whsec_old", previous_until=1_800_086_400)
    assert stored.signing_secrets(1_800_086_399) == ["whsec_new", "whsec_old"]
    assert stored.signing_secrets(1_800_086_400) == ["whsec_new"]
    assert decrypt_secrets(encrypt_secrets(stored)) == stored
    assert decrypt_secrets(encrypt_secret("whsec_bare")) == WebhookSecrets(current="whsec_bare")
    assert signature_header("whsec_new", 5, b"{}", also=["whsec_old"]) == (
        f"t=5,v1={compute_signature('whsec_new', 5, b'{}')},v1={compute_signature('whsec_old', 5, b'{}')}"
    )

async def test_webhook_crud_is_merchant_scoped(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    created = await create_hook(client, seeded)
    other = await seed_endpoint(sessionmaker, slug="other", address=OTHER_ADDRESS)
    path = f"/v1/webhooks/{created.webhook.id}"
    for method in ("GET", "PATCH", "DELETE"):
        response = await client.request(
            method, path, headers=bearer(other.api_key), json={} if method == "PATCH" else None
        )
        assert response.status_code == 404, method
    assert (await client.get("/v1/webhooks")).status_code == 401
    patched = await client.patch(
        path, json={"is_active": False, "event_types": ["prepaid.deposit"]}, headers=bearer(seeded.api_key)
    )
    assert patched.status_code == 200
    assert (patched.json()["is_active"], patched.json()["event_types"]) == (False, ["prepaid.deposit"])
    assert (await client.patch(path, json={"url": None}, headers=bearer(seeded.api_key))).status_code == 422
    assert (await client.patch(path, json={"nope": 1}, headers=bearer(seeded.api_key))).status_code == 422
    assert (await client.delete(path, headers=bearer(seeded.api_key))).status_code == 204
    assert (await client.get(path, headers=bearer(seeded.api_key))).status_code == 404


async def test_event_types_and_strict_payloads(client: httpx.AsyncClient, seeded: Seed) -> None:
    types = await client.get("/v1/webhooks/event-types", headers=bearer(seeded.api_key))
    assert set(types.json()["event_types"]) == {
        "payment.settled",
        "prepaid.deposit",
        "prepaid.debit",
        "trial.used",
        "endpoint.created",
        "endpoint.updated",
    }
    for body in (
        {"url": HOOK_URL, "event_types": []},
        {"url": HOOK_URL, "event_types": ["payment.refunded"]},
        {"url": HOOK_URL, "event_types": ["webhook.test"]},
        {"url": HOOK_URL, "event_types": "payment.settled"},
        {"url": HOOK_URL, "event_types": ["payment.settled"], "secret": "mine"},
    ):
        response = await client.post("/v1/webhooks", json=body, headers=bearer(seeded.api_key))
        assert response.status_code == 422, body


async def test_webhook_count_is_capped(client: httpx.AsyncClient, seeded: Seed, override_settings: Any) -> None:
    override_settings(webhook_max_per_merchant=2)
    await create_hook(client, seeded)
    await create_hook(client, seeded)
    third = await client.post(
        "/v1/webhooks", json={"url": HOOK_URL, "event_types": ["trial.used"]}, headers=bearer(seeded.api_key)
    )
    assert third.status_code == 409


# ---------------------------------------------------------------- SSRF


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/x",
        "ftp://hooks.example.com/x",
        "https://localhost/x",
        "https://api.localhost/x",
        "https://printer.local/x",
        "https://intranet/x",
        "https://127.0.0.1/x",
        "https://10.1.2.3/x",
        "https://192.168.1.10/x",
        "https://172.16.0.1/x",
        "https://169.254.169.254/latest/meta-data",
        "https://100.64.0.1/x",
        "https://0.0.0.0/x",
        "https://[::1]/x",
        "https://[fd00::1]/x",
        "https://[fe80::1]/x",
        "https://[::ffff:127.0.0.1]/x",
        "https://[::7f00:1]/x",  # IPv4-compatible 127.0.0.1
        "https://[::a9fe:a9fe]/x",  # IPv4-compatible 169.254.169.254 (cloud metadata)
        "https://[fec0::1]/x",  # site-local
        "https://[64:ff9b::a00:1]/x",  # NAT64 of 10.0.0.1
        "https://[2002:7f00:1::]/x",  # 6to4 of 127.0.0.1
        "https://user:pw@hooks.example.com/x",
        "https://hooks.example.com/x#frag",
        "https://hooks.example.com/a b",
        "https://hooks.example.com/" + "a" * 2100,
    ],
)
def test_webhook_url_validation_rejects(url: str) -> None:
    with pytest.raises(ValueError):
        validate_webhook_url(url)


def test_webhook_url_validation_accepts_public_https() -> None:
    assert validate_webhook_url("https://hooks.example.com/kasgate?x=1") == "https://hooks.example.com/kasgate?x=1"
    assert validate_webhook_url("https://93.184.216.34:8443/hook").startswith("https://93.184.216.34:8443")


async def test_create_route_rejects_private_urls(client: httpx.AsyncClient, seeded: Seed) -> None:
    for url in ("http://hooks.example.com/x", "https://169.254.169.254/", "https://10.0.0.1/hook"):
        response = await client.post(
            "/v1/webhooks", json={"url": url, "event_types": ["payment.settled"]}, headers=bearer(seeded.api_key)
        )
        assert response.status_code == 422, url


def fake_dns(monkeypatch: pytest.MonkeyPatch, table: dict[str, list[str]]) -> list[str]:
    lookups: list[str] = []

    async def getaddrinfo(host: str, port: int) -> list[tuple[Any, Any, Any, Any, tuple[Any, ...]]]:
        lookups.append(host)
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "unknown host")
        return [
            (
                socket.AF_INET6 if ":" in address else socket.AF_INET,
                socket.SOCK_STREAM,
                6,
                "",
                (address, port, 0, 0) if ":" in address else (address, port),
            )
            for address in table[host]
        ]

    monkeypatch.setattr(security, "_getaddrinfo", getaddrinfo)
    return lookups


@pytest.mark.parametrize(
    "answer", ["127.0.0.1", "10.0.0.7", "169.254.169.254", "100.64.0.9", "::1", "fd00::1", "::ffff:192.168.0.1"]
)
async def test_delivery_refuses_names_resolving_to_private_addresses(
    monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    lookups = fake_dns(monkeypatch, {"evil.example.com": [answer]})
    async with build_webhook_client(5) as client:
        result = await send_webhook(
            client,
            url="https://evil.example.com/hook",
            secret="whsec_x",
            delivery_id=uuid.uuid4(),
            event_id="evt_" + "0" * 32,
            event_type="payment.settled",
            body=b"{}",
            timeout_seconds=5,
        )
    assert lookups == ["evil.example.com"]
    assert not result.ok
    assert result.status_code is None
    assert result.error is not None and "non-public" in result.error


async def test_delivery_refuses_mixed_public_and_private_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_dns(monkeypatch, {"rebind.example.com": ["93.184.216.34", "127.0.0.1"]})
    async with build_webhook_client(5) as client:
        result = await send_webhook(
            client,
            url="https://rebind.example.com/hook",
            secret="whsec_x",
            delivery_id=uuid.uuid4(),
            event_id="evt_" + "0" * 32,
            event_type="payment.settled",
            body=b"{}",
            timeout_seconds=5,
        )
    assert not result.ok and result.error is not None and "non-public" in result.error


def test_webhook_client_never_follows_redirects_or_uses_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    client = build_webhook_client(5)
    assert client.follow_redirects is False
    assert client.trust_env is False
    assert isinstance(client._transport, security.GuardedTransport)


async def test_redirects_timeouts_and_huge_responses_are_failures() -> None:
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://169.254.169.254/"})

    async with webhook_client_for(WebhookReceiver(handler=redirect)) as client:
        result = await send_webhook(
            client,
            url=HOOK_URL,
            secret="s",
            delivery_id=uuid.uuid4(),
            event_id="e",
            event_type="t",
            body=b"{}",
            timeout_seconds=5,
        )
    assert (result.ok, result.status_code) == (False, 302)

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with webhook_client_for(WebhookReceiver(handler=slow)) as client:
        result = await send_webhook(
            client,
            url=HOOK_URL,
            secret="s",
            delivery_id=uuid.uuid4(),
            event_id="e",
            event_type="t",
            body=b"{}",
            timeout_seconds=5,
        )
    assert not result.ok and result.status_code is None and "ReadTimeout" in (result.error or "")

    big = WebhookReceiver(handler=lambda request: httpx.Response(200, content=b"x" * (1024 * 1024)))
    async with webhook_client_for(big) as client:
        result = await send_webhook(
            client,
            url=HOOK_URL,
            secret="s",
            delivery_id=uuid.uuid4(),
            event_id="e",
            event_type="t",
            body=b"{}",
            timeout_seconds=5,
        )
    assert result.ok


async def test_send_webhook_enforces_its_own_deadline() -> None:
    class Hang(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(10)
            return httpx.Response(200)

    async with httpx.AsyncClient(transport=Hang()) as client:
        started = time.monotonic()
        result = await send_webhook(
            client,
            url=HOOK_URL,
            secret="s",
            delivery_id=uuid.uuid4(),
            event_id="e",
            event_type="t",
            body=b"{}",
            timeout_seconds=0.2,
        )
    assert time.monotonic() - started < 2
    assert not result.ok and "timed out" in (result.error or "")


# ---------------------------------------------------------------- events + delivery


async def test_payment_settled_is_delivered_signed(
    client: httpx.AsyncClient,
    seeded: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    webhook_receiver: WebhookReceiver,
) -> None:
    created = await create_hook(client, seeded)
    await pay(client)
    [event] = await events(sessionmaker)
    assert event.event_type == "payment.settled"
    [delivery] = await deliveries(sessionmaker)
    assert (delivery.status, delivery.attempts) == ("pending", 0)

    async with webhook_client_for(webhook_receiver) as hook_client:
        assert await deliver_due(sessionmaker, hook_client) == 1
    [request] = webhook_receiver.requests
    assert str(request.url) == HOOK_URL
    body = request.content
    assert verify_signature(created.secret, request.headers[WEBHOOK_SIGNATURE_HEADER], body)
    assert request.headers[WEBHOOK_SIGNATURE_HEADER].startswith(f"t={request.headers[WEBHOOK_TIMESTAMP_HEADER]},v1=")
    envelope = json.loads(body)
    assert envelope["id"] == event.event_id == request.headers[WEBHOOK_EVENT_ID_HEADER]
    assert envelope["type"] == "payment.settled" == request.headers[WEBHOOK_EVENT_TYPE_HEADER]
    assert envelope["api_version"] == "2.12"
    assert request.headers[WEBHOOK_DELIVERY_ID_HEADER] == str(delivery.id)
    async with sessionmaker() as session:
        settlement = (await session.execute(select(Settlement))).scalar_one()
    data = envelope["data"]
    assert data["tx_id"] == settlement.tx_id
    assert data["amount_sompi"] == str(settlement.amount_sompi)
    assert data["fee_sompi"] == str(settlement.fee_amount_sompi)
    assert data["endpoint_slug"] == "weather"
    [done] = await deliveries(sessionmaker)
    assert (done.status, done.attempts, done.last_status_code) == ("succeeded", 1, 200)
    assert done.delivered_at is not None and done.locked_until is None
    # nothing else is due
    async with webhook_client_for(webhook_receiver) as hook_client:
        assert await deliver_due(sessionmaker, hook_client) == 0


async def test_unsubscribed_and_disabled_webhooks_get_nothing(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await create_hook(client, seeded, ["trial.used"])
    disabled = await create_hook(client, seeded, ["payment.settled"])
    await client.patch(f"/v1/webhooks/{disabled.webhook.id}", json={"is_active": False}, headers=bearer(seeded.api_key))
    await pay(client)
    assert await events(sessionmaker) == []
    assert await deliveries(sessionmaker) == []


async def test_disabling_a_webhook_dead_letters_its_pending_deliveries(
    client: httpx.AsyncClient,
    seeded: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    webhook_receiver: WebhookReceiver,
) -> None:
    created = await create_hook(client, seeded)
    await pay(client)
    await client.patch(f"/v1/webhooks/{created.webhook.id}", json={"is_active": False}, headers=bearer(seeded.api_key))
    async with webhook_client_for(webhook_receiver) as hook_client:
        await deliver_due(sessionmaker, hook_client)
    [delivery] = await deliveries(sessionmaker)
    assert (delivery.status, delivery.last_error) == ("dead", "webhook is disabled")
    assert webhook_receiver.requests == []


async def test_endpoint_and_prepaid_events(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await create_hook(client, seeded, ["endpoint.created", "endpoint.updated"])
    created = await client.post(
        "/v1/endpoints",
        json={
            "slug": "newapi",
            "name": "New API",
            "upstream_url": "https://api.upstream.test/n",
            "price_sompi": 100_000_000,
            "allowed_methods": ["GET"],
            "free_trial_calls": 3,
        },
        headers=bearer(seeded.api_key),
    )
    assert created.status_code == 201, created.text
    endpoint_id = created.json()["id"]
    patched = await client.patch(
        f"/v1/endpoints/{endpoint_id}", json={"free_trial_calls": 5, "name": "Renamed"}, headers=bearer(seeded.api_key)
    )
    assert patched.status_code == 200
    rows = await events(sessionmaker)
    assert [row.event_type for row in rows] == ["endpoint.created", "endpoint.updated"]
    assert rows[0].payload["data"]["free_trial_calls"] == 3
    assert rows[1].payload["data"]["changed_fields"] == ["free_trial_calls", "name"]


async def test_event_ids_are_idempotent(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await create_hook(client, seeded, ["trial.used"])
    first: str | None = None
    for _ in range(2):
        async with sessionmaker() as session:
            event_id = await enqueue_event(
                session, merchant_id=seeded.merchant_id, event_type="trial.used", data={"n": 1}, source_id="call-1"
            )
            await session.commit()
        first = first or event_id
        assert event_id == first
    assert len(await events(sessionmaker)) == 1
    assert len(await deliveries(sessionmaker)) == 1


async def test_outbox_failure_never_breaks_the_payment(
    client: httpx.AsyncClient,
    seeded: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await create_hook(client, seeded)
    # an event id that violates the table's CHECK constraint: the INSERT fails inside the savepoint
    monkeypatch.setattr(webhooks, "event_id_for", lambda event_type, source_id: "not-an-event-id")
    with caplog.at_level(logging.ERROR, logger="kasgate.webhooks"):
        await pay(client)
    async with sessionmaker() as session:
        assert (await session.execute(select(func.count()).select_from(Settlement))).scalar_one() == 1
    assert await events(sessionmaker) == []
    assert "could not queue payment.settled" in caplog.text


async def test_secret_never_reaches_the_logs(
    client: httpx.AsyncClient,
    seeded: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    webhook_receiver: WebhookReceiver,
    caplog: pytest.LogCaptureFixture,
) -> None:
    webhook_receiver.status = 500
    with caplog.at_level(logging.DEBUG):
        created = await create_hook(client, seeded)
        await pay(client)
        async with webhook_client_for(webhook_receiver) as hook_client:
            await deliver_due(sessionmaker, hook_client)
        await client.post(f"/v1/webhooks/{created.webhook.id}/test", headers=bearer(seeded.api_key))
    assert created.secret not in caplog.text
    assert created.secret[10:] not in caplog.text


# ---------------------------------------------------------------- retries, backoff, dead letters


def test_backoff_is_exponential_capped_and_jittered() -> None:
    settings = get_settings()
    assert settings.webhook_backoff_base_seconds == 30
    expected = [30, 60, 120, 240, 480, 960, 1920, 3600, 3600]
    for attempts, base in enumerate(expected, start=1):
        assert backoff_seconds(attempts, settings, jitter=0.5) == pytest.approx(base)
        assert backoff_seconds(attempts, settings, jitter=0.0) == pytest.approx(base * 0.9)
        assert backoff_seconds(attempts, settings, jitter=1.0) == pytest.approx(base * 1.1)
    for _ in range(200):
        assert 27 <= backoff_seconds(1, settings) <= 33


async def test_failed_delivery_is_retried_with_backoff_then_dead_lettered(
    client: httpx.AsyncClient,
    seeded: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    webhook_receiver: WebhookReceiver,
    override_settings: Any,
) -> None:
    override_settings(webhook_max_attempts=3)
    await create_hook(client, seeded)
    await pay(client)
    webhook_receiver.status = 500
    async with webhook_client_for(webhook_receiver) as hook_client:
        before = datetime.now(UTC)
        assert await deliver_due(sessionmaker, hook_client) == 1
        [delivery] = await deliveries(sessionmaker)
        assert (delivery.status, delivery.attempts, delivery.last_status_code) == ("pending", 1, 500)
        assert delivery.last_error == "receiver answered HTTP 500"
        delay = (delivery.next_attempt_at - before).total_seconds()
        assert 27 - 1 <= delay <= 33 + 1
        # not due yet: nothing is sent
        assert await deliver_due(sessionmaker, hook_client) == 0

        await make_due(sessionmaker)
        assert await deliver_due(sessionmaker, hook_client) == 1
        [delivery] = await deliveries(sessionmaker)
        assert (delivery.status, delivery.attempts) == ("pending", 2)
        delay = (delivery.next_attempt_at - delivery.last_attempt_at).total_seconds()
        assert 54 - 1 <= delay <= 66 + 1

        await make_due(sessionmaker)
        assert await deliver_due(sessionmaker, hook_client) == 1
        [delivery] = await deliveries(sessionmaker)
        assert (delivery.status, delivery.attempts) == ("dead", 3)
        await make_due(sessionmaker)
        assert await deliver_due(sessionmaker, hook_client) == 0
    assert len(webhook_receiver.requests) == 3
    # every attempt carries the same event id (receivers dedupe on it)
    assert len({r.headers[WEBHOOK_EVENT_ID_HEADER] for r in webhook_receiver.requests}) == 1


async def test_manual_redeliver_of_a_dead_delivery(
    client: httpx.AsyncClient,
    seeded: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    webhook_receiver: WebhookReceiver,
    override_settings: Any,
) -> None:
    override_settings(webhook_max_attempts=1)
    created = await create_hook(client, seeded)
    await pay(client)
    webhook_receiver.status = 503
    async with webhook_client_for(webhook_receiver) as hook_client:
        await deliver_due(sessionmaker, hook_client)
    log = await client.get(
        f"/v1/webhooks/{created.webhook.id}/deliveries", params={"status": "dead"}, headers=bearer(seeded.api_key)
    )
    [dead] = [WebhookDeliveryRead.model_validate(row) for row in log.json()]
    assert (dead.status, dead.attempts, dead.last_status_code) == ("dead", 1, 503)
    assert dead.next_attempt_at is None

    redeliver = f"/v1/webhooks/{created.webhook.id}/deliveries/{dead.id}/redeliver"
    other = await seed_endpoint(sessionmaker, slug="other", address=OTHER_ADDRESS)
    assert (await client.post(redeliver, headers=bearer(other.api_key))).status_code == 404
    queued = await client.post(redeliver, headers=bearer(seeded.api_key))
    assert queued.status_code == 202, queued.text
    assert (queued.json()["status"], queued.json()["attempts"]) == ("pending", 0)

    webhook_receiver.status = 200
    async with webhook_client_for(webhook_receiver) as hook_client:
        assert await deliver_due(sessionmaker, hook_client) == 1
    [delivery] = await deliveries(sessionmaker)
    assert delivery.status == "succeeded"
    assert len({r.headers[WEBHOOK_EVENT_ID_HEADER] for r in webhook_receiver.requests}) == 1
    filtered = await client.get(
        f"/v1/webhooks/{created.webhook.id}/deliveries", params={"status": "bogus"}, headers=bearer(seeded.api_key)
    )
    assert filtered.status_code == 422


async def test_redeliver_refuses_a_delivery_in_flight(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    created = await create_hook(client, seeded)
    await pay(client)
    async with sessionmaker() as session:
        [claimed] = await claim_due(session, get_settings())
    response = await client.post(
        f"/v1/webhooks/{created.webhook.id}/deliveries/{claimed.id}/redeliver", headers=bearer(seeded.api_key)
    )
    assert response.status_code == 409


async def test_expired_lease_is_reclaimed_and_stale_worker_cannot_overwrite(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await create_hook(client, seeded)
    await pay(client)
    settings = get_settings()
    async with sessionmaker() as session:
        [stale] = await claim_due(session, settings)
    async with sessionmaker() as session:
        assert await claim_due(session, settings) == []  # leased
        await session.execute(
            update(WebhookDelivery).values(locked_until=func.now() - timedelta(seconds=1))
        )  # the worker died
        await session.commit()
    async with sessionmaker() as session:
        [fresh] = await claim_due(session, settings)
    assert (stale.attempts, fresh.attempts) == (1, 2)
    async with sessionmaker() as session:
        await record_result(session, stale, webhooks.AttemptResult(ok=False, status_code=500, error="late"), settings)
    [row] = await deliveries(sessionmaker)
    assert (row.status, row.attempts) == ("delivering", 2)
    async with sessionmaker() as session:
        await record_result(session, fresh, webhooks.AttemptResult(ok=True, status_code=204, error=None), settings)
    [row] = await deliveries(sessionmaker)
    assert (row.status, row.last_status_code) == ("succeeded", 204)


async def test_concurrent_workers_claim_disjoint_batches(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await create_hook(client, seeded)
    for i in range(6):
        async with sessionmaker() as session:
            await enqueue_event(
                session, merchant_id=seeded.merchant_id, event_type="payment.settled", data={"i": i}, source_id=str(i)
            )
            await session.commit()
    settings = get_settings()

    async def claim() -> list[uuid.UUID]:
        async with sessionmaker() as session:
            return [item.id for item in await claim_due(session, settings)]

    batches = await asyncio.gather(*(claim() for _ in range(4)))
    claimed = [item for batch in batches for item in batch]
    assert len(claimed) == len(set(claimed)) == 6


# ---------------------------------------------------------------- test-fire + dispatcher


async def test_test_fire_delivers_immediately(
    client: httpx.AsyncClient, seeded: Seed, webhook_receiver: WebhookReceiver
) -> None:
    created = await create_hook(client, seeded, ["trial.used"])
    fired = await client.post(f"/v1/webhooks/{created.webhook.id}/test", headers=bearer(seeded.api_key))
    assert fired.status_code == 200, fired.text
    result = WebhookDeliveryRead.model_validate(fired.json())
    assert (result.status, result.attempts, result.event_type) == ("succeeded", 1, "webhook.test")
    [request] = webhook_receiver.requests
    assert verify_signature(created.secret, request.headers[WEBHOOK_SIGNATURE_HEADER], request.content)
    webhook_receiver.status = 410
    failed = await client.post(f"/v1/webhooks/{created.webhook.id}/test", headers=bearer(seeded.api_key))
    assert (failed.json()["status"], failed.json()["last_status_code"]) == ("pending", 410)


async def test_test_fire_is_rate_limited_per_merchant(
    client: httpx.AsyncClient,
    seeded: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    webhook_receiver: WebhookReceiver,
    override_settings: Any,
) -> None:
    override_settings(rl_webhook_test_per_minute=2)
    first = await create_hook(client, seeded, ["trial.used"])
    second = await create_hook(client, seeded, ["trial.used"])
    for hook in (first, second):  # the limit is the merchant's, across all of its webhooks
        fired = await client.post(f"/v1/webhooks/{hook.webhook.id}/test", headers=bearer(seeded.api_key))
        assert fired.status_code == 200, fired.text
    limited = await client.post(f"/v1/webhooks/{first.webhook.id}/test", headers=bearer(seeded.api_key))
    assert limited.status_code == 429, limited.text
    assert 0 < int(limited.headers["Retry-After"]) <= 60
    assert len(webhook_receiver.requests) == 2  # the refused fire sent nothing
    assert len(await events(sessionmaker)) == 2  # and queued nothing


async def test_test_fire_keeps_only_the_newest_finished_test_events(
    client: httpx.AsyncClient,
    seeded: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    webhook_receiver: WebhookReceiver,
    override_settings: Any,
) -> None:
    override_settings(webhook_test_events_kept=2)
    created = await create_hook(client, seeded, ["trial.used"])
    fired = [
        (await client.post(f"/v1/webhooks/{created.webhook.id}/test", headers=bearer(seeded.api_key))).json()
        for _ in range(4)
    ]
    kept = await events(sessionmaker)
    assert len(kept) == 2 and len(await deliveries(sessionmaker)) == 2
    assert {e.event_id for e in kept} == {fired[2]["event_id"], fired[3]["event_id"]}

    webhook_receiver.status = 500  # failing test deliveries stay pending for retry and are never pruned
    for _ in range(3):
        await client.post(f"/v1/webhooks/{created.webhook.id}/test", headers=bearer(seeded.api_key))
    statuses = sorted(d.status for d in await deliveries(sessionmaker))
    assert statuses == ["pending", "pending", "pending", "succeeded", "succeeded"]

async def test_dispatcher_delivers_in_the_background(
    client: httpx.AsyncClient,
    seeded: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    webhook_receiver: WebhookReceiver,
) -> None:
    await create_hook(client, seeded)
    hook_client = webhook_client_for(webhook_receiver)
    dispatcher = WebhookDispatcher(sessionmaker, hook_client)
    dispatcher.start()
    try:
        await pay(client)
        dispatcher.wake()
        for _ in range(100):
            if webhook_receiver.requests:
                break
            await asyncio.sleep(0.05)
    finally:
        await dispatcher.stop()
        await hook_client.aclose()
    assert len(webhook_receiver.requests) == 1
    [delivery] = await deliveries(sessionmaker)
    assert delivery.status == "succeeded"
