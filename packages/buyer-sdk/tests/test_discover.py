"""KasGateClient.discover / KasGateSyncClient.discover over the public catalog."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from conftest import PAYER_KEY, static_utxos
from pydantic import ValidationError

from kasgate_client import (
    CatalogEndpoint,
    CatalogPage,
    ConfigurationError,
    DiscoverResult,
    KasGateClient,
    KasGateSyncClient,
    ProtocolMismatchError,
)

KAS = 100_000_000
CHEAP = 60_000_000
EXPENSIVE = 5_000_000_000

ITEMS = [
    {
        "slug": "weather-abc",
        "name": "Weather",
        "description": "forecasts",
        "methods": ["GET"],
        "price_sompi": CHEAP,
        "network": "kaspa:testnet-10",
        "schemes": ["kaspa-exact-v1"],
        "paid_url": "http://gateway.test/p/weather-abc",
        "input_schema": None,
    },
    {
        "slug": "prices-xyz",
        "name": "Prices",
        "description": "market data",
        "methods": ["GET", "POST"],
        "price_sompi": EXPENSIVE,
        "network": "kaspa:testnet-10",
        "schemes": ["kaspa-exact-v1"],
        "paid_url": "http://gateway.test/p/prices-xyz",
        "input_schema": None,
    },
    {
        "slug": "news-123",
        "name": "News",
        "description": "headlines",
        "methods": ["GET"],
        "price_sompi": CHEAP,
        "network": "kaspa:testnet-10",
        "schemes": ["kaspa-exact-v1"],
        "paid_url": "http://gateway.test/p/news-123",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    },
]


def page_payload(items: list[dict[str, Any]], *, next_cursor: str | None, limit: int) -> dict[str, Any]:
    return {
        "catalog_version": 1,
        "x402_version": 2,
        "scheme": "kaspa-exact-v1",
        "asset": "KAS",
        "network": "kaspa:testnet-10",
        "gateway_base_url": "http://gateway.test",
        "items": items,
        "next_cursor": next_cursor,
        "limit": limit,
    }


class CatalogGateway:
    def __init__(self) -> None:
        self.calls: list[httpx.URL] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url)
        if request.url.path not in {"/v1/catalog", "/.well-known/x402"}:
            return httpx.Response(404, json={"detail": "not found"})
        try:
            limit = int(request.url.params.get("limit", "50"))
        except ValueError:
            return httpx.Response(422, json={"detail": "invalid limit"})
        cursor = request.url.params.get("cursor")
        query = (request.url.params.get("q") or "").casefold()
        entries = [
            item
            for item in ITEMS
            if not query
            or query in item["slug"]
            or query in item["name"].casefold()
            or query in (item["description"] or "").casefold()
        ]
        start = 0
        if cursor:
            slugs = [item["slug"] for item in entries]
            start = slugs.index(cursor) + 1 if cursor in slugs else len(entries)
        window = entries[start : start + limit]
        next_cursor = window[-1]["slug"] if start + limit < len(entries) else None
        return httpx.Response(200, json=page_payload(window, next_cursor=next_cursor, limit=limit))


def client_for(gw: CatalogGateway, **kwargs: Any) -> KasGateClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(gw), base_url="http://gateway.test")
    return KasGateClient(
        kwargs.pop("private_key_hex", PAYER_KEY),
        utxo_provider=static_utxos([]),
        http_client=http,
        **kwargs,
    )


async def test_discover_follows_pages_and_flags_endpoints_above_the_cap() -> None:
    gw = CatalogGateway()
    client = client_for(gw, max_price_sompi=1 * KAS)
    result = await client.discover("http://gateway.test", limit=1)
    assert isinstance(result, DiscoverResult)
    CatalogPage.model_validate(result.catalog.model_dump())
    assert [item.slug for item in result.items] == ["weather-abc", "prices-xyz", "news-123"]
    assert result.next_cursor is None
    assert [item.slug for item in result.payable] == ["weather-abc", "news-123"]
    assert [item.slug for item in result.exceeding_max_price] == ["prices-xyz"]
    assert result.items[2].input_schema == {"type": "object", "properties": {"q": {"type": "string"}}}
    assert len(gw.calls) == 3
    assert {url.path for url in gw.calls} == {"/v1/catalog"}


async def test_discover_can_return_a_single_page_and_use_the_well_known_path() -> None:
    gw = CatalogGateway()
    client = client_for(gw)
    first = await client.discover("http://gateway.test", limit=1, follow_pages=False, well_known=True)
    assert [item.slug for item in first.items] == ["weather-abc"]
    assert first.next_cursor == "weather-abc"
    second = await client.discover(
        "http://gateway.test", limit=1, cursor=first.next_cursor, follow_pages=False, well_known=True
    )
    assert [item.slug for item in second.items] == ["prices-xyz"]
    assert all(url.path == "/.well-known/x402" for url in gw.calls)


async def test_discover_query_filters_on_the_gateway() -> None:
    gw = CatalogGateway()
    client = client_for(gw)
    result = await client.discover("http://gateway.test", query="market")
    assert [item.slug for item in result.items] == ["prices-xyz"]
    assert gw.calls[0].params["q"] == "market"


async def test_discover_rejects_invalid_limit_and_query() -> None:
    client = client_for(CatalogGateway())
    with pytest.raises(ConfigurationError):
        await client.discover("http://gateway.test", limit=0)
    with pytest.raises(ConfigurationError):
        await client.discover("http://gateway.test", limit=201)
    with pytest.raises(ConfigurationError):
        await client.discover("http://gateway.test", query="x" * 121)


async def test_discover_raises_on_a_malformed_or_failed_catalog() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("fail") == "1":
            return httpx.Response(500, json={"detail": "no"})
        return httpx.Response(200, json={"unexpected": True})

    client = KasGateClient(PAYER_KEY, utxo_provider=static_utxos([]), http_client=httpx.AsyncClient())
    # Swap the transport after construction by using a dedicated client:
    http = httpx.AsyncClient(transport=httpx.MockTransport(broken))
    client._http = http
    with pytest.raises(ProtocolMismatchError, match="invalid body"):
        await client.discover("http://gateway.test")

    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "down"})

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(failing))
    with pytest.raises(ProtocolMismatchError, match="503"):
        await client.discover("http://gateway.test")


def test_catalog_models_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CatalogEndpoint.model_validate({**ITEMS[0], "upstream_url": "https://secret.example"})
    with pytest.raises(ValidationError):
        CatalogPage.model_validate({**page_payload(ITEMS, next_cursor=None, limit=50), "treasury": "hidden"})


def test_sync_discover() -> None:
    gw = CatalogGateway()
    http = httpx.AsyncClient(transport=httpx.MockTransport(gw), base_url="http://gateway.test")
    with KasGateSyncClient(PAYER_KEY, utxo_provider=static_utxos([]), http_client=http, max_price_sompi=1 * KAS) as client:
        result = client.discover("http://gateway.test", limit=2)
    assert [item.slug for item in result.items] == ["weather-abc", "prices-xyz", "news-123"]
    assert [item.slug for item in result.payable] == ["weather-abc", "news-123"]
    assert result.catalog.network == "kaspa:testnet-10"
