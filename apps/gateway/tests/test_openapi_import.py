"""POST /v1/endpoints/import: OpenAPI 3.0/3.1 parsing, preview, all-or-nothing creation, limits and SSRF."""

import json
import socket
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.api_routes as api_routes
from app import security
from app.models import Endpoint
from app.openapi_import import (
    MAX_NESTING_DEPTH,
    OpenApiImportError,
    allocate_slug,
    load_document,
    parse_operations,
    proxy_suffix,
    slug_base,
    static_prefix,
    upstream_url_for,
)
from app.schemas import (
    OPENAPI_MAX_DOCUMENT_BYTES,
    OPENAPI_MAX_OPERATIONS,
    EndpointRead,
    OpenApiImportCreated,
    OpenApiImportPreview,
    min_price_sompi,
)
from app.security import build_upstream_client, decrypt_headers
from tests.conftest import (
    OTHER_KEY,
    Seed,
    StubEngine,
    Upstream,
    paid_headers,
    register,
)

BASE_URL = "https://api.petstore.example/v1"
SPEC_URL = "https://specs.petstore.example/openapi.yaml"
DEFAULT_PRICE = 100_000_000  # 1 KAS
MIN_PRICE = 60_000_000

PETSTORE_30: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Petstore", "version": "1.0.0"},
    "servers": [{"url": "https://petstore.example.com/v1"}],
    "paths": {
        "/pets": {
            "get": {"operationId": "listPets", "summary": "List all pets", "tags": ["pets"]},
            "post": {"operationId": "createPet", "summary": "Create a pet"},
        },
        "/pets/{petId}": {
            "get": {
                "operationId": "showPetById",
                "summary": "Info for a specific pet",
                "responses": {
                    "200": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}}}
                },
            },
            "head": {"operationId": "petExists"},
            "delete": {"summary": "Delete a pet"},
        },
        "/pets/{petId}/photos/{photoId}": {"put": {}},
    },
    "components": {"schemas": {"Pet": {"type": "object", "properties": {"id": {"type": "integer"}}}}},
}

PETSTORE_31_YAML = """\
openapi: 3.1.0
info:
  title: Petstore YAML
  version: "2.0"
paths:
  /stores/{storeId}/orders:
    $ref: '#/components/pathItems/Orders'
  /health:
    get:
      summary: Health check
components:
  pathItems:
    Orders:
      get:
        operationId: listOrders
        summary: List orders
      patch:
        operationId: updateOrders
      trace:
        operationId: traceOrders
  schemas:
    Order:
      $ref: '#/components/schemas/Base'
    Base:
      type: object
"""


def bearer(seed: Seed) -> dict[str, str]:
    return {"Authorization": f"Bearer {seed.api_key}"}


def body(mode: str = "preview", **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mode": mode,
        "spec_text": json.dumps(PETSTORE_30),
        "upstream_base_url": BASE_URL,
        "default_price_sompi": DEFAULT_PRICE,
    }
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not None}


async def preview(client: httpx.AsyncClient, seed: Seed, **overrides: Any) -> OpenApiImportPreview:
    response = await client.post("/v1/endpoints/import", json=body(**overrides), headers=bearer(seed))
    assert response.status_code == 200, response.text
    return OpenApiImportPreview.model_validate(response.json())


async def endpoint_count(maker: async_sessionmaker[AsyncSession]) -> int:
    async with maker() as session:
        return int(await session.scalar(select(func.count(Endpoint.id))) or 0)


def detail(response: httpx.Response) -> str:
    return json.dumps(response.json()["detail"])


# ---------------------------------------------------------------- pure helpers


def test_path_params_map_onto_suffix_routing() -> None:
    assert static_prefix("/pets") == "/pets"
    assert static_prefix("/pets/") == "/pets/"
    assert static_prefix("/") == ""
    assert static_prefix("/pets/{petId}") == "/pets"
    assert static_prefix("/{tenant}/pets") == ""
    assert static_prefix("/files/{name}.json") == "/files"
    assert proxy_suffix("/pets") == ""
    assert proxy_suffix("/pets/{petId}") == "/{petId}"
    assert proxy_suffix("/pets/{petId}/photos/{photoId}") == "/{petId}/photos/{photoId}"
    assert proxy_suffix("/{tenant}/pets") == "/{tenant}/pets"
    assert upstream_url_for("https://api.example.com/v1/", "/pets/{petId}") == "https://api.example.com/v1/pets"
    assert upstream_url_for("https://api.example.com/v1", "/pets") == "https://api.example.com/v1/pets"
    assert upstream_url_for("https://api.example.com/v1?key=1", "/a b") == "https://api.example.com/v1/a%20b?key=1"
    assert upstream_url_for("https://api.example.com/", "/") == "https://api.example.com/"


def test_slugs_are_deterministic_and_collision_safe() -> None:
    operations = {op.key: op for op in parse_operations(load_document(json.dumps(PETSTORE_30)))}
    assert slug_base(operations["GET /pets"]) == "list-pets"
    assert slug_base(operations["DELETE /pets/{petId}"]) == "delete-pets-pet-id"
    assert allocate_slug("list-pets", set()) == "list-pets"
    assert allocate_slug("list-pets", {"list-pets", "list-pets-2"}) == "list-pets-3"
    long_base = "a" * 64
    assert allocate_slug(long_base, {long_base}) == "a" * 62 + "-2"


# ---------------------------------------------------------------- preview


async def test_preview_openapi_30_json_lists_operations_and_creates_nothing(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    before = await endpoint_count(sessionmaker)
    result = await preview(client, seeded)
    assert await endpoint_count(sessionmaker) == before
    assert result.openapi_version == "3.0.3"
    assert result.title == "Petstore"
    assert result.servers == ["https://petstore.example.com/v1"]
    assert result.min_price_sompi == min_price_sompi() == MIN_PRICE
    rows = {row.key: row for row in result.operations}
    assert list(rows) == [
        "GET /pets",
        "POST /pets",
        "GET /pets/{petId}",
        "DELETE /pets/{petId}",
        "HEAD /pets/{petId}",
        "PUT /pets/{petId}/photos/{photoId}",
    ]
    assert result.operation_count == 6 and result.importable_count == 5
    show = rows["GET /pets/{petId}"]
    assert show.slug == "show-pet-by-id"
    assert show.upstream_url == f"{BASE_URL}/pets"
    assert show.proxy_path == "/p/show-pet-by-id/{petId}"
    assert show.proxy_url == "http://gateway.test/p/show-pet-by-id"
    assert show.path_params == ["petId"]
    assert show.price_sompi == DEFAULT_PRICE
    head = rows["HEAD /pets/{petId}"]
    assert head.importable is False and head.slug is None
    assert head.skip_reason is not None and "HEAD" in head.skip_reason
    photo = rows["PUT /pets/{petId}/photos/{photoId}"]
    assert photo.operation_id is None
    assert photo.slug == "put-pets-pet-id-photos-photo-id"
    assert photo.proxy_path == "/p/put-pets-pet-id-photos-photo-id/{petId}/photos/{photoId}"


async def test_preview_openapi_31_yaml_with_local_path_item_refs(client: httpx.AsyncClient, seeded: Seed) -> None:
    result = await preview(client, seeded, spec_text=PETSTORE_31_YAML)
    assert result.openapi_version == "3.1.0"
    rows = {row.key: row for row in result.operations}
    assert set(rows) == {
        "GET /stores/{storeId}/orders",
        "PATCH /stores/{storeId}/orders",
        "TRACE /stores/{storeId}/orders",
        "GET /health",
    }
    orders = rows["GET /stores/{storeId}/orders"]
    assert orders.slug == "list-orders"
    assert orders.upstream_url == f"{BASE_URL}/stores"
    assert orders.proxy_path == "/p/list-orders/{storeId}/orders"
    assert rows["TRACE /stores/{storeId}/orders"].importable is False
    assert rows["GET /health"].slug == "get-health"
    assert rows["GET /health"].upstream_url == f"{BASE_URL}/health"


async def test_preview_accepts_unquoted_yaml_version_and_truncates_long_text(
    client: httpx.AsyncClient, seeded: Seed
) -> None:
    long = "word " * 2000
    spec = f"openapi: 3.1\npaths:\n  /a:\n    get:\n      summary: '{long}'\n      operationId: '{'x' * 500}'\n"
    result = await preview(client, seeded, spec_text=spec)
    row = result.operations[0]
    assert row.summary is not None and len(row.summary) <= 200
    assert row.operation_id is not None and len(row.operation_id) <= 200
    assert row.slug is not None and len(row.slug) <= 64


async def test_preview_slugs_avoid_existing_and_in_batch_duplicates(client: httpx.AsyncClient, seeded: Seed) -> None:
    taken = await client.post(
        "/v1/endpoints",
        json={"name": "x", "slug": "list-pets", "upstream_url": BASE_URL, "price_sompi": DEFAULT_PRICE},
        headers=bearer(seeded),
    )
    assert taken.status_code == 201
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "dup", "version": "1"},
        "paths": {
            "/a": {"get": {"operationId": "listPets"}},
            "/b": {"get": {"operationId": "listPets"}},
            "/c": {"get": {"operationId": "list_pets"}},
        },
    }
    result = await preview(client, seeded, spec_text=json.dumps(doc))
    assert [row.slug for row in result.operations] == ["list-pets-2", "list-pets-3", "list-pets-4"]


# ---------------------------------------------------------------- create


async def test_create_imports_selected_operations_with_overrides(
    client: httpx.AsyncClient,
    seeded: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    stub_engine: StubEngine,
    upstream: Upstream,
) -> None:
    selections = [
        {"method": "GET", "path": "/pets/{petId}", "price_sompi": 250_000_000},
        {"method": "post", "path": "/pets"},
    ]
    response = await client.post(
        "/v1/endpoints/import",
        json=body("create", selections=selections, upstream_headers={"X-Api-Key": "imported-secret"}, category="pets"),
        headers=bearer(seeded),
    )
    assert response.status_code == 201, response.text
    assert "imported-secret" not in response.text
    created = OpenApiImportCreated.model_validate(response.json())
    assert created.created_count == 2
    by_slug = {endpoint.slug: endpoint for endpoint in created.created}
    show = by_slug["show-pet-by-id"]
    assert show.price_sompi == 250_000_000 and show.price_kas == "2.5"
    assert show.allowed_methods == ["GET"]
    assert show.upstream_url == f"{BASE_URL}/pets"
    assert show.name == "Info for a specific pet"
    assert show.category == "pets"
    assert show.proxy_url == "http://gateway.test/p/show-pet-by-id"
    assert by_slug["create-pet"].price_sompi == DEFAULT_PRICE
    assert by_slug["create-pet"].allowed_methods == ["POST"]

    # identical to a hand-created endpoint: same list/read schema, encrypted headers, proxy semantics
    listing = await client.get("/v1/endpoints", headers=bearer(seeded))
    assert {"show-pet-by-id", "create-pet"} <= {item["slug"] for item in listing.json()}
    EndpointRead.model_validate(listing.json()[-1])
    async with sessionmaker() as session:
        stored = (await session.execute(select(Endpoint).where(Endpoint.slug == "show-pet-by-id"))).scalar_one()
        assert decrypt_headers(stored.upstream_headers_encrypted) == {"X-Api-Key": "imported-secret"}
        assert stored.merchant_id == seeded.merchant_id

    assert (await client.post("/p/show-pet-by-id/42")).status_code == 405
    paid = await client.get("/p/show-pet-by-id/42", headers=await paid_headers(client, slug="show-pet-by-id"))
    assert paid.status_code == 200, paid.text
    assert str(upstream.requests[-1].url) == f"{BASE_URL}/pets/42"
    assert upstream.requests[-1].headers["X-Api-Key"] == "imported-secret"


async def test_create_rejects_prices_below_the_minimum_per_row(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    before = await endpoint_count(sessionmaker)
    low_default = await client.post(
        "/v1/endpoints/import", json=body(default_price_sompi=MIN_PRICE - 1), headers=bearer(seeded)
    )
    assert low_default.status_code == 422
    assert low_default.json()["detail"][0]["loc"] == ["body", "default_price_sompi"]
    selections = [
        {"method": "GET", "path": "/pets"},
        {"method": "POST", "path": "/pets", "price_sompi": MIN_PRICE - 1},
        {"method": "GET", "path": "/pets/{petId}", "price_sompi": 10**18},
    ]
    low_row = await client.post(
        "/v1/endpoints/import", json=body("create", selections=selections), headers=bearer(seeded)
    )
    assert low_row.status_code == 422
    locs = [error["loc"] for error in low_row.json()["detail"]]
    assert ["body", "selections", 1, "price_sompi"] in locs
    assert ["body", "selections", 2, "price_sompi"] in locs
    assert "minimum price is 60000000 sompi" in detail(low_row)
    at_floor = await client.post(
        "/v1/endpoints/import",
        json=body("create", selections=[{"method": "GET", "path": "/pets", "price_sompi": MIN_PRICE}]),
        headers=bearer(seeded),
    )
    assert at_floor.status_code == 201
    assert await endpoint_count(sessionmaker) == before + 1


async def test_create_rejects_unknown_skipped_and_disallowed_selections(
    client: httpx.AsyncClient, seeded: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    before = await endpoint_count(sessionmaker)
    unknown = await client.post(
        "/v1/endpoints/import",
        json=body("create", selections=[{"method": "GET", "path": "/pets"}, {"method": "GET", "path": "/nope"}]),
        headers=bearer(seeded),
    )
    assert unknown.status_code == 422
    assert unknown.json()["detail"][0]["loc"] == ["body", "selections", 1]
    head = await client.post(
        "/v1/endpoints/import",
        json=body("create", selections=[{"method": "HEAD", "path": "/pets/{petId}"}]),
        headers=bearer(seeded),
    )
    assert head.status_code == 422
    assert head.json()["detail"][0]["loc"][:4] == ["body", "selections", 0, "method"]
    doc = {"openapi": "3.0.0", "paths": {"/a/../b": {"get": {}}, "/ok": {"get": {}}}}
    traversal = await client.post(
        "/v1/endpoints/import",
        json=body("create", spec_text=json.dumps(doc), selections=[{"method": "GET", "path": "/a/../b"}]),
        headers=bearer(seeded),
    )
    assert traversal.status_code == 422
    assert "cannot be imported" in detail(traversal)
    twice = await client.post(
        "/v1/endpoints/import",
        json=body("create", selections=[{"method": "GET", "path": "/pets"}, {"method": "get", "path": "/pets"}]),
        headers=bearer(seeded),
    )
    assert twice.status_code == 422
    assert await endpoint_count(sessionmaker) == before


async def test_create_is_all_or_nothing(
    client: httpx.AsyncClient,
    seeded: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clash = await client.post(
        "/v1/endpoints",
        json={"name": "x", "slug": "create-pet", "upstream_url": BASE_URL, "price_sompi": DEFAULT_PRICE},
        headers=bearer(seeded),
    )
    assert clash.status_code == 201
    before = await endpoint_count(sessionmaker)

    async def nothing_taken(session: AsyncSession, bases: set[str]) -> set[str]:
        return set()  # simulates a concurrent import claiming "create-pet" after the slug check

    monkeypatch.setattr(api_routes, "_taken_slugs", nothing_taken)
    selections = [{"method": "GET", "path": "/pets"}, {"method": "POST", "path": "/pets"}]
    response = await client.post(
        "/v1/endpoints/import", json=body("create", selections=selections), headers=bearer(seeded)
    )
    assert response.status_code == 409
    assert "nothing was created" in response.json()["detail"]
    assert await endpoint_count(sessionmaker) == before
    async with sessionmaker() as session:
        assert await session.scalar(select(Endpoint.id).where(Endpoint.slug == "list-pets")) is None


async def test_import_requires_auth_and_is_scoped_to_the_merchant(client: httpx.AsyncClient, seeded: Seed) -> None:
    assert (await client.post("/v1/endpoints/import", json=body())).status_code == 401
    bad = {"Authorization": "Bearer kg_" + "x" * 40}
    assert (await client.post("/v1/endpoints/import", json=body(), headers=bad)).status_code == 401
    other = await register(client, OTHER_KEY)
    session_headers = {"Authorization": f"Bearer {other.session_token}"}
    imported = await client.post(
        "/v1/endpoints/import",
        json=body("create", selections=[{"method": "GET", "path": "/pets"}]),
        headers=session_headers,
    )
    assert imported.status_code == 201, imported.text
    assert imported.json()["created"][0]["merchant_id"] == str(other.merchant.id)
    mine = await client.get("/v1/endpoints", headers=bearer(seeded))
    assert [item["slug"] for item in mine.json()] == ["weather"]
    endpoint_id = imported.json()["created"][0]["id"]
    assert (await client.get(f"/v1/endpoints/{endpoint_id}", headers=bearer(seeded))).status_code == 404


# ---------------------------------------------------------------- request validation


@pytest.mark.parametrize(
    "overrides",
    [
        {"spec_text": None},
        {"spec_url": SPEC_URL},
        {"selections": [{"method": "GET", "path": "/pets"}]},
        {"mode": "create"},
        {"mode": "upsert"},
        {"default_price_sompi": 10**18},
        {"default_price_sompi": 0},
        {"unexpected": True},
        {"upstream_headers": {"Host": "evil.example"}},
        {"category": "c" * 61},
    ],
)
async def test_invalid_requests_are_422(client: httpx.AsyncClient, seeded: Seed, overrides: dict[str, Any]) -> None:
    payload = body(**{key: value for key, value in overrides.items() if value is not None})
    if overrides.get("spec_text", "") is None:
        payload.pop("spec_text")
    response = await client.post("/v1/endpoints/import", json=payload, headers=bearer(seeded))
    assert response.status_code == 422, response.text


async def test_malformed_json_body_is_422(client: httpx.AsyncClient, seeded: Seed) -> None:
    response = await client.post(
        "/v1/endpoints/import",
        content=b"{not json",
        headers={**bearer(seeded), "Content-Type": "application/json"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------- bad documents


BILLION_LAUGHS = (
    "openapi: 3.0.0\npaths:\n  /a:\n    get: {}\nx-bomb:\n  l0: &l0 [lol, lol, lol, lol, lol, lol, lol, lol, lol]\n"
    + "".join(f"  l{i}: &l{i} [{', '.join([f'*l{i - 1}'] * 9)}]\n" for i in range(1, 9))
)


@pytest.mark.parametrize(
    ("spec_text", "message"),
    [
        ("[1, 2, 3]", "must be a JSON/YAML object"),
        ("just a string", "must be a JSON/YAML object"),
        ('{"openapi": "3.0.0", "info": {}}', "no 'paths'"),
        ('{"openapi": "3.0.0", "paths": {}}', "no 'paths'"),
        ('{"openapi": "3.0.0", "paths": {"/a": {"parameters": []}}}', "no operations"),
        ('{"swagger": "2.0", "info": {}, "paths": {"/a": {"get": {}}}}', "Swagger 2.0"),
        ('{"openapi": "2.5", "paths": {"/a": {"get": {}}}}', "unsupported OpenAPI version"),
        ('{"openapi": "4.0.0", "paths": {"/a": {"get": {}}}}', "unsupported OpenAPI version"),
        ('{"paths": {"/a": {"get": {}}}}', "unsupported OpenAPI version (missing)"),
        ('{"openapi": "3.0.0", "paths": ', "invalid JSON"),
        ("openapi: 3.0.0\npaths: [unclosed\n", "invalid YAML"),
        ("openapi: 3.0.0\n---\nopenapi: 3.1.0\n", "exactly one document"),
        ("openapi: 3.0.0\npaths: {/a: {get: !!python/object/apply:os.system [id]}}\n", "invalid YAML"),
        ("a: &a [*a]\n", "recursive"),
        (BILLION_LAUGHS, "expands to more than"),
        ("x: " + "[" * 150 + "]" * 150, f"deeper than {MAX_NESTING_DEPTH}"),
        ('{"a":' * 5000 + "1" + "}" * 5000, f"deeper than {MAX_NESTING_DEPTH}"),
        ('{"a":' * 200_000 + "1" + "}" * 200_000, f"deeper than {MAX_NESTING_DEPTH}"),
        ('{"openapi": "3.0.0", "paths": {"/a": {"$ref": "#/nowhere"}}}', "does not resolve"),
    ],
)
async def test_bad_documents_are_422_with_a_clear_message(
    client: httpx.AsyncClient, seeded: Seed, spec_text: str, message: str
) -> None:
    response = await client.post("/v1/endpoints/import", json=body(spec_text=spec_text), headers=bearer(seeded))
    assert response.status_code == 422, response.text
    assert message in response.json()["detail"]


def test_many_yaml_aliases_are_refused_before_construction() -> None:
    spec = "openapi: 3.0.0\npaths: {/a: {get: {}}}\nanchor: &x 1\nmany: [" + ", ".join(["*x"] * 1001) + "]\n"
    with pytest.raises(OpenApiImportError, match="more than 1000 aliases"):
        load_document(spec)


@pytest.mark.parametrize(
    "ref",
    [
        "http://evil.example/schema.json",
        "https://evil.example/schema.json#/Pet",
        "file:///etc/passwd",
        "other.yaml#/components/schemas/Pet",
        "//evil.example/x",
    ],
)
async def test_remote_refs_are_rejected_anywhere(client: httpx.AsyncClient, seeded: Seed, ref: str) -> None:
    doc = json.loads(json.dumps(PETSTORE_30))
    doc["components"]["schemas"]["Pet"]["properties"]["owner"] = {"$ref": ref}
    response = await client.post("/v1/endpoints/import", json=body(spec_text=json.dumps(doc)), headers=bearer(seeded))
    assert response.status_code == 422
    assert "only local '#/...' references" in response.json()["detail"]
    dynamic = {"openapi": "3.1.0", "paths": {"/a": {"get": {"x": {"$dynamicRef": ref}}}}}
    response = await client.post(
        "/v1/endpoints/import", json=body(spec_text=json.dumps(dynamic)), headers=bearer(seeded)
    )
    assert response.status_code == 422


async def test_document_size_limit(client: httpx.AsyncClient, seeded: Seed) -> None:
    filler = "x" * (OPENAPI_MAX_DOCUMENT_BYTES - 200)
    fits = {"openapi": "3.0.0", "info": {"description": filler}, "paths": {"/a": {"get": {}}}}
    assert len(json.dumps(fits).encode()) <= OPENAPI_MAX_DOCUMENT_BYTES
    ok = await client.post("/v1/endpoints/import", json=body(spec_text=json.dumps(fits)), headers=bearer(seeded))
    assert ok.status_code == 200, ok.text[:300]
    fits["info"]["description"] = filler + "x" * 400
    too_big = await client.post("/v1/endpoints/import", json=body(spec_text=json.dumps(fits)), headers=bearer(seeded))
    assert too_big.status_code == 413
    huge_body = b'{"mode": "preview", "spec_text": "' + b"y" * (5 * 1024 * 1024) + b'"}'
    raw = await client.post(
        "/v1/endpoints/import", content=huge_body, headers={**bearer(seeded), "Content-Type": "application/json"}
    )
    assert raw.status_code == 413


async def test_operation_limit(client: httpx.AsyncClient, seeded: Seed) -> None:
    at_limit = {"openapi": "3.0.0", "paths": {f"/r{i}": {"get": {}} for i in range(OPENAPI_MAX_OPERATIONS)}}
    ok = await client.post("/v1/endpoints/import", json=body(spec_text=json.dumps(at_limit)), headers=bearer(seeded))
    assert ok.status_code == 200
    assert ok.json()["operation_count"] == OPENAPI_MAX_OPERATIONS
    over = {"openapi": "3.0.0", "paths": {f"/r{i}": {"get": {}} for i in range(OPENAPI_MAX_OPERATIONS + 1)}}
    response = await client.post("/v1/endpoints/import", json=body(spec_text=json.dumps(over)), headers=bearer(seeded))
    assert response.status_code == 422
    assert "more than 500 operations" in response.json()["detail"]


# ---------------------------------------------------------------- spec_url fetching and SSRF


async def test_spec_url_is_fetched_through_the_upstream_client(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old.yaml":
            return httpx.Response(301, headers={"Location": "/openapi.yaml"})
        return httpx.Response(200, text=PETSTORE_31_YAML, headers={"Content-Type": "application/yaml"})

    upstream.handler = handler
    result = await preview(client, seeded, spec_text=None, spec_url="https://specs.petstore.example/old.yaml")
    assert result.title == "Petstore YAML"
    assert [str(r.url) for r in upstream.requests] == [
        "https://specs.petstore.example/old.yaml",
        "https://specs.petstore.example/openapi.yaml",
    ]
    assert all("authorization" not in r.headers for r in upstream.requests)


@pytest.mark.parametrize(
    "spec_url",
    [
        "http://127.0.0.1/openapi.json",
        "http://localhost:8000/openapi.json",
        "http://10.1.2.3/openapi.json",
        "http://192.168.1.10/openapi.json",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/openapi.json",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://100.64.0.1/openapi.json",
        "file:///etc/passwd",
        "ftp://specs.example.com/openapi.json",
        "gopher://specs.example.com/",
    ],
)
async def test_spec_url_ssrf_targets_are_refused(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, spec_url: str
) -> None:
    response = await client.post(
        "/v1/endpoints/import", json=body(spec_text=None, spec_url=spec_url), headers=bearer(seeded)
    )
    assert response.status_code == 422
    assert upstream.requests == []


@pytest.mark.parametrize(
    "location",
    ["http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:6379/", "file:///etc/passwd", "http://[fd00::1]/"],
)
async def test_redirects_to_private_targets_are_refused(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream, location: str
) -> None:
    upstream.handler = lambda request: httpx.Response(302, headers={"Location": location})
    response = await client.post(
        "/v1/endpoints/import", json=body(spec_text=None, spec_url=SPEC_URL), headers=bearer(seeded)
    )
    assert response.status_code == 422
    assert "spec_url" in response.json()["detail"]
    assert len(upstream.requests) == 1


async def test_redirect_loops_and_http_errors_are_422(
    client: httpx.AsyncClient, seeded: Seed, upstream: Upstream
) -> None:
    upstream.handler = lambda request: httpx.Response(302, headers={"Location": SPEC_URL})
    loop = await client.post(
        "/v1/endpoints/import", json=body(spec_text=None, spec_url=SPEC_URL), headers=bearer(seeded)
    )
    assert loop.status_code == 422 and "redirected more than" in loop.json()["detail"]
    upstream.handler = lambda request: httpx.Response(404, text="missing")
    missing = await client.post(
        "/v1/endpoints/import", json=body(spec_text=None, spec_url=SPEC_URL), headers=bearer(seeded)
    )
    assert missing.status_code == 422 and "HTTP 404" in missing.json()["detail"]
    upstream.handler = lambda request: httpx.Response(200, content=b"\xff\xfe\x00bad")
    binary = await client.post(
        "/v1/endpoints/import", json=body(spec_text=None, spec_url=SPEC_URL), headers=bearer(seeded)
    )
    assert binary.status_code == 422 and "UTF-8" in binary.json()["detail"]


async def test_spec_url_size_cap_while_streaming(client: httpx.AsyncClient, seeded: Seed, upstream: Upstream) -> None:
    async def endless() -> AsyncIterator[bytes]:
        for _ in range(64):
            yield b"#" * 65536

    upstream.handler = lambda request: httpx.Response(200, content=endless())
    streamed = await client.post(
        "/v1/endpoints/import", json=body(spec_text=None, spec_url=SPEC_URL), headers=bearer(seeded)
    )
    assert streamed.status_code == 413
    upstream.handler = lambda request: httpx.Response(
        200, content=b"{}", headers={"Content-Length": str(OPENAPI_MAX_DOCUMENT_BYTES + 1)}
    )
    declared = await client.post(
        "/v1/endpoints/import", json=body(spec_text=None, spec_url=SPEC_URL), headers=bearer(seeded)
    )
    assert declared.status_code == 413


async def test_spec_url_names_resolving_to_private_ips_are_refused_by_the_guard(
    app: FastAPI, client: httpx.AsyncClient, seeded: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def getaddrinfo(host: str, port: int) -> list[tuple[Any, Any, Any, Any, tuple[Any, ...]]]:
        answers = {"specs.rebind.example": "10.0.0.5", "metadata.rebind.example": "169.254.169.254"}
        if host not in answers:
            raise socket.gaierror(socket.EAI_NONAME, "unknown host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[host], port))]

    monkeypatch.setattr(security, "_getaddrinfo", getaddrinfo)
    mocked = app.state.http_client
    guarded = build_upstream_client()
    app.state.http_client = guarded
    try:
        for host in ("specs.rebind.example", "metadata.rebind.example"):
            response = await client.post(
                "/v1/endpoints/import",
                json=body(spec_text=None, spec_url=f"http://{host}/openapi.json"),
                headers=bearer(seeded),
            )
            assert response.status_code == 422
            assert "non-public address" in response.json()["detail"]
    finally:
        app.state.http_client = mocked
        await guarded.aclose()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://127.0.0.1:8080/api",
        "http://localhost/api",
        "http://10.0.0.1/",
        "http://169.254.169.254/",
        "http://[::1]/",
        "http://service.internal/",
        "ftp://api.example.com/",
        "not a url",
    ],
)
async def test_private_base_urls_are_refused(client: httpx.AsyncClient, seeded: Seed, base_url: str) -> None:
    response = await client.post("/v1/endpoints/import", json=body(upstream_base_url=base_url), headers=bearer(seeded))
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"][:2] == ["body", "upstream_base_url"]


# ---------------------------------------------------------------- schema guarantees


async def test_explicit_indexes_on_slug_and_tx_id_exist(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT indexname, indexdef FROM pg_indexes "
                    "WHERE indexname IN ('ix_endpoints_slug', 'ix_settlements_tx_id')"
                )
            )
        ).all()
    found = {name: definition for name, definition in rows}
    assert set(found) == {"ix_endpoints_slug", "ix_settlements_tx_id"}
    assert "UNIQUE" in found["ix_endpoints_slug"] and "(slug)" in found["ix_endpoints_slug"]
    assert "UNIQUE" in found["ix_settlements_tx_id"] and "(tx_id)" in found["ix_settlements_tx_id"]
