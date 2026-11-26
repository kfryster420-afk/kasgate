"""CONTRACT v2.11 prepaid credit: on-chain split deposits, signed single-use debits, refunds, prepaid-only endpoints."""

import asyncio
import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from kaspa import Address, PrivateKey, sign_message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.kaspa_engine import KaspaEngine
from app.models import (
    Endpoint,
    PrepaidBalance,
    PrepaidDebit,
    PrepaidDeposit,
    PriceCurrency,
    Settlement,
)
from app.proxy import (
    lock_key,
    payer_public_key,
    prepaid_challenge_key,
    prepaid_deposit_key,
    prepaid_nonce_lock_key,
)
from app.schemas import (
    HEADER_K402_PAYMENT,
    HEADER_PAYMENT_REQUIRED,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
    HEADER_PREPAID_DEBIT,
    HEADER_PREPAID_RECEIPT,
    PREPAID_EMPTY_BODY_HASH,
    PaymentResponse,
    PrepaidChallenge,
    PrepaidDebitHeader,
    PrepaidDepositQuote,
    PrepaidDepositReceipt,
    PrepaidReceipt,
    build_prepaid_debit_message,
    compute_fee_split,
    min_price_sompi,
    prepaid_body_hash,
    usd_micros_to_prepaid_sompi,
    usd_micros_to_sompi,
)
from tests.conftest import (
    MERCHANT_ADDRESS,
    OTHER_ADDRESS,
    OTHER_KEY,
    PAYER_ADDRESS,
    PAYER_KEY,
    PRICE_SOMPI,
    Seed,
    StubEngine,
    StubOracle,
    Upstream,
    bearer,
    fetch_challenge,
    make_signature_header,
    seed_endpoint,
)

CHEAP_SOMPI = 1_000  # far below the 0.6 KAS on-chain minimum
DEPOSIT_SOMPI = 60_000_000  # the minimum deposit: fee 20_000_000 (0.2 KAS floor), credit 40_000_000
CREDIT_SOMPI = 40_000_000
SECOND_MERCHANT_ADDRESS = PrivateKey("07" * 32).to_address("testnet").to_string()


# ---------------------------------------------------------------- helpers


async def add_endpoint(
    maker: async_sessionmaker[AsyncSession],
    merchant_id: uuid.UUID,
    slug: str,
    *,
    price_sompi: int | None = CHEAP_SOMPI,
    prepaid_only: bool = True,
    price_currency: PriceCurrency = PriceCurrency.KAS,
    price_usd_micros: int | None = None,
    methods: list[str] | None = None,
) -> uuid.UUID:
    async with maker() as session:
        endpoint = Endpoint(
            merchant_id=merchant_id,
            slug=slug,
            name="Cheap API",
            upstream_url="https://api.upstream.test/cheap",
            upstream_headers_encrypted=None,
            price_currency=price_currency,
            price_sompi=price_sompi if price_currency is PriceCurrency.KAS else None,
            price_usd_micros=price_usd_micros if price_currency is PriceCurrency.USD else None,
            allowed_methods=methods or ["GET", "POST"],
            prepaid_only=prepaid_only,
        )
        session.add(endpoint)
        await session.commit()
        return endpoint.id


async def quote_deposit(
    client: httpx.AsyncClient, amount: int = DEPOSIT_SOMPI, *, slug: str = "cheap", payer: str = PAYER_ADDRESS
) -> PrepaidDepositQuote:
    response = await client.post(
        "/v1/prepaid/deposits/quote", json={"slug": slug, "payer_address": payer, "amount_sompi": amount}
    )
    assert response.status_code == 201, response.text
    return PrepaidDepositQuote.model_validate(response.json())


async def post_deposit(
    client: httpx.AsyncClient,
    quote: PrepaidDepositQuote,
    *,
    tx_id: str | None = None,
    payer_key: PrivateKey = PAYER_KEY,
) -> httpx.Response:
    header = make_signature_header(tx_id, quote.required.nonce, payer_key=payer_key)
    return await client.post("/v1/prepaid/deposits", headers={HEADER_PAYMENT_SIGNATURE: header})


async def fund(client: httpx.AsyncClient, amount: int = DEPOSIT_SOMPI, *, slug: str = "cheap") -> PrepaidDepositReceipt:
    response = await post_deposit(client, await quote_deposit(client, amount, slug=slug))
    assert response.status_code == 201, response.text
    return PrepaidDepositReceipt.model_validate(response.json())


async def request_challenge(
    client: httpx.AsyncClient,
    *,
    slug: str = "cheap",
    path: str | None = None,
    method: str = "GET",
    body: bytes = b"",
    payer: str = PAYER_ADDRESS,
) -> httpx.Response:
    return await client.post(
        "/v1/prepaid/challenge",
        json={
            "slug": slug,
            "payer_address": payer,
            "method": method,
            "path": path if path is not None else f"/p/{slug}",
            "body_hash": prepaid_body_hash(body),
        },
    )


async def get_debit_challenge(client: httpx.AsyncClient, **kwargs: Any) -> PrepaidChallenge:
    response = await request_challenge(client, **kwargs)
    assert response.status_code == 201, response.text
    return PrepaidChallenge.model_validate(response.json())


def debit_header(challenge: PrepaidChallenge, key: PrivateKey = PAYER_KEY, *, message: str | None = None) -> str:
    signature = sign_message(message if message is not None else challenge.message, key)
    return PrepaidDebitHeader(nonce=challenge.nonce, signature=signature).to_header()


async def prepaid_call(
    client: httpx.AsyncClient, *, slug: str = "cheap", path: str | None = None, method: str = "GET", body: bytes = b""
) -> httpx.Response:
    target = path if path is not None else f"/p/{slug}"
    challenge = await get_debit_challenge(client, slug=slug, path=target, method=method, body=body)
    return await client.request(
        method, target, content=body or None, headers={HEADER_PREPAID_DEBIT: debit_header(challenge)}
    )


async def balance_row(maker: async_sessionmaker[AsyncSession], merchant_id: uuid.UUID) -> PrepaidBalance | None:
    async with maker() as session:
        return (
            await session.execute(select(PrepaidBalance).where(PrepaidBalance.merchant_id == merchant_id))
        ).scalar_one_or_none()


async def count(maker: async_sessionmaker[AsyncSession], model: Any) -> int:
    async with maker() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


def receipt_of(response: httpx.Response) -> PrepaidReceipt:
    return PrepaidReceipt.model_validate_json(base64.b64decode(response.headers[HEADER_PREPAID_RECEIPT]))


@pytest.fixture
async def shop(sessionmaker: async_sessionmaker[AsyncSession]) -> Seed:
    """Merchant with `weather` (20 KAS, on-chain + prepaid) and `cheap` (1_000 sompi, prepaid-only)."""
    seed = await seed_endpoint(sessionmaker)
    await add_endpoint(sessionmaker, seed.merchant_id, "cheap")
    return seed


# ---------------------------------------------------------------- schemas


def test_debit_message_is_the_multi_line_challenge_with_a_prepaid_purpose() -> None:
    merchant_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    expires = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)
    message = build_prepaid_debit_message(
        domain="gateway.test",
        address=PAYER_ADDRESS,
        merchant_id=merchant_id,
        method="POST",
        path="/p/cheap/x?y=1",
        body_hash="a" * 64,
        amount_sompi=1000,
        nonce="1" * 32,
        expires_at=expires,
    )
    assert message.split("\n") == [
        "KasGate",
        "Domain: gateway.test",
        "Purpose: kasgate:v1:prepaid-debit",
        f"Address: {PAYER_ADDRESS}",
        f"Merchant: {merchant_id}",
        "Method: POST",
        "Path: /p/cheap/x?y=1",
        f"Body-Hash: {'a' * 64}",
        "Amount: 1000",
        f"Nonce: {'1' * 32}",
        "Expires: 2026-11-26T10:00:00+00:00",
    ]
    assert prepaid_body_hash(b"") == PREPAID_EMPTY_BODY_HASH == "-"
    assert prepaid_body_hash(b"{}") == "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"


def test_debit_header_normalises_base64_signatures_and_rejects_garbage() -> None:
    raw = bytes(range(64))
    header = base64.b64encode(json.dumps({"nonce": "2" * 32, "signature": base64.b64encode(raw).decode()}).encode())
    assert PrepaidDebitHeader.from_header(header.decode()).signature == raw.hex()
    for bad in (
        {"nonce": "2" * 31, "signature": "a" * 128},
        {"nonce": "2" * 32, "signature": "a" * 127},
        {"nonce": "2" * 32, "signature": "a" * 128, "extra": 1},
    ):
        with pytest.raises(ValueError):
            PrepaidDebitHeader.from_header(base64.b64encode(json.dumps(bad).encode()).decode())
    with pytest.raises(ValueError):
        PrepaidDebitHeader.from_header("A" * 600)


def test_prepaid_usd_conversion_has_no_on_chain_floor() -> None:
    rate = Decimal("0.125")
    # $0.0001 at $0.125/KAS is 0.0008 KAS = 80_000 sompi: far below 0.6 KAS, debited as is
    assert usd_micros_to_prepaid_sompi(100, rate) == 80_000
    assert usd_micros_to_sompi(100, rate) == min_price_sompi()  # the on-chain quote keeps its floor
    assert usd_micros_to_prepaid_sompi(1, Decimal("1000000000")) == 1  # never zero
    with pytest.raises(ValueError):
        usd_micros_to_prepaid_sompi(0, rate)
    with pytest.raises(ValueError):
        usd_micros_to_prepaid_sompi(1, Decimal(0))


def test_payer_public_key_accepts_only_p2pk_schnorr() -> None:
    assert payer_public_key(PAYER_ADDRESS) == PAYER_KEY.to_public_key().to_string()[2:]
    assert payer_public_key(PAYER_KEY.to_address_ecdsa("testnet").to_string()) is None
    assert payer_public_key("kaspatest:not-an-address") is None


# ---------------------------------------------------------------- endpoint CRUD


async def test_sub_minimum_price_requires_prepaid_only(client: httpx.AsyncClient, seeded: Seed) -> None:
    headers = bearer(seeded.api_key)
    body = {"name": "Tiny", "upstream_url": "https://api.upstream.test/t", "price_sompi": CHEAP_SOMPI}
    refused = await client.post("/v1/endpoints", json=body, headers=headers)
    assert refused.status_code == 422
    created = await client.post("/v1/endpoints", json={**body, "prepaid_only": True}, headers=headers)
    assert created.status_code == 201, created.text
    endpoint = created.json()
    assert endpoint["prepaid_only"] is True and endpoint["price_sompi"] == CHEAP_SOMPI

    # leaving prepaid-only while still priced below the minimum is refused, and nothing changes
    patch = await client.patch(f"/v1/endpoints/{endpoint['id']}", json={"prepaid_only": False}, headers=headers)
    assert patch.status_code == 422
    assert "prepaid_only" in patch.json()["detail"]
    again = await client.get(f"/v1/endpoints/{endpoint['id']}", headers=headers)
    assert again.json()["prepaid_only"] is True
    # ...but works together with an on-chain price
    ok = await client.patch(
        f"/v1/endpoints/{endpoint['id']}", json={"prepaid_only": False, "price_sompi": PRICE_SOMPI}, headers=headers
    )
    assert ok.status_code == 200 and ok.json()["prepaid_only"] is False
    # a sub-minimum price on an on-chain endpoint is refused; with prepaid_only=true in the same patch it is fine
    low = await client.patch(f"/v1/endpoints/{endpoint['id']}", json={"price_sompi": 5}, headers=headers)
    assert low.status_code == 422
    both = await client.patch(
        f"/v1/endpoints/{endpoint['id']}", json={"price_sompi": 5, "prepaid_only": True}, headers=headers
    )
    assert both.status_code == 200 and both.json()["price_sompi"] == 5
    nulled = await client.patch(f"/v1/endpoints/{endpoint['id']}", json={"prepaid_only": None}, headers=headers)
    assert nulled.status_code == 422


# ---------------------------------------------------------------- 402 for prepaid-only endpoints


async def test_prepaid_only_402_offers_only_prepaid_terms_and_mints_nothing(
    client: httpx.AsyncClient, shop: Seed, redis: Any, stub_engine: StubEngine
) -> None:
    response = await client.get("/p/cheap")
    assert response.status_code == 402
    assert HEADER_PAYMENT_REQUIRED not in response.headers
    body = response.json()
    assert body["error"] == "payment_required" and body["k402"] is None
    [offer] = body["accepts"]
    assert offer["scheme"] == "kasgate-prepaid-v1"
    assert offer["amount"] == str(CHEAP_SOMPI)
    assert offer["payTo"] == MERCHANT_ADDRESS
    assert offer["prepaidOnly"] is True
    assert offer["minDepositSompi"] == str(min_price_sompi()) == "60000000"
    assert offer["challengeUrl"] == "http://gateway.test/v1/prepaid/challenge"
    assert offer["depositQuoteUrl"] == "http://gateway.test/v1/prepaid/deposits/quote"
    assert offer["merchantId"] == str(shop.merchant_id)
    keys = await redis.keys("kasgate:*")
    assert not [k for k in keys if k.startswith(("kasgate:nonce:", "kasgate:k402:", "kasgate:exact:"))]

    quote = await client.get("/v1/quote/cheap")
    assert quote.status_code == 402 and quote.json()["accepts"][0]["scheme"] == "kasgate-prepaid-v1"

    # an on-chain header (e.g. for an offer from before the switch) is refused without touching the engine
    for headers in (
        {HEADER_PAYMENT_SIGNATURE: make_signature_header(None, "3" * 32)},
        {HEADER_K402_PAYMENT: f"kaspa-utxo {'a' * 64} p_0123456789abcdef"},
    ):
        refused = await client.get("/p/cheap", headers=headers)
        assert refused.status_code == 402
        assert refused.json()["error"] == "invalid_payment_header"
    assert stub_engine.verify_calls == [] and stub_engine.calls == []


async def test_on_chain_endpoint_402_is_unchanged(client: httpx.AsyncClient, shop: Seed) -> None:
    response = await client.get("/p/weather")
    assert response.status_code == 402
    assert HEADER_PAYMENT_REQUIRED in response.headers
    assert [entry["scheme"] for entry in response.json()["accepts"]] == ["kaspa-exact-v1", "kaspa-utxo", "exact"]


# ---------------------------------------------------------------- deposits


async def test_deposit_quote_uses_the_v1_split_and_its_own_namespace(
    client: httpx.AsyncClient, shop: Seed, redis: Any
) -> None:
    quote = await quote_deposit(client)
    split = compute_fee_split(DEPOSIT_SOMPI)
    assert (quote.deposit_sompi, quote.fee_sompi, quote.credit_sompi) == ("60000000", "20000000", "40000000")
    assert split.fee_sompi == 20_000_000 and split.merchant_sompi == CREDIT_SOMPI
    assert quote.required.amount == "60000000"
    assert quote.required.payTo == MERCHANT_ADDRESS
    assert quote.required.platformFee.feeTo == get_settings().kasgate_treasury_address
    assert quote.required.platformFee.feeAmount == "20000000"
    assert quote.balance_sompi == "0"
    assert await redis.get(prepaid_deposit_key(quote.required.nonce)) is not None
    assert await redis.get(f"kasgate:nonce:{quote.required.nonce}") is None

    big = await quote_deposit(client, 2_000_000_000)  # 1.5 % above the 0.2 KAS floor
    assert (big.fee_sompi, big.credit_sompi) == ("30000000", "1970000000")


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"slug": "cheap", "payer_address": PAYER_ADDRESS, "amount_sompi": DEPOSIT_SOMPI - 1}, 422),
        ({"slug": "cheap", "payer_address": PAYER_ADDRESS, "amount_sompi": 0}, 422),
        ({"slug": "cheap", "payer_address": PAYER_ADDRESS, "amount_sompi": "60000000"}, 422),  # strict int
        ({"slug": "cheap", "payer_address": "kaspa:" + PAYER_ADDRESS[10:], "amount_sompi": DEPOSIT_SOMPI}, 422),
        ({"slug": "cheap", "payer_address": PAYER_ADDRESS, "amount_sompi": DEPOSIT_SOMPI, "x": 1}, 422),
        ({"slug": "Bad Slug", "payer_address": PAYER_ADDRESS, "amount_sompi": DEPOSIT_SOMPI}, 422),
        ({"slug": "missing", "payer_address": PAYER_ADDRESS, "amount_sompi": DEPOSIT_SOMPI}, 404),
        (
            {
                "slug": "cheap",
                "payer_address": PAYER_KEY.to_address_ecdsa("testnet").to_string(),
                "amount_sompi": DEPOSIT_SOMPI,
            },
            400,
        ),
    ],
)
async def test_deposit_quote_validation(
    client: httpx.AsyncClient, shop: Seed, body: dict[str, Any], status: int
) -> None:
    response = await client.post("/v1/prepaid/deposits/quote", json=body)
    assert response.status_code == status, response.text


async def test_deposit_quote_enforces_the_balance_cap(
    client: httpx.AsyncClient, shop: Seed, override_settings: Any
) -> None:
    override_settings(prepaid_max_balance_sompi=60_000_000)
    await fund(client)  # balance 40_000_000; another 40_000_000 credit would exceed the cap
    refused = await client.post(
        "/v1/prepaid/deposits/quote",
        json={"slug": "cheap", "payer_address": PAYER_ADDRESS, "amount_sompi": DEPOSIT_SOMPI},
    )
    assert refused.status_code == 422 and "cap" in refused.json()["detail"]


async def test_deposit_credits_the_merchant_share_and_records_the_split(
    client: httpx.AsyncClient,
    shop: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Any,
    stub_engine: StubEngine,
) -> None:
    quote = await quote_deposit(client)
    tx_id = "ab" * 32
    response = await post_deposit(client, quote, tx_id=tx_id)
    assert response.status_code == 201, response.text
    receipt = PrepaidDepositReceipt.model_validate(response.json())
    assert receipt.tx_id == tx_id
    assert (receipt.deposit_sompi, receipt.fee_sompi, receipt.credit_sompi) == ("60000000", "20000000", "40000000")
    assert receipt.balance_sompi == "40000000"
    assert PaymentResponse.from_header(response.headers[HEADER_PAYMENT_RESPONSE]).txId == tx_id
    assert stub_engine.events == ["verify", "resolve_inputs", "settle"]
    assert await redis.get(prepaid_deposit_key(quote.required.nonce)) is None  # consumed
    assert await redis.get(lock_key(tx_id)) is not None  # the shared replay lock stays held

    async with sessionmaker() as session:
        settlement = (await session.execute(select(Settlement))).scalar_one()
        deposit = (await session.execute(select(PrepaidDeposit))).scalar_one()
    assert settlement.payment_scheme == "kaspa-exact-v1"
    assert settlement.merchant_amount_sompi == CREDIT_SOMPI and settlement.fee_amount_sompi == 20_000_000
    assert settlement.treasury_address == get_settings().kasgate_treasury_address
    assert settlement.payment_nonce == quote.required.nonce
    assert settlement.request_method == "POST" and settlement.request_path == "/v1/prepaid/deposits"
    assert settlement.usd_value_at_settlement == Decimal("0.07500000")  # 0.6 KAS at the stub's $0.125
    assert deposit.settlement_id == settlement.id and deposit.tx_id == tx_id
    balance = await balance_row(sessionmaker, shop.merchant_id)
    assert balance is not None
    assert (balance.balance_sompi, balance.deposited_sompi, balance.debited_sompi) == (CREDIT_SOMPI, CREDIT_SOMPI, 0)
    assert balance.payer_public_key == payer_public_key(PAYER_ADDRESS)

    # a top-up lands in the same running balance
    await fund(client, 2_000_000_000)
    balance = await balance_row(sessionmaker, shop.merchant_id)
    assert balance is not None and balance.balance_sompi == CREDIT_SOMPI + 1_970_000_000
    assert await count(sessionmaker, PrepaidBalance) == 1 and await count(sessionmaker, PrepaidDeposit) == 2

    public = await client.get("/v1/prepaid/balance", params={"slug": "weather", "payer_address": PAYER_ADDRESS})
    assert public.status_code == 200 and public.json()["balance_sompi"] == str(CREDIT_SOMPI + 1_970_000_000)


async def test_deposit_replay_is_idempotent_and_never_credits_twice(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    quote = await quote_deposit(client)
    header = make_signature_header("cd" * 32, quote.required.nonce)
    first = await client.post("/v1/prepaid/deposits", headers={HEADER_PAYMENT_SIGNATURE: header})
    again = await client.post("/v1/prepaid/deposits", headers={HEADER_PAYMENT_SIGNATURE: header})
    assert first.status_code == 201 and again.status_code == 200
    assert again.json() == first.json()
    assert stub_engine.calls == ["cd" * 32]
    other_payer = make_signature_header("cd" * 32, quote.required.nonce, payer_key=OTHER_KEY)
    assert (
        await client.post("/v1/prepaid/deposits", headers={HEADER_PAYMENT_SIGNATURE: other_payer})
    ).status_code == 409
    other_nonce = make_signature_header("cd" * 32, "9" * 32)
    assert (
        await client.post("/v1/prepaid/deposits", headers={HEADER_PAYMENT_SIGNATURE: other_nonce})
    ).status_code == 409
    balance = await balance_row(sessionmaker, shop.merchant_id)
    assert balance is not None and balance.balance_sompi == CREDIT_SOMPI


async def test_concurrent_deposit_of_one_tx_credits_once(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    stub_engine.delay = 0.05
    quote = await quote_deposit(client)
    header = make_signature_header("ef" * 32, quote.required.nonce)
    responses = await asyncio.gather(
        *(client.post("/v1/prepaid/deposits", headers={HEADER_PAYMENT_SIGNATURE: header}) for _ in range(8))
    )
    codes = sorted(r.status_code for r in responses)
    assert codes.count(201) == 1 and set(codes) <= {200, 201, 409}
    assert stub_engine.calls == ["ef" * 32]
    balance = await balance_row(sessionmaker, shop.merchant_id)
    assert balance is not None and balance.balance_sompi == CREDIT_SOMPI


async def test_deposit_and_call_nonces_never_cross(
    client: httpx.AsyncClient, shop: Seed, stub_engine: StubEngine
) -> None:
    deposit_quote = await quote_deposit(client, slug="weather")
    as_call = await client.get(
        "/p/weather", headers={HEADER_PAYMENT_SIGNATURE: make_signature_header(None, deposit_quote.required.nonce)}
    )
    assert as_call.status_code == 402 and as_call.json()["error"] == "payment_expired"
    call_challenge = await fetch_challenge(client, "weather")
    as_deposit = await client.post(
        "/v1/prepaid/deposits", headers={HEADER_PAYMENT_SIGNATURE: make_signature_header(None, call_challenge.nonce)}
    )
    assert as_deposit.status_code == 402 and as_deposit.json()["error"] == "payment_expired"
    assert stub_engine.calls == []


async def test_deposit_tx_already_locked_by_a_call_is_replayed(
    client: httpx.AsyncClient, shop: Seed, redis: Any, stub_engine: StubEngine
) -> None:
    quote = await quote_deposit(client)
    await redis.set(lock_key("12" * 32), "1")
    response = await post_deposit(client, quote, tx_id="12" * 32)
    assert response.status_code == 409 and response.json()["error"] == "payment_replayed"
    assert stub_engine.calls == []


async def test_deposit_failures_do_not_credit_and_release_the_locks(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], stub_engine: StubEngine
) -> None:
    missing = await client.post("/v1/prepaid/deposits")
    assert missing.status_code == 402 and missing.json()["error"] == "payment_required"
    garbage = await client.post("/v1/prepaid/deposits", headers={HEADER_PAYMENT_SIGNATURE: "%%%"})
    assert garbage.status_code == 402 and garbage.json()["error"] == "invalid_payment_header"
    unknown = await client.post(
        "/v1/prepaid/deposits", headers={HEADER_PAYMENT_SIGNATURE: make_signature_header(None, "4" * 32)}
    )
    assert unknown.status_code == 402 and unknown.json()["error"] == "payment_expired"

    quote = await quote_deposit(client)
    wrong_payer = await post_deposit(client, quote, payer_key=OTHER_KEY)
    assert wrong_payer.status_code == 402 and wrong_payer.json()["error"] == "invalid_payment_header"

    stub_engine.fail_verify("invalid_fee_split", "treasury output 1 is short by 1 sompi")
    split = await post_deposit(client, quote, tx_id="56" * 32)
    assert split.status_code == 402 and split.json()["error"] == "invalid_fee_split"
    assert HEADER_PAYMENT_REQUIRED in split.headers
    stub_engine.verify_error = None
    stub_engine.fail("tx_rejected")
    rejected = await post_deposit(client, quote, tx_id="56" * 32)
    assert rejected.status_code == 402 and rejected.json()["error"] == "tx_rejected"
    assert await count(sessionmaker, PrepaidBalance) == 0 and await count(sessionmaker, Settlement) == 0

    stub_engine.error = None  # locks were released: the same tx and nonce can still settle
    ok = await post_deposit(client, quote, tx_id="56" * 32)
    assert ok.status_code == 201, ok.text


async def test_deposit_is_checked_by_the_real_engine_outputs(
    app: Any, client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """The deposit runs the REAL pure verifier: output 0 merchant exact, output 1 treasury exact, payload nonce."""
    from tests.test_kaspa_engine import make_engine, signature_for, signed_transaction

    real, _client = make_engine()

    class PureEngine(StubEngine):
        def verify(self, signature: Any, required: Any) -> Any:
            self.events.append("verify")
            return real.verify(signature, required)

        async def resolve_inputs(self, signature: Any, tx: Any) -> object:
            self.events.append("resolve_inputs")
            return object()

        async def settle(self, tx: Any, verified: Any, required: Any) -> Any:
            self.events.append("settle")
            return verified

    app.state.engine = PureEngine()
    assert isinstance(real, KaspaEngine)
    quote = await quote_deposit(client)
    required = quote.required
    nonce = required.nonce
    from app.schemas import payment_payload

    merchant, treasury = Address(required.payTo), Address(required.platformFee.feeTo)
    change = 30 * 100_000_000 - DEPOSIT_SOMPI - 100_000
    payer = Address(PAYER_ADDRESS)
    cases = {
        "treasury_short": [(merchant, CREDIT_SOMPI + 1), (treasury, 20_000_000 - 1), (payer, change)],
        "swapped": [(treasury, 20_000_000), (merchant, CREDIT_SOMPI), (payer, change)],
        "no_treasury": [(merchant, DEPOSIT_SOMPI), (payer, change)],
    }
    for name, outputs in cases.items():
        tx = signed_transaction(outputs, payload=payment_payload(nonce))
        header = signature_for(tx, nonce=nonce).to_header()
        response = await client.post("/v1/prepaid/deposits", headers={HEADER_PAYMENT_SIGNATURE: header})
        assert response.status_code == 402, name
        assert response.json()["error"] in {"invalid_fee_split", "underpaid"}, (name, response.text)
    assert await count(sessionmaker, PrepaidBalance) == 0

    good = signed_transaction(
        [(merchant, CREDIT_SOMPI), (treasury, 20_000_000), (payer, change)], payload=payment_payload(nonce)
    )
    response = await client.post(
        "/v1/prepaid/deposits", headers={HEADER_PAYMENT_SIGNATURE: signature_for(good, nonce=nonce).to_header()}
    )
    assert response.status_code == 201, response.text
    assert response.json()["credit_sompi"] == str(CREDIT_SOMPI)


# ---------------------------------------------------------------- debits


async def test_prepaid_call_debits_exactly_the_price_and_forwards(
    client: httpx.AsyncClient,
    shop: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    redis: Any,
    upstream: Upstream,
) -> None:
    await fund(client)
    challenge = await get_debit_challenge(client, path="/p/cheap/forecast?city=hanoi")
    assert challenge.amount_sompi == str(CHEAP_SOMPI) and challenge.balance_sompi == str(CREDIT_SOMPI)
    assert "Purpose: kasgate:v1:prepaid-debit" in challenge.message
    response = await client.get("/p/cheap/forecast?city=hanoi", headers={HEADER_PREPAID_DEBIT: debit_header(challenge)})
    assert response.status_code == 200, response.text
    receipt = receipt_of(response)
    assert receipt.status == "debited" and receipt.amountSompi == str(CHEAP_SOMPI)
    assert receipt.balanceSompi == str(CREDIT_SOMPI - CHEAP_SOMPI)
    [forwarded] = upstream.requests
    assert str(forwarded.url) == "https://api.upstream.test/cheap/forecast?city=hanoi"
    assert HEADER_PREPAID_DEBIT.lower() not in {name.lower() for name in forwarded.headers}
    assert response.headers.get(HEADER_PAYMENT_RESPONSE) is None  # no on-chain payment happened
    assert await redis.get(prepaid_challenge_key(challenge.nonce)) is None
    assert await redis.get(prepaid_nonce_lock_key(challenge.nonce)) is not None

    balance = await balance_row(sessionmaker, shop.merchant_id)
    assert balance is not None
    assert (balance.balance_sompi, balance.debited_sompi) == (CREDIT_SOMPI - CHEAP_SOMPI, CHEAP_SOMPI)
    async with sessionmaker() as session:
        debit = (await session.execute(select(PrepaidDebit))).scalar_one()
    assert debit.nonce == challenge.nonce and debit.status == "debited" and debit.upstream_status_code == 200
    assert debit.request_path == "/p/cheap/forecast?city=hanoi" and debit.body_hash is None
    assert await count(sessionmaker, Settlement) == 1  # only the deposit: a debit is not a settlement

    # replaying the same signed header never debits twice
    replay = await client.get("/p/cheap/forecast?city=hanoi", headers={HEADER_PREPAID_DEBIT: debit_header(challenge)})
    assert replay.status_code == 409 and replay.json()["error"] == "payment_replayed"
    balance = await balance_row(sessionmaker, shop.merchant_id)
    assert balance is not None and balance.balance_sompi == CREDIT_SOMPI - CHEAP_SOMPI


async def test_on_chain_endpoint_also_accepts_prepaid_debits(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client, 2 * PRICE_SOMPI)  # credit 3_940_000_000
    response = await prepaid_call(client, slug="weather")
    assert response.status_code == 200, response.text
    assert receipt_of(response).amountSompi == str(PRICE_SOMPI)  # the list price, no second platform fee


async def test_post_body_is_bound_by_its_hash(client: httpx.AsyncClient, shop: Seed, upstream: Upstream) -> None:
    await fund(client)
    body = b'{"q": "rain"}'
    challenge = await get_debit_challenge(client, method="POST", body=body)
    assert challenge.body_hash == prepaid_body_hash(body)
    tampered = await client.post(
        "/p/cheap", content=b'{"q": "sun"}', headers={HEADER_PREPAID_DEBIT: debit_header(challenge)}
    )
    assert tampered.status_code == 402 and tampered.json()["error"] == "invalid_payment_header"
    ok = await client.post("/p/cheap", content=body, headers={HEADER_PREPAID_DEBIT: debit_header(challenge)})
    assert ok.status_code == 200, ok.text
    assert upstream.requests[-1].content == body


async def test_debit_is_bound_to_method_path_and_endpoint(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    await add_endpoint(sessionmaker, shop.merchant_id, "cheap-two")
    challenge = await get_debit_challenge(client, path="/p/cheap/a?x=1")
    header = {HEADER_PREPAID_DEBIT: debit_header(challenge)}
    for method, target in (
        ("GET", "/p/cheap/a?x=2"),
        ("GET", "/p/cheap/b?x=1"),
        ("GET", "/p/cheap/a"),
        ("POST", "/p/cheap/a?x=1"),
        ("GET", "/p/cheap-two/a?x=1"),
    ):
        response = await client.request(method, target, headers=header)
        assert response.status_code == 402, (method, target)
        assert response.json()["error"] == "invalid_payment_header"
    assert await count(sessionmaker, PrepaidDebit) == 0
    assert (await client.get("/p/cheap/a?x=1", headers=header)).status_code == 200


async def test_signature_must_come_from_the_depositing_key_over_the_server_message(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    challenge = await get_debit_challenge(client)
    forged_key = await client.get("/p/cheap", headers={HEADER_PREPAID_DEBIT: debit_header(challenge, OTHER_KEY)})
    cheaper = challenge.message.replace(f"Amount: {CHEAP_SOMPI}", "Amount: 1")
    altered = await client.get("/p/cheap", headers={HEADER_PREPAID_DEBIT: debit_header(challenge, message=cheaper)})
    login_style = await client.get(
        "/p/cheap", headers={HEADER_PREPAID_DEBIT: debit_header(challenge, message=f"kasgate:v1:{challenge.nonce}")}
    )
    for response in (forged_key, altered, login_style):
        assert response.status_code == 402 and response.json()["error"] == "invalid_payment_header"
    assert await count(sessionmaker, PrepaidDebit) == 0
    malformed = await client.get("/p/cheap", headers={HEADER_PREPAID_DEBIT: "not base64!"})
    assert malformed.status_code == 402 and malformed.json()["error"] == "invalid_payment_header"


async def test_expired_or_unknown_challenge_is_payment_expired(
    client: httpx.AsyncClient, shop: Seed, redis: Any
) -> None:
    await fund(client)
    challenge = await get_debit_challenge(client)
    raw = json.loads(await redis.get(prepaid_challenge_key(challenge.nonce)))
    raw["expiresAt"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await redis.set(prepaid_challenge_key(challenge.nonce), json.dumps(raw))
    expired = await client.get("/p/cheap", headers={HEADER_PREPAID_DEBIT: debit_header(challenge)})
    assert expired.status_code == 402 and expired.json()["error"] == "payment_expired"
    await redis.delete(prepaid_challenge_key(challenge.nonce))
    unknown = await client.get("/p/cheap", headers={HEADER_PREPAID_DEBIT: debit_header(challenge)})
    assert unknown.status_code == 402 and unknown.json()["error"] == "payment_expired"


async def test_prepaid_header_cannot_be_combined_with_an_on_chain_payment(
    client: httpx.AsyncClient, shop: Seed, stub_engine: StubEngine
) -> None:
    await fund(client, 2 * PRICE_SOMPI, slug="weather")
    challenge = await get_debit_challenge(client, slug="weather")
    required = await fetch_challenge(client, "weather")
    response = await client.get(
        "/p/weather",
        headers={
            HEADER_PREPAID_DEBIT: debit_header(challenge),
            HEADER_PAYMENT_SIGNATURE: make_signature_header(None, required.nonce),
        },
    )
    assert response.status_code == 402 and response.json()["error"] == "invalid_payment_header"
    assert len(stub_engine.calls) == 1  # only the deposit was settled on-chain


async def test_insufficient_balance_never_goes_negative(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    none_yet = await request_challenge(client)
    assert none_yet.status_code == 402 and none_yet.json()["error"] == "insufficient_balance"
    assert none_yet.json()["accepts"][0]["scheme"] == "kasgate-prepaid-v1"

    await fund(client)  # credit 40_000_000
    await add_endpoint(sessionmaker, shop.merchant_id, "pricey", price_sompi=30_000_000)
    first = await get_debit_challenge(client, slug="pricey")
    second = await get_debit_challenge(client, slug="pricey")  # both quoted while 40_000_000 was available
    ok = await client.get("/p/pricey", headers={HEADER_PREPAID_DEBIT: debit_header(first)})
    assert ok.status_code == 200
    short = await client.get("/p/pricey", headers={HEADER_PREPAID_DEBIT: debit_header(second)})
    assert short.status_code == 402 and short.json()["error"] == "insufficient_balance"
    balance = await balance_row(sessionmaker, shop.merchant_id)
    assert balance is not None and balance.balance_sompi == 10_000_000
    assert await count(sessionmaker, PrepaidDebit) == 1
    later = await request_challenge(client, slug="pricey")
    assert later.status_code == 402 and later.json()["error"] == "insufficient_balance"


async def test_parallel_debits_serialise_on_the_balance(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)  # credit 40_000_000
    await add_endpoint(sessionmaker, shop.merchant_id, "tenth", price_sompi=10_000_000)
    challenges = [await get_debit_challenge(client, slug="tenth") for _ in range(7)]
    responses = await asyncio.gather(
        *(client.get("/p/tenth", headers={HEADER_PREPAID_DEBIT: debit_header(c)}) for c in challenges)
    )
    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 200, 200, 200, 402, 402, 402]
    balance = await balance_row(sessionmaker, shop.merchant_id)
    assert balance is not None
    assert (balance.balance_sompi, balance.debited_sompi) == (0, CREDIT_SOMPI)


async def test_parallel_replay_of_one_debit_debits_once(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    challenge = await get_debit_challenge(client)
    header = {HEADER_PREPAID_DEBIT: debit_header(challenge)}
    responses = await asyncio.gather(*(client.get("/p/cheap", headers=header) for _ in range(10)))
    codes = [r.status_code for r in responses]
    assert codes.count(200) == 1 and set(codes) <= {200, 402, 409}
    assert await count(sessionmaker, PrepaidDebit) == 1
    balance = await balance_row(sessionmaker, shop.merchant_id)
    assert balance is not None and balance.balance_sompi == CREDIT_SOMPI - CHEAP_SOMPI


async def test_upstream_failure_refunds_the_debit(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession], upstream: Upstream
) -> None:
    await fund(client)
    upstream.handler = lambda request: httpx.Response(503, json={"down": True})
    failed = await prepaid_call(client)
    assert failed.status_code == 503
    assert receipt_of(failed).status == "refunded" and receipt_of(failed).balanceSompi == str(CREDIT_SOMPI)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    upstream.handler = boom
    unreachable = await prepaid_call(client)
    assert unreachable.status_code == 502 and unreachable.json()["error"] == "upstream_error"
    assert receipt_of(unreachable).status == "refunded"

    upstream.handler = lambda request: httpx.Response(404, json={"missing": True})
    not_found = await prepaid_call(client)  # a 4xx is a delivered answer: it stays debited
    assert not_found.status_code == 404 and receipt_of(not_found).status == "debited"

    balance = await balance_row(sessionmaker, shop.merchant_id)
    assert balance is not None
    assert (balance.balance_sompi, balance.debited_sompi) == (CREDIT_SOMPI - CHEAP_SOMPI, CHEAP_SOMPI)
    async with sessionmaker() as session:
        statuses = sorted(
            (row.status, row.upstream_status_code or 0)
            for row in (await session.execute(select(PrepaidDebit))).scalars()
        )
    assert statuses == [("debited", 404), ("refunded", 0), ("refunded", 503)]  # 0: no upstream response


async def test_credit_is_per_merchant(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await fund(client)
    other = await seed_endpoint(
        sessionmaker, slug="elsewhere", address=SECOND_MERCHANT_ADDRESS, price_sompi=CHEAP_SOMPI
    )
    async with sessionmaker() as session:
        endpoint = await session.get(Endpoint, other.endpoint_id)
        assert endpoint is not None
        endpoint.prepaid_only = True
        await session.commit()
    refused = await request_challenge(client, slug="elsewhere")
    assert refused.status_code == 402 and refused.json()["error"] == "insufficient_balance"
    # a challenge for merchant A cannot be spent at merchant B
    challenge = await get_debit_challenge(client, path="/p/cheap")
    crossed = await client.get("/p/elsewhere", headers={HEADER_PREPAID_DEBIT: debit_header(challenge)})
    assert crossed.status_code == 402 and crossed.json()["error"] == "invalid_payment_header"


async def test_usd_prepaid_only_endpoint_debits_the_unfloored_quote(
    client: httpx.AsyncClient,
    shop: Seed,
    sessionmaker: async_sessionmaker[AsyncSession],
    stub_oracle: StubOracle,
) -> None:
    await add_endpoint(
        sessionmaker,
        shop.merchant_id,
        "usd-cheap",
        price_sompi=None,
        price_currency=PriceCurrency.USD,
        price_usd_micros=100,  # $0.0001 -> 80_000 sompi at $0.125/KAS
    )
    unpaid = await client.get("/p/usd-cheap")
    assert unpaid.status_code == 402 and unpaid.json()["accepts"][0]["amount"] == "80000"
    await fund(client)
    challenge = await get_debit_challenge(client, slug="usd-cheap")
    assert challenge.amount_sompi == "80000" and challenge.kas_usd_rate == "0.12500000"
    stub_oracle.rate = Decimal("0.25")  # the locked quote is what gets debited
    response = await client.get("/p/usd-cheap", headers={HEADER_PREPAID_DEBIT: debit_header(challenge)})
    assert response.status_code == 200 and receipt_of(response).amountSompi == "80000"
    async with sessionmaker() as session:
        debit = (await session.execute(select(PrepaidDebit))).scalar_one()
    assert debit.kas_usd_rate == Decimal("0.125") and debit.usd_value == Decimal("0.0001")

    stub_oracle.rate = None
    down = await client.get("/p/usd-cheap")
    assert down.status_code == 503 and down.json()["error"] == "price_unavailable"
    no_quote = await request_challenge(client, slug="usd-cheap")
    assert no_quote.status_code == 503 and no_quote.json()["error"] == "price_unavailable"


async def test_challenge_validation(client: httpx.AsyncClient, shop: Seed) -> None:
    await fund(client)
    cases = [
        ({"path": "/p/cheapish"}, 422),  # another slug's prefix
        ({"path": "/p/cheap x"}, 422),
        ({"path": "/p/cheap#frag"}, 422),
        ({"method": "TRACE"}, 422),
        ({"body_hash": "A" * 64}, 422),
        ({"payer_address": PAYER_KEY.to_address_ecdsa("testnet").to_string()}, 400),
        ({"slug": "nope", "path": "/p/nope"}, 404),
        ({"method": "DELETE"}, 405),
    ]
    for override, status in cases:
        body = {"slug": "cheap", "payer_address": PAYER_ADDRESS, "method": "GET", "path": "/p/cheap", **override}
        response = await client.post("/v1/prepaid/challenge", json=body)
        assert response.status_code == status, (override, response.text)


async def test_merchant_ledger_routes(
    client: httpx.AsyncClient, shop: Seed, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    for path in ("/v1/prepaid/balances", "/v1/prepaid/deposits", "/v1/prepaid/debits"):
        assert (await client.get(path)).status_code == 401
    await fund(client)
    assert (await prepaid_call(client)).status_code == 200
    headers = bearer(shop.api_key)
    [balance] = (await client.get("/v1/prepaid/balances", headers=headers)).json()
    assert balance["payer_address"] == PAYER_ADDRESS and balance["balance_sompi"] == str(CREDIT_SOMPI - CHEAP_SOMPI)
    [deposit] = (await client.get("/v1/prepaid/deposits", headers=headers)).json()
    assert deposit["credit_sompi"] == str(CREDIT_SOMPI) and deposit["fee_sompi"] == "20000000"
    [debit] = (await client.get("/v1/prepaid/debits", headers=headers)).json()
    assert debit["amount_sompi"] == str(CHEAP_SOMPI) and debit["status"] == "debited"

    other = await seed_endpoint(sessionmaker, slug="elsewhere", address=OTHER_ADDRESS)
    assert (await client.get("/v1/prepaid/balances", headers=bearer(other.api_key))).json() == []
    empty = await client.get("/v1/prepaid/balance", params={"slug": "elsewhere", "payer_address": PAYER_ADDRESS})
    assert empty.status_code == 200 and empty.json()["balance_sompi"] == "0"
