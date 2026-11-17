"""KasGate MCP server (stdio): discover, quote and pay for KasGate endpoints from an AI agent."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import unquote

import httpx
from kasgate_client import (
    DEFAULT_MAX_PRICE_SOMPI,
    KasGateClient,
    KasGateError,
    PaymentReceipt,
    kas_to_sompi,
    receipt_of,
    sompi_to_kas,
)

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])$")
ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
MAX_RESULT_CHARS = 100_000
_BAD_PATH_CHARS = re.compile(r"[\x00-\x1f\x7f\\#]")


@dataclass(frozen=True)
class Config:
    gateway_url: str = "http://localhost:8000"
    buyer_private_key: str | None = None
    network: str = "kaspa:testnet-10"
    rpc_url: str | None = None
    max_price_sompi: int = DEFAULT_MAX_PRICE_SOMPI
    timeout_seconds: float = 60.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = dict(os.environ if env is None else env)
        try:
            max_price = kas_to_sompi(env.get("KASGATE_MAX_PRICE_KAS", "20"))
        except ValueError as exc:
            raise SystemExit(f"KASGATE_MAX_PRICE_KAS is invalid: {exc}") from exc
        return cls(
            gateway_url=env.get("KASGATE_GATEWAY_URL", "http://localhost:8000").rstrip("/"),
            buyer_private_key=(env.get("KASGATE_BUYER_PRIVATE_KEY") or "").strip() or None,
            network=env.get("KASGATE_NETWORK", "kaspa:testnet-10"),
            rpc_url=(env.get("KASGATE_RPC_URL") or "").strip() or None,
            max_price_sompi=max_price,
            timeout_seconds=float(env.get("KASGATE_REQUEST_TIMEOUT", "60")),
        )


def validate_slug(slug: str) -> str:
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise ToolError("slug must be 3-64 characters of lowercase letters, digits and hyphens")
    return slug


def validate_path(path: str | None) -> str:
    """Keep the request inside /p/{slug}: no traversal, no absolute URLs, no control characters."""
    path = (path or "").strip()
    if "://" in path or path.startswith("//") or _BAD_PATH_CHARS.search(path):
        raise ToolError("path must be a relative URL path (optionally with a ?query), without scheme or host")
    raw_path, _, query = path.partition("?")
    segments = [segment for segment in raw_path.split("/") if segment != ""]
    for segment in segments:
        decoded = unquote(unquote(segment))
        if decoded in {".", ".."} or "/" in decoded or "\\" in decoded or "\x00" in decoded:
            raise ToolError("path traversal segments are not allowed")
    cleaned = "/".join(segments)
    return f"{cleaned}?{query}" if query else cleaned


def _truncate(text: str) -> str:
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + "\n...[truncated]"


def _describe_response(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    body: Any
    if "json" in content_type:
        try:
            body = response.json()
        except ValueError:
            body = _truncate(response.text)
    else:
        body = _truncate(response.text)
    return {"status_code": response.status_code, "content_type": content_type, "body": body}


def _describe_receipt(receipt: PaymentReceipt) -> dict[str, Any]:
    return {
        "tx_id": receipt.tx_id,
        "network": receipt.network,
        "paid_kas": sompi_to_kas(receipt.amount_sompi),
        "merchant_kas": sompi_to_kas(receipt.merchant_sompi),
        "platform_fee_kas": sompi_to_kas(receipt.fee_sompi),
        "network_fee_kas": sompi_to_kas(receipt.network_fee_sompi),
        "usd_value_at_settlement": str(receipt.usd_value_at_settlement),
        "payer_address": receipt.payer_address,
        "pay_to": receipt.pay_to,
    }


def build_server(
    config: Config | None = None,
    *,
    http_client: httpx.AsyncClient | None = None,
    buyer: KasGateClient | None = None,
) -> Any:
    """Create the MCP server. `http_client` and `buyer` can be injected for tests."""
    cfg = config or Config.from_env()
    server = _Server(
        "kasgate",
        instructions=(
            "Browse paid APIs with list_directory, check a price with quote_endpoint, then call_paid_endpoint. "
            "Every paid call is capped: pass max_price_kas to set a tighter limit than the configured ceiling."
        ),
    )
    state: dict[str, Any] = {"http": http_client, "buyer": buyer}

    def public_http() -> httpx.AsyncClient:
        if state["http"] is None:
            state["http"] = httpx.AsyncClient(timeout=cfg.timeout_seconds, follow_redirects=False)
        return state["http"]

    def paying_client() -> KasGateClient:
        if state["buyer"] is None:
            if not cfg.buyer_private_key:
                raise ToolError("KASGATE_BUYER_PRIVATE_KEY is not configured on this MCP server; paid calls are disabled")
            try:
                state["buyer"] = KasGateClient(
                    cfg.buyer_private_key,
                    network=cfg.network,
                    rpc_url=cfg.rpc_url,
                    max_price_sompi=cfg.max_price_sompi,
                    timeout=cfg.timeout_seconds,
                    follow_redirects=False,
                )
            except KasGateError as exc:
                raise ToolError(f"cannot start the payment client: {exc}") from exc
        return state["buyer"]

    async def get_json(path: str) -> Any:
        try:
            response = await public_http().get(f"{cfg.gateway_url}{path}")
        except httpx.HTTPError as exc:
            raise ToolError(f"gateway request failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise ToolError(f"gateway answered {response.status_code}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError as exc:
            raise ToolError("gateway returned a non-JSON body") from exc

    @server.tool()
    async def list_directory() -> dict[str, Any]:
        """List every public paid endpoint (slug, description, price in KAS, allowed methods, proxy URL)."""
        return await get_json("/v1/directory")

    @server.tool()
    async def quote_endpoint(slug: str) -> dict[str, Any]:
        """Return the price quote for an endpoint (total, merchant share, platform fee) without paying anything."""
        quote = await get_json(f"/v1/quote/{validate_slug(slug)}")
        try:
            total = int(quote["amount"])
            fee = int(quote["platformFee"]["feeAmount"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolError("gateway returned a malformed quote") from exc
        return {
            "quote": quote,
            "total_kas": sompi_to_kas(total),
            "merchant_kas": sompi_to_kas(total - fee),
            "platform_fee_kas": sompi_to_kas(fee),
        }

    @server.tool()
    async def call_paid_endpoint(
        slug: str,
        method: str = "GET",
        path: str = "",
        body: Any = None,
        max_price_kas: float | str | None = None,
    ) -> dict[str, Any]:
        """Call a paid endpoint and pay its HTTP 402 challenge in KAS.

        Refuses before signing anything if the quoted total exceeds max_price_kas (or the server's ceiling,
        whichever is lower). `path` is appended to the endpoint (e.g. "v1/items?limit=3"); `body` is sent as JSON.
        """
        slug = validate_slug(slug)
        method = (method or "GET").upper()
        if method not in ALLOWED_METHODS:
            raise ToolError(f"method must be one of {sorted(ALLOWED_METHODS)}")
        suffix = validate_path(path)
        url = f"{cfg.gateway_url}/p/{slug}" + (f"/{suffix}" if suffix else "")
        if max_price_kas is not None:
            if isinstance(max_price_kas, bool):
                raise ToolError("max_price_kas must be a positive number")
            try:
                kas_to_sompi(Decimal(str(max_price_kas)))
            except (ValueError, ArithmeticError) as exc:
                raise ToolError(f"max_price_kas is invalid: {exc}") from exc
        kwargs: dict[str, Any] = {}
        if body is not None:
            if method == "GET":
                raise ToolError("a GET request cannot carry a body")
            kwargs["content"] = body if isinstance(body, str) else json.dumps(body)
            if not isinstance(body, str):
                kwargs["headers"] = {"Content-Type": "application/json"}
        client = paying_client()
        try:
            response = await client.request(method, url, max_price_kas=max_price_kas, **kwargs)
        except KasGateError as exc:
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ToolError(f"gateway request failed: {type(exc).__name__}") from exc
        result = _describe_response(response)
        receipt = receipt_of(response)
        result["payment"] = _describe_receipt(receipt) if receipt else None
        return result

    server.kasgate_config = cfg  # type: ignore[attr-defined]
    return server


def main() -> None:
    build_server(Config.from_env()).run("stdio")


if __name__ == "__main__":
    main()
