"""CONTRACT v2.13: prepaid refunds (buyer request -> merchant on-chain payout) and prepaid credit expiry."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from kaspa import PrivateKey, sign_message
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ledger import expire_due_balances, finalize_payout, run_ledger_pass, spendable_sompi
from app.models import (
    Endpoint,
    Merchant,
    PrepaidBalance,
    PrepaidDeposit,
    PrepaidLedgerEntry,
    PrepaidRefund,
    PrepaidRefundPayout,
    WebhookEvent,
)
from app.payments import PaymentVerificationError, RpcUnavailableError
from app.refunds import refund_challenge_key, refund_nonce_lock_key
from app.schemas import PrepaidRefundChallenge, PrepaidRefundRead, refund_payout_payload
from tests.conftest import (
    OTHER_ADDRESS,
    OTHER_KEY,
    PAYER_ADDRESS,
    PAYER_KEY,
    Seed,
    SettingsOverride,
    StubEngine,
    bearer,
    seed_endpoint,
)
from tests.test_prepaid import CREDIT_SOMPI, DEPOSIT_SOMPI, add_endpoint, fund, request_challenge
from tests.test_prepaid_balance_privacy import signed_headers

MIN_REFUND = 20_000_000
PAYOUT_TX = "ab" * 32
REFUND_EVENTS = [
    "prepaid.refund.requested",
    "prepaid.refund.paid",
    "prepaid.refund.rejected",
    "prepaid.refund.cancelled",
    "prepaid.refund.payout_reverted",
    "prepaid.credit.expired",
]


@pytest.fixture
async def shop(sessionmaker: async_sessionmaker[AsyncSession]) -> Seed:
    seed = await seed_endpoint(sessionmaker)
    await add_endpoint(sessionmaker, seed.merchant_id, "cheap")
    return seed


# ---------------------------------------------------------------- helpers


async def refund_challenge(
    client: httpx.AsyncClient,
    *,
    action: str = "request",
    amount: int | None = None,
    refund_id: uuid.UUID | None = None,
    slug: str = "cheap",
    payer: str = PAYER_ADDRESS,
) -> httpx.Response:
    body: dict[str, Any] = {"slug": slug, "payer_address": payer, "action": action}
    if amount is not None:
        body["amount_sompi"] = amount
    if refund_id is not None:
        body["refund_id"] = str(refund_id)
    return await client.post("/v1/prepaid/refunds/challenge", json=body)


async def challenge_of(client: httpx.AsyncClient, **kwargs: Any) -> PrepaidRefundChallenge:
    response = await refund_challenge(client, **kwargs)
    assert response.status_code == 201, response.text
    return PrepaidRefundChallenge.model_validate(response.json())


def signed(challenge: PrepaidRefundChallenge, key: PrivateKey = PAYER_KEY) -> dict[str, str]:
    return {"nonce": challenge.nonce, "signature": sign_message(challenge.message, key)}


async def open_refund(client: httpx.AsyncClient, amount: int | None = None) -> PrepaidRefundRead:
    challenge = await challenge_of(client, amount=amount)
    response = await client.post("/v1/prepaid/refunds", json=signed(challenge))
    assert response.status_code == 201, response.text
    return PrepaidRefundRead.model_validate(response.json())


async def add_attempt(
    maker: async_sessionmaker[AsyncSession], refund_id: uuid.UUID, merchant_id: uuid.UUID, tx_id: str
) -> uuid.UUID:
    attempt_id = uuid.uuid4()
    async with maker() as session:
        session.add(
            PrepaidRefundPayout(
                id=attempt_id,
                refund_id=refund_id,
                merchant_id=merchant_id,
                tx_id=tx_id,
                method="tx_id",
                status="pending",
                payload_verified=True,
                check_count=0,
                error_count=0,
            )
        )
        await session.commit()
    return attempt_id


async def balance_of(maker: async_sessionmaker[AsyncSession], merchant_id: uuid.UUID) -> PrepaidBalance:
    async with maker() as session:
        return (
            await session.execute(select(PrepaidBalance).where(PrepaidBalance.merchant_id == merchant_id))
        ).scalar_one()


async def refund_row(maker: async_sessionmaker[AsyncSession], refund_id: uuid.UUID) -> PrepaidRefund:
    async with maker() as session:
        return (await session.execute(select(PrepaidRefund).where(PrepaidRefund.id == refund_id))).scalar_one()


async def ledger_rows(maker: async_sessionmaker[AsyncSession]) -> list[PrepaidLedgerEntry]:
    async with maker() as session:
        return list(
            (await session.execute(select(PrepaidLedgerEntry).order_by(PrepaidLedgerEntry.created_at))).scalars()
        )


async def event_types(maker: async_sessionmaker[AsyncSession]) -> list[str]:
    async with maker() as session:
        rows = (await session.execute(select(WebhookEvent).order_by(WebhookEvent.created_at))).scalars()
        return [row.event_type for row in rows]


async def subscribe(client: httpx.AsyncClient, seed: Seed) -> None:
    response = await client.post(
        "/v1/webhooks",
        json={"url": "https://hooks.merchant.test/kasgate", "event_types": REFUND_EVENTS},
        headers=bearer(seed.api_key),
    )
    assert response.status_code == 201, response.text


def assert_identity(balance: PrepaidBalance) -> None:
    assert balance.balance_sompi == (
        balance.deposited_sompi - balance.debited_sompi - balance.refunded_sompi - balance.expired_sompi
    )
    assert 0 <= balance.refund_hold_sompi <= balance.balance_sompi


async def payout(client: httpx.AsyncClient, seed: Seed, refund_id: uuid.UUID, **body: str) -> httpx.Response:
    return await client.post(f"/v1/prepaid/refunds/{refund_id}/payout", json=body, headers=bearer(seed.api_key))


async def set_expiry(maker: async_sessionmaker[AsyncSession], merchant_id: uuid.UUID, when: datetime) -> None:
    async with maker() as session:
        await session.execute(
            update(PrepaidBalance).where(PrepaidBalance.merchant_id == merchant_id).values(expires_at=when)
        )
        await session.commit()


# ---------------------------------------------------------------- request -> payout


async def test_full_refund_is_held_then_debited_only_when_the_payout_is_seen(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await subscribe(client, shop)
    await fund(client)
    challenge = await challenge_of(client)
    assert (challenge.amount_sompi, challenge.spendable_sompi) == (str(CREDIT_SOMPI), str(CREDIT_SOMPI))
    lines = challenge.message.splitlines()
    assert lines[0] == "KasGate" and "Purpose: kasgate:v1:prepaid-refund" in lines and "Action: request" in lines
    assert f"Address: {PAYER_ADDRESS}" in lines and "Refund: -" in lines

    created = await client.post("/v1/prepaid/refunds", json=signed(challenge))
    assert created.status_code == 201, created.text
    refund = PrepaidRefundRead.model_validate(created.json())
    assert refund.status == "requested" and refund.amount_sompi == str(CREDIT_SOMPI)
    assert refund.payout_address == PAYER_ADDRESS
    assert refund.payout_payload_hex == refund_payout_payload(refund.id).hex()
    held = await balance_of(sessionmaker, shop.merchant_id)
    assert (held.balance_sompi, held.refund_hold_sompi, held.refunded_sompi) == (CREDIT_SOMPI, CREDIT_SOMPI, 0)
    assert spendable_sompi(held) == 0
    assert_identity(held)

    # The held credit no longer pays calls.
    blocked = await request_challenge(client)
    assert blocked.status_code == 402 and "spendable" in blocked.json()["message"]
    public = (await client.get("/v1/prepaid/balance", params={"slug": "cheap", "payer_address": PAYER_ADDRESS})).json()
    assert public["spendable_sompi"] == "0" and public["balance_sompi"] == str(CREDIT_SOMPI)
    assert public["refund_hold_sompi"] is public["refunded_sompi"] is public["expired_sompi"] is None
    totals = (
        await client.get(
            "/v1/prepaid/balance",
            params={"slug": "cheap", "payer_address": PAYER_ADDRESS},
            headers=signed_headers(),
        )
    ).json()
    assert (totals["refund_hold_sompi"], totals["spendable_sompi"]) == (str(CREDIT_SOMPI), "0")

    paid = await payout(client, shop, refund.id, tx_id=PAYOUT_TX)
    assert paid.status_code == 200, paid.text
    assert paid.json()["status"] == "paid" and paid.json()["tx_id"] == PAYOUT_TX
    assert stub_engine.payout_checks == [(PAYOUT_TX, PAYER_ADDRESS, CREDIT_SOMPI)]
    assert stub_engine.payout_refund_ids == [refund.id]  # verify_payout is bound to this refund's payload
    after = await balance_of(sessionmaker, shop.merchant_id)
    assert (after.balance_sompi, after.refund_hold_sompi, after.refunded_sompi) == (0, 0, CREDIT_SOMPI)
    assert_identity(after)
    [entry] = await ledger_rows(sessionmaker)
    assert (entry.kind, entry.direction, entry.amount_sompi, entry.balance_after_sompi, entry.tx_id) == (
        "refund",
        "debit",
        CREDIT_SOMPI,
        0,
        PAYOUT_TX,
    )
    assert entry.refund_id == refund.id
    assert await event_types(sessionmaker) == ["prepaid.refund.requested", "prepaid.refund.paid"]

    again = await payout(client, shop, refund.id, tx_id=PAYOUT_TX)
    assert again.status_code == 200 and again.json()["status"] == "paid"  # same tx_id: idempotent
    other_tx = await payout(client, shop, refund.id, tx_id="cd" * 32)
    assert other_tx.status_code == 409 and other_tx.json()["error"] == "refund_closed"
    ledger = await client.get("/v1/prepaid/ledger", headers=bearer(shop.api_key))
    assert ledger.status_code == 200 and [row["kind"] for row in ledger.json()] == ["refund"]


async def test_partial_refund_keeps_the_rest_spendable_and_one_open_refund_per_balance(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    refund = await open_refund(client, MIN_REFUND)
    row = await balance_of(sessionmaker, shop.merchant_id)
    assert spendable_sompi(row) == CREDIT_SOMPI - MIN_REFUND
    assert (await request_challenge(client)).status_code == 201
    second = await refund_challenge(client, amount=MIN_REFUND)
    assert second.status_code == 409 and second.json()["error"] == "refund_already_open"
    assert str(refund.id) in second.json()["message"]


async def test_two_racing_requests_open_only_one_refund(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    first = await challenge_of(client, amount=MIN_REFUND)
    second = await challenge_of(client, amount=MIN_REFUND)
    results = await asyncio.gather(
        client.post("/v1/prepaid/refunds", json=signed(first)),
        client.post("/v1/prepaid/refunds", json=signed(second)),
    )
    assert sorted(r.status_code for r in results) == [201, 409]
    row = await balance_of(sessionmaker, shop.merchant_id)
    assert row.refund_hold_sompi == MIN_REFUND
    assert_identity(row)


async def test_request_validation(client: httpx.AsyncClient, shop: Seed) -> None:
    nothing = await refund_challenge(client)
    assert nothing.status_code == 422 and nothing.json()["error"] == "refund_too_small"
    await fund(client)
    too_small = await refund_challenge(client, amount=MIN_REFUND - 1)
    assert too_small.status_code == 422 and too_small.json()["error"] == "refund_too_small"
    too_big = await refund_challenge(client, amount=CREDIT_SOMPI + 1)
    assert too_big.status_code == 409 and too_big.json()["error"] == "insufficient_balance"
    unknown = await refund_challenge(client, slug="no-such-api")
    assert unknown.status_code == 404
    other_buyer = await refund_challenge(client, payer=OTHER_ADDRESS)
    assert other_buyer.status_code == 422  # OTHER has no credit here
    for body in (
        {"slug": "cheap", "payer_address": PAYER_ADDRESS, "amount_sompi": "20000000"},  # strict: no numeric strings
        {"slug": "cheap", "payer_address": PAYER_ADDRESS, "refund_id": str(uuid.uuid4())},
        {"slug": "cheap", "payer_address": PAYER_ADDRESS, "action": "cancel"},
        {"slug": "cheap", "payer_address": PAYER_ADDRESS, "action": "steal"},
        {"slug": "cheap", "payer_address": "kaspa:nope"},
        {"slug": "cheap", "payer_address": PAYER_ADDRESS, "extra": 1},
    ):
        response = await client.post("/v1/prepaid/refunds/challenge", json=body)
        assert response.status_code == 422, body


async def test_signatures_are_bound_to_the_depositing_key_and_single_use(
    client: httpx.AsyncClient, shop: Seed, redis: Any, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    challenge = await challenge_of(client, amount=MIN_REFUND)
    forged = await client.post("/v1/prepaid/refunds", json=signed(challenge, OTHER_KEY))
    assert forged.status_code == 400 and forged.json()["error"] == "invalid_signature"
    tampered = {"nonce": challenge.nonce, "signature": sign_message(challenge.message + "\n", PAYER_KEY)}
    assert (await client.post("/v1/prepaid/refunds", json=tampered)).status_code == 400
    ok = await client.post("/v1/prepaid/refunds", json=signed(challenge))
    assert ok.status_code == 201
    assert await redis.get(refund_challenge_key(challenge.nonce)) is None
    assert await redis.get(refund_nonce_lock_key(challenge.nonce)) == "1"
    replay = await client.post("/v1/prepaid/refunds", json=signed(challenge))
    assert replay.status_code == 410
    unknown = await client.post("/v1/prepaid/refunds", json={"nonce": "0" * 32, "signature": "a" * 128})
    assert unknown.status_code == 410
    bad = await client.post("/v1/prepaid/refunds", json={"nonce": challenge.nonce, "signature": "zz"})
    assert bad.status_code == 422


async def test_a_cancel_challenge_cannot_open_a_refund(client: httpx.AsyncClient, shop: Seed) -> None:
    await fund(client)
    refund = await open_refund(client, MIN_REFUND)
    cancel = await challenge_of(client, action="cancel", refund_id=refund.id)
    assert cancel.amount_sompi == str(MIN_REFUND) and f"Refund: {refund.id}" in cancel.message
    misuse = await client.post("/v1/prepaid/refunds", json=signed(cancel))
    assert misuse.status_code == 400 and "cancel" in misuse.json()["message"]
    wrong_id = await client.post(f"/v1/prepaid/refunds/{uuid.uuid4()}/cancel", json=signed(cancel))
    assert wrong_id.status_code in (400, 409)


# ---------------------------------------------------------------- cancel and reject


async def test_buyer_cancels_and_the_hold_is_released(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await subscribe(client, shop)
    await fund(client)
    refund = await open_refund(client)
    cancel = await challenge_of(client, action="cancel", refund_id=refund.id)
    response = await client.post(f"/v1/prepaid/refunds/{refund.id}/cancel", json=signed(cancel))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled" and response.json()["resolved_at"] is not None
    row = await balance_of(sessionmaker, shop.merchant_id)
    assert (row.balance_sompi, row.refund_hold_sompi) == (CREDIT_SOMPI, 0)
    assert await event_types(sessionmaker) == ["prepaid.refund.requested", "prepaid.refund.cancelled"]
    gone = await refund_challenge(client, action="cancel", refund_id=refund.id)
    assert gone.status_code == 404
    # A new request is possible once the old one is closed.
    newer = await open_refund(client)
    assert newer.status == "requested"
    # The merchant's payout of the cancelled refund lands anyway: it is recorded (backstop), the credit is taken
    # back, and the newer request that held it is closed so the buyer is not refunded twice.
    late = await payout(client, shop, refund.id, tx_id=PAYOUT_TX)
    assert late.status_code == 200, late.text
    assert (late.json()["status"], late.json()["late_paid_from"], late.json()["shortfall_sompi"]) == (
        "paid",
        "cancelled",
        "0",
    )
    row = await balance_of(sessionmaker, shop.merchant_id)
    assert (row.balance_sompi, row.refund_hold_sompi, row.refunded_sompi) == (0, 0, CREDIT_SOMPI)
    assert_identity(row)
    superseded = await refund_row(sessionmaker, uuid.UUID(str(newer.id)))
    assert superseded.status == "rejected" and superseded.reason is not None and str(refund.id) in superseded.reason
    [entry] = await ledger_rows(sessionmaker)
    assert (entry.amount_sompi, entry.refund_id) == (CREDIT_SOMPI, refund.id)


async def test_merchant_rejects_with_a_reason(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await subscribe(client, shop)
    other = await seed_endpoint(sessionmaker, slug="elsewhere", address=OTHER_ADDRESS)
    await fund(client)
    refund = await open_refund(client)
    path = f"/v1/prepaid/refunds/{refund.id}/reject"
    assert (await client.post(path, json={"reason": "x"})).status_code == 401
    assert (await client.post(path, json={"reason": "x"}, headers=bearer(other.api_key))).status_code == 404
    assert (await client.post(path, json={"reason": ""}, headers=bearer(shop.api_key))).status_code == 422
    assert (await client.post(path, json={"reason": "r" * 301}, headers=bearer(shop.api_key))).status_code == 422
    rejected = await client.post(path, json={"reason": "credit was promotional"}, headers=bearer(shop.api_key))
    assert rejected.status_code == 200, rejected.text
    assert (rejected.json()["status"], rejected.json()["reason"]) == ("rejected", "credit was promotional")
    row = await balance_of(sessionmaker, shop.merchant_id)
    assert (row.balance_sompi, row.refund_hold_sompi) == (CREDIT_SOMPI, 0)
    twice = await client.post(path, json={"reason": "again"}, headers=bearer(shop.api_key))
    assert twice.status_code == 409 and twice.json()["error"] == "refund_not_rejectable"
    assert await event_types(sessionmaker) == ["prepaid.refund.requested", "prepaid.refund.rejected"]
    public = await client.get("/v1/prepaid/refunds/status", params={"slug": "cheap", "payer_address": PAYER_ADDRESS})
    assert public.status_code == 200 and [r["status"] for r in public.json()] == ["rejected"]
    assert public.json()[0]["reason"] is None  # reasons are not public
    status_challenge = await challenge_of(client, action="status")
    assert status_challenge.amount_sompi == "0" and "Action: status" in status_challenge.message
    mine = await client.post("/v1/prepaid/refunds/status", json=signed(status_challenge))
    assert mine.status_code == 200 and mine.json()[0]["reason"] == "credit was promotional"
    replay = await client.post("/v1/prepaid/refunds/status", json=signed(status_challenge))
    assert replay.status_code in (409, 410)
    forged_challenge = await challenge_of(client, action="status")
    forged = await client.post("/v1/prepaid/refunds/status", json=signed(forged_challenge, OTHER_KEY))
    assert forged.status_code == 400


async def test_merchant_listing_is_scoped_and_filterable(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    other = await seed_endpoint(sessionmaker, slug="elsewhere", address=OTHER_ADDRESS)
    await fund(client)
    refund = await open_refund(client)
    listed = await client.get("/v1/prepaid/refunds", headers=bearer(shop.api_key))
    assert [r["id"] for r in listed.json()] == [str(refund.id)]
    filtered = await client.get("/v1/prepaid/refunds", params={"status": "paid"}, headers=bearer(shop.api_key))
    assert filtered.json() == []
    invalid = await client.get("/v1/prepaid/refunds", params={"status": "bogus"}, headers=bearer(shop.api_key))
    assert invalid.status_code == 422
    assert (await client.get("/v1/prepaid/refunds", headers=bearer(other.api_key))).json() == []
    assert (await client.get("/v1/prepaid/refunds")).status_code == 401
    assert (await client.get("/v1/prepaid/ledger")).status_code == 401
    assert (await payout(client, other, refund.id, tx_id=PAYOUT_TX)).status_code == 404


# ---------------------------------------------------------------- payout modes and failures


async def test_payout_not_visible_yet_is_rechecked_by_the_worker(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await fund(client)
    refund = await open_refund(client)
    stub_engine.payout_error = PaymentVerificationError("payment_not_found", "not in the mempool yet")
    pending = await payout(client, shop, refund.id, tx_id=PAYOUT_TX)
    assert pending.status_code == 202, pending.text
    assert pending.json()["status"] == "payout_submitted" and pending.json()["tx_id"] == PAYOUT_TX
    row = await balance_of(sessionmaker, shop.merchant_id)
    assert (row.balance_sompi, row.refund_hold_sompi) == (CREDIT_SOMPI, CREDIT_SOMPI)  # nothing debited yet
    # Still not visible: the worker leaves it alone.
    assert await run_ledger_pass(sessionmaker, stub_engine) == 0
    assert (await refund_row(sessionmaker, refund.id)).status == "payout_submitted"
    stub_engine.payout_error = None
    assert await run_ledger_pass(sessionmaker, stub_engine) == 1
    paid = await refund_row(sessionmaker, refund.id)
    assert (paid.status, paid.tx_id) == ("paid", PAYOUT_TX)
    after = await balance_of(sessionmaker, shop.merchant_id)
    assert (after.balance_sompi, after.refunded_sompi, after.refund_hold_sompi) == (0, CREDIT_SOMPI, 0)


async def test_node_outage_records_the_payout_as_submitted(
    client: httpx.AsyncClient, shop: Seed, stub_engine: StubEngine
) -> None:
    await fund(client)
    refund = await open_refund(client)
    stub_engine.payout_error = RpcUnavailableError("node down")
    pending = await payout(client, shop, refund.id, tx_id=PAYOUT_TX)
    assert pending.status_code == 202 and "node unavailable" in pending.json()["last_error"]


async def test_a_payout_that_never_appears_goes_back_to_requested(
    client: httpx.AsyncClient,
    shop: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    stub_engine: StubEngine,
    override_settings: SettingsOverride,
) -> None:
    await fund(client)
    refund = await open_refund(client)
    stub_engine.payout_error = PaymentVerificationError("payment_not_found", "not found")
    assert (await payout(client, shop, refund.id, tx_id=PAYOUT_TX)).status_code == 202
    async with sessionmaker() as session:
        await session.execute(
            update(PrepaidRefund)
            .where(PrepaidRefund.id == refund.id)
            .values(payout_submitted_at=datetime.now(UTC) - timedelta(days=2))
        )
        await session.commit()
    override_settings(prepaid_payout_pending_max_seconds=3600)
    await run_ledger_pass(sessionmaker, stub_engine)
    row = await refund_row(sessionmaker, refund.id)
    assert (row.status, row.tx_id, row.payout_submitted_at) == ("requested", None, None)
    assert row.last_error is not None and row.last_error.startswith("payment_not_found")
    held = await balance_of(sessionmaker, shop.merchant_id)
    assert held.refund_hold_sompi == CREDIT_SOMPI  # still owed, the merchant can pay again


async def test_a_wrong_payout_is_rejected_and_the_refund_stays_open(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await fund(client)
    refund = await open_refund(client)
    stub_engine.payout_error = PaymentVerificationError("invalid_payout", "no output pays the buyer exactly")
    wrong = await payout(client, shop, refund.id, tx_id=PAYOUT_TX)
    assert wrong.status_code == 422 and wrong.json()["error"] == "invalid_payout"
    assert (await refund_row(sessionmaker, refund.id)).status == "requested"
    for body in ({}, {"tx_id": PAYOUT_TX, "transaction": "{}"}, {"tx_id": "xyz"}):
        response = await client.post(f"/v1/prepaid/refunds/{refund.id}/payout", json=body, headers=bearer(shop.api_key))
        assert response.status_code == 422, body


async def test_kasgate_payments_and_reused_tx_ids_cannot_pay_a_refund(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await fund(client)
    async with sessionmaker() as session:
        deposit_tx = (await session.execute(select(PrepaidDeposit.tx_id))).scalar_one()
    refund = await open_refund(client, MIN_REFUND)
    own = await payout(client, shop, refund.id, tx_id=deposit_tx)
    assert own.status_code == 422 and "KasGate payment" in own.json()["message"]
    assert (await payout(client, shop, refund.id, tx_id=PAYOUT_TX)).status_code == 200
    second = await open_refund(client, MIN_REFUND)
    reused = await payout(client, shop, second.id, tx_id=PAYOUT_TX)
    assert reused.status_code == 409 and reused.json()["error"] == "payout_replayed"
    assert stub_engine.payout_checks == [(PAYOUT_TX, PAYER_ADDRESS, MIN_REFUND)]


async def test_finalize_is_idempotent_and_tx_ids_are_unique(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    first = await open_refund(client, MIN_REFUND)
    attempt_id = await add_attempt(sessionmaker, first.id, shop.merchant_id, PAYOUT_TX)
    async with sessionmaker() as session:
        paid = await finalize_payout(session, attempt_id)
        assert paid is not None and paid.debited_sompi == MIN_REFUND
    async with sessionmaker() as session:
        again = await finalize_payout(session, attempt_id)
        assert again is not None and again.debited_sompi == 0  # idempotent: nothing moves twice
    second = await open_refund(client, MIN_REFUND)
    with pytest.raises(IntegrityError):
        await add_attempt(sessionmaker, second.id, shop.merchant_id, PAYOUT_TX)
    row = await balance_of(sessionmaker, shop.merchant_id)
    assert (row.refunded_sompi, row.refund_hold_sompi) == (MIN_REFUND, MIN_REFUND)
    assert_identity(row)


async def test_raw_signed_transaction_mode(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await fund(client)
    refund = await open_refund(client, MIN_REFUND)
    stub_engine.payout_tx_id_value = "cd" * 32
    stub_engine.payout_accepted = False
    pending = await payout(client, shop, refund.id, transaction='{"version": 0}')
    assert pending.status_code == 202 and pending.json()["tx_id"] == "cd" * 32
    [(raw, payload, address, amount)] = stub_engine.payout_submits
    assert (raw, payload, address, amount) == (
        '{"version": 0}',
        refund_payout_payload(refund.id),
        PAYER_ADDRESS,
        MIN_REFUND,
    )
    assert payload == b"kasgate:v1:refund:" + refund.id.hex.encode()
    await run_ledger_pass(sessionmaker, stub_engine)
    assert (await refund_row(sessionmaker, refund.id)).status == "paid"

    second = await open_refund(client, MIN_REFUND)
    stub_engine.payout_tx_id_value, stub_engine.payout_accepted = "ef" * 32, True
    paid = await payout(client, shop, second.id, transaction='{"version": 0}')
    assert paid.status_code == 200 and paid.json()["status"] == "paid"
    emptied = await balance_of(sessionmaker, shop.merchant_id)
    assert (emptied.balance_sompi, emptied.refunded_sompi) == (0, 2 * MIN_REFUND)
    assert_identity(emptied)


async def test_raw_transaction_errors_are_reported(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await fund(client)
    refund = await open_refund(client, MIN_REFUND)
    stub_engine.payout_error = RpcUnavailableError("broadcast failed")
    down = await payout(client, shop, refund.id, transaction='{"version": 0}')
    # Unknown whether the broadcast reached the network: kept as submitted and re-checked, never dropped.
    assert down.status_code == 202 and down.json()["status"] == "payout_submitted"
    stub_engine.payout_error = PaymentVerificationError("invalid_payout", "payload mismatch")
    bad = await payout(client, shop, refund.id, transaction='{"version": 0}')
    assert bad.status_code == 422
    assert (await refund_row(sessionmaker, refund.id)).status == "requested"


# ---------------------------------------------------------------- expiry


async def test_merchant_expiry_policy_and_endpoint_override(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    path = "/v1/merchants/me"
    for body in ({}, {"prepaid_expiry_days": 0}, {"prepaid_expiry_days": 3651}, {"prepaid_expiry_days": "30"}):
        assert (await client.patch(path, json=body, headers=bearer(shop.api_key))).status_code == 422, body
    assert (await client.patch(path, json={"display_name": None}, headers=bearer(shop.api_key))).status_code == 422
    updated = await client.patch(path, json={"prepaid_expiry_days": 30}, headers=bearer(shop.api_key))
    assert updated.status_code == 200 and updated.json()["prepaid_expiry_days"] == 30
    assert (await client.get(path, headers=bearer(shop.api_key))).json()["prepaid_expiry_days"] == 30

    created = await client.post(
        "/v1/endpoints",
        json={
            "slug": "short-lived",
            "name": "Short",
            "upstream_url": "https://api.upstream.test/s",
            "price_sompi": 1_000,
            "prepaid_only": True,
            "allowed_methods": ["GET"],
            "prepaid_expiry_days": 7,
        },
        headers=bearer(shop.api_key),
    )
    assert created.status_code == 201, created.text
    assert created.json()["prepaid_expiry_days"] == 7
    before = datetime.now(UTC)
    receipt = await fund(client, slug="short-lived")
    assert receipt.balance_sompi == str(CREDIT_SOMPI)
    row = await balance_of(sessionmaker, shop.merchant_id)
    assert row.expires_at is not None
    assert before + timedelta(days=7) - timedelta(minutes=1) <= row.expires_at <= datetime.now(UTC) + timedelta(days=7)
    # A deposit through the merchant-default endpoint extends to the later expiry.
    await fund(client)
    extended = await balance_of(sessionmaker, shop.merchant_id)
    assert extended.expires_at is not None and extended.expires_at >= before + timedelta(days=30) - timedelta(minutes=1)
    # The longer expiry stays when a shorter-policy deposit lands (greatest wins).
    await fund(client, slug="short-lived")
    assert (await balance_of(sessionmaker, shop.merchant_id)).expires_at == extended.expires_at

    cleared = await client.patch(
        f"/v1/endpoints/{created.json()['id']}", json={"prepaid_expiry_days": None}, headers=bearer(shop.api_key)
    )
    assert cleared.status_code == 200 and cleared.json()["prepaid_expiry_days"] is None
    async with sessionmaker() as session:
        merchant = (await session.execute(select(Merchant).where(Merchant.id == shop.merchant_id))).scalar_one()
        endpoint = (await session.execute(select(Endpoint).where(Endpoint.slug == "short-lived"))).scalar_one()
    assert (merchant.prepaid_expiry_days, endpoint.prepaid_expiry_days) == (30, None)
    never = await client.patch(path, json={"prepaid_expiry_days": None}, headers=bearer(shop.api_key))
    assert never.status_code == 200 and never.json()["prepaid_expiry_days"] is None


async def test_expiry_is_not_retroactive_for_credit_promised_never_to_expire(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    assert (await balance_of(sessionmaker, shop.merchant_id)).expires_at is None
    await client.patch("/v1/merchants/me", json={"prepaid_expiry_days": 1}, headers=bearer(shop.api_key))
    await fund(client)
    row = await balance_of(sessionmaker, shop.merchant_id)
    assert row.expires_at is None and row.balance_sompi == 2 * CREDIT_SOMPI


async def test_expired_credit_stops_paying_and_is_swept_into_the_ledger(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await subscribe(client, shop)
    await client.patch("/v1/merchants/me", json={"prepaid_expiry_days": 1}, headers=bearer(shop.api_key))
    await fund(client)
    await set_expiry(sessionmaker, shop.merchant_id, datetime.now(UTC) - timedelta(seconds=1))
    blocked = await request_challenge(client)
    assert blocked.status_code == 402
    assert (await refund_challenge(client)).status_code == 422  # nothing spendable to refund
    balance = (await client.get("/v1/prepaid/balance", params={"slug": "cheap", "payer_address": PAYER_ADDRESS})).json()
    assert (balance["balance_sompi"], balance["spendable_sompi"]) == (str(CREDIT_SOMPI), "0")

    async with sessionmaker() as session:
        assert await expire_due_balances(session, 10) == 1
    async with sessionmaker() as session:
        assert await expire_due_balances(session, 10) == 0
    row = await balance_of(sessionmaker, shop.merchant_id)
    assert (row.balance_sompi, row.expired_sompi) == (0, CREDIT_SOMPI)
    assert_identity(row)
    [entry] = await ledger_rows(sessionmaker)
    assert (entry.kind, entry.amount_sompi, entry.balance_after_sompi, entry.tx_id) == ("expiry", CREDIT_SOMPI, 0, None)
    assert await event_types(sessionmaker) == ["prepaid.credit.expired"]


async def test_expiry_never_takes_an_open_refund_hold(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await client.patch("/v1/merchants/me", json={"prepaid_expiry_days": 1}, headers=bearer(shop.api_key))
    await fund(client)
    refund = await open_refund(client, MIN_REFUND)
    await set_expiry(sessionmaker, shop.merchant_id, datetime.now(UTC) - timedelta(seconds=1))
    async with sessionmaker() as session:
        assert await expire_due_balances(session, 10) == 1
    row = await balance_of(sessionmaker, shop.merchant_id)
    assert (row.balance_sompi, row.refund_hold_sompi, row.expired_sompi) == (
        MIN_REFUND,
        MIN_REFUND,
        CREDIT_SOMPI - MIN_REFUND,
    )
    assert (await payout(client, shop, refund.id, tx_id=PAYOUT_TX)).status_code == 200
    done = await balance_of(sessionmaker, shop.merchant_id)
    assert (done.balance_sompi, done.refunded_sompi) == (0, MIN_REFUND)
    assert_identity(done)


async def test_a_deposit_expires_overdue_credit_before_adding_new_credit(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await client.patch("/v1/merchants/me", json={"prepaid_expiry_days": 1}, headers=bearer(shop.api_key))
    await fund(client)
    await set_expiry(sessionmaker, shop.merchant_id, datetime.now(UTC) - timedelta(seconds=1))
    receipt = await fund(client)
    assert receipt.balance_sompi == str(CREDIT_SOMPI)  # the old credit expired, only the new deposit counts
    row = await balance_of(sessionmaker, shop.merchant_id)
    assert (row.expired_sompi, row.deposited_sompi) == (CREDIT_SOMPI, 2 * CREDIT_SOMPI)
    assert row.expires_at is not None and row.expires_at > datetime.now(UTC)
    assert_identity(row)
    assert [e.kind for e in await ledger_rows(sessionmaker)] == ["expiry"]
    assert DEPOSIT_SOMPI > CREDIT_SOMPI


async def test_a_submitted_payout_can_no_longer_be_cancelled_or_rejected(
    client: httpx.AsyncClient, shop: Seed, stub_engine: StubEngine
) -> None:
    await fund(client)
    refund = await open_refund(client)
    cancel = await challenge_of(client, action="cancel", refund_id=refund.id)
    stub_engine.payout_error = PaymentVerificationError("payment_not_found", "not yet")
    assert (await payout(client, shop, refund.id, tx_id=PAYOUT_TX)).status_code == 202
    late = await client.post(f"/v1/prepaid/refunds/{refund.id}/cancel", json=signed(cancel))
    assert late.status_code == 409 and late.json()["error"] == "refund_not_cancellable"
    no_new = await refund_challenge(client, action="cancel", refund_id=refund.id)
    assert no_new.status_code == 409
    reject = await client.post(
        f"/v1/prepaid/refunds/{refund.id}/reject", json={"reason": "no"}, headers=bearer(shop.api_key)
    )
    assert reject.status_code == 409
    # The merchant can correct the tx_id while the payout is still unconfirmed.
    stub_engine.payout_error = None
    fixed = await payout(client, shop, refund.id, tx_id="cd" * 32)
    assert fixed.status_code == 200 and fixed.json()["tx_id"] == "cd" * 32


async def test_ledger_worker_runs_passes_until_stopped(
    sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine, override_settings: SettingsOverride
) -> None:
    from app.ledger import LedgerWorker

    override_settings(ledger_poll_interval_seconds=0.01)
    worker = LedgerWorker(sessionmaker, stub_engine)
    worker.start()
    worker.start()  # idempotent
    await asyncio.sleep(0.05)
    await worker.stop()
    await worker.stop()
