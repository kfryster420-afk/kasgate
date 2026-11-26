"""CONTRACT v2.13 refund hardening, second red-team round: the cancel-time node check and cooldown hold after a
claim, hidden claim details, bounded claims, attempt caps and backoff, per-merchant payout limits, the late-payout
supersede rule and the guard against an earlier-draft 0008 schema."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.ledger import run_ledger_pass, spendable_sompi, unseen_backoff
from app.models import PrepaidRefundPayout
from app.payments import PaymentVerificationError, PayoutSighting, RpcUnavailableError
from app.schema_guard import STALE_0008_MESSAGE, check_database_schema
from tests.conftest import PAYER_ADDRESS, PgServer, Seed, SettingsOverride, StubEngine, bearer
from tests.test_k402 import make_engine
from tests.test_prepaid import CREDIT_SOMPI, fund
from tests.test_refund_hardening import age, attempts_of, cancel, claim
from tests.test_refund_payouts import AMOUNT, REFUND_ID, PayloadMempoolNode, bound_tx, payout_tx
from tests.test_refunds import (
    PAYOUT_TX,
    assert_identity,
    balance_of,
    challenge_of,
    open_refund,
    payout,
    refund_row,
    shop,
    signed,
)

__all__ = ["shop"]

SECOND_TX = "cd" * 32
LAPSED = timedelta(hours=1)


def tx_of(i: int) -> str:
    return f"{i:064x}"


# ---------------------------------------------------------------- cancel after a lapsed claim


async def test_a_cancel_is_refused_while_the_node_shows_a_payout_of_a_claimed_refund(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await fund(client)
    refund = await open_refund(client)
    assert (await claim(client, shop, refund.id)).status_code == 200
    await age(sessionmaker, refund.id, claimed_at=datetime.now(UTC) - LAPSED)
    stub_engine.payout_sightings = [PayoutSighting(SECOND_TX, payload_seen=True)]
    challenge = await challenge_of(client, action="cancel", refund_id=refund.id)
    refused = await client.post(f"/v1/prepaid/refunds/{refund.id}/cancel", json=signed(challenge))
    assert refused.status_code == 409 and refused.json()["error"] == "refund_not_cancellable"
    assert SECOND_TX in refused.json()["message"]
    assert stub_engine.payout_scans == [(PAYER_ADDRESS, CREDIT_SOMPI, refund.id)]
    [attempt] = await attempts_of(sessionmaker, refund.id)
    assert (attempt.tx_id, attempt.method, attempt.status, attempt.payload_verified) == (
        SECOND_TX,
        "scan",
        "pending",
        True,
    )
    row = await refund_row(sessionmaker, refund.id)
    assert row.status == "payout_submitted" and row.tx_id == SECOND_TX
    assert spendable_sompi(await balance_of(sessionmaker, shop.merchant_id)) == 0
    # The worker confirms it like any other attempt.
    await run_ledger_pass(sessionmaker, stub_engine)
    assert (await refund_row(sessionmaker, refund.id)).status == "paid"
    assert_identity(await balance_of(sessionmaker, shop.merchant_id))


async def test_a_node_outage_refuses_the_cancel_without_burning_the_challenge(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await fund(client)
    refund = await open_refund(client)
    assert (await claim(client, shop, refund.id)).status_code == 200
    await age(sessionmaker, refund.id, claimed_at=datetime.now(UTC) - LAPSED)
    stub_engine.payout_scan_error = RpcUnavailableError("node down")
    challenge = await challenge_of(client, action="cancel", refund_id=refund.id)
    down = await client.post(f"/v1/prepaid/refunds/{refund.id}/cancel", json=signed(challenge))
    assert down.status_code == 503 and down.json()["error"] == "service_unavailable"
    assert (await refund_row(sessionmaker, refund.id)).status == "paying"
    stub_engine.payout_scan_error = None
    ok = await client.post(f"/v1/prepaid/refunds/{refund.id}/cancel", json=signed(challenge))
    assert ok.status_code == 200 and ok.json()["status"] == "cancelled"


async def test_known_transactions_do_not_block_a_cancel(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    """A sighting the gateway already knows (an abandoned attempt of this refund) is not a new payout."""
    await fund(client)
    refund = await open_refund(client)
    assert (await claim(client, shop, refund.id)).status_code == 200
    stub_engine.payout_error = PaymentVerificationError("payment_not_found", "not visible")
    assert (await payout(client, shop, refund.id, tx_id=PAYOUT_TX)).status_code == 202
    async with sessionmaker() as session:
        await session.execute(
            update(PrepaidRefundPayout)
            .where(PrepaidRefundPayout.refund_id == refund.id)
            .values(created_at=datetime.now(UTC) - timedelta(days=30), next_check_at=datetime.now(UTC))
        )
        await session.commit()
    await run_ledger_pass(sessionmaker, stub_engine)
    stub_engine.payout_sightings = [PayoutSighting(PAYOUT_TX, payload_seen=False)]
    assert (await cancel(client, refund.id)).status_code == 200


async def test_a_never_claimed_refund_is_cancelled_at_once_without_asking_the_node(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await fund(client)
    refund = await open_refund(client)
    cancelled = await cancel(client, refund.id)
    assert cancelled.status_code == 200 and cancelled.json()["hold_until"] is None
    assert stub_engine.payout_scans == []
    assert spendable_sompi(await balance_of(sessionmaker, shop.merchant_id)) == CREDIT_SOMPI


async def test_a_lapsed_claim_cancel_keeps_a_cooldown_hold_until_the_worker_releases_it(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await fund(client)
    refund = await open_refund(client)
    assert (await claim(client, shop, refund.id)).status_code == 200
    await age(sessionmaker, refund.id, claimed_at=datetime.now(UTC) - LAPSED)
    before = datetime.now(UTC)
    cancelled = await cancel(client, refund.id)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    hold_until = datetime.fromisoformat(cancelled.json()["hold_until"])
    assert timedelta(seconds=1790) < hold_until - before < timedelta(seconds=1810)
    balance = await balance_of(sessionmaker, shop.merchant_id)
    assert spendable_sompi(balance) == 0 and balance.refund_hold_sompi == CREDIT_SOMPI
    await run_ledger_pass(sessionmaker, stub_engine)  # not due yet
    assert (await balance_of(sessionmaker, shop.merchant_id)).refund_hold_sompi == CREDIT_SOMPI
    await age(sessionmaker, refund.id, hold_until=datetime.now(UTC) - timedelta(seconds=1))
    await run_ledger_pass(sessionmaker, stub_engine)
    balance = await balance_of(sessionmaker, shop.merchant_id)
    assert spendable_sompi(balance) == CREDIT_SOMPI and balance.refund_hold_sompi == 0
    assert (await refund_row(sessionmaker, refund.id)).hold_until is None
    assert_identity(balance)


async def test_a_late_payout_within_the_cooldown_is_debited_in_full(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await fund(client)
    refund = await open_refund(client)
    assert (await claim(client, shop, refund.id)).status_code == 200
    await age(sessionmaker, refund.id, claimed_at=datetime.now(UTC) - LAPSED)
    assert (await cancel(client, refund.id)).status_code == 200
    late = await payout(client, shop, refund.id, tx_id=PAYOUT_TX)
    assert late.status_code == 200 and late.json()["late_paid_from"] == "cancelled"
    assert late.json()["shortfall_sompi"] == "0" and late.json()["hold_until"] is None
    balance = await balance_of(sessionmaker, shop.merchant_id)
    assert balance.balance_sompi == 0 and balance.refund_hold_sompi == 0 and balance.refunded_sompi == CREDIT_SOMPI
    assert_identity(balance)


async def test_cooldown_zero_releases_at_once(
    client: httpx.AsyncClient,
    shop: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    override_settings: SettingsOverride,
) -> None:
    override_settings(prepaid_refund_cancel_cooldown_seconds="0")
    await fund(client)
    refund = await open_refund(client)
    assert (await claim(client, shop, refund.id)).status_code == 200
    await age(sessionmaker, refund.id, claimed_at=datetime.now(UTC) - LAPSED)
    assert (await cancel(client, refund.id)).json()["hold_until"] is None
    assert spendable_sompi(await balance_of(sessionmaker, shop.merchant_id)) == CREDIT_SOMPI


# ---------------------------------------------------------------- claim visibility and bounds


async def test_claim_details_are_hidden_publicly_and_shown_to_the_merchant_and_signed_buyer(
    client: httpx.AsyncClient, shop: Seed
) -> None:
    await fund(client)
    refund = await open_refund(client)
    claimed = await claim(client, shop, refund.id)
    body = claimed.json()
    assert body["status"] == "paying" and body["first_claimed_at"] == body["claimed_at"]
    claimed_at = datetime.fromisoformat(body["claimed_at"])
    assert datetime.fromisoformat(body["claim_expires_at"]) == claimed_at + timedelta(seconds=900)
    public = await client.get("/v1/prepaid/refunds/status", params={"slug": shop.slug, "payer_address": PAYER_ADDRESS})
    row = public.json()[0]
    assert row["status"] == "requested"
    assert row["claimed_at"] is None and row["first_claimed_at"] is None and row["claim_expires_at"] is None
    status = await challenge_of(client, action="status")
    mine = (await client.post("/v1/prepaid/refunds/status", json=signed(status))).json()[0]
    assert mine["status"] == "paying" and mine["claimed_at"] and mine["claim_expires_at"]


async def test_claims_cannot_be_renewed_past_the_total_claim_window(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    refund = await open_refund(client)
    assert (await claim(client, shop, refund.id)).status_code == 200
    first = datetime.now(UTC) - timedelta(minutes=55)
    await age(sessionmaker, refund.id, first_claimed_at=first)
    renewed = await claim(client, shop, refund.id)
    assert renewed.status_code == 200
    # The last 5 minutes of the 1 h window cap the renewed claim, not claimed_at + 15 min.
    assert datetime.fromisoformat(renewed.json()["claim_expires_at"]) == first + timedelta(seconds=3600)
    await age(sessionmaker, refund.id, first_claimed_at=datetime.now(UTC) - timedelta(minutes=61))
    over = await claim(client, shop, refund.id)
    assert over.status_code == 409 and over.json()["error"] == "refund_not_claimable"
    assert "claim window" in over.json()["message"]
    # The window ended, so the claim lapsed even though claimed_at is recent: the buyer may cancel.
    assert (await cancel(client, refund.id)).status_code == 200


# ---------------------------------------------------------------- attempt caps, revival, backoff, rate limit


async def test_pending_attempts_per_refund_are_capped(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await fund(client)
    refund = await open_refund(client)
    stub_engine.payout_error = PaymentVerificationError("payment_not_found", "not visible")
    for i in range(1, 4):
        assert (await payout(client, shop, refund.id, tx_id=tx_of(i))).status_code == 202
    capped = await payout(client, shop, refund.id, tx_id=tx_of(4))
    assert capped.status_code == 409 and capped.json()["error"] == "too_many_attempts"
    assert (await payout(client, shop, refund.id, tx_id=tx_of(1))).status_code == 202  # a known one is fine
    assert len(await attempts_of(sessionmaker, refund.id)) == 3


async def test_an_abandoned_attempt_is_revived_only_by_a_transaction_proof_keeping_its_age(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await fund(client)
    refund = await open_refund(client)
    stub_engine.payout_error = PaymentVerificationError("payment_not_found", "not visible")
    stub_engine.payout_tx_id_value = PAYOUT_TX
    assert (await payout(client, shop, refund.id, tx_id=PAYOUT_TX)).status_code == 202
    old = datetime.now(UTC) - timedelta(days=30)
    async with sessionmaker() as session:
        await session.execute(
            update(PrepaidRefundPayout).where(PrepaidRefundPayout.refund_id == refund.id).values(created_at=old)
        )
        await session.commit()
    await run_ledger_pass(sessionmaker, stub_engine)
    again = await payout(client, shop, refund.id, tx_id=PAYOUT_TX)
    assert again.status_code == 409 and again.json()["error"] == "payout_abandoned"
    [attempt] = await attempts_of(sessionmaker, refund.id)
    assert attempt.status == "abandoned"
    stub_engine.payout_error = None
    proven = await payout(client, shop, refund.id, transaction='{"signed": "tx"}')
    assert proven.status_code == 200 and proven.json()["status"] == "paid"
    [attempt] = await attempts_of(sessionmaker, refund.id)
    assert attempt.status == "confirmed" and abs(attempt.created_at - old) < timedelta(seconds=1)


async def test_unseen_attempts_back_off_with_their_age(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    now = datetime.now(UTC)
    assert unseen_backoff(now, now) == timedelta(0)
    assert unseen_backoff(now - timedelta(seconds=1000), now) == timedelta(seconds=100)
    assert unseen_backoff(now - timedelta(days=3), now) == timedelta(seconds=1800)
    await fund(client)
    refund = await open_refund(client)
    stub_engine.payout_error = PaymentVerificationError("payment_not_found", "not visible")
    assert (await payout(client, shop, refund.id, tx_id=PAYOUT_TX)).status_code == 202
    async with sessionmaker() as session:
        await session.execute(
            update(PrepaidRefundPayout)
            .where(PrepaidRefundPayout.refund_id == refund.id)
            .values(created_at=datetime.now(UTC) - timedelta(hours=1), next_check_at=datetime.now(UTC))
        )
        await session.commit()
    checks = len(stub_engine.payout_checks)
    await run_ledger_pass(sessionmaker, stub_engine)
    [attempt] = await attempts_of(sessionmaker, refund.id)
    delay = attempt.next_check_at - datetime.now(UTC)
    assert timedelta(seconds=340) < delay <= timedelta(seconds=361)
    await run_ledger_pass(sessionmaker, stub_engine)  # not due: no node call
    assert len(stub_engine.payout_checks) == checks + 1


async def test_payout_posts_are_rate_limited_per_merchant(
    client: httpx.AsyncClient,
    shop: Seed,
    stub_engine: StubEngine,
    override_settings: SettingsOverride,
) -> None:
    override_settings(rl_refund_payout_per_merchant_per_minute="2")
    await fund(client)
    refund = await open_refund(client)
    stub_engine.payout_error = PaymentVerificationError("payment_not_found", "not visible")
    codes = [(await payout(client, shop, refund.id, tx_id=tx_of(i))).status_code for i in range(1, 4)]
    assert codes == [202, 202, 429]
    other_ip = await client.post(
        f"/v1/prepaid/refunds/{refund.id}/payout",
        json={"tx_id": tx_of(5)},
        headers={**bearer(shop.api_key), "X-Forwarded-For": "203.0.113.50"},
    )
    assert other_ip.status_code == 429  # the bucket is the merchant, not the client IP


# ---------------------------------------------------------------- late payout vs a newer claimed refund


async def test_a_late_payout_supersedes_a_newer_refund_only_once_its_claim_lapsed(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    r1 = await open_refund(client)
    assert (await cancel(client, r1.id)).status_code == 200
    r2 = await open_refund(client)
    assert (await claim(client, shop, r2.id)).status_code == 200
    await age(sessionmaker, r2.id, claimed_at=datetime.now(UTC) - LAPSED)
    late = await payout(client, shop, r1.id, tx_id=PAYOUT_TX)
    assert late.status_code == 200 and late.json()["shortfall_sompi"] == "0"
    assert (await refund_row(sessionmaker, r2.id)).status == "rejected"
    assert_identity(await balance_of(sessionmaker, shop.merchant_id))


async def test_a_late_payout_records_a_shortfall_instead_of_closing_a_live_claim(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    r1 = await open_refund(client)
    assert (await cancel(client, r1.id)).status_code == 200
    r2 = await open_refund(client)
    assert (await claim(client, shop, r2.id)).status_code == 200
    late = await payout(client, shop, r1.id, tx_id=PAYOUT_TX)
    assert late.status_code == 200 and late.json()["shortfall_sompi"] == str(CREDIT_SOMPI)
    assert (await refund_row(sessionmaker, r2.id)).status == "paying"
    balance = await balance_of(sessionmaker, shop.merchant_id)
    assert balance.refund_hold_sompi == CREDIT_SOMPI and balance.balance_sompi == CREDIT_SOMPI
    assert_identity(balance)


# ---------------------------------------------------------------- engine: cancel-time scan


async def test_engine_finds_payload_bound_mempool_payouts_and_exact_amount_utxos() -> None:
    node = PayloadMempoolNode()
    in_flight = bound_tx([(PAYER_ADDRESS, 1_000_000_000)])  # any amount: the payload names this refund
    unrelated = payout_tx([(PAYER_ADDRESS, AMOUNT)])  # no payload, in the mempool: not a sighting
    accepted = payout_tx([(PAYER_ADDRESS, AMOUNT)], payload=b"something else")
    other_amount = payout_tx([(PAYER_ADDRESS, AMOUNT + 1)])
    node.add_to_mempool(in_flight)
    node.add_to_mempool(unrelated)
    node.accept_into_utxo_index(accepted)
    node.accept_into_utxo_index(other_amount)
    found = await make_engine(node).find_refund_payouts(PAYER_ADDRESS, AMOUNT, REFUND_ID)
    assert found == [PayoutSighting(in_flight.id, payload_seen=True), PayoutSighting(accepted.id, payload_seen=False)]


class DownMempoolNode(PayloadMempoolNode):
    async def get_mempool_entries_by_addresses(self, request: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("websocket connection closed")


async def test_engine_scan_reports_outages() -> None:
    node = DownMempoolNode()
    with pytest.raises(RpcUnavailableError):
        await make_engine(node).find_refund_payouts(PAYER_ADDRESS, AMOUNT, REFUND_ID)


# ---------------------------------------------------------------- earlier-draft 0008 guard at app startup


async def test_app_startup_refuses_an_earlier_draft_0008_schema(pg_server: PgServer) -> None:
    url = pg_server.make_url("kasgate_guard")
    pg_server.create_database("kasgate_guard")
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        await check_database_schema(engine)  # empty database: nothing to check
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE prepaid_refunds (id uuid PRIMARY KEY)"))
        with pytest.raises(RuntimeError) as info:
            await check_database_schema(engine)
        assert str(info.value) == STALE_0008_MESSAGE and "downgrade 0007_trials_webhooks" in STALE_0008_MESSAGE
    finally:
        await engine.dispose()
    unreachable = create_async_engine("postgresql+asyncpg://kasgate:x@127.0.0.1:9/none", poolclass=NullPool)
    try:
        await check_database_schema(unreachable)  # logged, not raised
    finally:
        await unreachable.dispose()


async def test_scan_attempts_do_not_fill_the_tx_id_cap(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await fund(client)
    refund = await open_refund(client)
    assert (await claim(client, shop, refund.id)).status_code == 200
    await age(sessionmaker, refund.id, claimed_at=datetime.now(UTC) - LAPSED)
    stub_engine.payout_sightings = [PayoutSighting(tx_of(i), payload_seen=False) for i in range(1, 4)]
    assert (await cancel(client, refund.id)).status_code == 409
    assert len(await attempts_of(sessionmaker, refund.id)) == 3
    stub_engine.payout_error = PaymentVerificationError("payment_not_found", "not visible")
    posted = await payout(client, shop, refund.id, tx_id=PAYOUT_TX)
    assert posted.status_code == 202
    methods = {a.method for a in await attempts_of(sessionmaker, refund.id)}
    assert methods == {"scan", "tx_id"}


async def test_cooldown_hold_is_released_once_pending_attempts_are_abandoned(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await fund(client)
    refund = await open_refund(client)
    assert (await claim(client, shop, refund.id)).status_code == 200
    await age(sessionmaker, refund.id, claimed_at=datetime.now(UTC) - LAPSED)
    assert (await cancel(client, refund.id)).status_code == 200
    stub_engine.payout_error = PaymentVerificationError("payload_unverified", "accepted, payload not seen yet")
    assert (await payout(client, shop, refund.id, tx_id=PAYOUT_TX)).status_code == 202
    await age(sessionmaker, refund.id, hold_until=datetime.now(UTC) - timedelta(seconds=1))
    await run_ledger_pass(sessionmaker, stub_engine)
    assert spendable_sompi(await balance_of(sessionmaker, shop.merchant_id)) == 0
    async with sessionmaker() as session:
        await session.execute(
            update(PrepaidRefundPayout)
            .where(PrepaidRefundPayout.refund_id == refund.id)
            .values(created_at=datetime.now(UTC) - timedelta(days=30), next_check_at=datetime.now(UTC))
        )
        await session.commit()
    await run_ledger_pass(sessionmaker, stub_engine)
    balance = await balance_of(sessionmaker, shop.merchant_id)
    assert spendable_sompi(balance) == CREDIT_SOMPI and balance.refund_hold_sompi == 0
    assert_identity(balance)
