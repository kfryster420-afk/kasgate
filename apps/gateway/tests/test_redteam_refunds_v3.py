"""Third red-team round of CONTRACT v2.13 prepaid refunds (feat/prepaid-refund @ 9c13ae2).

Tests named ``test_hole_*`` fail on 9c13ae2: each states an invariant the branch does not keep yet.
``test_info_*`` pin a behaviour worth knowing (they pass). Every other test pins an attack 9c13ae2 stops.
"""

import asyncio
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic import command
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.ledger import run_ledger_pass, spendable_sompi
from app.models import PrepaidRefundPayout
from app.payments import PaymentVerificationError, PayoutSighting
from app.schema_guard import check_database_schema
from tests.conftest import GATEWAY_DIR, PAYER_ADDRESS, PgServer, Seed, StubEngine, alembic_config
from tests.test_k402 import FakeNode, _script_hex, make_engine
from tests.test_migration import _diff
from tests.test_prepaid import fund
from tests.test_prepaid_balance_privacy import refunds_status_headers
from tests.test_refund_hardening import age, attempts_of, cancel, claim
from tests.test_refund_payouts import AMOUNT, REFUND_ID, PayloadMempoolNode, bound_tx, payout_tx
from tests.test_refunds import (
    PAYOUT_TX,
    assert_identity,
    balance_of,
    open_refund,
    payout,
    refund_challenge,
    refund_row,
    shop,
)

__all__ = ["shop"]

LAPSED = timedelta(hours=1)


def tx_of(i: int) -> str:
    return f"{i:064x}"


async def claimed_and_lapsed(client: httpx.AsyncClient, seed: Seed, maker: async_sessionmaker[AsyncSession]):  # type: ignore[no-untyped-def]
    await fund(client)
    refund = await open_refund(client)
    assert (await claim(client, seed, refund.id)).status_code == 200
    await age(maker, refund.id, claimed_at=datetime.now(UTC) - LAPSED)
    return refund


async def age_attempts(maker: async_sessionmaker[AsyncSession], refund_id: object, days: int) -> None:
    async with maker() as session:
        await session.execute(
            update(PrepaidRefundPayout)
            .where(PrepaidRefundPayout.refund_id == refund_id)
            .values(created_at=datetime.now(UTC) - timedelta(days=days), next_check_at=datetime.now(UTC))
        )
        await session.commit()


# ================================================================ HOLE 1: cooldown released under a pending payout


async def test_hole_cooldown_hold_is_released_while_a_payout_posted_in_the_cooldown_is_still_pending(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    """Claim lapses, the buyer cancels (nothing on-chain yet): 30 min cooldown hold. The merchant broadcasts and
    posts the tx_id INSIDE the cooldown, but it cannot be confirmed yet (accepted before the post, so
    `payload_unverified`; or a node outage with backoff). `release_cooldown_holds` releases the hold anyway once
    `hold_until` passes, the buyer spends, and the payout is recorded late with a shortfall: the double refund the
    cooldown exists to prevent, for a payout that landed within it."""
    refund = await claimed_and_lapsed(client, shop, sessionmaker)
    assert (await cancel(client, refund.id)).status_code == 200
    stub_engine.payout_error = PaymentVerificationError("payload_unverified", "accepted, payload not seen yet")
    posted = await payout(client, shop, refund.id, tx_id=PAYOUT_TX)
    assert posted.status_code == 202
    [attempt] = await attempts_of(sessionmaker, refund.id)
    assert attempt.status == "pending"
    await age(sessionmaker, refund.id, hold_until=datetime.now(UTC) - timedelta(seconds=1))
    await run_ledger_pass(sessionmaker, stub_engine)
    balance = await balance_of(sessionmaker, shop.merchant_id)
    assert spendable_sompi(balance) == 0, "cooldown hold released although a payout posted within it is pending"


# ================================================================ HOLE 2 (Low): the attempt cap blocks a proven payout


async def test_hole_three_unseen_tx_ids_block_a_proven_transaction_payout_for_seven_days(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    """Three typo'd (never-seen) tx_ids fill the cap. The merchant then posts the signed transaction, which the
    gateway can verify and broadcast itself, and gets 409 too_many_attempts until the typos are abandoned (7 d);
    there is no route to withdraw an attempt, so buyer and merchant are both stuck for a week."""
    await fund(client)
    refund = await open_refund(client)
    stub_engine.payout_error = PaymentVerificationError("payment_not_found", "not visible")
    for i in range(1, 4):
        assert (await payout(client, shop, refund.id, tx_id=tx_of(i))).status_code == 202
    stub_engine.payout_error = None
    proven = await payout(client, shop, refund.id, transaction='{"signed": "tx"}')
    assert proven.status_code in (200, 202), f"a verifiable signed payout was refused: {proven.text}"


# ================================================================ held: the pre-cancel scan


async def test_held_third_party_exact_amount_utxos_block_a_cancel_only_boundedly(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    """A griefer (or the buyer) sends 5 exact-amount UTXOs: at most 3 are recorded (cap), the cancel is refused,
    they can never be confirmed (no payload), and after PREPAID_PAYOUT_ATTEMPT_MAX_SECONDS they are abandoned and,
    being known, no longer block. Griefing costs the griefer 5 x the amount, paid to the buyer, per 7 days."""
    refund = await claimed_and_lapsed(client, shop, sessionmaker)
    stub_engine.payout_sightings = [PayoutSighting(tx_of(i), payload_seen=False) for i in range(1, 6)]
    refused = await cancel(client, refund.id)
    assert refused.status_code == 409
    attempts = await attempts_of(sessionmaker, refund.id)
    assert len(attempts) == 3 and {a.method for a in attempts} == {"scan"}
    assert not any(a.payload_verified for a in attempts)
    # They never pay: accepted outputs without a seen payload.
    stub_engine.payout_error = PaymentVerificationError("payload_unverified", "payload never seen")
    await run_ledger_pass(sessionmaker, stub_engine)
    assert (await refund_row(sessionmaker, refund.id)).status != "paid"
    await age_attempts(sessionmaker, refund.id, days=30)
    await run_ledger_pass(sessionmaker, stub_engine)
    assert {a.status for a in await attempts_of(sessionmaker, refund.id)} == {"abandoned"}
    # The two unrecorded sightings are new now and block once more (bounded by the cap again) ...
    second = await cancel(client, refund.id)
    assert second.status_code == 409
    await age_attempts(sessionmaker, refund.id, days=30)
    await run_ledger_pass(sessionmaker, stub_engine)
    # ... then everything is known and the cancel goes through, with the cooldown hold.
    done = await cancel(client, refund.id)
    assert done.status_code == 200 and done.json()["hold_until"] is not None
    assert_identity(await balance_of(sessionmaker, shop.merchant_id))


async def test_held_scan_mempool_sightings_precede_utxo_ones_so_a_real_payout_is_not_crowded_out() -> None:
    """The real payout (payload-bound, in the mempool) is reported first, so 3 decoy UTXOs cannot push it past
    the cap of recorded scan attempts while it is still in the mempool."""
    node = PayloadMempoolNode()
    for i in range(3):
        node.accept_into_utxo_index(payout_tx([(PAYER_ADDRESS, AMOUNT)], payload=f"decoy{i}".encode()))
    real = bound_tx([(PAYER_ADDRESS, AMOUNT)])
    node.add_to_mempool(real)
    sightings = await make_engine(node).find_refund_payouts(PAYER_ADDRESS, AMOUNT, REFUND_ID)
    assert sightings[0] == PayoutSighting(real.id, payload_seen=True) and len(sightings) == 4


async def test_info_scan_ignores_utxo_outputs_older_than_the_first_claim() -> None:
    """The UTXO-index branch ignores outputs whose blockDaaScore is at or below the virtual DAA stored at the
    first claim, so a buyer UTXO that was already there cannot block the cancel. Mempool (payload-bound) sightings
    are not filtered. Without a claim-time score (`min_daa_score=0`) every exact-amount UTXO still counts."""
    node = FakeNode()
    old, fresh = tx_of(9), tx_of(10)
    node.utxos[old] = [(0, AMOUNT, _script_hex(PAYER_ADDRESS))]
    node.utxos[fresh] = [(0, AMOUNT, _script_hex(PAYER_ADDRESS))]
    node.utxo_daa[old] = 50
    node.utxo_daa[fresh] = 200
    unfiltered = await make_engine(node).find_refund_payouts(PAYER_ADDRESS, AMOUNT, REFUND_ID)
    assert {s.tx_id for s in unfiltered} == {old, fresh}
    found = await make_engine(node).find_refund_payouts(PAYER_ADDRESS, AMOUNT, REFUND_ID, min_daa_score=100)
    assert [s.tx_id for s in found] == [fresh]


async def test_info_claim_state_is_still_observable_without_a_signature(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The public view hides `paying`, but the unauthenticated cancel challenge still says "paying right now", and
    the public `updated_at` moves on every claim. Harmless now (scan + cooldown), but not a secrecy boundary."""
    await fund(client)
    refund = await open_refund(client)
    before = (
        await client.get(
            "/v1/prepaid/refunds/status",
            params={"slug": shop.slug, "payer_address": PAYER_ADDRESS},
            headers=refunds_status_headers(slug=shop.slug),
        )
    ).json()[0]
    await asyncio.sleep(0.01)
    assert (await claim(client, shop, refund.id)).status_code == 200
    after = (
        await client.get(
            "/v1/prepaid/refunds/status",
            params={"slug": shop.slug, "payer_address": PAYER_ADDRESS},
            headers=refunds_status_headers(slug=shop.slug),
        )
    ).json()[0]
    assert after["status"] == "requested" and after["updated_at"] != before["updated_at"]
    probe = await refund_challenge(client, action="cancel", refund_id=refund.id)
    assert probe.status_code == 409 and "paying" in probe.json()["message"]


async def test_held_claim_expires_at_never_exceeds_the_total_window_and_matches_the_cancel_rule(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    refund = await open_refund(client)
    assert (await claim(client, shop, refund.id)).status_code == 200
    first = datetime.now(UTC) - timedelta(minutes=59, seconds=30)
    await age(sessionmaker, refund.id, first_claimed_at=first)
    renewed = await claim(client, shop, refund.id)
    expires = datetime.fromisoformat(renewed.json()["claim_expires_at"])
    assert expires == first + timedelta(seconds=3600)
    assert (await refund_challenge(client, action="cancel", refund_id=refund.id)).status_code == 409
    await age(sessionmaker, refund.id, first_claimed_at=first - timedelta(seconds=31))
    assert (await refund_challenge(client, action="cancel", refund_id=refund.id)).status_code == 201


# ================================================================ held: schema guard


def _script_dir_with_0008_from(rev: str, tmp: Path) -> Path:
    git = shutil.which("git")
    if git is None:
        pytest.skip("needs git to read an earlier 0008 draft")
    try:
        old = subprocess.run(  # noqa: S603 - fixed arguments, no shell
            [git, "show", f"{rev}:apps/gateway/alembic/versions/0008_prepaid_refunds.py"],
            cwd=GATEWAY_DIR,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip(f"needs git history with {rev}")
    script = tmp / "alembic"
    shutil.copytree(GATEWAY_DIR / "alembic", script, ignore=shutil.ignore_patterns("__pycache__"))
    (script / "versions" / "0008_prepaid_refunds.py").write_text(old)
    return script


async def test_held_a_706855a_draft_0008_is_refused_by_every_forward_command_and_the_app(
    pg_server: PgServer, tmp_path: Path
) -> None:
    url = pg_server.make_url("kasgate_draft2")
    pg_server.create_database("kasgate_draft2")
    old = alembic_config(url)
    old.set_main_option("script_location", str(_script_dir_with_0008_from("706855a", tmp_path)))
    await asyncio.to_thread(command.upgrade, old, "head")
    new = alembic_config(url)
    for run in (
        lambda: command.upgrade(new, "head"),
        lambda: command.upgrade(new, "heads"),
        lambda: command.upgrade(new, "0008_prepaid_refunds"),
        lambda: command.stamp(new, "head"),
    ):
        with pytest.raises(RuntimeError, match="earlier draft of revision 0008_prepaid_refunds"):
            await asyncio.to_thread(run)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        with pytest.raises(RuntimeError):
            await check_database_schema(engine)
        await asyncio.to_thread(command.downgrade, new, "0007_trials_webhooks")
        await asyncio.to_thread(command.upgrade, new, "head")
        await check_database_schema(engine)
        async with engine.connect() as conn:
            assert await conn.run_sync(_diff) == []
    finally:
        await engine.dispose()
