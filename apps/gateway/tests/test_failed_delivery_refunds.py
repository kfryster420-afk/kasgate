"""CONTRACT v2.14 x v2.13: failed-delivery credit is call-only and never reaches an on-chain refund.

refundable = balance - refund/cooldown holds - unspent failed-delivery credit (deposited and unspent only, never
negative). Calls spend the credit first; a refund or cancel-cooldown hold never covers it; a late payout re-debits
deposited sompi first; expiry drops it.
"""

import base64
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ledger import expire_due_balances, refundable_sompi, run_ledger_pass, spendable_sompi
from app.models import PrepaidBalance, PrepaidDebit, PrepaidLedgerEntry
from app.schemas import HEADER_CREDIT_SUMMARY, BuyerCreditSummary, compute_fee_split
from tests.conftest import (
    PAYER_ADDRESS,
    PRICE_SOMPI,
    Seed,
    SettingsOverride,
    StubEngine,
    Upstream,
    paid_headers,
    seed_endpoint,
)
from tests.test_prepaid import CHEAP_SOMPI, CREDIT_SOMPI, add_endpoint, fund, prepaid_call
from tests.test_refund_hardening import age, cancel, claim
from tests.test_refunds import MIN_REFUND, PAYOUT_TX, challenge_of, open_refund, payout, refund_challenge, signed

TX = "cd" * 32
SPLIT = compute_fee_split(PRICE_SOMPI)
BIG_SOMPI = 15_000_000
# PREPAID_MAX_BALANCE_SOMPI that leaves exactly 20_000_000 sompi of room above one 40_000_000 deposit.
CAP = 60_000_000
CAPPED_CREDIT = CAP - CREDIT_SOMPI


@pytest.fixture
async def shop(sessionmaker: async_sessionmaker[AsyncSession]) -> Seed:
    seed = await seed_endpoint(sessionmaker)
    await add_endpoint(sessionmaker, seed.merchant_id, "cheap")
    await add_endpoint(sessionmaker, seed.merchant_id, "big", price_sompi=BIG_SOMPI)
    return seed


def _fail_upstream(upstream: Upstream) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    upstream.handler = handler


async def _credit_weather(client: httpx.AsyncClient, upstream: Upstream) -> None:
    """Three failed deliveries of one paid `weather` call: the merchant share becomes failed-delivery credit."""
    _fail_upstream(upstream)
    headers = await paid_headers(client, TX, "weather")
    responses = [await client.get("/p/weather", headers=headers) for _ in range(3)]
    assert [r.status_code for r in responses] == [502, 502, 502]
    upstream.handler = None


async def _balance(maker: async_sessionmaker[AsyncSession], merchant_id: uuid.UUID) -> PrepaidBalance:
    async with maker() as session:
        return (
            await session.execute(select(PrepaidBalance).where(PrepaidBalance.merchant_id == merchant_id))
        ).scalar_one()


def _identity(row: PrepaidBalance) -> None:
    assert row.balance_sompi == (
        row.deposited_sompi + row.credited_sompi - row.debited_sompi - row.refunded_sompi - row.expired_sompi
    )
    assert 0 <= row.refund_hold_sompi <= row.balance_sompi
    assert 0 <= row.credit_balance_sompi <= min(row.credited_sompi, row.balance_sompi - row.refund_hold_sompi)


async def test_a_refund_takes_only_deposited_unspent_credit_and_its_hold_never_covers_credit(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    await _credit_weather(client, upstream)
    row = await _balance(sessionmaker, shop.merchant_id)
    assert (row.balance_sompi, row.credit_balance_sompi) == (CREDIT_SOMPI + SPLIT.merchant_sompi, SPLIT.merchant_sompi)
    assert (refundable_sompi(row), spendable_sompi(row)) == (CREDIT_SOMPI, CREDIT_SOMPI + SPLIT.merchant_sompi)

    challenge = await challenge_of(client)
    assert challenge.amount_sompi == str(CREDIT_SOMPI)  # the default amount is the refundable part only
    assert challenge.refundable_sompi == str(CREDIT_SOMPI)
    assert challenge.spendable_sompi == str(CREDIT_SOMPI + SPLIT.merchant_sompi)
    too_much = await refund_challenge(client, amount=CREDIT_SOMPI + 1)
    assert too_much.status_code == 409 and too_much.json()["error"] == "insufficient_balance"

    refund = await open_refund(client)
    held = await _balance(sessionmaker, shop.merchant_id)
    assert (held.refund_hold_sompi, held.credit_balance_sompi) == (CREDIT_SOMPI, SPLIT.merchant_sompi)
    assert refundable_sompi(held) == 0
    _identity(held)
    # The hold covers deposited sompi only: the credit still pays calls while the refund is open.
    assert (await prepaid_call(client)).status_code == 200
    spent = await _balance(sessionmaker, shop.merchant_id)
    assert spent.credit_balance_sompi == SPLIT.merchant_sompi - 1_000
    assert spent.refund_hold_sompi == CREDIT_SOMPI
    _identity(spent)

    paid = await payout(client, shop, refund.id, tx_id=PAYOUT_TX)
    assert paid.status_code == 200, paid.text
    after = await _balance(sessionmaker, shop.merchant_id)
    assert (after.refunded_sompi, after.refund_hold_sompi) == (CREDIT_SOMPI, 0)
    assert after.balance_sompi == after.credit_balance_sompi == SPLIT.merchant_sompi - 1_000
    assert refundable_sompi(after) == 0
    _identity(after)
    # Nothing refundable is left, so no further refund can be opened.
    assert (await refund_challenge(client)).status_code == 422


async def test_a_credit_only_balance_cannot_be_refunded(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _credit_weather(client, upstream)
    row = await _balance(sessionmaker, shop.merchant_id)
    assert (row.deposited_sompi, row.balance_sompi, refundable_sompi(row)) == (0, SPLIT.merchant_sompi, 0)
    default = await refund_challenge(client)
    assert default.status_code == 422 and default.json()["error"] == "refund_too_small"
    explicit = await refund_challenge(client, amount=MIN_REFUND)
    assert explicit.status_code == 409 and explicit.json()["error"] == "insufficient_balance"
    assert "calls only" in explicit.json()["message"]


async def test_calls_spend_credit_before_deposited_sompi(
    client: httpx.AsyncClient,
    shop: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    override_settings: SettingsOverride,
) -> None:
    await fund(client)
    override_settings(prepaid_max_balance_sompi=CAP)
    await _credit_weather(client, upstream)
    row = await _balance(sessionmaker, shop.merchant_id)
    assert row.credit_balance_sompi == CAPPED_CREDIT  # clamped to the cap (shortfall recorded elsewhere)
    assert (await prepaid_call(client, slug="big")).status_code == 200
    assert (await prepaid_call(client, slug="big")).status_code == 200
    async with sessionmaker() as session:
        debits = list((await session.execute(select(PrepaidDebit).order_by(PrepaidDebit.created_at))).scalars())
    assert [(d.amount_sompi, d.credit_sompi) for d in debits] == [
        (BIG_SOMPI, BIG_SOMPI),
        (BIG_SOMPI, CAPPED_CREDIT - BIG_SOMPI),
    ]
    after = await _balance(sessionmaker, shop.merchant_id)
    assert after.credit_balance_sompi == 0
    assert refundable_sompi(after) == CREDIT_SOMPI - (2 * BIG_SOMPI - CAPPED_CREDIT)
    _identity(after)


async def test_a_late_payout_re_debits_deposited_credit_before_failed_delivery_credit(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    refund = await open_refund(client, MIN_REFUND)
    cancel = await challenge_of(client, action="cancel", refund_id=refund.id)
    assert (await client.post(f"/v1/prepaid/refunds/{refund.id}/cancel", json=signed(cancel))).status_code == 200
    # Two big calls before any credit: 30_000_000 of the 40_000_000 deposit is spent, 10_000_000 is left.
    for _ in range(2):
        assert (await prepaid_call(client, slug="big")).status_code == 200
    await _credit_weather(client, upstream)
    before = await _balance(sessionmaker, shop.merchant_id)
    deposited_left = CREDIT_SOMPI - 2 * BIG_SOMPI
    assert before.balance_sompi - before.credit_balance_sompi == deposited_left

    late = await payout(client, shop, refund.id, tx_id=PAYOUT_TX)
    assert late.status_code == 200, late.text
    assert (late.json()["late_paid_from"], late.json()["shortfall_sompi"]) == ("cancelled", "0")
    after = await _balance(sessionmaker, shop.merchant_id)
    assert after.refunded_sompi == MIN_REFUND
    # The 10_000_000 deposited sompi go first; only the other 10_000_000 come out of the credit.
    assert after.credit_balance_sompi == SPLIT.merchant_sompi - (MIN_REFUND - deposited_left)
    assert after.balance_sompi == after.credit_balance_sompi
    assert refundable_sompi(after) == 0
    _identity(after)


async def test_expiry_drops_unspent_credit_but_keeps_the_refund_hold(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    await _credit_weather(client, upstream)
    await open_refund(client, MIN_REFUND)
    async with sessionmaker() as session:
        row = (await session.execute(select(PrepaidBalance).with_for_update())).scalar_one()
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    async with sessionmaker() as session:
        assert await expire_due_balances(session, 10) == 1
    after = await _balance(sessionmaker, shop.merchant_id)
    assert (after.balance_sompi, after.refund_hold_sompi, after.credit_balance_sompi) == (MIN_REFUND, MIN_REFUND, 0)
    assert after.expired_sompi == CREDIT_SOMPI - MIN_REFUND + SPLIT.merchant_sompi
    _identity(after)


async def test_signed_refund_status_carries_the_buyers_credit_summary(
    client: httpx.AsyncClient,
    shop: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    override_settings: SettingsOverride,
) -> None:
    await fund(client)
    override_settings(prepaid_max_balance_sompi=CAP)
    await _credit_weather(client, upstream)
    status = await challenge_of(client, action="status")
    reply = await client.post("/v1/prepaid/refunds/status", json=signed(status))
    assert reply.status_code == 200, reply.text
    assert reply.json() == []  # the body stays v2.13's list of refunds
    summary = BuyerCreditSummary.model_validate_json(base64.b64decode(reply.headers[HEADER_CREDIT_SUMMARY]))
    assert summary.merchantId == shop.merchant_id
    assert (summary.creditedSompi, summary.creditBalanceSompi, summary.refundableSompi) == (
        str(CAPPED_CREDIT),
        str(CAPPED_CREDIT),
        str(CREDIT_SOMPI),
    )
    assert summary.creditCount == 1
    [item] = summary.credits
    assert (item.txId, item.slug, item.reason) == (TX, "weather", "attempts_exhausted")
    assert (item.creditSompi, item.shortfallSompi) == (
        str(CAPPED_CREDIT),
        str(SPLIT.merchant_sompi - CAPPED_CREDIT),
    )
    async with sessionmaker() as session:
        [entry] = list(
            (
                await session.execute(
                    select(PrepaidLedgerEntry).where(PrepaidLedgerEntry.kind == "failed_delivery_credit")
                )
            ).scalars()
        )
    assert entry.shortfall_sompi == SPLIT.merchant_sompi - CAPPED_CREDIT
    # The public, unsigned view carries no credit summary.
    public = await client.get("/v1/prepaid/refunds/status", params={"slug": "cheap", "payer_address": PAYER_ADDRESS})
    assert public.status_code == 200 and HEADER_CREDIT_SUMMARY not in public.headers


LAPSED = timedelta(hours=1)


async def test_a_cooldown_hold_blocks_another_refund_while_credit_still_pays_calls(
    client: httpx.AsyncClient,
    shop: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    stub_engine: StubEngine,
) -> None:
    """A cancel after a lapsed claim keeps the deposit in `refund_hold_sompi` until hold_until.

    refundable = balance - that hold - unspent credit (0 here). Spendable still includes the failed-delivery
    credit, so calls go through during the cooldown. After the worker releases the hold, the deposit is
    refundable again.
    """
    await fund(client)
    await _credit_weather(client, upstream)
    refund = await open_refund(client)
    assert refund.amount_sompi == str(CREDIT_SOMPI)
    assert (await claim(client, shop, refund.id)).status_code == 200
    await age(sessionmaker, refund.id, claimed_at=datetime.now(UTC) - LAPSED)
    cancelled = await cancel(client, refund.id)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled" and cancelled.json()["hold_until"] is not None
    held = await _balance(sessionmaker, shop.merchant_id)
    assert held.refund_hold_sompi == CREDIT_SOMPI
    assert spendable_sompi(held) == SPLIT.merchant_sompi
    assert refundable_sompi(held) == 0
    _identity(held)
    refused = await refund_challenge(client, amount=MIN_REFUND)
    assert refused.status_code == 409 and refused.json()["error"] == "insufficient_balance"
    assert (await prepaid_call(client)).status_code == 200
    spent = await _balance(sessionmaker, shop.merchant_id)
    assert spent.credit_balance_sompi == SPLIT.merchant_sompi - CHEAP_SOMPI
    assert spent.refund_hold_sompi == CREDIT_SOMPI
    assert refundable_sompi(spent) == 0
    _identity(spent)
    await run_ledger_pass(sessionmaker, stub_engine)  # cooldown not due yet
    assert (await _balance(sessionmaker, shop.merchant_id)).refund_hold_sompi == CREDIT_SOMPI
    await age(sessionmaker, refund.id, hold_until=datetime.now(UTC) - timedelta(seconds=1))
    await run_ledger_pass(sessionmaker, stub_engine)
    after = await _balance(sessionmaker, shop.merchant_id)
    assert after.refund_hold_sompi == 0
    assert spendable_sompi(after) == CREDIT_SOMPI + SPLIT.merchant_sompi - CHEAP_SOMPI
    assert refundable_sompi(after) == CREDIT_SOMPI
    _identity(after)
