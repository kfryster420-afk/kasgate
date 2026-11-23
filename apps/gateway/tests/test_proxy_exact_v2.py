"""CONTRACT v2.10 at the proxy: one x402 v2 envelope offering both bindings, and the kaspa-exact-v2 payment path."""

import base64
import json
from decimal import Decimal
from typing import Any

import httpx
from kaspa import PrivateKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import PriceCurrency, Settlement
from app.payments import RpcUnavailableError
from app.proxy import lock_key, payment_identifier_lock_key
from app.schemas import (
    HEADER_K402_PAYMENT,
    HEADER_PAYMENT_REQUIRED,
    HEADER_PAYMENT_RESPONSE,
    HEADER_PAYMENT_SIGNATURE,
    PaymentRequired,
    PaymentRequiredEnvelope,
)
from app.x402_v2 import PAYMENT_IDENTIFIER_SCHEMA, ExactV2Requirements, SettlementResponse, encode_canonical_header
from tests.conftest import (
    MERCHANT_ADDRESS,
    PAYER_ADDRESS,
    PRICE_SOMPI,
    TREASURY,
    Seed,
    StubEngine,
    StubOracle,
    Upstream,
    make_signature_header,
    seed_endpoint,
)
from tests.exact_v2_support import BuiltPayment, ExactPaymentSpec, Funding, build_payment

BASE = "http://gateway.test"
PAYER = PrivateKey("03" * 32)


async def fetch_envelope(client: httpx.AsyncClient, path: str = "/p/weather") -> PaymentRequiredEnvelope:
    response = await client.get(path)
    assert response.status_code == 402, response.text
    return PaymentRequiredEnvelope.from_header(response.headers[HEADER_PAYMENT_REQUIRED])


async def exact_offer(client: httpx.AsyncClient, path: str = "/p/weather") -> ExactV2Requirements:
    offer = (await fetch_envelope(client, path)).exact_v2()
    assert offer is not None
    return offer


async def build(
    client: httpx.AsyncClient,
    path: str = "/p/weather",
    *,
    method: str = "GET",
    body: str | None = None,
    payment_identifier: str = "pay_0123456789abcdef0123",
    funding_tx: str = "70" * 32,
    **overrides: Any,
) -> BuiltPayment:
    spec = ExactPaymentSpec(
        requirement=await exact_offer(client, path.split("?")[0]),
        url=f"{BASE}{path}",
        method=method,
        body=body,
        payment_identifier=payment_identifier,
        fundings=[Funding(amount=PRICE_SOMPI + 500_000_000, key=PAYER, tx_id=funding_tx)],
    )
    for name, value in overrides.items():
        setattr(spec, name, value)
    return build_payment(spec)


def pay(built: BuiltPayment) -> dict[str, str]:
    return {HEADER_PAYMENT_SIGNATURE: built.header()}


async def settlements(maker: async_sessionmaker[AsyncSession]) -> list[Settlement]:
    async with maker() as session:
        return list((await session.execute(select(Settlement))).scalars().all())


# ---------------------------------------------------------------- the 402 envelope


async def test_402_is_one_x402_v2_envelope_offering_both_bindings(client: httpx.AsyncClient, seeded: Seed) -> None:
    response = await client.get("/p/weather")
    assert response.status_code == 402
    header = response.headers[HEADER_PAYMENT_REQUIRED]
    decoded = json.loads(base64.b64decode(header))
    assert encode_canonical_header(decoded) == header  # canonical (sorted, compact) JSON, like @kaspa-x402/core
    body = response.json()
    assert body["error"] == "payment_required" and body["k402"] == "0.2"
    # the body keeps the CONTRACT v2.9 k402 offer list and appends the kaspa-exact-v2 entry to it
    assert [entry["scheme"] for entry in body["accepts"]] == ["kaspa-exact-v1", "kaspa-utxo", "exact"]
    assert body["accepts"][2] == decoded["accepts"][0]
    assert body["accepts"][0]["nonce"] == decoded["accepts"][1]["nonce"]

    assert decoded["x402Version"] == 2
    assert decoded["resource"]["url"] == f"{BASE}/p/weather"
    exact, v1 = decoded["accepts"]
    assert exact == {
        "scheme": "exact",
        "network": "kaspa:testnet-10",
        "amount": str(PRICE_SOMPI),  # the full list price: no platform fee on this binding
        "asset": "KAS",
        "payTo": MERCHANT_ADDRESS,
        "maxTimeoutSeconds": 120,
        "extra": {
            "binding": "kaspa-exact-v2",
            "profile": "standard-native",
            "paymentFlow": "upfront",
            "finality": "accepted",
            "transactionEncoding": "kaspa-sdk-safe-json-v2.0.0",
            "payToScriptPublicKey": exact["extra"]["payToScriptPublicKey"],
        },
    }
    assert v1["scheme"] == "kaspa-exact-v1" and v1["platformFee"]["feeTo"] == TREASURY
    assert "x402Version" not in v1
    assert decoded["extensions"]["payment-identifier"] == {
        "info": {"required": True},
        "schema": PAYMENT_IDENTIFIER_SCHEMA,
    }
    # backward compatibility: the kaspa-exact-v1 terms are mirrored at the top level for pre-v2.10 readers
    for key in ("scheme", "network", "asset", "amount", "payTo", "platformFee", "nonce", "expiresAt"):
        assert decoded[key] == v1[key]
    terms = PaymentRequired.from_header(header)
    assert terms.nonce == v1["nonce"] and terms.merchant_amount_sompi + terms.fee_amount_sompi == PRICE_SOMPI


async def test_resource_url_is_the_exact_public_request_url(client: httpx.AsyncClient, seeded: Seed) -> None:
    envelope = await fetch_envelope(client, "/p/weather/forecast%20today?city=Hanoi&units=metric")
    assert envelope.resource.url == f"{BASE}/p/weather/forecast%20today?city=Hanoi&units=metric"


# ---------------------------------------------------------------- paying with kaspa-exact-v2


async def test_v2_payment_settles_without_platform_fee_and_returns_a_settlement_response(
    client: httpx.AsyncClient,
    seeded: Seed,
    stub_engine: StubEngine,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    built = await build(client)
    response = await client.get("/p/weather", headers=pay(built))
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}
    assert stub_engine.events[-3:] == ["verify_exact_v2", "resolve_exact_v2_inputs", "settle_exact_v2"]
    assert stub_engine.calls == [built.tx_id]
    assert HEADER_PAYMENT_SIGNATURE.lower() not in {k.lower() for k in upstream.requests[0].headers}

    receipt = SettlementResponse.from_header(response.headers[HEADER_PAYMENT_RESPONSE], 10_000)
    assert receipt.success and receipt.transaction == built.tx_id
    assert receipt.amount == str(PRICE_SOMPI) and receipt.payer == PAYER_ADDRESS
    assert receipt.network == "kaspa:testnet-10"
    kaspa_ext = receipt.extensions.kaspa
    assert (kaspa_ext.exactProfile, kaspa_ext.finality, kaspa_ext.paymentOutputIndex) == (
        "standard-native",
        "accepted",
        0,
    )
    assert kaspa_ext.requestHash == built.request_hash
    assert receipt.extensions.kasgate.platformFeeSompi == "0"
    assert str(receipt.extensions.kasgate.usdValueAtSettlement) == "2.50000000"

    (row,) = await settlements(sessionmaker)
    assert row.payment_scheme == "kaspa-exact-v2"
    assert row.amount_sompi == row.merchant_amount_sompi == PRICE_SOMPI and row.fee_amount_sompi == 0
    assert row.treasury_address is None and row.payment_nonce is None
    assert row.payment_identifier == "pay_0123456789abcdef0123" and row.request_hash == built.request_hash
    assert row.payer_address == PAYER_ADDRESS and row.upstream_status_code == 200

    listed = (await client.get("/v1/settlements", headers={"Authorization": f"Bearer {seeded.api_key}"})).json()
    assert listed[0]["payment_scheme"] == "kaspa-exact-v2" and listed[0]["treasury_address"] is None
    assert listed[0]["fee_amount_sompi"] == 0 and listed[0]["payment_identifier"] == row.payment_identifier


async def test_post_body_and_query_are_part_of_the_request_fingerprint(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine
) -> None:
    built = await build(client, "/p/weather?city=Hanoi", method="POST", body='{"q":"rain"}')
    tampered = await client.post("/p/weather?city=Hanoi", content='{"q":"snow"}', headers=pay(built))
    assert tampered.status_code == 402
    assert tampered.json()["error"] == "invalid_exact_payment" and "requestHash" in tampered.json()["message"]
    assert PaymentRequiredEnvelope.from_header(tampered.headers[HEADER_PAYMENT_REQUIRED]).exact_v2() is not None
    other_query = await client.post("/p/weather?city=Hue", content='{"q":"rain"}', headers=pay(built))
    assert other_query.status_code == 402
    ok = await client.post("/p/weather?city=Hanoi", content='{"q":"rain"}', headers=pay(built))
    assert ok.status_code == 200, ok.text
    assert stub_engine.calls == [built.tx_id]


async def test_replay_and_payment_identifier_conflicts_are_409(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine
) -> None:
    built = await build(client)
    assert (await client.get("/p/weather", headers=pay(built))).status_code == 200
    again = await client.get("/p/weather", headers=pay(built))  # upstream already answered 200: no redelivery
    assert again.status_code == 409 and again.json()["error"] == "payment_replayed"

    same_tx_new_id = await build(client, payment_identifier="pay_ffffffffffffffffffff")
    assert same_tx_new_id.tx_id == built.tx_id
    assert (await client.get("/p/weather", headers=pay(same_tx_new_id))).status_code == 409

    same_id_new_request = await build(client, "/p/weather?city=Hue", funding_tx="71" * 32)
    conflict = await client.get("/p/weather?city=Hue", headers=pay(same_id_new_request))
    assert conflict.status_code == 409
    assert "kaspa_payment_identifier_conflict" in conflict.json()["message"]
    assert stub_engine.calls == [built.tx_id]


async def test_a_v2_transaction_cannot_be_claimed_again_through_kaspa_exact_v1(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine
) -> None:
    built = await build(client)
    assert (await client.get("/p/weather", headers=pay(built))).status_code == 200
    challenge = PaymentRequired.from_header((await client.get("/p/weather")).headers[HEADER_PAYMENT_REQUIRED])
    v1 = await client.get(
        "/p/weather", headers={HEADER_PAYMENT_SIGNATURE: make_signature_header(built.tx_id, challenge.nonce)}
    )
    assert v1.status_code == 409
    assert stub_engine.calls == [built.tx_id]


async def test_same_payment_identifier_retry_is_redelivered_after_an_upstream_5xx(
    client: httpx.AsyncClient,
    seeded: Seed,
    stub_engine: StubEngine,
    upstream: Upstream,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    upstream.handler = lambda request: httpx.Response(503, json={"busy": True})
    built = await build(client)
    first = await client.get("/p/weather", headers=pay(built))
    assert first.status_code == 503 and HEADER_PAYMENT_RESPONSE in first.headers
    upstream.handler = None
    second = await client.get("/p/weather", headers=pay(built))
    assert second.status_code == 200
    assert SettlementResponse.from_header(second.headers[HEADER_PAYMENT_RESPONSE], 10_000).transaction == built.tx_id
    assert stub_engine.calls == [built.tx_id]  # settled once, delivered twice
    (row,) = await settlements(sessionmaker)
    assert row.delivery_attempts == 2 and row.upstream_status_code == 200


async def test_verification_failures_answer_402_with_a_fresh_envelope_and_release_locks(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any
) -> None:
    built = await build(client)
    stub_engine.fail_resolve("invalid_exact_payment", "input 0 spends an unknown outpoint")
    failed = await client.get("/p/weather", headers=pay(built))
    assert failed.status_code == 402 and failed.json()["error"] == "invalid_exact_payment"
    envelope = PaymentRequiredEnvelope.from_header(failed.headers[HEADER_PAYMENT_REQUIRED])
    assert envelope.error == "invalid_exact_payment" and envelope.exact_v2() is not None
    assert not await redis.exists(lock_key(built.tx_id), payment_identifier_lock_key("pay_0123456789abcdef0123"))

    stub_engine.resolve_error = None
    stub_engine.error = RpcUnavailableError("node down")
    unavailable = await client.get("/p/weather", headers=pay(built))
    assert unavailable.status_code == 503 and unavailable.headers["Retry-After"] == "5"

    stub_engine.error = None
    assert (await client.get("/p/weather", headers=pay(built))).status_code == 200


async def test_mispaying_payments_never_reach_the_node(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine
) -> None:
    offer = await exact_offer(client)
    cheaper = offer.model_copy(update={"amount": str(PRICE_SOMPI - 1)}).wire()
    not_offered = await build(client, accepted_override=cheaper)
    response = await client.get("/p/weather", headers=pay(not_offered))
    assert response.status_code == 402 and "invalid_kaspa_x402_accepted" in response.json()["message"]

    underpaid = await build(client, outputs=[(offer.extra.payToScriptPublicKey, PRICE_SOMPI - 1)])
    response = await client.get("/p/weather", headers=pay(underpaid))
    assert response.status_code == 402 and response.json()["error"] == "underpaid"

    with_treasury = await build(
        client,
        outputs=[(offer.extra.payToScriptPublicKey, PRICE_SOMPI), (script_of_address(TREASURY), 30_000_000)],
    )
    response = await client.get("/p/weather", headers=pay(with_treasury))
    assert response.status_code == 402 and response.json()["error"] == "invalid_exact_payment"
    assert stub_engine.calls == [] and stub_engine.resolve_calls == []


async def test_malformed_v2_payload_is_invalid_payment_header(client: httpx.AsyncClient, seeded: Seed) -> None:
    built = await build(client)
    broken = json.loads(json.dumps(built.payload))
    broken["payload"]["transactionId"] = built.tx_id  # forbidden: the verifier recomputes the id
    header = encode_canonical_header(broken)
    response = await client.get("/p/weather", headers={HEADER_PAYMENT_SIGNATURE: header})
    assert response.status_code == 402 and response.json()["error"] == "invalid_payment_header"
    assert "transactionId" in response.json()["message"]


async def test_payment_identifier_in_flight_is_409(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any
) -> None:
    built = await build(client)
    await redis.set(payment_identifier_lock_key("pay_0123456789abcdef0123"), "other", ex=60)
    response = await client.get("/p/weather", headers=pay(built))
    assert response.status_code == 409
    assert not await redis.exists(lock_key(built.tx_id))
    assert stub_engine.calls == []


def script_of_address(address: str) -> str:
    from app.kaspa_types import serialized_script_public_key

    return serialized_script_public_key(address)


# ---------------------------------------------------------------- locked USD pricing and offer lifetime


async def test_usd_endpoint_offers_the_same_locked_sompi_and_settles_at_the_locked_rate(
    client: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    stub_engine: StubEngine,
    stub_oracle: StubOracle,
) -> None:
    await seed_endpoint(
        sessionmaker, slug="usd-api", price_currency=PriceCurrency.USD, price_usd_micros=1_000_000, price_sompi=None
    )
    response = await client.get("/p/usd-api")
    assert response.status_code == 402
    envelope = PaymentRequiredEnvelope.from_header(response.headers[HEADER_PAYMENT_REQUIRED])
    exact, v1 = envelope.exact_v2(), envelope.v1_terms()
    assert exact is not None and v1.extra is not None
    assert exact.amount == v1.amount == "800000000"  # $1 at the locked 0.125 KAS/USD, the full total to the merchant
    body_offers = response.json()["accepts"]
    assert body_offers[2]["amount"] == v1.amount and body_offers[0]["amount"] == v1.amount

    built = build_payment(
        ExactPaymentSpec(
            requirement=exact,
            url=f"{BASE}/p/usd-api",
            method="GET",
            body=None,
            payment_identifier="pay_usd_0123456789abcdef",
            fundings=[Funding(amount=2_000_000_000, key=PAYER, tx_id="72" * 32)],
        )
    )
    stub_oracle.rate = Decimal("0.25")  # the market moves after the quote: the payment keeps the locked terms
    paid = await client.get("/p/usd-api", headers=pay(built))
    assert paid.status_code == 200, paid.text
    (row,) = await settlements(sessionmaker)
    assert row.payment_scheme == "kaspa-exact-v2" and row.amount_sompi == 800_000_000
    assert row.kas_usd_rate == Decimal("0.12500000")
    assert row.usd_value_at_settlement == Decimal("1.00000000")
    receipt = SettlementResponse.from_header(paid.headers[HEADER_PAYMENT_RESPONSE], 10_000)
    assert str(receipt.extensions.kasgate.usdValueAtSettlement) == "1.00000000"


async def test_usd_endpoint_fails_closed_without_any_offer_when_the_oracle_is_down(
    client: httpx.AsyncClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    stub_engine: StubEngine,
    stub_oracle: StubOracle,
    redis: Any,
) -> None:
    await seed_endpoint(
        sessionmaker, slug="usd-api", price_currency=PriceCurrency.USD, price_usd_micros=1_000_000, price_sompi=None
    )
    exact = (await fetch_envelope(client, "/p/usd-api")).exact_v2()
    assert exact is not None
    await redis.flushall()  # the issued offer is gone (expired) when the oracle goes down
    stub_oracle.rate = None
    response = await client.get("/p/usd-api")
    assert response.status_code == 503 and response.json()["error"] == "price_unavailable"
    assert response.json().get("accepts") is None and HEADER_PAYMENT_REQUIRED not in response.headers
    assert await redis.keys("kasgate:exact:*") == []

    built = build_payment(
        ExactPaymentSpec(
            requirement=exact,
            url=f"{BASE}/p/usd-api",
            method="GET",
            body=None,
            payment_identifier="pay_usd_0123456789abcdef",
            fundings=[Funding(amount=2_000_000_000, key=PAYER, tx_id="73" * 32)],
        )
    )
    retry = await client.get("/p/usd-api", headers=pay(built))
    assert retry.status_code == 503 and retry.json()["error"] == "price_unavailable"
    assert await redis.keys("kasgate:tx:*") == [] and await redis.keys("kasgate:pid:*") == []
    assert stub_engine.calls == [] and await settlements(sessionmaker) == []


async def test_an_expired_offer_is_402_payment_expired_and_never_settles(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any
) -> None:
    built = await build(client)
    for key in await redis.keys("kasgate:exact:*"):
        await redis.delete(key)
    response = await client.get("/p/weather", headers=pay(built))
    assert response.status_code == 402 and response.json()["error"] == "payment_expired"
    assert "invalid_kaspa_x402_accepted" in response.json()["message"]
    assert PaymentRequiredEnvelope.from_header(response.headers[HEADER_PAYMENT_REQUIRED]).exact_v2() is not None
    assert stub_engine.calls == [] and not await redis.exists(lock_key(built.tx_id))


async def test_a_settled_payment_is_redelivered_even_after_its_offer_expired(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, upstream: Upstream, redis: Any
) -> None:
    upstream.handler = lambda request: httpx.Response(502, json={"down": True})
    built = await build(client)
    assert (await client.get("/p/weather", headers=pay(built))).status_code == 502
    for key in await redis.keys("kasgate:exact:*"):
        await redis.delete(key)
    upstream.handler = None
    again = await client.get("/p/weather", headers=pay(built))
    assert again.status_code == 200, again.text
    assert stub_engine.calls == [built.tx_id]


async def test_the_txid_lock_is_shared_with_the_other_bindings(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, redis: Any
) -> None:
    built = await build(client)
    await redis.set(lock_key(built.tx_id), "1", ex=60)  # a kaspa-exact-v1 or k402 request is settling this txId
    response = await client.get("/p/weather", headers=pay(built))
    assert response.status_code == 409 and response.json()["error"] == "payment_replayed"
    assert not await redis.exists(payment_identifier_lock_key("pay_0123456789abcdef0123"))
    assert stub_engine.calls == []


async def test_a_v2_transaction_cannot_be_claimed_again_through_k402(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine
) -> None:
    built = await build(client)
    assert (await client.get("/p/weather", headers=pay(built))).status_code == 200
    offer = (await client.get("/p/weather")).json()["accepts"][1]
    assert offer["scheme"] == "kaspa-utxo"
    k402 = await client.get(
        "/p/weather", headers={HEADER_K402_PAYMENT: f"kaspa-utxo {built.tx_id} {offer['payment_id']}"}
    )
    assert k402.status_code == 409 and k402.json()["error"] == "payment_replayed"
    assert stub_engine.calls == [built.tx_id]
