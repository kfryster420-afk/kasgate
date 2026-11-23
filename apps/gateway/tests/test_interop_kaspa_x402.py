"""CONTRACT v2.10 interop: the OFFICIAL @kaspa-x402/client 1.0.1 (Node) pays a KasGate 402 and accepts its receipt.

The Node harness (tests/interop/kaspa-x402/harness.mjs) runs the published client's own code for everything
protocol-side: parsing our PAYMENT-REQUIRED envelope (skipping the kaspa-exact-v1 entry), selecting the exact
entry, binding the request URL, computing paymentRequirementsHash and requestHash, assembling and encoding the
PaymentPayload, and validating our SettlementResponse with `applySettlement`. Only the wallet (the funding provider
that signs the transaction and authorization) is the Python test wallet, because the client package ships no
standalone wallet (`@kaspa-x402/adapters` is not published to npm).

Skipped when `node` or the harness's node_modules are missing: `cd tests/interop/kaspa-x402 && npm ci`.
"""

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
from kaspa import PrivateKey

from app.schemas import HEADER_PAYMENT_REQUIRED, HEADER_PAYMENT_RESPONSE, HEADER_PAYMENT_SIGNATURE
from app.x402_v2 import ExactV2Requirements, http_request_hash, payment_requirements_hash
from tests.conftest import MERCHANT_ADDRESS, PRICE_SOMPI, Seed, StubEngine
from tests.exact_v2_support import ExactPaymentSpec, Funding, address_of, build_payment
from tests.test_proxy_exact_v2 import BASE, fetch_envelope

HARNESS_DIR = Path(__file__).parent / "interop" / "kaspa-x402"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(
    NODE is None or not (HARNESS_DIR / "node_modules" / "@kaspa-x402" / "client").is_dir(),
    reason="node or tests/interop/kaspa-x402/node_modules missing (run npm ci there)",
)
PAYER = PrivateKey("03" * 32)


class Harness:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process

    async def send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def receive(self) -> dict[str, Any]:
        assert self.process.stdout is not None
        line = await asyncio.wait_for(self.process.stdout.readline(), timeout=60)
        if not line:
            stderr = await self.process.stderr.read() if self.process.stderr else b""
            raise AssertionError(f"harness exited: {stderr.decode(errors='replace')}")
        decoded: dict[str, Any] = json.loads(line)
        return decoded


async def start_harness() -> Harness:
    assert NODE is not None
    process = await asyncio.create_subprocess_exec(
        NODE,
        "harness.mjs",
        cwd=HARNESS_DIR,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    harness = Harness(process)
    offer_script = ExactV2Requirements.for_merchant(
        network="kaspa:testnet-10", amount_sompi=PRICE_SOMPI, pay_to=MERCHANT_ADDRESS, max_timeout_seconds=120
    ).extra.payToScriptPublicKey
    await harness.send(
        {"network": "kaspa:testnet-10", "payerAddress": address_of(PAYER), "scripts": {MERCHANT_ADDRESS: offer_script}}
    )
    return harness


async def official_client_payment(
    harness: Harness,
    header: str,
    offer: ExactV2Requirements,
    *,
    url: str,
    method: str,
    body: str | None,
    funding_tx: str,
    seen: dict[str, Any],
) -> dict[str, Any]:
    await harness.send({"cmd": "create", "header": header, "url": url, "method": method, "body": body})
    while True:
        message = await harness.receive()
        if "rpc" not in message:
            return message
        assert message["rpc"] == "payExactTransaction"
        request = message["params"]
        seen.update(request)
        built = build_payment(
            ExactPaymentSpec(
                requirement=offer,
                url=url,
                method=method,
                body=body,
                payment_identifier=request["paymentIdentifier"],
                fundings=[Funding(amount=PRICE_SOMPI + 500_000_000, key=PAYER, tx_id=funding_tx)],
                request_hash=request["requestHash"],
                expires_at=request["authorizationExpiresAt"],
            )
        )
        inner = built.payload["payload"]
        await harness.send(
            {
                "result": {
                    "transaction": inner["transaction"],
                    "transactionEncoding": inner["transactionEncoding"],
                    "transactionId": built.tx_id,
                    "paymentOutputIndex": inner["paymentOutputIndex"],
                    "authorization": inner["authorization"],
                    "inputOutpoints": [{"txid": funding_tx, "index": 0}],  # FundingOutpoint
                    "payerAddress": address_of(PAYER),
                }
            }
        )


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [("GET", "/p/weather", None), ("POST", "/p/weather/forecast%20today?city=Hanoi&units=metric", '{"q":"rain"}')],
)
async def test_official_client_pays_and_accepts_the_settlement(
    client: httpx.AsyncClient, seeded: Seed, stub_engine: StubEngine, method: str, path: str, body: str | None
) -> None:
    response = await client.request(method, path, content=body)
    assert response.status_code == 402
    header = response.headers[HEADER_PAYMENT_REQUIRED]
    offer = (await fetch_envelope(client, path)).exact_v2()
    assert offer is not None
    url = f"{BASE}{path}"

    harness = await start_harness()
    try:
        seen: dict[str, Any] = {}
        created = await official_client_payment(
            harness, header, offer, url=url, method=method, body=body, funding_tx="74" * 32, seen=seen
        )
        assert created.get("ok"), created
        # The official client selected OUR exact entry (skipping kaspa-exact-v1) and fingerprinted the request the
        # same way the gateway does.
        assert ExactV2Requirements.model_validate(created["accepted"]) == offer
        assert seen["paymentRequirementsHash"] == payment_requirements_hash(offer)
        assert seen["requestHash"] == http_request_hash(method, url, body, payment_requirements_hash(offer))
        assert seen["resourceUrl"] == url and seen["amount"] == str(PRICE_SOMPI)
        assert seen["requiredFinality"] == "accepted" and seen["profile"] == "standard-native"

        signature = {HEADER_PAYMENT_SIGNATURE: created["signatureHeader"]}
        paid = await client.request(method, path, content=body, headers=signature)
        assert paid.status_code == 200, paid.text
        assert stub_engine.calls == [created["transactionId"]]

        await harness.send({"cmd": "settle", "header": paid.headers[HEADER_PAYMENT_RESPONSE]})
        settled = await harness.receive()
        assert settled.get("ok"), settled
        assert settled["result"]["chargedAmount"] == str(PRICE_SOMPI)
    finally:
        if harness.process.stdin is not None:
            harness.process.stdin.close()
        await harness.process.wait()
