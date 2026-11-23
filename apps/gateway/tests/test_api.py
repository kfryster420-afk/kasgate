"""Merchant REST API: auth, endpoint CRUD, settlements, analytics, tax CSV, directory, quote."""

import csv
import io
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.models import Endpoint, PriceCurrency, Settlement, SettlementStatus
from app.proxy import nonce_key
from app.schemas import (
    HEADER_PAYMENT_SIGNATURE,
    TAX_CSV_COLUMNS,
    DirectoryManifest,
    EndpointRead,
    MerchantAnalytics,
    PaymentRequired,
    SettlementRead,
    compute_fee_split,
    min_price_sompi,
    usd_micros_to_sompi,
)
from app.security import decrypt_headers
from tests.conftest import (
    MERCHANT_ADDRESS,
    OTHER_ADDRESS,
    OTHER_KEY,
    PAYER_ADDRESS,
    PRICE_SOMPI,
    TREASURY,
    Seed,
    StubEngine,
    Upstream,
    fetch_challenge,
    get_challenge,
    make_signature_header,
    proof_fields,
    register,
    seed_endpoint,
)

NEW_ADDRESS = OTHER_ADDRESS
SPLIT = compute_fee_split(PRICE_SOMPI)


def bearer(seed: Seed) -> dict[str, str]:
    return {"Authorization": f"Bearer {seed.api_key}"}


def endpoint_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "Price Feed",
        "upstream_url": "https://feeds.example.com/prices",
        "upstream_headers": {"X-Api-Key": "very-secret"},
        "price_sompi": PRICE_SOMPI,
        "allowed_methods": ["GET"],
    }
    payload.update(overrides)
    if payload.get("price_currency") == "USD":
        payload.pop("price_sompi", None)
    return payload


async def add_settlement(
    maker: async_sessionmaker[AsyncSession], seed: Seed, *, tx: str, at: datetime, usd: str = "2.5"
) -> None:
    async with maker() as session:
        session.add(
            Settlement(
                tx_id=tx,
                endpoint_id=seed.endpoint_id,
                merchant_id=seed.merchant_id,
                network="kaspa:testnet-10",
                payer_address=PAYER_ADDRESS,
                merchant_address=MERCHANT_ADDRESS,
                treasury_address=TREASURY,
                amount_sompi=PRICE_SOMPI,
                merchant_amount_sompi=SPLIT.merchant_sompi,
                fee_amount_sompi=SPLIT.fee_sompi,
                kas_usd_rate=Decimal("0.125"),
                usd_value_at_settlement=Decimal(usd),
                status=SettlementStatus.SETTLED,
                request_method="GET",
                request_path="/p/weather",
                upstream_status_code=200,
                payment_nonce=uuid.uuid4().hex,
                created_at=at,
                settled_at=at,
            )
        )
        await session.commit()


# ---------------------------------------------------------------- auth & merchants


async def test_create_merchant_returns_key_once(client: httpx.AsyncClient) -> None:
    created = await register(client, OTHER_KEY, email="Owner@Example.com")
    assert created.api_key.startswith("kg_")
    assert created.session_token.startswith("kgs_")
    assert created.merchant.email == "owner@example.com"
    assert "api_key_hash" not in created.model_dump_json()

    me = await client.get("/v1/merchants/me", headers={"Authorization": f"Bearer {created.api_key}"})
    assert me.status_code == 200
    assert me.json()["id"] == str(created.merchant.id)
    assert "api_key" not in me.json()


async def test_create_merchant_without_wallet_proof_is_422(client: httpx.AsyncClient) -> None:
    body = {"kaspa_address": NEW_ADDRESS, "display_name": "Shop", "email": "Owner@Example.com"}
    assert (await client.post("/v1/merchants", json=body)).status_code == 422


async def test_duplicate_merchant_address_is_409(client: httpx.AsyncClient) -> None:
    await register(client, OTHER_KEY)
    challenge = await get_challenge(client, NEW_ADDRESS, "register")
    body = {"kaspa_address": NEW_ADDRESS, "display_name": "Shop", **proof_fields(challenge.challenge, OTHER_KEY)}
    assert (await client.post("/v1/merchants", json=body)).status_code == 409


async def test_invalid_merchant_payload_is_422(client: httpx.AsyncClient) -> None:
    assert (await client.post("/v1/merchants", json={"kaspa_address": "nope", "display_name": "x"})).status_code == 422


async def test_protected_routes_require_a_valid_bearer_key(client: httpx.AsyncClient, seeded: Seed) -> None:
    protected = [
        ("GET", "/v1/merchants/me"),
        ("GET", "/v1/endpoints"),
        ("GET", f"/v1/endpoints/{seeded.endpoint_id}"),
        ("GET", "/v1/settlements"),
        ("GET", "/v1/analytics"),
        ("GET", "/v1/tax-report.csv"),
    ]
    for method, path in protected:
        missing = await client.request(method, path)
        assert missing.status_code == 401, path
        assert missing.headers["www-authenticate"] == "Bearer"
        wrong = await client.request(method, path, headers={"Authorization": "Bearer kg_" + "x" * 43})
        assert wrong.status_code == 401, path
        malformed = await client.request(method, path, headers={"Authorization": "Bearer not-a-key"})
        assert malformed.status_code == 401, path
        basic = await client.request(method, path, headers={"Authorization": "Basic abc"})
        assert basic.status_code == 401, path
    assert (await client.post("/v1/endpoints", json=endpoint_payload())).status_code == 401
    assert (await client.patch(f"/v1/endpoints/{seeded.endpoint_id}", json={"name": "x"})).status_code == 401
    assert (await client.delete(f"/v1/endpoints/{seeded.endpoint_id}")).status_code == 401
    assert (await client.get("/v1/merchants/me", headers=bearer(seeded))).status_code == 200


async def test_disabled_merchant_is_403(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    from sqlalchemy import update

    from app.models import Merchant

    async with sessionmaker() as session:
        await session.execute(update(Merchant).where(Merchant.id == seeded.merchant_id).values(is_active=False))
        await session.commit()
    assert (await client.get("/v1/merchants/me", headers=bearer(seeded))).status_code == 403


# ---------------------------------------------------------------- endpoints


async def test_endpoint_crud_flow(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    created = await client.post("/v1/endpoints", json=endpoint_payload(), headers=bearer(seeded))
    assert created.status_code == 201
    endpoint = EndpointRead.model_validate(created.json())
    assert endpoint.slug.startswith("price-feed-")
    assert len(endpoint.slug) == len("price-feed-") + 6
    assert endpoint.proxy_url == f"http://gateway.test/p/{endpoint.slug}"
    assert endpoint.price_kas == "20"
    assert endpoint.is_active is True
    assert "very-secret" not in created.text
    assert "upstream_headers" not in created.json()

    async with sessionmaker() as session:
        stored = (await session.execute(select(Endpoint).where(Endpoint.id == endpoint.id))).scalar_one()
        assert stored.upstream_headers_encrypted is not None
        assert b"very-secret" not in stored.upstream_headers_encrypted
        assert decrypt_headers(stored.upstream_headers_encrypted) == {"X-Api-Key": "very-secret"}

    listing = await client.get("/v1/endpoints", headers=bearer(seeded))
    assert {item["id"] for item in listing.json()} == {str(seeded.endpoint_id), str(endpoint.id)}

    one = await client.get(f"/v1/endpoints/{endpoint.id}", headers=bearer(seeded))
    assert one.json()["slug"] == endpoint.slug

    patched = await client.patch(
        f"/v1/endpoints/{endpoint.id}",
        json={
            "name": "Prices v2",
            "price_sompi": 3_000_000_000,
            "allowed_methods": ["POST", "GET"],
            "upstream_headers": {"X-Api-Key": "rotated"},
            "upstream_url": "https://feeds.example.com/v2",
            "description": "now with docs",
            "is_public_listed": False,
        },
        headers=bearer(seeded),
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Prices v2"
    assert patched.json()["price_sompi"] == 3_000_000_000
    assert patched.json()["allowed_methods"] == ["GET", "POST"] or patched.json()["allowed_methods"] == ["POST", "GET"]
    assert patched.json()["upstream_url"] == "https://feeds.example.com/v2"
    assert patched.json()["is_public_listed"] is False
    async with sessionmaker() as session:
        stored = (await session.execute(select(Endpoint).where(Endpoint.id == endpoint.id))).scalar_one()
        assert stored.upstream_headers_encrypted is not None
        assert decrypt_headers(stored.upstream_headers_encrypted) == {"X-Api-Key": "rotated"}

    cleared = await client.patch(
        f"/v1/endpoints/{endpoint.id}", json={"upstream_headers": {}, "description": None}, headers=bearer(seeded)
    )
    assert cleared.status_code == 200
    assert cleared.json()["description"] is None

    deleted = await client.delete(f"/v1/endpoints/{endpoint.id}", headers=bearer(seeded))
    assert deleted.status_code == 204
    after = await client.get(f"/v1/endpoints/{endpoint.id}", headers=bearer(seeded))
    assert after.status_code == 200
    assert after.json()["is_active"] is False
    assert (await client.get(f"/p/{endpoint.slug}")).status_code == 404


async def test_explicit_slug_and_conflicts(client: httpx.AsyncClient, seeded: Seed) -> None:
    ok = await client.post("/v1/endpoints", json=endpoint_payload(slug="my-feed"), headers=bearer(seeded))
    assert ok.status_code == 201
    assert ok.json()["slug"] == "my-feed"
    dup = await client.post("/v1/endpoints", json=endpoint_payload(slug="my-feed"), headers=bearer(seeded))
    assert dup.status_code == 409


async def test_endpoint_validation(client: httpx.AsyncClient, seeded: Seed) -> None:
    too_cheap = endpoint_payload(price_sompi=min_price_sompi() - 1)
    assert (await client.post("/v1/endpoints", json=too_cheap, headers=bearer(seeded))).status_code == 422
    at_floor = endpoint_payload(price_sompi=min_price_sompi())
    assert (await client.post("/v1/endpoints", json=at_floor, headers=bearer(seeded))).status_code == 201
    private = endpoint_payload(upstream_url="http://127.0.0.1/admin")
    assert (await client.post("/v1/endpoints", json=private, headers=bearer(seeded))).status_code == 422
    managed = endpoint_payload(upstream_headers={"Host": "evil"})
    assert (await client.post("/v1/endpoints", json=managed, headers=bearer(seeded))).status_code == 422
    nulled = await client.patch(
        f"/v1/endpoints/{seeded.endpoint_id}", json={"price_sompi": None}, headers=bearer(seeded)
    )
    assert nulled.status_code == 422


async def test_minimum_price_follows_the_fee_floor() -> None:
    floor = get_settings().min_output_sompi
    assert (
        min_price_sompi() == 3 * floor
    )  # v2.3: fee floor + a merchant output of 2 * floor, so change fits the mass cap
    at_minimum = compute_fee_split(min_price_sompi())
    assert (at_minimum.fee_sompi, at_minimum.merchant_sompi) == (floor, 2 * floor)
    with pytest.raises(ValueError):
        compute_fee_split(min_price_sompi() - 1)
    # the fee is the flat floor until 1.5 % overtakes it, then 1.5 % rounded up
    assert compute_fee_split(PRICE_SOMPI).fee_sompi == -(-PRICE_SOMPI * 150 // 10_000) > floor
    knee = floor * 10_000 // 150
    assert compute_fee_split(knee).fee_sompi == floor
    assert compute_fee_split(knee + 1).fee_sompi == floor + 1


async def test_endpoints_are_scoped_to_their_merchant(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    other = await seed_endpoint(sessionmaker, slug="other", address=NEW_ADDRESS)
    assert (await client.get(f"/v1/endpoints/{other.endpoint_id}", headers=bearer(seeded))).status_code == 404
    assert (
        await client.patch(f"/v1/endpoints/{other.endpoint_id}", json={"name": "x"}, headers=bearer(seeded))
    ).status_code == 404
    assert (await client.delete(f"/v1/endpoints/{other.endpoint_id}", headers=bearer(seeded))).status_code == 404
    assert (await client.get(f"/v1/endpoints/{uuid.uuid4()}", headers=bearer(seeded))).status_code == 404
    mine = await client.get("/v1/endpoints", headers=bearer(seeded))
    assert [e["slug"] for e in mine.json()] == ["weather"]


# ---------------------------------------------------------------- settlements, analytics, tax


async def test_settlements_pagination_and_scoping(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    now = datetime.now(UTC)
    for i in range(3):
        await add_settlement(sessionmaker, seeded, tx=f"{i + 1:064x}", at=now - timedelta(minutes=i))
    other = await seed_endpoint(sessionmaker, slug="other", address=NEW_ADDRESS)
    await add_settlement(sessionmaker, other, tx=f"{99:064x}", at=now)

    page = await client.get("/v1/settlements?limit=2&offset=0", headers=bearer(seeded))
    rows = [SettlementRead.model_validate(r) for r in page.json()]
    assert [r.tx_id for r in rows] == [f"{1:064x}", f"{2:064x}"]
    tail = await client.get("/v1/settlements?limit=2&offset=2", headers=bearer(seeded))
    assert [r["tx_id"] for r in tail.json()] == [f"{3:064x}"]
    assert (await client.get("/v1/settlements?limit=0", headers=bearer(seeded))).status_code == 422
    assert (await client.get("/v1/settlements?limit=1000", headers=bearer(seeded))).status_code == 422


async def test_analytics_totals_daily_and_by_endpoint(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # Noon yesterday: always in the past (noon today is still in the future before 12:00 UTC).
    now = (datetime.now(UTC) - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    await add_settlement(sessionmaker, seeded, tx=f"{1:064x}", at=now)
    await add_settlement(sessionmaker, seeded, tx=f"{2:064x}", at=now - timedelta(days=1))
    await add_settlement(sessionmaker, seeded, tx=f"{3:064x}", at=now - timedelta(days=90))

    response = await client.get("/v1/analytics", headers=bearer(seeded))
    assert response.status_code == 200
    data = MerchantAnalytics.model_validate(response.json())
    assert data.total_calls == 2
    assert data.gross_sompi == 2 * PRICE_SOMPI
    assert data.merchant_sompi == 2 * SPLIT.merchant_sompi
    assert data.fee_sompi == 2 * SPLIT.fee_sompi
    assert data.usd_value == Decimal("5")
    assert [d.call_count for d in data.daily] == [1, 1]
    assert data.daily[0].day < data.daily[1].day
    assert len(data.by_endpoint) == 1
    assert data.by_endpoint[0].slug == "weather"
    assert data.by_endpoint[0].call_count == 2

    start = (now - timedelta(days=120)).isoformat()
    wide = await client.get(
        "/v1/analytics", params={"start": start, "end": (now + timedelta(days=1)).isoformat()}, headers=bearer(seeded)
    )
    assert wide.json()["total_calls"] == 3
    bad = await client.get("/v1/analytics", params={"start": now.isoformat(), "end": start}, headers=bearer(seeded))
    assert bad.status_code == 422


async def test_tax_report_streams_csv(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    now = datetime.now(UTC) - timedelta(hours=1)
    await add_settlement(sessionmaker, seeded, tx=f"{1:064x}", at=now - timedelta(minutes=5))
    await add_settlement(sessionmaker, seeded, tx=f"{2:064x}", at=now)
    response = await client.get("/v1/tax-report.csv", headers=bearer(seeded))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(response.text)))
    assert tuple(rows[0]) == TAX_CSV_COLUMNS
    assert len(rows) == 3
    record = dict(zip(TAX_CSV_COLUMNS, rows[1], strict=True))
    assert record["tx_id"] == f"{1:064x}"
    assert record["network"] == "kaspa:testnet-10"
    assert record["endpoint_slug"] == "weather"
    assert record["payer_address"] == PAYER_ADDRESS
    assert record["gross_kas"] == "20"
    assert record["merchant_kas"] == "19.7"
    assert record["platform_fee_kas"] == "0.3"
    assert Decimal(record["kas_usd_rate"]) == Decimal("0.125")
    assert Decimal(record["usd_value_at_settlement"]) == Decimal("2.5")
    assert record["settled_at_utc"].endswith(("Z", "+00:00"))


async def test_tax_report_is_empty_for_other_merchants(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    other = await seed_endpoint(sessionmaker, slug="other", address=NEW_ADDRESS)
    await add_settlement(sessionmaker, other, tx=f"{5:064x}", at=datetime.now(UTC) - timedelta(minutes=1))
    response = await client.get("/v1/tax-report.csv", headers=bearer(seeded))
    assert len(list(csv.reader(io.StringIO(response.text)))) == 1


# ---------------------------------------------------------------- public routes


async def test_directory_is_public_listed_only_and_cached_for_30s(
    client: httpx.AsyncClient,
    seeded: Seed,
    redis: Any,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    hidden = await client.post(
        "/v1/endpoints", json=endpoint_payload(slug="hidden-one", is_public_listed=False), headers=bearer(seeded)
    )
    assert hidden.status_code == 201

    first = await client.get("/v1/directory")
    assert first.status_code == 200
    manifest = DirectoryManifest.model_validate(first.json())
    assert [e.slug for e in manifest.endpoints] == ["weather"]
    entry = manifest.endpoints[0]
    assert entry.pay_to == MERCHANT_ADDRESS
    assert entry.price_sompi == PRICE_SOMPI
    assert entry.proxy_url == "http://gateway.test/p/weather"
    assert manifest.network == "kaspa:testnet-10"
    assert manifest.min_output_sompi == 20_000_000
    assert "max-age=30" in first.headers["cache-control"]
    assert 0 < await redis.ttl("kasgate:directory:v1") <= 30

    await client.post("/v1/endpoints", json=endpoint_payload(slug="newly-listed"), headers=bearer(seeded))
    second = await client.get("/v1/directory")
    assert second.json() == first.json()

    await redis.delete("kasgate:directory:v1")
    third = await client.get("/v1/directory")
    assert {e["slug"] for e in third.json()["endpoints"]} == {"weather", "newly-listed"}


async def test_quote_returns_a_payable_nonce_bound_payment_required(
    client: httpx.AsyncClient, seeded: Seed, redis: Any
) -> None:
    response = await client.get("/v1/quote/weather")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    required = PaymentRequired.model_validate(response.json())
    assert required.payTo == MERCHANT_ADDRESS
    assert required.amount == str(PRICE_SOMPI)
    assert required.platformFee.feeTo == TREASURY
    assert required.fee_amount_sompi == SPLIT.fee_sompi
    assert required.merchant_amount_sompi == SPLIT.merchant_sompi
    assert len(required.nonce) == 32
    assert 0 < await redis.ttl(nonce_key(required.nonce)) <= 120
    assert (await client.get("/v1/quote/weather")).json()["nonce"] != required.nonce


async def test_a_quote_can_be_paid_at_the_proxy(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, upstream: Upstream
) -> None:
    quote = PaymentRequired.model_validate((await client.get("/v1/quote/weather")).json())
    headers = {HEADER_PAYMENT_SIGNATURE: make_signature_header("cd" * 32, quote.nonce)}
    assert (await client.get("/p/weather", headers=headers)).status_code == 200
    assert stub_engine.calls == ["cd" * 32]
    # a quote and a 402 challenge are interchangeable but each nonce settles once
    assert (await client.get("/p/weather", headers=headers)).status_code == 409
    assert (await fetch_challenge(client)).nonce != quote.nonce


async def test_quote_unknown_slug_is_404(client: httpx.AsyncClient, seeded: Seed) -> None:
    response = await client.get("/v1/quote/missing")
    assert response.status_code == 404
    assert response.json()["error"] == "endpoint_not_found"


async def test_healthz_reports_dependencies(client: httpx.AsyncClient, seeded: Seed, monkeypatch: Any) -> None:
    from app import main

    monkeypatch.setattr(main.kaspa_engine, "is_connected", lambda: True)
    ok = await client.get("/healthz")
    assert ok.status_code == 200
    assert ok.json() == {
        "status": "ok",
        "network": "kaspa:testnet-10",
        "database": True,
        "redis": True,
        "kaspa_rpc": True,
    }
    monkeypatch.setattr(main.kaspa_engine, "is_connected", lambda: False)
    degraded = await client.get("/healthz")
    assert degraded.status_code == 200
    assert degraded.json()["status"] == "degraded"
    assert degraded.json()["kaspa_rpc"] is False


async def test_cors_exposes_payment_headers(client: httpx.AsyncClient, seeded: Seed) -> None:
    response = await client.get("/p/weather", headers={"Origin": "http://localhost:3000"})
    assert response.status_code == 402
    exposed = response.headers["access-control-expose-headers"].lower()
    for name in ("payment-required", "payment-signature", "payment-response"):
        assert name in exposed
    preflight = await client.options(
        "/p/weather",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "payment-signature",
        },
    )
    assert preflight.status_code == 200
    assert "payment-signature" in preflight.headers["access-control-allow-headers"].lower() or (
        preflight.headers["access-control-allow-headers"] == "payment-signature"
    )


# ---------------------------------------------------------------- USD-priced endpoints


USD_MICROS = 1_000_000  # $1.00
USD_RATE = Decimal("0.12500000")
USD_SOMPI = usd_micros_to_sompi(USD_MICROS, USD_RATE)  # 800_000_000


async def test_create_and_read_a_usd_priced_endpoint(client: httpx.AsyncClient, seeded: Seed) -> None:
    response = await client.post(
        "/v1/endpoints",
        json=endpoint_payload(slug="usd-feed", price_currency=PriceCurrency.USD, price_usd_micros=USD_MICROS),
        headers=bearer(seeded),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["price_currency"] == "USD"
    assert body["price_usd_micros"] == USD_MICROS
    assert body["price_usd"] == "1"
    assert body["price_sompi"] is None
    assert body["price_kas"] is None
    read = await client.get(f"/v1/endpoints/{body['id']}", headers=bearer(seeded))
    assert read.status_code == 200
    assert read.json()["price_currency"] == "USD"
    assert read.json()["price_usd_micros"] == USD_MICROS


async def test_usd_create_rejects_a_kas_price_and_missing_micros(client: httpx.AsyncClient, seeded: Seed) -> None:
    missing = endpoint_payload(price_currency="USD")
    missing.pop("price_sompi", None)
    assert (await client.post("/v1/endpoints", json=missing, headers=bearer(seeded))).status_code == 422
    both = {
        "name": "Price Feed",
        "upstream_url": "https://feeds.example.com/prices",
        "price_currency": "USD",
        "price_usd_micros": USD_MICROS,
        "price_sompi": PRICE_SOMPI,
    }
    assert (await client.post("/v1/endpoints", json=both, headers=bearer(seeded))).status_code == 422
    kas_and_micros = endpoint_payload(price_usd_micros=USD_MICROS)
    assert (await client.post("/v1/endpoints", json=kas_and_micros, headers=bearer(seeded))).status_code == 422


async def test_update_switches_between_kas_and_usd(client: httpx.AsyncClient, seeded: Seed) -> None:
    created = await client.post(
        "/v1/endpoints",
        json=endpoint_payload(slug="switchable", price_sompi=PRICE_SOMPI),
        headers=bearer(seeded),
    )
    endpoint_id = created.json()["id"]
    to_usd = await client.patch(
        f"/v1/endpoints/{endpoint_id}",
        json={"price_currency": "USD", "price_usd_micros": 2_500_000},
        headers=bearer(seeded),
    )
    assert to_usd.status_code == 200, to_usd.text
    assert to_usd.json()["price_currency"] == "USD"
    assert to_usd.json()["price_usd_micros"] == 2_500_000
    assert to_usd.json()["price_usd"] == "2.5"
    assert to_usd.json()["price_sompi"] is None
    to_kas = await client.patch(
        f"/v1/endpoints/{endpoint_id}",
        json={"price_currency": "KAS", "price_sompi": 3_000_000_000},
        headers=bearer(seeded),
    )
    assert to_kas.status_code == 200, to_kas.text
    assert to_kas.json()["price_currency"] == "KAS"
    assert to_kas.json()["price_sompi"] == 3_000_000_000
    assert to_kas.json()["price_usd_micros"] is None
    # currency switch without the matching price is 422
    assert (
        await client.patch(f"/v1/endpoints/{endpoint_id}", json={"price_currency": "USD"}, headers=bearer(seeded))
    ).status_code == 422


async def test_directory_lists_usd_and_kas_prices(client: httpx.AsyncClient, seeded: Seed, redis: Any) -> None:
    await redis.delete("kasgate:directory:v1")
    await client.post(
        "/v1/endpoints",
        json=endpoint_payload(
            slug="usd-listed", price_currency=PriceCurrency.USD, price_usd_micros=USD_MICROS, name="USD Feed"
        ),
        headers=bearer(seeded),
    )
    manifest = (await client.get("/v1/directory")).json()
    by_slug = {entry["slug"]: entry for entry in manifest["endpoints"]}
    kas = by_slug["weather"]
    assert kas["price_currency"] == "KAS"
    assert kas["price_sompi"] == PRICE_SOMPI
    assert kas["price_usd_micros"] is None
    usd = by_slug["usd-listed"]
    assert usd["price_currency"] == "USD"
    assert usd["price_usd_micros"] == USD_MICROS
    assert usd["price_usd"] == "1"
    assert usd["price_sompi"] is None
    assert usd["price_kas"] is None


async def test_quote_of_a_usd_endpoint_locks_the_converted_sompi(
    client: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    stub_oracle: Any,
    redis: Any,
) -> None:
    seed = await seed_endpoint(
        sessionmaker,
        slug="usd-quote",
        address=NEW_ADDRESS,
        price_currency=PriceCurrency.USD,
        price_usd_micros=USD_MICROS,
        price_sompi=None,
    )
    response = await client.get("/v1/quote/usd-quote")
    assert response.status_code == 200, response.text
    required = PaymentRequired.model_validate(response.json())
    assert required.amount == str(USD_SOMPI)
    assert required.extra is not None
    assert required.extra.kasUsdRate == "0.12500000"
    assert required.extra.quotedAt.tzinfo is not None
    stub_oracle.rate = Decimal("0.25000000")  # a later quote would convert to half as many sompi
    later = PaymentRequired.model_validate((await client.get("/v1/quote/usd-quote")).json())
    assert later.amount == str(usd_micros_to_sompi(USD_MICROS, Decimal("0.25000000")))
    # the first nonce still settles at the locked 800_000_000, not the new rate
    headers = {HEADER_PAYMENT_SIGNATURE: make_signature_header("ab" * 32, required.nonce)}
    paid = await client.get("/p/usd-quote", headers=headers)
    assert paid.status_code == 200, paid.text
    async with sessionmaker() as session:
        row = (await session.execute(select(Settlement).where(Settlement.payment_nonce == required.nonce))).scalar_one()
    assert row.amount_sompi == USD_SOMPI
    assert row.kas_usd_rate == Decimal("0.12500000")
    assert seed.slug == "usd-quote"


async def test_quote_of_a_usd_endpoint_fails_closed_when_the_oracle_is_down(
    client: httpx.AsyncClient, sessionmaker: async_sessionmaker[AsyncSession], stub_oracle: Any
) -> None:
    await seed_endpoint(
        sessionmaker,
        slug="usd-down",
        address=NEW_ADDRESS,
        price_currency=PriceCurrency.USD,
        price_usd_micros=USD_MICROS,
        price_sompi=None,
    )
    stub_oracle.rate = None
    response = await client.get("/v1/quote/usd-down")
    assert response.status_code == 503
    assert response.json()["error"] == "price_unavailable"
    assert response.headers["retry-after"]
