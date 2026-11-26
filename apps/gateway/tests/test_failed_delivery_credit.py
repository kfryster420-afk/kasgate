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
    alert_stale_pending_deliveries,
    credit_failed_delivery,
    policy_enabled,
    redelivery_closed,
    stamped_policy,
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
from tests.test_prepaid import CREDIT_SOMPI, DEPOSIT_SOMPI, SECOND_MERCHANT_ADDRESS, add_endpoint, fund, prepaid_call

TX = "cd" * 32
TX2 = "ce" * 32
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


def _merchant(enabled_at: datetime, disabled_at: datetime | None, *, timeouts: bool = True) -> Merchant:
    return Merchant(
        kaspa_address=MERCHANT_ADDRESS,
        display_name="m",
        api_key_hash="a" * 64,
        failed_delivery_credit_enabled_at=enabled_at,
        failed_delivery_credit_disabled_at=disabled_at,
        failed_delivery_credit_timeouts=timeouts,
    )


def test_policy_stamp_follows_the_last_switch() -> None:
    t0 = datetime(2026, 11, 1, tzinfo=UTC)
    on = _merchant(t0, None)
    assert policy_enabled(on) and stamped_policy(on) == "errors_and_timeouts"
    assert stamped_policy(_merchant(t0, None, timeouts=False)) == "errors"
    off = _merchant(t0, t0 + timedelta(hours=1))
    assert not policy_enabled(off) and stamped_policy(off) is None
    back_on = _merchant(t0 + timedelta(hours=2), t0 + timedelta(hours=1))
    assert policy_enabled(back_on) and stamped_policy(back_on) == "errors_and_timeouts"


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
    assert read.json()["credit_timeouts"] is True
    assert (await client.get("/v1/prepaid/failed-delivery-policy")).status_code == 401
    for bad in (
        {"enabled": "no"},
        {"enabled": 0},
        {},
        {"enabled": None, "credit_timeouts": None},
        {"credit_timeouts": "yes"},
        {"enabled": True, "extra": 1},
    ):
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
    assert on.json()["enabled"] is True and on.json()["credit_timeouts"] is True
    # turning it back on is not retroactive: the call paid while it was off stays uncredited (stamped NULL)
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
        "shortfall_sompi": "0",
        "balance_sompi": str(SPLIT.merchant_sompi),
        "reason": "attempts_exhausted",
        "delivery_failure": "upstream_5xx",
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


# ---------------------------------------------------------------- expiry and the balance cap


async def _set_expiry_policy(client: httpx.AsyncClient, seed: Seed, days: int | None) -> None:
    response = await client.patch("/v1/merchants/me", json={"prepaid_expiry_days": days}, headers=bearer(seed.api_key))
    assert response.status_code == 200, response.text


def _expires_around(value: datetime | None, before: datetime, days: int) -> None:
    assert value is not None
    assert before + timedelta(days=days) - timedelta(minutes=1) <= value <= datetime.now(UTC) + timedelta(days=days)


async def test_credit_extends_expiry_like_a_deposit_and_never_shortens_it(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """expires_at = max(current, now + policy at credit time); a later current expiry is kept."""
    await _set_expiry_policy(client, seeded, 30)
    _fail_upstream(upstream)
    before = datetime.now(UTC)
    await _exhaust(client, TX)
    row = await _balance(sessionmaker, seeded.merchant_id)
    assert row is not None
    first = row.expires_at
    _expires_around(first, before, 30)

    await _set_expiry_policy(client, seeded, 7)
    assert (await _balance(sessionmaker, seeded.merchant_id)).expires_at == first  # not retroactive

    await _exhaust(client, TX2)
    later = await _balance(sessionmaker, seeded.merchant_id)
    assert later is not None and later.expires_at == first  # a 7-day policy must not shorten the 30-day expiry
    assert later.credited_sompi == 2 * SPLIT.merchant_sompi


async def test_credit_extends_an_earlier_existing_expiry(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _set_expiry_policy(client, seeded, 7)
    _fail_upstream(upstream)
    await _exhaust(client, TX)
    early = datetime.now(UTC) + timedelta(days=1)
    async with sessionmaker() as session:
        await session.execute(update(PrepaidBalance).values(expires_at=early))
        await session.commit()
    await _set_expiry_policy(client, seeded, 30)
    before = datetime.now(UTC)
    await _exhaust(client, TX2)
    row = await _balance(sessionmaker, seeded.merchant_id)
    assert row is not None and row.expires_at is not None and row.expires_at > early
    _expires_around(row.expires_at, before, 30)


async def test_credit_expiry_is_not_retroactive_for_credit_promised_never_to_expire(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    _fail_upstream(upstream)
    await _exhaust(client, TX)
    assert (await _balance(sessionmaker, seeded.merchant_id)).expires_at is None
    await _set_expiry_policy(client, seeded, 7)
    await _exhaust(client, TX2)
    row = await _balance(sessionmaker, seeded.merchant_id)
    assert row is not None and row.expires_at is None  # promised "never" stays, like a deposit
    assert row.credited_sompi == 2 * SPLIT.merchant_sompi


async def test_a_never_policy_credit_clears_the_existing_expiry(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _set_expiry_policy(client, seeded, 7)
    _fail_upstream(upstream)
    await _exhaust(client, TX)
    assert (await _balance(sessionmaker, seeded.merchant_id)).expires_at is not None
    await _set_expiry_policy(client, seeded, None)
    await _exhaust(client, TX2)
    assert (await _balance(sessionmaker, seeded.merchant_id)).expires_at is None


async def test_credit_uses_the_expiry_policy_in_force_at_credit_time(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _set_expiry_policy(client, seeded, 1)
    _fail_upstream(upstream)
    response = await client.get("/p/weather", headers=await paid_headers(client, TX))
    assert response.status_code == 502
    assert HEADER_FAILED_DELIVERY_CREDIT not in response.headers
    await _age(sessionmaker, WINDOW_PAST)
    await _set_expiry_policy(client, seeded, 30)
    before = datetime.now(UTC)
    assert await sweep_failed_deliveries(sessionmaker, limit=10) == 1
    row = await _balance(sessionmaker, seeded.merchant_id)
    assert row is not None
    _expires_around(row.expires_at, before, 30)
    [entry] = await _credits(sessionmaker)
    assert entry.credit_reason == "window_expired"


async def test_credit_expires_overdue_credit_before_adding_the_new_credit(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _set_expiry_policy(client, seeded, 1)
    _fail_upstream(upstream)
    await _exhaust(client, TX)
    async with sessionmaker() as session:
        await session.execute(update(PrepaidBalance).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        await session.commit()
    before = datetime.now(UTC)
    await _exhaust(client, TX2)
    row = await _balance(sessionmaker, seeded.merchant_id)
    assert row is not None
    assert (row.expired_sompi, row.credited_sompi, row.balance_sompi) == (
        SPLIT.merchant_sompi,
        2 * SPLIT.merchant_sompi,
        SPLIT.merchant_sompi,
    )
    _expires_around(row.expires_at, before, 1)


async def test_credit_is_clamped_to_prepaid_max_balance_and_the_shortfall_is_recorded(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    override_settings: SettingsOverride,
    webhook_receiver: WebhookReceiver,
) -> None:
    """Revised decision 4: a credit never lifts a balance above PREPAID_MAX_BALANCE_SOMPI. The cut part is kept on
    the ledger row (`shortfall_sompi`) and announced with a signed `prepaid.credit.failed_delivery_shortfall`."""
    created = await client.post(
        "/v1/webhooks",
        json={"url": HOOK_URL, "event_types": ["prepaid.credit.failed_delivery_shortfall"]},
        headers=bearer(seeded.api_key),
    )
    assert created.status_code == 201, created.text
    secret = WebhookCreated.model_validate(created.json()).secret
    await add_endpoint(sessionmaker, seeded.merchant_id, "cheap")
    await fund(client)
    cap = DEPOSIT_SOMPI
    override_settings(prepaid_max_balance_sompi=cap)
    _fail_upstream(upstream)
    receipt = _receipt((await _exhaust(client))[-1])
    room = cap - CREDIT_SOMPI
    assert (int(receipt.creditSompi), receipt.shortfallSompi) == (room, str(SPLIT.merchant_sompi - room))
    row = await _balance(sessionmaker, seeded.merchant_id)
    assert row is not None
    assert (row.balance_sompi, row.credited_sompi, row.credit_balance_sompi) == (cap, room, room)
    [entry] = await _credits(sessionmaker)
    assert (entry.amount_sompi, entry.shortfall_sompi) == (room, SPLIT.merchant_sompi - room)
    async with httpx.AsyncClient(transport=httpx.MockTransport(webhook_receiver)) as hook_client:
        assert await deliver_due(sessionmaker, hook_client) == 1
    [request] = webhook_receiver.requests
    assert verify_signature(secret, request.headers[WEBHOOK_SIGNATURE_HEADER], request.content)
    envelope: dict[str, Any] = json.loads(request.content)
    assert envelope["type"] == "prepaid.credit.failed_delivery_shortfall"
    assert (envelope["data"]["credit_sompi"], envelope["data"]["shortfall_sompi"]) == (
        str(room),
        str(SPLIT.merchant_sompi - room),
    )
    assert envelope["data"]["merchant_share_sompi"] == str(SPLIT.merchant_sompi)
    assert envelope["data"]["max_balance_sompi"] == str(cap)


async def test_a_full_balance_gets_a_zero_credit_row_and_only_the_shortfall_event(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    override_settings: SettingsOverride,
) -> None:
    subscribed = await client.post(
        "/v1/webhooks",
        json={
            "url": HOOK_URL,
            "event_types": ["prepaid.credit.failed_delivery", "prepaid.credit.failed_delivery_shortfall"],
        },
        headers=bearer(seeded.api_key),
    )
    assert subscribed.status_code == 201, subscribed.text
    await add_endpoint(sessionmaker, seeded.merchant_id, "cheap")
    await fund(client)
    override_settings(prepaid_max_balance_sompi=60_000_000)
    async with sessionmaker() as session:
        # Lift the balance to the cap with deposited credit (identity kept), so nothing is left for a credit.
        await session.execute(
            update(PrepaidBalance).values(
                balance_sompi=60_000_000, deposited_sompi=PrepaidBalance.deposited_sompi + 20_000_000
            )
        )
        await session.commit()
    _fail_upstream(upstream)
    responses = await _exhaust(client)
    assert HEADER_FAILED_DELIVERY_CREDIT not in responses[-1].headers  # nothing was credited
    [entry] = await _credits(sessionmaker)
    assert (entry.amount_sompi, entry.shortfall_sompi) == (0, SPLIT.merchant_sompi)
    async with sessionmaker() as session:
        types = [e.event_type for e in (await session.execute(select(WebhookEvent))).scalars()]
    assert "prepaid.credit.failed_delivery" not in types
    assert types.count("prepaid.credit.failed_delivery_shortfall") == 1
    async with sessionmaker() as session:
        outcome = (
            await session.execute(select(Settlement.delivery_outcome).where(Settlement.tx_id == TX))
        ).scalar_one()
    assert outcome == "credited"
    # The settlement is closed: no redelivery and no second credit row.
    assert await sweep_failed_deliveries(sessionmaker, limit=10) == 0


# ---------------------------------------------------------------- what gets credited (QA decision 3)


async def test_timeouts_are_credited_by_default_and_never_once_the_merchant_excludes_them(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    upstream.handler = timeout
    responses = await _exhaust(client)
    assert responses[-1].status_code == 502
    assert _receipt(responses[-1]).reason == "attempts_exhausted"
    assert (await _settlement(sessionmaker)).delivery_failure == "timeout"
    off = await client.put(
        "/v1/prepaid/failed-delivery-policy", json={"credit_timeouts": False}, headers=bearer(seeded.api_key)
    )
    assert off.status_code == 200 and off.json() == {**off.json(), "enabled": True, "credit_timeouts": False}
    later = await _exhaust(client, TX2)
    assert HEADER_FAILED_DELIVERY_CREDIT not in later[-1].headers
    await _age(sessionmaker, WINDOW_PAST)
    assert await sweep_failed_deliveries(sessionmaker, limit=10) == 0
    assert len(await _credits(sessionmaker)) == 1
    # A 5xx and a connection error are still credited under the `errors` policy.
    _fail_upstream(upstream)
    third = await _exhaust(client, "cf" * 32)
    assert HEADER_FAILED_DELIVERY_CREDIT in third[-1].headers


async def test_an_unrecorded_delivery_outcome_is_alerted_and_never_credited(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The outcome write fails: the settlement stays `pending` (written before the upstream call), the failure is
    logged at ERROR and pushed to the delivery-outcome dead-letter list, and `pending` is never credited."""
    import app.proxy as proxy_module

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise proxy_module.DeliveryOutcomeError('{"settlementId": "x"}')

    monkeypatch.setattr(proxy_module, "_write_delivery_outcome", broken)
    _fail_upstream(upstream, 503)
    with caplog.at_level("ERROR", logger="kasgate.proxy"):
        responses = await _exhaust(client)
    assert all(r.status_code == 503 for r in responses)
    assert HEADER_FAILED_DELIVERY_CREDIT not in responses[-1].headers
    assert any("unrecorded delivery outcome" in r.getMessage() for r in caplog.records if r.levelname == "ERROR")
    assert await redis.llen(proxy_module.DELIVERY_OUTCOME_DEAD_LETTER_KEY) == 3
    monkeypatch.undo()
    settlement = await _settlement(sessionmaker)
    assert (settlement.delivery_outcome, settlement.delivery_failure) == ("pending", None)
    await _age(sessionmaker, WINDOW_PAST)
    assert await sweep_failed_deliveries(sessionmaker, limit=10) == 0
    assert await _credits(sessionmaker) == []


# ---------------------------------------------------------------- stale pending alert (crash / lost outcome write)


async def test_a_settlement_still_pending_after_the_window_is_alerted_once(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A settlement left `pending` past the redelivery window plus grace (crash mid-call or a lost outcome write)
    is logged at ERROR once; it is never credited. A second pass is a no-op."""
    served = await client.get("/p/weather", headers=await paid_headers(client, TX))
    assert served.status_code == 200
    async with sessionmaker() as session:
        await session.execute(
            update(Settlement).values(delivery_outcome="pending", delivery_failure=None, upstream_status_code=None)
        )
        await session.commit()
    # Still inside the window: no alert, no credit.
    assert await alert_stale_pending_deliveries(sessionmaker, limit=10) == 0
    await _age(sessionmaker, WINDOW_PAST)
    with caplog.at_level("ERROR", logger="kasgate.failed_delivery"):
        assert await alert_stale_pending_deliveries(sessionmaker, limit=10) == 1
    settlement = await _settlement(sessionmaker)
    assert settlement.delivery_outcome == "pending"
    assert settlement.pending_alerted_at is not None
    assert any(
        "delivery outcome still pending after the redelivery window" in rec.getMessage()
        and str(settlement.id) in rec.getMessage()
        and TX in rec.getMessage()
        for rec in caplog.records
        if rec.levelname == "ERROR"
    )
    caplog.clear()
    with caplog.at_level("ERROR", logger="kasgate.failed_delivery"):
        assert await alert_stale_pending_deliveries(sessionmaker, limit=10) == 0
    assert not any(
        "delivery outcome still pending after the redelivery window" in rec.getMessage() for rec in caplog.records
    )
    assert await sweep_failed_deliveries(sessionmaker, limit=10) == 0
    assert await _credits(sessionmaker) == []


async def test_delivered_and_failed_settlements_are_not_pending_alerts(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    assert (await client.get("/p/weather", headers=await paid_headers(client, TX))).status_code == 200
    _fail_upstream(upstream, 503)
    assert (await client.get("/p/weather", headers=await paid_headers(client, TX2))).status_code == 503
    await _age(sessionmaker, WINDOW_PAST)
    assert await alert_stale_pending_deliveries(sessionmaker, limit=10) == 0
    async with sessionmaker() as session:
        outcomes = {
            row[0]: (row[1], row[2])
            for row in (
                await session.execute(
                    select(Settlement.tx_id, Settlement.delivery_outcome, Settlement.pending_alerted_at)
                )
            ).all()
        }
    assert outcomes[TX] == ("delivered", None)
    assert outcomes[TX2] == ("failed", None)


async def test_ledger_worker_pass_alerts_stale_pending_and_credits_failures(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    stub_engine: StubEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    served = await client.get("/p/weather", headers=await paid_headers(client, TX))
    assert served.status_code == 200
    async with sessionmaker() as session:
        await session.execute(
            update(Settlement).where(Settlement.tx_id == TX).values(delivery_outcome="pending", delivery_failure=None)
        )
        await session.commit()
    _fail_upstream(upstream, 503)
    assert (await client.get("/p/weather", headers=await paid_headers(client, TX2))).status_code == 503
    await _age(sessionmaker, WINDOW_PAST)
    with caplog.at_level("ERROR", logger="kasgate.failed_delivery"):
        assert await run_ledger_pass(sessionmaker, stub_engine, [sweep_pass]) == 2
    assert any(
        "delivery outcome still pending after the redelivery window" in rec.getMessage() for rec in caplog.records
    )
    async with sessionmaker() as session:
        pending = (await session.execute(select(Settlement).where(Settlement.tx_id == TX))).scalar_one()
        failed = (await session.execute(select(Settlement).where(Settlement.tx_id == TX2))).scalar_one()
    assert pending.delivery_outcome == "pending" and pending.pending_alerted_at is not None
    assert failed.delivery_outcome == "credited"
    assert len(await _credits(sessionmaker)) == 1
