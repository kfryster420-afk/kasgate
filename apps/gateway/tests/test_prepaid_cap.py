"""Prepaid balance cap at deposit time (05's MEDIUM): re-check under a row lock before submit, plus a backstop."""

import asyncio
import json
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import PrepaidBalance, PrepaidDeposit, Settlement
from app.proxy import DEAD_LETTER_KEY, lock_key, nonce_lock_key
from app.schemas import PaymentSignature
from tests.conftest import Seed, StubEngine
from tests.test_prepaid import CREDIT_SOMPI, DEPOSIT_SOMPI, fund, post_deposit, quote_deposit, shop  # noqa: F401


async def _balance(maker: async_sessionmaker[AsyncSession]) -> int | None:
    async with maker() as session:
        return (await session.execute(select(PrepaidBalance.balance_sompi))).scalar_one_or_none()


async def _count(maker: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with maker() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def test_over_cap_deposit_is_refused_before_submit_and_frees_its_locks(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    stub_engine: StubEngine,
    redis: Any,
    override_settings: Any,
) -> None:
    override_settings(prepaid_max_balance_sompi=60_000_000)
    first = await quote_deposit(client)
    second = await quote_deposit(client)
    assert (await post_deposit(client, first, tx_id="b1" * 32)).status_code == 201

    over = await post_deposit(client, second, tx_id="b2" * 32)

    assert over.status_code == 422, over.text
    assert over.json()["error"] == "prepaid_cap_exceeded"
    assert stub_engine.resolve_calls == ["b1" * 32] and stub_engine.calls == ["b1" * 32]  # never reached the node
    assert await redis.get(lock_key("b2" * 32)) is None
    assert await redis.get(nonce_lock_key(second.required.nonce)) is None
    assert await _balance(sessionmaker) == CREDIT_SOMPI
    assert await _count(sessionmaker, Settlement) == 1


async def test_first_deposit_over_the_cap_leaves_no_zero_balance_row(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    stub_engine: StubEngine,
    override_settings: Any,
) -> None:
    quote = await quote_deposit(client, 120_000_000)  # credit 80_000_000 with the cap at its default
    override_settings(prepaid_max_balance_sompi=60_000_000)  # the cap drops between quote and deposit

    response = await post_deposit(client, quote, tx_id="b3" * 32)

    assert response.status_code == 422, response.text
    assert stub_engine.calls == []
    assert await _count(sessionmaker, PrepaidBalance) == 0  # the lock row was rolled back with the refusal


async def test_concurrent_deposits_serialize_on_the_balance_row(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    stub_engine: StubEngine,
    override_settings: Any,
) -> None:
    override_settings(prepaid_max_balance_sompi=60_000_000)
    quotes = [await quote_deposit(client) for _ in range(3)]
    stub_engine.delay = 0.2  # each settle holds the row lock long enough for the others to queue on it

    responses = await asyncio.gather(*(post_deposit(client, q, tx_id=f"c{i}" * 32) for i, q in enumerate(quotes)))

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [201, 422, 422], [r.text for r in responses]
    assert len(stub_engine.calls) == 1  # only the winner was submitted
    assert await _balance(sessionmaker) == CREDIT_SOMPI


async def test_backstop_dead_letters_a_settled_deposit_that_would_pass_the_cap(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    stub_engine: StubEngine,
    redis: Any,
    override_settings: Any,
) -> None:
    override_settings(prepaid_max_balance_sompi=100_000_000)
    await fund(client)
    quote = await quote_deposit(client)

    async def lower_cap(signature: PaymentSignature) -> None:
        override_settings(prepaid_max_balance_sompi=60_000_000)  # cap moves after the pre-submit check passed

    stub_engine.before_submit = lower_cap
    response = await post_deposit(client, quote, tx_id="d1" * 32)

    assert response.status_code == 503, response.text
    assert "could not be credited" in response.json()["message"]
    assert stub_engine.calls[-1] == "d1" * 32
    assert await _balance(sessionmaker) == CREDIT_SOMPI
    assert await _count(sessionmaker, PrepaidDeposit) == 1
    letters = [json.loads(x) for x in await redis.lrange(DEAD_LETTER_KEY, 0, -1)]
    assert len(letters) == 1
    assert letters[0]["reason"] == "prepaid_cap_exceeded" and letters[0]["creditSompi"] == CREDIT_SOMPI
    assert letters[0]["prepaidDeposit"]["txId"] == "d1" * 32
