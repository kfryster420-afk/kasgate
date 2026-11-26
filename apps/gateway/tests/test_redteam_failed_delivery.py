"""Red-team of failed-delivery auto-credit (feat/failed-delivery-credit @ a9e3ba5, migration 0009).

Tests named ``test_hole_*`` fail on a9e3ba5: each one states an invariant the branch does not keep yet.
Every other test pins down an attack that the branch already stops.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from kaspa import sign_message
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.proxy as proxy_module
from app.failed_delivery import credit_failed_delivery, sweep_failed_deliveries
from app.models import Merchant, PrepaidBalance, Settlement
from app.schemas import HEADER_FAILED_DELIVERY_CREDIT, HEADER_PREPAID_DEBIT
from tests.conftest import (
    OTHER_ADDRESS,
    PAYER_ADDRESS,
    PAYER_KEY,
    Seed,
    SettingsOverride,
    Upstream,
    bearer,
    paid_headers,
    seed_endpoint,
)
from tests.test_failed_delivery_credit import (
    SPLIT,
    TX,
    WINDOW_PAST,
    _age,
    _balance,
    _credits,
    _exhaust,
    _fail_upstream,
    _settlement,
)
from tests.test_prepaid import CHEAP_SOMPI, add_endpoint, debit_header, fund, get_debit_challenge, prepaid_call

POLICY = "/v1/prepaid/failed-delivery-policy"


def assert_identity(balance: PrepaidBalance) -> None:
    assert balance.balance_sompi == (
        balance.deposited_sompi
        + balance.credited_sompi
        - balance.debited_sompi
        - balance.refunded_sompi
        - balance.expired_sompi
    )
    assert 0 <= balance.refund_hold_sompi <= balance.balance_sompi


# ================================================================ HOLE 1: off -> on drops pending credits


async def test_hole_opt_out_then_opt_in_drops_credits_for_calls_paid_while_on(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """A call settles and fails while the policy is on, and waits for the window_expired sweep. The merchant toggles
    off and straight back on: `enabled_at` moves past the settlement, so the sweep never credits it. The spec says
    opting out "does not drop credits for calls that were settled while the policy was on"."""
    _fail_upstream(upstream, 503)
    assert (await client.get("/p/weather", headers=await paid_headers(client, TX))).status_code == 503
    await _age(sessionmaker, WINDOW_PAST)  # settled while on; window and grace are over
    auth = bearer(seeded.api_key)
    assert (await client.put(POLICY, json={"enabled": False}, headers=auth)).status_code == 200
    assert (await client.put(POLICY, json={"enabled": True}, headers=auth)).status_code == 200
    # On a9e3ba5 this is 0: the call is lost.
    assert await sweep_failed_deliveries(sessionmaker, limit=10) == 1


# ================================================================ HOLE 2: an unrecorded success counts as a failure


async def test_hole_successful_delivery_whose_status_was_not_recorded_is_credited(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_record_upstream_status` swallows database errors, and a crash or deploy between the upstream answer and the
    write has the same effect: `upstream_status_code` stays NULL, which the sweep reads as "no response". The buyer
    got the 200 and later gets the credit as well."""

    async def lost_write(session: AsyncSession, settlement_id: uuid.UUID, status_code: int | None) -> None:
        return None

    monkeypatch.setattr(proxy_module, "_record_upstream_status", lost_write)
    served = await client.get("/p/weather", headers=await paid_headers(client, TX))
    assert served.status_code == 200
    monkeypatch.undo()
    await _age(sessionmaker, WINDOW_PAST)
    await sweep_failed_deliveries(sessionmaker, limit=10)
    # On a9e3ba5 the served call is credited with the full merchant share.
    assert await _credits(sessionmaker) == []


# ================================================================ HOLE 3: buyer-controlled "failures"


async def test_hole_oversized_2xx_answer_is_credited(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    override_settings: SettingsOverride,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The upstream did the work and answered 200, but the buyer asked for more than the gateway relays (for
    example `?limit=` large). This counts as "no response" and is credited, so a buyer can make the merchant serve
    heavy calls and get the merchant share back as credit, and with v2.13 refunds, back on-chain."""
    override_settings(redelivery_window_seconds=0)
    monkeypatch.setattr(proxy_module, "MAX_RESPONSE_BODY_BYTES", 1024)
    upstream.handler = lambda request: httpx.Response(200, content=b"x" * 4096)
    response = await client.get("/p/weather", headers=await paid_headers(client, TX))
    assert response.status_code == 502
    assert HEADER_FAILED_DELIVERY_CREDIT not in response.headers
    assert await _credits(sessionmaker) == []


# ================================================================ HOLE 4: credits ignore the prepaid balance cap


async def test_hole_credit_can_push_a_balance_above_the_prepaid_cap(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    override_settings: SettingsOverride,
) -> None:
    override_settings(prepaid_max_balance_sompi=60_000_000)
    _fail_upstream(upstream)
    await _exhaust(client)
    balance = await _balance(sessionmaker, seeded.merchant_id)
    assert balance is not None
    # On a9e3ba5: 1_970_000_000 sompi of credit against a 60_000_000 cap that deposits must respect.
    assert balance.balance_sompi <= 60_000_000


# ================================================================ what held up


async def test_held_upstream_cannot_forge_the_credit_header_on_the_final_attempt(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    forged = "eyJmb3JnZWQiOnRydWV9"
    upstream.handler = lambda request: httpx.Response(503, text="boom", headers={"X-KasGate-Credit": forged})
    responses = await _exhaust(client)
    assert all(r.headers.get(HEADER_FAILED_DELIVERY_CREDIT) != forged for r in responses)
    assert HEADER_FAILED_DELIVERY_CREDIT in responses[-1].headers  # the genuine one
    # The buyer's copy of the header is never forwarded upstream either.
    assert all("x-kasgate-credit" not in {k.lower() for k in r.headers} for r in upstream.requests)


async def test_held_inline_and_sweep_never_credit_twice(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    _fail_upstream(upstream)
    await _exhaust(client)  # attempts_exhausted: credited inline
    await _age(sessionmaker, WINDOW_PAST)
    settlement = await _settlement(sessionmaker)

    async def once() -> object:
        async with sessionmaker() as session:
            return await credit_failed_delivery(session, settlement.id)

    results = await asyncio.gather(
        sweep_failed_deliveries(sessionmaker, limit=10), sweep_failed_deliveries(sessionmaker, limit=10), once()
    )
    assert results[0] == 0 and results[1] == 0 and results[2] is None
    assert len(await _credits(sessionmaker)) == 1


async def test_held_parallel_sweeps_on_a_window_expired_call_credit_once(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    _fail_upstream(upstream, 503)
    assert (await client.get("/p/weather", headers=await paid_headers(client, TX))).status_code == 503
    await _age(sessionmaker, WINDOW_PAST)
    results = await asyncio.gather(*(sweep_failed_deliveries(sessionmaker, limit=10) for _ in range(4)))
    assert sum(results) == 1
    balance = await _balance(sessionmaker, seeded.merchant_id)
    assert balance is not None and balance.credited_sompi == SPLIT.merchant_sompi


async def test_held_no_credit_while_redelivery_is_open_and_redelivery_refused_after_credit(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    _fail_upstream(upstream, 503)
    headers = await paid_headers(client, TX)
    assert (await client.get("/p/weather", headers=headers)).status_code == 503
    await _age(sessionmaker, 300)  # window still open
    assert await sweep_failed_deliveries(sessionmaker, limit=10) == 0
    upstream.handler = None
    assert (await client.get("/p/weather", headers=headers)).status_code == 200  # redelivered
    await _age(sessionmaker, WINDOW_PAST)
    assert await sweep_failed_deliveries(sessionmaker, limit=10) == 0
    assert await _credits(sessionmaker) == []


async def test_held_replaying_the_payment_after_credit_gets_nothing(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    _fail_upstream(upstream)
    headers = await paid_headers(client, TX)
    for _ in range(3):
        await client.get("/p/weather", headers=headers)
    upstream.handler = None
    for _ in range(3):
        replay = await client.get("/p/weather", headers=headers)
        assert replay.status_code == 409 and HEADER_FAILED_DELIVERY_CREDIT not in replay.headers
    fresh = await client.get("/p/weather", headers=await paid_headers(client, TX))  # same tx_id, new challenge
    assert fresh.status_code in (402, 409)
    assert len(await _credits(sessionmaker)) == 1


async def test_held_opt_out_racing_an_in_flight_failure_still_credits_by_settle_time(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    auth = bearer(seeded.api_key)
    seen: list[int] = []
    _fail_upstream(upstream, 503)
    headers = await paid_headers(client, TX)
    await client.get("/p/weather", headers=headers)
    await client.get("/p/weather", headers=headers)
    # The merchant opts out while the last attempt is in flight (settled before the opt-out).
    seen.append((await client.put(POLICY, json={"enabled": False}, headers=auth)).status_code)
    last = await client.get("/p/weather", headers=headers)
    assert seen == [200] and HEADER_FAILED_DELIVERY_CREDIT in last.headers
    # A call settled after the opt-out is not credited.
    later = await _exhaust(client, "ce" * 32)
    assert HEADER_FAILED_DELIVERY_CREDIT not in later[-1].headers
    assert len(await _credits(sessionmaker)) == 1


async def test_held_identity_under_concurrent_credit_debit_and_refund(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await add_endpoint(sessionmaker, seeded.merchant_id, "cheap")
    await add_endpoint(sessionmaker, seeded.merchant_id, "tenth", price_sompi=10_000_000)
    await fund(client)  # 40_000_000 credit
    _fail_upstream(upstream, 503)
    headers = await paid_headers(client, TX)
    for _ in range(2):
        await client.get("/p/weather", headers=headers)
    await _age(sessionmaker, WINDOW_PAST)
    upstream.handler = None
    settlement = await _settlement_for(sessionmaker, TX)
    challenge = (
        await client.post(
            "/v1/prepaid/refunds/challenge",
            json={"slug": "cheap", "payer_address": PAYER_ADDRESS, "amount_sompi": 20_000_000},
        )
    ).json()
    debits = [await get_debit_challenge(client, slug="tenth") for _ in range(3)]

    async def credit() -> object:
        async with sessionmaker() as session:
            return await credit_failed_delivery(session, settlement.id)

    await asyncio.gather(
        credit(),
        client.post(
            "/v1/prepaid/refunds",
            json={"nonce": challenge["nonce"], "signature": sign_message(challenge["message"], PAYER_KEY)},
        ),
        *(client.get("/p/tenth", headers={HEADER_PREPAID_DEBIT: debit_header(d)}) for d in debits),
    )
    balance = await _balance(sessionmaker, seeded.merchant_id)
    assert balance is not None
    assert balance.credited_sompi == SPLIT.merchant_sompi
    assert_identity(balance)
    assert balance.refund_hold_sompi + balance.debited_sompi <= 40_000_000 + SPLIT.merchant_sompi


async def test_held_credited_balance_is_spendable_on_calls_not_refundable_and_identity_holds(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Decision 3: failed-delivery credit is call-only. The earlier expectation (credit refundable on-chain in
    full) no longer holds; a refund may take only deposited and unspent sompi."""
    await add_endpoint(sessionmaker, seeded.merchant_id, "cheap")
    _fail_upstream(upstream)
    await _exhaust(client)
    balance = await _balance(sessionmaker, seeded.merchant_id)
    assert balance is not None
    assert balance.credited_sompi == balance.credit_balance_sompi == SPLIT.merchant_sompi
    assert balance.refunded_sompi == 0
    assert_identity(balance)
    # refundable = balance - hold - unspent credit = 0: a request for the credit (or the default amount) is refused.
    too_much = await client.post(
        "/v1/prepaid/refunds/challenge",
        json={"slug": "cheap", "payer_address": PAYER_ADDRESS, "amount_sompi": SPLIT.merchant_sompi},
    )
    assert too_much.status_code == 409 and too_much.json()["error"] == "insufficient_balance"
    default = await client.post("/v1/prepaid/refunds/challenge", json={"slug": "cheap", "payer_address": PAYER_ADDRESS})
    assert default.status_code == 422 and default.json()["error"] == "refund_too_small"
    # The credit still pays calls.
    upstream.handler = None
    paid = await prepaid_call(client)
    assert paid.status_code == 200, paid.text
    spent = await _balance(sessionmaker, seeded.merchant_id)
    assert spent is not None
    assert spent.credit_balance_sompi == SPLIT.merchant_sompi - CHEAP_SOMPI
    assert spent.debited_sompi == CHEAP_SOMPI
    assert spent.refunded_sompi == 0
    assert_identity(spent)


async def test_held_credit_lands_after_overdue_credit_expires(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await add_endpoint(sessionmaker, seeded.merchant_id, "cheap")
    await client.patch("/v1/merchants/me", json={"prepaid_expiry_days": 1}, headers=bearer(seeded.api_key))
    await fund(client)
    async with sessionmaker() as session:
        await session.execute(update(PrepaidBalance).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        await session.commit()
    _fail_upstream(upstream)
    await _exhaust(client)
    balance = await _balance(sessionmaker, seeded.merchant_id)
    assert balance is not None
    assert (balance.expired_sompi, balance.balance_sompi) == (40_000_000, SPLIT.merchant_sompi)
    assert balance.expires_at is not None and balance.expires_at > datetime.now(UTC)
    assert_identity(balance)


async def _settlement_for(maker: async_sessionmaker[AsyncSession], tx_id: str) -> Settlement:
    async with maker() as session:
        return (await session.execute(select(Settlement).where(Settlement.tx_id == tx_id))).scalar_one()


async def test_held_policy_of_another_merchant_is_not_touched(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    other = await seed_endpoint(sessionmaker, slug="elsewhere", address=OTHER_ADDRESS)
    assert (await client.put(POLICY, json={"enabled": False}, headers=bearer(other.api_key))).status_code == 200
    async with sessionmaker() as session:
        mine = (await session.execute(select(Merchant).where(Merchant.id == seeded.merchant_id))).scalar_one()
    assert mine.failed_delivery_credit_disabled_at is None
