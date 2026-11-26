"""CONTRACT v2.13 refund hardening after the red-team round: claim/lock, payout attempt history, late payouts,
transient node errors, signed status, nonce handling, rate limits and the trusted-proxy client IP."""

import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from app.ledger import run_ledger_pass, spendable_sompi
from app.models import PrepaidBalance, PrepaidRefund, PrepaidRefundPayout, WebhookEvent
from app.payments import PaymentVerificationError, RpcUnavailableError
from app.security import client_ip
from tests.conftest import Seed, SettingsOverride, StubEngine, bearer, make_asgi_client
from tests.test_prepaid import CREDIT_SOMPI, fund
from tests.test_refunds import (
    MIN_REFUND,
    PAYOUT_TX,
    assert_identity,
    balance_of,
    challenge_of,
    event_types,
    open_refund,
    payout,
    refund_challenge,
    refund_row,
    shop,
    signed,
    subscribe,
)

__all__ = ["shop"]

SECOND_TX = "cd" * 32


async def attempts_of(maker: async_sessionmaker[AsyncSession], refund_id: uuid.UUID) -> list[PrepaidRefundPayout]:
    async with maker() as session:
        return list(
            (
                await session.execute(
                    select(PrepaidRefundPayout)
                    .where(PrepaidRefundPayout.refund_id == refund_id)
                    .order_by(PrepaidRefundPayout.created_at)
                )
            ).scalars()
        )


async def cancel(client: httpx.AsyncClient, refund_id: uuid.UUID) -> httpx.Response:
    challenge = await challenge_of(client, action="cancel", refund_id=refund_id)
    return await client.post(f"/v1/prepaid/refunds/{refund_id}/cancel", json=signed(challenge))


async def claim(client: httpx.AsyncClient, seed: Seed, refund_id: uuid.UUID) -> httpx.Response:
    return await client.post(f"/v1/prepaid/refunds/{refund_id}/claim", headers=bearer(seed.api_key))


async def age(maker: async_sessionmaker[AsyncSession], refund_id: uuid.UUID, **values: datetime) -> None:
    async with maker() as session:
        await session.execute(update(PrepaidRefund).where(PrepaidRefund.id == refund_id).values(**values))
        await session.commit()


# ---------------------------------------------------------------- claim / lock


async def test_a_claimed_refund_cannot_be_cancelled_until_the_claim_lapses(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    refund = await open_refund(client)
    claimed = await claim(client, shop, refund.id)
    assert claimed.status_code == 200 and claimed.json()["status"] == "paying" and claimed.json()["claimed_at"]
    assert (await claim(client, shop, refund.id)).status_code == 200  # renewing is fine
    blocked = await refund_challenge(client, action="cancel", refund_id=refund.id)
    assert blocked.status_code == 409 and blocked.json()["error"] == "refund_not_cancellable"
    assert "paying" in blocked.json()["message"]
    # The claim lapses after prepaid_refund_claim_ttl_seconds without a payout.
    await age(sessionmaker, refund.id, claimed_at=datetime.now(UTC) - timedelta(hours=1))
    assert (await cancel(client, refund.id)).status_code == 200
    again = await claim(client, shop, refund.id)
    assert again.status_code == 409 and again.json()["error"] == "refund_not_claimable"


async def test_claim_is_merchant_scoped_and_reject_still_works(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    refund = await open_refund(client)
    assert (await client.post(f"/v1/prepaid/refunds/{refund.id}/claim")).status_code == 401
    assert (await claim(client, shop, uuid.uuid4())).status_code == 404
    assert (await claim(client, shop, refund.id)).status_code == 200
    rejected = await client.post(
        f"/v1/prepaid/refunds/{refund.id}/reject", json={"reason": "promo credit"}, headers=bearer(shop.api_key)
    )
    assert rejected.status_code == 200 and rejected.json()["claimed_at"] is None
    assert (await balance_of(sessionmaker, shop.merchant_id)).refund_hold_sompi == 0


async def test_a_payout_moves_the_refund_out_of_reach_of_cancel_before_verification(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await fund(client)
    refund = await open_refund(client)
    seen: list[str] = []

    async def verify(
        tx_id: str, to_address: str, amount_sompi: int, refund_id: uuid.UUID, *, payload_checked: bool = False
    ) -> None:
        seen.append((await refund_row(sessionmaker, refund_id)).status)
        raise PaymentVerificationError("payout_unconfirmed", "valid, not accepted yet")

    stub_engine.verify_payout = verify  # type: ignore[method-assign]
    response = await payout(client, shop, refund.id, tx_id=PAYOUT_TX)
    assert response.status_code == 202 and seen == ["payout_submitted"]
    [attempt] = await attempts_of(sessionmaker, refund.id)
    assert (attempt.status, attempt.payload_verified, attempt.method) == ("pending", True, "tx_id")


# ---------------------------------------------------------------- attempts, reverts, late payouts


async def test_a_corrected_tx_id_keeps_both_attempts_and_either_can_pay(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await fund(client)
    refund = await open_refund(client)
    stub_engine.payout_error = PaymentVerificationError("payment_not_found", "not yet")
    assert (await payout(client, shop, refund.id, tx_id=PAYOUT_TX)).status_code == 202
    corrected = await payout(client, shop, refund.id, tx_id=SECOND_TX)
    assert corrected.status_code == 202 and corrected.json()["tx_id"] == SECOND_TX
    listed = await client.get(f"/v1/prepaid/refunds/{refund.id}/payouts", headers=bearer(shop.api_key))
    assert listed.status_code == 200 and [a["tx_id"] for a in listed.json()] == [PAYOUT_TX, SECOND_TX]
    assert {a["status"] for a in listed.json()} == {"pending"}

    stub_engine.payout_error = None
    await run_ledger_pass(sessionmaker, stub_engine)
    row = await refund_row(sessionmaker, refund.id)
    assert row.status == "paid" and row.tx_id in (PAYOUT_TX, SECOND_TX)
    statuses = sorted(a.status for a in await attempts_of(sessionmaker, refund.id))
    assert statuses == ["confirmed", "duplicate"]  # the second payment is flagged, the ledger moves once
    balance = await balance_of(sessionmaker, shop.merchant_id)
    assert (balance.refunded_sompi, balance.balance_sompi) == (CREDIT_SOMPI, 0)
    assert_identity(balance)


async def test_after_a_revert_cancel_is_blocked_while_an_attempt_is_pending_and_a_webhook_fires(
    client: httpx.AsyncClient,
    shop: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    stub_engine: StubEngine,
    override_settings: SettingsOverride,
) -> None:
    await subscribe(client, shop)
    await fund(client)
    refund = await open_refund(client)
    stub_engine.payout_error = PaymentVerificationError("payment_not_found", "not visible")
    assert (await payout(client, shop, refund.id, tx_id=PAYOUT_TX)).status_code == 202
    await age(sessionmaker, refund.id, payout_submitted_at=datetime.now(UTC) - timedelta(days=2))
    override_settings(prepaid_payout_pending_max_seconds=3600)
    await run_ledger_pass(sessionmaker, stub_engine)
    row = await refund_row(sessionmaker, refund.id)
    assert (row.status, row.tx_id) == ("requested", None)
    assert "prepaid.refund.payout_reverted" in await event_types(sessionmaker)
    async with sessionmaker() as session:
        event = (
            await session.execute(
                select(WebhookEvent).where(WebhookEvent.event_type == "prepaid.refund.payout_reverted")
            )
        ).scalar_one()
    assert event.payload["data"]["pending_tx_ids"] == PAYOUT_TX
    blocked = await refund_challenge(client, action="cancel", refund_id=refund.id)
    assert blocked.status_code == 409 and "being confirmed" in blocked.json()["message"]
    rejected = await client.post(
        f"/v1/prepaid/refunds/{refund.id}/reject", json={"reason": "no"}, headers=bearer(shop.api_key)
    )
    assert rejected.status_code == 409

    # After prepaid_payout_attempt_max_seconds the unseen attempt is abandoned and the buyer may cancel again.
    async with sessionmaker() as session:
        await session.execute(
            update(PrepaidRefundPayout)
            .where(PrepaidRefundPayout.refund_id == refund.id)
            .values(created_at=datetime.now(UTC) - timedelta(days=30))
        )
        await session.commit()
    await run_ledger_pass(sessionmaker, stub_engine)
    [attempt] = await attempts_of(sessionmaker, refund.id)
    assert attempt.status == "abandoned"
    assert (await cancel(client, refund.id)).status_code == 200


async def test_transient_node_errors_back_off_and_never_revert(
    client: httpx.AsyncClient,
    shop: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    stub_engine: StubEngine,
    override_settings: SettingsOverride,
) -> None:
    await fund(client)
    refund = await open_refund(client)
    stub_engine.payout_error = RpcUnavailableError("node down")
    assert (await payout(client, shop, refund.id, tx_id=PAYOUT_TX)).status_code == 202
    [attempt] = await attempts_of(sessionmaker, refund.id)
    assert attempt.error_count == 1 and attempt.next_check_at > datetime.now(UTC)
    await age(sessionmaker, refund.id, payout_submitted_at=datetime.now(UTC) - timedelta(days=2))
    override_settings(prepaid_payout_pending_max_seconds=3600)
    checks = len(stub_engine.payout_checks)
    await run_ledger_pass(sessionmaker, stub_engine)
    assert len(stub_engine.payout_checks) == checks  # backing off: not due yet
    async with sessionmaker() as session:
        await session.execute(
            update(PrepaidRefundPayout)
            .where(PrepaidRefundPayout.refund_id == refund.id)
            .values(next_check_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    await run_ledger_pass(sessionmaker, stub_engine)
    [attempt] = await attempts_of(sessionmaker, refund.id)
    assert attempt.error_count == 2 and attempt.status == "pending"
    row = await refund_row(sessionmaker, refund.id)
    assert (row.status, row.tx_id) == ("payout_submitted", PAYOUT_TX)


async def test_a_definite_invalid_payout_reverts_only_when_no_other_attempt_is_pending(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await fund(client)
    refund = await open_refund(client)
    stub_engine.payout_error = PaymentVerificationError("payment_not_found", "not yet")
    assert (await payout(client, shop, refund.id, tx_id=PAYOUT_TX)).status_code == 202
    stub_engine.payout_error = PaymentVerificationError("invalid_payout", "no refund payload")
    wrong = await payout(client, shop, refund.id, tx_id=SECOND_TX)
    assert wrong.status_code == 422
    row = await refund_row(sessionmaker, refund.id)
    assert (row.status, row.tx_id) == ("payout_submitted", PAYOUT_TX)  # the first attempt is still pending
    reposted = await payout(client, shop, refund.id, tx_id=SECOND_TX)
    assert reposted.status_code == 422  # a proven-invalid tx_id stays invalid
    await run_ledger_pass(sessionmaker, stub_engine)  # now the first one is proven invalid too
    row = await refund_row(sessionmaker, refund.id)
    assert (row.status, row.tx_id) == ("requested", None)
    assert sorted(a.status for a in await attempts_of(sessionmaker, refund.id)) == ["invalid", "invalid"]


async def test_a_late_payout_after_cancel_re_debits_what_is_left_and_records_the_shortfall(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await subscribe(client, shop)
    await fund(client)
    refund = await open_refund(client, MIN_REFUND)
    assert (await cancel(client, refund.id)).status_code == 200
    # The buyer spends most of the released credit before the merchant's (already broadcast) payout is posted.
    async with sessionmaker() as session:
        await session.execute(
            update(PrepaidBalance)
            .where(PrepaidBalance.merchant_id == shop.merchant_id)
            .values(
                balance_sompi=MIN_REFUND // 2,
                debited_sompi=CREDIT_SOMPI - MIN_REFUND // 2,
            )
        )
        await session.commit()
    late = await payout(client, shop, refund.id, tx_id=PAYOUT_TX)
    assert late.status_code == 200, late.text
    body = late.json()
    assert (body["status"], body["late_paid_from"], body["shortfall_sompi"]) == (
        "paid",
        "cancelled",
        str(MIN_REFUND - MIN_REFUND // 2),
    )
    row = await balance_of(sessionmaker, shop.merchant_id)
    assert (row.balance_sompi, row.refunded_sompi, spendable_sompi(row)) == (0, MIN_REFUND // 2, 0)
    assert_identity(row)
    assert (await event_types(sessionmaker))[-1] == "prepaid.refund.paid"
    # The gateway never broadcasts a new payout for a closed refund.
    other = await open_refund(client, MIN_REFUND) if spendable_sompi(row) >= MIN_REFUND else None
    assert other is None
    raw = await payout(client, shop, refund.id, transaction='{"signed": "tx"}')
    assert raw.status_code == 409 and stub_engine.payout_submits == []


async def test_raw_mode_records_the_attempt_before_broadcasting(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await fund(client)
    refund = await open_refund(client)
    seen: list[tuple[str, int]] = []
    real_submit = stub_engine.submit_payout

    async def submit(transaction: str, payload: bytes, to_address: str, amount_sompi: int) -> tuple[str, bool]:
        seen.append(
            ((await refund_row(sessionmaker, refund.id)).status, len(await attempts_of(sessionmaker, refund.id)))
        )
        return await real_submit(transaction, payload, to_address, amount_sompi)

    stub_engine.submit_payout = submit  # type: ignore[method-assign]
    stub_engine.payout_accepted = False
    pending = await payout(client, shop, refund.id, transaction='{"signed": "tx"}')
    assert pending.status_code == 202 and seen == [("payout_submitted", 1)]
    [attempt] = await attempts_of(sessionmaker, refund.id)
    assert (attempt.method, attempt.payload_verified, attempt.tx_id) == ("transaction", True, "e" * 64)


# ---------------------------------------------------------------- buyer-side hardening


async def test_signed_refund_writes_are_rate_limited_per_ip(
    client: httpx.AsyncClient, shop: Seed, override_settings: SettingsOverride
) -> None:
    await fund(client)
    override_settings(rl_prepaid_refund_per_minute=2)
    bogus = {"nonce": "0" * 32, "signature": "ab" * 64}
    codes = [(await client.post("/v1/prepaid/refunds", json=bogus)).status_code for _ in range(2)]
    codes.append((await client.post(f"/v1/prepaid/refunds/{uuid.uuid4()}/cancel", json=bogus)).status_code)
    assert codes == [410, 410, 429]


def _request(peer: str, forwarded: str | None) -> Request:
    headers = [] if forwarded is None else [(b"x-forwarded-for", forwarded.encode())]
    return Request({"type": "http", "client": (peer, 1234), "headers": headers, "method": "GET", "path": "/"})


async def test_client_ip_honours_x_forwarded_for_only_from_trusted_proxies(
    client: httpx.AsyncClient, override_settings: SettingsOverride
) -> None:
    assert client_ip(_request("10.0.0.5", "203.0.113.9")) == "10.0.0.5"  # default: no trusted proxies
    override_settings(trusted_proxy_ips='["10.0.0.0/8", "::1"]')
    assert client_ip(_request("10.0.0.5", "203.0.113.9")) == "203.0.113.9"
    # The right-most untrusted hop wins: a client-supplied left-most value cannot pick the bucket.
    assert client_ip(_request("10.0.0.5", "198.51.100.1, 203.0.113.9, 10.0.0.7")) == "203.0.113.9"
    assert client_ip(_request("192.0.2.1", "203.0.113.9")) == "192.0.2.1"  # untrusted peer: header ignored
    assert client_ip(_request("10.0.0.5", None)) == "10.0.0.5"
    assert client_ip(_request("10.0.0.5", "not-an-ip")) == "10.0.0.5"


async def test_rate_limit_buckets_follow_the_forwarded_client_behind_a_trusted_proxy(
    client: httpx.AsyncClient, shop: Seed, override_settings: SettingsOverride
) -> None:
    override_settings(trusted_proxy_ips='["10.9.9.9"]', rl_prepaid_refund_per_minute=1)
    bogus = {"nonce": "0" * 32, "signature": "ab" * 64}
    async with make_asgi_client(client, "10.9.9.9") as proxy:
        first = await proxy.post("/v1/prepaid/refunds", json=bogus, headers={"X-Forwarded-For": "203.0.113.1"})
        second = await proxy.post("/v1/prepaid/refunds", json=bogus, headers={"X-Forwarded-For": "203.0.113.2"})
        again = await proxy.post("/v1/prepaid/refunds", json=bogus, headers={"X-Forwarded-For": "203.0.113.1"})
    assert (first.status_code, second.status_code, again.status_code) == (410, 410, 429)


async def test_refund_errors_use_the_documented_error_body(client: httpx.AsyncClient, shop: Seed) -> None:
    from app.schemas import X402ErrorBody

    await fund(client)
    response = await refund_challenge(client, amount=1)
    assert response.status_code == 422
    assert X402ErrorBody.model_validate(response.json()).error == "refund_too_small"
