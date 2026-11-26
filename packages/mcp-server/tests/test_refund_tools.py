"""MCP prepaid refund tools (CONTRACT v2.13) over the real MCP protocol against a fake gateway on the gateway models."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import kaspa
from app import schemas as gw
from kasgate_client import KasGateClient
from mcp import Client
from sdk_helpers import PAYER, PAYER_KEY, make_utxo, static_utxos

from kasgate_mcp import Config, build_server

KAS = 100_000_000
MERCHANT_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
NONCE = "fedcba9876543210" * 2


class RefundGateway:
    def __init__(self) -> None:
        self.spendable = 40_000_000
        self.message = ""
        self.amount = 0
        self.action = "request"
        self.refunds: list[dict[str, Any]] = []
        self.paths: list[str] = []
        self.refund_id = uuid.uuid4()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        if path == "/v1/prepaid/refunds/challenge":
            body = gw.PrepaidRefundChallengeRequest.model_validate(json.loads(request.content))
            amount = body.amount_sompi if body.amount_sompi is not None else self.spendable
            if body.action == "request" and amount > self.spendable:
                return httpx.Response(409, json={"error": "insufficient_balance", "message": "too much"})
            expires = datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=60)
            self.amount, self.action = amount, body.action
            self.message = gw.build_prepaid_refund_message(
                domain="gateway.test",
                action=body.action,
                address=body.payer_address,
                merchant_id=MERCHANT_ID,
                amount_sompi=amount,
                refund_id=body.refund_id,
                nonce=NONCE,
                expires_at=expires,
            )
            challenge = gw.PrepaidRefundChallenge(
                nonce=NONCE,
                message=self.message,
                action=body.action,
                slug=body.slug,
                merchant_id=MERCHANT_ID,
                payer_address=body.payer_address,
                amount_sompi=str(amount),
                refund_id=body.refund_id,
                spendable_sompi=str(self.spendable),
                expires_at=expires,
            )
            return httpx.Response(201, content=challenge.model_dump_json(), headers={"content-type": "application/json"})
        if path == "/v1/prepaid/refunds/status":
            return httpx.Response(200, json=self.refunds)
        signed = gw.PrepaidRefundSigned.model_validate(json.loads(request.content))
        public_key = kaspa.PrivateKey(PAYER_KEY).to_public_key().to_x_only_public_key().to_string()
        assert kaspa.verify_message(self.message, signed.signature, kaspa.PublicKey(public_key))
        now = datetime.now(UTC).isoformat()
        cancelled = self.action == "cancel"
        refund = {
            "id": str(self.refund_id),
            "merchant_id": str(MERCHANT_ID),
            "payer_address": PAYER,
            "amount_sompi": str(self.amount),
            "status": "cancelled" if cancelled else "requested",
            "tx_id": None,
            "reason": None,
            "last_error": None,
            "created_at": now,
            "updated_at": now,
            "payout_submitted_at": None,
            "paid_at": None,
            "resolved_at": now if cancelled else None,
            "payout_address": PAYER,
            "payout_payload_hex": gw.refund_payout_payload(self.refund_id).hex(),
        }
        self.refunds.insert(0, refund)
        return httpx.Response(200 if cancelled else 201, json=refund)


def make_server(fake: RefundGateway) -> Any:
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    buyer = KasGateClient(
        PAYER_KEY, max_price_sompi=20 * KAS, utxo_provider=static_utxos([make_utxo(0, 500 * KAS)]), http_client=http
    )
    cfg = Config(
        gateway_url="http://gateway.test",
        buyer_private_key=PAYER_KEY,
        max_price_sompi=20 * KAS,
        session_budget_sompi=20 * KAS,
    )
    return build_server(cfg, http_client=http, buyer=buyer)


def structured(result: Any) -> dict[str, Any]:
    if getattr(result, "structured_content", None):
        return result.structured_content
    return json.loads(result.content[0].text)


async def test_request_list_and_cancel_a_refund() -> None:
    fake = RefundGateway()
    async with Client(make_server(fake)) as client:
        opened = await client.call_tool("prepaid_refund_request", {"slug": "cheap-api", "amount_kas": "0.25"})
        assert not opened.is_error, opened.content[0].text
        data = structured(opened)
        assert (data["status"], data["amount_sompi"], data["amount_kas"]) == ("requested", 25_000_000, "0.25")
        assert data["payout_address"] == PAYER
        listed = structured(await client.call_tool("prepaid_refunds", {"slug": "cheap-api"}))
        assert [r["refund_id"] for r in listed["refunds"]] == [data["refund_id"]]
        cancelled = await client.call_tool(
            "prepaid_refund_cancel", {"slug": "cheap-api", "refund_id": data["refund_id"]}
        )
        assert not cancelled.is_error, cancelled.content[0].text
        assert structured(cancelled)["status"] == "cancelled"
        budget = structured(await client.call_tool("get_budget", {}))
    assert budget["spent_sompi"] == 0  # a refund request never spends the session budget
    assert f"/v1/prepaid/refunds/{fake.refund_id}/cancel" in fake.paths


async def test_request_all_credit_and_errors_are_tool_errors() -> None:
    fake = RefundGateway()
    async with Client(make_server(fake)) as client:
        everything = structured(await client.call_tool("prepaid_refund_request", {"slug": "cheap-api"}))
        assert everything["amount_sompi"] == 40_000_000
        too_much = await client.call_tool("prepaid_refund_request", {"slug": "cheap-api", "amount_kas": "1"})
        assert too_much.is_error and "InsufficientPrepaidBalanceError" in too_much.content[0].text
        bad_id = await client.call_tool("prepaid_refund_cancel", {"slug": "cheap-api", "refund_id": "nope"})
        assert bad_id.is_error and "UUID" in bad_id.content[0].text
        bad_slug = await client.call_tool("prepaid_refunds", {"slug": "../x"})
        assert bad_slug.is_error
