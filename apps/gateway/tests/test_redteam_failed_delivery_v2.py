"""Red-team round 2 of PR #3 (failed-delivery credit, CONTRACT v2.14) at 79efeff: the round-1 fixes and the paginated
signed POST /v1/prepaid/credits/status (credit_id, before/limit, next_cursor, invalid_cursor).

Tests named ``test_hole_*`` fail on 79efeff: each one states an invariant the branch does not keep yet.
Tests named ``test_info_*`` pin down behaviour worth knowing that is not a defect on its own.
Every other test pins down an attack that the branch already stops.
"""

import asyncio
import uuid
from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.proxy as proxy_module
from app.failed_delivery import credit_failed_delivery, sweep_failed_deliveries
from app.models import PrepaidBalance, PrepaidLedgerEntry, Settlement
from app.refunds import encode_credit_status_cursor
from app.schemas import HEADER_FAILED_DELIVERY_CREDIT, BuyerCreditStatus, PrepaidCreditStatusChallenge
from tests.conftest import (
    OTHER_ADDRESS,
    OTHER_KEY,
    PAYER_ADDRESS,
    PAYER_KEY,
    Seed,
    SettingsOverride,
    Upstream,
    bearer,
    make_asgi_client,
    paid_headers,
    seed_endpoint,
)
from tests.test_failed_delivery_credit import SPLIT, WINDOW_PAST, _age, _receipt
from tests.test_prepaid import add_endpoint, fund
from tests.test_refunds import signed

POLICY = "/v1/prepaid/failed-delivery-policy"
STATUS = "/v1/prepaid/credits/status"
CREDIT_KIND = "failed_delivery_credit"


@pytest.fixture
async def shop(sessionmaker: async_sessionmaker[AsyncSession]) -> Seed:
    seed = await seed_endpoint(sessionmaker)
    await add_endpoint(sessionmaker, seed.merchant_id, "cheap")
    return seed


def _refuse(upstream: Upstream) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    upstream.handler = handler


async def _credit_n(
    client: httpx.AsyncClient, upstream: Upstream, n: int, *, slug: str = "weather", start: int = 0
) -> list[uuid.UUID]:
    """n distinct paid calls at `slug` that fail three times: n failed-delivery credits. Returns their creditIds."""
    _refuse(upstream)
    ids: list[uuid.UUID] = []
    for i in range(start, start + n):
        headers = await paid_headers(client, f"{i:02x}" * 32, slug)
        responses = [await client.get(f"/p/{slug}", headers=headers) for _ in range(3)]
        assert [r.status_code for r in responses] == [502, 502, 502], responses[-1].text
        ids.append(_receipt(responses[-1]).creditId)
    upstream.handler = None
    return ids


async def _challenge(
    client: httpx.AsyncClient, *, slug: str = "cheap", payer: str = PAYER_ADDRESS
) -> PrepaidCreditStatusChallenge:
    response = await client.post("/v1/prepaid/credits/challenge", json={"slug": slug, "payer_address": payer})
    assert response.status_code == 201, response.text
    return PrepaidCreditStatusChallenge.model_validate(response.json())


async def _page(
    client: httpx.AsyncClient,
    *,
    before: str | None = None,
    limit: int | None = None,
    slug: str = "cheap",
    payer: str = PAYER_ADDRESS,
    key=PAYER_KEY,  # noqa: ANN001
) -> httpx.Response:
    challenge = await _challenge(client, slug=slug, payer=payer)
    body: dict[str, object] = dict(signed(challenge, key))
    if before is not None:
        body["before"] = before
    if limit is not None:
        body["limit"] = limit
    return await client.post(STATUS, json=body)


async def _walk(client: httpx.AsyncClient, limit: int) -> tuple[list[uuid.UUID], list[int]]:
    """Every page, newest first. Returns (credit ids in order, credit_count of each page)."""
    seen: list[uuid.UUID] = []
    counts: list[int] = []
    before: str | None = None
    for _ in range(50):
        reply = await _page(client, before=before, limit=limit)
        assert reply.status_code == 200, reply.text
        page = BuyerCreditStatus.model_validate(reply.json())
        assert len(page.credits) <= limit
        seen.extend(c.credit_id for c in page.credits)
        counts.append(page.credit_count)
        if page.next_cursor is None:
            return seen, counts
        before = page.next_cursor
    raise AssertionError("pagination never ended")


async def _credit_rows(maker: async_sessionmaker[AsyncSession]) -> list[PrepaidLedgerEntry]:
    async with maker() as session:
        return list(
            (
                await session.execute(
                    select(PrepaidLedgerEntry)
                    .where(PrepaidLedgerEntry.kind == CREDIT_KIND)
                    .order_by(PrepaidLedgerEntry.created_at.desc(), PrepaidLedgerEntry.id.desc())
                )
            ).scalars()
        )


async def _set_created_at(maker: async_sessionmaker[AsyncSession], ids: list[uuid.UUID], at: datetime) -> None:
    async with maker() as session:
        await session.execute(update(PrepaidLedgerEntry).where(PrepaidLedgerEntry.id.in_(ids)).values(created_at=at))
        await session.commit()


def _raw_cursor(stamp: str, credit_id: uuid.UUID, merchant_id: uuid.UUID, public_key: str, version: str = "v1") -> str:
    payload = "|".join((version, stamp, str(credit_id), str(merchant_id), public_key))
    return urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _public_key(address: str) -> str:
    from app.refunds import payer_public_key

    key = payer_public_key(address)
    assert key is not None
    return key


# ================================================================ round-1 findings, re-checked and extended


async def test_held_f1_coverage_is_stamped_at_settle_so_toggles_in_the_window_change_nothing(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """A timeout paid while timeouts were credited stays credited after the merchant flips enabled off/on AND
    excludes timeouts inside the redelivery window; a timeout paid after the exclusion is never credited."""

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    upstream.handler = timeout
    covered = await client.get("/p/weather", headers=await paid_headers(client, "a1" * 32))
    assert covered.status_code == 502
    auth = bearer(seeded.api_key)
    for body in ({"enabled": False}, {"enabled": True}, {"credit_timeouts": False}):
        changed = await client.put(POLICY, json=body, headers=auth)
        assert changed.status_code == 200, changed.text
    excluded = await client.get("/p/weather", headers=await paid_headers(client, "a2" * 32))
    assert excluded.status_code == 502
    # Turning timeouts back on is not retroactive for the call paid while they were excluded.
    assert (await client.put(POLICY, json={"credit_timeouts": True}, headers=auth)).status_code == 200
    await _age(sessionmaker, WINDOW_PAST)
    assert await sweep_failed_deliveries(sessionmaker, limit=10) == 1
    rows = await _credit_rows(sessionmaker)
    assert [r.tx_id for r in rows] == ["a1" * 32]


async def test_held_f2_pending_outcome_is_never_credited_even_long_after_the_window(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """A settlement whose outcome stayed `pending` (process died mid-delivery) or NULL is never credited, by the
    inline path or the sweep, however old it is."""
    served = await client.get("/p/weather", headers=await paid_headers(client, "b1" * 32))
    assert served.status_code == 200
    async with sessionmaker() as session:
        await session.execute(update(Settlement).values(delivery_outcome="pending", upstream_status_code=None))
        await session.commit()
    await _age(sessionmaker, WINDOW_PAST * 10)
    assert await sweep_failed_deliveries(sessionmaker, limit=10) == 0
    async with sessionmaker() as session:
        settlement_id = (await session.execute(select(Settlement.id))).scalar_one()
    async with sessionmaker() as session:
        assert await credit_failed_delivery(session, settlement_id) is None
    assert await _credit_rows(sessionmaker) == []


@pytest.mark.parametrize("status", [400, 404, 409, 422, 429])
async def test_held_f3_buyer_forced_4xx_answers_are_never_credited(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    status: int,
) -> None:
    upstream.handler = lambda request: httpx.Response(status, text="no")
    headers = await paid_headers(client, "c1" * 32)
    responses = [await client.get("/p/weather", headers=headers) for _ in range(3)]
    assert all(HEADER_FAILED_DELIVERY_CREDIT not in r.headers for r in responses)
    await _age(sessionmaker, WINDOW_PAST)
    assert await sweep_failed_deliveries(sessionmaker, limit=10) == 0
    assert await _credit_rows(sessionmaker) == []


async def test_held_f3_an_oversized_5xx_answer_is_not_credited_either(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The oversized rule wins over the 5xx rule: the upstream answered, the gateway refused to relay it."""
    monkeypatch.setattr(proxy_module, "MAX_RESPONSE_BODY_BYTES", 1024)
    upstream.handler = lambda request: httpx.Response(503, content=b"x" * 4096)
    headers = await paid_headers(client, "c2" * 32)
    responses = [await client.get("/p/weather", headers=headers) for _ in range(3)]
    assert all(HEADER_FAILED_DELIVERY_CREDIT not in r.headers for r in responses)
    await _age(sessionmaker, WINDOW_PAST)
    assert await sweep_failed_deliveries(sessionmaker, limit=10) == 0
    assert await _credit_rows(sessionmaker) == []


async def test_held_f4_concurrent_credits_near_the_cap_never_exceed_it(
    client: httpx.AsyncClient,
    seeded: Seed,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
    override_settings: SettingsOverride,
) -> None:
    """Four failed calls credited in parallel against a cap that fits less than one share: the balance row lock
    serialises them, the balance ends exactly at the cap and every sompi not credited is a recorded shortfall."""
    cap = SPLIT.merchant_sompi + SPLIT.merchant_sompi // 2
    override_settings(prepaid_max_balance_sompi=cap)
    _refuse(upstream)
    for i in range(4):
        assert (await client.get("/p/weather", headers=await paid_headers(client, f"d{i}" * 32))).status_code == 502
    upstream.handler = None
    await _age(sessionmaker, WINDOW_PAST)
    async with sessionmaker() as session:
        ids = list((await session.execute(select(Settlement.id))).scalars())

    async def one(settlement_id: uuid.UUID) -> object:
        async with sessionmaker() as session:
            return await credit_failed_delivery(session, settlement_id)

    outcomes = await asyncio.gather(*(one(i) for i in ids))
    assert all(o is not None for o in outcomes)
    rows = await _credit_rows(sessionmaker)
    assert len(rows) == 4
    assert sum(r.amount_sompi for r in rows) == cap
    assert sum(r.shortfall_sompi for r in rows) == 4 * SPLIT.merchant_sompi - cap
    async with sessionmaker() as session:
        balance = (await session.execute(select(PrepaidBalance))).scalar_one()
    assert balance.balance_sompi == balance.credited_sompi == balance.credit_balance_sompi == cap


# ================================================================ pagination: cursor binding and forgery


async def test_held_a_cursor_forged_with_the_victims_key_and_merchant_is_refused(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream
) -> None:
    """The attacker knows the victim's public key (it is in the victim's address) and the merchant id (it is on
    every challenge). A cursor built for them, sent with the attacker's own signed challenge, is invalid_cursor."""
    victim_ids = await _credit_n(client, upstream, 3)
    forged = encode_credit_status_cursor(
        created_at=datetime.now(UTC) + timedelta(days=1),
        credit_id=uuid.UUID(int=(1 << 128) - 1),
        merchant_id=shop.merchant_id,
        public_key=_public_key(PAYER_ADDRESS),
    )
    reply = await _page(client, before=forged, payer=OTHER_ADDRESS, key=OTHER_KEY)
    assert reply.status_code == 400 and reply.json()["error"] == "invalid_cursor"
    text = reply.text
    assert all(str(i) not in text for i in victim_ids) and "credit_count" not in text


async def test_held_a_cursor_forged_for_the_own_key_at_any_position_returns_only_own_credits(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The cursor carries no MAC, so a buyer can point it anywhere, including at another buyer's credit_id and
    created_at. That only moves the window over the buyer's own rows: the query is filtered by the balance."""
    mine = await _credit_n(client, upstream, 2)
    other_shop = await seed_endpoint(sessionmaker, slug="other-weather", address=OTHER_ADDRESS)
    theirs = await _credit_n(client, upstream, 2, slug="other-weather", start=0x40)
    theirs_rows = [r for r in await _credit_rows(sessionmaker) if r.merchant_id == other_shop.merchant_id]
    target = theirs_rows[0]
    own_key = _public_key(PAYER_ADDRESS)
    for at, credit_id in (
        (target.created_at, target.id),
        (datetime(9999, 1, 1, tzinfo=UTC), uuid.UUID(int=(1 << 128) - 1)),
        (datetime(1970, 1, 1, tzinfo=UTC), uuid.UUID(int=0)),
    ):
        cursor = encode_credit_status_cursor(
            created_at=at, credit_id=credit_id, merchant_id=shop.merchant_id, public_key=own_key
        )
        reply = await _page(client, before=cursor, limit=100)
        assert reply.status_code == 200, reply.text
        page = BuyerCreditStatus.model_validate(reply.json())
        assert page.merchant_id == shop.merchant_id and page.credit_count == 2
        assert {c.credit_id for c in page.credits} <= set(mine)
        assert {c.credit_id for c in page.credits}.isdisjoint(theirs)


@pytest.mark.parametrize(
    "token",
    [
        "",  # treated as "no cursor" by the validator; checked separately below
        "=",
        "%%%",
        "\u00e9t\u00e9",
        "A" * 513,
        "djE",  # "v1" only
    ],
)
async def test_held_garbage_cursors_are_invalid_cursor_or_first_page_never_a_500(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream, token: str
) -> None:
    await _credit_n(client, upstream, 1)
    reply = await _page(client, before=token)
    if token == "":
        assert reply.status_code == 200 and BuyerCreditStatus.model_validate(reply.json()).credit_count == 1
    else:
        assert reply.status_code == 400 and reply.json()["error"] == "invalid_cursor", reply.text


async def test_held_structurally_wrong_cursors_are_invalid_cursor(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream
) -> None:
    await _credit_n(client, upstream, 1)
    key = _public_key(PAYER_ADDRESS)
    now = datetime.now(UTC).isoformat()
    bad = [
        _raw_cursor(now, uuid.uuid4(), shop.merchant_id, key, version="v2"),
        _raw_cursor(now, uuid.uuid4(), shop.merchant_id, key.upper()),
        _raw_cursor("2026-11-27T00:00:00", uuid.uuid4(), shop.merchant_id, key),  # naive timestamp
        _raw_cursor(now, uuid.uuid4(), shop.merchant_id, key) + "fQ",  # trailing junk
        _raw_cursor(now, uuid.uuid4(), shop.merchant_id, key + "|extra"),
        _raw_cursor("yesterday", uuid.uuid4(), shop.merchant_id, key),
        _raw_cursor(now, uuid.uuid4(), uuid.uuid4(), key),  # another merchant
    ]
    for token in bad:
        reply = await _page(client, before=token)
        assert reply.status_code == 400 and reply.json()["error"] == "invalid_cursor", (token, reply.text)


@pytest.mark.parametrize("stamp", ["9999-12-31T23:59:59-01:00", "0001-01-01T00:00:00+01:00"])
async def test_hole_a_cursor_timestamp_that_overflows_utc_is_a_500_not_invalid_cursor(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream, stamp: str
) -> None:
    """decode_credit_status_cursor converts with astimezone(UTC), which raises OverflowError (not ValueError) for
    a valid ISO time whose UTC value falls outside year 1..9999. The except clause misses it: 500."""
    await _credit_n(client, upstream, 1)
    token = _raw_cursor(stamp, uuid.uuid4(), shop.merchant_id, _public_key(PAYER_ADDRESS))
    try:
        reply = await _page(client, before=token)
    except OverflowError as exc:  # ASGITransport re-raises the unhandled app exception
        pytest.fail(f"unhandled OverflowError for cursor time {stamp}: {exc}")
    assert reply.status_code == 400 and reply.json()["error"] == "invalid_cursor", reply.text


# ================================================================ pagination: nonce handling


async def test_held_cursor_checks_never_run_for_an_unsigned_or_wrong_key_request(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream
) -> None:
    """Without the depositing key there is no cursor oracle: the signature is checked first, so a bad or a foreign
    cursor gives the same invalid_signature as no cursor, and the challenge is not burned."""
    await _credit_n(client, upstream, 1)
    challenge = await _challenge(client)
    wrong = signed(challenge, OTHER_KEY)
    for before in (None, "junk", _raw_cursor(datetime.now(UTC).isoformat(), uuid.uuid4(), uuid.uuid4(), "ab" * 32)):
        body: dict[str, object] = dict(wrong)
        if before is not None:
            body["before"] = before
        reply = await client.post(STATUS, json=body)
        assert reply.status_code == 400 and reply.json()["error"] == "invalid_signature", reply.text
    ok = await client.post(STATUS, json=signed(challenge))
    assert ok.status_code == 200


async def test_held_a_consumed_nonce_is_410_whatever_the_cursor(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream
) -> None:
    """Page 1's challenge cannot read page 2: reusing it gives 410 for a valid, a junk or no cursor."""
    await _credit_n(client, upstream, 3)
    challenge = await _challenge(client)
    first = await client.post(STATUS, json={**signed(challenge), "limit": 1})
    assert first.status_code == 200
    cursor = BuyerCreditStatus.model_validate(first.json()).next_cursor
    assert cursor is not None
    for extra in ({"before": cursor}, {"before": "junk"}, {}):
        again = await client.post(STATUS, json={**signed(challenge), "limit": 1, **extra})
        assert again.status_code == 410 and again.json()["error"] == "challenge_expired", again.text


async def test_held_an_invalid_cursor_then_a_valid_one_on_the_same_nonce_reads_exactly_once(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream
) -> None:
    """invalid_cursor does not burn the nonce; the next valid use does, and only once even when raced."""
    await _credit_n(client, upstream, 2)
    challenge = await _challenge(client)
    for _ in range(3):
        bad = await client.post(STATUS, json={**signed(challenge), "before": "junk"})
        assert bad.status_code == 400 and bad.json()["error"] == "invalid_cursor"
    replies = await asyncio.gather(*(client.post(STATUS, json=signed(challenge)) for _ in range(6)))
    codes = sorted(r.status_code for r in replies)
    assert codes.count(200) == 1, codes
    assert set(codes) <= {200, 409, 410}, codes


async def test_held_invalid_cursor_retries_on_one_nonce_are_bounded_by_the_rate_limit(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream, override_settings: SettingsOverride
) -> None:
    """A non-burning error is still a request: RL_PREPAID_REFUND_PER_MINUTE caps how often one IP can retry it,
    and a second IP has its own budget only."""
    await _credit_n(client, upstream, 1)
    challenge = await _challenge(client)
    override_settings(rl_prepaid_refund_per_minute=3)
    codes = [(await client.post(STATUS, json={**signed(challenge), "before": "junk"})).status_code for _ in range(5)]
    assert codes == [400, 400, 400, 429, 429], codes
    async with make_asgi_client(client, "203.0.113.9") as other_ip:
        ok = await other_ip.post(STATUS, json=signed(challenge))
        assert ok.status_code == 200, ok.text


# ================================================================ pagination: request validation


@pytest.mark.parametrize("limit", [0, -1, 101, 10**9, "10", 1.5, True, None])
async def test_held_limit_outside_1_to_100_or_not_a_strict_int_is_422_and_does_not_burn(
    client: httpx.AsyncClient, shop: Seed, limit: object
) -> None:
    challenge = await _challenge(client)
    reply = await client.post(STATUS, json={**signed(challenge), "limit": limit})
    assert reply.status_code == 422, reply.text
    assert (await client.post(STATUS, json=signed(challenge))).status_code == 200


@pytest.mark.parametrize(
    "extra",
    [{"cursor": "x"}, {"payer_address": OTHER_ADDRESS}, {"slug": "weather"}, {"merchant_id": str(uuid.uuid4())}],
)
async def test_held_extra_fields_are_422_and_do_not_burn(
    client: httpx.AsyncClient, shop: Seed, extra: dict[str, str]
) -> None:
    challenge = await _challenge(client)
    reply = await client.post(STATUS, json={**signed(challenge), **extra})
    assert reply.status_code == 422, reply.text
    assert (await client.post(STATUS, json=signed(challenge))).status_code == 200


async def test_held_a_non_string_cursor_is_422(client: httpx.AsyncClient, shop: Seed) -> None:
    challenge = await _challenge(client)
    for before in (1, ["a"], {"a": 1}, True):
        reply = await client.post(STATUS, json={**signed(challenge), "before": before})
        assert reply.status_code == 422, (before, reply.text)


# ================================================================ pagination: keyset correctness


async def test_held_equal_created_at_ties_are_ordered_by_id_with_no_skip_or_duplicate(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ids = await _credit_n(client, upstream, 7)
    tie = datetime(2026, 11, 26, 12, 0, 0, 123456, tzinfo=UTC)
    await _set_created_at(sessionmaker, ids, tie)
    for limit in (1, 2, 3, 7, 100):
        seen, counts = await _walk(client, limit)
        assert seen == sorted(ids, reverse=True), limit
        assert set(counts) == {7}


async def test_held_mixed_ties_and_distinct_times_walk_in_keyset_order(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ids = await _credit_n(client, upstream, 9)
    base = datetime(2026, 11, 26, 12, 0, 0, tzinfo=UTC)
    await _set_created_at(sessionmaker, ids[:3], base)
    await _set_created_at(sessionmaker, ids[3:6], base + timedelta(microseconds=1))
    await _set_created_at(sessionmaker, ids[6:], base - timedelta(microseconds=1))
    expected = [r.id for r in await _credit_rows(sessionmaker)]
    for limit in (1, 2, 4):
        seen, counts = await _walk(client, limit)
        assert seen == expected, limit
        assert set(counts) == {9}


async def test_held_a_credit_landing_mid_walk_neither_duplicates_nor_shifts_older_pages(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream
) -> None:
    """A new credit is newer than every cursor already issued: later pages are unchanged, credit_count grows."""
    ids = await _credit_n(client, upstream, 5)
    first = BuyerCreditStatus.model_validate((await _page(client, limit=2)).json())
    assert first.credit_count == 5 and first.next_cursor is not None
    [late] = await _credit_n(client, upstream, 1, start=0x30)
    rest: list[uuid.UUID] = []
    before: str | None = first.next_cursor
    counts: list[int] = []
    while before is not None:
        page = BuyerCreditStatus.model_validate((await _page(client, before=before, limit=2)).json())
        rest.extend(c.credit_id for c in page.credits)
        counts.append(page.credit_count)
        before = page.next_cursor
    walked = [c.credit_id for c in first.credits] + rest
    assert late not in walked and len(walked) == len(set(walked)) == 5
    assert set(walked) == set(ids) and set(counts) == {6}


async def test_info_a_credit_stamped_behind_an_issued_cursor_is_skipped_but_counted(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """created_at comes from the replica clock (failed_delivery.py `created_at=datetime.now(UTC)`), not commit
    order. A credit committed after a cursor was issued but stamped earlier than that cursor's newer side (replica
    clock skew, or a slow transaction) is never seen by the rest of that walk, while credit_count includes it. A
    fresh walk from the top does see it."""
    await _credit_n(client, upstream, 4)
    first = BuyerCreditStatus.model_validate((await _page(client, limit=2)).json())
    assert first.next_cursor is not None
    newest, second = first.credits
    [late] = await _credit_n(client, upstream, 1, start=0x31)
    between = second.created_at + (newest.created_at - second.created_at) / 2
    await _set_created_at(sessionmaker, [late], between)
    rest: list[uuid.UUID] = []
    before: str | None = first.next_cursor
    last_count = 0
    while before is not None:
        page = BuyerCreditStatus.model_validate((await _page(client, before=before, limit=2)).json())
        rest.extend(c.credit_id for c in page.credits)
        last_count = page.credit_count
        before = page.next_cursor
    walked = [newest.credit_id, second.credit_id, *rest]
    assert late not in walked and len(walked) == 4 and last_count == 5
    fresh, _ = await _walk(client, 2)
    assert late in fresh and len(fresh) == 5


async def test_held_credit_count_and_pages_agree_on_a_quiet_balance(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream
) -> None:
    ids = await _credit_n(client, upstream, 12)
    for limit in (5, 12, 13):
        seen, counts = await _walk(client, limit)
        assert len(seen) == len(set(seen)) == 12 and set(counts) == {12}
        assert set(seen) == set(ids)


# ================================================================ credit_id


async def test_held_credit_id_matches_the_receipt_and_reveals_nothing_about_other_buyers(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """credit_id is the random (v4) ledger UUID, equal to the X-KasGate-Credit creditId; another buyer's
    credits at the same merchant never show up, and every id is unguessable from its neighbours."""
    mine = await _credit_n(client, upstream, 3)
    rows = {r.id: r for r in await _credit_rows(sessionmaker)}
    assert set(rows) == set(mine)
    page = BuyerCreditStatus.model_validate((await _page(client, limit=100)).json())
    assert [c.credit_id for c in page.credits] == list(reversed(mine))
    assert all(c.credit_id.version == 4 for c in page.credits)
    other = await _page(client, payer=OTHER_ADDRESS, key=OTHER_KEY, limit=100)
    assert other.status_code == 200
    other_page = BuyerCreditStatus.model_validate(other.json())
    assert other_page.credit_count == 0 and other_page.credits == [] and other_page.next_cursor is None
    assert all(str(i) not in other.text for i in mine)


# ================================================================ DoS


async def test_held_limit_100_with_many_challenges_is_bounded_per_ip(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream, override_settings: SettingsOverride
) -> None:
    """Each limit=100 read needs its own challenge (unpaid-proxy limit) and its own status call (refund limit)."""
    await _credit_n(client, upstream, 3)
    override_settings(rl_unpaid_proxy_per_minute=4, rl_prepaid_refund_per_minute=2)
    async with make_asgi_client(client, "192.0.2.50") as reader:  # a fresh IP: the paid calls above used ours
        challenges = [
            await reader.post("/v1/prepaid/credits/challenge", json={"slug": "cheap", "payer_address": PAYER_ADDRESS})
            for _ in range(6)
        ]
        assert [c.status_code for c in challenges] == [201, 201, 201, 201, 429, 429]
        issued = [PrepaidCreditStatusChallenge.model_validate(c.json()) for c in challenges[:4]]
        codes = [(await reader.post(STATUS, json={**signed(ch), "limit": 100})).status_code for ch in issued]
    assert codes == [200, 200, 429, 429], codes


# ================================================================ unsigned challenge discloses the credit balance


async def test_hole_an_unsigned_challenge_discloses_any_buyers_credit_balance(
    client: httpx.AsyncClient, shop: Seed, upstream: Upstream
) -> None:
    """POST /credits/challenge and POST /refunds/challenge are unauthenticated. They must not return
    spendable_sompi or refundable_sompi: those would let anyone who knows a buyer's address derive unspent
    call-only credit (spendable minus refundable, or spendable minus a default request amount_sompi). The
    signed status is the only source for those values."""
    await fund(client)
    await _credit_n(client, upstream, 1)
    async with make_asgi_client(client, "198.51.100.7") as stranger:
        credits_challenge = (
            await stranger.post("/v1/prepaid/credits/challenge", json={"slug": "cheap", "payer_address": PAYER_ADDRESS})
        ).json()
        refund = (
            await stranger.post("/v1/prepaid/refunds/challenge", json={"slug": "cheap", "payer_address": PAYER_ADDRESS})
        ).json()
    for body in (credits_challenge, refund):
        assert "spendable_sompi" not in body and "refundable_sompi" not in body, body
