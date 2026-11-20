from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import kaspa
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GATEWAY_DIR = REPO_ROOT / "apps" / "gateway"

PAYER_KEY = "b7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfef"
MERCHANT_KEY = "c7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfef"
TREASURY_KEY = "d7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfef"
OTHER_KEY = "e7e151628aed2a6abf7158809cf4f3c762e7160f38b4da56a784d9045190cfef"


def address_of(key_hex: str, network_type: str = "testnet") -> str:
    return kaspa.PrivateKey(key_hex).to_address(network_type).to_string()


PAYER = address_of(PAYER_KEY)
MERCHANT = address_of(MERCHANT_KEY)
TREASURY = address_of(TREASURY_KEY)
OTHER = address_of(OTHER_KEY)

os.environ.setdefault("KASPA_NETWORK", "kaspa:testnet-10")
os.environ.setdefault("KASGATE_TREASURY_ADDRESS", TREASURY)
os.environ.setdefault("ENCRYPTION_KEY", "A" * 43 + "=")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
if str(GATEWAY_DIR) not in sys.path:
    sys.path.insert(0, str(GATEWAY_DIR))

from kasgate_client import PaymentRequired, PlatformFee
from kasgate_client.protocol import min_price_sompi

PRICE = 1_500_000_000
FLOOR = 20_000_000
MIN_PRICE = min_price_sompi(
    FLOOR
)  # CONTRACT v2.3: 3 * floor = 60_000_000 sompi (0.6 KAS)
NONCE = "0123456789abcdef" * 2


def future(seconds: float = 120.0) -> datetime:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).replace(microsecond=0)


def make_utxo(
    index: int, amount: int, address: str = PAYER, tx_prefix: int = 0xAA
) -> dict[str, Any]:
    script = kaspa.pay_to_address_script(kaspa.Address(address)).script
    return {
        "address": address,
        "outpoint": {
            "transactionId": f"{tx_prefix:02x}" * 31 + f"{index:02x}",
            "index": 0,
        },
        "amount": amount,
        "scriptPublicKey": {"version": 0, "script": script},
        "blockDaaScore": 1000,
        "isCoinbase": False,
        "covenantId": None,
    }


def challenge(
    price: int = PRICE,
    *,
    pay_to: str = MERCHANT,
    fee_to: str = TREASURY,
    network: str = "kaspa:testnet-10",
    fee_amount: int | None = None,
    floor: int = FLOOR,
    nonce: str = NONCE,
    expires_in: float = 120.0,
    **overrides: Any,
) -> PaymentRequired:
    fee = max(
        -(-price * 150 // 10_000), floor
    )  # independent of the SDK: v2.1 fee = max(ceil(1.5%), floor)
    fields: dict[str, Any] = {
        "x402Version": 2,
        "scheme": "kaspa-exact-v1",
        "network": network,
        "asset": "KAS",
        "amount": str(price),
        "payTo": pay_to,
        "platformFee": PlatformFee(
            feeTo=fee_to, feeAmount=str(fee if fee_amount is None else fee_amount)
        ),
        "nonce": nonce,
        "expiresAt": future(expires_in),
    }
    fields.update(overrides)
    return PaymentRequired(**fields)


class FakeGateway:
    """Programmable stand-in for the gateway's paywalled route. Records every request it receives."""

    def __init__(
        self,
        required: PaymentRequired | None = None,
        challenge_header: str | None = None,
    ) -> None:
        self.required = required or challenge()
        self.challenge_header = challenge_header
        self.requests: list[httpx.Request] = []
        self.signatures: list[dict[str, Any]] = []
        self.directory_calls = 0
        self.manifest_floor: int | None = (
            None  # None: the gateway has no manifest (404)
        )
        self.paid_handler: (
            Callable[[httpx.Request, dict[str, Any]], httpx.Response] | None
        ) = None

    def directory_response(self) -> httpx.Response:
        self.directory_calls += 1
        if self.manifest_floor is None:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(
            200,
            json={
                "network": "kaspa:testnet-10",
                "min_output_sompi": self.manifest_floor,
                "endpoints": [],
            },
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/directory":
            return self.directory_response()
        from kasgate_client import PaymentResponse, PaymentSignature

        self.requests.append(request)
        raw = request.headers.get("PAYMENT-SIGNATURE")
        header = self.challenge_header or self.required.to_header()
        if raw is None:
            return httpx.Response(
                402,
                headers={"PAYMENT-REQUIRED": header},
                json={
                    "error": "payment_required",
                    "message": "payment required",
                    "accepts": None,
                },
            )
        signature = PaymentSignature.from_header(raw)
        tx = kaspa.Transaction.from_dict(json.loads(signature.rawTx))
        parsed = {
            "signature": signature,
            "tx": tx,
            "payload": bytes.fromhex(tx.payload),
        }
        self.signatures.append(parsed)
        if self.paid_handler is not None:
            return self.paid_handler(request, parsed)
        receipt = PaymentResponse(
            txId=signature.txId,
            network="kaspa:testnet-10",
            usdValueAtSettlement="1.23456789",
        ).to_header()
        return httpx.Response(
            200, headers={"PAYMENT-RESPONSE": receipt}, json={"ok": True}
        )


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


def static_utxos(entries: list[dict[str, Any]]) -> Callable[[str], Any]:
    calls: list[str] = []

    async def provider(address: str) -> list[dict[str, Any]]:
        calls.append(address)
        return [dict(e) for e in entries]

    provider.calls = calls  # type: ignore[attr-defined]
    return provider


@pytest.fixture
def make_client():
    from kasgate_client import KasGateClient

    clients: list[KasGateClient] = []

    def factory(
        gw: FakeGateway, utxos: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> KasGateClient:
        provider = static_utxos(
            utxos if utxos is not None else [make_utxo(0, 500 * 100_000_000)]
        )
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(gw), base_url="http://gateway.test"
        )
        client = KasGateClient(
            kwargs.pop("private_key_hex", PAYER_KEY),
            utxo_provider=provider,
            http_client=http,
            **kwargs,
        )
        client.provider = provider  # type: ignore[attr-defined]
        clients.append(client)
        return client

    yield factory
