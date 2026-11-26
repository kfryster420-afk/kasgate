"""Red-team of CONTRACT v2.13 prepaid refunds (feat/prepaid-refund @ 15d7bd4).

Tests named ``test_hole_*`` fail on 15d7bd4: each one states an invariant the branch does not keep yet.
Every other test pins down an attack that the branch already stops.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from kaspa import Transaction
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ledger import expire_due_balances, run_ledger_pass, spendable_sompi
from app.models import PrepaidBalance, PrepaidRefund
from app.payments import PaymentVerificationError
from app.schemas import HEADER_PREPAID_DEBIT, PrepaidRefundChallenge, refund_payout_payload
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
from tests.test_k402 import FakeNode, _script_hex, make_engine
from tests.test_prepaid import CREDIT_SOMPI, add_endpoint, debit_header, fund, get_debit_challenge
from tests.test_refund_payouts import AMOUNT, REFUND_ID, SubmittingNode, payout_tx, wire
from tests.test_refunds import (
    MIN_REFUND,
    PAYOUT_TX,
    assert_identity,
    balance_of,
    challenge_of,
    open_refund,
    payout,
    refund_challenge,
    refund_row,
    set_expiry,
    signed,
)

OTHER_TX = "cd" * 32


@pytest.fixture
async def shop(sessionmaker: async_sessionmaker[AsyncSession]) -> Seed:
    seed = await seed_endpoint(sessionmaker)
    await add_endpoint(sessionmaker, seed.merchant_id, "cheap")
    return seed


async def cancel(client: httpx.AsyncClient, challenge: PrepaidRefundChallenge, refund_id: uuid.UUID) -> httpx.Response:
    return await client.post(f"/v1/prepaid/refunds/{refund_id}/cancel", json=signed(challenge))


async def age_submitted_payout(maker: async_sessionmaker[AsyncSession], refund_id: uuid.UUID) -> None:
    async with maker() as session:
        await session.execute(
            update(PrepaidRefund)
            .where(PrepaidRefund.id == refund_id)
            .values(payout_submitted_at=datetime.now(UTC) - timedelta(days=2))
        )
        await session.commit()


# ================================================================ HOLE 1: cancel races the on-chain payout


async def test_hole_cancel_during_tx_id_payout_verification_double_refunds(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    """The merchant broadcast the payout (KasWare sendKaspa) and posts its tx_id; the buyer, watching their address,
    cancels while the gateway verifies. The coins reached the buyer, so the credit must not come back as spendable."""
    await fund(client)
    refund = await open_refund(client)
    cancel_challenge = await challenge_of(client, action="cancel", refund_id=refund.id)
    real_verify = stub_engine.verify_payout
    cancelled: list[int] = []

    async def verify_while_buyer_cancels(
        tx_id: str, to_address: str, amount_sompi: int, refund_id: uuid.UUID, *, payload_checked: bool = False
    ) -> None:
        cancelled.append((await cancel(client, cancel_challenge, refund.id)).status_code)
        await real_verify(tx_id, to_address, amount_sompi, refund_id, payload_checked=payload_checked)

    stub_engine.verify_payout = verify_while_buyer_cancels  # type: ignore[method-assign]
    response = await payout(client, shop, refund.id, tx_id=PAYOUT_TX)
    row = await balance_of(sessionmaker, shop.merchant_id)
    assert_identity(row)
    # On 15d7bd4: cancel -> 200, payout -> 409 refund_closed, refunded 0 and all 40_000_000 sompi spendable again.
    assert (row.refunded_sompi, spendable_sompi(row)) == (CREDIT_SOMPI, 0), (cancelled, response.json())


async def test_hole_cancel_during_raw_payout_broadcast_double_refunds(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    """Same race in `{transaction}` mode: the gateway itself broadcasts before it records anything."""
    await fund(client)
    refund = await open_refund(client)
    cancel_challenge = await challenge_of(client, action="cancel", refund_id=refund.id)
    real_submit = stub_engine.submit_payout

    async def submit_while_buyer_cancels(
        transaction: str, payload: bytes, to_address: str, amount_sompi: int
    ) -> tuple[str, bool]:
        await cancel(client, cancel_challenge, refund.id)
        return await real_submit(transaction, payload, to_address, amount_sompi)

    stub_engine.submit_payout = submit_while_buyer_cancels  # type: ignore[method-assign]
    await payout(client, shop, refund.id, transaction='{"signed": "by the merchant"}')
    assert stub_engine.payout_submits, "the payout was broadcast"
    row = await balance_of(sessionmaker, shop.merchant_id)
    assert (row.refunded_sompi, spendable_sompi(row)) == (CREDIT_SOMPI, 0)


# ================================================================ HOLE 2: revert after 24h forgets a real payout


async def test_hole_reverted_payout_that_lands_later_is_never_recorded(
    client: httpx.AsyncClient,
    shop: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    stub_engine: StubEngine,
    override_settings: SettingsOverride,
) -> None:
    """A payout invisible for PREPAID_PAYOUT_PENDING_MAX_SECONDS reverts to `requested` with tx_id cleared. When the
    original transaction then becomes visible, the worker must still record it; otherwise the merchant pays again."""
    await fund(client)
    refund = await open_refund(client)
    stub_engine.payout_error = PaymentVerificationError("payment_not_found", "not visible")
    assert (await payout(client, shop, refund.id, tx_id=PAYOUT_TX)).status_code == 202
    await age_submitted_payout(sessionmaker, refund.id)
    override_settings(prepaid_payout_pending_max_seconds=3600)
    await run_ledger_pass(sessionmaker, stub_engine)
    assert (await refund_row(sessionmaker, refund.id)).status == "requested"

    stub_engine.payout_error = None  # the original payout is now visible on the node
    await run_ledger_pass(sessionmaker, stub_engine)
    row = await refund_row(sessionmaker, refund.id)
    # On 15d7bd4: still `requested` with tx_id None, hold kept, so the dashboard asks the merchant to pay again.
    assert (row.status, row.tx_id) == ("paid", PAYOUT_TX)


async def test_hole_worker_reverts_on_a_transient_node_error(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    """A malformed node reply (`invalid_payment_header` from the engine's mempool parser) is not proof the payout is
    wrong, but the worker reverts at once (no cutoff) and forgets the tx_id."""
    await fund(client)
    refund = await open_refund(client)
    stub_engine.payout_error = PaymentVerificationError("payment_not_found", "not visible")
    assert (await payout(client, shop, refund.id, tx_id=PAYOUT_TX)).status_code == 202
    stub_engine.payout_error = PaymentVerificationError("invalid_payment_header", "output 0 is malformed")
    await run_ledger_pass(sessionmaker, stub_engine)
    row = await refund_row(sessionmaker, refund.id)
    assert (row.status, row.tx_id) == ("payout_submitted", PAYOUT_TX)


# ================================================================ HOLE 3: any exact payment to the buyer counts


class PayloadNode(FakeNode):
    """A FakeNode whose mempool replies carry the whole transaction (payload included), like kaspad does."""

    def __init__(self) -> None:
        super().__init__()
        self.full: dict[str, dict[str, Any]] = {}

    def add_full(self, tx: Transaction) -> None:
        self.add_to_mempool(tx)
        body = tx.to_dict()
        body["verboseData"] = {"transactionId": tx.id}
        self.full[tx.id] = body

    async def get_mempool_entries_by_addresses(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("mempool")
        wanted = {_script_hex(address) for address in request["addresses"]}
        entries = []
        for address in request["addresses"]:
            receiving = [
                {"fee": 1, "isOrphan": False, "transaction": body}
                for body in self.full.values()
                if any(str(o["scriptPublicKey"]["script"]).lower() in wanted for o in body["outputs"])
            ]
            entries.append({"address": address, "sending": [], "receiving": receiving})
        return {"entries": entries}


async def test_hole_buyers_own_self_transfer_verifies_as_a_merchant_payout() -> None:
    """The buyer moves AMOUNT to their own address (their key, no refund payload). The merchant posts that tx_id
    and the refund is marked paid although the merchant paid nothing. The same works with another merchant's payout
    of the same amount to the same buyer (first poster wins; the honest merchant then gets 409 payout_replayed)."""
    node = PayloadNode()
    self_transfer = payout_tx([(PAYER_ADDRESS, AMOUNT)], key=PAYER_KEY)
    node.add_full(self_transfer)
    with pytest.raises(PaymentVerificationError) as info:
        await make_engine(node).verify_payout(self_transfer.id, PAYER_ADDRESS, AMOUNT, REFUND_ID)
    assert info.value.code == "invalid_payout"


# ================================================================ what held up


async def test_held_one_transaction_cannot_close_refunds_at_two_merchants(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    other = await seed_endpoint(sessionmaker, slug="elsewhere", address=OTHER_ADDRESS)
    await add_endpoint(sessionmaker, other.merchant_id, "cheap2")
    await fund(client)
    await fund(client, slug="cheap2")
    first = await open_refund(client, MIN_REFUND)
    challenge = await challenge_of(client, amount=MIN_REFUND, slug="cheap2")
    created = await client.post("/v1/prepaid/refunds", json=signed(challenge))
    assert created.status_code == 201
    second_id = uuid.UUID(created.json()["id"])
    assert (await payout(client, shop, first.id, tx_id=PAYOUT_TX)).status_code == 200
    reused = await payout(client, other, second_id, tx_id=PAYOUT_TX)
    assert reused.status_code == 409 and reused.json()["error"] == "payout_replayed"


async def test_held_a_signed_request_opens_exactly_one_refund_under_parallel_replay(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    challenge = await challenge_of(client, amount=MIN_REFUND)
    body = signed(challenge)
    results = await asyncio.gather(*(client.post("/v1/prepaid/refunds", json=body) for _ in range(8)))
    codes = sorted(r.status_code for r in results)
    assert codes.count(201) == 1 and set(codes) <= {201, 409, 410}
    row = await balance_of(sessionmaker, shop.merchant_id)
    assert row.refund_hold_sompi == MIN_REFUND


async def test_held_many_racing_requests_open_one_refund(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    challenges = [await challenge_of(client, amount=MIN_REFUND) for _ in range(5)]
    results = await asyncio.gather(*(client.post("/v1/prepaid/refunds", json=signed(c)) for c in challenges))
    assert sorted(r.status_code for r in results) == [201, 409, 409, 409, 409]
    row = await balance_of(sessionmaker, shop.merchant_id)
    assert row.refund_hold_sompi == MIN_REFUND
    assert_identity(row)


async def test_held_cancel_nonce_is_single_use_and_bound_to_its_refund(client: httpx.AsyncClient, shop: Seed) -> None:
    await fund(client)
    request_challenge_ = await challenge_of(client, amount=MIN_REFUND)
    refund = await open_refund(client, MIN_REFUND)
    as_cancel = await cancel(client, request_challenge_, refund.id)
    assert as_cancel.status_code == 400  # a request challenge cannot cancel
    cancel_challenge = await challenge_of(client, action="cancel", refund_id=refund.id)
    wrong_refund = await client.post(f"/v1/prepaid/refunds/{uuid.uuid4()}/cancel", json=signed(cancel_challenge))
    assert wrong_refund.status_code == 400
    # The refund binding is checked before the nonce lock (low finding): a wrong-refund call does not burn it.
    again = await cancel(client, cancel_challenge, refund.id)
    assert again.status_code == 200
    # Still single use: the same signed challenge never cancels twice.
    assert (await cancel(client, cancel_challenge, refund.id)).status_code == 410
    stale = await refund_challenge(client, action="cancel", refund_id=refund.id)
    assert stale.status_code == 404  # nothing open to cancel any more


async def test_held_another_key_cannot_cancel_or_refund_my_balance(client: httpx.AsyncClient, shop: Seed) -> None:
    await fund(client)
    refund = await open_refund(client, MIN_REFUND)
    stranger = await refund_challenge(client, action="cancel", refund_id=refund.id, payer=OTHER_ADDRESS)
    assert stranger.status_code == 404
    mine = await challenge_of(client, action="cancel", refund_id=refund.id)
    forged = await client.post(f"/v1/prepaid/refunds/{refund.id}/cancel", json=signed(mine, OTHER_KEY))
    assert forged.status_code == 400 and forged.json()["error"] == "invalid_signature"
    # The payout destination is always the depositing address; there is no field to redirect it.
    assert refund.payout_address == PAYER_ADDRESS


async def test_held_spending_after_the_challenge_cannot_overdraw_the_refund(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    await add_endpoint(sessionmaker, shop.merchant_id, "tenth", price_sompi=10_000_000)
    challenge = await challenge_of(client)  # all 40_000_000 spendable
    debit = await get_debit_challenge(client, slug="tenth")
    assert (await client.get("/p/tenth", headers={HEADER_PREPAID_DEBIT: debit_header(debit)})).status_code == 200
    late = await client.post("/v1/prepaid/refunds", json=signed(challenge))
    assert late.status_code == 409 and late.json()["error"] == "insufficient_balance"
    row = await balance_of(sessionmaker, shop.merchant_id)
    assert (row.refund_hold_sompi, row.balance_sompi) == (0, CREDIT_SOMPI - 10_000_000)


async def test_held_refund_and_spend_race_never_overdraws(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    await add_endpoint(sessionmaker, shop.merchant_id, "tenth", price_sompi=10_000_000)
    challenge = await challenge_of(client)
    debits = [await get_debit_challenge(client, slug="tenth") for _ in range(4)]
    results = await asyncio.gather(
        client.post("/v1/prepaid/refunds", json=signed(challenge)),
        *(client.get("/p/tenth", headers={HEADER_PREPAID_DEBIT: debit_header(d)}) for d in debits),
    )
    row = await balance_of(sessionmaker, shop.merchant_id)
    assert_identity(row)
    assert row.refund_hold_sompi + row.debited_sompi <= CREDIT_SOMPI
    refund_ok = results[0].status_code == 201
    paid_calls = sum(r.status_code == 200 for r in results[1:])
    assert (refund_ok and paid_calls == 0) or (not refund_ok and paid_calls >= 1)


async def test_held_minimum_and_amount_bounds(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    exact_min = await open_refund(client, MIN_REFUND)  # exactly 0.2 KAS is allowed
    assert exact_min.amount_sompi == str(MIN_REFUND)
    for amount in (10**30, True, 0, -1):
        response = await client.post(
            "/v1/prepaid/refunds/challenge",
            json={"slug": "cheap", "payer_address": PAYER_ADDRESS, "amount_sompi": amount},
        )
        assert response.status_code in (409, 422), (amount, response.text)
    # Spendable left = 20_000_000 - 1 after spending: an all-credit refund is below the minimum.
    await client.post(
        f"/v1/prepaid/refunds/{exact_min.id}/reject", json={"reason": "test"}, headers=bearer(shop.api_key)
    )
    async with sessionmaker() as session:
        await session.execute(
            update(PrepaidBalance)
            .where(PrepaidBalance.merchant_id == shop.merchant_id)
            .values(
                balance_sompi=MIN_REFUND - 1,
                debited_sompi=CREDIT_SOMPI - MIN_REFUND + 1,
            )
        )
        await session.commit()
    small = await refund_challenge(client)
    assert small.status_code == 422 and small.json()["error"] == "refund_too_small"


async def test_held_expiry_boundary_and_cancel_after_expiry(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await client.patch("/v1/merchants/me", json={"prepaid_expiry_days": 1}, headers=bearer(shop.api_key))
    await fund(client)
    row = await balance_of(sessionmaker, shop.merchant_id)
    assert row.expires_at is not None
    assert spendable_sompi(row, now=row.expires_at) == 0  # exactly at expires_at the credit is gone
    assert spendable_sompi(row, now=row.expires_at - timedelta(microseconds=1)) == CREDIT_SOMPI

    refund = await open_refund(client, MIN_REFUND)
    cancel_challenge = await challenge_of(client, action="cancel", refund_id=refund.id)
    await set_expiry(sessionmaker, shop.merchant_id, datetime.now(UTC) - timedelta(seconds=1))
    async with sessionmaker() as session:
        assert await expire_due_balances(session, 10) == 1
    # Cancelling after expiry releases the hold, but the released credit is not spendable and the sweep takes it.
    assert (await cancel(client, cancel_challenge, refund.id)).status_code == 200
    released = await balance_of(sessionmaker, shop.merchant_id)
    assert spendable_sompi(released) == 0
    assert (await refund_challenge(client)).status_code == 422
    async with sessionmaker() as session:
        assert await expire_due_balances(session, 10) == 1
    done = await balance_of(sessionmaker, shop.merchant_id)
    assert (done.balance_sompi, done.expired_sompi) == (0, CREDIT_SOMPI)
    assert_identity(done)


async def test_held_wrong_amount_raw_payout_is_refused_before_broadcast() -> None:

    node = SubmittingNode()
    tx = payout_tx([(PAYER_ADDRESS, AMOUNT + 1)], payload=refund_payout_payload(REFUND_ID))
    with pytest.raises(PaymentVerificationError):
        await make_engine(node).submit_payout(wire(tx), refund_payout_payload(REFUND_ID), PAYER_ADDRESS, AMOUNT)
    assert node.submitted == []
