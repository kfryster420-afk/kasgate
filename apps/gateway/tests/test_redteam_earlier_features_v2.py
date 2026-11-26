"""Second red-team pass over the earlier-features fixes (PR #6, fix/earlier-features @0dff846).

`test_hole_*` fails on 0dff846 and passes once that finding is fixed; every other test is a probe that held.
Findings and repros: /workspace/kg-wt/rt-earlier-features-v2-review.md.
"""

import asyncio
import ipaddress
import json
import time
from datetime import datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import security
from app.models import PrepaidBalance, PrepaidDeposit, Settlement
from app.proxy import DEAD_LETTER_KEY, lock_key, nonce_lock_key
from app.schemas import (
    HEADER_PREPAID_DEBIT,
    WEBHOOK_SIGNATURE_HEADER,
    WEBHOOK_TIMESTAMP_HEADER,
    PaymentSignature,
    X402ErrorBody,
    is_forbidden_ip,
    validate_webhook_url,
)
from app.security import UpstreamBlockedError, resolve_public_address
from app.webhooks import compute_signature, verify_signature
from tests.conftest import OTHER_ADDRESS, PAYER_ADDRESS, Seed, StubEngine, WebhookReceiver, bearer, seed_endpoint
from tests.test_prepaid import (
    CREDIT_SOMPI,
    DEPOSIT_SOMPI,
    add_endpoint,
    debit_header,
    fund,
    get_debit_challenge,
    post_deposit,
    quote_deposit,
    shop,  # noqa: F401
)
from tests.test_prepaid_balance_privacy import refunds_status_headers
from tests.test_refunds import open_refund, payout
from tests.test_webhooks import create_hook

# ---------------------------------------------------------------- helpers


async def _scalar(maker: async_sessionmaker[AsyncSession], statement: Any) -> Any:
    async with maker() as session:
        return (await session.execute(statement)).scalar_one_or_none()


async def _count(maker: async_sessionmaker[AsyncSession], model: Any) -> int:
    return int(await _scalar(maker, select(func.count()).select_from(model)))


async def _balance(maker: async_sessionmaker[AsyncSession]) -> int | None:
    value = await _scalar(maker, select(PrepaidBalance.balance_sompi))
    return None if value is None else int(value)


async def _letters(redis: Any) -> list[dict[str, Any]]:
    return [json.loads(x) for x in await redis.lrange(DEAD_LETTER_KEY, 0, -1)]


# ---------------------------------------------------------------- M1 under real concurrency


async def test_twelve_concurrent_deposits_credit_exactly_up_to_the_cap(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    stub_engine: StubEngine,
    redis: Any,
    override_settings: Any,
) -> None:
    """12 quotes taken while the balance is 0, posted at once: room for 3 credits, so 3 settle and 9 are refused
    before submit, with nothing double-counted, no lock leaked and nothing dead-lettered."""
    override_settings(prepaid_max_balance_sompi=3 * CREDIT_SOMPI + 10_000_000)
    quotes = [await quote_deposit(client) for _ in range(12)]
    stub_engine.delay = 0.05
    tx_ids = [f"{i:02x}" * 32 for i in range(12)]

    responses = await asyncio.gather(*(post_deposit(client, q, tx_id=t) for q, t in zip(quotes, tx_ids, strict=True)))

    codes = [r.status_code for r in responses]
    assert sorted(codes) == [201] * 3 + [422] * 9, [r.text for r in responses]
    assert all(r.json()["error"] == "prepaid_cap_exceeded" for r in responses if r.status_code == 422)
    winners = {t for t, r in zip(tx_ids, responses, strict=True) if r.status_code == 201}
    assert set(stub_engine.calls) == winners and len(stub_engine.calls) == 3  # refused ones never reached the node
    assert await _count(sessionmaker, Settlement) == 3
    assert await _count(sessionmaker, PrepaidDeposit) == 3
    async with sessionmaker() as session:
        row = (await session.execute(select(PrepaidBalance))).scalar_one()
        credited = set((await session.execute(select(PrepaidDeposit.tx_id))).scalars())
    assert (row.balance_sompi, row.deposited_sompi) == (3 * CREDIT_SOMPI, 3 * CREDIT_SOMPI)
    assert credited == winners
    for tx_id, quote, response in zip(tx_ids, quotes, responses, strict=True):
        if response.status_code == 422:  # refused: both locks released so the buyer can reuse the coins
            assert await redis.get(lock_key(tx_id)) is None
            assert await redis.get(nonce_lock_key(quote.required.nonce)) is None
    assert await _letters(redis) == []


async def test_concurrent_deposits_across_the_backstop_dead_letter_once_and_never_credit(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    stub_engine: StubEngine,
    redis: Any,
    override_settings: Any,
) -> None:
    """12 concurrent deposits; the cap drops while the third is being submitted. Every deposit that reached the node
    after that is dead-lettered exactly once (never credited, never lost) and a replay cannot credit it later.

    After I1 the cap is re-read under the row lock, so waiters that have not yet submitted see the new cap and
    get 422 instead of a DLQ 503. The third deposit, already past the pre-check when the cap drops mid-settle,
    is still dead-lettered exactly once."""
    override_settings(prepaid_max_balance_sompi=5 * CREDIT_SOMPI)
    quotes = [await quote_deposit(client) for _ in range(12)]
    tx_ids = [f"{i + 0x40:02x}" * 32 for i in range(12)]
    submitted: list[str] = []

    async def drop_cap_on_third(signature: PaymentSignature) -> None:
        submitted.append(signature.txId)
        if len(submitted) == 3:
            override_settings(prepaid_max_balance_sompi=2 * CREDIT_SOMPI + 20_000_000)
        await asyncio.sleep(0.02)

    stub_engine.before_submit = drop_cap_on_third
    responses = await asyncio.gather(*(post_deposit(client, q, tx_id=t) for q, t in zip(quotes, tx_ids, strict=True)))

    by_code: dict[int, list[str]] = {}
    for tx_id, response in zip(tx_ids, responses, strict=True):
        by_code.setdefault(response.status_code, []).append(tx_id)
    assert set(by_code) <= {201, 422, 503}, [r.text for r in responses]
    assert len(by_code[201]) == 2 and submitted[2] in by_code[503]
    assert set(submitted) == set(by_code[201]) | set(by_code[503])  # 422s never reached the node
    letters = await _letters(redis)
    assert sorted(x["prepaidDeposit"]["txId"] for x in letters) == sorted(by_code[503])  # one letter per 503
    assert {(x["reason"], x["creditSompi"]) for x in letters} == {("prepaid_cap_exceeded", CREDIT_SOMPI)}
    async with sessionmaker() as session:
        credited = set((await session.execute(select(PrepaidDeposit.tx_id))).scalars())
        settled = set((await session.execute(select(Settlement.tx_id))).scalars())
    assert credited == settled == set(by_code[201])  # dead-lettered txs have no settlement and no credit
    assert await _balance(sessionmaker) == 2 * CREDIT_SOMPI

    for lettered in by_code[503]:
        index = tx_ids.index(lettered)
        replay = await post_deposit(client, quotes[index], tx_id=lettered)
        assert replay.status_code == 409, replay.text  # replay locks are kept: no second submit, no late credit
    assert await _balance(sessionmaker) == 2 * CREDIT_SOMPI
    assert len(await _letters(redis)) == len(by_code[503])


async def test_open_refund_hold_counts_toward_the_cap_and_a_paid_refund_frees_room(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    override_settings: Any,
) -> None:
    override_settings(prepaid_max_balance_sompi=60_000_000)
    await fund(client)
    refund = await open_refund(client)  # the whole 40_000_000 is held, still in balance_sompi
    over = await client.post(
        "/v1/prepaid/deposits/quote",
        json={"slug": "cheap", "payer_address": PAYER_ADDRESS, "amount_sompi": DEPOSIT_SOMPI},
    )
    assert over.status_code == 422, over.text
    assert (await payout(client, shop, refund.id, tx_id="ab" * 32)).status_code == 200
    assert await _balance(sessionmaker) == 0
    await fund(client)
    assert await _balance(sessionmaker) == CREDIT_SOMPI


async def test_hole_a_5xx_debit_refund_lands_past_the_prepaid_cap(
    app: FastAPI,
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],
    override_settings: Any,
) -> None:
    """Option (a): in-flight debits (status=debited, upstream_status_code IS NULL) count toward the cap under the
    same FOR UPDATE lock. A deposit that would pass only by ignoring a hanging debit is 422; the later 5xx refund
    cannot land the balance past the cap."""
    cap = 60_000_000
    await add_endpoint(sessionmaker, shop.merchant_id, "half", price_sompi=20_000_000)
    override_settings(prepaid_max_balance_sompi=100_000_000)
    await fund(client)  # balance 40_000_000
    stale_quote = await quote_deposit(client)  # fits while nothing is in flight: 40M + 40M <= 100M
    override_settings(prepaid_max_balance_sompi=cap)
    release = asyncio.Event()
    reached = asyncio.Event()

    async def slow_failure(request: httpx.Request) -> httpx.Response:
        reached.set()
        await release.wait()
        return httpx.Response(503, json={"down": True})

    original = app.state.http_client
    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(slow_failure), follow_redirects=False)
    try:
        challenge = await get_debit_challenge(client, slug="half")
        call = asyncio.create_task(client.get("/p/half", headers={HEADER_PREPAID_DEBIT: debit_header(challenge)}))
        await asyncio.wait_for(reached.wait(), 5)  # debited: balance 20_000_000 while the upstream hangs
        deposited = await post_deposit(client, stale_quote, tx_id="5e" * 32)
        # 20M balance + 20M in-flight + 40M credit > 60M cap
        assert deposited.status_code == 422, deposited.text
        assert deposited.json()["error"] == "prepaid_cap_exceeded"
        # A new quote now gives the same answer (the shared helper counts the in-flight debit), without figures.
        requote = await client.post(
            "/v1/prepaid/deposits/quote",
            json={"slug": "cheap", "payer_address": PAYER_ADDRESS, "amount_sompi": DEPOSIT_SOMPI},
        )
        assert requote.status_code == 422 and requote.json() == deposited.json(), requote.text
        release.set()
        failed = await call
        assert failed.status_code == 503
    finally:
        await app.state.http_client.aclose()
        app.state.http_client = original

    balance = await _balance(sessionmaker)
    assert balance == CREDIT_SOMPI, f"balance {balance} after the 5xx refund"
    assert balance <= cap


# ---------------------------------------------------------------- L1 SSRF


SSRF_REFUSED = [
    "::127.0.0.1",  # IPv4-compatible (the L1 fix)
    "::169.254.169.254",
    "fec0::1",  # site-local (the L1 fix)
    "::ffff:127.0.0.1",  # v4-mapped
    "64:ff9b::7f00:1",  # NAT64 well-known
    "64:ff9b::a9fe:a9fe",
    "64:ff9b:1::a9fe:a9fe",  # NAT64 local-use (RFC 8215)
    "2002:7f00:1::",  # 6to4 of 127.0.0.1
    "2002:a9fe:a9fe::",
    "2001:0:4136:e378:8000:63bf:80ff:fffe",  # Teredo
    "100.64.0.1",
    "0.0.0.0",  # noqa: S104
]


@pytest.mark.parametrize("address", SSRF_REFUSED)
def test_tunnelled_and_special_addresses_are_refused(address: str) -> None:
    assert is_forbidden_ip(ipaddress.ip_address(address))


@pytest.mark.parametrize(
    "url",
    [
        "https://2130706433/",  # decimal
        "https://0x7f000001/",  # hex
        "https://0177.0.0.1/",  # octal
        "https://017700000001/",
        "https://0x7f.1/",
        "https://127.1/",
        "https://%31%32%37.0.0.1/",
        "https://[::ffff:7f00:1]/",
        "https://[::127.0.0.1]/",
        "https://[fec0::1]/",
        "https://[64:ff9b::a9fe:a9fe]/",
    ],
)
def test_encoded_ip_literals_are_refused_at_registration(url: str) -> None:
    with pytest.raises(ValueError):
        validate_webhook_url(url)


async def test_dns_answers_are_checked_at_connect_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """A public-looking name is accepted at registration; the connect-time guard refuses it when ANY answer is
    private, and connects to the validated IP so a second (rebinding) lookup is never made."""
    assert validate_webhook_url("https://127.0.0.1.nip.io/") == "https://127.0.0.1.nip.io/"
    answers = [[("8.8.8.8", 443)], [("127.0.0.1", 443)]]
    calls: list[str] = []

    async def fake_lookup(host: str, port: int) -> list[tuple[Any, Any, Any, Any, tuple[Any, ...]]]:
        calls.append(host)
        return [(None, None, None, None, addr) for addr in answers[min(len(calls) - 1, 1)]]

    monkeypatch.setattr(security, "_getaddrinfo", fake_lookup)
    assert await resolve_public_address("rebind.example", 443) == "8.8.8.8"
    with pytest.raises(UpstreamBlockedError):
        await resolve_public_address("rebind.example", 443)
    mixed = [("8.8.8.8", 443), ("::ffff:10.0.0.1", 443)]

    async def mixed_lookup(host: str, port: int) -> list[tuple[Any, Any, Any, Any, tuple[Any, ...]]]:
        return [(None, None, None, None, addr) for addr in mixed]

    monkeypatch.setattr(security, "_getaddrinfo", mixed_lookup)
    with pytest.raises(UpstreamBlockedError):
        await resolve_public_address("mixed.example", 443)


async def test_hole_siit_ipv4_translated_prefix_passes_the_ssrf_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """`::ffff:0:a.b.c.d` (IPv4-translated, ::ffff:0:0/96, RFC 2765/6145) embeds an IPv4 address like `::/96` and
    `::ffff:0/96` do, but `is_forbidden_ip` neither refuses the block nor unwraps it, and Python reports it as
    global. Same class and impact as L1: defence in depth on hosts with SIIT/stateless NAT64 routing."""

    async def lookup(host: str, port: int) -> list[tuple[Any, Any, Any, Any, tuple[Any, ...]]]:
        return [(None, None, None, None, ("::ffff:0:a9fe:a9fe", 443, 0, 0))]

    monkeypatch.setattr(security, "_getaddrinfo", lookup)
    refused_url = True
    try:
        validate_webhook_url("https://[::ffff:0:a9fe:a9fe]/latest/meta-data/")
        refused_url = False
    except ValueError:
        pass
    try:
        await resolve_public_address("siit.example", 443)
        refused_dns = False
    except UpstreamBlockedError:
        refused_dns = True
    assert is_forbidden_ip(ipaddress.ip_address("::ffff:0:7f00:1"))
    assert refused_url and refused_dns


# ---------------------------------------------------------------- L2 test-fire limiter


async def test_test_fire_budget_is_the_callers_not_the_webhook_owners(
    client: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    webhook_receiver: WebhookReceiver,
    override_settings: Any,
) -> None:
    """Another merchant firing at your webhook id gets 404 and spends only its own budget."""
    override_settings(rl_webhook_test_per_minute=2)
    owner = await seed_endpoint(sessionmaker, slug="owner")
    other = await seed_endpoint(sessionmaker, slug="other", address=OTHER_ADDRESS)
    hook = await create_hook(client, owner, ["trial.used"])
    for _ in range(3):
        probe = await client.post(f"/v1/webhooks/{hook.webhook.id}/test", headers=bearer(other.api_key))
        assert probe.status_code in (404, 429)
    assert probe.status_code == 429 and X402ErrorBody.model_validate(probe.json()).error == "rate_limited"
    mine = await client.post(f"/v1/webhooks/{hook.webhook.id}/test", headers=bearer(owner.api_key))
    assert mine.status_code == 200, mine.text
    assert len(webhook_receiver.requests) == 1


# ---------------------------------------------------------------- L3 rotation overlap end to end


async def test_previous_secret_stops_signing_live_deliveries_after_the_overlap(
    client: httpx.AsyncClient,
    seeded: Seed,
    webhook_receiver: WebhookReceiver,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = await create_hook(client, seeded, ["trial.used"])
    rotate_url = f"/v1/webhooks/{created.webhook.id}/rotate-secret"
    rotated = (await client.post(rotate_url, headers=bearer(seeded.api_key))).json()
    until = int(datetime.fromisoformat(rotated["previous_secret_expires_at"]).timestamp())
    monkeypatch.setattr(time, "time", lambda: float(until - 1))
    await client.post(f"/v1/webhooks/{created.webhook.id}/test", headers=bearer(seeded.api_key))
    inside = webhook_receiver.requests[-1]
    stamp = int(inside.headers[WEBHOOK_TIMESTAMP_HEADER])
    assert stamp == until - 1
    header = inside.headers[WEBHOOK_SIGNATURE_HEADER]
    assert header.count("v1=") == 2
    assert verify_signature(created.secret, header, inside.content, now=stamp)
    assert verify_signature(rotated["secret"], header, inside.content, now=stamp)

    monkeypatch.setattr(time, "time", lambda: float(until))
    await client.post(f"/v1/webhooks/{created.webhook.id}/test", headers=bearer(seeded.api_key))
    after = webhook_receiver.requests[-1]
    header = after.headers[WEBHOOK_SIGNATURE_HEADER]
    assert header.count("v1=") == 1
    assert not verify_signature(created.secret, header, after.content, now=until)
    assert verify_signature(rotated["secret"], header, after.content, now=until)


# ---------------------------------------------------------------- L4 Unicode digits


JUNK_HEADERS = [
    "t=²,v1=00",
    "t=١٧٠٠٠٠٠٠٠٠,v1=00",  # Arabic-Indic digits
    "t=１７００００００００,v1=00",  # fullwidth digits
    "t=1700000000,v1=é" + "0" * 63,
    "t=1700000000,v1=ß",
    "t=" + "9" * 400 + ",v1=00",
    "t=1700000000",
    "v1=00",
    "",
    ",,,=",
]


@pytest.mark.parametrize("header", JUNK_HEADERS)
def test_gateway_verifier_returns_false_on_junk_headers(header: str) -> None:
    assert verify_signature("whsec_x", header, b"{}", now=1_700_000_000) is False


@pytest.mark.parametrize("header", JUNK_HEADERS)
def test_sdk_verifier_returns_false_on_junk_headers(header: str) -> None:
    trials = pytest.importorskip("kasgate_client.trials")
    assert trials.verify_webhook_signature("whsec_x", header, b"{}", now=1_700_000_000) is False


def test_a_valid_signature_next_to_unicode_junk_still_verifies() -> None:
    good = compute_signature("whsec_x", 1_700_000_000, b"{}")
    assert verify_signature("whsec_x", f"t=1700000000,v1=é,v1={good}", b"{}", now=1_700_000_000)


# ---------------------------------------------------------------- L7 public reads


async def test_unsigned_balance_returns_only_public_fields(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
) -> None:
    await fund(client)
    body = (await client.get("/v1/prepaid/balance", params={"slug": "cheap", "payer_address": PAYER_ADDRESS})).json()
    hidden = (
        "merchant_id",
        "payer_address",
        "balance_sompi",
        "deposited_sompi",
        "debited_sompi",
        "updated_at",
        "refunded_sompi",
        "expired_sompi",
        "refund_hold_sompi",
    )
    assert all(body[name] is None for name in hidden)
    assert body["spendable_sompi"] == str(CREDIT_SOMPI)
    assert "expires_at" in body


async def test_hole_unsigned_refund_status_still_reveals_the_hidden_refund_totals(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
) -> None:
    """GET /v1/prepaid/refunds/status is buyer-signed. Unsigned or invalid requests are 401 and do not list
    amounts; a valid signature returns the public refund rows (reasons still withheld)."""
    await fund(client)
    await open_refund(client, 20_000_000)
    params = {"slug": "cheap", "payer_address": PAYER_ADDRESS}
    balance = (await client.get("/v1/prepaid/balance", params=params)).json()
    assert balance["refund_hold_sompi"] is None
    assert balance["balance_sompi"] is None
    assert balance["spendable_sompi"] == str(CREDIT_SOMPI - 20_000_000)
    unsigned = await client.get("/v1/prepaid/refunds/status", params=params)
    assert unsigned.status_code == 401, unsigned.text
    assert unsigned.json()["error"] == "invalid_balance_signature"
    assert "20000000" not in unsigned.text
    refunds = (await client.get("/v1/prepaid/refunds/status", params=params, headers=refunds_status_headers())).json()
    open_amounts = [r["amount_sompi"] for r in refunds if r["status"] in ("requested", "paying", "payout_submitted")]
    assert open_amounts == ["20000000"]
    assert refunds[0]["reason"] is None


# ---------------------------------------------------------------- 429 shape


async def test_proxy_and_v1_limiter_429s_are_x402_with_retry_after(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    override_settings: Any,
) -> None:
    override_settings(rl_unpaid_proxy_per_minute=1)
    first = await client.get("/p/weather")
    assert first.status_code == 402
    for response in (
        await client.get("/p/weather"),
        await client.get("/v1/prepaid/balance", params={"slug": "cheap", "payer_address": PAYER_ADDRESS}),
        await client.post(
            "/v1/prepaid/deposits/quote",
            json={"slug": "cheap", "payer_address": PAYER_ADDRESS, "amount_sompi": DEPOSIT_SOMPI},
        ),
    ):
        assert response.status_code == 429, response.text
        assert X402ErrorBody.model_validate(response.json()).error == "rate_limited"
        assert "detail" not in response.json()
        assert 0 < int(response.headers["Retry-After"]) <= 60
