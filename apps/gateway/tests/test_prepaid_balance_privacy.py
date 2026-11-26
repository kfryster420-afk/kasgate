"""05's L7: the public prepaid balance read no longer shows deposited/debited totals unless the buyer signs it."""

import time
from typing import Any

import httpx
import pytest
from kaspa import PrivateKey, sign_message

from app.schemas import (
    HEADER_BALANCE_SIGNATURE,
    HEADER_BALANCE_TIMESTAMP,
    PrepaidBalanceRead,
    build_prepaid_balance_read_message,
)
from app.security import auth_domain
from tests.conftest import OTHER_KEY, PAYER_ADDRESS, PAYER_KEY, Seed, bearer
from tests.test_prepaid import CREDIT_SOMPI, fund, shop  # noqa: F401


def signed_headers(
    *, key: PrivateKey = PAYER_KEY, slug: str = "cheap", timestamp: int | None = None, domain: str | None = None
) -> dict[str, str]:
    stamp = int(time.time()) if timestamp is None else timestamp
    message = build_prepaid_balance_read_message(
        domain=auth_domain() if domain is None else domain, address=PAYER_ADDRESS, slug=slug, timestamp=stamp
    )
    return {HEADER_BALANCE_TIMESTAMP: str(stamp), HEADER_BALANCE_SIGNATURE: sign_message(message, key)}


async def read(client: httpx.AsyncClient, headers: dict[str, Any] | None = None) -> httpx.Response:
    return await client.get(
        "/v1/prepaid/balance", params={"slug": "cheap", "payer_address": PAYER_ADDRESS}, headers=headers or {}
    )


async def test_public_read_shows_the_balance_but_not_the_totals(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
) -> None:
    await fund(client)
    response = await read(client)
    assert response.status_code == 200, response.text
    body = PrepaidBalanceRead.model_validate(response.json())
    assert body.balance_sompi == str(CREDIT_SOMPI)
    assert body.deposited_sompi is None and body.debited_sompi is None
    assert HEADER_BALANCE_SIGNATURE in response.headers["Vary"]
    assert response.headers["Cache-Control"] == "no-store"


async def test_buyer_signed_read_shows_the_totals(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
) -> None:
    await fund(client)
    response = await read(client, signed_headers())
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["balance_sompi"], body["deposited_sompi"], body["debited_sompi"]) == (
        str(CREDIT_SOMPI),
        str(CREDIT_SOMPI),
        "0",
    )


async def test_signed_read_with_no_balance_yet_shows_zero_totals(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
) -> None:
    body = (await read(client, signed_headers())).json()
    assert (body["balance_sompi"], body["deposited_sompi"], body["debited_sompi"]) == ("0", "0", "0")


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param(lambda: signed_headers(key=OTHER_KEY), id="someone-elses-key"),
        pytest.param(lambda: signed_headers(slug="weather"), id="signed-for-another-slug"),
        pytest.param(lambda: signed_headers(domain="evil.example"), id="signed-for-another-domain"),
        pytest.param(lambda: signed_headers(timestamp=int(time.time()) - 301), id="stale"),
        pytest.param(lambda: signed_headers(timestamp=int(time.time()) + 301), id="future"),
        pytest.param(lambda: {HEADER_BALANCE_TIMESTAMP: str(int(time.time()))}, id="timestamp-only"),
        pytest.param(lambda: {HEADER_BALANCE_SIGNATURE: "ab" * 64}, id="signature-only"),
        pytest.param(
            lambda: {**signed_headers(), HEADER_BALANCE_TIMESTAMP: "\u00b2".encode()}, id="non-ascii-timestamp"
        ),
        pytest.param(lambda: {**signed_headers(), HEADER_BALANCE_SIGNATURE: "zz"}, id="malformed-signature"),
    ],
)
async def test_bad_balance_signatures_are_refused(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
    headers: Any,
) -> None:
    await fund(client)
    response = await read(client, headers())
    assert response.status_code == 401, response.text
    assert response.json()["error"] == "invalid_balance_signature"
    assert str(CREDIT_SOMPI) not in response.text


async def test_merchant_ledger_still_carries_the_totals(
    client: httpx.AsyncClient,
    shop: Seed,  # noqa: F811
) -> None:
    await fund(client)
    [row] = (await client.get("/v1/prepaid/balances", headers=bearer(shop.api_key))).json()
    assert (row["deposited_sompi"], row["debited_sompi"]) == (str(CREDIT_SOMPI), "0")


def test_balance_read_message_is_bound_to_its_own_purpose() -> None:
    message = build_prepaid_balance_read_message(domain="gw.example", address="kaspa:q", slug="s-1", timestamp=7)
    assert message == (
        "KasGate\nDomain: gw.example\nPurpose: kasgate:v1:prepaid-balance-read\nAddress: kaspa:q\nSlug: s-1\n"
        "Timestamp: 7"
    )
