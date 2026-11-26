"""CONTRACT v2.11 prepaid credit from the buyer side, against a fake gateway that checks with the gateway's own models."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import kaspa
import pytest
from app import schemas as gw
from conftest import (
    MERCHANT,
    NONCE,
    PAYER,
    PAYER_KEY,
    TREASURY,
    challenge,
    make_utxo,
    proof_verifies,
)

import kasgate_client as sdk
from kasgate_client import (
    InsufficientPrepaidBalanceError,
    InvalidChallengeError,
    KasGateClient,
    PaymentReceiptError,
    PaymentRejectedError,
    PaymentReplayedError,
    PrepaidDebitHeader,
    PrepaidReceipt,
    PriceCapExceededError,
    prepaid_receipt_of,
)

KAS = 100_000_000
MERCHANT_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
DEBIT_NONCE = "abcdef0123456789" * 2
DEPOSIT = 10 * KAS


def xonly(key_hex: str) -> str:
    return kaspa.PrivateKey(key_hex).to_public_key().to_x_only_public_key().to_string()



def balance_read_is_signed(request: httpx.Request) -> bool:
    """What the gateway checks on GET /v1/prepaid/balance before it shows the deposited/debited totals."""
    stamp = request.headers.get(gw.HEADER_BALANCE_TIMESTAMP)
    signature = request.headers.get(gw.HEADER_BALANCE_SIGNATURE)
    if stamp is None or signature is None:
        return False
    message = gw.build_prepaid_balance_read_message(
        domain=request.url.netloc.decode("ascii").lower(),
        address=request.url.params["payer_address"],
        slug=request.url.params["slug"],
        timestamp=int(stamp),
    )
    return bool(kaspa.verify_message(message, signature, kaspa.PrivateKey(PAYER_KEY).to_public_key()))

class PrepaidGateway:
    """Fake /v1/prepaid/* plus one /p/{slug} route. The debit message is built with the GATEWAY's function, so an SDK
    that drifts from the contract text fails here."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.price = 1000
        self.balance = 0
        self.challenge_overrides: dict[str, Any] = {}
        self.message_override: str | None = None
        self.challenge_status = 201
        self.call_status = 200
        self.receipt_status = "debited"
        self.send_receipt = True
        self.deposit_tx: str | None = None
        self.last_challenge: dict[str, Any] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/directory":
            return httpx.Response(404, json={"detail": "not found"})
        if path == "/v1/prepaid/deposits/quote":
            return self.quote(json.loads(request.content))
        if path == "/v1/prepaid/deposits":
            return self.deposit(request)
        if path == "/v1/prepaid/challenge":
            return self.challenge(json.loads(request.content))
        if path == "/v1/prepaid/balance":
            signed = balance_read_is_signed(request)
            return httpx.Response(
                200,
                json={
                    "merchant_id": str(MERCHANT_ID),
                    "payer_address": request.url.params["payer_address"],
                    "balance_sompi": str(self.balance),
                    "deposited_sompi": str(self.balance) if signed else None,
                    "debited_sompi": "0" if signed else None,
                    "updated_at": None,
                },
            )
        return self.call(request)

    def quote(self, body: dict[str, Any]) -> httpx.Response:
        gw.PrepaidDepositQuoteRequest.model_validate(body)
        required = challenge(body["amount_sompi"])
        merchant, fee = sdk.compute_fee_split(body["amount_sompi"])
        return httpx.Response(
            201,
            json={
                "slug": body["slug"],
                "merchant_id": str(MERCHANT_ID),
                "payer_address": body["payer_address"],
                "deposit_sompi": str(body["amount_sompi"]),
                "fee_sompi": str(fee),
                "credit_sompi": str(merchant),
                "balance_sompi": str(self.balance),
                "required": json.loads(required.model_dump_json()),
                "deposit_url": "http://gateway.test/v1/prepaid/deposits",
            },
        )

    def deposit(self, request: httpx.Request) -> httpx.Response:
        signature = gw.PaymentSignature.from_header(request.headers["PAYMENT-SIGNATURE"])
        assert proof_verifies(signature)
        tx = kaspa.Transaction.from_dict(json.loads(signature.rawTx))
        credit = tx.outputs[0].value
        self.balance += credit
        self.deposit_tx = signature.txId
        receipt = gw.PaymentResponse(txId=signature.txId, network="kaspa:testnet-10", usdValueAtSettlement="0")
        return httpx.Response(
            201,
            headers={"PAYMENT-RESPONSE": receipt.to_header()},
            json={
                "deposit_id": str(uuid.uuid4()),
                "tx_id": signature.txId,
                "slug": "weather",
                "merchant_id": str(MERCHANT_ID),
                "payer_address": signature.payerAddress,
                "deposit_sompi": str(credit + tx.outputs[1].value),
                "fee_sompi": str(tx.outputs[1].value),
                "credit_sompi": str(credit),
                "balance_sompi": str(self.balance),
                "created_at": datetime.now(UTC).isoformat(),
            },
        )

    def challenge(self, body: dict[str, Any]) -> httpx.Response:
        parsed = gw.PrepaidChallengeRequest.model_validate(body)
        if self.challenge_status != 201:
            return httpx.Response(
                self.challenge_status,
                json={"error": "insufficient_balance", "message": "balance too low", "accepts": None},
            )
        expires = datetime.now(UTC) + timedelta(seconds=60)
        fields: dict[str, Any] = {
            "nonce": DEBIT_NONCE,
            "slug": parsed.slug,
            "merchant_id": MERCHANT_ID,
            "payer_address": parsed.payer_address,
            "method": parsed.method,
            "path": parsed.path,
            "body_hash": parsed.body_hash,
            "amount_sompi": self.price,
            "expires_at": expires,
        }
        message = gw.build_prepaid_debit_message(domain="gateway.test", address=parsed.payer_address, **{
            k: fields[k] for k in ("merchant_id", "method", "path", "body_hash", "amount_sompi", "nonce", "expires_at")
        })
        fields.update(self.challenge_overrides)
        payload = gw.PrepaidChallenge(
            nonce=fields["nonce"],
            message=self.message_override or message,
            slug=fields["slug"],
            merchant_id=fields["merchant_id"],
            payer_address=fields["payer_address"],
            method=fields["method"],
            path=fields["path"],
            body_hash=fields["body_hash"],
            amount_sompi=str(fields["amount_sompi"]),
            kas_usd_rate=None,
            balance_sompi=str(self.balance),
            expires_at=fields["expires_at"],
        )
        self.last_challenge = {"message": message}
        return httpx.Response(201, content=payload.model_dump_json(), headers={"content-type": "application/json"})

    def call(self, request: httpx.Request) -> httpx.Response:
        raw = request.headers.get("X-KasGate-Prepaid")
        if raw is None:
            return httpx.Response(402, json={"error": "payment_required", "message": "pay", "accepts": None})
        header = gw.PrepaidDebitHeader.from_header(raw)
        assert self.last_challenge is not None
        assert kaspa.verify_message(self.last_challenge["message"], header.signature, kaspa.PublicKey(xonly(PAYER_KEY)))
        if self.call_status == 409:
            return httpx.Response(409, json={"error": "payment_replayed", "message": "used", "accepts": None})
        headers: dict[str, str] = {}
        if self.send_receipt:
            self.balance -= self.price if self.receipt_status == "debited" else 0
            headers["X-KasGate-Prepaid-Receipt"] = gw.PrepaidReceipt(
                debitId=uuid.uuid4(),
                nonce=header.nonce,
                amountSompi=str(self.price),
                balanceSompi=str(self.balance),
                status=self.receipt_status,
            ).to_header()
        return httpx.Response(self.call_status, headers=headers, json={"ok": True})


@pytest.fixture
def pgw() -> PrepaidGateway:
    return PrepaidGateway()


def calls(gateway: PrepaidGateway, path: str) -> list[httpx.Request]:
    return [r for r in gateway.requests if r.url.path == path]


# ------------------------------------------------------------------ deposits


async def test_deposit_pays_exact_split_and_returns_credit(pgw, make_client) -> None:
    client: KasGateClient = make_client(pgw)
    receipt = await client.prepaid_deposit("http://gateway.test", "weather", DEPOSIT)

    merchant, fee = sdk.compute_fee_split(DEPOSIT)
    assert fee == 20_000_000  # 1.5 % of 10 KAS is 0.15 KAS, below the 0.2 KAS floor
    assert (int(receipt.credit_sompi), int(receipt.fee_sompi)) == (merchant, fee)
    assert receipt.tx_id == pgw.deposit_tx
    deposit_request = calls(pgw, "/v1/prepaid/deposits")[0]
    tx = kaspa.Transaction.from_dict(
        json.loads(gw.PaymentSignature.from_header(deposit_request.headers["PAYMENT-SIGNATURE"]).rawTx)
    )
    assert tx.outputs[0].value == merchant
    assert tx.outputs[0].script_public_key.script == kaspa.pay_to_address_script(kaspa.Address(MERCHANT)).script
    assert tx.outputs[1].value == fee
    assert tx.outputs[1].script_public_key.script == kaspa.pay_to_address_script(kaspa.Address(TREASURY)).script
    assert bytes.fromhex(tx.payload) == gw.payment_payload(NONCE)
    quote_body = json.loads(calls(pgw, "/v1/prepaid/deposits/quote")[0].content)
    gw.PrepaidDepositQuoteRequest.model_validate(quote_body)  # strict gateway model accepts the SDK request


async def test_deposit_below_minimum_is_refused_locally(pgw, make_client) -> None:
    client = make_client(pgw)
    with pytest.raises(sdk.ConfigurationError):
        await client.prepaid_deposit("http://gateway.test", "weather", 59_999_999)
    assert calls(pgw, "/v1/prepaid/deposits/quote") == []


async def test_deposit_quote_for_more_than_asked_is_refused(pgw, make_client, monkeypatch) -> None:
    original = pgw.quote

    def inflated(body: dict[str, Any]) -> httpx.Response:
        return original({**body, "amount_sompi": body["amount_sompi"] * 2})

    monkeypatch.setattr(pgw, "quote", inflated)
    client = make_client(pgw)
    with pytest.raises(PriceCapExceededError):
        await client.prepaid_deposit("http://gateway.test", "weather", DEPOSIT)
    assert calls(pgw, "/v1/prepaid/deposits") == []
    assert client.provider.calls == []  # type: ignore[attr-defined]


async def test_deposit_to_unexpected_merchant_is_refused(pgw, make_client) -> None:
    client = make_client(pgw)
    with pytest.raises(sdk.UnexpectedRecipientError):
        await client.prepaid_deposit("http://gateway.test", "weather", DEPOSIT, expected_pay_to=TREASURY)
    assert calls(pgw, "/v1/prepaid/deposits") == []


async def test_deposit_receipt_for_another_tx_is_an_error(pgw, make_client, monkeypatch) -> None:
    original = pgw.deposit

    def lying(request: httpx.Request) -> httpx.Response:
        response = original(request)
        body = json.loads(response.content)
        body["tx_id"] = "ff" * 32
        return httpx.Response(201, headers=dict(response.headers), json=body)

    monkeypatch.setattr(pgw, "deposit", lying)
    client = make_client(pgw)
    with pytest.raises(PaymentReceiptError):
        await client.prepaid_deposit("http://gateway.test", "weather", DEPOSIT)


async def test_deposit_spent_utxos_are_not_reused(pgw, make_client) -> None:
    client = make_client(pgw, utxos=[make_utxo(0, 50 * KAS), make_utxo(1, 50 * KAS)])
    await client.prepaid_deposit("http://gateway.test", "weather", DEPOSIT)
    await client.prepaid_deposit("http://gateway.test", "weather", DEPOSIT)
    txs = [
        kaspa.Transaction.from_dict(json.loads(gw.PaymentSignature.from_header(r.headers["PAYMENT-SIGNATURE"]).rawTx))
        for r in calls(pgw, "/v1/prepaid/deposits")
    ]
    first = {(i.previous_outpoint.transaction_id, i.previous_outpoint.index) for i in txs[0].inputs}
    second = {(i.previous_outpoint.transaction_id, i.previous_outpoint.index) for i in txs[1].inputs}
    assert first.isdisjoint(second)


# ------------------------------------------------------------------ debits


async def test_prepaid_request_signs_the_gateway_message_and_attaches_receipt(pgw, make_client) -> None:
    pgw.balance = 5000
    client = make_client(pgw)
    response = await client.prepaid_request(
        "POST", "http://gateway.test/p/weather/today?city=hanoi", json={"q": 1}, max_price_kas="0.0001"
    )
    assert response.status_code == 200
    receipt = prepaid_receipt_of(response)
    assert isinstance(receipt, PrepaidReceipt)
    assert (receipt.status, receipt.amountSompi, receipt.balanceSompi, receipt.nonce) == (
        "debited",
        "1000",
        "4000",
        DEBIT_NONCE,
    )
    challenge_body = json.loads(calls(pgw, "/v1/prepaid/challenge")[0].content)
    paid = calls(pgw, "/p/weather/today")[0]
    assert challenge_body["path"] == "/p/weather/today?city=hanoi"
    assert challenge_body["method"] == "POST"
    assert challenge_body["body_hash"] == gw.prepaid_body_hash(paid.content) == sdk.prepaid_body_hash(paid.content)
    assert "PAYMENT-SIGNATURE" not in paid.headers
    assert client.provider.calls == []  # type: ignore[attr-defined]  # no UTXO touched for a prepaid call


async def test_prepaid_get_without_body_uses_dash(pgw, make_client) -> None:
    pgw.balance = 5000
    client = make_client(pgw)
    await client.prepaid_request("GET", "/p/weather")
    assert json.loads(calls(pgw, "/v1/prepaid/challenge")[0].content)["body_hash"] == "-"


def test_message_text_matches_gateway_byte_for_byte() -> None:
    args = {
        "domain": "gateway.test",
        "address": PAYER,
        "merchant_id": MERCHANT_ID,
        "method": "PUT",
        "path": "/p/x1/a?b=c",
        "body_hash": "ab" * 32,
        "amount_sompi": 123,
        "nonce": DEBIT_NONCE,
        "expires_at": datetime(2026, 11, 25, 10, 0, tzinfo=UTC),
    }
    assert sdk.build_prepaid_debit_message(**args) == gw.build_prepaid_debit_message(**args)
    assert PrepaidDebitHeader(nonce=DEBIT_NONCE, signature="ab" * 64).to_header() == gw.PrepaidDebitHeader(
        nonce=DEBIT_NONCE, signature="ab" * 64
    ).to_header()


async def test_price_above_cap_is_never_signed(pgw, make_client) -> None:
    pgw.balance = 10 * KAS
    pgw.price = 2 * KAS
    client = make_client(pgw)
    with pytest.raises(PriceCapExceededError):
        await client.prepaid_request("GET", "/p/weather", max_price_kas="1")
    assert calls(pgw, "/p/weather") == []


@pytest.mark.parametrize(
    "override",
    [
        {"path": "/p/weather/other"},
        {"method": "DELETE"},
        {"body_hash": "cd" * 32},
        {"payer_address": MERCHANT},
    ],
)
async def test_challenge_for_another_request_is_never_signed(pgw, make_client, override) -> None:
    pgw.balance = 5000
    pgw.challenge_overrides = override
    client = make_client(pgw)
    with pytest.raises(InvalidChallengeError):
        await client.prepaid_request("GET", "/p/weather")
    assert calls(pgw, "/p/weather") == []


async def test_tampered_message_text_is_never_signed(pgw, make_client) -> None:
    pgw.balance = 5000
    pgw.message_override = "KasGate\nDomain: evil.test\nPurpose: kasgate:v1:prepaid-debit"
    client = make_client(pgw)
    with pytest.raises(InvalidChallengeError):
        await client.prepaid_request("GET", "/p/weather")
    assert calls(pgw, "/p/weather") == []


async def test_expired_challenge_is_never_signed(pgw, make_client) -> None:
    pgw.balance = 5000
    pgw.challenge_overrides = {"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    client = make_client(pgw)
    with pytest.raises(InvalidChallengeError):
        await client.prepaid_request("GET", "/p/weather")


async def test_insufficient_balance_raises_typed_error(pgw, make_client) -> None:
    pgw.challenge_status = 402
    client = make_client(pgw)
    with pytest.raises(InsufficientPrepaidBalanceError) as info:
        await client.prepaid_request("GET", "/p/weather")
    assert info.value.code == "insufficient_balance"
    assert isinstance(info.value, PaymentRejectedError)


async def test_refunded_upstream_failure_returns_response_with_refund_receipt(pgw, make_client) -> None:
    pgw.balance = 5000
    pgw.call_status = 502
    pgw.receipt_status = "refunded"
    client = make_client(pgw)
    response = await client.prepaid_request("GET", "/p/weather")
    assert response.status_code == 502
    receipt = prepaid_receipt_of(response)
    assert receipt is not None and receipt.status == "refunded" and receipt.balanceSompi == "5000"
    assert len(calls(pgw, "/p/weather")) == 1  # never retried automatically


async def test_replayed_debit_raises(pgw, make_client) -> None:
    pgw.balance = 5000
    pgw.call_status = 409
    client = make_client(pgw)
    with pytest.raises(PaymentReplayedError):
        await client.prepaid_request("GET", "/p/weather")


async def test_success_without_receipt_is_an_error(pgw, make_client) -> None:
    pgw.balance = 5000
    pgw.send_receipt = False
    client = make_client(pgw)
    with pytest.raises(PaymentReceiptError):
        await client.prepaid_request("GET", "/p/weather")


async def test_non_proxy_path_is_refused(pgw, make_client) -> None:
    client = make_client(pgw)
    with pytest.raises(InvalidChallengeError):
        await client.prepaid_request("GET", "/v1/catalog")
    assert pgw.requests == []


async def test_balance_read(pgw, make_client) -> None:
    pgw.balance = 777
    client = make_client(pgw)
    balance = await client.prepaid_balance("http://gateway.test", "weather")
    assert balance.balance_sompi == "777" and balance.payer_address == PAYER
    assert (balance.deposited_sompi, balance.debited_sompi) == ("777", "0")  # the SDK signs, so totals come back
    [request] = [r for r in pgw.requests if r.url.path == "/v1/prepaid/balance"]
    stamp = int(request.headers[sdk.HEADER_BALANCE_TIMESTAMP])
    assert abs(stamp - int(datetime.now(UTC).timestamp())) <= 5


def test_unsigned_public_balance_parses_without_totals() -> None:
    balance = sdk.PrepaidBalance.model_validate(
        {"merchant_id": str(MERCHANT_ID), "payer_address": PAYER, "balance_sompi": "5", "updated_at": None}
        | {"deposited_sompi": None, "debited_sompi": None}
    )
    assert balance.balance_sompi == "5" and balance.deposited_sompi is None and balance.debited_sompi is None


def test_balance_read_message_and_headers_match_the_gateway() -> None:
    args = {"domain": "gw.example", "address": PAYER, "slug": "weather", "timestamp": 1_800_000_000}
    assert sdk.build_prepaid_balance_read_message(**args) == gw.build_prepaid_balance_read_message(**args)
    assert (sdk.HEADER_BALANCE_TIMESTAMP, sdk.HEADER_BALANCE_SIGNATURE) == (
        gw.HEADER_BALANCE_TIMESTAMP,
        gw.HEADER_BALANCE_SIGNATURE,
    )


def test_sync_wrapper_round_trip(pgw) -> None:
    from conftest import static_utxos

    with sdk.KasGateSyncClient(
        PAYER_KEY,
        utxo_provider=static_utxos([make_utxo(0, 500 * KAS)]),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(pgw), base_url="http://gateway.test"),
    ) as client:
        client.prepaid_deposit("http://gateway.test", "weather", DEPOSIT)
        assert int(client.prepaid_balance("http://gateway.test", "weather").balance_sompi) == pgw.balance
        response = client.prepaid_request("GET", "/p/weather")
    assert prepaid_receipt_of(response) is not None
