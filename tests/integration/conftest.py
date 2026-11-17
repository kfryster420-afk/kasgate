"""Shared fixtures for the KasGate integration suite.

Live tests need a running gateway and, for payments, a funded testnet key:

    KASGATE_GATEWAY_URL        default http://localhost:8000
    KASGATE_TEST_PAYER_KEY     hex private key of a funded kaspa:testnet-10 address (skips funded tests if unset)
    KASGATE_TEST_RPC_URL       optional wRPC URL used by the test client (default: public resolver)
    KASGATE_TEST_UPSTREAM_URL  public upstream the gateway proxies to (default https://httpbin.org/anything)
    KASGATE_TEST_RPC_DOWN=1    set only when the gateway runs with an unreachable node: enables the 503 test

Offline tests (test_offline_*.py) import the gateway's own pydantic models and verifier and need no services.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path
from typing import Any

import httpx
import kaspa
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_DIR = REPO_ROOT / "apps" / "gateway"
SDK_DIR = REPO_ROOT / "packages" / "buyer-sdk"

OFFLINE_PAYER_KEY = "b7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfef"
OFFLINE_TREASURY_KEY = "d7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfef"


def address_for_key(key_hex: str) -> str:
    return kaspa.PrivateKey(key_hex).to_address("testnet").to_string()


os.environ.setdefault("KASPA_NETWORK", "kaspa:testnet-10")
os.environ.setdefault("KASGATE_TREASURY_ADDRESS", address_for_key(OFFLINE_TREASURY_KEY))
os.environ.setdefault("ENCRYPTION_KEY", "A" * 43 + "=")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
for path in (GATEWAY_DIR, SDK_DIR, Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app.schemas import TAX_CSV_COLUMNS, min_price_sompi  # noqa: E402,F401
from kasgate_client import PaymentRequired  # noqa: E402

GATEWAY_URL = os.environ.get("KASGATE_GATEWAY_URL", "http://localhost:8000").rstrip("/")
UPSTREAM_URL = os.environ.get("KASGATE_TEST_UPSTREAM_URL", "https://httpbin.org/anything")
RPC_URL = os.environ.get("KASGATE_TEST_RPC_URL") or None


# ---------------------------------------------------------------- live fixtures


@pytest.fixture(scope="session")
def gateway_url() -> str:
    try:
        response = httpx.get(f"{GATEWAY_URL}/healthz", timeout=5)
    except httpx.HTTPError as exc:
        pytest.skip(f"gateway not reachable at {GATEWAY_URL} ({type(exc).__name__}); start it or set KASGATE_GATEWAY_URL")
    if response.status_code != 200:
        pytest.skip(f"gateway at {GATEWAY_URL} is unhealthy: GET /healthz -> {response.status_code}")
    return GATEWAY_URL


@pytest.fixture(scope="session")
def health(gateway_url: str) -> dict[str, Any]:
    return httpx.get(f"{gateway_url}/healthz", timeout=5).json()


@pytest.fixture(scope="session")
def network(health: dict[str, Any]) -> str:
    if health["network"] != "kaspa:testnet-10":
        pytest.skip(f"live payment tests only run against kaspa:testnet-10, gateway reports {health['network']}")
    return health["network"]


@pytest.fixture(scope="session")
def directory(gateway_url: str) -> dict[str, Any]:
    response = httpx.get(f"{gateway_url}/v1/directory", timeout=10)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture(scope="session")
def min_price(directory: dict[str, Any]) -> int:
    """Smallest legal price for the gateway's floor, derived from the public manifest."""
    return min_price_sompi(int(directory["min_output_sompi"]))


@pytest.fixture(scope="session")
def payer_key(network: str, health: dict[str, Any]) -> str:
    key = os.environ.get("KASGATE_TEST_PAYER_KEY", "").strip()
    if not key:
        pytest.skip("KASGATE_TEST_PAYER_KEY is not set: export the hex private key of a funded kaspa:testnet-10 address")
    if not health.get("kaspa_rpc"):
        pytest.skip("gateway reports kaspa_rpc=false: payments cannot be submitted")
    return key


@pytest.fixture(scope="session")
def payer_address(payer_key: str) -> str:
    return address_for_key(payer_key)


@pytest.fixture(scope="session")
def pool(payer_key: str, payer_address: str, network: str):
    from txforge import UtxoPool, fetch_utxos_sync

    if not fetch_utxos_sync(payer_address, network, RPC_URL):
        pytest.skip(f"payer {payer_address} has no UTXOs: fund it from a kaspa testnet-10 faucet")
    return UtxoPool(payer_address, network, RPC_URL)


@pytest.fixture(scope="session")
def forge(pool, payer_key: str, network: str):
    """forge(outputs) -> ForgedPayment signed by the funded payer; spends are tracked only via forge.commit()."""
    from txforge import forge_payment

    def _forge(outputs: list[tuple[str, int]], key: str | None = None, **kwargs: Any):
        needed = sum(amount for _, amount in outputs) + 5_000_000
        return forge_payment(
            private_key_hex=key or payer_key,
            network=network,
            utxos=pool.available(needed),
            outputs=outputs,
            **kwargs,
        )

    _forge.commit = pool.mark_spent  # type: ignore[attr-defined]
    return _forge


@pytest.fixture(scope="session")
def merchant_key() -> str:
    return secrets.token_hex(32)


@pytest.fixture(scope="session")
def merchant_address(merchant_key: str) -> str:
    return address_for_key(merchant_key)


@pytest.fixture(scope="session")
def merchant(gateway_url: str, network: str, merchant_address: str) -> dict[str, Any]:
    response = httpx.post(
        f"{gateway_url}/v1/merchants",
        json={"kaspa_address": merchant_address, "display_name": f"qa-merchant-{secrets.token_hex(3)}"},
        timeout=10,
    )
    if response.status_code == 429:
        pytest.skip(
            "POST /v1/merchants is rate limited: run the gateway with RATE_LIMIT_ENABLED=false for this suite "
            f"(or wait Retry-After={response.headers.get('retry-after')}s / delete the kasgate:rl:merchants:* Redis keys)"
        )
    assert response.status_code == 201, response.text
    body = response.json()
    return {"id": body["merchant"]["id"], "api_key": body["api_key"], "address": merchant_address}


@pytest.fixture(scope="session")
def auth(merchant: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {merchant['api_key']}"}


def create_endpoint(
    gateway_url: str, auth: dict[str, str], price_sompi: int, *, methods: tuple[str, ...] = ("GET", "POST")
) -> dict[str, Any]:
    response = httpx.post(
        f"{gateway_url}/v1/endpoints",
        headers=auth,
        json={
            "name": f"qa-endpoint-{secrets.token_hex(3)}",
            "upstream_url": UPSTREAM_URL,
            "price_sompi": price_sompi,
            "allowed_methods": list(methods),
            "is_public_listed": False,
        },
        timeout=10,
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture(scope="session")
def endpoint(gateway_url: str, auth: dict[str, str], min_price: int) -> dict[str, Any]:
    return create_endpoint(gateway_url, auth, min_price)


@pytest.fixture(scope="session")
def proxy_url(gateway_url: str, endpoint: dict[str, Any]) -> str:
    return f"{gateway_url}/p/{endpoint['slug']}"


@pytest.fixture(scope="session")
def quote(gateway_url: str, endpoint: dict[str, Any]) -> PaymentRequired:
    response = httpx.get(f"{gateway_url}/v1/quote/{endpoint['slug']}", timeout=10)
    assert response.status_code == 200, response.text
    return PaymentRequired.model_validate(response.json())


def merchant_fee_outputs(required: PaymentRequired) -> list[tuple[str, int]]:
    return [(required.payTo, required.merchant_sompi), (required.platformFee.feeTo, required.fee_sompi)]
