"""Public agent catalog: /v1/catalog, /.well-known/x402, /llms.txt, caching, pagination, no secret leakage."""

from __future__ import annotations

from typing import Any

import httpx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Merchant
from app.schemas import CatalogEndpoint, CatalogPage
from tests.conftest import PRICE_SOMPI, UPSTREAM_SECRET, Seed
from tests.test_api import bearer, endpoint_payload

SECRET_MARKERS = (
    UPSTREAM_SECRET,
    "very-secret",
    "feeds.example.com",
    "api.upstream.test",
    "merchant-private@example.com",
    "api_key",
    "kg_",
)


def _assert_public_item(item: CatalogEndpoint | dict[str, Any], slug: str) -> CatalogEndpoint:
    parsed = item if isinstance(item, CatalogEndpoint) else CatalogEndpoint.model_validate(item)
    assert parsed.slug == slug
    assert parsed.price_currency == "KAS"
    assert parsed.price_sompi == PRICE_SOMPI
    assert parsed.price_kas is not None
    assert parsed.price_usd_micros is None
    assert parsed.price_usd is None
    assert parsed.network == "kaspa:testnet-10"
    assert parsed.schemes == ["kaspa-exact-v1", "kasgate-prepaid-v1"]  # CONTRACT v2.11: prepaid debits too
    assert parsed.prepaid_only is False
    assert parsed.paid_url == f"http://gateway.test/p/{slug}"
    assert parsed.input_schema is None
    assert parsed.methods
    return parsed


def _assert_no_secrets(text: str) -> None:
    lowered = text.lower()
    for marker in SECRET_MARKERS:
        assert marker.lower() not in lowered


async def _mark_merchant_email(sessionmaker: async_sessionmaker[AsyncSession], seed: Seed) -> None:
    async with sessionmaker() as session:
        await session.execute(
            update(Merchant).where(Merchant.id == seed.merchant_id).values(email="merchant-private@example.com")
        )
        await session.commit()


async def test_catalog_and_well_known_list_only_public_listed_endpoints(
    client: httpx.AsyncClient,
    seeded: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _mark_merchant_email(sessionmaker, seeded)
    hidden = await client.post(
        "/v1/endpoints", json=endpoint_payload(slug="hidden-catalog", is_public_listed=False), headers=bearer(seeded)
    )
    assert hidden.status_code == 201
    listed = await client.post(
        "/v1/endpoints", json=endpoint_payload(slug="catalog-extra", description="listed extra"), headers=bearer(seeded)
    )
    assert listed.status_code == 201

    for path in ("/v1/catalog", "/.well-known/x402"):
        response = await client.get(path)
        assert response.status_code == 200
        assert "max-age=30" in response.headers["cache-control"]
        assert response.headers["etag"].startswith('"') and response.headers["etag"].endswith('"')
        page = CatalogPage.model_validate(response.json())
        slugs = [item.slug for item in page.items]
        assert "weather" in slugs
        assert "catalog-extra" in slugs
        assert "hidden-catalog" not in slugs
        assert page.next_cursor is None
        assert page.limit == 50
        assert page.x402_version == 2 and page.scheme == "kaspa-exact-v1"
        assert page.network == "kaspa:testnet-10"
        assert page.gateway_base_url == "http://gateway.test"
        for item in page.items:
            _assert_public_item(item, item.slug)
        _assert_no_secrets(response.text)
        # The two catalog URLs must agree on the public contract (etag is of the exact body).
        other = "/.well-known/x402" if path == "/v1/catalog" else "/v1/catalog"
        twin = await client.get(other)
        assert twin.json() == response.json()
        assert twin.headers["etag"] == response.headers["etag"]


async def test_catalog_if_none_match_returns_304(
    client: httpx.AsyncClient, seeded: Seed, redis: Any
) -> None:
    first = await client.get("/v1/catalog")
    assert first.status_code == 200
    etag = first.headers["etag"]
    cached = await client.get("/v1/catalog", headers={"If-None-Match": etag})
    assert cached.status_code == 304
    assert cached.content == b""
    assert cached.headers["etag"] == etag
    weak = await client.get("/v1/catalog", headers={"If-None-Match": f"W/{etag}"})
    assert weak.status_code == 304
    # A different page has a different body and must not 304 under the first page's etag.
    page2 = await client.get("/v1/catalog", params={"limit": 1}, headers={"If-None-Match": etag})
    assert page2.status_code == 200
    assert page2.headers["etag"] != etag


async def test_catalog_is_cached_for_30s_and_omits_unlisted_until_expiry(
    client: httpx.AsyncClient, seeded: Seed, redis: Any
) -> None:
    first = await client.get("/v1/catalog")
    assert first.status_code == 200
    assert 0 < await redis.ttl("kasgate:catalog:v1") <= 30
    created = await client.post(
        "/v1/endpoints", json=endpoint_payload(slug="catalog-cached-later"), headers=bearer(seeded)
    )
    assert created.status_code == 201
    second = await client.get("/v1/catalog")
    assert second.json() == first.json()
    await redis.delete("kasgate:catalog:v1")
    third = await client.get("/v1/catalog")
    assert {item["slug"] for item in third.json()["items"]} >= {"weather", "catalog-cached-later"}


async def test_catalog_pagination_and_query(
    client: httpx.AsyncClient, seeded: Seed, redis: Any
) -> None:
    for slug, name, description in (
        ("catalog-alpha", "Alpha Weather", "forecasts"),
        ("catalog-beta", "Beta Prices", "market data"),
        ("catalog-gamma", "Gamma News", "headlines"),
    ):
        response = await client.post(
            "/v1/endpoints",
            json=endpoint_payload(slug=slug, name=name, description=description),
            headers=bearer(seeded),
        )
        assert response.status_code == 201
    await redis.delete("kasgate:catalog:v1")

    first = await client.get("/v1/catalog", params={"limit": 2})
    page = CatalogPage.model_validate(first.json())
    assert len(page.items) == 2
    assert page.limit == 2
    assert page.next_cursor
    seen = [item.slug for item in page.items]

    second = await client.get("/v1/catalog", params={"limit": 2, "cursor": page.next_cursor})
    nxt = CatalogPage.model_validate(second.json())
    seen.extend(item.slug for item in nxt.items)
    assert len(set(seen)) == len(seen)
    assert {"weather", "catalog-alpha", "catalog-beta", "catalog-gamma"} <= set(seen)

    # Walking off the end yields an empty page, not an error.
    if nxt.next_cursor:
        last = await client.get("/v1/catalog", params={"limit": 2, "cursor": nxt.next_cursor})
        assert last.status_code == 200
        # May still have leftover items if more than 4 exist; either way the cursor must be well-formed.

    searched = await client.get("/v1/catalog", params={"q": "market"})
    hits = CatalogPage.model_validate(searched.json())
    assert [item.slug for item in hits.items] == ["catalog-beta"]

    named = await client.get("/v1/catalog", params={"q": "Weather"})
    assert {"weather", "catalog-alpha"} <= {item.slug for item in CatalogPage.model_validate(named.json()).items}


async def test_catalog_rejects_an_invalid_cursor_and_out_of_range_limit(
    client: httpx.AsyncClient, seeded: Seed
) -> None:
    assert (await client.get("/v1/catalog", params={"cursor": "not-a-cursor"})).status_code == 422
    assert (await client.get("/v1/catalog", params={"limit": 0})).status_code == 422
    assert (await client.get("/v1/catalog", params={"limit": 201})).status_code == 422


async def test_catalog_item_returns_a_listed_endpoint_and_404s_otherwise(
    client: httpx.AsyncClient, seeded: Seed
) -> None:
    hidden = await client.post(
        "/v1/endpoints", json=endpoint_payload(slug="secret-item", is_public_listed=False), headers=bearer(seeded)
    )
    assert hidden.status_code == 201
    found = await client.get("/v1/catalog/weather")
    assert found.status_code == 200
    _assert_public_item(found.json(), "weather")
    _assert_no_secrets(found.text)
    assert "etag" in found.headers
    not_modified = await client.get("/v1/catalog/weather", headers={"If-None-Match": found.headers["etag"]})
    assert not_modified.status_code == 304
    missing = await client.get("/v1/catalog/secret-item")
    assert missing.status_code == 404
    assert missing.json()["error"] == "endpoint_not_found"
    assert (await client.get("/v1/catalog/does-not-exist")).status_code == 404


async def test_llms_txt_describes_agent_payment_and_leaks_no_secrets(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await _mark_merchant_email(sessionmaker, seeded)
    response = await client.get("/llms.txt")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    for needle in (
        "PAYMENT-REQUIRED",
        "PAYMENT-SIGNATURE",
        "PAYMENT-RESPONSE",
        "0.6 KAS",
        "0.2 KAS",
        "98.5%",
        "1.5%",
        "kaspa:testnet-10",
        "/v1/catalog",
        "/.well-known/x402",
        "KasGateClient.discover",
        "call_with_budget",
        "kasgate-mcp",
        "kasgate-prepaid-v1",
        "/v1/prepaid/challenge",
        "X-KasGate-Prepaid",
        "call_prepaid_endpoint",
        "kasgate-trial-v1",
        "/v1/trials/challenge",
        "X-KasGate-Trial",
        "trial_exhausted",
        "verify_webhook_signature",
    ):
        assert needle in body
    _assert_no_secrets(body)
    assert "TODO" not in body and "FIXME" not in body and "implement later" not in body
