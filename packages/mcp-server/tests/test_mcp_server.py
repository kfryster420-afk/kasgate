"""MCP server tests: real MCP protocol over an in-process transport, fake gateway, no network and no node."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from kasgate_client import KasGateClient
from kasgate_mcp import Config, build_server
from kasgate_mcp.server import validate_path, validate_slug
from mcp import Client
from sdk_helpers import (
    MERCHANT,
    MIN_PRICE,
    PAYER_KEY,
    NONCE,
    PRICE,
    FakeGateway,
    challenge,
    make_utxo,
    static_utxos,
)

KAS = 100_000_000
DIRECTORY = {
    "manifest_version": 1,
    "network": "kaspa:testnet-10",
    "min_output_sompi": 20_000_000,
    "endpoints": [{"slug": "demo-abc123", "price_sompi": PRICE, "price_kas": "15"}],
}


def gateway_with_catalog(fake: FakeGateway):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/directory":
            return httpx.Response(200, json=DIRECTORY)
        if request.url.path.startswith("/v1/quote/"):
            slug = request.url.path.rsplit("/", 1)[1]
            if slug == "missing-slug":
                return httpx.Response(404, json={"error": "endpoint_not_found", "message": "no"})
            return httpx.Response(200, json=json.loads(fake.required.model_dump_json()))
        return fake(request)

    return handler


def make_server(fake: FakeGateway, *, max_price_sompi: int = 20 * KAS, key: str | None = PAYER_KEY, utxos=None):
    http = httpx.AsyncClient(transport=httpx.MockTransport(gateway_with_catalog(fake)))
    buyer = None
    if key:
        buyer = KasGateClient(
            key,
            max_price_sompi=max_price_sompi,
            utxo_provider=static_utxos(utxos if utxos is not None else [make_utxo(0, 500 * KAS)]),
            http_client=http,
        )
    cfg = Config(gateway_url="http://gateway.test", buyer_private_key=key, max_price_sompi=max_price_sompi)
    return build_server(cfg, http_client=http, buyer=buyer), buyer


def structured(result: Any) -> dict[str, Any]:
    if getattr(result, "structured_content", None):
        return result.structured_content
    return json.loads(result.content[0].text)


async def test_server_exposes_exactly_the_three_tools() -> None:
    server, _ = make_server(FakeGateway())
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"list_directory", "quote_endpoint", "call_paid_endpoint"}
    assert set(tools["call_paid_endpoint"].input_schema["properties"]) == {"slug", "method", "path", "body", "max_price_kas"}
    assert tools["call_paid_endpoint"].input_schema["required"] == ["slug"]


async def test_list_directory_and_quote() -> None:
    server, _ = make_server(FakeGateway())
    async with Client(server) as client:
        directory = structured(await client.call_tool("list_directory", {}))
        quote = structured(await client.call_tool("quote_endpoint", {"slug": "demo-abc123"}))
    assert directory["endpoints"][0]["slug"] == "demo-abc123"
    assert quote["total_kas"] == "15" and quote["platform_fee_kas"] == "0.225" and quote["merchant_kas"] == "14.775"
    assert quote["quote"]["payTo"] == MERCHANT


async def test_quote_of_missing_slug_is_a_tool_error() -> None:
    server, _ = make_server(FakeGateway())
    async with Client(server) as client:
        result = await client.call_tool("quote_endpoint", {"slug": "missing-slug"})
    assert result.is_error


@pytest.mark.parametrize("slug", ["../etc/passwd", "UPPER", "a", "has space", "x/y", "a" * 70, "demo?x=1"])
async def test_bad_slug_never_reaches_the_gateway(slug: str) -> None:
    fake = FakeGateway()
    server, _ = make_server(fake)
    async with Client(server) as client:
        quote = await client.call_tool("quote_endpoint", {"slug": slug})
        paid = await client.call_tool("call_paid_endpoint", {"slug": slug})
    assert quote.is_error and paid.is_error
    assert fake.requests == []


async def test_call_paid_endpoint_pays_and_reports_the_receipt() -> None:
    fake = FakeGateway()
    server, _ = make_server(fake)
    async with Client(server) as client:
        result = await client.call_tool(
            "call_paid_endpoint",
            {"slug": "demo-abc123", "method": "POST", "path": "v1/items?limit=3", "body": {"q": "kaspa"}, "max_price_kas": 20},
        )
    assert not result.is_error
    data = structured(result)
    assert data["status_code"] == 200 and data["body"] == {"ok": True}
    assert data["payment"]["tx_id"] == fake.signatures[0]["signature"].txId
    assert data["payment"]["paid_kas"] == "15" and data["payment"]["platform_fee_kas"] == "0.225"
    first, second = fake.requests
    assert str(first.url) == "http://gateway.test/p/demo-abc123/v1/items?limit=3"
    assert first.content == second.content == b'{"q": "kaspa"}'
    assert second.headers["content-type"] == "application/json"


async def test_max_price_kas_cap_refuses_before_signing() -> None:
    fake = FakeGateway()
    server, buyer = make_server(fake)
    async with Client(server) as client:
        result = await client.call_tool("call_paid_endpoint", {"slug": "demo-abc123", "max_price_kas": 14.99})
    assert result.is_error
    assert "PriceCapExceededError" in result.content[0].text
    assert fake.signatures == []
    assert buyer._utxo_provider.calls == []  # type: ignore[union-attr]


async def test_agent_cannot_raise_the_server_side_ceiling() -> None:
    fake = FakeGateway(challenge(PRICE))
    server, _ = make_server(fake, max_price_sompi=10 * KAS)
    async with Client(server) as client:
        result = await client.call_tool("call_paid_endpoint", {"slug": "demo-abc123", "max_price_kas": 1_000_000})
    assert result.is_error and fake.signatures == []


@pytest.mark.parametrize("bad", [0, -1, "abc", "1.000000001"])
async def test_invalid_max_price_kas_is_rejected(bad) -> None:
    fake = FakeGateway()
    server, _ = make_server(fake)
    async with Client(server) as client:
        result = await client.call_tool("call_paid_endpoint", {"slug": "demo-abc123", "max_price_kas": bad})
    assert result.is_error
    assert fake.requests == []


async def test_paid_calls_are_disabled_without_a_key() -> None:
    fake = FakeGateway()
    server, _ = make_server(fake, key=None)
    async with Client(server) as client:
        result = await client.call_tool("call_paid_endpoint", {"slug": "demo-abc123"})
        listing = await client.call_tool("list_directory", {})
    assert result.is_error and "KASGATE_BUYER_PRIVATE_KEY" in result.content[0].text
    assert not listing.is_error


async def test_error_text_never_contains_the_private_key() -> None:
    fake = FakeGateway(challenge(network="kaspa:mainnet"))
    server, _ = make_server(fake)
    async with Client(server) as client:
        result = await client.call_tool("call_paid_endpoint", {"slug": "demo-abc123"})
    assert result.is_error
    assert PAYER_KEY not in result.content[0].text


@pytest.mark.parametrize(
    "path",
    ["../admin", "a/../../b", "%2e%2e/x", "%252e%252e/x", "https://evil.test/x", "//evil.test/x", "a\\b", "a%2fb/..%2f", "x\x00y", "a#frag"],
)
async def test_hostile_paths_are_rejected(path: str) -> None:
    fake = FakeGateway()
    server, _ = make_server(fake)
    async with Client(server) as client:
        result = await client.call_tool("call_paid_endpoint", {"slug": "demo-abc123", "path": path})
    assert result.is_error
    assert fake.requests == []


def test_path_and_slug_helpers() -> None:
    assert validate_path("/a//b/c?x=1&y=2") == "a/b/c?x=1&y=2"
    assert validate_path("") == "" and validate_path(None) == ""
    assert validate_slug("demo-abc123") == "demo-abc123"


async def test_get_with_body_and_unknown_method_are_rejected() -> None:
    fake = FakeGateway()
    server, _ = make_server(fake)
    async with Client(server) as client:
        with_body = await client.call_tool("call_paid_endpoint", {"slug": "demo-abc123", "body": {"a": 1}})
        trace = await client.call_tool("call_paid_endpoint", {"slug": "demo-abc123", "method": "TRACE"})
    assert with_body.is_error and trace.is_error and fake.requests == []


def test_config_from_env() -> None:
    cfg = Config.from_env(
        {
            "KASGATE_BUYER_PRIVATE_KEY": PAYER_KEY,
            "KASGATE_GATEWAY_URL": "https://gw.example/",
            "KASGATE_MAX_PRICE_KAS": "15.5",
        }
    )
    assert cfg.gateway_url == "https://gw.example" and cfg.buyer_private_key == PAYER_KEY
    assert cfg.max_price_sompi == 1_550_000_000
    assert Config.from_env({}).buyer_private_key is None
    with pytest.raises(SystemExit):
        Config.from_env({"KASGATE_MAX_PRICE_KAS": "-3"})


async def test_call_at_the_minimum_price_splits_40_20() -> None:
    fake = FakeGateway(challenge(MIN_PRICE))
    server, _ = make_server(fake)
    async with Client(server) as client:
        result = await client.call_tool("call_paid_endpoint", {"slug": "demo-abc123", "max_price_kas": 1})
    assert not result.is_error
    data = structured(result)
    assert data["payment"]["paid_kas"] == "0.6" and data["payment"]["platform_fee_kas"] == "0.2"
    tx = fake.signatures[0]["tx"]
    assert [o.value for o in tx.outputs[:2]] == [40_000_000, 20_000_000]
    assert fake.signatures[0]["payload"] == b"kasgate:v1:" + NONCE.encode()


@pytest.mark.parametrize("price", [59_999_999, 40_000_000, 39_999_999, 20_000_000])
async def test_sub_floor_challenge_is_refused_before_signing(price: int) -> None:
    fake = FakeGateway(challenge(price))
    server, buyer = make_server(fake)
    async with Client(server) as client:
        result = await client.call_tool("call_paid_endpoint", {"slug": "demo-abc123"})
    assert result.is_error and "FeeSplitMismatchError" in result.content[0].text
    assert fake.signatures == []
    assert buyer._utxo_provider.calls == []  # type: ignore[union-attr]


async def test_expired_challenge_is_refused_before_signing() -> None:
    fake = FakeGateway(challenge(expires_in=-10))
    server, buyer = make_server(fake)
    async with Client(server) as client:
        result = await client.call_tool("call_paid_endpoint", {"slug": "demo-abc123"})
    assert result.is_error and "ChallengeExpiredError" in result.content[0].text
    assert fake.signatures == [] and buyer._utxo_provider.calls == []  # type: ignore[union-attr]


# ---------------------------------------------------------------- KASGATE_BUYER_PRIVATE_KEY_FILE / KASGATE_REQUEST_TIMEOUT


def key_file(tmp_path, content: str = PAYER_KEY + "\n", mode: int = 0o600):
    path = tmp_path / "buyer.key"
    path.write_text(content)
    path.chmod(mode)
    return path


def test_key_file_is_read_and_whitespace_stripped(tmp_path) -> None:
    for content in (PAYER_KEY, PAYER_KEY + "\n", f"  \t{PAYER_KEY}\r\n\n", f"\n{PAYER_KEY.upper()}  "):
        path = key_file(tmp_path, content)
        cfg = Config.from_env({"KASGATE_BUYER_PRIVATE_KEY_FILE": str(path)})
        assert cfg.buyer_private_key == content.strip()


def test_key_file_wins_over_the_inline_variable(tmp_path, caplog) -> None:
    other = "ab" * 32
    path = key_file(tmp_path)
    with caplog.at_level("WARNING", logger="kasgate_mcp"):
        cfg = Config.from_env({"KASGATE_BUYER_PRIVATE_KEY_FILE": str(path), "KASGATE_BUYER_PRIVATE_KEY": other})
    assert cfg.buyer_private_key == PAYER_KEY
    assert "ignoring KASGATE_BUYER_PRIVATE_KEY" in caplog.text
    assert PAYER_KEY not in caplog.text and other not in caplog.text


def test_blank_file_variable_falls_back_to_the_inline_key() -> None:
    cfg = Config.from_env({"KASGATE_BUYER_PRIVATE_KEY_FILE": "  ", "KASGATE_BUYER_PRIVATE_KEY": PAYER_KEY})
    assert cfg.buyer_private_key == PAYER_KEY


def test_home_directory_is_expanded(tmp_path, monkeypatch) -> None:
    key_file(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert Config.from_env({"KASGATE_BUYER_PRIVATE_KEY_FILE": "~/buyer.key"}).buyer_private_key == PAYER_KEY


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o604, 0o660, 0o666, 0o700 | 0o010, 0o777])
def test_key_file_readable_by_group_or_others_is_refused(tmp_path, mode: int) -> None:
    path = key_file(tmp_path, mode=mode)
    with pytest.raises(SystemExit) as caught:
        Config.from_env({"KASGATE_BUYER_PRIVATE_KEY_FILE": str(path)})
    message = str(caught.value)
    assert "chmod 600" in message and f"{mode:04o}" in message
    assert PAYER_KEY not in message


@pytest.mark.parametrize("mode", [0o600, 0o400, 0o500])
def test_owner_only_key_files_are_accepted(tmp_path, mode: int) -> None:
    path = key_file(tmp_path, mode=mode)
    assert Config.from_env({"KASGATE_BUYER_PRIVATE_KEY_FILE": str(path)}).buyer_private_key == PAYER_KEY


@pytest.mark.parametrize("flag", ["1", "true", "YES", "on"])
def test_insecure_permissions_can_be_overridden_with_a_warning(tmp_path, caplog, flag: str) -> None:
    path = key_file(tmp_path, mode=0o644)
    with caplog.at_level("WARNING", logger="kasgate_mcp"):
        cfg = Config.from_env(
            {"KASGATE_BUYER_PRIVATE_KEY_FILE": str(path), "KASGATE_ALLOW_INSECURE_KEY_FILE": flag}
        )
    assert cfg.buyer_private_key == PAYER_KEY
    assert "0644" in caplog.text and str(path) in caplog.text
    assert PAYER_KEY not in caplog.text


@pytest.mark.parametrize("flag", ["", "0", "false", "no", "maybe"])
def test_override_flag_must_be_truthy(tmp_path, flag: str) -> None:
    path = key_file(tmp_path, mode=0o644)
    with pytest.raises(SystemExit):
        Config.from_env({"KASGATE_BUYER_PRIVATE_KEY_FILE": str(path), "KASGATE_ALLOW_INSECURE_KEY_FILE": flag})


def test_a_missing_directory_or_unreadable_key_file_exits_without_leaking(tmp_path) -> None:
    with pytest.raises(SystemExit, match="cannot be opened"):
        Config.from_env({"KASGATE_BUYER_PRIVATE_KEY_FILE": str(tmp_path / "absent")})
    with pytest.raises(SystemExit, match="not a regular file"):
        Config.from_env({"KASGATE_BUYER_PRIVATE_KEY_FILE": str(tmp_path)})


@pytest.mark.parametrize(
    "content",
    ["", "\n", "not a key", PAYER_KEY[:-1], PAYER_KEY + "00", "zz" * 32, PAYER_KEY + "\n" + PAYER_KEY, "mnemonic words " * 10],
)
def test_malformed_key_file_exits_and_the_message_never_contains_the_contents(tmp_path, content: str) -> None:
    path = key_file(tmp_path, content)
    with pytest.raises(SystemExit) as caught:
        Config.from_env({"KASGATE_BUYER_PRIVATE_KEY_FILE": str(path)})
    message = str(caught.value)
    assert "64-character hex" in message
    for fragment in (content.strip()[:12], PAYER_KEY[:12]):
        assert not fragment or fragment not in message.replace(str(path), "")


def test_binary_key_file_exits_cleanly(tmp_path) -> None:
    path = tmp_path / "buyer.key"
    path.write_bytes(b"\xff\xfe\x00\x80" * 40)
    path.chmod(0o600)
    with pytest.raises(SystemExit):
        Config.from_env({"KASGATE_BUYER_PRIVATE_KEY_FILE": str(path)})


def test_a_huge_key_file_is_not_read_in_full(tmp_path) -> None:
    path = key_file(tmp_path, "a" * 5_000_000)
    with pytest.raises(SystemExit, match="64-character hex"):
        Config.from_env({"KASGATE_BUYER_PRIVATE_KEY_FILE": str(path)})


def test_config_repr_never_contains_the_key(tmp_path) -> None:
    cfg = Config.from_env({"KASGATE_BUYER_PRIVATE_KEY_FILE": str(key_file(tmp_path))})
    assert PAYER_KEY not in repr(cfg) and PAYER_KEY not in str(cfg)
    assert PAYER_KEY not in repr(Config.from_env({"KASGATE_BUYER_PRIVATE_KEY": PAYER_KEY}))


async def test_a_key_loaded_from_a_file_pays(tmp_path) -> None:
    cfg = Config.from_env({"KASGATE_BUYER_PRIVATE_KEY_FILE": str(key_file(tmp_path))})
    fake = FakeGateway(challenge())
    server, _ = make_server(fake, key=cfg.buyer_private_key)
    async with Client(server) as client:
        result = await client.call_tool("call_paid_endpoint", {"slug": "demo-abc123"})
    assert not result.is_error, result.content[0].text
    assert PAYER_KEY not in result.content[0].text


def test_request_timeout_default_and_override() -> None:
    assert Config.from_env({}).timeout_seconds == 60.0
    assert Config.from_env({"KASGATE_REQUEST_TIMEOUT": "7.5"}).timeout_seconds == 7.5


@pytest.mark.parametrize("value", ["abc", "", "0", "-1", "inf", "nan", "1e999"])
def test_invalid_request_timeout_exits(value: str) -> None:
    with pytest.raises(SystemExit, match="KASGATE_REQUEST_TIMEOUT"):
        Config.from_env({"KASGATE_REQUEST_TIMEOUT": value})


def test_a_fifo_key_file_is_refused_without_blocking(tmp_path) -> None:
    import os

    fifo = tmp_path / "buyer.fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(SystemExit, match="not a regular file"):
        Config.from_env({"KASGATE_BUYER_PRIVATE_KEY_FILE": str(fifo)})
