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
DEFAULT_PAGE_BUDGET = sdk.DEFAULT_CREDIT_PAGE_BUDGET
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
    credit_id: uuid.UUID | None = None,
    slug: str = "demo",
    credit: int = MERCHANT_SHARE,
    reason: str = "attempts_exhausted",
) -> dict[str, Any]:
    return {
        "credit_id": str(credit_id or uuid.uuid4()),
        "tx_id": tx_id,
        "slug": slug,
        "credit_sompi": str(credit),
        "shortfall_sompi": "0",
        "reason": reason,
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }


class CreditGateway(FakeGateway):
    """The paid route answers 502 with an X-KasGate-Credit header and (like the gateway) records the credit under the
    header's creditId; the fake `/v1/prepaid/credits/challenge` + `/credits/status` validate the SDK's bodies with the
    gateway's own strict models (PrepaidCreditStatusRequest: extra=forbid, limit 1..100), build the challenge text
    with the gateway's builder, check the signature, issue a fresh single-use nonce per challenge and page newest
    first with an opaque keyset cursor."""

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
        self.fixed_nonce: str | None = None  # set to reuse one nonce, like a replayed challenge
        self.issued: dict[str, str] = {}  # nonce -> signed text
        self.credit_ids: dict[str, uuid.UUID] = {}  # tx_id -> the creditId the header announced
        self.used_nonces: set[str] = set()
        self.invalid_cursor_answers = 0  # answer this many cursor requests with 400 invalid_cursor
        self.after_last_page: Any = None  # called once when the last page is served
        self.status_bodies: list[dict[str, Any]] = []
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
        credit_id = self.credit_ids.setdefault(tx_id, uuid.uuid4())  # a redelivery of the same payment: same credit
        if header is None and self.credit_for_tx:
            header = receipt_header(tx_id, creditId=credit_id)
        if header is not None:
            headers["X-KasGate-Credit"] = header
            if self.register_credit and self.credit_header is None and all(c["tx_id"] != tx_id for c in self.credits):
                self.credits.insert(0, credit_item(tx_id, credit_id=credit_id))
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
        nonce = self.fixed_nonce or uuid.uuid4().hex
        expires = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=60)
        message = gw.build_prepaid_refund_message(
            domain="gateway.test",
            action="credits",
            address=parsed.payer_address,
            merchant_id=self.status_merchant,
            amount_sompi=0,
            refund_id=None,
            nonce=nonce,
            expires_at=expires,
        )
        payload = {
            "nonce": nonce,
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
        payload = {key: value for key, value in payload.items() if value is not ...}
        self.issued[nonce] = message
        self.last = {"message": message, "nonce": nonce}
        return httpx.Response(201, json=payload)

    def credit_status(self, body: dict[str, Any]) -> httpx.Response:
        signed = gw.PrepaidCreditStatusRequest.model_validate(body)
        self.status_calls += 1
        self.status_bodies.append(body)
        self.status_signatures.append(signed.signature)
        assert signed.nonce in self.issued
        assert kaspa.verify_message(self.issued[signed.nonce], signed.signature, kaspa.PublicKey(xonly(PAYER_KEY)))
        if self.status_error is not None:
            status, code = self.status_error
            return httpx.Response(status, json={"error": code, "message": "no"})
        ids = [c["credit_id"] for c in self.credits]
        start = 0
        if signed.before is not None:
            if self.invalid_cursor_answers > 0 or signed.before not in ids:
                self.invalid_cursor_answers = max(0, self.invalid_cursor_answers - 1)
                return httpx.Response(400, json={"error": "invalid_cursor", "message": "bad cursor"})  # nonce not burned
            start = ids.index(signed.before) + 1
        if signed.nonce in self.used_nonces:
            return httpx.Response(409, json={"error": "challenge_replayed", "message": "used"})
        self.used_nonces.add(signed.nonce)
        if self.raw_status_body is not None:
            return httpx.Response(200, json=self.raw_status_body)
        page = self.credits[start : start + signed.limit]
        more = start + signed.limit < len(self.credits)
        count = len(self.credits) if self.credit_count is None else self.credit_count
        reply = {
            "merchant_id": str(self.status_merchant),
            "credited_sompi": str(self.credited_sompi),
            "credit_balance_sompi": str(self.credited_sompi),
            "refundable_sompi": "100",
            "credit_count": count,
            "credits": page,
            "next_cursor": page[-1]["credit_id"] if more else None,
        } | self.status_overrides
        gw.BuyerCreditStatus.model_validate(reply)  # the fake itself speaks the gateway's strict shape
        if not more and self.after_last_page is not None:
            hook, self.after_last_page = self.after_last_page, None
            hook()
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
    credit_gateway.fixed_nonce = NONCE
    await client.credit_status(GATEWAY, "demo")
    with pytest.raises(PaymentReplayedError):  # the fake reuses the nonce, like a replayed challenge
        await client.credit_status(GATEWAY, "demo")
    credit_gateway.fixed_nonce = None
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
        {"next_cursor": ...},  # the field is required (null on the last page)
        {"next_cursor": 5},
        {"next_cursor": ""},
        {"next_cursor": "c" * 513},
        {"next_cursor": "abc"},  # a cursor on a page without credits
        {"credits": [{k: v for k, v in credit_item().items() if k != "credit_id"}]},
        {"credits": [{**credit_item(), "credit_id": "not-a-uuid"}]},
        {"credit_count": 2, "credits": [credit_item(credit_id=MERCHANT_ID), credit_item("ab" * 32, credit_id=MERCHANT_ID)]},
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
        "next_cursor": None,
    } | mutate
    credit_gateway.raw_status_body = {k: v for k, v in credit_gateway.raw_status_body.items() if v is not ...}
    with pytest.raises(sdk.PaymentReceiptError, match="cannot be decoded"):
        await make_client(credit_gateway).credit_status(GATEWAY, "demo")


async def test_more_than_a_hundred_credits_or_a_non_object_reply_is_refused(
    make_client, credit_gateway: CreditGateway
) -> None:
    items = [credit_item(f"{i:064x}") for i in range(101)]
    credit_gateway.raw_status_body = {
        "merchant_id": str(MERCHANT_ID),
        "credited_sompi": "0",
        "credit_balance_sompi": "0",
        "refundable_sompi": "0",
        "credit_count": 101,
        "credits": items,
        "next_cursor": None,
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
    assert (checked.confirmation.basis, checked.confirmation.credited_sompi) == ("credit_id", MERCHANT_SHARE)
    assert checked.confirmation.pages_read == 1 and credit_gateway.status_bodies[0]["limit"] == 100
    assert "before" not in credit_gateway.status_bodies[0]
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
    observation = credit_of(response)
    assert observation is not None and observation.hint is not None
    credit_gateway.credits = [
        credit_item(receipt_of(response).tx_id, credit_id=observation.hint.creditId, **changes)  # type: ignore[union-attr]
    ]
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


def _hint_of(response: httpx.Response) -> FailedDeliveryCreditHint:
    observation = credit_of(response)
    assert observation is not None and observation.hint is not None
    return observation.hint


def _older(n: int, start: int = 0) -> list[dict[str, Any]]:
    return [credit_item(f"{i:064x}") for i in range(start, start + n)]


async def test_a_credit_id_match_confirms(make_client, credit_gateway: CreditGateway) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    hint = _hint_of(response)
    assert credit_gateway.credits[0]["credit_id"] == str(hint.creditId)
    checked = await client.confirm_credit(GATEWAY, "demo", response)
    assert checked.confirmed and checked.confirmation is not None and checked.confirmation.basis == "credit_id"


async def test_the_hinted_payment_under_another_credit_id_is_not_confirmed(
    make_client, credit_gateway: CreditGateway
) -> None:
    """Same txId, amount, reason and slug, but the gateway's credit_id is not the header's creditId."""
    credit_gateway.register_credit = False
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    credit_gateway.credits = [credit_item(_hint_of(response).txId, credit_id=uuid.uuid4())]
    credit_gateway.credited_sompi = 10**12
    checked = await client.confirm_credit(GATEWAY, "demo", response, baseline_credited_sompi=0)
    assert checked.status == "unconfirmed" and checked.confirmation is not None
    assert checked.confirmation.basis == "none" and "another credit_id" in checked.confirmation.detail


async def test_the_hinted_credit_id_for_another_payment_is_not_confirmed(
    make_client, credit_gateway: CreditGateway
) -> None:
    credit_gateway.register_credit = False
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    credit_gateway.credits = [credit_item("ab" * 32, credit_id=_hint_of(response).creditId)]
    checked = await client.confirm_credit(GATEWAY, "demo", response)
    assert checked.status == "unconfirmed" and checked.confirmation is not None
    assert "another payment" in checked.confirmation.detail


async def test_a_multi_page_walk_uses_a_fresh_challenge_per_page(make_client, credit_gateway: CreditGateway) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    mine = credit_gateway.credits.pop(0)
    credit_gateway.credits = _older(250, 1) + [mine]  # the hinted credit is the oldest of 251: page 3
    checked = await client.confirm_credit(GATEWAY, "demo", response)
    assert checked.confirmed and checked.confirmation is not None
    assert (checked.confirmation.basis, checked.confirmation.pages_read) == ("credit_id", 3)
    bodies = credit_gateway.status_bodies
    assert [b["limit"] for b in bodies] == [100, 100, 100]
    assert "before" not in bodies[0]
    assert [b.get("before") for b in bodies[1:]] == [credit_gateway.credits[99]["credit_id"], credit_gateway.credits[199]["credit_id"]]
    nonces = [b["nonce"] for b in bodies]
    assert len(set(nonces)) == 3 and credit_gateway.challenge_calls == 3


async def test_the_walk_stops_at_the_page_that_lists_the_credit(make_client, credit_gateway: CreditGateway) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    credit_gateway.credits += _older(400, 1)  # the hinted credit is the newest
    checked = await client.confirm_credit(GATEWAY, "demo", response)
    assert checked.confirmed and credit_gateway.status_calls == 1


async def test_an_invalid_cursor_restarts_from_the_newest_page_once(make_client, credit_gateway: CreditGateway) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    mine = credit_gateway.credits.pop(0)
    credit_gateway.credits = _older(150, 1) + [mine]
    credit_gateway.invalid_cursor_answers = 1
    checked = await client.confirm_credit(GATEWAY, "demo", response)
    assert checked.confirmed and checked.confirmation is not None
    befores = [b.get("before") for b in credit_gateway.status_bodies]
    assert befores[0] is None and befores[1] is not None and befores[2] is None and befores[3] is not None
    assert checked.confirmation.pages_read == 4


async def test_a_second_invalid_cursor_leaves_the_credit_unconfirmed(make_client, credit_gateway: CreditGateway) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    mine = credit_gateway.credits.pop(0)
    credit_gateway.credits = _older(150, 1) + [mine]
    credit_gateway.invalid_cursor_answers = 2
    checked = await client.confirm_credit(GATEWAY, "demo", response)
    assert checked.status == "unconfirmed" and checked.confirmation is not None
    assert (checked.confirmation.http_status, checked.confirmation.error_code) == (400, "invalid_cursor")


async def test_a_spent_page_budget_needs_a_baseline_to_be_confirmed(make_client, credit_gateway: CreditGateway) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0, credit_page_budget=2)
    response = await client.get("/p/demo")
    mine = credit_gateway.credits.pop(0)
    credit_gateway.credits = _older(250, 1) + [mine]  # needs 3 pages, only 2 allowed
    credit_gateway.credited_sompi = 500 + MERCHANT_SHARE
    no_baseline = await client.confirm_credit(GATEWAY, "demo", response)
    assert no_baseline.status == "unconfirmed" and no_baseline.confirmation is not None
    assert "200 of 251 credits read" in no_baseline.confirmation.detail and no_baseline.confirmation.pages_read == 2
    assert credit_gateway.status_calls == 2
    grew = await client.confirm_credit(GATEWAY, "demo", response, baseline_credited_sompi=500)
    assert grew.status == "confirmed" and grew.confirmation is not None
    assert (grew.confirmation.basis, grew.confirmation.baseline_credited_sompi) == ("increase", 500)
    flat = await client.confirm_credit(GATEWAY, "demo", response, baseline_credited_sompi=501)
    assert flat.status == "unconfirmed" and flat.confirmation is not None
    assert "less than the hinted" in flat.confirmation.detail
    assert credit_gateway.status_calls == 6


async def test_a_count_above_the_listed_credits_is_walked_again_once(make_client, credit_gateway: CreditGateway) -> None:
    """The credit lands while the walk pages (newer than its cursor): the count is 1 higher; a re-walk finds it."""
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    mine = credit_gateway.credits.pop(0)
    credit_gateway.credits = _older(150, 1)
    credit_gateway.credit_count = 151

    def land() -> None:
        credit_gateway.credits.insert(0, mine)
        credit_gateway.credit_count = None

    credit_gateway.after_last_page = land
    checked = await client.confirm_credit(GATEWAY, "demo", response)
    assert checked.confirmed and checked.confirmation is not None and checked.confirmation.pages_read == 3
    assert [b.get("before") is None for b in credit_gateway.status_bodies] == [True, False, True]


async def test_a_count_that_never_matches_falls_back_to_the_baseline(make_client, credit_gateway: CreditGateway) -> None:
    credit_gateway.register_credit = False
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    credit_gateway.credits = _older(150)
    credit_gateway.credit_count = 175  # the gateway counts credits it never lists
    credit_gateway.credited_sompi = 500 + MERCHANT_SHARE
    no_baseline = await client.confirm_credit(GATEWAY, "demo", response)
    assert no_baseline.status == "unconfirmed" and no_baseline.confirmation is not None
    assert "150 of 175 credits read" in no_baseline.confirmation.detail
    assert no_baseline.confirmation.pages_read == 4  # 2 pages, then one re-walk of 2
    grew = await client.confirm_credit(GATEWAY, "demo", response, baseline_credited_sompi=500)
    assert grew.confirmed and grew.confirmation is not None and grew.confirmation.basis == "increase"


async def test_a_complete_walk_without_the_credit_does_not_use_the_baseline(
    make_client, credit_gateway: CreditGateway
) -> None:
    credit_gateway.register_credit = False
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    credit_gateway.credits = _older(150)
    credit_gateway.credited_sompi = 10**12
    checked = await client.confirm_credit(GATEWAY, "demo", response, baseline_credited_sompi=0)
    assert checked.status == "unconfirmed" and checked.confirmation is not None
    assert "lists no credit" in checked.confirmation.detail and checked.confirmation.pages_read == 2


async def test_pages_that_do_not_advance_or_repeat_a_credit_are_unconfirmed(
    make_client, credit_gateway: CreditGateway
) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    credit_gateway.credits = _older(3)
    credit_gateway.credit_count = 9
    credit_gateway.raw_status_body = {
        "merchant_id": str(MERCHANT_ID),
        "credited_sompi": "0",
        "credit_balance_sompi": "0",
        "refundable_sompi": "0",
        "credit_count": 9,
        "credits": credit_gateway.credits,
        "next_cursor": credit_gateway.credits[-1]["credit_id"],
    }
    checked = await client.confirm_credit(GATEWAY, "demo", response)
    assert checked.status == "unconfirmed" and checked.confirmation is not None
    assert "PaymentReceiptError" in checked.confirmation.detail
    assert credit_gateway.status_calls <= DEFAULT_PAGE_BUDGET


async def test_a_challenge_without_the_informational_balances_is_accepted(
    make_client, credit_gateway: CreditGateway
) -> None:
    credit_gateway.challenge_overrides = {"spendable_sompi": ..., "refundable_sompi": ...}
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    assert (await client.confirm_credit(GATEWAY, "demo", response)).confirmed
    challenge = sdk.CreditStatusChallenge.model_validate(
        {
            "nonce": NONCE,
            "message": "m",
            "action": "credits",
            "slug": "demo",
            "merchant_id": str(MERCHANT_ID),
            "payer_address": PAYER,
            "amount_sompi": "0",
            "refund_id": None,
            "expires_at": datetime.now(UTC).isoformat(),
        }
    )
    assert challenge.spendable_sompi is None and challenge.refundable_sompi is None


async def test_credit_status_pages_by_hand(make_client, credit_gateway: CreditGateway) -> None:
    credit_gateway.credits = _older(25)
    client = make_client(credit_gateway)
    first = await client.credit_status(GATEWAY, "demo")
    assert len(first.credits) == 10 and first.credit_count == 25 and first.next_cursor is not None
    assert credit_gateway.status_bodies[0].keys() == {"nonce", "signature"}  # gateway default page
    rest = await client.credit_status(GATEWAY, "demo", before=first.next_cursor, limit=20)
    assert len(rest.credits) == 15 and rest.next_cursor is None
    assert [c.tx_id for c in first.credits + rest.credits] == [c["tx_id"] for c in credit_gateway.credits]
    credit_gateway.invalid_cursor_answers = 1
    with pytest.raises(PaymentRejectedError) as caught:
        await client.credit_status(GATEWAY, "demo", before=first.next_cursor)
    assert (caught.value.status_code, caught.value.code) == (400, "invalid_cursor")


@pytest.mark.parametrize(
    "kwargs",
    [{"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": "10"}, {"before": ""}, {"before": "c" * 513}, {"before": 5}],
)
async def test_bad_page_arguments_are_refused_before_any_request(
    make_client, credit_gateway: CreditGateway, kwargs: dict[str, Any]
) -> None:
    with pytest.raises(sdk.ConfigurationError):
        await make_client(credit_gateway).credit_status(GATEWAY, "demo", **kwargs)
    assert credit_gateway.challenge_calls == 0


@pytest.mark.parametrize("budget", [0, 101, True, 1.5])
def test_the_page_budget_is_bounded(budget: Any) -> None:
    with pytest.raises(sdk.ConfigurationError):
        sdk.KasGateClient(PAYER_KEY, credit_page_budget=budget, utxo_provider=object())  # type: ignore[arg-type]


def test_our_status_request_is_strict() -> None:
    good = {"nonce": NONCE, "signature": "ab" * 64}
    assert sdk.CreditStatusRequest.model_validate(good).body() == good
    assert sdk.CreditStatusRequest.model_validate(good | {"before": "x", "limit": 100}).body() == good | {
        "before": "x",
        "limit": 100,
    }
    for bad in (
        {"extra": 1},
        {"limit": 0},
        {"limit": 101},
        {"limit": "10"},
        {"limit": True},
        {"before": ""},
        {"signature": "AB" * 64},
        {"nonce": "x"},
    ):
        with pytest.raises(ValueError):
            sdk.CreditStatusRequest.model_validate(good | bad)
    # Every body the SDK sends is accepted by the gateway's own strict request model.
    gw.PrepaidCreditStatusRequest.model_validate(sdk.CreditStatusRequest.model_validate(good | {"limit": 100}).body())


def test_our_reply_models_refuse_extra_fields() -> None:
    item = credit_item()
    with pytest.raises(ValueError):
        sdk.BuyerCreditItem.model_validate(item | {"extra": 1})
    with pytest.raises(ValueError):
        sdk.CreditStatusChallenge.model_validate(
            {
                "nonce": NONCE,
                "message": "m",
                "action": "credits",
                "slug": "demo",
                "merchant_id": str(MERCHANT_ID),
                "payer_address": PAYER,
                "amount_sompi": "0",
                "refund_id": None,
                "expires_at": datetime.now(UTC).isoformat(),
                "hold_sompi": "1",
            }
        )


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
    credit_gateway.credits = [credit_item(credit_id=hint.creditId)]
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
    assert observation.confirmation is not None and observation.confirmation.basis == "credit_id"
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
            "next_cursor": None,
        }
    )


def test_judge_credit_and_model_strictness() -> None:
    hint, _ = parse_credit_header(receipt_header())
    assert hint is not None
    listed = _status([credit_item(credit_id=hint.creditId)], credited=MERCHANT_SHARE)
    assert judge_credit(hint, listed).confirmed
    assert not judge_credit(hint, _status([credit_item()])).confirmed  # same txId, another credit_id
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
