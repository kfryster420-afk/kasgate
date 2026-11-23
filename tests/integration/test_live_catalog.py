"""Live gateway tests that need no funds: 402 challenge, registration rules, tax CSV, directory and quote."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app.schemas import TAX_CSV_COLUMNS
from kasgate_client import PaymentRequired, compute_fee_split

from conftest import create_endpoint

pytestmark = pytest.mark.live


def decode_challenge(response: httpx.Response) -> PaymentRequired:
    assert "PAYMENT-REQUIRED" in response.headers, response.text
    return PaymentRequired.from_header(response.headers["PAYMENT-REQUIRED"])


def test_unpaid_call_gets_402_with_a_decodable_challenge(proxy_url, endpoint, network, merchant_address, min_price) -> None:
    response = httpx.get(proxy_url, timeout=15)
    assert response.status_code == 402
    required = decode_challenge(response)
    merchant, fee = compute_fee_split(min_price)
    assert required.x402Version == 2 and required.scheme == "kaspa-exact-v1" and required.asset == "KAS"
    assert required.network == network
    assert required.total_sompi == min_price == endpoint["price_sompi"]
    assert (required.merchant_sompi, required.fee_sompi) == (merchant, fee)
    assert required.payTo == merchant_address
    body = response.json()
    assert body["error"] == "payment_required"
    assert body["accepts"]["amount"] == str(min_price)


def test_quote_endpoint_matches_the_402_challenge(gateway_url, proxy_url, endpoint) -> None:
    challenge = decode_challenge(httpx.get(proxy_url, timeout=15))
    quote = httpx.get(f"{gateway_url}/v1/quote/{endpoint['slug']}", timeout=10)
    assert quote.status_code == 200
    quoted = PaymentRequired.model_validate(quote.json())
    assert quoted.nonce != challenge.nonce, "every quote and every 402 mints its own payment nonce"
    volatile = {"nonce", "expiresAt"}
    assert quoted.model_dump(exclude=volatile) == challenge.model_dump(exclude=volatile)


def test_unknown_slug_is_404_and_bad_method_is_405(gateway_url, proxy_url) -> None:
    missing = httpx.get(f"{gateway_url}/p/does-not-exist-{datetime.now(UTC).timestamp():.0f}", timeout=10)
    assert missing.status_code == 404 and missing.json()["error"] == "endpoint_not_found"
    wrong_method = httpx.delete(proxy_url, timeout=10)
    assert wrong_method.status_code == 405 and wrong_method.json()["error"] == "method_not_allowed"


def test_garbage_payment_signature_is_402_invalid_payment_header(proxy_url) -> None:
    for value in ("not-base64!!", "e30=", "bnVsbA==", "A" * 64):
        response = httpx.get(proxy_url, headers={"PAYMENT-SIGNATURE": value}, timeout=15)
        assert response.status_code == 402, (value, response.status_code, response.text)
        assert response.json()["error"] in {"invalid_payment_header", "wrong_network"}
        assert "PAYMENT-REQUIRED" in response.headers


def test_oversized_payment_signature_never_causes_a_server_error(proxy_url) -> None:
    for size in (20_000, 70_000, 140_000, 1_000_000):
        try:
            response = httpx.get(proxy_url, headers={"PAYMENT-SIGNATURE": "A" * size}, timeout=15)
        except httpx.HTTPError:
            continue  # connection closed by the HTTP server is an acceptable refusal
        assert response.status_code < 500, (size, response.status_code)
        assert response.status_code in {400, 402, 413, 431}, (size, response.status_code)


def test_price_below_the_floor_is_422(gateway_url, auth, min_price) -> None:
    for price in (min_price - 1, 40_000_000, 1, 0, -5):
        response = httpx.post(
            f"{gateway_url}/v1/endpoints",
            headers=auth,
            json={"name": "too cheap", "upstream_url": "https://example.com/api", "price_sompi": price},
            timeout=10,
        )
        assert response.status_code == 422, (price, response.text)


def test_price_exactly_at_the_floor_is_accepted(gateway_url, auth, min_price) -> None:
    created = create_endpoint(gateway_url, auth, min_price)
    assert created["price_sompi"] == min_price


def test_endpoint_update_cannot_go_below_the_floor(gateway_url, auth, endpoint, min_price) -> None:
    response = httpx.patch(
        f"{gateway_url}/v1/endpoints/{endpoint['id']}", headers=auth, json={"price_sompi": min_price - 1}, timeout=10
    )
    assert response.status_code == 422


@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "http://localhost/x", "http://169.254.169.254/latest", "http://10.1.2.3/"])
def test_private_upstream_urls_are_rejected_with_422(gateway_url, auth, min_price, url) -> None:
    response = httpx.post(
        f"{gateway_url}/v1/endpoints",
        headers=auth,
        json={"name": "ssrf", "upstream_url": url, "price_sompi": min_price},
        timeout=10,
    )
    assert response.status_code == 422


def test_endpoints_require_the_merchant_api_key(gateway_url, min_price) -> None:
    payload = {"name": "anon", "upstream_url": "https://example.com", "price_sompi": min_price}
    assert httpx.post(f"{gateway_url}/v1/endpoints", json=payload, timeout=10).status_code in {401, 403}
    bad = httpx.post(
        f"{gateway_url}/v1/endpoints", headers={"Authorization": "Bearer kg_wrong"}, json=payload, timeout=10
    )
    assert bad.status_code in {401, 403}


def test_merchant_cannot_register_a_mainnet_address_on_testnet(gateway_url, network) -> None:
    response = httpx.post(
        f"{gateway_url}/v1/merchants",
        json={"kaspa_address": "kaspa:qr0lr4ml9fn3chekrqmjdkergxl93l4wrk3dankcgvjq776s9wn9jkdskewva", "display_name": "x"},
        timeout=10,
    )
    assert response.status_code == 422


def test_tax_csv_header_equals_tax_csv_columns(gateway_url, auth) -> None:
    end = datetime.now(UTC)
    response = httpx.get(
        f"{gateway_url}/v1/tax-report.csv",
        headers=auth,
        params={"start": (end - timedelta(days=1)).isoformat(), "end": end.isoformat()},
        timeout=15,
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    header = next(csv.reader(io.StringIO(response.text)))
    assert tuple(header) == TAX_CSV_COLUMNS


def test_directory_manifest_is_consistent_with_the_contract(directory, network) -> None:
    assert directory["x402_version"] == 2 and directory["scheme"] == "kaspa-exact-v1" and directory["asset"] == "KAS"
    assert directory["network"] == network
    assert directory["platform_fee_bps"] == 150
    for entry in directory["endpoints"]:
        assert entry["pay_to"].startswith("kaspatest:")
        assert entry["price_sompi"] > 0


def test_agent_catalog_matches_well_known_and_leaks_no_private_fields(gateway_url, network) -> None:
    catalog = httpx.get(f"{gateway_url}/v1/catalog", timeout=10)
    well_known = httpx.get(f"{gateway_url}/.well-known/x402", timeout=10)
    assert catalog.status_code == 200, catalog.text
    assert well_known.status_code == 200, well_known.text
    assert catalog.json() == well_known.json()
    body = catalog.json()
    assert body["x402_version"] == 2 and body["scheme"] == "kaspa-exact-v1" and body["asset"] == "KAS"
    assert body["network"] == network
    assert "etag" in catalog.headers and "max-age=" in catalog.headers.get("cache-control", "")
    not_modified = httpx.get(f"{gateway_url}/v1/catalog", headers={"If-None-Match": catalog.headers["etag"]}, timeout=10)
    assert not_modified.status_code == 304
    private_keys = {"upstream_url", "upstream_headers", "email", "api_key", "api_key_hash", "pay_to"}
    for key in private_keys:
        assert key not in body
    for item in body["items"]:
        assert set(item) >= {"slug", "name", "description", "methods", "price_sompi", "network", "schemes", "paid_url"}
        assert "input_schema" in item
        assert item["network"] == network
        assert "kaspa-exact-v1" in item["schemes"]
        assert not (private_keys & set(item))


def test_llms_txt_is_served_on_the_gateway(gateway_url) -> None:
    response = httpx.get(f"{gateway_url}/llms.txt", timeout=10)
    assert response.status_code == 200
    text = response.text
    for needle in ("PAYMENT-REQUIRED", "PAYMENT-SIGNATURE", "PAYMENT-RESPONSE", "/v1/catalog", "0.6 KAS", "kasgate-mcp"):
        assert needle in text
