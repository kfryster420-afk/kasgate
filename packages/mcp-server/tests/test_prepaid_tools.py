"""MCP prepaid tools (CONTRACT v2.11) over the real MCP protocol against a fake gateway built on the gateway models."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import kaspa
from app import schemas as gw
from kasgate_client import KasGateClient, compute_fee_split
from mcp import Client
from sdk_helpers import PAYER, PAYER_KEY, challenge, make_utxo, static_utxos

from kasgate_mcp import Config, build_server

KAS = 100_000_000
MERCHANT_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
DEBIT_NONCE = "abcdef0123456789" * 2



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

class Gateway:
    def __init__(self) -> None:
        self.balance = 0
        self.price = 1000
        self.message = ""
        self.upstream_status = 200
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        if path == "/v1/directory":
            return httpx.Response(404, json={"detail": "not found"})
        if path == "/v1/prepaid/deposits/quote":
            body = gw.PrepaidDepositQuoteRequest.model_validate(json.loads(request.content))
            merchant, fee = compute_fee_split(body.amount_sompi)
            return httpx.Response(
                201,
                json={
                    "slug": body.slug,
                    "merchant_id": str(MERCHANT_ID),
                    "payer_address": body.payer_address,
                    "deposit_sompi": str(body.amount_sompi),
                    "fee_sompi": str(fee),
                    "credit_sompi": str(merchant),
                    "balance_sompi": str(self.balance),
                    "required": json.loads(challenge(body.amount_sompi).model_dump_json()),
                    "deposit_url": "http://gateway.test/v1/prepaid/deposits",
                },
            )
        if path == "/v1/prepaid/deposits":
            signature = gw.PaymentSignature.from_header(request.headers["PAYMENT-SIGNATURE"])
            tx = kaspa.Transaction.from_dict(json.loads(signature.rawTx))
            credit, fee = tx.outputs[0].value, tx.outputs[1].value
            self.balance += credit
            return httpx.Response(
                201,
                headers={
                    "PAYMENT-RESPONSE": gw.PaymentResponse(
                        txId=signature.txId, network="kaspa:testnet-10", usdValueAtSettlement="0"
                    ).to_header()
                },
                json={
                    "deposit_id": str(uuid.uuid4()),
                    "tx_id": signature.txId,
                    "slug": "cheap-api",
                    "merchant_id": str(MERCHANT_ID),
                    "payer_address": signature.payerAddress,
                    "deposit_sompi": str(credit + fee),
                    "fee_sompi": str(fee),
                    "credit_sompi": str(credit),
                    "balance_sompi": str(self.balance),
                    "created_at": datetime.now(UTC).isoformat(),
                },
            )
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
        if path == "/v1/prepaid/challenge":
            body = gw.PrepaidChallengeRequest.model_validate(json.loads(request.content))
            if self.balance < self.price:
                return httpx.Response(
                    402, json={"error": "insufficient_balance", "message": "top up", "accepts": None}
                )
            expires = datetime.now(UTC) + timedelta(seconds=60)
            self.message = gw.build_prepaid_debit_message(
                domain="gateway.test",
                address=body.payer_address,
                merchant_id=MERCHANT_ID,
                method=body.method,
                path=body.path,
                body_hash=body.body_hash,
                amount_sompi=self.price,
                nonce=DEBIT_NONCE,
                expires_at=expires,
            )
            payload = gw.PrepaidChallenge(
                nonce=DEBIT_NONCE,
                message=self.message,
                slug=body.slug,
                merchant_id=MERCHANT_ID,
                payer_address=body.payer_address,
                method=body.method,
                path=body.path,
                body_hash=body.body_hash,
                amount_sompi=str(self.price),
                kas_usd_rate=None,
                balance_sompi=str(self.balance),
                expires_at=expires,
            )
            return httpx.Response(201, content=payload.model_dump_json(), headers={"content-type": "application/json"})
        header = gw.PrepaidDebitHeader.from_header(request.headers["X-KasGate-Prepaid"])
        public_key = kaspa.PrivateKey(PAYER_KEY).to_public_key().to_x_only_public_key().to_string()
        assert kaspa.verify_message(self.message, header.signature, kaspa.PublicKey(public_key))
        refunded = self.upstream_status >= 500
        if not refunded:
            self.balance -= self.price
        receipt = gw.PrepaidReceipt(
            debitId=uuid.uuid4(),
            nonce=header.nonce,
            amountSompi=str(self.price),
            balanceSompi=str(self.balance),
            status="refunded" if refunded else "debited",
        )
        return httpx.Response(
            self.upstream_status, headers={"X-KasGate-Prepaid-Receipt": receipt.to_header()}, json={"ok": not refunded}
        )


def make_server(fake: Gateway, *, session_budget_sompi: int = 20 * KAS):
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    buyer = KasGateClient(
        PAYER_KEY,
        max_price_sompi=20 * KAS,
        utxo_provider=static_utxos([make_utxo(0, 500 * KAS)]),
        http_client=http,
    )
    cfg = Config(
        gateway_url="http://gateway.test",
        buyer_private_key=PAYER_KEY,
        max_price_sompi=20 * KAS,
        session_budget_sompi=session_budget_sompi,
    )
    return build_server(cfg, http_client=http, buyer=buyer)


def structured(result: Any) -> dict[str, Any]:
    if getattr(result, "structured_content", None):
        return result.structured_content
    return json.loads(result.content[0].text)


async def test_deposit_then_prepaid_calls_and_balance() -> None:
    fake = Gateway()
    server = make_server(fake)
    async with Client(server) as client:
        deposit = await client.call_tool("prepaid_deposit", {"slug": "cheap-api", "amount_kas": "1"})
        assert not deposit.is_error, deposit.content[0].text
        data = structured(deposit)
        merchant, fee = compute_fee_split(KAS)
        assert data["credit_kas"] == "0.8" and data["platform_fee_kas"] == "0.2"
        assert (merchant, fee) == (80_000_000, 20_000_000)
        assert data["budget"]["spent_sompi"] == KAS

        call = await client.call_tool(
            "call_prepaid_endpoint", {"slug": "cheap-api", "path": "v1/items?limit=3", "max_price_kas": "0.001"}
        )
        assert not call.is_error, call.content[0].text
        result = structured(call)
        assert result["status_code"] == 200
        assert result["prepaid"]["status"] == "debited"
        assert result["prepaid"]["balance_sompi"] == merchant - 1000

        balance = structured(await client.call_tool("prepaid_balance", {"slug": "cheap-api"}))
        assert balance["balance_sompi"] == merchant - 1000 and balance["payer_address"] == PAYER
        assert balance["deposited_sompi"] == merchant - 1000 and balance["debited_sompi"] == 0  # signed read
        budget = structured(await client.call_tool("get_budget", {}))
    assert budget["spent_sompi"] == KAS  # debits do not draw the session budget a second time
    assert "/p/cheap-api/v1/items" in fake.paths


async def test_deposit_over_session_budget_is_refused_before_signing() -> None:
    fake = Gateway()
    server = make_server(fake, session_budget_sompi=KAS)
    async with Client(server) as client:
        result = await client.call_tool("prepaid_deposit", {"slug": "cheap-api", "amount_kas": "2"})
        assert result.is_error
        assert "session budget" in result.content[0].text
    assert "/v1/prepaid/deposits" not in fake.paths


async def test_deposit_below_minimum_releases_the_reservation() -> None:
    fake = Gateway()
    server = make_server(fake)
    async with Client(server) as client:
        result = await client.call_tool("prepaid_deposit", {"slug": "cheap-api", "amount_kas": "0.5"})
        assert result.is_error
        budget = structured(await client.call_tool("get_budget", {}))
    assert budget["reserved_sompi"] == 0 and budget["spent_sompi"] == 0


async def test_prepaid_call_without_balance_is_a_tool_error() -> None:
    fake = Gateway()
    server = make_server(fake)
    async with Client(server) as client:
        result = await client.call_tool("call_prepaid_endpoint", {"slug": "cheap-api"})
    assert result.is_error
    assert "InsufficientPrepaidBalanceError" in result.content[0].text
    assert "/p/cheap-api" not in fake.paths


async def test_prepaid_call_over_cap_is_never_signed() -> None:
    fake = Gateway()
    fake.balance = 10 * KAS
    fake.price = KAS
    server = make_server(fake)
    async with Client(server) as client:
        result = await client.call_tool("call_prepaid_endpoint", {"slug": "cheap-api", "max_price_kas": "0.5"})
    assert result.is_error
    assert "/p/cheap-api" not in fake.paths


async def test_refunded_upstream_failure_is_reported() -> None:
    fake = Gateway()
    fake.balance = 5000
    fake.upstream_status = 502
    server = make_server(fake)
    async with Client(server) as client:
        result = structured(await client.call_tool("call_prepaid_endpoint", {"slug": "cheap-api"}))
    assert result["status_code"] == 502
    assert result["prepaid"]["status"] == "refunded" and result["prepaid"]["balance_sompi"] == 5000


async def test_prepaid_tools_validate_slug_and_path() -> None:
    server = make_server(Gateway())
    async with Client(server) as client:
        bad_slug = await client.call_tool("prepaid_balance", {"slug": "../x"})
        bad_path = await client.call_tool("call_prepaid_endpoint", {"slug": "cheap-api", "path": "../../v1/admin"})
    assert bad_slug.is_error and bad_path.is_error


def test_server_exposes_its_config() -> None:
    server = make_server(Gateway())
    assert server.kasgate_config.gateway_url == "http://gateway.test"
