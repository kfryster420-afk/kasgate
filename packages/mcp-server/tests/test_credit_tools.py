"""MCP paid-call tools and `X-KasGate-Credit` (CONTRACT v2.14): a hint is never reported as a credit until the
public balance confirms it, and a credit never reduces what the session budget has spent."""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

import httpx
from app import schemas as gw
from kasgate_client import KasGateClient, PaymentResponse
from mcp import Client
from sdk_helpers import PAYER_KEY, PRICE, FakeGateway, make_utxo, static_utxos
from test_mcp_server import gateway_with_catalog, structured

from kasgate_mcp import Config, build_server

KAS = 100_000_000
MERCHANT_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
FEE = max(-(-PRICE * 150 // 10_000), 20_000_000)
MERCHANT_SHARE = PRICE - FEE


def credit_header(tx_id: str, *, credit: int = MERCHANT_SHARE) -> str:
    return gw.FailedDeliveryCreditReceipt(
        creditId=uuid.uuid4(),
        kind="failed_delivery_credit",
        txId=tx_id,
        merchantId=MERCHANT_ID,
        creditSompi=str(credit),
        balanceSompi=str(credit),
        reason="attempts_exhausted",
    ).to_header()


class CreditWorld:
    """A fake gateway whose paid route fails with 502 and (optionally) announces a credit; plus the public balance."""

    def __init__(self) -> None:
        self.fake = FakeGateway()
        self.fake.paid_handler = self.failing
        self.header: str | None = (
            "auto"  # "auto": the genuine receipt; None: no header; anything else: sent as is
        )
        self.credited = "0"
        self.balance_calls = 0
        self.status = 502

    def failing(self, request: httpx.Request, parsed: dict[str, Any]) -> httpx.Response:
        tx_id = parsed["signature"].txId
        headers = {
            "PAYMENT-RESPONSE": PaymentResponse(
                txId=tx_id,
                network="kaspa:testnet-10",
                usdValueAtSettlement="0.00000000",
            ).to_header()
        }
        if self.header == "auto":
            headers["X-KasGate-Credit"] = credit_header(tx_id)
        elif self.header is not None:
            headers["X-KasGate-Credit"] = self.header
        return httpx.Response(
            self.status,
            headers=headers,
            json={"error": "upstream_error", "message": "down"},
        )

    def handler(self) -> Any:
        catalog = gateway_with_catalog(self.fake)

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/prepaid/balance":
                self.balance_calls += 1
                return httpx.Response(
                    200,
                    json={
                        "merchant_id": str(MERCHANT_ID),
                        "payer_address": request.url.params["payer_address"],
                        "balance_sompi": self.credited,
                        "deposited_sompi": "0",
                        "debited_sompi": "0",
                        "credited_sompi": self.credited,
                        "updated_at": None,
                    },
                )
            return catalog(request)

        return handle


def make_world_server(
    world: CreditWorld, *, auto_confirm: bool = True, budget: int = 50 * KAS
) -> Any:
    http = httpx.AsyncClient(transport=httpx.MockTransport(world.handler()))
    buyer = KasGateClient(
        PAYER_KEY,
        max_price_sompi=20 * KAS,
        utxo_provider=static_utxos([make_utxo(0, 500 * KAS)]),
        http_client=http,
        unavailable_retry_delay=0,
    )
    cfg = Config(
        gateway_url="http://gateway.test",
        buyer_private_key=PAYER_KEY,
        max_price_sompi=20 * KAS,
        session_budget_sompi=budget,
        auto_confirm_credit=auto_confirm,
    )
    return build_server(cfg, http_client=http, buyer=buyer)


async def call(server: Any, tool: str) -> tuple[dict[str, Any], dict[str, Any]]:
    async with Client(server) as client:
        result = await client.call_tool(tool, {"slug": "demo-abc123"})
        assert not result.is_error, result.content[0].text
        budget = structured(await client.call_tool("get_budget", {}))
        return structured(result), budget


async def test_a_header_alone_is_reported_as_an_unverified_hint() -> None:
    world = CreditWorld()
    data, _ = await call(
        make_world_server(world, auto_confirm=False), "call_paid_endpoint"
    )
    credit = data["credit"]
    assert data["status_code"] == 502
    assert (credit["status"], credit["confirmed"]) == ("hint", False)
    assert (
        credit["credit_sompi"] is None and credit["credit_kas"] is None
    )  # no amount is stated as real
    assert (
        credit["hint"]["credit_sompi"] == MERCHANT_SHARE
        and credit["confirmation"] is None
    )
    assert "UNVERIFIED" in credit["note"]
    assert world.balance_calls == 0


async def test_a_credit_the_balance_shows_is_confirmed() -> None:
    world = CreditWorld()
    world.credited = str(MERCHANT_SHARE)
    data, _ = await call(make_world_server(world), "call_paid_endpoint")
    credit = data["credit"]
    assert (credit["status"], credit["confirmed"]) == ("confirmed", True)
    assert credit["credit_sompi"] == MERCHANT_SHARE and credit["credit_kas"] == "14.775"
    assert (
        credit["confirmation"]["basis"] == "cumulative"
        and credit["confirmation"]["credited_sompi"] == MERCHANT_SHARE
    )
    assert world.balance_calls == 1
    assert "not refundable" in credit["note"]


async def test_a_header_with_no_balance_increase_is_not_confirmed() -> None:
    world = CreditWorld()
    world.credited = "0"
    data, _ = await call(make_world_server(world), "call_paid_endpoint")
    credit = data["credit"]
    assert (credit["status"], credit["confirmed"], credit["credit_sompi"]) == (
        "unconfirmed",
        False,
        None,
    )
    assert (
        credit["confirmation"]["credited_sompi"] == 0
        and "less than" in credit["confirmation"]["detail"]
    )
    assert "Do not rely" in credit["note"]


async def test_forged_and_malformed_headers_are_invalid_and_never_confirmed() -> None:
    for header in (
        "###",
        "A" * 5000,
        base64.b64encode(
            json.dumps({"kind": "failed_delivery_credit"}).encode()
        ).decode(),
        credit_header("ab" * 32),  # names another payment
        credit_header(
            "cd" * 32, credit=PRICE
        ),  # wrong amount (and wrong tx for this call)
    ):
        world = CreditWorld()
        world.header = header
        world.credited = str(10**12)  # even a huge balance cannot validate a bad hint
        data, _ = await call(make_world_server(world), "call_paid_endpoint")
        credit = data["credit"]
        assert credit["status"] == "invalid" and credit["confirmed"] is False, header[
            :20
        ]
        assert (
            credit["hint"] is None
            and credit["credit_sompi"] is None
            and credit["error"]
        )
        assert world.balance_calls == 0


async def test_no_header_no_credit_block() -> None:
    world = CreditWorld()
    world.header = None
    data, _ = await call(make_world_server(world), "call_paid_endpoint")
    assert (
        data["credit"] is None
        and data["status_code"] == 502
        and data["payment"] is not None
    )


async def test_a_success_response_has_no_credit() -> None:
    world = CreditWorld()
    world.fake.paid_handler = None
    data, _ = await call(make_world_server(world), "call_paid_endpoint")
    assert data["status_code"] == 200 and data["credit"] is None


async def test_call_with_budget_keeps_the_spend_even_with_a_confirmed_credit() -> None:
    world = CreditWorld()
    world.credited = str(MERCHANT_SHARE)
    data, budget = await call(make_world_server(world), "call_with_budget")
    assert data["credit"]["status"] == "confirmed"
    assert (
        budget["spent_sompi"] == PRICE and budget["reserved_sompi"] == 0
    )  # not treated as refunded spend
    assert budget["remaining_sompi"] == 50 * KAS - PRICE
    assert data["budget"]["spent_sompi"] == PRICE


async def test_call_with_budget_keeps_the_spend_with_an_unconfirmed_hint() -> None:
    world = CreditWorld()
    world.credited = "0"
    data, budget = await call(
        make_world_server(world, auto_confirm=False), "call_with_budget"
    )
    assert data["credit"]["status"] == "hint"
    assert (
        budget["spent_sompi"] == PRICE and budget["remaining_sompi"] == 50 * KAS - PRICE
    )


async def test_secrets_never_appear_in_credit_output() -> None:
    world = CreditWorld()
    world.credited = str(MERCHANT_SHARE)
    data, _ = await call(make_world_server(world), "call_paid_endpoint")
    assert PAYER_KEY not in json.dumps(data)


async def test_prepaid_balance_reports_credited_sompi() -> None:
    world = CreditWorld()
    world.credited = "123"
    async with Client(make_world_server(world)) as client:
        result = structured(
            await client.call_tool("prepaid_balance", {"slug": "demo-abc123"})
        )
    assert result["credited_sompi"] == 123


def test_config_flag_defaults_on_and_can_be_switched_off() -> None:
    assert Config.from_env({}).auto_confirm_credit is True
    assert (
        Config.from_env({"KASGATE_AUTO_CONFIRM_CREDIT": "0"}).auto_confirm_credit
        is False
    )
