"""CONTRACT v2.13 prepaid refunds from the buyer side, against a fake gateway built on the gateway's own models."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import kaspa
import pytest
from app import schemas as gw
from conftest import PAYER, PAYER_KEY

import kasgate_client as sdk
from kasgate_client import (
    InsufficientPrepaidBalanceError,
    InvalidChallengeError,
    KasGateClient,
    PaymentReceiptError,
    PaymentRejectedError,
    PrepaidRefund,
)

MERCHANT_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
NONCE = "0123456789abcdef" * 2
SPENDABLE = 40_000_000


def xonly(key_hex: str) -> str:
    return kaspa.PrivateKey(key_hex).to_public_key().to_x_only_public_key().to_string()



def refunds_read_is_signed(request: httpx.Request) -> bool:
    """What the gateway checks on GET /v1/prepaid/refunds/status."""
    stamp = request.headers.get(gw.HEADER_BALANCE_TIMESTAMP)
    signature = request.headers.get(gw.HEADER_BALANCE_SIGNATURE)
    if stamp is None or signature is None:
        return False
    message = gw.build_prepaid_refunds_read_message(
        domain=request.url.netloc.decode("ascii").lower(),
        address=request.url.params["payer_address"],
        slug=request.url.params["slug"],
        timestamp=int(stamp),
    )
    return bool(kaspa.verify_message(message, signature, kaspa.PrivateKey(PAYER_KEY).to_public_key()))

class RefundGateway:
    """Fake /v1/prepaid/refunds*: validates the SDK's bodies with the gateway's strict models and builds the
    challenge text with the gateway's builder, so a drifting SDK fails here."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.overrides: dict[str, Any] = {}
        self.message_override: str | None = None
        self.challenge_error: tuple[int, str] | None = None
        self.amount_override: int | None = None
        self.refund_id = uuid.uuid4()
        self.last: dict[str, Any] | None = None
        self.refunds: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/prepaid/refunds/challenge":
            return self.challenge(json.loads(request.content))
        if path == "/v1/prepaid/refunds/status" and request.method == "POST":
            body = gw.PrepaidRefundSigned.model_validate(json.loads(request.content))
            assert self.last is not None and self.last["action"] == "status"
            assert kaspa.verify_message(self.last["message"], body.signature, kaspa.PublicKey(xonly(PAYER_KEY)))
            return httpx.Response(200, json=[{**row, "reason": "promo credit"} for row in self.refunds])
        if path == "/v1/prepaid/refunds/status":
            if not refunds_read_is_signed(request):
                return httpx.Response(401, json={"error": "invalid_balance_signature", "message": "unsigned"})
            return httpx.Response(200, json=self.refunds)
        if path == "/v1/prepaid/refunds" or path.endswith("/cancel"):
            return self.signed(request)
        return httpx.Response(404, json={"detail": "not found"})

    def challenge(self, body: dict[str, Any]) -> httpx.Response:
        parsed = gw.PrepaidRefundChallengeRequest.model_validate(body)
        if self.challenge_error is not None:
            status, code = self.challenge_error
            return httpx.Response(status, json={"error": code, "message": "no"})
        amount = parsed.amount_sompi if parsed.amount_sompi is not None else SPENDABLE
        if parsed.action == "cancel":
            amount = 25_000_000
        if parsed.action == "status":
            amount = 0
        expires = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=60)
        fields: dict[str, Any] = {
            "action": parsed.action,
            "address": parsed.payer_address,
            "merchant_id": MERCHANT_ID,
            "amount_sompi": amount,
            "refund_id": parsed.refund_id,
            "nonce": NONCE,
            "expires_at": expires,
        }
        message = gw.build_prepaid_refund_message(domain="gateway.test", **fields)
        payload = {
            "nonce": NONCE,
            "message": self.message_override or message,
            "action": parsed.action,
            "slug": parsed.slug,
            "merchant_id": str(MERCHANT_ID),
            "payer_address": parsed.payer_address,
            "amount_sompi": str(amount),
            "refund_id": None if parsed.refund_id is None else str(parsed.refund_id),
            "spendable_sompi": str(SPENDABLE),
            "expires_at": expires.isoformat(),
        } | self.overrides
        self.last = {"message": message, "amount": amount, "action": parsed.action}
        return httpx.Response(201, json=payload)

    def signed(self, request: httpx.Request) -> httpx.Response:
        body = gw.PrepaidRefundSigned.model_validate(json.loads(request.content))
        assert self.last is not None
        assert kaspa.verify_message(self.last["message"], body.signature, kaspa.PublicKey(xonly(PAYER_KEY)))
        now = datetime.now(UTC)
        cancelled = self.last["action"] == "cancel"
        refund = {
            "id": str(self.refund_id),
            "merchant_id": str(MERCHANT_ID),
            "payer_address": PAYER,
            "amount_sompi": str(self.amount_override or self.last["amount"]),
            "status": "cancelled" if cancelled else "requested",
            "tx_id": None,
            "reason": None,
            "last_error": None,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
            "payout_submitted_at": None,
            "paid_at": None,
            "resolved_at": now.isoformat() if cancelled else None,
            "payout_address": PAYER,
            "payout_payload_hex": gw.refund_payout_payload(self.refund_id).hex(),
            "payout_payload": gw.refund_payout_payload(self.refund_id).decode(),
            "claimed_at": None,
            "late_paid_from": None,
            "shortfall_sompi": "0",
        }
        self.refunds.insert(0, refund)
        return httpx.Response(200 if cancelled else 201, json=refund)


@pytest.fixture
def rgw() -> RefundGateway:
    return RefundGateway()


def paths(gateway: RefundGateway) -> list[str]:
    return [r.url.path for r in gateway.requests]


async def test_request_all_spendable_credit(rgw, make_client) -> None:
    client: KasGateClient = make_client(rgw)
    refund = await client.prepaid_refund_request("http://gateway.test", "weather")
    assert isinstance(refund, PrepaidRefund)
    assert (refund.status, int(refund.amount_sompi), refund.payout_address) == ("requested", SPENDABLE, PAYER)
    assert paths(rgw) == ["/v1/prepaid/refunds/challenge", "/v1/prepaid/refunds"]
    body = json.loads(rgw.requests[0].content)
    assert body == {"slug": "weather", "payer_address": PAYER, "action": "request"}


async def test_request_a_partial_amount_and_list_and_cancel(rgw, make_client) -> None:
    client: KasGateClient = make_client(rgw)
    refund = await client.prepaid_refund_request("http://gateway.test", "weather", 25_000_000)
    assert int(refund.amount_sompi) == 25_000_000
    assert json.loads(rgw.requests[0].content)["amount_sompi"] == 25_000_000
    cancelled = await client.prepaid_refund_cancel("http://gateway.test", "weather", refund.id)
    assert cancelled.status == "cancelled"
    assert rgw.requests[-1].url.path == f"/v1/prepaid/refunds/{refund.id}/cancel"
    listed = await client.prepaid_refunds("http://gateway.test", "weather")
    assert [r.status for r in listed] == ["cancelled", "requested"]
    assert rgw.requests[-1].url.params["payer_address"] == PAYER


@pytest.mark.parametrize(
    ("override", "needle"),
    [
        ({"payer_address": "kaspatest:qz0000000000000000000000000000000000000000000000000000000000x"}, "payer"),
        ({"action": "cancel"}, "action"),
        ({"slug": "other"}, "endpoint"),
        ({"amount_sompi": "30000000"}, "30000000"),
        ({"expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}, "expired"),
    ],
)
async def test_mismatched_challenges_are_never_signed(rgw, make_client, override, needle) -> None:
    client: KasGateClient = make_client(rgw)
    rgw.overrides = override
    with pytest.raises(InvalidChallengeError, match=needle):
        await client.prepaid_refund_request("http://gateway.test", "weather", 25_000_000)
    assert paths(rgw) == ["/v1/prepaid/refunds/challenge"]


async def test_tampered_text_or_overreach_is_never_signed(rgw, make_client) -> None:
    client: KasGateClient = make_client(rgw)
    rgw.message_override = "KasGate\nsomething else"
    with pytest.raises(InvalidChallengeError, match="does not match"):
        await client.prepaid_refund_request("http://gateway.test", "weather")
    rgw.message_override = None
    rgw.overrides = {"amount_sompi": str(SPENDABLE + 1)}
    with pytest.raises(InvalidChallengeError, match="more than the spendable"):
        await client.prepaid_refund_request("http://gateway.test", "weather")
    assert "/v1/prepaid/refunds" not in paths(rgw)


async def test_gateway_errors_and_bad_results(rgw, make_client) -> None:
    client: KasGateClient = make_client(rgw)
    rgw.challenge_error = (409, "insufficient_balance")
    with pytest.raises(InsufficientPrepaidBalanceError):
        await client.prepaid_refund_request("http://gateway.test", "weather")
    rgw.challenge_error = (422, "refund_too_small")
    with pytest.raises(PaymentRejectedError) as info:
        await client.prepaid_refund_request("http://gateway.test", "weather")
    assert info.value.code == "refund_too_small"
    rgw.challenge_error = None
    rgw.amount_override = 1
    with pytest.raises(PaymentReceiptError):
        await client.prepaid_refund_request("http://gateway.test", "weather")
    with pytest.raises(sdk.ConfigurationError):
        await client.prepaid_refund_request("http://gateway.test", "weather", 0)


def test_message_builder_matches_the_gateway() -> None:
    expires = datetime(2026, 1, 1, tzinfo=UTC)
    refund_id = uuid.uuid4()
    for action, rid in (("request", None), ("cancel", refund_id)):
        args: dict[str, Any] = {
            "domain": "gateway.test",
            "action": action,
            "address": PAYER,
            "merchant_id": MERCHANT_ID,
            "amount_sompi": 5,
            "refund_id": rid,
            "nonce": NONCE,
            "expires_at": expires,
        }
        assert sdk.build_prepaid_refund_message(**args) == gw.build_prepaid_refund_message(**args)


async def test_balance_reads_the_v2_13_fields(make_client) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "merchant_id": str(MERCHANT_ID),
                "payer_address": PAYER,
                "balance_sompi": "40000000",
                "deposited_sompi": "50000000",
                "debited_sompi": "5000000",
                "refunded_sompi": "5000000",
                "expired_sompi": "0",
                "refund_hold_sompi": "20000000",
                "spendable_sompi": "20000000",
                "expires_at": "2027-01-01T00:00:00+00:00",
                "updated_at": None,
            },
        )

    client: KasGateClient = make_client(handler)
    balance = await client.prepaid_balance("http://gateway.test", "weather")
    assert (balance.refund_hold_sompi, balance.spendable_sompi) == ("20000000", "20000000")
    assert balance.expires_at is not None and balance.expires_at.year == 2027


async def test_signed_status_returns_reasons_and_checks_the_challenge(rgw, make_client) -> None:
    client: KasGateClient = make_client(rgw)
    await client.prepaid_refund_request("http://gateway.test", "weather")
    public = await client.prepaid_refunds("http://gateway.test", "weather")
    assert public[0].reason is None
    mine = await client.prepaid_refunds("http://gateway.test", "weather", signed=True)
    assert mine[0].reason == "promo credit" and mine[0].shortfall_sompi == "0"
    assert rgw.last is not None and "Action: status" in rgw.last["message"] and "Amount: 0" in rgw.last["message"]
    rgw.overrides = {"amount_sompi": "1"}  # a status challenge that names an amount is never signed
    with pytest.raises(InvalidChallengeError):
        await client.prepaid_refunds("http://gateway.test", "weather", signed=True)


def test_paying_and_late_payout_fields_decode() -> None:
    now = datetime.now(UTC).isoformat()
    refund = PrepaidRefund.model_validate(
        {
            "id": str(uuid.uuid4()),
            "merchant_id": str(MERCHANT_ID),
            "payer_address": PAYER,
            "amount_sompi": "25000000",
            "status": "paying",
            "created_at": now,
            "updated_at": now,
            "claimed_at": now,
            "first_claimed_at": now,
            "claim_expires_at": now,
            "hold_until": None,
            "payout_address": PAYER,
            "payout_payload_hex": "00",
            "late_paid_from": None,
            "shortfall_sompi": "0",
        }
    )
    assert refund.status == "paying" and refund.claimed_at is not None
    assert refund.claim_expires_at is not None and refund.first_claimed_at is not None and refund.hold_until is None
