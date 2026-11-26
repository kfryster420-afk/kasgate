"""Failed-delivery auto-credit: a paid call the upstream never served becomes prepaid credit at that merchant."""

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.failed_delivery import (
    credit_failed_delivery,
    policy_covers,
    policy_enabled,
    redelivery_closed,
    sweep_failed_deliveries,
    sweep_pass,
)
from app.ledger import run_ledger_pass
from app.models import (
    Merchant,
    PrepaidBalance,
    PrepaidLedgerEntry,
    Settlement,
    SettlementStatus,
    WebhookEvent,
)
from app.schemas import (
    HEADER_FAILED_DELIVERY_CREDIT,
    HEADER_PAYMENT_RESPONSE,
    WEBHOOK_SIGNATURE_HEADER,
    FailedDeliveryCreditReceipt,
    PrepaidCreditRead,
    WebhookCreated,
    compute_fee_split,
)
from app.webhooks import deliver_due, verify_signature
from tests.conftest import (
    MERCHANT_ADDRESS,
    PAYER_ADDRESS,
    PRICE_SOMPI,
    Seed,
    SettingsOverride,
    StubEngine,
    Upstream,
    WebhookReceiver,
    bearer,
    paid_headers,
    seed_endpoint,
)
from tests.test_prepaid import SECOND_MERCHANT_ADDRESS, add_endpoint, fund, prepaid_call

TX = "cd" * 32
SPLIT = compute_fee_split(PRICE_SOMPI)
HOOK_URL = "https://hooks.example.com/kasgate"
WINDOW_PAST = 600 + 60 + 5  # redelivery window + default grace + margin


def _fail_upstream(upstream: Upstream, status: int | None = None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if status is None:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(status, text="boom")

    upstream.handler = handler


async def _settlement(maker: async_sessionmaker[AsyncSession]) -> Settlement:
    async with maker() as session:
        return (await session.execute(select(Settlement))).scalar_one()


async def _credits(maker: async_sessionmaker[AsyncSession]) -> list[PrepaidLedgerEntry]:
    async with maker() as session:
        return list(
            (
                await session.execute(
                    select(PrepaidLedgerEntry)
                    .where(PrepaidLedgerEntry.kind == "failed_delivery_credit")
                    .order_by(PrepaidLedgerEntry.created_at)
                )
            ).scalars()
        )


async def _balance(maker: async_sessionmaker[AsyncSession], merchant_id: uuid.UUID) -> PrepaidBalance | None:
    async with maker() as session:
        return (
            await session.execute(select(PrepaidBalance).where(PrepaidBalance.merchant_id == merchant_id))
        ).scalar_one_or_none()


async def _age(maker: async_sessionmaker[AsyncSession], seconds: int) -> None:
    """Move every settlement (and the merchants' policy start, which must precede them) `seconds` into the past."""
    past = datetime.now(UTC) - timedelta(seconds=seconds)
    async with maker() as session:
        await session.execute(update(Settlement).values(settled_at=past, created_at=past))
        await session.execute(update(Merchant).values(failed_delivery_credit_enabled_at=past - timedelta(seconds=10)))
        await session.commit()


async def _create_hook(client: httpx.AsyncClient, seed: Seed) -> WebhookCreated:
    response = await client.post(
        "/v1/webhooks",
        json={"url": HOOK_URL, "event_types": ["prepaid.credit.failed_delivery"]},
        headers=bearer(seed.api_key),
    )
    assert response.status_code == 201, response.text
    return WebhookCreated.model_validate(response.json())


def _receipt(response: httpx.Response) -> FailedDeliveryCreditReceipt:
    return FailedDeliveryCreditReceipt.model_validate_json(
        base64.b64decode(response.headers[HEADER_FAILED_DELIVERY_CREDIT])
    )


async def _exhaust(client: httpx.AsyncClient, tx_id: str = TX, slug: str = "weather") -> list[httpx.Response]:
    headers = await paid_headers(client, tx_id, slug)
    return [await client.get(f"/p/{slug}", headers=headers) for _ in range(3)]


# ---------------------------------------------------------------- pure policy rules


def _merchant(enabled_at: datetime, disabled_at: datetime | None) -> Merchant:
    return Merchant(
        kaspa_address=MERCHANT_ADDRESS,
        display_name="m",
        api_key_hash="a" * 64,
        failed_delivery_credit_enabled_at=enabled_at,
        failed_delivery_credit_disabled_at=disabled_at,
    )


def test_policy_covers_only_the_latest_enabled_interval() -> None:
    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    on = _merchant(t0, None)
    assert policy_enabled(on)
    assert policy_covers(on, t0) and policy_covers(on, t0 + timedelta(days=9))
    assert not policy_covers(on, t0 - timedelta(seconds=1))  # never retroactive
    off = _merchant(t0, t0 + timedelta(hours=1))
    assert not policy_enabled(off)
    assert policy_covers(off, t0 + timedelta(minutes=59))  # paid while on: still credited after opting out
    assert not policy_covers(off, t0 + timedelta(hours=1))
    back_on = _merchant(t0 + timedelta(hours=2), t0 + timedelta(hours=1))
    assert policy_enabled(back_on)
    assert not policy_covers(back_on, t0 + timedelta(minutes=90))  # paid while off: never credited
    assert policy_covers(back_on, t0 + timedelta(hours=3))


def test_redelivery_closed_reasons(override_settings: SettingsOverride) -> None:
    now = datetime.now(UTC)
    row = Settlement(delivery_attempts=1, settled_at=now - timedelta(seconds=30))
    assert redelivery_closed(get_settings(), row, now) is None
    row.delivery_attempts = 3
    assert redelivery_closed(get_settings(), row, now) == "attempts_exhausted"
    row.delivery_attempts = 1
    row.settled_at = now - timedelta(seconds=659)
    assert redelivery_closed(get_settings(), row, now) is None  # inside window + grace
    row.settled_at = now - timedelta(seconds=660)
    assert redelivery_closed(get_settings(), row, now) == "window_expired"
    override_settings(redelivery_window_seconds=0)
    row.settled_at = now
    assert redelivery_closed(get_settings(), row, now) == "attempts_exhausted"  # redelivery switched off


# ---------------------------------------------------------------- crediting on the request path


async def test_last_failed_delivery_credits_the_merchant_share(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    stub_engine: StubEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _fail_upstream(upstream)
    first, second, third = await _exhaust(client)
    assert [first.status_code, second.status_code, third.status_code] == [502, 502, 502]
    assert HEADER_FAILED_DELIVERY_CREDIT not in first.headers
    assert HEADER_FAILED_DELIVERY_CREDIT not in second.headers
    assert HEADER_PAYMENT_RESPONSE in third.headers
    receipt = _receipt(third)
    assert receipt.txId == TX and receipt.kind == "failed_delivery_credit"
    assert receipt.reason == "attempts_exhausted"
    assert receipt.merchantId == seeded.merchant_id
    assert int(receipt.creditSompi) == SPLIT.merchant_sompi == int(receipt.balanceSompi)
    assert stub_engine.calls == [TX]  # nothing new went on-chain

    [entry] = await _credits(sessionmaker)
    assert entry.id == receipt.creditId
    assert (entry.direction, entry.amount_sompi, entry.balance_after_sompi) == (
        "credit",
        SPLIT.merchant_sompi,
        SPLIT.merchant_sompi,
    )
    assert (entry.tx_id, entry.slug, entry.credit_reason, entry.refund_id) == (
        TX,
        "weather",
        "attempts_exhausted",
        None,
    )
    assert entry.note == "upstream no response after 3 deliveries (attempts_exhausted)"
    settlement = await _settlement(sessionmaker)
    assert entry.settlement_id == settlement.id
    assert settlement.status == SettlementStatus.SETTLED  # the on-chain payment itself is untouched
    balance = await _balance(sessionmaker, seeded.merchant_id)
    assert balance is not None
    assert (balance.balance_sompi, balance.credited_sompi, balance.deposited_sompi) == (SPLIT.merchant_sompi,) * 2 + (
        0,
    )
    assert balance.payer_address == PAYER_ADDRESS

    # the buyer's public balance shows the credit, and it is spendable through prepaid debits
    read = await client.get("/v1/prepaid/balance", params={"slug": "weather", "payer_address": PAYER_ADDRESS})
    assert read.status_code == 200
    assert read.json()["credited_sompi"] == str(SPLIT.merchant_sompi)
    assert read.json()["spendable_sompi"] == str(SPLIT.merchant_sompi)
    upstream.handler = None
    await add_endpoint(sessionmaker, seeded.merchant_id, "cheap")
    assert (await prepaid_call(client)).status_code == 200
    after = await _balance(sessionmaker, seeded.merchant_id)
    assert after is not None and after.balance_sompi == SPLIT.merchant_sompi - 1_000

    # a fourth try is a plain replay: no delivery, no second credit
    replay = await client.get("/p/weather", headers=await paid_headers(client, TX))
    assert replay.status_code == 409
    assert len(await _credits(sessionmaker)) == 1


@pytest.mark.parametrize("status", [500, 503, 504])
async def test_final_5xx_is_relayed_with_the_credit(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    override_settings: SettingsOverride,
    status: int,
) -> None:
    override_settings(redelivery_max_attempts=1)
    _fail_upstream(upstream, status)
    response = await client.get("/p/weather", headers=await paid_headers(client, TX))
    assert response.status_code == status
    assert response.text == "boom"
    assert _receipt(response).reason == "attempts_exhausted"
    [entry] = await _credits(sessionmaker)
    assert entry.note == f"upstream HTTP {status} after 1 delivery (attempts_exhausted)"


async def test_redelivery_switched_off_credits_the_first_failure(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    override_settings: SettingsOverride,
) -> None:
    override_settings(redelivery_window_seconds=0)
    _fail_upstream(upstream)
    response = await client.get("/p/weather", headers=await paid_headers(client, TX))
    assert response.status_code == 502
    assert _receipt(response).reason == "attempts_exhausted"


@pytest.mark.parametrize("status", [200, 302, 404, 418])
async def test_non_5xx_answers_are_never_credited(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    override_settings: SettingsOverride,
    status: int,
) -> None:
    override_settings(redelivery_max_attempts=1)
    upstream.handler = lambda request: httpx.Response(status, text="x")
    response = await client.get("/p/weather", headers=await paid_headers(client, TX))
    assert response.status_code == status
    assert HEADER_FAILED_DELIVERY_CREDIT not in response.headers
    await _age(sessionmaker, WINDOW_PAST)
    assert await sweep_failed_deliveries(sessionmaker, limit=10) == 0
    assert await _credits(sessionmaker) == []


async def test_upstream_cannot_forge_the_credit_header(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    forged = FailedDeliveryCreditReceipt(
        creditId=uuid.uuid4(),
        kind="failed_delivery_credit",
        txId=TX,
        merchantId=seeded.merchant_id,
        creditSompi="1",
        balanceSompi="1",
        reason="attempts_exhausted",
    ).to_header()
    upstream.handler = lambda request: httpx.Response(503, text="boom", headers={"X-KasGate-Credit": forged})
    response = await client.get("/p/weather", headers=await paid_headers(client, TX))
    assert response.status_code == 503
    assert HEADER_FAILED_DELIVERY_CREDIT not in response.headers  # first of three attempts: nothing credited yet
    assert await _credits(sessionmaker) == []


async def test_failure_then_success_is_never_credited(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    _fail_upstream(upstream)
    headers = await paid_headers(client, TX)
    assert (await client.get("/p/weather", headers=headers)).status_code == 502
    upstream.handler = None
    assert (await client.get("/p/weather", headers=headers)).status_code == 200
    await _age(sessionmaker, WINDOW_PAST)
    assert await sweep_failed_deliveries(sessionmaker, limit=10) == 0
    assert await _credits(sessionmaker) == []


# ---------------------------------------------------------------- idempotency


async def test_crediting_is_idempotent_per_tx(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    _fail_upstream(upstream)
    await _exhaust(client)
    settlement = await _settlement(sessionmaker)
    for _ in range(3):
        async with sessionmaker() as session:
            assert await credit_failed_delivery(session, settlement.id) is None
    await _age(sessionmaker, WINDOW_PAST)
    assert await sweep_failed_deliveries(sessionmaker, limit=10) == 0
    assert len(await _credits(sessionmaker)) == 1
    balance = await _balance(sessionmaker, seeded.merchant_id)
    assert balance is not None and balance.credited_sompi == SPLIT.merchant_sompi


async def test_concurrent_credits_of_one_tx_credit_once(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    import asyncio

    _fail_upstream(upstream)
    headers = await paid_headers(client, TX)
    assert (await client.get("/p/weather", headers=headers)).status_code == 502
    await _age(sessionmaker, WINDOW_PAST)
    settlement = await _settlement(sessionmaker)

    async def attempt() -> object:
        async with sessionmaker() as session:
            return await credit_failed_delivery(session, settlement.id)

    results = await asyncio.gather(*(attempt() for _ in range(5)))
    assert sum(result is not None for result in results) == 1
    assert len(await _credits(sessionmaker)) == 1


async def test_unique_partial_index_rejects_a_second_credit_row_for_a_tx(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from sqlalchemy.exc import IntegrityError

    _fail_upstream(upstream)
    await _exhaust(client)
    [entry] = await _credits(sessionmaker)
    async with sessionmaker() as session:
        session.add(
            PrepaidLedgerEntry(
                balance_id=entry.balance_id,
                merchant_id=entry.merchant_id,
                payer_address=entry.payer_address,
                kind="failed_delivery_credit",
                direction="credit",
                amount_sompi=1,
                balance_after_sompi=1,
                tx_id=TX,
                settlement_id=None,
                slug="weather",
                credit_reason="window_expired",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


# ---------------------------------------------------------------- the sweep (redelivery window closed)


async def test_sweep_credits_after_the_window_and_grace(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    _fail_upstream(upstream, 503)
    headers = await paid_headers(client, TX)
    first = await client.get("/p/weather", headers=headers)
    assert first.status_code == 503 and HEADER_FAILED_DELIVERY_CREDIT not in first.headers
    await _age(sessionmaker, 620)  # window closed, but an in-flight redelivery could still be finishing
    assert await sweep_failed_deliveries(sessionmaker, limit=10) == 0
    await _age(sessionmaker, WINDOW_PAST)
    assert await sweep_failed_deliveries(sessionmaker, limit=10) == 1
    [entry] = await _credits(sessionmaker)
    assert entry.credit_reason == "window_expired"
    assert entry.note == "upstream HTTP 503 after 1 delivery (window_expired)"
    # a credited payment can never be delivered as well, even if its window looked open again
    upstream.handler = None
    async with sessionmaker() as session:
        await session.execute(update(Settlement).values(settled_at=datetime.now(UTC)))
        await session.commit()
    assert (await client.get("/p/weather", headers=headers)).status_code == 409
    assert len(upstream.requests) == 1


async def test_sweep_walks_every_candidate_in_batches(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    _fail_upstream(upstream)
    for index in range(5):
        tx_id = f"{index:02x}" * 32
        assert (await client.get("/p/weather", headers=await paid_headers(client, tx_id))).status_code == 502
    await _age(sessionmaker, WINDOW_PAST)
    assert await sweep_failed_deliveries(sessionmaker, limit=2) == 5
    assert len(await _credits(sessionmaker)) == 5
    balance = await _balance(sessionmaker, seeded.merchant_id)
    assert balance is not None and balance.credited_sompi == 5 * SPLIT.merchant_sompi
    assert await sweep_failed_deliveries(sessionmaker, limit=2) == 0


async def test_sweep_respects_the_lookback_horizon(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    override_settings: SettingsOverride,
) -> None:
    override_settings(failed_delivery_credit_lookback_seconds=3600)
    _fail_upstream(upstream)
    assert (await client.get("/p/weather", headers=await paid_headers(client, TX))).status_code == 502
    await _age(sessionmaker, 3700)
    assert await sweep_failed_deliveries(sessionmaker, limit=10) == 0


async def test_ledger_worker_pass_runs_the_sweep(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    stub_engine: StubEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _fail_upstream(upstream)
    assert (await client.get("/p/weather", headers=await paid_headers(client, TX))).status_code == 502
    await _age(sessionmaker, WINDOW_PAST)
    assert await run_ledger_pass(sessionmaker, stub_engine, [sweep_pass]) == 1
    assert len(await _credits(sessionmaker)) == 1


async def test_prepaid_deposits_are_never_credited(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await add_endpoint(sessionmaker, seeded.merchant_id, "cheap")
    await fund(client)
    settlement = await _settlement(sessionmaker)
    assert settlement.upstream_status_code is None  # a deposit never calls an upstream
    await _age(sessionmaker, WINDOW_PAST)
    assert await sweep_failed_deliveries(sessionmaker, limit=10) == 0
    async with sessionmaker() as session:
        assert await credit_failed_delivery(session, settlement.id) is None
    assert await _credits(sessionmaker) == []


async def test_payments_without_a_proven_payer_key_are_never_credited(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    past = datetime.now(UTC) - timedelta(seconds=WINDOW_PAST)
    async with sessionmaker() as session:
        session.add(
            Settlement(
                tx_id="ef" * 32,
                endpoint_id=seeded.endpoint_id,
                merchant_id=seeded.merchant_id,
                network="kaspa:testnet-10",
                payment_scheme="k402-kaspa-utxo",
                payer_address=None,
                merchant_address=MERCHANT_ADDRESS,
                treasury_address="kaspatest:treasury",
                amount_sompi=PRICE_SOMPI,
                merchant_amount_sompi=SPLIT.merchant_sompi,
                fee_amount_sompi=SPLIT.fee_sompi,
                status=SettlementStatus.SETTLED,
                request_method="GET",
                request_path="/p/weather",
                payment_nonce="1" * 32,
                k402_payment_id="p_" + "1" * 16,
                created_at=past,
                settled_at=past,
            )
        )
        await session.execute(update(Merchant).values(failed_delivery_credit_enabled_at=past - timedelta(seconds=10)))
        await session.commit()
    assert await sweep_failed_deliveries(sessionmaker, limit=10) == 0
    assert await _credits(sessionmaker) == []


# ---------------------------------------------------------------- merchant opt-out policy


async def test_policy_defaults_on_and_can_be_switched_off_and_on(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    auth = bearer(seeded.api_key)
    read = await client.get("/v1/prepaid/failed-delivery-policy", headers=auth)
    assert read.status_code == 200
    assert read.json()["enabled"] is True and read.json()["disabled_at"] is None
    assert (await client.get("/v1/prepaid/failed-delivery-policy")).status_code == 401
    for bad in ({"enabled": "no"}, {"enabled": 0}, {}, {"enabled": True, "extra": 1}):
        assert (await client.put("/v1/prepaid/failed-delivery-policy", json=bad, headers=auth)).status_code == 422

    off = await client.put("/v1/prepaid/failed-delivery-policy", json={"enabled": False}, headers=auth)
    assert off.status_code == 200 and off.json()["enabled"] is False and off.json()["disabled_at"] is not None
    again = await client.put("/v1/prepaid/failed-delivery-policy", json={"enabled": False}, headers=auth)
    assert again.json() == off.json()  # setting the same state changes nothing

    _fail_upstream(upstream)
    responses = await _exhaust(client)
    assert HEADER_FAILED_DELIVERY_CREDIT not in responses[-1].headers
    assert await _credits(sessionmaker) == []

    on = await client.put("/v1/prepaid/failed-delivery-policy", json={"enabled": True}, headers=auth)
    assert on.json()["enabled"] is True
    # turning it back on is not retroactive: the call paid while it was off stays uncredited
    async with sessionmaker() as session:
        settlement_id = (await session.execute(select(Settlement.id))).scalar_one()
        assert await credit_failed_delivery(session, settlement_id) is None
    # a call paid after re-enabling is credited
    later = await _exhaust(client, "ce" * 32)
    assert _receipt(later[-1]).txId == "ce" * 32


async def test_opting_out_never_drops_calls_paid_while_on(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    _fail_upstream(upstream)
    assert (await client.get("/p/weather", headers=await paid_headers(client, TX))).status_code == 502
    await _age(sessionmaker, WINDOW_PAST)
    off = await client.put(
        "/v1/prepaid/failed-delivery-policy", json={"enabled": False}, headers=bearer(seeded.api_key)
    )
    assert off.status_code == 200
    assert await sweep_failed_deliveries(sessionmaker, limit=10) == 1


# ---------------------------------------------------------------- webhook


async def test_each_credit_sends_one_signed_webhook(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    webhook_receiver: WebhookReceiver,
) -> None:
    created = await _create_hook(client, seeded)
    _fail_upstream(upstream, 502)
    responses = await _exhaust(client)
    receipt = _receipt(responses[-1])
    async with sessionmaker() as session:
        [event] = list((await session.execute(select(WebhookEvent))).scalars())
    assert event.event_type == "prepaid.credit.failed_delivery"
    async with httpx.AsyncClient(transport=httpx.MockTransport(webhook_receiver)) as hook_client:
        assert await deliver_due(sessionmaker, hook_client) == 1
    [request] = webhook_receiver.requests
    assert verify_signature(created.secret, request.headers[WEBHOOK_SIGNATURE_HEADER], request.content)
    envelope: dict[str, Any] = json.loads(request.content)
    assert envelope["type"] == "prepaid.credit.failed_delivery"
    data = envelope["data"]
    settlement = await _settlement(sessionmaker)
    assert data == {
        "ledger_entry_id": str(receipt.creditId),
        "kind": "failed_delivery_credit",
        "settlement_id": str(settlement.id),
        "tx_id": TX,
        "payment_scheme": "kaspa-exact-v1",
        "endpoint_id": str(seeded.endpoint_id),
        "endpoint_slug": "weather",
        "payer_address": PAYER_ADDRESS,
        "amount_sompi": str(PRICE_SOMPI),
        "fee_sompi": str(SPLIT.fee_sompi),
        "credit_sompi": str(SPLIT.merchant_sompi),
        "balance_sompi": str(SPLIT.merchant_sompi),
        "reason": "attempts_exhausted",
        "upstream_status_code": 502,
        "delivery_attempts": 3,
        "settled_at": settlement.settled_at.isoformat() if settlement.settled_at else None,
    }


# ---------------------------------------------------------------- list-credits API and the ledger


async def test_list_credits_filters_and_is_merchant_scoped(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    override_settings: SettingsOverride,
) -> None:
    override_settings(redelivery_max_attempts=1)
    other = await seed_endpoint(sessionmaker, slug="maps", address=SECOND_MERCHANT_ADDRESS)
    _fail_upstream(upstream, 500)
    assert (await client.get("/p/weather", headers=await paid_headers(client, TX))).status_code == 500
    assert (await client.get("/p/maps", headers=await paid_headers(client, "ce" * 32, "maps"))).status_code == 500

    auth = bearer(seeded.api_key)
    assert (await client.get("/v1/prepaid/credits")).status_code == 401
    listed = await client.get("/v1/prepaid/credits", headers=auth)
    assert listed.status_code == 200
    [credit] = [PrepaidCreditRead.model_validate(item, strict=False) for item in listed.json()]
    assert (credit.tx_id, credit.slug, credit.reason, credit.upstream_status_code) == (
        TX,
        "weather",
        "attempts_exhausted",
        500,
    )
    assert (credit.amount_sompi, credit.fee_sompi, credit.credit_sompi) == (
        str(PRICE_SOMPI),
        str(SPLIT.fee_sompi),
        str(SPLIT.merchant_sompi),
    )
    assert credit.payment_scheme == "kaspa-exact-v1" and credit.delivery_attempts == 1
    assert credit.endpoint_id == seeded.endpoint_id

    for params, expected in (
        ({"slug": "weather"}, 1),
        ({"slug": "maps"}, 0),  # another merchant's endpoint
        ({"tx_id": TX}, 1),
        ({"tx_id": "ce" * 32}, 0),
        ({"payer_address": PAYER_ADDRESS}, 1),
        ({"payer_address": SECOND_MERCHANT_ADDRESS}, 0),
    ):
        response = await client.get("/v1/prepaid/credits", params=params, headers=auth)
        assert response.status_code == 200, params
        assert len(response.json()) == expected, params
    for bad in ({"slug": "NO"}, {"tx_id": "zz"}, {"payer_address": "kaspatest:nope0000000"}, {"limit": 0}):
        assert (await client.get("/v1/prepaid/credits", params=bad, headers=auth)).status_code == 422, bad

    other_list = await client.get("/v1/prepaid/credits", headers=bearer(other.api_key))
    assert [item["slug"] for item in other_list.json()] == ["maps"]

    ledger = await client.get("/v1/prepaid/ledger", headers=auth)
    assert ledger.status_code == 200
    [row] = ledger.json()
    assert (row["kind"], row["direction"], row["tx_id"], row["slug"], row["credit_reason"]) == (
        "failed_delivery_credit",
        "credit",
        TX,
        "weather",
        "attempts_exhausted",
    )


async def test_credit_counts_toward_the_balance_identity(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await add_endpoint(sessionmaker, seeded.merchant_id, "cheap")
    await fund(client)
    _fail_upstream(upstream)
    await _exhaust(client)
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(
                    PrepaidBalance.balance_sompi,
                    PrepaidBalance.deposited_sompi + PrepaidBalance.credited_sompi - PrepaidBalance.debited_sompi,
                    func.count(),
                ).group_by(PrepaidBalance.id)
            )
        ).one()
    assert row[0] == row[1] == 40_000_000 + SPLIT.merchant_sompi
    assert row[2] == 1
