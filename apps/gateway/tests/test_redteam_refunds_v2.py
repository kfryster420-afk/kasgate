"""Second red-team round of CONTRACT v2.13 prepaid refunds (feat/prepaid-refund @ 706855a).

Tests named ``test_hole_*`` fail on 706855a: each states an invariant the branch does not keep yet.
Every other test pins down an attack that 706855a already stops.
"""

import asyncio
import json
import shutil
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.requests import Request

from app.ledger import run_ledger_pass, spendable_sompi
from app.models import PrepaidRefundPayout
from app.payments import PaymentVerificationError
from app.refunds import refund_challenge_key
from app.schemas import refund_payout_payload
from app.security import client_ip
from tests.conftest import (
    GATEWAY_DIR,
    OTHER_KEY,
    PAYER_ADDRESS,
    PgServer,
    Seed,
    SettingsOverride,
    StubEngine,
    alembic_config,
    bearer,
)
from tests.test_k402 import FakeNode, make_engine
from tests.test_migration import _diff
from tests.test_prepaid import CREDIT_SOMPI, fund
from tests.test_refund_hardening import age, attempts_of, cancel, claim
from tests.test_refund_payouts import AMOUNT, REFUND_ID, payout_tx, wire
from tests.test_refunds import (
    PAYOUT_TX,
    assert_identity,
    balance_of,
    challenge_of,
    open_refund,
    payout,
    refund_challenge,
    refund_row,
    shop,
    signed,
)

__all__ = ["shop"]

SECOND_TX = "cd" * 32
LAPSED = timedelta(hours=1)


def fake_tx(i: int) -> str:
    return f"{i:064x}"


# ================================================================ HOLE A: claim lapse -> cancel -> late payout


async def test_hole_claim_lapse_lets_the_buyer_cancel_and_spend_a_payout_already_on_chain(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    """The merchant claims, opens the KasWare popup and approves it after the 15 min claim TTL. The buyer (who sees
    `paying` and `claimed_at` in the PUBLIC status, and the payout in their own mempool) cancels the moment the claim
    lapses and spends the released credit. The payout is then recorded late with a shortfall: refunded twice."""
    await fund(client)
    refund = await open_refund(client)
    assert (await claim(client, shop, refund.id)).status_code == 200
    public = await client.get("/v1/prepaid/refunds/status", params={"slug": shop.slug, "payer_address": PAYER_ADDRESS})
    assert public.json()[0]["status"] == "paying" and public.json()[0]["claimed_at"]  # the buyer can time the lapse
    await age(sessionmaker, refund.id, claimed_at=datetime.now(UTC) - LAPSED)
    # The payload-bound payout is already visible to the node (the stub engine verifies it).
    await cancel(client, refund.id)
    balance = await balance_of(sessionmaker, shop.merchant_id)
    # Secure: credit of a refund the merchant claimed must not become spendable while its payout may be in flight
    # (refuse the cancel while a payload-bound payout to the buyer is visible, or keep a cooldown hold).
    assert spendable_sompi(balance) == 0, "released credit is spendable although the payout is already on-chain"


# ================================================================ HOLE B: attempt spam freezes the buyer's credit


async def test_hole_unbounded_unseen_attempts_per_refund(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    """tx_id mode stores the attempt BEFORE the node is asked, so random tx_ids are all kept, all `pending`, all
    re-checked every worker pass with no backoff (payment_not_found is 'unseen'), and each blocks cancel/reject."""
    await fund(client)
    refund = await open_refund(client)
    stub_engine.payout_error = PaymentVerificationError("payment_not_found", "not visible")
    for i in range(1, 26):
        assert (await payout(client, shop, refund.id, tx_id=fake_tx(i))).status_code in (202, 409, 422, 429)
    attempts = await attempts_of(sessionmaker, refund.id)
    pending = [a for a in attempts if a.status == "pending"]
    assert len(pending) <= 5, f"{len(pending)} never-seen attempts kept pending for one refund"


async def test_hole_reposting_an_abandoned_tx_id_restarts_the_freeze(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    """After PREPAID_PAYOUT_ATTEMPT_MAX_SECONDS (7 d) an unseen attempt is abandoned and the buyer may cancel. The
    merchant re-posts the same never-seen tx_id: `_record_attempt` turns it back to `pending` with a NEW created_at,
    so the buyer is blocked for another 7 days, indefinitely."""
    await fund(client)
    refund = await open_refund(client)
    stub_engine.payout_error = PaymentVerificationError("payment_not_found", "not visible")
    assert (await payout(client, shop, refund.id, tx_id=PAYOUT_TX)).status_code == 202
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
    await payout(client, shop, refund.id, tx_id=PAYOUT_TX)  # same never-seen tx_id again
    after = await refund_challenge(client, action="cancel", refund_id=refund.id)
    assert after.status_code == 201, f"buyer frozen again by a re-posted abandoned tx_id: {after.text}"


# ================================================================ HOLE C: late payout supersedes a claimed refund


async def test_hole_late_payout_rejects_a_newer_refund_the_merchant_is_paying(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    """R1 is cancelled, R2 opened for the same credit and CLAIMED (`paying`: the merchant is broadcasting it). R1's
    payout lands late: `_supersede_open_refund` rejects R2 although it is claimed, breaking the claim promise; the
    merchant's R2 broadcast then pays the buyer a second time (late, full shortfall)."""
    await fund(client)
    r1 = await open_refund(client)
    assert (await cancel(client, r1.id)).status_code == 200
    r2 = await open_refund(client)
    assert (await claim(client, shop, r2.id)).status_code == 200
    late = await payout(client, shop, r1.id, tx_id=PAYOUT_TX)
    assert late.status_code == 200 and late.json()["late_paid_from"] == "cancelled"
    row = await refund_row(sessionmaker, r2.id)
    assert row.status == "paying", f"claimed refund R2 was closed as {row.status} by R1's late payout"


# ================================================================ HOLE D: 0008 edited in place


def _old_0008_script_dir(tmp: Path) -> Path:
    git = shutil.which("git")
    if git is None:
        pytest.skip("needs git to read the first 0008 from 15d7bd4")
    try:
        old = subprocess.run(  # noqa: S603 - fixed arguments, no shell
            [git, "show", "15d7bd4:apps/gateway/alembic/versions/0008_prepaid_refunds.py"],
            cwd=GATEWAY_DIR,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("needs git history with 15d7bd4 (the first 0008)")
    script = tmp / "alembic"
    shutil.copytree(GATEWAY_DIR / "alembic", script, ignore=shutil.ignore_patterns("__pycache__"))
    (script / "versions" / "0008_prepaid_refunds.py").write_text(old)
    return script


async def test_hole_a_database_at_the_first_0008_never_gets_the_revised_schema(
    pg_server: PgServer, tmp_path: Path
) -> None:
    """A testnet DB migrated with 15d7bd4 is stamped 0008_prepaid_refunds. The revised 0008 keeps the same revision
    id, so `alembic upgrade head` is a no-op: no prepaid_refund_payouts, no `paying`, no late-payout columns."""
    url = pg_server.make_url("kasgate_old0008")
    pg_server.create_database("kasgate_old0008")
    old = alembic_config(url)
    old.set_main_option("script_location", str(_old_0008_script_dir(tmp_path)))
    await asyncio.to_thread(command.upgrade, old, "head")
    await asyncio.to_thread(command.upgrade, alembic_config(url), "head")  # the revised scripts
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            diff = await conn.run_sync(_diff)
    finally:
        await engine.dispose()
    kinds = sorted({str(d[0][0] if isinstance(d, list) else d[0]) for d in diff})
    assert diff == [], f"upgrade head left {len(diff)} schema differences ({', '.join(kinds)})"


# ================================================================ held: nonce, signed status, payload, proxies


async def test_held_a_wrong_refund_cancel_keeps_the_nonce_but_can_never_cancel_another_refund(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    r1 = await open_refund(client, CREDIT_SOMPI // 2)
    signed_r1 = signed(await challenge_of(client, action="cancel", refund_id=r1.id))
    probe = await client.post(f"/v1/prepaid/refunds/{uuid.uuid4()}/cancel", json=signed_r1)
    assert probe.status_code == 400  # not consumed, and it says nothing about other refunds
    # The buyer cancels r1 with a fresh challenge and opens r2: the unused r1 challenge cannot touch r2.
    assert (await cancel(client, r1.id)).status_code == 200
    r2 = await open_refund(client, CREDIT_SOMPI // 2)
    replay = await client.post(f"/v1/prepaid/refunds/{r2.id}/cancel", json=signed_r1)
    assert replay.status_code == 400 and (await refund_row(sessionmaker, r2.id)).status == "requested"
    # Used challenges never work twice.
    fresh = signed(await challenge_of(client, action="cancel", refund_id=r2.id))
    assert (await client.post(f"/v1/prepaid/refunds/{r2.id}/cancel", json=fresh)).status_code == 200
    assert (await client.post(f"/v1/prepaid/refunds/{r2.id}/cancel", json=fresh)).status_code in (409, 410)


async def test_held_signed_status_is_single_use_key_bound_and_action_bound(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    refund = await open_refund(client)
    rejected = await client.post(
        f"/v1/prepaid/refunds/{refund.id}/reject", json={"reason": "secret merchant note"}, headers=bearer(shop.api_key)
    )
    assert rejected.status_code == 200
    public = await client.get("/v1/prepaid/refunds/status", params={"slug": shop.slug, "payer_address": PAYER_ADDRESS})
    assert "secret merchant note" not in public.text
    status = await challenge_of(client, action="status")
    assert (await client.post("/v1/prepaid/refunds/status", json=signed(status, OTHER_KEY))).status_code == 400
    assert (await client.post("/v1/prepaid/refunds", json=signed(status))).status_code == 400  # not a request
    ok = await client.post("/v1/prepaid/refunds/status", json=signed(status))
    assert ok.status_code == 200 and ok.json()[0]["reason"] == "secret merchant note"
    assert (await client.post("/v1/prepaid/refunds/status", json=signed(status))).status_code in (409, 410)


async def test_held_signed_status_expires(client: httpx.AsyncClient, shop: Seed, redis: Any) -> None:
    await fund(client)
    status = await challenge_of(client, action="status")
    key = refund_challenge_key(status.nonce)
    stored = json.loads(await redis.get(key))
    stored["expiresAt"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await redis.set(key, json.dumps(stored))
    expired = await client.post("/v1/prepaid/refunds/status", json=signed(status))
    assert expired.status_code == 410 and expired.json()["error"] == "challenge_expired"


async def test_held_a_raw_payout_for_another_refund_id_is_refused_locally() -> None:
    other = uuid.UUID("fedcba9876543210fedcba9876543210")
    tx = payout_tx([(PAYER_ADDRESS, AMOUNT)], payload=refund_payout_payload(other))
    with pytest.raises(PaymentVerificationError) as info:
        make_engine(FakeNode()).payout_tx_id(wire(tx), refund_payout_payload(REFUND_ID), PAYER_ADDRESS, AMOUNT)
    assert info.value.code == "invalid_payout"


async def test_held_an_accepted_payout_with_an_unseen_payload_never_pays(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await fund(client)
    refund = await open_refund(client)
    stub_engine.payout_error = PaymentVerificationError("payload_unverified", "accepted, payload never seen")
    assert (await payout(client, shop, refund.id, tx_id=PAYOUT_TX)).status_code == 202
    await run_ledger_pass(sessionmaker, stub_engine)
    row = await refund_row(sessionmaker, refund.id)
    [attempt] = await attempts_of(sessionmaker, refund.id)
    assert row.status == "payout_submitted" and attempt.status == "pending" and not attempt.payload_verified
    assert (await balance_of(sessionmaker, shop.merchant_id)).refunded_sompi == 0


async def test_held_late_payout_never_drives_the_balance_negative_when_a_newer_payout_holds_it(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await fund(client)
    r1 = await open_refund(client)
    assert (await cancel(client, r1.id)).status_code == 200
    r2 = await open_refund(client)
    stub_engine.payout_error = PaymentVerificationError("payment_not_found", "not yet")
    assert (await payout(client, shop, r2.id, tx_id=SECOND_TX)).status_code == 202
    stub_engine.payout_error = None
    late = await payout(client, shop, r1.id, tx_id=PAYOUT_TX)
    assert late.status_code == 200 and late.json()["shortfall_sompi"] == str(CREDIT_SOMPI)
    balance = await balance_of(sessionmaker, shop.merchant_id)
    assert balance.balance_sompi == CREDIT_SOMPI and balance.refund_hold_sompi == CREDIT_SOMPI
    assert (await refund_row(sessionmaker, r2.id)).status == "payout_submitted"
    assert_identity(balance)


async def test_held_a_tx_id_of_one_refund_cannot_pay_another_even_after_abandonment(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    await fund(client)
    r1 = await open_refund(client, CREDIT_SOMPI // 2)
    stub_engine.payout_error = PaymentVerificationError("payment_not_found", "not visible")
    assert (await payout(client, shop, r1.id, tx_id=PAYOUT_TX)).status_code == 202
    async with sessionmaker() as session:
        await session.execute(
            update(PrepaidRefundPayout)
            .where(PrepaidRefundPayout.refund_id == r1.id)
            .values(created_at=datetime.now(UTC) - timedelta(days=30))
        )
        await session.commit()
    await run_ledger_pass(sessionmaker, stub_engine)
    assert (await cancel(client, r1.id)).status_code == 200
    r2 = await open_refund(client, CREDIT_SOMPI // 2)
    stub_engine.payout_error = None
    reused = await payout(client, shop, r2.id, tx_id=PAYOUT_TX)
    assert reused.status_code == 409 and reused.json()["error"] == "payout_replayed"


def _req(peer: str, *forwarded: str) -> Request:
    headers = [(b"x-forwarded-for", value.encode()) for value in forwarded]
    return Request({"type": "http", "client": (peer, 1234), "headers": headers})


def test_held_x_forwarded_for_spoofing(override_settings: SettingsOverride) -> None:
    override_settings(trusted_proxy_ips='["10.0.0.0/8"]')
    # Untrusted peer: every XFF ignored.
    assert client_ip(_req("198.51.100.7", "10.0.0.1", "203.0.113.9")) == "198.51.100.7"
    # Trusted proxy appends the real client: a spoofed left-most hop never wins.
    assert client_ip(_req("10.0.0.2", "1.1.1.1, 203.0.113.9")) == "203.0.113.9"
    # Several XFF headers are joined in order; the right-most untrusted hop wins.
    assert client_ip(_req("10.0.0.2", "1.1.1.1", "203.0.113.9")) == "203.0.113.9"
    # A client posing as an internal hop is skipped only if it is right of the real client, which a proxy that
    # appends never produces; an injected trusted-looking value on the left is ignored.
    assert client_ip(_req("10.0.0.2", "10.9.9.9, 203.0.113.9, 10.0.0.3")) == "203.0.113.9"
    # Garbage or ports stop the walk at the last trusted hop (shared bucket, never an attacker-chosen one).
    assert client_ip(_req("10.0.0.2", "203.0.113.9:443")) == "10.0.0.2"
    assert client_ip(_req("10.0.0.2", "not-an-ip")) == "10.0.0.2"
    # IPv4-mapped IPv6 peers are not treated as trusted IPv4 proxies.
    assert client_ip(_req("::ffff:10.0.0.2", "203.0.113.9")) == "::ffff:10.0.0.2"


async def test_held_0008_roundtrips_from_0007(pg_server: PgServer) -> None:
    url = pg_server.make_url("kasgate_rt0008")
    pg_server.create_database("kasgate_rt0008")
    config = alembic_config(url)
    await asyncio.to_thread(command.upgrade, config, "head")
    await asyncio.to_thread(command.downgrade, config, "0007_trials_webhooks")
    await asyncio.to_thread(command.upgrade, config, "head")
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            assert await conn.run_sync(_diff) == []
    finally:
        await engine.dispose()
