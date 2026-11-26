"""Red-team follow-up of PR #3 at e27163d: L1/L2/I1/I2 fixes, extra cursor edges, residual unsigned disclosure,
and that the credits/status queries can use ix_prepaid_ledger_entries_credit_balance_created.
"""

import uuid

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import PrepaidBalance
from app.refunds import decode_credit_status_cursor
from app.schemas import BuyerCreditStatus
from tests.conftest import PAYER_ADDRESS, Seed, SettingsOverride, Upstream, make_asgi_client, seed_endpoint
from tests.test_prepaid import CREDIT_SOMPI, add_endpoint, fund
from tests.test_redteam_failed_delivery_v2 import (
    STATUS,
    _challenge,
    _credit_n,
    _page,
    _public_key,
    _raw_cursor,
)
from tests.test_refunds import signed


@pytest.fixture
async def shop(sessionmaker: async_sessionmaker[AsyncSession]) -> Seed:
    seed = await seed_endpoint(sessionmaker)
    await add_endpoint(sessionmaker, seed.merchant_id, "cheap")
    return seed


INDEX = "ix_prepaid_ledger_entries_credit_balance_created"


# ================================================================ L1: overflow edges and nonce


OVERFLOW_STAMPS = (
    "9999-12-31T23:59:59-01:00",
    "0001-01-01T00:00:00+01:00",
    "9999-12-31T23:59:59.999999-14:00",  # UTC lands in year 10000
    "0001-01-01T00:00:00+14:00",  # UTC lands in year 0
    "9999-12-31T23:59:59.999999-23:59",
    "0001-01-01T00:00:00.000001+00:01",  # just across the year-0 boundary
)


@pytest.mark.parametrize("stamp", OVERFLOW_STAMPS)
def test_held_decode_maps_overflow_and_underflow_stamps_to_none(stamp: str) -> None:
    token = _raw_cursor(stamp, uuid.uuid4(), uuid.uuid4(), "ab" * 32)
    assert decode_credit_status_cursor(token) is None


@pytest.mark.parametrize(
    "stamp",
    [
        "0000-01-01T00:00:00+00:00",
        "10000-01-01T00:00:00+00:00",
        "9999-12-31T23:59:59-25:00",
        "not-a-time",
    ],
)
def test_held_decode_maps_unparseable_stamps_to_none(stamp: str) -> None:
    token = _raw_cursor(stamp, uuid.uuid4(), uuid.uuid4(), "ab" * 32)
    assert decode_credit_status_cursor(token) is None


@pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00+00:00", "9999-12-31T23:59:59+00:00"])
def test_held_decode_accepts_utc_year_bounds(stamp: str) -> None:
    mid, key = uuid.uuid4(), "ab" * 32
    parsed = decode_credit_status_cursor(_raw_cursor(stamp, mid, mid, key))
    assert parsed is not None and parsed[1] == mid and parsed[3] == key


@pytest.mark.parametrize("stamp", OVERFLOW_STAMPS)
async def test_held_overflow_cursors_are_invalid_cursor_and_do_not_burn_the_nonce(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream, stamp: str
) -> None:
    await _credit_n(client, upstream, 1)
    challenge = await _challenge(client)
    token = _raw_cursor(stamp, uuid.uuid4(), shop.merchant_id, _public_key(PAYER_ADDRESS))
    bad = await client.post(STATUS, json={**signed(challenge), "before": token})
    assert bad.status_code == 400 and bad.json()["error"] == "invalid_cursor", (stamp, bad.text)
    assert "credits" not in bad.json()
    ok = await client.post(STATUS, json=signed(challenge))
    assert ok.status_code == 200, ok.text
    assert BuyerCreditStatus.model_validate(ok.json()).credit_count == 1


async def test_held_a_cursor_at_the_utc_year_bounds_is_200_not_500(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream
) -> None:
    """A well-formed cursor at datetime.min/max UTC is just an empty or full window, never a 500."""
    await _credit_n(client, upstream, 1)
    key = _public_key(PAYER_ADDRESS)
    early = _raw_cursor("0001-01-01T00:00:00+00:00", uuid.UUID(int=0), shop.merchant_id, key)
    late = _raw_cursor("9999-12-31T23:59:59+00:00", uuid.UUID(int=(1 << 128) - 1), shop.merchant_id, key)
    empty = BuyerCreditStatus.model_validate((await _page(client, before=early, limit=100)).json())
    assert empty.credit_count == 1 and empty.credits == [] and empty.next_cursor is None
    full = BuyerCreditStatus.model_validate((await _page(client, before=late, limit=100)).json())
    assert full.credit_count == 1 and len(full.credits) == 1 and full.next_cursor is None


# ================================================================ L2 residuals (unsigned disclosure elsewhere)


async def test_info_public_balance_and_deposit_quote_still_disclose_credit(
    client: httpx.AsyncClient,
    shop: Seed,
    upstream: Upstream,
    override_settings: SettingsOverride,
) -> None:
    """The unsigned challenges no longer carry spendable or refundable, but these public routes still do.

    GET /v1/prepaid/balance returns credit_balance_sompi (and spendable, refundable, deposited, credited) for any
    payer_address. POST /v1/prepaid/deposits/quote returns balance_sompi, which includes unspent credit; its cap
    error names that same figure. #6 is making the public balance spendable-only; even then, spendable minus
    known on-chain deposits (or, today, minus deposited_sompi on this same body) can still reveal credit.
    """
    await fund(client)
    await _credit_n(client, upstream, 1)
    async with make_asgi_client(client, "198.51.100.9") as stranger:
        balance = await stranger.get("/v1/prepaid/balance", params={"slug": "cheap", "payer_address": PAYER_ADDRESS})
        quote = await stranger.post(
            "/v1/prepaid/deposits/quote",
            json={"slug": "cheap", "payer_address": PAYER_ADDRESS, "amount_sompi": 20_000_000_000},
        )
        override_settings(prepaid_max_balance_sompi=60_000_000)
        cap = await stranger.post(
            "/v1/prepaid/deposits/quote",
            json={"slug": "cheap", "payer_address": PAYER_ADDRESS, "amount_sompi": 20_000_000_000},
        )
    body = balance.json()
    assert balance.status_code == 200, balance.text
    assert int(body["credit_balance_sompi"]) > 0
    assert int(body["spendable_sompi"]) - int(body["refundable_sompi"]) == int(body["credit_balance_sompi"])
    assert quote.status_code == 201, quote.text
    assert int(quote.json()["balance_sompi"]) == int(body["balance_sompi"])
    assert cap.status_code == 422 and str(body["balance_sompi"]) in cap.text


async def test_info_an_unsigned_refunds_challenge_still_names_refundable_as_amount_or_in_errors(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream
) -> None:
    """amount_sompi on a default request challenge is the refundable total (deposited unspent, not credit).
    The 422/409 error text also names that figure. Neither path yields credit_balance by itself."""
    await fund(client)
    await _credit_n(client, upstream, 1)
    async with make_asgi_client(client, "198.51.100.10") as stranger:
        default = await stranger.post(
            "/v1/prepaid/refunds/challenge", json={"slug": "cheap", "payer_address": PAYER_ADDRESS}
        )
        too_small = await stranger.post(
            "/v1/prepaid/refunds/challenge",
            json={"slug": "cheap", "payer_address": PAYER_ADDRESS, "amount_sompi": 1},
        )
        too_big = await stranger.post(
            "/v1/prepaid/refunds/challenge",
            json={"slug": "cheap", "payer_address": PAYER_ADDRESS, "amount_sompi": CREDIT_SOMPI + 1},
        )
        credits = await stranger.post(
            "/v1/prepaid/credits/challenge", json={"slug": "cheap", "payer_address": PAYER_ADDRESS}
        )
    assert default.status_code == 201, default.text
    assert default.json()["amount_sompi"] == str(CREDIT_SOMPI)
    assert "spendable_sompi" not in default.json() and "refundable_sompi" not in default.json()
    assert too_small.status_code == 422 and too_small.json()["error"] == "refund_too_small"
    assert str(CREDIT_SOMPI) in too_small.text
    assert too_big.status_code == 409 and too_big.json()["error"] == "insufficient_balance"
    assert str(CREDIT_SOMPI) in too_big.text
    assert credits.status_code == 201 and credits.json()["amount_sompi"] == "0"
    assert "spendable_sompi" not in credits.json() and "refundable_sompi" not in credits.json()


# ================================================================ I2: the partial index serves page and count


async def test_held_credits_status_count_and_page_queries_use_the_credit_balance_created_index(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Both queries in _credit_summary (the total count, and the keyset page) can use the partial index."""
    ids = await _credit_n(client, upstream, 12)
    reply = await _page(client, limit=5)
    cursor_at = BuyerCreditStatus.model_validate(reply.json()).credits[-1].created_at
    cursor_id = BuyerCreditStatus.model_validate(reply.json()).credits[-1].credit_id
    async with sessionmaker() as session:
        indexdef = (
            await session.execute(text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"), {"n": INDEX})
        ).scalar_one()
        assert "balance_id" in indexdef and "created_at" in indexdef and "failed_delivery_credit" in indexdef
        balance_id = (
            await session.execute(select(PrepaidBalance.id).where(PrepaidBalance.merchant_id == shop.merchant_id))
        ).scalar_one()
        await session.execute(text("SET LOCAL enable_seqscan = off"))
        count_plan = "\n".join(
            (
                await session.execute(
                    text(
                        "EXPLAIN SELECT count(*) FROM prepaid_ledger_entries "
                        "WHERE balance_id = :bid AND kind = 'failed_delivery_credit'"
                    ),
                    {"bid": balance_id},
                )
            ).scalars()
        )
        page_plan = "\n".join(
            (
                await session.execute(
                    text(
                        "EXPLAIN SELECT id FROM prepaid_ledger_entries "
                        "WHERE balance_id = :bid AND kind = 'failed_delivery_credit' "
                        "AND (created_at < :at OR (created_at = :at AND id < :cid)) "
                        "ORDER BY created_at DESC, id DESC LIMIT 6"
                    ),
                    {"bid": balance_id, "at": cursor_at, "cid": cursor_id},
                )
            ).scalars()
        )
    assert INDEX in count_plan, count_plan
    assert INDEX in page_plan, page_plan
    assert len(ids) == 12
