"""PR #6 batch: 05's privacy residuals (unsigned quote/receipt/cap/refund-challenge bodies carry no balance or credit
figures), the shared in-flight cap rule on quotes, the orphaned in-flight debit sweeper, and single-use signatures
on GET /v1/prepaid/refunds/status."""

import asyncio
import hashlib
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.debit_sweeper import DebitSweeper, sweep_orphaned_debits
from app.models import (
    PREPAID_DEBIT_DEBITED,
    PREPAID_DEBIT_REFUNDED,
    Endpoint,
    PrepaidBalance,
    PrepaidDebit,
)
from app.prepaid import CAP_EXCEEDED_MESSAGE
from app.refunds import SIGNATURE_REPLAY_PREFIX
from tests.conftest import PAYER_ADDRESS, Seed, StubEngine
from tests.test_prepaid import CREDIT_SOMPI, DEPOSIT_SOMPI, fund, post_deposit, quote_deposit, shop  # noqa: F401
from tests.test_prepaid_balance_privacy import refunds_status_headers, signed_headers
from tests.test_refunds import MIN_REFUND, challenge_of

AGE = 900  # the default PREPAID_DEBIT_ORPHAN_AGE_SECONDS
FIGURES = re.compile(r"\d{6,}")  # any sompi-sized number


def no_figures(text: str) -> None:
    assert not FIGURES.search(text), f"unsigned text carries a figure: {text}"


async def _balance_row(maker: async_sessionmaker[AsyncSession]) -> PrepaidBalance:
    async with maker() as session:
        return (await session.execute(select(PrepaidBalance))).scalar_one()


async def add_debit(
    maker: async_sessionmaker[AsyncSession],
    *,
    amount: int = 10_000_000,
    age_seconds: float = 0,
    status_code: int | None = None,
    status: str = PREPAID_DEBIT_DEBITED,
) -> uuid.UUID:
    """A prepaid debit row as the proxy writes it: balance moved to `debited_sompi`, upstream status not yet known."""
    async with maker() as session:
        balance = (await session.execute(select(PrepaidBalance))).scalar_one()
        endpoint = (await session.execute(select(Endpoint))).scalars().first()
        assert endpoint is not None
        debit = PrepaidDebit(
            balance_id=balance.id,
            merchant_id=balance.merchant_id,
            endpoint_id=endpoint.id,
            nonce=uuid.uuid4().hex,
            payer_address=PAYER_ADDRESS,
            amount_sompi=amount,
            status=status,
            request_method="GET",
            request_path="/p/cheap",
            upstream_status_code=status_code,
            created_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
            refunded_at=datetime.now(UTC) if status == PREPAID_DEBIT_REFUNDED else None,
        )
        session.add(debit)
        if status == PREPAID_DEBIT_DEBITED:
            await session.execute(
                update(PrepaidBalance)
                .where(PrepaidBalance.id == balance.id)
                .values(
                    balance_sompi=PrepaidBalance.balance_sompi - amount,
                    debited_sompi=PrepaidBalance.debited_sompi + amount,
                )
            )
        await session.commit()
        return debit.id


async def _debit(maker: async_sessionmaker[AsyncSession], debit_id: uuid.UUID) -> PrepaidDebit:
    async with maker() as session:
        return (await session.execute(select(PrepaidDebit).where(PrepaidDebit.id == debit_id))).scalar_one()


# ---------------------------------------------------------------- 2a: unsigned quote and receipt


async def test_unsigned_quote_and_receipt_carry_no_balance(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
) -> None:
    await fund(client)  # balance 40_000_000
    quote = await client.post(
        "/v1/prepaid/deposits/quote",
        json={"slug": "cheap", "payer_address": PAYER_ADDRESS, "amount_sompi": DEPOSIT_SOMPI},
    )
    assert quote.status_code == 201, quote.text
    assert set(quote.json()) == {
        "slug", "merchant_id", "payer_address", "deposit_sompi", "fee_sompi", "credit_sompi", "required", "deposit_url",
    }  # fmt: skip
    assert str(2 * CREDIT_SOMPI) not in quote.text and "balance" not in quote.text
    parsed = await quote_deposit(client)
    receipt = await post_deposit(client, parsed, tx_id="a1" * 32)
    assert receipt.status_code == 201, receipt.text
    assert set(receipt.json()) == {
        "deposit_id", "tx_id", "slug", "merchant_id", "payer_address", "deposit_sompi", "fee_sompi", "credit_sompi",
        "created_at",
    }  # fmt: skip
    assert str(2 * CREDIT_SOMPI) not in receipt.text and "balance" not in receipt.text
    again = await post_deposit(client, parsed, tx_id="a1" * 32)  # idempotent replay of the same receipt
    assert again.status_code == 200 and "balance" not in again.text and str(2 * CREDIT_SOMPI) not in again.text


# ---------------------------------------------------------------- 2b: generic cap text; A: shared in-flight rule


async def test_cap_exceeded_text_is_generic_for_quote_and_deposit(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    override_settings: Any,
) -> None:
    override_settings(prepaid_max_balance_sompi=100_000_000)
    await fund(client)
    stale = await quote_deposit(client)
    override_settings(prepaid_max_balance_sompi=60_000_000)
    quote = await client.post(
        "/v1/prepaid/deposits/quote",
        json={"slug": "cheap", "payer_address": PAYER_ADDRESS, "amount_sompi": DEPOSIT_SOMPI},
    )
    deposit = await post_deposit(client, stale, tx_id="a2" * 32)
    for response in (quote, deposit):
        assert response.status_code == 422, response.text
        assert response.json()["error"] == "prepaid_cap_exceeded"
        assert response.json()["message"] == CAP_EXCEEDED_MESSAGE
        no_figures(response.text)
        assert "headroom" not in response.text and "current" not in response.text


async def test_quote_counts_in_flight_debits_like_deposit(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    override_settings: Any,
) -> None:
    override_settings(prepaid_max_balance_sompi=75_000_000)
    await fund(client)  # balance 40M
    in_flight = await add_debit(sessionmaker, amount=10_000_000)  # balance 30M + 10M in flight + 40M credit > 75M
    body = {"slug": "cheap", "payer_address": PAYER_ADDRESS, "amount_sompi": DEPOSIT_SOMPI}
    refused = await client.post("/v1/prepaid/deposits/quote", json=body)
    assert refused.status_code == 422 and refused.json()["error"] == "prepaid_cap_exceeded", refused.text
    # Once the call has completed (upstream status recorded) the 10M is spent: 30M + 40M <= 75M.
    async with sessionmaker() as session:
        await session.execute(update(PrepaidDebit).where(PrepaidDebit.id == in_flight).values(upstream_status_code=200))
        await session.commit()
    assert (await client.post("/v1/prepaid/deposits/quote", json=body)).status_code == 201


# ---------------------------------------------------------------- 2c: refunds/challenge


async def test_unsigned_refund_challenge_without_amount_needs_a_signed_read(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await fund(client)
    async with sessionmaker() as session:  # 5M of the balance is call-only failed-delivery credit
        await session.execute(
            update(PrepaidBalance).values(
                balance_sompi=PrepaidBalance.balance_sompi + 5_000_000,
                credit_balance_sompi=5_000_000,
                credited_sompi=5_000_000,
            )
        )
        await session.commit()
    body = {"slug": "cheap", "payer_address": PAYER_ADDRESS, "action": "request"}
    unsigned = await client.post("/v1/prepaid/refunds/challenge", json=body)
    assert unsigned.status_code == 422 and unsigned.json()["error"] == "refund_amount_required", unsigned.text
    no_figures(unsigned.text)

    bad = await client.post("/v1/prepaid/refunds/challenge", json=body, headers=signed_headers(slug="other-slug"))
    assert bad.status_code == 401 and bad.json()["error"] == "invalid_balance_signature"
    purpose_mixup = await client.post("/v1/prepaid/refunds/challenge", json=body, headers=refunds_status_headers())
    assert purpose_mixup.status_code == 401  # a refunds-status signature is not a balance-read signature

    ok = await client.post("/v1/prepaid/refunds/challenge", json=body, headers=signed_headers())
    assert ok.status_code == 201, ok.text
    assert ok.json()["amount_sompi"] == str(CREDIT_SOMPI)  # refundable only: credit excluded
    assert "spendable_sompi" not in ok.json() and "refundable_sompi" not in ok.json()


async def test_unsigned_refund_challenge_errors_carry_no_figures(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
) -> None:
    await fund(client)
    too_small = await client.post(
        "/v1/prepaid/refunds/challenge",
        json={"slug": "cheap", "payer_address": PAYER_ADDRESS, "action": "request", "amount_sompi": 1},
    )
    assert too_small.status_code == 422 and too_small.json()["error"] == "refund_too_small"
    too_big = await client.post(
        "/v1/prepaid/refunds/challenge",
        json={"slug": "cheap", "payer_address": PAYER_ADDRESS, "action": "request", "amount_sompi": 10 * CREDIT_SOMPI},
    )
    assert too_big.status_code == 409 and too_big.json()["error"] == "insufficient_balance"
    no_figures(too_big.text)
    assert str(CREDIT_SOMPI) not in too_big.text
    # An amount inside the public spendable total is issued; the refundable cap is enforced on the signed POST.
    challenge = await challenge_of(client, amount=MIN_REFUND)
    assert challenge.amount_sompi == str(MIN_REFUND)


async def test_a_signed_default_for_a_key_with_nothing_refundable_is_a_generic_422(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
) -> None:
    response = await client.post(
        "/v1/prepaid/refunds/challenge",
        json={"slug": "cheap", "payer_address": PAYER_ADDRESS, "action": "request"},
        headers=signed_headers(),
    )
    assert response.status_code == 422 and response.json()["error"] == "refund_too_small"
    no_figures(response.text.replace(str(MIN_REFUND), ""))  # the minimum is a public constant


# ---------------------------------------------------------------- B: orphan sweeper


async def test_orphaned_debit_is_refunded_once_and_a_second_sweep_is_a_no_op(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Any,
) -> None:
    await fund(client)
    orphan = await add_debit(sessionmaker, amount=10_000_000, age_seconds=AGE + 60)
    before = await _balance_row(sessionmaker)
    assert (before.balance_sompi, before.debited_sompi) == (CREDIT_SOMPI - 10_000_000, 10_000_000)

    assert await sweep_orphaned_debits(sessionmaker, redis, age_seconds=AGE, batch_size=10) == 1
    row = await _debit(sessionmaker, orphan)
    assert row.status == PREPAID_DEBIT_REFUNDED and row.refunded_at is not None
    after = await _balance_row(sessionmaker)
    assert (after.balance_sompi, after.debited_sompi) == (CREDIT_SOMPI, 0)

    assert await sweep_orphaned_debits(sessionmaker, redis, age_seconds=AGE, batch_size=10) == 0
    again = await _balance_row(sessionmaker)
    assert (again.balance_sompi, again.debited_sompi) == (CREDIT_SOMPI, 0)  # not credited twice


async def test_two_concurrent_sweeps_refund_an_orphan_once(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Any,
) -> None:
    await fund(client)
    await add_debit(sessionmaker, amount=10_000_000, age_seconds=AGE + 60)
    counts = await asyncio.gather(
        *(sweep_orphaned_debits(sessionmaker, redis, age_seconds=AGE, batch_size=10) for _ in range(4))
    )
    assert sum(counts) == 1, counts
    after = await _balance_row(sessionmaker)
    assert (after.balance_sompi, after.debited_sompi) == (CREDIT_SOMPI, 0)


async def test_fresh_in_flight_and_completed_rows_are_left_alone(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Any,
) -> None:
    await fund(client)
    fresh = await add_debit(sessionmaker, amount=1_000_000, age_seconds=AGE - 120)  # a live request, younger than N
    done_old = await add_debit(sessionmaker, amount=2_000_000, age_seconds=AGE * 4, status_code=200)
    refunded_old = await add_debit(
        sessionmaker, amount=3_000_000, age_seconds=AGE * 4, status_code=503, status=PREPAID_DEBIT_REFUNDED
    )
    before = await _balance_row(sessionmaker)

    assert await sweep_orphaned_debits(sessionmaker, redis, age_seconds=AGE, batch_size=10) == 0

    assert (await _debit(sessionmaker, fresh)).status == PREPAID_DEBIT_DEBITED
    assert (await _debit(sessionmaker, done_old)).status == PREPAID_DEBIT_DEBITED  # finished: status code recorded
    assert (await _debit(sessionmaker, refunded_old)).status == PREPAID_DEBIT_REFUNDED
    after = await _balance_row(sessionmaker)
    assert (after.balance_sompi, after.debited_sompi) == (before.balance_sompi, before.debited_sompi)


async def test_a_debit_that_finishes_between_scan_and_refund_is_not_refunded(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Any,
) -> None:
    """The refund itself re-checks `upstream_status_code IS NULL` and the age, so a live request that records its
    status after the sweeper's read is never refunded under its own feet."""
    from app.proxy import refund_prepaid_debit

    await fund(client)
    debit = await add_debit(sessionmaker, amount=10_000_000, age_seconds=AGE + 60)
    async with sessionmaker() as session:
        await session.execute(update(PrepaidDebit).where(PrepaidDebit.id == debit).values(upstream_status_code=200))
        await session.commit()
    async with sessionmaker() as session:
        assert await refund_prepaid_debit(session, redis, debit, None, orphaned_for_seconds=AGE) is None
    young = await add_debit(sessionmaker, amount=1_000_000, age_seconds=5)
    async with sessionmaker() as session:
        assert await refund_prepaid_debit(session, redis, young, None, orphaned_for_seconds=AGE) is None
    assert (await _debit(sessionmaker, debit)).status == PREPAID_DEBIT_DEBITED


async def test_the_sweeper_task_refunds_in_the_background_and_stops_cleanly(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Any,
    override_settings: Any,
) -> None:
    override_settings(prepaid_debit_sweeper_interval_seconds="0.05")
    await fund(client)
    orphan = await add_debit(sessionmaker, amount=10_000_000, age_seconds=AGE + 60)
    sweeper = DebitSweeper(sessionmaker, redis)
    sweeper.start()
    try:
        for _ in range(100):
            if (await _debit(sessionmaker, orphan)).status == PREPAID_DEBIT_REFUNDED:
                break
            await asyncio.sleep(0.05)
        assert (await _debit(sessionmaker, orphan)).status == PREPAID_DEBIT_REFUNDED
    finally:
        await sweeper.stop()
    assert sweeper._task is None  # noqa: SLF001


def test_orphan_age_must_stay_well_above_the_upstream_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UPSTREAM_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("PREPAID_DEBIT_ORPHAN_AGE_SECONDS", "300")  # < 3 x 120 + 60
    with pytest.raises(ValueError, match="PREPAID_DEBIT_ORPHAN_AGE_SECONDS"):
        Settings()
    monkeypatch.setenv("PREPAID_DEBIT_ORPHAN_AGE_SECONDS", "420")
    assert Settings().prepaid_debit_orphan_age_seconds == 420


def test_sweeper_defaults() -> None:
    settings = Settings()
    assert settings.prepaid_debit_sweeper_enabled is True
    assert settings.prepaid_debit_sweeper_interval_seconds == 60
    assert settings.prepaid_debit_orphan_age_seconds == 900
    assert settings.prepaid_debit_sweeper_batch_size == 100


# ---------------------------------------------------------------- C: single-use refunds-status signatures


async def _status(client: httpx.AsyncClient, headers: dict[str, str]) -> httpx.Response:
    return await client.get(
        "/v1/prepaid/refunds/status", params={"slug": "cheap", "payer_address": PAYER_ADDRESS}, headers=headers
    )


async def test_a_refunds_status_signature_works_once_and_a_replay_is_the_bad_signature_401(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    redis: Any,
) -> None:
    await fund(client)
    now = int(time.time())
    first = refunds_status_headers(timestamp=now)
    assert (await _status(client, first)).status_code == 200
    bad = await _status(client, {**first, "X-KasGate-Balance-Signature": "00" * 64})
    replay = await _status(client, first)
    assert replay.status_code == 401 and replay.json()["error"] == "invalid_balance_signature", replay.text
    assert set(replay.json()) == set(bad.json())  # same shape as any bad signature
    assert (await _status(client, refunds_status_headers(timestamp=now + 1))).status_code == 200  # fresh: fine
    # The key is the SHA-256 of the lower-case signature and lives no longer than the signature could verify.
    signature = first["X-KasGate-Balance-Signature"].lower()
    key = SIGNATURE_REPLAY_PREFIX + hashlib.sha256(signature.encode("ascii")).hexdigest()
    assert key == f"kasgate:sigreplay:refunds-status:{hashlib.sha256(signature.encode('ascii')).hexdigest()}"
    assert 0 < await redis.ttl(key) <= 601
    upper = {**first, "X-KasGate-Balance-Signature": first["X-KasGate-Balance-Signature"].upper()}
    assert (await _status(client, upper)).status_code == 401  # letter case cannot dodge the replay key


async def test_an_invalid_signature_does_not_burn_the_key_or_consume_a_valid_one(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    redis: Any,
) -> None:
    await fund(client)
    assert (await _status(client, signed_headers())).status_code == 401  # wrong purpose
    assert [k async for k in redis.scan_iter(f"{SIGNATURE_REPLAY_PREFIX}*")] == []
    assert (await _status(client, refunds_status_headers())).status_code == 200


async def test_refunds_status_fails_closed_when_the_replay_store_is_down(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    redis: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await fund(client)

    async def down(*args: Any, **kwargs: Any) -> None:
        raise RedisError("down")

    monkeypatch.setattr(redis, "set", down)
    response = await _status(client, refunds_status_headers())
    assert response.status_code == 503 and response.json()["error"] == "service_unavailable", response.text
    assert StubEngine is not None  # keep the shared import used
