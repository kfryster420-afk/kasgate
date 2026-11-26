"""MCP free-trial tools (CONTRACT v2.12) over the real MCP protocol against a fake gateway built on the gateway models."""

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
from sdk_helpers import PAYER, PAYER_KEY, FakeGateway, make_utxo, static_utxos

from kasgate_mcp import Config, build_server

KAS = 100_000_000
MERCHANT_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
CATALOG_ITEM = {
    "slug": "weather",
    "name": "Weather",
    "description": None,
    "price_sompi": 15 * KAS,
    "price_currency": "KAS",
    "price_usd_micros": None,
    "methods": ["GET"],
    "merchant_address": "kaspatest:qq",
    "network": "kaspa:testnet-10",
    "schemes": ["kaspa-exact-v1", "kasgate-trial-v1"],
    "paid_url": "http://gateway.test/p/weather",
    "input_schema": None,
    "free_trial_calls": 1,
}


class TrialGateway:
    def __init__(self, trials: int = 1) -> None:
        self.paid = FakeGateway()
        self.trials = trials
        self.used = 0
        self.messages: dict[str, str] = {}
        self.upstream_status = 200
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        if path == "/v1/directory":
            return httpx.Response(404, json={"detail": "not found"})
        if path == "/v1/catalog/weather":
            return httpx.Response(200, json=CATALOG_ITEM)
        if path == "/v1/trials/status":
            return httpx.Response(
                200,
                json={
                    "slug": "weather",
                    "merchant_id": str(MERCHANT_ID),
                    "payer_address": request.url.params["payer_address"],
                    "free_trial_calls": self.trials,
                    "calls_used": self.used,
                    "remaining": self.trials - self.used,
                },
            )
        if path == "/v1/trials/challenge":
            body = gw.TrialChallengeRequest.model_validate(json.loads(request.content))
            if self.used >= self.trials:
                return httpx.Response(402, json={"error": "trial_exhausted", "message": "used up", "accepts": None})
            nonce = uuid.uuid4().hex
            expires = (datetime.now(UTC) + timedelta(seconds=60)).replace(microsecond=0)
            message = gw.build_trial_call_message(
                domain="gateway.test",
                address=body.payer_address,
                merchant_id=MERCHANT_ID,
                slug=body.slug,
                method=body.method,
                path=body.path,
                body_hash=body.body_hash,
                nonce=nonce,
                expires_at=expires,
            )
            self.messages[nonce] = message
            payload = gw.TrialChallenge(
                nonce=nonce,
                message=message,
                slug=body.slug,
                merchant_id=MERCHANT_ID,
                payer_address=body.payer_address,
                method=body.method,
                path=body.path,
                body_hash=body.body_hash,
                free_trial_calls=self.trials,
                calls_used=self.used,
                remaining=self.trials - self.used,
                expires_at=expires,
            )
            return httpx.Response(201, content=payload.model_dump_json(), headers={"content-type": "application/json"})
        raw = request.headers.get("X-KasGate-Trial")
        if raw is not None:
            header = gw.TrialCallHeader.from_header(raw)
            public_key = kaspa.PrivateKey(PAYER_KEY).to_public_key().to_x_only_public_key().to_string()
            assert kaspa.verify_message(self.messages.pop(header.nonce), header.signature, kaspa.PublicKey(public_key))
            refunded = self.upstream_status >= 500
            if not refunded:
                self.used += 1
            receipt = gw.TrialReceipt(
                trialCallId=uuid.uuid4(),
                nonce=header.nonce,
                limit=self.trials,
                remaining=self.trials - self.used,
                status="refunded" if refunded else "used",
            )
            return httpx.Response(
                self.upstream_status, headers={"X-KasGate-Trial-Receipt": receipt.to_header()}, json={"trial": True}
            )
        if "PAYMENT-SIGNATURE" not in request.headers and self.used < self.trials:
            offer = gw.TrialOffer(
                freeTrialCalls=self.trials,
                challengeUrl="http://gateway.test/v1/trials/challenge",
                statusUrl="http://gateway.test/v1/trials/status?slug=weather",
            )
            response = self.paid(request)
            response.headers["X-KasGate-Trial-Offer"] = offer.to_header()
            return response
        return self.paid(request)


def make_server(fake: TrialGateway, *, use_free_trials: bool = True):
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    buyer = KasGateClient(
        PAYER_KEY,
        max_price_sompi=20 * KAS,
        utxo_provider=static_utxos([make_utxo(0, 500 * KAS)]),
        http_client=http,
        use_free_trials=use_free_trials,
    )
    cfg = Config(
        gateway_url="http://gateway.test",
        buyer_private_key=PAYER_KEY,
        max_price_sompi=20 * KAS,
        session_budget_sompi=20 * KAS,
        use_free_trials=use_free_trials,
    )
    return build_server(cfg, http_client=http, buyer=buyer)


def structured(result: Any) -> dict[str, Any]:
    if getattr(result, "structured_content", None):
        return result.structured_content
    return json.loads(result.content[0].text)


async def test_call_with_budget_uses_the_trial_first_and_spends_nothing() -> None:
    fake = TrialGateway()
    async with Client(make_server(fake)) as client:
        status = structured(await client.call_tool("trial_status", {"slug": "weather"}))
        assert (status["remaining"], status["payer_address"]) == (1, PAYER)

        first = structured(await client.call_tool("call_with_budget", {"slug": "weather"}))
        assert first["status_code"] == 200
        assert first["payment"] is None
        assert (first["trial"]["status"], first["trial"]["remaining"], first["trial"]["paid_kas"]) == ("used", 0, "0")
        budget = structured(await client.call_tool("get_budget", {}))
        assert budget["spent_sompi"] == 0  # a trial never draws the session budget

        second = structured(await client.call_tool("call_with_budget", {"slug": "weather"}))
        assert second["trial"] is None
        assert second["payment"] is not None
        assert structured(await client.call_tool("get_budget", {}))["spent_sompi"] == 15 * KAS
    assert len(fake.paid.signatures) == 1


async def test_call_free_trial_never_pays() -> None:
    fake = TrialGateway()
    async with Client(make_server(fake)) as client:
        ok = structured(await client.call_tool("call_free_trial", {"slug": "weather", "path": "today?city=hanoi"}))
        assert ok["trial"]["status"] == "used" and ok["payment"] is None
        exhausted = await client.call_tool("call_free_trial", {"slug": "weather"})
        assert exhausted.is_error
        assert "trial_exhausted" in exhausted.content[0].text
    assert fake.paid.signatures == []
    assert "/p/weather/today" in fake.paths


async def test_refunded_trial_is_reported() -> None:
    fake = TrialGateway()
    fake.upstream_status = 503
    async with Client(make_server(fake)) as client:
        result = structured(await client.call_tool("call_free_trial", {"slug": "weather"}))
    assert result["status_code"] == 503
    assert result["trial"]["status"] == "refunded"


async def test_trials_can_be_disabled_by_config() -> None:
    fake = TrialGateway()
    async with Client(make_server(fake, use_free_trials=False)) as client:
        result = structured(await client.call_tool("call_paid_endpoint", {"slug": "weather"}))
    assert result["trial"] is None and result["payment"] is not None
    assert "/v1/trials/challenge" not in fake.paths


def test_config_reads_use_free_trials() -> None:
    assert Config.from_env({}).use_free_trials is True
    assert Config.from_env({"KASGATE_USE_FREE_TRIALS": "0"}).use_free_trials is False
    assert Config.from_env({"KASGATE_USE_FREE_TRIALS": "yes"}).use_free_trials is True
