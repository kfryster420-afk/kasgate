"""CONTRACT v2.12 free trial calls from the buyer side, against a fake gateway built on the gateway's own models."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import kaspa
import pytest
from app import schemas as gw
from app import webhooks as gw_webhooks
from conftest import PAYER, PAYER_KEY, FakeGateway

import kasgate_client as sdk
from kasgate_client import (
    InvalidChallengeError,
    KasGateClient,
    PaymentReceiptError,
    PaymentRejectedError,
    receipt_of,
    trial_receipt_of,
)

MERCHANT_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
TRIAL_NONCE = "fedcba9876543210" * 2


def xonly(key_hex: str) -> str:
    return kaspa.PrivateKey(key_hex).to_public_key().to_x_only_public_key().to_string()


class TrialGateway(FakeGateway):
    """FakeGateway (on-chain 402 + paid route) plus /v1/trials/*. The trial text is built with the GATEWAY's
    function, so an SDK that drifts from the contract text fails here."""

    def __init__(self, trials: int = 2) -> None:
        super().__init__()
        self.trials = trials
        self.used = 0
        self.trial_requests: list[httpx.Request] = []
        self.messages: dict[str, str] = {}
        self.message_override: str | None = None
        self.challenge_overrides: dict[str, Any] = {}
        self.call_status = 200
        self.receipt_status = "used"
        self.send_receipt = True
        self.receipt_nonce: str | None = None
        self.trial_reply: int | None = None  # force a status on the trial call (no receipt)
        self.counter = 0

    def offer_headers(self) -> dict[str, str]:
        if self.used >= self.trials:
            return {}
        offer = gw.TrialOffer(
            freeTrialCalls=self.trials,
            challengeUrl="http://gateway.test/v1/trials/challenge",
            statusUrl="http://gateway.test/v1/trials/status?slug=weather",
        )
        return {"X-KasGate-Trial-Offer": offer.to_header()}

    def payment_402(self, error: str = "payment_required") -> httpx.Response:
        headers = {"PAYMENT-REQUIRED": self.required.to_header(), **self.offer_headers()}
        return httpx.Response(402, headers=headers, json={"error": error, "message": error, "accepts": None})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/trials/challenge":
            self.trial_requests.append(request)
            return self.challenge(json.loads(request.content))
        if path == "/v1/trials/status":
            self.trial_requests.append(request)
            return httpx.Response(
                200,
                json={
                    "slug": request.url.params["slug"],
                    "merchant_id": str(MERCHANT_ID),
                    "payer_address": request.url.params["payer_address"],
                    "free_trial_calls": self.trials,
                    "calls_used": self.used,
                    "remaining": max(self.trials - self.used, 0),
                },
            )
        raw = request.headers.get("X-KasGate-Trial")
        if raw is not None:
            self.trial_requests.append(request)
            return self.trial_call(request, raw)
        if path.startswith("/p/") and "PAYMENT-SIGNATURE" not in request.headers:
            self.requests.append(request)
            return self.payment_402()
        return super().__call__(request)

    def challenge(self, body: dict[str, Any]) -> httpx.Response:
        parsed = gw.TrialChallengeRequest.model_validate(body)  # the strict gateway model accepts the SDK request
        if self.used >= self.trials:
            return httpx.Response(402, json={"error": "trial_exhausted", "message": "used up", "accepts": None})
        self.counter += 1
        nonce = f"{self.counter:032x}"
        expires = (datetime.now(UTC) + timedelta(seconds=60)).replace(microsecond=0)
        fields: dict[str, Any] = {
            "nonce": nonce,
            "slug": parsed.slug,
            "merchant_id": MERCHANT_ID,
            "payer_address": parsed.payer_address,
            "method": parsed.method,
            "path": parsed.path,
            "body_hash": parsed.body_hash,
            "expires_at": expires,
        }
        message = gw.build_trial_call_message(
            domain="gateway.test",
            address=parsed.payer_address,
            merchant_id=MERCHANT_ID,
            slug=parsed.slug,
            method=parsed.method,
            path=parsed.path,
            body_hash=parsed.body_hash,
            nonce=nonce,
            expires_at=expires,
        )
        self.messages[nonce] = message
        fields.update(self.challenge_overrides)
        payload = gw.TrialChallenge(
            message=self.message_override or message,
            free_trial_calls=self.trials,
            calls_used=self.used,
            remaining=self.trials - self.used,
            **fields,
        )
        return httpx.Response(201, content=payload.model_dump_json(), headers={"content-type": "application/json"})

    def trial_call(self, request: httpx.Request, raw: str) -> httpx.Response:
        header = gw.TrialCallHeader.from_header(raw)
        message = self.messages.pop(header.nonce)
        assert kaspa.verify_message(message, header.signature, kaspa.PublicKey(xonly(PAYER_KEY)))
        if self.trial_reply is not None:
            if self.trial_reply == 402:
                return self.payment_402("trial_exhausted")
            return httpx.Response(self.trial_reply, json={"error": "rate_limited", "message": "slow", "accepts": None})
        if self.used >= self.trials:
            return self.payment_402("trial_exhausted")
        self.used += 1
        headers: dict[str, str] = {}
        if self.send_receipt:
            if self.receipt_status == "refunded":
                self.used -= 1
            headers["X-KasGate-Trial-Receipt"] = gw.TrialReceipt(
                trialCallId=uuid.uuid4(),
                nonce=self.receipt_nonce or header.nonce,
                limit=self.trials,
                remaining=self.trials - self.used,
                status=self.receipt_status,
            ).to_header()
        return httpx.Response(self.call_status, headers=headers, json={"ok": True, "trial": True})


@pytest.fixture
def tgw() -> TrialGateway:
    return TrialGateway()


# ------------------------------------------------------------------ automatic trials


async def test_request_uses_free_trials_before_paying(tgw: TrialGateway, make_client: Any) -> None:
    client: KasGateClient = make_client(tgw)
    for remaining in (1, 0):
        response = await client.get("/p/weather?city=hanoi")
        assert response.status_code == 200
        assert response.json() == {"ok": True, "trial": True}
        receipt = trial_receipt_of(response)
        assert receipt is not None and (receipt.status, receipt.remaining) == ("used", remaining)
        assert receipt_of(response) is None
    assert tgw.signatures == []  # nothing paid on-chain
    assert client.provider.calls == []  # no UTXO was even fetched  # type: ignore[attr-defined]

    paid = await client.get("/p/weather?city=hanoi")
    assert paid.status_code == 200
    assert receipt_of(paid) is not None
    assert trial_receipt_of(paid) is None
    assert len(tgw.signatures) == 1


async def test_exhausted_during_call_pays_the_fresh_402(tgw: TrialGateway, make_client: Any) -> None:
    tgw.trial_reply = 402
    client = make_client(tgw)
    response = await client.get("/p/weather")
    assert response.status_code == 200
    assert receipt_of(response) is not None
    assert len(tgw.signatures) == 1


async def test_trial_rate_limit_falls_back_to_paying(tgw: TrialGateway, make_client: Any) -> None:
    tgw.trial_reply = 429
    client = make_client(tgw)
    response = await client.get("/p/weather")
    assert response.status_code == 200
    assert receipt_of(response) is not None


async def test_trials_can_be_switched_off(tgw: TrialGateway, make_client: Any) -> None:
    client = make_client(tgw, use_free_trials=False)
    response = await client.get("/p/weather")
    assert receipt_of(response) is not None
    assert tgw.trial_requests == []


async def test_no_offer_means_no_trial_request(tgw: TrialGateway, make_client: Any) -> None:
    tgw.trials = 0
    client = make_client(tgw)
    await client.get("/p/weather")
    assert tgw.trial_requests == []
    assert len(tgw.signatures) == 1


async def test_refunded_trial_is_returned_not_paid(tgw: TrialGateway, make_client: Any) -> None:
    tgw.call_status = 503
    tgw.receipt_status = "refunded"
    client = make_client(tgw)
    response = await client.get("/p/weather")
    assert response.status_code == 503
    receipt = trial_receipt_of(response)
    assert receipt is not None and receipt.status == "refunded"
    assert tgw.signatures == []


async def test_post_body_is_bound(tgw: TrialGateway, make_client: Any) -> None:
    client = make_client(tgw)
    response = await client.post("/p/weather", content=b'{"q":1}')
    assert response.status_code == 200
    body = json.loads(tgw.trial_requests[0].content)
    assert body["body_hash"] == gw.prepaid_body_hash(b'{"q":1}')
    assert body["method"] == "POST"


# ------------------------------------------------------------------ never sign a foreign challenge


@pytest.mark.parametrize(
    "override",
    [
        {"path": "/p/weather/admin"},
        {"slug": "other"},
        {"method": "DELETE"},
        {"payer_address": "kaspatest:qqdcf32k0vfxgsyet5ldt246q4jaw8scx3sysx0lnstlt6w4m5rc7tsx6em34"},
        {"body_hash": "0" * 64},
        {"expires_at": datetime(2020, 1, 1, tzinfo=UTC)},
    ],
)
async def test_mismatching_challenge_is_never_signed(tgw: TrialGateway, make_client: Any, override: Any) -> None:
    tgw.challenge_overrides = override
    client = make_client(tgw)
    with pytest.raises(InvalidChallengeError):
        await client.get("/p/weather")
    assert not any("X-KasGate-Trial" in r.headers for r in tgw.trial_requests)
    assert tgw.signatures == []


async def test_tampered_message_is_never_signed(tgw: TrialGateway, make_client: Any) -> None:
    tgw.message_override = "KasGate\nPurpose: kasgate:v1:prepaid-debit\nAmount: 999999999"
    client = make_client(tgw)
    with pytest.raises(InvalidChallengeError):
        await client.trial_request("GET", "/p/weather")


async def test_receipt_for_another_nonce_is_an_error(tgw: TrialGateway, make_client: Any) -> None:
    tgw.receipt_nonce = "0" * 32
    client = make_client(tgw)
    with pytest.raises(PaymentReceiptError):
        await client.trial_request("GET", "/p/weather")


# ------------------------------------------------------------------ explicit API


async def test_trial_request_and_status(tgw: TrialGateway, make_client: Any) -> None:
    client = make_client(tgw)
    status = await client.trial_status("http://gateway.test", "weather")
    assert (status.free_trial_calls, status.remaining, status.payer_address) == (2, 2, PAYER)
    response = await client.trial_request("GET", "/p/weather")
    assert trial_receipt_of(response) is not None
    await client.trial_request("GET", "/p/weather")
    with pytest.raises(PaymentRejectedError) as excinfo:
        await client.trial_request("GET", "/p/weather")
    assert excinfo.value.code == "trial_exhausted"
    assert tgw.signatures == []
    assert (await client.trial_status("http://gateway.test", "weather")).remaining == 0


async def test_trial_request_without_receipt_raises(tgw: TrialGateway, make_client: Any) -> None:
    tgw.send_receipt = False
    client = make_client(tgw)
    with pytest.raises(PaymentReceiptError):
        await client.trial_request("GET", "/p/weather")


def test_message_matches_gateway_byte_for_byte() -> None:
    expires = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    kwargs: dict[str, Any] = {
        "domain": "gateway.test",
        "address": PAYER,
        "merchant_id": MERCHANT_ID,
        "slug": "weather",
        "method": "GET",
        "path": "/p/weather?x=1",
        "body_hash": "-",
        "nonce": TRIAL_NONCE,
        "expires_at": expires,
    }
    assert sdk.build_trial_call_message(**kwargs) == gw.build_trial_call_message(**kwargs)
    assert sdk.TRIAL_SCHEME == gw.TRIAL_SCHEME
    assert sdk.HEADER_TRIAL_CALL == gw.HEADER_TRIAL_CALL
    assert sdk.HEADER_TRIAL_OFFER == gw.HEADER_TRIAL_OFFER
    assert sdk.HEADER_TRIAL_RECEIPT == gw.HEADER_TRIAL_RECEIPT
    header = sdk.TrialCallHeader(nonce=TRIAL_NONCE, signature="ab" * 64).to_header()
    assert gw.TrialCallHeader.from_header(header).nonce == TRIAL_NONCE


# ------------------------------------------------------------------ webhook signature helper


def test_verify_webhook_signature_matches_the_gateway_signer() -> None:
    secret = "whsec_" + "Z" * 43
    body = b'{"api_version":"2.12","id":"evt_' + b"1" * 32 + b'","type":"payment.settled"}'
    now = 1_800_000_000
    header = gw_webhooks.signature_header(secret, now, body)
    assert sdk.verify_webhook_signature(secret, header, body, now=now)
    assert sdk.compute_webhook_signature(secret, now, body) == gw_webhooks.compute_signature(secret, now, body)
    assert not sdk.verify_webhook_signature(secret, header, body + b"\n", now=now)
    assert not sdk.verify_webhook_signature("whsec_other", header, body, now=now)
    assert not sdk.verify_webhook_signature(secret, header, body, now=now + 301)
    assert not sdk.verify_webhook_signature(secret, "t=1,v1=", body, now=now)
    assert sdk.WEBHOOK_SIGNATURE_HEADER == gw.WEBHOOK_SIGNATURE_HEADER
    assert sdk.WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS == gw.WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS
