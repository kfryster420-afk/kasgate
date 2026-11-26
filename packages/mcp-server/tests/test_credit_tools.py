"""MCP paid-call tools and `X-KasGate-Credit` (CONTRACT v2.14): a hint is never reported as a credit until the
gateway's SIGNED credit status lists it, and a credit never reduces what the session budget has spent."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import kaspa
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
NONCE = "0123456789abcdef" * 2
SLUG = "demo-abc123"


def xonly(key_hex: str) -> str:
    return kaspa.PrivateKey(key_hex).to_public_key().to_x_only_public_key().to_string()


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
    """A fake gateway whose paid route fails with 502 and (optionally) announces a credit, with the signed
    `/v1/prepaid/credits/challenge` + `/credits/status` built on the gateway's own models and message builder."""

    def __init__(self) -> None:
        self.fake = FakeGateway()
        self.fake.paid_handler = self.failing
        self.header: str | None = "auto"  # "auto": the genuine receipt; None: no header; anything else: sent as is
        self.register_credit = True  # the gateway really credited the paid call
        self.credits: list[dict[str, Any]] = []
        self.challenge_error: tuple[int, str] | None = None
        self.status_error: tuple[int, str] | None = None
        self.challenge_overrides: dict[str, Any] = {}
        self.used: set[str] = set()
        self.last: dict[str, Any] | None = None
        self.balance_calls = 0
        self.challenge_calls = 0
        self.status_calls = 0
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
            if self.register_credit and all(c["tx_id"] != tx_id for c in self.credits):
                self.credits.insert(0, credit_item(tx_id))
        elif self.header is not None:
            headers["X-KasGate-Credit"] = self.header
        return httpx.Response(
            self.status,
            headers=headers,
            json={"error": "upstream_error", "message": "down"},
        )

    @property
    def credited(self) -> int:
        return sum(int(c["credit_sompi"]) for c in self.credits)

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
            merchant_id=MERCHANT_ID,
            amount_sompi=0,
            refund_id=None,
            nonce=NONCE,
            expires_at=expires,
        )
        self.last = {"message": message}
        return httpx.Response(
            201,
            json={
                "nonce": NONCE,
                "message": message,
                "action": "credits",
                "slug": parsed.slug,
                "merchant_id": str(MERCHANT_ID),
                "payer_address": parsed.payer_address,
                "amount_sompi": "0",
                "refund_id": None,
                "spendable_sompi": "0",
                "refundable_sompi": "0",
                "expires_at": expires.isoformat(),
            }
            | self.challenge_overrides,
        )

    def credit_status(self, body: dict[str, Any]) -> httpx.Response:
        signed = gw.PrepaidRefundSigned.model_validate(body)
        self.status_calls += 1
        assert self.last is not None
        assert kaspa.verify_message(self.last["message"], signed.signature, kaspa.PublicKey(xonly(PAYER_KEY)))
        if self.status_error is not None:
            status, code = self.status_error
            return httpx.Response(status, json={"error": code, "message": "no"})
        if signed.nonce in self.used:
            return httpx.Response(409, json={"error": "challenge_replayed", "message": "used"})
        self.used.add(signed.nonce)
        reply = {
            "merchant_id": str(MERCHANT_ID),
            "credited_sompi": str(self.credited),
            "credit_balance_sompi": str(self.credited),
            "refundable_sompi": "0",
            "credit_count": len(self.credits),
            "credits": self.credits[:10],
        }
        gw.BuyerCreditStatus.model_validate(reply)
        return httpx.Response(200, json=reply)

    def handler(self) -> Any:
        catalog = gateway_with_catalog(self.fake)

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/prepaid/credits/challenge":
                return self.challenge(json.loads(request.content))
            if request.url.path == "/v1/prepaid/credits/status":
                return self.credit_status(json.loads(request.content))
            if request.url.path == "/v1/prepaid/balance":
                self.balance_calls += 1
                return httpx.Response(
                    200,
                    json={
                        "merchant_id": str(MERCHANT_ID),
                        "payer_address": request.url.params["payer_address"],
                        "balance_sompi": str(self.credited),
                        "deposited_sompi": "0",
                        "debited_sompi": "0",
                        "credited_sompi": str(self.credited),
                        "updated_at": None,
                    },
                )
            return catalog(request)

        return handle


def credit_item(tx_id: str, *, credit: int = MERCHANT_SHARE) -> dict[str, Any]:
    return {
        "tx_id": tx_id,
        "slug": SLUG,
        "credit_sompi": str(credit),
        "shortfall_sompi": "0",
        "reason": "attempts_exhausted",
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
    }


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
    data, _ = await call(make_world_server(world, auto_confirm=False), "call_paid_endpoint")
    credit = data["credit"]
    assert data["status_code"] == 502
    assert (credit["status"], credit["confirmed"]) == ("hint", False)
    assert credit["credit_sompi"] is None and credit["credit_kas"] is None  # no amount is stated as real
    assert credit["hint"]["credit_sompi"] == MERCHANT_SHARE and credit["confirmation"] is None
    assert "UNVERIFIED" in credit["note"] and "get_credit_status" in credit["note"]
    assert (world.challenge_calls, world.status_calls, world.balance_calls) == (0, 0, 0)


async def test_a_credit_the_signed_status_lists_is_confirmed() -> None:
    world = CreditWorld()
    data, _ = await call(make_world_server(world), "call_paid_endpoint")
    credit = data["credit"]
    assert (credit["status"], credit["confirmed"]) == ("confirmed", True)
    assert credit["credit_sompi"] == MERCHANT_SHARE and credit["credit_kas"] == "14.775"
    assert credit["confirmation"]["basis"] == "tx_id" and credit["confirmation"]["credited_sompi"] == MERCHANT_SHARE
    assert (world.challenge_calls, world.status_calls) == (1, 1) and world.balance_calls == 0  # no public balance
    assert "not refundable" in credit["note"]


async def test_a_header_the_gateway_never_credited_is_not_confirmed() -> None:
    world = CreditWorld()
    world.register_credit = False
    data, _ = await call(make_world_server(world), "call_paid_endpoint")
    credit = data["credit"]
    assert (credit["status"], credit["confirmed"], credit["credit_sompi"]) == ("unconfirmed", False, None)
    assert "lists no credit" in credit["confirmation"]["detail"] and "Do not rely" in credit["note"]


async def test_gateway_errors_leave_the_call_result_intact_and_the_credit_unconfirmed() -> None:
    for kind, code, name in (
        ("status", 429, "rate_limited"),
        ("status", 503, "service_unavailable"),
        ("challenge", 429, "rate_limited"),
        ("status", 410, "challenge_expired"),
    ):
        world = CreditWorld()
        setattr(world, f"{kind}_error", (code, name))
        data, _ = await call(make_world_server(world), "call_paid_endpoint")
        credit = data["credit"]
        assert data["status_code"] == 502 and data["payment"] is not None
        assert (credit["status"], credit["confirmed"], credit["credit_sompi"]) == ("unconfirmed", False, None)
        assert (credit["confirmation"]["http_status"], credit["confirmation"]["error_code"]) == (code, name)


async def test_a_wrong_action_challenge_is_never_signed_and_leaves_the_credit_unconfirmed() -> None:
    world = CreditWorld()
    world.challenge_overrides = {"action": "status"}
    data, _ = await call(make_world_server(world), "call_paid_endpoint")
    assert data["credit"]["status"] == "unconfirmed" and world.status_calls == 0


async def test_forged_and_malformed_headers_are_invalid_and_never_confirmed() -> None:
    for header in (
        "###",
        "A" * 5000,
        base64.b64encode(json.dumps({"kind": "failed_delivery_credit"}).encode()).decode(),
        credit_header("ab" * 32),  # names another payment
        credit_header("cd" * 32, credit=PRICE),  # wrong amount (and wrong tx for this call)
    ):
        world = CreditWorld()
        world.header = header
        world.credits = [credit_item("ab" * 32), credit_item("cd" * 32)]  # even listed credits cannot validate a bad hint
        data, _ = await call(make_world_server(world), "call_paid_endpoint")
        credit = data["credit"]
        assert credit["status"] == "invalid" and credit["confirmed"] is False, header[:20]
        assert credit["hint"] is None and credit["credit_sompi"] is None and credit["error"]
        assert world.challenge_calls == 0


async def test_no_header_no_credit_block() -> None:
    world = CreditWorld()
    world.header = None
    data, _ = await call(make_world_server(world), "call_paid_endpoint")
    assert data["credit"] is None and data["status_code"] == 502 and data["payment"] is not None
    assert world.challenge_calls == 0


async def test_a_success_response_has_no_credit() -> None:
    world = CreditWorld()
    world.fake.paid_handler = None
    data, _ = await call(make_world_server(world), "call_paid_endpoint")
    assert data["status_code"] == 200 and data["credit"] is None


async def test_call_with_budget_keeps_the_spend_even_with_a_confirmed_credit() -> None:
    world = CreditWorld()
    data, budget = await call(make_world_server(world), "call_with_budget")
    assert data["credit"]["status"] == "confirmed"
    assert budget["spent_sompi"] == PRICE and budget["reserved_sompi"] == 0  # not treated as refunded spend
    assert budget["remaining_sompi"] == 50 * KAS - PRICE
    assert data["budget"]["spent_sompi"] == PRICE


async def test_call_with_budget_keeps_the_spend_with_an_unconfirmed_hint() -> None:
    world = CreditWorld()
    data, budget = await call(make_world_server(world, auto_confirm=False), "call_with_budget")
    assert data["credit"]["status"] == "hint"
    assert budget["spent_sompi"] == PRICE and budget["remaining_sompi"] == 50 * KAS - PRICE


async def test_secrets_never_appear_in_credit_output() -> None:
    world = CreditWorld()
    data, _ = await call(make_world_server(world), "call_paid_endpoint")
    assert PAYER_KEY not in json.dumps(data)


# ---------------------------------------------------------------- get_credit_status


async def test_get_credit_status_lists_this_keys_credits() -> None:
    world = CreditWorld()
    world.credits = [credit_item("ab" * 32, credit=MERCHANT_SHARE)]
    async with Client(make_world_server(world)) as client:
        data = structured(await client.call_tool("get_credit_status", {"slug": SLUG}))
    assert data["merchant_id"] == str(MERCHANT_ID)
    assert (data["credited_sompi"], data["credit_balance_sompi"], data["credit_count"]) == (MERCHANT_SHARE,) * 2 + (1,)
    assert data["credited_kas"] == "14.775"
    [item] = data["credits"]
    assert (item["tx_id"], item["slug"], item["reason"], item["credit_sompi"]) == ("ab" * 32, SLUG, "attempts_exhausted", MERCHANT_SHARE)
    assert "not refundable" in data["note"] and PAYER_KEY not in json.dumps(data)
    assert (world.challenge_calls, world.status_calls, world.balance_calls) == (1, 1, 0)


async def test_get_credit_status_for_an_empty_buyer_is_zeros() -> None:
    world = CreditWorld()
    async with Client(make_world_server(world)) as client:
        data = structured(await client.call_tool("get_credit_status", {"slug": SLUG}))
    assert (data["credited_sompi"], data["credit_count"], data["credits"]) == (0, 0, [])


async def test_get_credit_status_errors_are_tool_errors_not_crashes() -> None:
    for kind, code, name in (
        ("challenge", 404, "endpoint_not_found"),
        ("status", 429, "rate_limited"),
        ("status", 503, "service_unavailable"),
        ("status", 410, "challenge_expired"),
        ("status", 409, "challenge_replayed"),
        ("status", 400, "invalid_signature"),
    ):
        world = CreditWorld()
        setattr(world, f"{kind}_error", (code, name))
        async with Client(make_world_server(world)) as client:
            result = await client.call_tool("get_credit_status", {"slug": SLUG})
        assert result.is_error and name in result.content[0].text and PAYER_KEY not in result.content[0].text


async def test_get_credit_status_refuses_a_wrong_action_challenge_and_bad_slugs() -> None:
    world = CreditWorld()
    world.challenge_overrides = {"action": "status"}
    async with Client(make_world_server(world)) as client:
        result = await client.call_tool("get_credit_status", {"slug": SLUG})
        bad = await client.call_tool("get_credit_status", {"slug": "../x"})
    assert result.is_error and world.status_calls == 0
    assert bad.is_error and world.challenge_calls == 1  # only the first call reached the gateway


async def test_prepaid_balance_reports_credited_sompi() -> None:
    world = CreditWorld()
    world.credits = [credit_item("ab" * 32, credit=123)]
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
