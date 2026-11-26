"""CONTRACT v2.14 `X-KasGate-Credit`: parsed as an unsigned hint, confirmed only by the gateway's SIGNED credit status
(`POST /v1/prepaid/credits/challenge` then `POST /v1/prepaid/credits/status`)."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import kaspa
import pytest
from app import schemas as gw
from conftest import PAYER, PAYER_KEY, PRICE, FakeGateway

import kasgate_client as sdk
from kasgate_client import (
    BuyerCreditStatus,
    CreditObservation,
    FailedDeliveryCreditHint,
    PaymentExpiredError,
    PaymentRejectedError,
    PaymentReplayedError,
    credit_of,
    judge_credit,
    observe_credit,
    parse_credit_header,
    receipt_of,
)
from kasgate_client.sync import KasGateSyncClient

MERCHANT_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
FEE = max(-(-PRICE * 150 // 10_000), 20_000_000)
MERCHANT_SHARE = PRICE - FEE
TX = "cd" * 32
NONCE = "0123456789abcdef" * 2
GATEWAY = "http://gateway.test"


def xonly(key_hex: str) -> str:
    return kaspa.PrivateKey(key_hex).to_public_key().to_x_only_public_key().to_string()


def receipt_header(
    tx_id: str = TX,
    *,
    credit: int = MERCHANT_SHARE,
    merchant: uuid.UUID = MERCHANT_ID,
    **extra: Any,
) -> str:
    fields: dict[str, Any] = {
        "creditId": uuid.uuid4(),
        "kind": "failed_delivery_credit",
        "txId": tx_id,
        "merchantId": merchant,
        "creditSompi": str(credit),
        "balanceSompi": str(credit),
        "reason": "attempts_exhausted",
    } | extra
    return gw.FailedDeliveryCreditReceipt(**fields).to_header()


def raw_header(payload: Any) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode()


def credit_item(
    tx_id: str = TX,
    *,
    slug: str = "demo",
    credit: int = MERCHANT_SHARE,
    reason: str = "attempts_exhausted",
) -> dict[str, Any]:
    return {
        "tx_id": tx_id,
        "slug": slug,
        "credit_sompi": str(credit),
        "shortfall_sompi": "0",
        "reason": reason,
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }


class CreditGateway(FakeGateway):
    """The paid route answers 502 with an X-KasGate-Credit header and (like the gateway) records the credit; the fake
    `/v1/prepaid/credits/challenge` + `/credits/status` validate the SDK's bodies with the gateway's own strict
    models, build the challenge text with the gateway's builder and check the signature."""

    def __init__(self) -> None:
        super().__init__()
        self.credit_header: str | None = None
        self.credit_for_tx: bool = True
        self.register_credit: bool = True
        self.credits: list[dict[str, Any]] = []  # newest first
        self.credit_count: int | None = None  # None = len(credits)
        self.credited_sompi = 0
        self.status_merchant = MERCHANT_ID
        self.challenge_error: tuple[int, str] | None = None
        self.status_error: tuple[int, str] | None = None
        self.challenge_overrides: dict[str, Any] = {}
        self.status_overrides: dict[str, Any] = {}
        self.message_override: str | None = None
        self.raw_status_body: Any = None
        self.used_nonces: set[str] = set()
        self.last: dict[str, Any] | None = None
        self.status_calls = 0
        self.challenge_calls = 0
        self.status_signatures: list[str] = []
        self.status = 502
        self.paid_handler = self.failing_upstream

    def failing_upstream(self, request: httpx.Request, parsed: dict[str, Any]) -> httpx.Response:
        tx_id = parsed["signature"].txId
        headers = {
            "PAYMENT-RESPONSE": sdk.PaymentResponse(
                txId=tx_id,
                network="kaspa:testnet-10",
                usdValueAtSettlement="0.00000000",
            ).to_header()
        }
        header = self.credit_header
        if header is None and self.credit_for_tx:
            header = receipt_header(tx_id)
        if header is not None:
            headers["X-KasGate-Credit"] = header
            if self.register_credit and self.credit_header is None and all(c["tx_id"] != tx_id for c in self.credits):
                self.credits.insert(0, credit_item(tx_id))
                self.credited_sompi += MERCHANT_SHARE
        return httpx.Response(
            self.status,
            headers=headers,
            json={"error": "upstream_error", "message": "down"},
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/prepaid/credits/challenge":
            return self.challenge(json.loads(request.content))
        if request.url.path == "/v1/prepaid/credits/status":
            return self.credit_status(json.loads(request.content))
        return super().__call__(request)

    def challenge(self, body: dict[str, Any]) -> httpx.Response:
        parsed = gw.PrepaidCreditStatusChallengeRequest.model_validate(body)
        self.challenge_calls += 1
        if self.challenge_error is not None:
            status, code = self.challenge_error
            return httpx.Response(status, json={"error": code, "message": "no"})
        expires = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=60)
        message = gw.build_prepaid_refund_message(
            domain="gateway.test",
            action="credits",
            address=parsed.payer_address,
            merchant_id=self.status_merchant,
            amount_sompi=0,
            refund_id=None,
            nonce=NONCE,
            expires_at=expires,
        )
        payload = {
            "nonce": NONCE,
            "message": self.message_override or message,
            "action": "credits",
            "slug": parsed.slug,
            "merchant_id": str(self.status_merchant),
            "payer_address": parsed.payer_address,
            "amount_sompi": "0",
            "refund_id": None,
            "spendable_sompi": "100",
            "refundable_sompi": "100",
            "expires_at": expires.isoformat(),
        } | self.challenge_overrides
        self.last = {"message": message, "nonce": NONCE}
        return httpx.Response(201, json=payload)

    def credit_status(self, body: dict[str, Any]) -> httpx.Response:
        signed = gw.PrepaidRefundSigned.model_validate(body)
        self.status_calls += 1
        self.status_signatures.append(signed.signature)
        assert self.last is not None and signed.nonce == self.last["nonce"]
        assert kaspa.verify_message(self.last["message"], signed.signature, kaspa.PublicKey(xonly(PAYER_KEY)))
        if self.status_error is not None:
            status, code = self.status_error
            return httpx.Response(status, json={"error": code, "message": "no"})
        if signed.nonce in self.used_nonces:
            return httpx.Response(409, json={"error": "challenge_replayed", "message": "used"})
        self.used_nonces.add(signed.nonce)
        if self.raw_status_body is not None:
            return httpx.Response(200, json=self.raw_status_body)
        count = len(self.credits) if self.credit_count is None else self.credit_count
        reply = {
            "merchant_id": str(self.status_merchant),
            "credited_sompi": str(self.credited_sompi),
            "credit_balance_sompi": str(self.credited_sompi),
            "refundable_sompi": "100",
            "credit_count": count,
            "credits": self.credits[:10],
        } | self.status_overrides
        gw.BuyerCreditStatus.model_validate(reply)  # the fake itself speaks the gateway's strict shape
        return httpx.Response(200, json=reply)

    def reset_nonce(self) -> None:
        self.used_nonces.clear()


@pytest.fixture
def credit_gateway() -> CreditGateway:
    return CreditGateway()


# ---------------------------------------------------------------- parsing


def test_the_gateway_receipt_decodes_into_a_strict_hint() -> None:
    hint, error = parse_credit_header(receipt_header())
    assert error is None and isinstance(hint, FailedDeliveryCreditHint)
    assert (hint.kind, hint.reason, hint.credit_sompi, hint.balance_sompi) == (
        "failed_delivery_credit",
        "attempts_exhausted",
        MERCHANT_SHARE,
        MERCHANT_SHARE,
    )
    assert parse_credit_header(None) == (None, None)


@pytest.mark.parametrize(
    "value",
    [
        "###not-base64###",
        "",
        base64.b64encode(b"not json").decode(),
        base64.b64encode(b"[]").decode(),
        raw_header({"kind": "failed_delivery_credit"}),
        raw_header(
            {
                "creditId": str(uuid.uuid4()),
                "kind": "refund",
                "txId": TX,
                "merchantId": str(MERCHANT_ID),
                "creditSompi": "1",
                "balanceSompi": "1",
                "reason": "attempts_exhausted",
            }
        ),
        raw_header(
            {
                "creditId": str(uuid.uuid4()),
                "kind": "failed_delivery_credit",
                "txId": "XY",
                "merchantId": str(MERCHANT_ID),
                "creditSompi": "1",
                "balanceSompi": "1",
                "reason": "attempts_exhausted",
            }
        ),
        raw_header(
            {
                "creditId": str(uuid.uuid4()),
                "kind": "failed_delivery_credit",
                "txId": TX,
                "merchantId": str(MERCHANT_ID),
                "creditSompi": "-5",
                "balanceSompi": "1",
                "reason": "attempts_exhausted",
            }
        ),
        raw_header(
            {
                "creditId": str(uuid.uuid4()),
                "kind": "failed_delivery_credit",
                "txId": TX,
                "merchantId": str(MERCHANT_ID),
                "creditSompi": 5,
                "balanceSompi": "1",
                "reason": "attempts_exhausted",
            }
        ),
        raw_header(
            {
                "creditId": str(uuid.uuid4()),
                "kind": "failed_delivery_credit",
                "txId": TX,
                "merchantId": str(MERCHANT_ID),
                "creditSompi": "1",
                "balanceSompi": "1",
                "reason": "attempts_exhausted",
                "extra": True,
            }
        ),
        base64.b64encode(b"[" * 5000).decode(),
        "A" * 5000,
    ],
)
def test_malformed_or_oversized_headers_yield_an_error_and_never_raise(
    value: str,
) -> None:
    assert parse_credit_header(value)[0] is None
    observation = observe_credit(value, status_code=502)
    assert (
        observation is not None
        and observation.status == "invalid"
        and observation.error
    )
    assert observation.hint is None and not observation.confirmed


def test_no_header_means_no_observation() -> None:
    assert observe_credit(None, status_code=502) is None


def test_a_hint_must_fit_the_call_it_arrived_on() -> None:
    header = receipt_header()
    ok = observe_credit(
        header, status_code=502, tx_id=TX, merchant_sompi=MERCHANT_SHARE
    )
    assert ok is not None and ok.status == "hint" and ok.confirmation is None
    for kwargs, fragment in (
        ({"status_code": 200}, "only failed (5xx)"),
        ({"status_code": 404}, "only failed (5xx)"),
        ({"status_code": 502, "tx_id": "ab" * 32}, "different payment"),
        ({"status_code": 502, "merchant_sompi": MERCHANT_SHARE - 1}, "merchant share"),
    ):
        observation = observe_credit(header, **kwargs)
        assert observation is not None and observation.status == "invalid", kwargs
        assert fragment in (observation.error or "")
    inflated = receipt_header(credit=5, balanceSompi="4")
    bad = observe_credit(inflated, status_code=502)
    assert bad is not None and bad.status == "invalid"


# ---------------------------------------------------------------- on a response


async def test_a_credit_header_is_surfaced_as_an_unconfirmed_hint(
    make_client, credit_gateway: CreditGateway
) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    assert response.status_code == 502
    observation = credit_of(response)
    assert observation is not None and observation.status == "hint"
    assert observation.confirmed is False and observation.confirmation is None
    assert (
        observation.hint is not None and observation.hint.credit_sompi == MERCHANT_SHARE
    )
    assert observation.hint.txId == receipt_of(response).tx_id  # type: ignore[union-attr]
    assert credit_gateway.status_calls == 0  # a hint alone never triggers a status read by default


async def test_no_header_no_observation(
    make_client, credit_gateway: CreditGateway
) -> None:
    credit_gateway.credit_for_tx = False
    response = await make_client(credit_gateway, unavailable_retry_delay=0).get(
        "/p/demo"
    )
    assert response.status_code == 502 and credit_of(response) is None


async def test_a_forged_header_for_another_payment_is_invalid(
    make_client, credit_gateway: CreditGateway
) -> None:
    credit_gateway.credit_header = receipt_header("ab" * 32)
    response = await make_client(credit_gateway, unavailable_retry_delay=0).get(
        "/p/demo"
    )
    observation = credit_of(response)
    assert (
        observation is not None
        and observation.status == "invalid"
        and observation.hint is None
    )
    assert "different payment" in (observation.error or "")


async def test_a_forged_amount_is_invalid(
    make_client, credit_gateway: CreditGateway
) -> None:
    async def forged(client: sdk.KasGateClient) -> CreditObservation | None:
        credit_gateway.credit_header = None
        return credit_of(await client.get("/p/demo"))

    def inflate(request: httpx.Request, parsed: dict[str, Any]) -> httpx.Response:
        response = credit_gateway.failing_upstream(request, parsed)
        response.headers["X-KasGate-Credit"] = receipt_header(
            parsed["signature"].txId, credit=PRICE
        )
        return response

    credit_gateway.paid_handler = inflate
    observation = await forged(make_client(credit_gateway, unavailable_retry_delay=0))
    assert observation is not None and observation.status == "invalid"
    assert "merchant share" in (observation.error or "")


@pytest.mark.parametrize(
    "garbage", ["###", "A" * 9000, base64.b64encode(b"{}").decode()]
)
async def test_garbage_on_a_response_never_crashes_the_call(
    make_client, credit_gateway: CreditGateway, garbage: str
) -> None:
    credit_gateway.credit_header = garbage
    client = make_client(
        credit_gateway, unavailable_retry_delay=0, auto_confirm_credit=True
    )
    response = await client.get("/p/demo")
    assert response.status_code == 502
    observation = credit_of(response)
    assert (
        observation is not None
        and observation.status == "invalid"
        and observation.error
    )
    assert not observation.confirmed


async def test_a_credit_header_on_a_success_is_invalid(
    make_client, credit_gateway: CreditGateway
) -> None:
    credit_gateway.status = 200
    response = await make_client(credit_gateway, unavailable_retry_delay=0).get(
        "/p/demo"
    )
    assert response.status_code == 200
    observation = credit_of(response)
    assert observation is not None and observation.status == "invalid"


# ---------------------------------------------------------------- the signed credit status


async def test_credit_status_happy_path(make_client, credit_gateway: CreditGateway) -> None:
    credit_gateway.credits = [credit_item(TX), credit_item("ab" * 32, credit=5)]
    credit_gateway.credited_sompi = MERCHANT_SHARE + 5
    client = make_client(credit_gateway)
    status = await client.credit_status(GATEWAY, "demo")
    assert isinstance(status, BuyerCreditStatus) and status.merchant_id == MERCHANT_ID
    assert (status.credited_sompi, status.credit_count, len(status.credits)) == (MERCHANT_SHARE + 5, 2, 2)
    assert [c.tx_id for c in status.credits] == [TX, "ab" * 32]
    assert isinstance(status.credits[0].credit_sompi, int) and status.credits[0].reason == "attempts_exhausted"
    assert credit_gateway.challenge_calls == 1 and credit_gateway.status_calls == 1


async def test_credit_status_for_a_buyer_with_nothing_is_all_zeros(make_client, credit_gateway: CreditGateway) -> None:
    status = await make_client(credit_gateway).credit_status(GATEWAY, "demo")
    assert (status.credited_sompi, status.credit_balance_sompi, status.credit_count, status.credits) == (0, 0, 0, [])


def test_credit_status_sync_wrapper(credit_gateway: CreditGateway) -> None:
    http = httpx.AsyncClient(transport=httpx.MockTransport(credit_gateway), base_url=GATEWAY)
    credit_gateway.credits = [credit_item()]
    credit_gateway.credited_sompi = MERCHANT_SHARE
    with KasGateSyncClient(PAYER_KEY, http_client=http) as client:
        assert client.credit_status(GATEWAY, "demo").credit_count == 1


@pytest.mark.parametrize(
    ("override", "needle"),
    [
        ({"action": "status"}, ""),  # a refunds-status challenge: wrong action (not even a credits model)
        ({"slug": "other"}, "another payer or endpoint"),
        ({"payer_address": "kaspatest:qq"}, "another payer or endpoint"),
        ({"refund_id": str(uuid.uuid4())}, ""),
        ({"amount_sompi": "5"}, ""),
        ({"extra": 1}, ""),
        ({"expires_at": (datetime.now(UTC) - timedelta(seconds=5)).isoformat()}, "expired"),
        ({"nonce": "XYZ"}, ""),
    ],
)
async def test_a_wrong_or_unexpected_challenge_is_never_signed(
    make_client, credit_gateway: CreditGateway, override: dict[str, Any], needle: str
) -> None:
    credit_gateway.challenge_overrides = override
    with pytest.raises(sdk.InvalidChallengeError) as caught:
        await make_client(credit_gateway).credit_status(GATEWAY, "demo")
    assert needle in str(caught.value)
    assert credit_gateway.status_calls == 0 and credit_gateway.status_signatures == []


async def test_a_challenge_whose_text_is_not_the_credits_text_is_never_signed(
    make_client, credit_gateway: CreditGateway
) -> None:
    """A gateway that names `credits` but hands over the text of a refund request (money-moving) gets no signature."""
    expires = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=60)
    credit_gateway.message_override = gw.build_prepaid_refund_message(
        domain="gateway.test",
        action="request",
        address=PAYER,
        merchant_id=MERCHANT_ID,
        amount_sompi=40_000_000,
        refund_id=None,
        nonce=NONCE,
        expires_at=expires,
    )
    credit_gateway.challenge_overrides = {"expires_at": expires.isoformat()}
    with pytest.raises(sdk.InvalidChallengeError, match="does not match"):
        await make_client(credit_gateway).credit_status(GATEWAY, "demo")
    assert credit_gateway.status_calls == 0


@pytest.mark.parametrize(
    ("status", "code", "error"),
    [
        (400, "invalid_signature", PaymentRejectedError),  # e.g. a refunds-status challenge used here
        (409, "challenge_replayed", PaymentReplayedError),
        (410, "challenge_expired", PaymentExpiredError),
        (429, "rate_limited", PaymentRejectedError),
        (503, "service_unavailable", PaymentRejectedError),
    ],
)
async def test_credit_status_maps_gateway_errors_to_typed_errors(
    make_client, credit_gateway: CreditGateway, status: int, code: str, error: type[Exception]
) -> None:
    credit_gateway.status_error = (status, code)
    with pytest.raises(error) as caught:
        await make_client(credit_gateway).credit_status(GATEWAY, "demo")
    assert isinstance(caught.value, PaymentRejectedError)
    assert (caught.value.status_code, caught.value.code) == (status, code)


async def test_a_challenge_error_is_a_typed_rejection(make_client, credit_gateway: CreditGateway) -> None:
    for status, code in ((404, "endpoint_not_found"), (400, "invalid_payer"), (429, "rate_limited"), (503, "service_unavailable")):
        credit_gateway.challenge_error = (status, code)
        with pytest.raises(PaymentRejectedError) as caught:
            await make_client(credit_gateway).credit_status(GATEWAY, "demo")
        assert (caught.value.status_code, caught.value.code) == (status, code)
    assert credit_gateway.status_calls == 0


async def test_a_replayed_nonce_is_a_409_and_a_fresh_challenge_works(make_client, credit_gateway: CreditGateway) -> None:
    client = make_client(credit_gateway)
    await client.credit_status(GATEWAY, "demo")
    with pytest.raises(PaymentReplayedError):  # the fake reuses the nonce, like a replayed challenge
        await client.credit_status(GATEWAY, "demo")
    credit_gateway.reset_nonce()
    assert (await client.credit_status(GATEWAY, "demo")).credit_count == 0


@pytest.mark.parametrize(
    "mutate",
    [
        {"extra": 1},
        {"credited_sompi": 5},  # a JSON number, not a decimal string
        {"credited_sompi": "05"},
        {"credited_sompi": "-1"},
        {"credit_count": "1"},
        {"credit_count": -1},
        {"credit_count": 1, "credits": [credit_item(), credit_item("ab" * 32)]},  # fewer counted than listed
        {"credits": [{**credit_item(), "extra": 1}]},
        {"credits": [{**credit_item(), "reason": "refund"}]},
        {"credits": [{**credit_item(), "tx_id": "XY"}]},
        {"credits": [{**credit_item(), "credit_sompi": 5}]},
        {"merchant_id": "nope"},
    ],
)
async def test_malformed_status_replies_are_refused(
    make_client, credit_gateway: CreditGateway, mutate: dict[str, Any]
) -> None:
    credit_gateway.raw_status_body = {
        "merchant_id": str(MERCHANT_ID),
        "credited_sompi": "0",
        "credit_balance_sompi": "0",
        "refundable_sompi": "0",
        "credit_count": 0,
        "credits": [],
    } | mutate
    with pytest.raises(sdk.PaymentReceiptError, match="cannot be decoded"):
        await make_client(credit_gateway).credit_status(GATEWAY, "demo")


async def test_more_than_ten_credits_or_a_non_object_reply_is_refused(make_client, credit_gateway: CreditGateway) -> None:
    items = [credit_item(f"{i:064x}") for i in range(11)]
    credit_gateway.raw_status_body = {
        "merchant_id": str(MERCHANT_ID),
        "credited_sompi": "0",
        "credit_balance_sompi": "0",
        "refundable_sompi": "0",
        "credit_count": 11,
        "credits": items,
    }
    with pytest.raises(sdk.PaymentReceiptError):
        await make_client(credit_gateway).credit_status(GATEWAY, "demo")
    credit_gateway.reset_nonce()
    credit_gateway.raw_status_body = [1, 2]
    with pytest.raises(sdk.PaymentReceiptError):
        await make_client(credit_gateway).credit_status(GATEWAY, "demo")


async def test_a_status_for_another_merchant_than_the_signed_challenge_is_refused(
    make_client, credit_gateway: CreditGateway
) -> None:
    credit_gateway.status_overrides = {"merchant_id": str(uuid.uuid4())}
    with pytest.raises(sdk.PaymentReceiptError, match="another merchant"):
        await make_client(credit_gateway).credit_status(GATEWAY, "demo")


# ---------------------------------------------------------------- confirmation


async def test_a_credit_the_status_lists_is_confirmed(make_client, credit_gateway: CreditGateway) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    checked = await client.confirm_credit(GATEWAY, "demo", response)
    assert checked.status == "confirmed" and checked.confirmed is True
    assert checked.confirmation is not None
    assert (checked.confirmation.basis, checked.confirmation.credited_sompi) == ("tx_id", MERCHANT_SHARE)
    assert checked.hint is not None and checked.hint.txId == receipt_of(response).tx_id  # type: ignore[union-attr]
    original = credit_of(response)
    assert original is not None and original.status == "hint"  # the response keeps its unconfirmed hint


async def test_a_header_present_but_the_credit_not_listed_is_unconfirmed(
    make_client, credit_gateway: CreditGateway
) -> None:
    credit_gateway.register_credit = False  # the header is a forgery: the gateway never credited anything
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    checked = await client.confirm_credit(GATEWAY, "demo", response)
    assert checked.status == "unconfirmed" and checked.confirmed is False
    assert checked.confirmation is not None and checked.confirmation.basis == "none"
    assert "lists no credit" in checked.confirmation.detail


async def test_a_status_with_credits_but_not_this_payment_is_unconfirmed(
    make_client, credit_gateway: CreditGateway
) -> None:
    credit_gateway.register_credit = False
    credit_gateway.credits = [credit_item("ab" * 32)]
    credit_gateway.credited_sompi = 10**12  # a big lifetime total does not matter: the txId is what counts
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    checked = await client.confirm_credit(GATEWAY, "demo", response, baseline_credited_sompi=0)
    assert checked.status == "unconfirmed" and checked.confirmation is not None
    assert checked.confirmation.basis == "none" and "lists no credit" in checked.confirmation.detail


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"credit": MERCHANT_SHARE - 1}, "not the hinted"),
        ({"reason": "window_expired"}, "reason window_expired"),
        ({"slug": "other-endpoint"}, "endpoint other-endpoint"),
    ],
)
async def test_a_listed_payment_with_other_details_contradicts_the_hint(
    make_client, credit_gateway: CreditGateway, changes: dict[str, Any], fragment: str
) -> None:
    credit_gateway.register_credit = False
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    credit_gateway.credits = [credit_item(receipt_of(response).tx_id, **changes)]  # type: ignore[union-attr]
    checked = await client.confirm_credit(GATEWAY, "demo", response)
    assert checked.status == "unconfirmed" and checked.confirmation is not None
    assert fragment in checked.confirmation.detail


async def test_the_status_of_another_merchant_does_not_confirm(make_client, credit_gateway: CreditGateway) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    credit_gateway.status_merchant = uuid.uuid4()  # challenge and status agree with each other, not with the hint
    checked = await client.confirm_credit(GATEWAY, "demo", response)
    assert checked.status == "unconfirmed"
    assert checked.confirmation is not None and "different merchant" in checked.confirmation.detail


async def test_a_payment_beyond_the_newest_ten_needs_a_baseline_to_be_confirmed(
    make_client, credit_gateway: CreditGateway
) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    credit_gateway.credit_header = None
    credit_gateway.register_credit = False
    response = await client.get("/p/demo")
    # The hinted payment is not listed, but 25 credits exist: only the newest 10 are visible.
    credit_gateway.credits = [credit_item(f"{i:064x}") for i in range(10)]
    credit_gateway.credit_count = 25
    credit_gateway.credited_sompi = 500 + MERCHANT_SHARE
    no_baseline = await client.confirm_credit(GATEWAY, "demo", response)
    assert no_baseline.status == "unconfirmed" and no_baseline.confirmation is not None
    assert "newest 10" in no_baseline.confirmation.detail
    credit_gateway.reset_nonce()
    grew = await client.confirm_credit(GATEWAY, "demo", response, baseline_credited_sompi=500)
    assert grew.status == "confirmed" and grew.confirmation is not None
    assert (grew.confirmation.basis, grew.confirmation.baseline_credited_sompi) == ("increase", 500)
    credit_gateway.reset_nonce()
    flat = await client.confirm_credit(GATEWAY, "demo", response, baseline_credited_sompi=501)
    assert flat.status == "unconfirmed" and flat.confirmation is not None
    assert "less than the hinted" in flat.confirmation.detail


@pytest.mark.parametrize(
    ("kind", "code"),
    [("challenge", 429), ("challenge", 503), ("challenge", 404), ("status", 429), ("status", 503), ("status", 410), ("status", 409), ("status", 400)],
)
async def test_gateway_errors_make_the_credit_unconfirmed_with_a_reason_and_never_raise(
    make_client, credit_gateway: CreditGateway, kind: str, code: int
) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    names = {400: "invalid_signature", 404: "endpoint_not_found", 409: "challenge_replayed", 410: "challenge_expired", 429: "rate_limited", 503: "service_unavailable"}
    if kind == "challenge":
        credit_gateway.challenge_error = (code, names[code])
    else:
        credit_gateway.status_error = (code, names[code])
    checked = await client.confirm_credit(GATEWAY, "demo", response)
    assert checked.status == "unconfirmed" and checked.confirmed is False
    assert checked.confirmation is not None and checked.confirmation.basis == "none"
    assert (checked.confirmation.http_status, checked.confirmation.error_code) == (code, names[code])
    assert "unverified" in checked.confirmation.detail


async def test_a_malformed_or_unsafe_challenge_or_reply_during_confirmation_is_unconfirmed(
    make_client, credit_gateway: CreditGateway
) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    credit_gateway.challenge_overrides = {"action": "status"}
    bad_challenge = await client.confirm_credit(GATEWAY, "demo", response)
    assert bad_challenge.status == "unconfirmed" and credit_gateway.status_calls == 0
    credit_gateway.challenge_overrides = {}
    credit_gateway.raw_status_body = {"merchant_id": str(MERCHANT_ID), "extra": 1}
    bad_reply = await client.confirm_credit(GATEWAY, "demo", response)
    assert bad_reply.status == "unconfirmed" and bad_reply.confirmation is not None
    assert "PaymentReceiptError" in bad_reply.confirmation.detail


async def test_a_dead_gateway_during_confirmation_is_unconfirmed(make_client, credit_gateway: CreditGateway) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")

    def dead(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(dead), base_url=GATEWAY)
    checked = await client.confirm_credit(GATEWAY, "demo", response)
    assert checked.status == "unconfirmed" and checked.confirmation is not None
    assert "ConnectError" in checked.confirmation.detail


async def test_an_invalid_observation_is_never_confirmed_and_asks_nothing(
    make_client, credit_gateway: CreditGateway
) -> None:
    credit_gateway.credit_header = "###"
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    checked = await client.confirm_credit(GATEWAY, "demo", response, baseline_credited_sompi=0)
    assert checked.status == "invalid" and credit_gateway.challenge_calls == 0


async def test_confirm_credit_argument_errors(make_client, credit_gateway: CreditGateway) -> None:
    credit_gateway.credit_for_tx = False
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    with pytest.raises(sdk.ConfigurationError):
        await client.confirm_credit(GATEWAY, "demo", response)
    hint, _ = parse_credit_header(receipt_header())
    assert hint is not None
    for bad in (-1, True, 1.5):
        with pytest.raises(sdk.ConfigurationError):
            await client.confirm_credit(GATEWAY, "demo", hint, baseline_credited_sompi=bad)  # type: ignore[arg-type]
    with pytest.raises(sdk.ConfigurationError):
        await client.confirm_credit(GATEWAY, "demo", "nope")  # type: ignore[arg-type]
    credit_gateway.credits = [credit_item()]
    assert (await client.confirm_credit(GATEWAY, "demo", hint)).confirmed


async def test_no_key_or_signature_is_logged_or_surfaced(make_client, credit_gateway: CreditGateway, caplog) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    credit_gateway.status_error = (429, "rate_limited")
    with caplog.at_level("DEBUG"):
        checked = await client.confirm_credit(GATEWAY, "demo", response)
    dump = checked.model_dump_json() + caplog.text
    assert PAYER_KEY not in dump and not any(sig in dump for sig in credit_gateway.status_signatures)


# ---------------------------------------------------------------- auto-confirm


async def test_auto_confirm_confirms_a_credit_the_status_lists(make_client, credit_gateway: CreditGateway) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0, auto_confirm_credit=True)
    response = await client.get("/p/demo")
    observation = credit_of(response)
    assert observation is not None and observation.status == "confirmed"
    assert observation.confirmation is not None and observation.confirmation.basis == "tx_id"
    assert (credit_gateway.challenge_calls, credit_gateway.status_calls) == (1, 1)  # once, only because a hint came


async def test_auto_confirm_with_a_forged_header_stays_unconfirmed(make_client, credit_gateway: CreditGateway) -> None:
    credit_gateway.register_credit = False
    client = make_client(credit_gateway, unavailable_retry_delay=0, auto_confirm_credit=True)
    observation = credit_of(await client.get("/p/demo"))
    assert observation is not None and observation.status == "unconfirmed" and not observation.confirmed


@pytest.mark.parametrize("code", [429, 503])
async def test_auto_confirm_survives_a_busy_or_dead_gateway(
    make_client, credit_gateway: CreditGateway, code: int
) -> None:
    credit_gateway.status_error = (code, "rate_limited" if code == 429 else "service_unavailable")
    client = make_client(credit_gateway, unavailable_retry_delay=0, auto_confirm_credit=True)
    response = await client.get("/p/demo")
    observation = credit_of(response)
    assert response.status_code == 502
    assert observation is not None and observation.status == "unconfirmed"
    assert observation.confirmation is not None and observation.confirmation.http_status == code


async def test_auto_confirm_makes_no_extra_calls_when_there_is_no_hint(make_client, credit_gateway: CreditGateway) -> None:
    credit_gateway.credit_for_tx = False
    client = make_client(credit_gateway, unavailable_retry_delay=0, auto_confirm_credit=True)
    assert credit_of(await client.get("/p/demo")) is None
    assert (credit_gateway.challenge_calls, credit_gateway.status_calls) == (0, 0)


async def test_auto_confirm_off_by_default_and_no_status_read_without_a_proxy_path(
    make_client, credit_gateway: CreditGateway
) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0, auto_confirm_credit=True)
    response = await client.get("/not-a-proxy-path")
    observation = credit_of(response)
    assert observation is not None and observation.status == "unconfirmed"
    assert observation.confirmation is not None and observation.confirmation.basis == "none"
    assert credit_gateway.challenge_calls == 0
    assert make_client(credit_gateway).auto_confirm_credit is False


# ---------------------------------------------------------------- judge_credit and models


def _status(credits: list[dict[str, Any]], *, count: int | None = None, credited: int = 0) -> BuyerCreditStatus:
    return BuyerCreditStatus.model_validate(
        {
            "merchant_id": str(MERCHANT_ID),
            "credited_sompi": str(credited),
            "credit_balance_sompi": "0",
            "refundable_sompi": "0",
            "credit_count": len(credits) if count is None else count,
            "credits": credits,
        }
    )


def test_judge_credit_and_model_strictness() -> None:
    hint, _ = parse_credit_header(receipt_header())
    assert hint is not None
    listed = _status([credit_item()], credited=MERCHANT_SHARE)
    assert judge_credit(hint, listed).confirmed
    assert judge_credit(hint, listed, slug="demo").confirmed
    assert not judge_credit(hint, listed, slug="other").confirmed
    assert not judge_credit(hint, _status([])).confirmed
    assert not judge_credit(hint, _status([], count=0, credited=10**12), baseline_credited_sompi=0).confirmed
    with pytest.raises(ValueError):
        sdk.CreditObservation(status="confirmed", extra=1)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        hint.creditSompi = "1"  # type: ignore[misc]
    with pytest.raises(ValueError):
        listed.credited_sompi = 1  # type: ignore[misc]
