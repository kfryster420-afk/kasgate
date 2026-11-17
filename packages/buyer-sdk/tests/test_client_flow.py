"""End-to-end SDK behaviour against a programmable fake gateway (no network, no node)."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import kasgate_client as sdk
import kaspa
import pytest
from app import schemas as gw
from conftest import (
    MERCHANT,
    OTHER,
    PAYER,
    PAYER_KEY,
    PRICE,
    TREASURY,
    FakeGateway,
    challenge,
    make_utxo,
)
from kasgate_client import KasGateClient, KasGateSyncClient, receipt_of

KAS = 100_000_000
FLOOR_PRICE = 1_333_333_267


def script_of(address: str) -> str:
    return kaspa.pay_to_address_script(kaspa.Address(address)).script


def assert_untouched(client: KasGateClient, gateway: FakeGateway) -> None:
    """The challenge was refused: no UTXO lookup, no signature header on the wire."""
    assert client.provider.calls == []  # type: ignore[attr-defined]
    assert all("PAYMENT-SIGNATURE" not in r.headers for r in gateway.requests)
    assert gateway.signatures == []


# ------------------------------------------------------------------ happy path


async def test_pays_with_exact_output_layout_and_attaches_receipt(gateway, make_client) -> None:
    client = make_client(gateway)
    response = await client.get("/p/demo", max_price_kas="20")

    assert response.status_code == 200
    assert len(gateway.requests) == 2
    sig = gateway.signatures[0]["signature"]
    tx = gateway.signatures[0]["tx"]

    # the gateway's own pydantic model accepts exactly what the SDK sent
    gateway_sig = gw.PaymentSignature.from_header(gateway.requests[1].headers["PAYMENT-SIGNATURE"])
    assert gateway_sig.txId == sig.txId == tx.id
    assert gateway_sig.payerAddress == PAYER

    merchant, fee = sdk.compute_fee_split(PRICE)
    outputs = tx.outputs
    assert len(outputs) == 3
    assert (outputs[0].value, outputs[0].script_public_key.script) == (merchant, script_of(MERCHANT))
    assert (outputs[1].value, outputs[1].script_public_key.script) == (fee, script_of(TREASURY))
    assert outputs[2].script_public_key.script == script_of(PAYER)
    assert merchant + fee == PRICE

    # rawTx is json.dumps(tx.to_dict()) of a SIGNED tx
    raw = json.loads(sig.rawTx)
    assert raw["id"] == tx.id
    assert all(i["signatureScript"] for i in raw["inputs"])
    assert sig.rawTx == json.dumps(raw)

    receipt = receipt_of(response)
    assert receipt is not None
    assert receipt.tx_id == tx.id and receipt.amount_sompi == PRICE
    assert receipt.merchant_sompi == merchant and receipt.fee_sompi == fee
    assert 0 < receipt.network_fee_sompi <= sdk.DEFAULT_MAX_NETWORK_FEE_SOMPI
    assert str(receipt.usd_value_at_settlement) == "1.23456789"
    assert str(kaspa.Transaction.from_dict(json.loads(sig.rawTx)).finalize()) == tx.id


async def test_unpaid_success_is_passed_through(make_client) -> None:
    gw_fake = FakeGateway()
    gw_fake.required = challenge()
    client = make_client(gw_fake)

    def free(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"free": True})

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(free), base_url="http://x")  # noqa: SLF001
    response = await client.get("/free")
    assert response.status_code == 200 and receipt_of(response) is None
    assert client.provider.calls == []  # type: ignore[attr-defined]


async def test_402_without_challenge_header_is_returned_untouched(make_client) -> None:
    def plain(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"detail": "pay the toll another way"})

    client = make_client(FakeGateway())
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(plain), base_url="http://x")  # noqa: SLF001
    response = await client.get("/p/x")
    assert response.status_code == 402
    assert client.provider.calls == []  # type: ignore[attr-defined]


async def test_post_body_and_headers_are_replayed_after_payment(gateway, make_client) -> None:
    client = make_client(gateway)
    response = await client.post("/p/demo/search", json={"q": "kaspa"}, headers={"X-Trace": "t1"})
    assert response.status_code == 200
    first, second = gateway.requests
    assert first.content == second.content == b'{"q":"kaspa"}'
    assert first.headers["x-trace"] == second.headers["x-trace"] == "t1"
    assert first.method == second.method == "POST"
    assert "PAYMENT-SIGNATURE" in second.headers and "PAYMENT-SIGNATURE" not in first.headers


async def test_streaming_body_is_refused_before_any_request(gateway, make_client) -> None:
    client = make_client(gateway)

    async def chunks():
        yield b"x"

    with pytest.raises(sdk.ConfigurationError):
        await client.post("/p/demo", content=chunks())
    assert gateway.requests == []


# ------------------------------------------------------------------ circuit breaker


async def test_price_over_client_cap_refuses_before_signing(make_client) -> None:
    gateway = FakeGateway(challenge(PRICE))
    client = make_client(gateway, max_price_sompi=PRICE - 1)
    with pytest.raises(sdk.PriceCapExceededError) as caught:
        await client.get("/p/demo")
    assert caught.value.amount_sompi == PRICE and caught.value.cap_sompi == PRICE - 1
    assert_untouched(client, gateway)
    assert len(gateway.requests) == 1


async def test_cap_is_inclusive(make_client) -> None:
    gateway = FakeGateway(challenge(PRICE))
    client = make_client(gateway, max_price_sompi=PRICE)
    assert (await client.get("/p/demo")).status_code == 200


async def test_per_call_max_price_kas_tightens_the_cap(make_client) -> None:
    gateway = FakeGateway(challenge(PRICE))  # 15 KAS
    client = make_client(gateway, max_price_sompi=20 * KAS)
    with pytest.raises(sdk.PriceCapExceededError) as caught:
        await client.get("/p/demo", max_price_kas="14.99999999")
    assert caught.value.cap_sompi == 1_499_999_999
    assert_untouched(client, gateway)


async def test_per_call_max_price_kas_can_never_raise_the_client_cap(make_client) -> None:
    gateway = FakeGateway(challenge(PRICE))
    client = make_client(gateway, max_price_sompi=10 * KAS)
    with pytest.raises(sdk.PriceCapExceededError) as caught:
        await client.get("/p/demo", max_price_kas="1000000")
    assert caught.value.cap_sompi == 10 * KAS
    assert_untouched(client, gateway)


async def test_malicious_astronomical_price_is_refused(make_client) -> None:
    gateway = FakeGateway(challenge(9_999_999_999_999_999_999 // 2))
    client = make_client(gateway)
    with pytest.raises(sdk.PriceCapExceededError):
        await client.get("/p/demo")
    assert_untouched(client, gateway)


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "1.000000001", "nan"])
async def test_invalid_per_call_cap_is_a_configuration_error(gateway, make_client, bad) -> None:
    client = make_client(gateway)
    with pytest.raises(sdk.ConfigurationError):
        await client.get("/p/demo", max_price_kas=bad)
    assert gateway.requests == []


@pytest.mark.parametrize("bad", [0, -5, True, 1.5])
def test_invalid_client_cap_is_rejected(bad) -> None:
    with pytest.raises(sdk.ConfigurationError):
        KasGateClient(PAYER_KEY, max_price_sompi=bad)


# ------------------------------------------------------------------ challenge validation


def _bad_header(mutate) -> str:
    payload = json.loads(challenge().model_dump_json())
    mutate(payload)
    return base64.b64encode(json.dumps(payload).encode()).decode()


@pytest.mark.parametrize(
    ("make_gateway", "error"),
    [
        (lambda: FakeGateway(challenge(x402Version=1)), sdk.ProtocolMismatchError),
        (lambda: FakeGateway(challenge(x402Version=3)), sdk.ProtocolMismatchError),
        (lambda: FakeGateway(challenge(scheme="exact")), sdk.ProtocolMismatchError),
        (lambda: FakeGateway(challenge(asset="USDC")), sdk.ProtocolMismatchError),
        (lambda: FakeGateway(challenge(network="kaspa:mainnet")), sdk.NetworkMismatchError),
        (lambda: FakeGateway(challenge(network="eip155:1")), sdk.NetworkMismatchError),
        (
            lambda: FakeGateway(challenge(pay_to="kaspa:" + MERCHANT.split(":", 1)[1])),
            sdk.NetworkMismatchError,
        ),
        (
            lambda: FakeGateway(challenge(fee_to="kaspa:" + TREASURY.split(":", 1)[1])),
            sdk.NetworkMismatchError,
        ),
        (lambda: FakeGateway(challenge(fee_amount=sdk.compute_fee_split(PRICE)[1] + 1)), sdk.FeeSplitMismatchError),
        (lambda: FakeGateway(challenge(fee_amount=sdk.compute_fee_split(PRICE)[1] - 1)), sdk.FeeSplitMismatchError),
        (lambda: FakeGateway(challenge(fee_amount=1)), sdk.FeeSplitMismatchError),
        (lambda: FakeGateway(challenge(FLOOR_PRICE - 1)), sdk.FeeSplitMismatchError),
        (lambda: FakeGateway(challenge(pay_to=TREASURY, fee_to=TREASURY)), sdk.FeeSplitMismatchError),
        (lambda: FakeGateway(challenge_header="%%%not-base64%%%"), sdk.InvalidChallengeError),
        (lambda: FakeGateway(challenge_header=base64.b64encode(b"not json").decode()), sdk.InvalidChallengeError),
        (lambda: FakeGateway(challenge_header=base64.b64encode(b"{}").decode()), sdk.InvalidChallengeError),
        (lambda: FakeGateway(challenge_header=_bad_header(lambda p: p.update(amount="-5"))), sdk.InvalidChallengeError),
        (lambda: FakeGateway(challenge_header=_bad_header(lambda p: p.update(amount="01500000000"))), sdk.InvalidChallengeError),
        (lambda: FakeGateway(challenge_header=_bad_header(lambda p: p.update(amount=1500000000))), sdk.InvalidChallengeError),
        (lambda: FakeGateway(challenge_header=_bad_header(lambda p: p.update(extra="x"))), sdk.InvalidChallengeError),
        (lambda: FakeGateway(challenge_header=_bad_header(lambda p: p.update(payTo="kaspatest:qq"))), sdk.InvalidChallengeError),
        (lambda: FakeGateway(challenge_header="A" * 200_000), sdk.InvalidChallengeError),
    ],
)
async def test_bad_challenges_are_rejected_without_signing(make_client, make_gateway, error) -> None:
    gateway = make_gateway()
    client = make_client(gateway)
    with pytest.raises(error):
        await client.get("/p/demo")
    assert_untouched(client, gateway)


async def test_pinned_fee_to_mismatch_is_refused(make_client) -> None:
    gateway = FakeGateway(challenge(fee_to=OTHER))
    client = make_client(gateway, pinned_fee_to=TREASURY)
    with pytest.raises(sdk.UnexpectedRecipientError):
        await client.get("/p/demo")
    assert_untouched(client, gateway)


async def test_expected_pay_to_mismatch_is_refused(make_client) -> None:
    gateway = FakeGateway(challenge(pay_to=OTHER))
    client = make_client(gateway)
    with pytest.raises(sdk.UnexpectedRecipientError):
        await client.get("/p/demo", expected_pay_to=MERCHANT)
    assert_untouched(client, gateway)


async def test_mainnet_client_refuses_testnet_challenge(make_client) -> None:
    gateway = FakeGateway(challenge())
    client = make_client(gateway, network="kaspa:mainnet")
    with pytest.raises(sdk.NetworkMismatchError):
        await client.get("/p/demo")
    assert_untouched(client, gateway)
    assert client.address.startswith("kaspa:")


# ------------------------------------------------------------------ response handling


async def test_retry_402_after_payment_raises_and_never_pays_twice(make_client) -> None:
    gateway = FakeGateway()

    def reject(request, parsed):
        return httpx.Response(
            402,
            headers={"PAYMENT-REQUIRED": gateway.required.to_header()},
            json={"error": "underpaid", "message": "short", "accepts": None},
        )

    gateway.paid_handler = reject
    client = make_client(gateway)
    with pytest.raises(sdk.PaymentRejectedError) as caught:
        await client.get("/p/demo")
    assert caught.value.code == "underpaid" and caught.value.status_code == 402
    assert len(gateway.signatures) == 1  # exactly one payment attempt


async def test_409_raises_replayed(make_client) -> None:
    gateway = FakeGateway()
    gateway.paid_handler = lambda r, p: httpx.Response(409, json={"error": "payment_replayed", "message": "used"})
    client = make_client(gateway)
    with pytest.raises(sdk.PaymentReplayedError) as caught:
        await client.get("/p/demo")
    assert caught.value.code == "payment_replayed" and caught.value.status_code == 409


async def test_receipt_for_another_tx_is_rejected(make_client) -> None:
    gateway = FakeGateway()
    other_receipt = sdk.PaymentResponse(
        txId="cd" * 32, network="kaspa:testnet-10", usdValueAtSettlement="1.00000000"
    ).to_header()
    gateway.paid_handler = lambda r, p: httpx.Response(200, headers={"PAYMENT-RESPONSE": other_receipt}, json={})
    client = make_client(gateway)
    with pytest.raises(sdk.PaymentReceiptError) as caught:
        await client.get("/p/demo")
    assert caught.value.tx_id == gateway.signatures[0]["signature"].txId
    assert caught.value.response is not None and caught.value.response.status_code == 200


async def test_success_without_receipt_header_is_an_error(make_client) -> None:
    gateway = FakeGateway()
    gateway.paid_handler = lambda r, p: httpx.Response(200, json={"data": 1})
    client = make_client(gateway)
    with pytest.raises(sdk.PaymentReceiptError):
        await client.get("/p/demo")


async def test_garbage_receipt_header_is_an_error(make_client) -> None:
    gateway = FakeGateway()
    gateway.paid_handler = lambda r, p: httpx.Response(200, headers={"PAYMENT-RESPONSE": "###"}, json={})
    client = make_client(gateway)
    with pytest.raises(sdk.PaymentReceiptError):
        await client.get("/p/demo")


async def test_upstream_502_with_receipt_returns_response_and_receipt(make_client) -> None:
    gateway = FakeGateway()

    def upstream_down(request, parsed):
        receipt = sdk.PaymentResponse(
            txId=parsed["signature"].txId, network="kaspa:testnet-10", usdValueAtSettlement="0.00000000"
        ).to_header()
        return httpx.Response(
            502, headers={"PAYMENT-RESPONSE": receipt}, json={"error": "upstream_error", "message": "down"}
        )

    gateway.paid_handler = upstream_down
    client = make_client(gateway)
    response = await client.get("/p/demo")
    assert response.status_code == 502
    assert receipt_of(response) is not None


async def test_503_rpc_unavailable_retries_once_with_the_same_signature(make_client) -> None:
    gateway = FakeGateway()
    attempts: list[str] = []

    def flaky(request, parsed):
        attempts.append(request.headers["PAYMENT-SIGNATURE"])
        if len(attempts) == 1:
            return httpx.Response(
                503, headers={"Retry-After": "0"}, json={"error": "rpc_unavailable", "message": "node down"}
            )
        receipt = sdk.PaymentResponse(
            txId=parsed["signature"].txId, network="kaspa:testnet-10", usdValueAtSettlement="2.00000000"
        ).to_header()
        return httpx.Response(200, headers={"PAYMENT-RESPONSE": receipt}, json={"ok": 1})

    gateway.paid_handler = flaky
    client = make_client(gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    assert response.status_code == 200
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert receipt_of(response) is not None


async def test_503_with_current_gateway_code_tx_rejected_and_retry_after_is_also_retried(make_client) -> None:
    gateway = FakeGateway()
    attempts: list[int] = []

    def flaky(request, parsed):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(503, headers={"Retry-After": "5"}, json={"error": "tx_rejected", "message": "n/a"})
        receipt = sdk.PaymentResponse(
            txId=parsed["signature"].txId, network="kaspa:testnet-10", usdValueAtSettlement="0.00000000"
        ).to_header()
        return httpx.Response(200, headers={"PAYMENT-RESPONSE": receipt}, json={})

    gateway.paid_handler = flaky
    client = make_client(gateway, unavailable_retry_delay=0)
    assert (await client.get("/p/demo")).status_code == 200
    assert len(attempts) == 2


async def test_persistent_503_is_retried_only_once_then_raises(make_client) -> None:
    gateway = FakeGateway()
    gateway.paid_handler = lambda r, p: httpx.Response(
        503, headers={"Retry-After": "0"}, json={"error": "rpc_unavailable", "message": "down"}
    )
    client = make_client(gateway, unavailable_retry_delay=0)
    with pytest.raises(sdk.PaymentRejectedError) as caught:
        await client.get("/p/demo")
    assert caught.value.status_code == 503
    assert len(gateway.signatures) == 2  # original + exactly one retry, same signature


async def test_non_retryable_503_is_not_retried(make_client) -> None:
    gateway = FakeGateway()
    gateway.paid_handler = lambda r, p: httpx.Response(
        503, json={"error": "service_unavailable", "message": "accepted but not recorded"}
    )
    client = make_client(gateway, unavailable_retry_delay=0)
    with pytest.raises(sdk.PaymentRejectedError):
        await client.get("/p/demo")
    assert len(gateway.signatures) == 1


# ------------------------------------------------------------------ funds, fees, ledger


async def test_insufficient_funds(gateway, make_client) -> None:
    client = make_client(gateway, utxos=[make_utxo(0, 5 * KAS)])
    with pytest.raises(sdk.InsufficientFundsError):
        await client.get("/p/demo")
    assert gateway.signatures == []


async def test_no_utxos(gateway, make_client) -> None:
    client = make_client(gateway, utxos=[])
    with pytest.raises(sdk.InsufficientFundsError):
        await client.get("/p/demo")


async def test_network_fee_ceiling(gateway, make_client) -> None:
    client = make_client(gateway, max_network_fee_sompi=1)
    with pytest.raises(sdk.NetworkFeeTooHighError):
        await client.get("/p/demo")
    assert gateway.signatures == []


async def test_signature_header_size_limit(gateway, make_client) -> None:
    client = make_client(gateway, max_signature_header_bytes=1_000)
    with pytest.raises(sdk.TransactionBuildError):
        await client.get("/p/demo")
    assert gateway.signatures == []


async def test_largest_utxo_is_preferred_to_keep_header_small(gateway, make_client) -> None:
    utxos = [make_utxo(i, 20 * KAS) for i in range(1, 30)] + [make_utxo(0, 500 * KAS)]
    client = make_client(gateway, utxos=utxos)
    await client.get("/p/demo")
    tx = gateway.signatures[0]["tx"]
    assert len(tx.inputs) == 1
    assert len(gateway.requests[1].headers["PAYMENT-SIGNATURE"]) < sdk.client.DEFAULT_MAX_SIGNATURE_HEADER_BYTES


async def test_sequential_payments_never_reuse_inputs_even_when_rpc_is_stale(gateway, make_client) -> None:
    client = make_client(gateway, utxos=[make_utxo(0, 100 * KAS)])  # RPC keeps returning the same single UTXO
    await client.get("/p/a")
    await client.get("/p/b")
    first, second = (s["tx"] for s in gateway.signatures)
    first_inputs = {(str(i.previous_outpoint.transaction_id), i.previous_outpoint.index) for i in first.inputs}
    second_inputs = {(str(i.previous_outpoint.transaction_id), i.previous_outpoint.index) for i in second.inputs}
    assert first_inputs.isdisjoint(second_inputs)
    assert second_inputs == {(first.id, 2)}  # spends the unconfirmed change of the first payment


async def test_concurrent_calls_are_serialised_and_use_disjoint_inputs(gateway, make_client) -> None:
    utxos = [make_utxo(i, 100 * KAS) for i in range(6)]
    client = make_client(gateway, utxos=utxos)
    responses = await asyncio.gather(*(client.get(f"/p/demo/{i}") for i in range(5)))
    assert [r.status_code for r in responses] == [200] * 5
    txs = [s["tx"] for s in gateway.signatures]
    assert len({t.id for t in txs}) == 5
    seen: set[tuple[str, int]] = set()
    for tx in txs:
        for i in tx.inputs:
            outpoint = (str(i.previous_outpoint.transaction_id), i.previous_outpoint.index)
            assert outpoint not in seen
            seen.add(outpoint)


async def test_timeout_during_paid_retry_quarantines_inputs(make_client) -> None:
    gateway = FakeGateway()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if "PAYMENT-SIGNATURE" in request.headers:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(402, headers={"PAYMENT-REQUIRED": gateway.required.to_header()}, json={})

    client = make_client(gateway, utxos=[make_utxo(0, 100 * KAS)])
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x")  # noqa: SLF001
    with pytest.raises(httpx.ReadTimeout):
        await client.get("/p/demo")
    # the inputs may have been spent: the next payment must not touch them
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(gateway), base_url="http://x")  # noqa: SLF001
    with pytest.raises(sdk.InsufficientFundsError):
        await client.get("/p/demo")


# ------------------------------------------------------------------ keys, sync wrapper


def test_private_key_is_validated_and_never_echoed() -> None:
    secret = "zz" * 32
    with pytest.raises(sdk.ConfigurationError) as caught:
        KasGateClient(secret)
    assert secret not in str(caught.value)
    with pytest.raises(sdk.ConfigurationError):
        KasGateClient("00" * 32)  # outside the secp256k1 range
    with pytest.raises(sdk.ConfigurationError):
        KasGateClient(PAYER_KEY, network="kaspa:devnet")


def test_repr_does_not_leak_the_key(gateway) -> None:
    client = KasGateClient(PAYER_KEY, utxo_provider=lambda a: None)  # type: ignore[arg-type,return-value]
    text = repr(client)
    assert PAYER_KEY not in text and PAYER in text
    assert client.address == PAYER


def test_accepts_0x_prefixed_key() -> None:
    assert KasGateClient("0x" + PAYER_KEY, utxo_provider=lambda a: None).address == PAYER  # type: ignore[arg-type,return-value]


def test_sync_wrapper_pays_and_returns_receipt(gateway) -> None:
    from conftest import static_utxos

    http = httpx.AsyncClient(transport=httpx.MockTransport(gateway), base_url="http://gateway.test")
    with KasGateSyncClient(
        PAYER_KEY, utxo_provider=static_utxos([make_utxo(0, 100 * KAS)]), http_client=http
    ) as client:
        response = client.get("/p/demo", max_price_kas="20")
        assert response.status_code == 200
        assert receipt_of(response).tx_id == gateway.signatures[0]["signature"].txId  # type: ignore[union-attr]
        with pytest.raises(sdk.PriceCapExceededError):
            client.get("/p/demo", max_price_kas="1")
        assert client.address == PAYER
