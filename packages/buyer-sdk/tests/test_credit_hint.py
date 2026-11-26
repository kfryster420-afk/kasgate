"""CONTRACT v2.14 `X-KasGate-Credit`: parsed as an unsigned hint, confirmed only against the public prepaid balance."""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

import httpx
import pytest
from app import schemas as gw
from conftest import PRICE, FakeGateway

import kasgate_client as sdk
from kasgate_client import (
    CreditObservation,
    FailedDeliveryCreditHint,
    credit_of,
    judge_credit,
    observe_credit,
    parse_credit_header,
    receipt_of,
)

MERCHANT_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
FEE = max(-(-PRICE * 150 // 10_000), 20_000_000)
MERCHANT_SHARE = PRICE - FEE
TX = "cd" * 32


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


class CreditGateway(FakeGateway):
    """The paid route answers 502 with an X-KasGate-Credit header; /v1/prepaid/balance reports `credited`."""

    def __init__(self) -> None:
        super().__init__()
        self.credit_header: str | None = None
        self.credit_for_tx: bool = True
        self.credited_sompi: str | None = "0"
        self.balance_merchant = MERCHANT_ID
        self.balance_status = 200
        self.balance_calls = 0
        self.status = 502
        self.paid_handler = self.failing_upstream

    def failing_upstream(
        self, request: httpx.Request, parsed: dict[str, Any]
    ) -> httpx.Response:
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
        return httpx.Response(
            self.status,
            headers=headers,
            json={"error": "upstream_error", "message": "down"},
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/prepaid/balance":
            self.balance_calls += 1
            if self.balance_status != 200:
                return httpx.Response(
                    self.balance_status,
                    json={"error": "service_unavailable", "message": "no"},
                )
            body: dict[str, Any] = {
                "merchant_id": str(self.balance_merchant),
                "payer_address": request.url.params["payer_address"],
                "balance_sompi": "100",
                "deposited_sompi": "0",
                "debited_sompi": "0",
                "updated_at": None,
            }
            if self.credited_sompi is not None:
                body["credited_sompi"] = self.credited_sompi
            return httpx.Response(200, json=body)
        return super().__call__(request)


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
    assert (
        credit_gateway.balance_calls == 0
    )  # a hint alone never triggers a balance read by default


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


# ---------------------------------------------------------------- confirmation


async def test_a_header_present_but_the_balance_not_increased_is_unconfirmed(
    make_client, credit_gateway: CreditGateway
) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    credit_gateway.credited_sompi = "0"
    checked = await client.confirm_credit(
        "http://gateway.test", "demo", response, baseline_credited_sompi=0
    )
    assert checked.status == "unconfirmed" and checked.confirmed is False
    assert checked.confirmation is not None and checked.confirmation.basis == "increase"
    assert (
        "less than" in checked.confirmation.detail
        and checked.confirmation.credited_sompi == 0
    )


async def test_a_credit_that_was_already_counted_before_the_call_is_not_confirmed(
    make_client, credit_gateway: CreditGateway
) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    credit_gateway.credited_sompi = str(
        MERCHANT_SHARE
    )  # an earlier credit; nothing new arrived
    checked = await client.confirm_credit(
        "http://gateway.test", "demo", response, baseline_credited_sompi=MERCHANT_SHARE
    )
    assert checked.status == "unconfirmed"


async def test_a_credit_that_shows_in_the_balance_is_confirmed(
    make_client, credit_gateway: CreditGateway
) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    credit_gateway.credited_sompi = str(MERCHANT_SHARE)
    checked = await client.confirm_credit(
        "http://gateway.test", "demo", response, baseline_credited_sompi=0
    )
    assert checked.status == "confirmed" and checked.confirmed is True
    assert checked.confirmation is not None
    assert (checked.confirmation.basis, checked.confirmation.credited_sompi) == (
        "increase",
        MERCHANT_SHARE,
    )
    assert credit_of(response) is not None and credit_of(response).status == "hint"  # type: ignore[union-attr]


async def test_without_a_baseline_the_check_is_cumulative(
    make_client, credit_gateway: CreditGateway
) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    credit_gateway.credited_sompi = str(MERCHANT_SHARE)
    confirmed = await client.confirm_credit("http://gateway.test", "demo", response)
    assert (
        confirmed.confirmed
        and confirmed.confirmation is not None
        and confirmed.confirmation.basis == "cumulative"
    )
    credit_gateway.credited_sompi = str(MERCHANT_SHARE - 1)
    short = await client.confirm_credit("http://gateway.test", "demo", response)
    assert short.status == "unconfirmed"


async def test_the_balance_of_another_merchant_does_not_confirm(
    make_client, credit_gateway: CreditGateway
) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    credit_gateway.credited_sompi = str(MERCHANT_SHARE)
    credit_gateway.balance_merchant = uuid.uuid4()
    checked = await client.confirm_credit(
        "http://gateway.test", "demo", response, baseline_credited_sompi=0
    )
    assert checked.status == "unconfirmed"
    assert (
        checked.confirmation is not None
        and "different merchant" in checked.confirmation.detail
    )


async def test_an_old_gateway_without_credited_sompi_never_confirms(
    make_client, credit_gateway: CreditGateway
) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    credit_gateway.credited_sompi = None
    checked = await client.confirm_credit("http://gateway.test", "demo", response)
    assert checked.status == "unconfirmed" and "credited_sompi" in (
        checked.confirmation.detail if checked.confirmation else ""
    )


async def test_a_failing_balance_check_is_unconfirmed_not_an_error(
    make_client, credit_gateway: CreditGateway
) -> None:
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    credit_gateway.balance_status = 503
    checked = await client.confirm_credit("http://gateway.test", "demo", response)
    assert checked.status == "unconfirmed" and checked.confirmation is not None
    assert (
        checked.confirmation.basis == "none"
        and "unverified" in checked.confirmation.detail
    )


async def test_an_invalid_observation_is_never_confirmed(
    make_client, credit_gateway: CreditGateway
) -> None:
    credit_gateway.credit_header = "###"
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    credit_gateway.credited_sompi = str(10**9)
    checked = await client.confirm_credit(
        "http://gateway.test", "demo", response, baseline_credited_sompi=0
    )
    assert checked.status == "invalid" and credit_gateway.balance_calls == 0


async def test_confirm_credit_argument_errors(
    make_client, credit_gateway: CreditGateway
) -> None:
    credit_gateway.credit_for_tx = False
    client = make_client(credit_gateway, unavailable_retry_delay=0)
    response = await client.get("/p/demo")
    with pytest.raises(sdk.ConfigurationError):
        await client.confirm_credit("http://gateway.test", "demo", response)
    hint, _ = parse_credit_header(receipt_header())
    assert hint is not None
    for bad in (-1, True, 1.5):
        with pytest.raises(sdk.ConfigurationError):
            await client.confirm_credit(
                "http://gateway.test", "demo", hint, baseline_credited_sompi=bad
            )  # type: ignore[arg-type]
    with pytest.raises(sdk.ConfigurationError):
        await client.confirm_credit("http://gateway.test", "demo", "nope")  # type: ignore[arg-type]
    credit_gateway.credited_sompi = str(MERCHANT_SHARE)
    assert (await client.confirm_credit("http://gateway.test", "demo", hint)).confirmed


# ---------------------------------------------------------------- auto-confirm


async def test_auto_confirm_takes_a_baseline_and_confirms_a_real_increase(
    make_client, credit_gateway: CreditGateway
) -> None:
    client = make_client(
        credit_gateway, unavailable_retry_delay=0, auto_confirm_credit=True
    )
    credit_gateway.credited_sompi = "500"
    original = credit_gateway.failing_upstream

    def credited_by_the_gateway(
        request: httpx.Request, parsed: dict[str, Any]
    ) -> httpx.Response:
        credit_gateway.credited_sompi = str(
            500 + MERCHANT_SHARE
        )  # the inline credit lands before the response
        return original(request, parsed)

    credit_gateway.paid_handler = credited_by_the_gateway
    response = await client.get("/p/demo")
    observation = credit_of(response)
    assert observation is not None and observation.status == "confirmed"
    assert observation.confirmation is not None
    assert (
        observation.confirmation.basis,
        observation.confirmation.baseline_credited_sompi,
    ) == ("increase", 500)
    assert credit_gateway.balance_calls == 2  # one before the call, one to confirm


async def test_auto_confirm_with_a_header_but_no_increase_stays_unconfirmed(
    make_client, credit_gateway: CreditGateway
) -> None:
    client = make_client(
        credit_gateway, unavailable_retry_delay=0, auto_confirm_credit=True
    )
    credit_gateway.credited_sompi = "500"
    observation = credit_of(await client.get("/p/demo"))
    assert (
        observation is not None
        and observation.status == "unconfirmed"
        and not observation.confirmed
    )
    assert (
        observation.confirmation is not None
        and observation.confirmation.baseline_credited_sompi == 500
    )


async def test_auto_confirm_survives_a_dead_balance_endpoint(
    make_client, credit_gateway: CreditGateway
) -> None:
    credit_gateway.balance_status = 503
    client = make_client(
        credit_gateway, unavailable_retry_delay=0, auto_confirm_credit=True
    )
    response = await client.get("/p/demo")
    observation = credit_of(response)
    assert response.status_code == 502
    assert observation is not None and observation.status == "unconfirmed"


async def test_auto_confirm_off_by_default_and_no_balance_read_without_a_proxy_path(
    make_client, credit_gateway: CreditGateway
) -> None:
    client = make_client(
        credit_gateway, unavailable_retry_delay=0, auto_confirm_credit=True
    )
    response = await client.get("/not-a-proxy-path")
    observation = credit_of(response)
    assert observation is not None and observation.status == "unconfirmed"
    assert (
        observation.confirmation is not None
        and observation.confirmation.basis == "none"
    )
    assert credit_gateway.balance_calls == 0
    assert make_client(credit_gateway).auto_confirm_credit is False


# ---------------------------------------------------------------- judge_credit and models


def test_judge_credit_and_model_strictness() -> None:
    hint, _ = parse_credit_header(receipt_header())
    assert hint is not None
    balance = sdk.PrepaidBalance(
        merchant_id=MERCHANT_ID,
        payer_address="kaspatest:q",
        balance_sompi="1",
        deposited_sompi="0",
        debited_sompi="0",
        credited_sompi=str(MERCHANT_SHARE),
    )
    assert judge_credit(hint, balance).confirmed
    assert not judge_credit(hint, balance, baseline_credited_sompi=1).confirmed
    with pytest.raises(ValueError):
        sdk.CreditObservation(status="confirmed", extra=1)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        hint.creditSompi = "1"  # type: ignore[misc]
