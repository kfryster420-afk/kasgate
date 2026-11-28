"""Testnet faucet: POST /v1/faucet/claim with the send mocked, Redis caps (address, IP, daily), rollback, guards."""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import fakeredis
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from kaspa import PrivateKey
from pydantic import SecretStr, ValidationError

import app.faucet as faucet_module
from app.config import Settings, get_settings
from app.faucet import FaucetSendError, KaspaFaucetSender, get_faucet_sender
from app.faucet import router as faucet_router
from tests.conftest import MERCHANT_ADDRESS, OTHER_ADDRESS, PAYER_ADDRESS, TREASURY

AMOUNT = 5_000_000_000
FAUCET_KEY = "07" * 32
MAINNET_ADDRESS = PrivateKey("01" * 32).to_address("mainnet").to_string()
TX_ID = "ab" * 32


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, int]] = []
        self.error: Exception | None = None
        self.delay = 0.0

    async def send(self, address: str, amount_sompi: int) -> str:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        self.sent.append((address, amount_sompi))
        return f"{len(self.sent):064x}"


@pytest.fixture
def sender() -> FakeSender:
    return FakeSender()


@pytest.fixture
def faucet_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = get_settings().model_copy(
        update={
            "faucet_private_key": SecretStr(FAUCET_KEY),
            "faucet_amount_sompi": AMOUNT,
            "faucet_daily_cap_sompi": 3 * AMOUNT,
        }
    )
    monkeypatch.setattr(faucet_module, "get_settings", lambda: settings)
    return settings


@pytest_asyncio.fixture
async def client(sender: FakeSender, faucet_settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.state.redis = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer(), decode_responses=True)
    app.include_router(faucet_router)
    app.dependency_overrides[get_faucet_sender] = lambda: sender
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://faucet.test") as http:
        yield http
    await app.state.redis.aclose()


async def claim(client: httpx.AsyncClient, address: str = PAYER_ADDRESS) -> httpx.Response:
    return await client.post("/v1/faucet/claim", json={"address": address})


async def test_claim_happy_path(client: httpx.AsyncClient, sender: FakeSender) -> None:
    response = await claim(client)
    assert response.status_code == 200
    assert response.json() == {"tx_id": f"{1:064x}", "amount_sompi": AMOUNT, "address": PAYER_ADDRESS}
    assert sender.sent == [(PAYER_ADDRESS, AMOUNT)]


async def test_address_claims_once_per_24h(client: httpx.AsyncClient, sender: FakeSender) -> None:
    assert (await claim(client)).status_code == 200
    again = await claim(client)
    assert again.status_code == 429
    assert again.json()["error"] == "rate_limited"
    assert 0 < int(again.headers["Retry-After"]) <= 86_400
    assert len(sender.sent) == 1


async def test_ip_claims_once_per_24h(client: httpx.AsyncClient, sender: FakeSender) -> None:
    assert (await claim(client, PAYER_ADDRESS)).status_code == 200
    other = await claim(client, OTHER_ADDRESS)
    assert other.status_code == 429
    assert other.json()["error"] == "rate_limited"
    assert "Retry-After" in other.headers
    assert len(sender.sent) == 1


async def test_rejected_ip_does_not_burn_the_address(client: httpx.AsyncClient, sender: FakeSender) -> None:
    await claim(client, PAYER_ADDRESS)
    assert (await claim(client, OTHER_ADDRESS)).status_code == 429
    redis = client._transport.app.state.redis  # type: ignore[attr-defined]
    assert await redis.exists(f"faucet:addr:{OTHER_ADDRESS}") == 0


async def test_daily_cap_exhausted(client: httpx.AsyncClient, sender: FakeSender, faucet_settings: Settings) -> None:
    faucet_settings.faucet_daily_cap_sompi = AMOUNT
    redis = client._transport.app.state.redis  # type: ignore[attr-defined]
    assert (await claim(client, PAYER_ADDRESS)).status_code == 200
    await redis.delete("faucet:ip:203.0.113.9")
    response = await claim(client, OTHER_ADDRESS)
    assert response.status_code == 503
    assert response.json()["error"] == "faucet_unavailable"
    assert "faucet_exhausted" in response.json()["message"]
    assert int(response.headers["Retry-After"]) > 0
    assert len(sender.sent) == 1
    day_keys = [k async for k in redis.scan_iter("faucet:day:*")]
    assert int(await redis.get(day_keys[0])) == AMOUNT  # the refused claim's reservation was rolled back
    assert await redis.exists(f"faucet:addr:{OTHER_ADDRESS}") == 0
    assert await redis.exists("faucet:ip:203.0.113.9") == 0


async def test_daily_cap_holds_under_concurrency(faucet_settings: Settings, sender: FakeSender) -> None:
    faucet_settings.faucet_daily_cap_sompi = 2 * AMOUNT
    sender.delay = 0.05
    app = FastAPI()
    app.state.redis = fakeredis.FakeAsyncRedis(server=fakeredis.FakeServer(), decode_responses=True)
    app.include_router(faucet_router)
    app.dependency_overrides[get_faucet_sender] = lambda: sender
    clients = [
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=(f"198.51.100.{i}", 1)), base_url="http://faucet.test"
        )
        for i in range(1, 7)
    ]
    keys = [PrivateKey(f"{i:02x}" * 32).to_address("testnet").to_string() for i in range(10, 16)]
    results = await asyncio.gather(*(claim(c, a) for c, a in zip(clients, keys, strict=True)))
    for c in clients:
        await c.aclose()
    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 200, 503, 503, 503, 503]
    assert len(sender.sent) == 2


async def test_send_failure_rolls_everything_back(client: httpx.AsyncClient, sender: FakeSender) -> None:
    sender.error = FaucetSendError("node down")
    failed = await claim(client)
    assert failed.status_code == 503
    assert failed.json()["error"] == "faucet_unavailable"
    redis = client._transport.app.state.redis  # type: ignore[attr-defined]
    day_keys = [k async for k in redis.scan_iter("faucet:day:*")]
    assert [int(await redis.get(k)) for k in day_keys] == [0]
    assert await redis.exists(f"faucet:addr:{PAYER_ADDRESS}") == 0
    assert await redis.exists("faucet:ip:203.0.113.9") == 0
    sender.error = None
    assert (await claim(client)).status_code == 200  # the same address and IP can retry at once


@pytest.mark.parametrize("address", [MAINNET_ADDRESS, "kaspatest:notanaddress", "", MERCHANT_ADDRESS[:-2] + "qq"])
async def test_invalid_and_mainnet_addresses_rejected(
    client: httpx.AsyncClient, sender: FakeSender, address: str
) -> None:
    response = await claim(client, address)
    assert response.status_code == 422
    assert sender.sent == []


async def test_extra_fields_rejected(client: httpx.AsyncClient, sender: FakeSender) -> None:
    response = await client.post("/v1/faucet/claim", json={"address": PAYER_ADDRESS, "amount": 1})
    assert response.status_code == 422
    assert sender.sent == []


async def test_disabled_without_key(client: httpx.AsyncClient, sender: FakeSender, faucet_settings: Settings) -> None:
    faucet_settings.faucet_private_key = None
    response = await claim(client)
    assert response.status_code == 503
    assert response.json()["error"] == "faucet_unavailable"
    assert sender.sent == []


def test_refuses_to_start_off_testnet(monkeypatch: pytest.MonkeyPatch) -> None:
    mainnet_treasury = PrivateKey("01" * 32).to_address("mainnet").to_string()
    base: dict[str, Any] = {
        "kasgate_treasury_address": mainnet_treasury,
        "encryption_key": "a" * 44,
        "database_url": "postgresql+asyncpg://x/x",
        "redis_url": "redis://localhost/0",
        "kaspa_network": "kaspa:mainnet",
        "public_base_url": "https://gw.example",
    }
    with pytest.raises(ValidationError, match="testnet only"):
        Settings(_env_file=None, faucet_private_key=FAUCET_KEY, **base)  # type: ignore[call-arg,arg-type]
    assert Settings(_env_file=None, **base).faucet_enabled is False  # type: ignore[call-arg,arg-type]
    with pytest.raises(ValidationError, match="64 hex"):
        Settings(_env_file=None, faucet_private_key="not-hex", **{**base, "kaspa_network": "kaspa:testnet-10"})  # type: ignore[call-arg,arg-type]


class FakeNode:
    def __init__(self, address: str, amounts: list[int]) -> None:
        from kaspa import pay_to_address_script

        script = pay_to_address_script(PrivateKey(FAUCET_KEY).to_address("testnet")).script
        self.entries = [
            {
                "address": address,
                "outpoint": {"transactionId": f"{i + 1:064x}", "index": 0},
                "utxoEntry": {
                    "amount": amount,
                    "scriptPublicKey": {"version": 0, "script": script},
                    "blockDaaScore": 1,
                    "isCoinbase": False,
                },
            }
            for i, amount in enumerate(amounts)
        ]
        self.broadcasts: list[tuple[Any, str]] = []

    async def utxos_for_address(self, address: str) -> list[dict[str, Any]]:
        return self.entries

    async def broadcast(self, tx: Any, tx_id: str) -> None:
        self.broadcasts.append((tx, tx_id))


async def test_real_sender_builds_signs_and_broadcasts(
    monkeypatch: pytest.MonkeyPatch, faucet_settings: Settings
) -> None:
    faucet_address = PrivateKey(FAUCET_KEY).to_address("testnet").to_string()
    node = FakeNode(faucet_address, [100_000_000_000])
    monkeypatch.setattr(faucet_module, "kaspa_engine", node)
    sender = KaspaFaucetSender(faucet_settings)
    tx_id = await sender.send(PAYER_ADDRESS, AMOUNT)
    assert node.broadcasts[0][1] == tx_id
    tx = node.broadcasts[0][0].to_dict()
    assert tx["outputs"][0]["value"] == AMOUNT
    assert len(tx["outputs"]) == 2  # payment + change back to the faucet wallet
    assert tx["inputs"][0]["signatureScript"] != ""
    # a second claim before the node indexes the first spends the pending change, not the same outpoint
    second = await sender.send(OTHER_ADDRESS, AMOUNT)
    assert second != tx_id
    spent_second = node.broadcasts[1][0].to_dict()["inputs"][0]["previousOutpoint"]
    assert spent_second["transactionId"] == tx_id


async def test_real_sender_low_balance_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, faucet_settings: Settings
) -> None:
    node = FakeNode(PrivateKey(FAUCET_KEY).to_address("testnet").to_string(), [1_000_000_000])
    monkeypatch.setattr(faucet_module, "kaspa_engine", node)
    with pytest.raises(FaucetSendError, match="too low"):
        await KaspaFaucetSender(faucet_settings).send(TREASURY, AMOUNT)
    assert node.broadcasts == []
