"""KasGate MCP server (stdio): discover, quote and pay for KasGate endpoints from an AI agent."""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import quote, unquote

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

from kasgate_mcp.budget import BudgetError, SpendBudget

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]

logger = logging.getLogger("kasgate_mcp")

KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
TRUTHY = frozenset({"1", "true", "yes", "on"})


def read_key_file(path: str, *, allow_insecure: bool = False) -> str:
    """Read a hex private key from `path` (strip whitespace). Exits with a message that never contains the contents.

    A file that is readable by group or others (mode bits `077`) is refused unless `allow_insecure`, in which case a
    warning is logged to stderr. The permissions are checked on the opened descriptor, not on the path.
    """
    expanded = os.path.expanduser(path)
    try:
        fd = os.open(expanded, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))  # never block on a FIFO
    except OSError as exc:
        raise SystemExit(f"KASGATE_BUYER_PRIVATE_KEY_FILE {expanded!r} cannot be opened: {exc.strerror}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit(f"KASGATE_BUYER_PRIVATE_KEY_FILE {expanded!r} is not a regular file")
        if os.name != "nt" and info.st_mode & 0o077:
            mode = stat.S_IMODE(info.st_mode)
            if not allow_insecure:
                raise SystemExit(
                    f"KASGATE_BUYER_PRIVATE_KEY_FILE {expanded!r} has mode {mode:04o}: it must not be readable by "
                    f"group or others (chmod 600), or set KASGATE_ALLOW_INSECURE_KEY_FILE=1 to override"
                )
            logger.warning("buyer key file %s has mode %04o and is readable by others (override in effect)", expanded, mode)
        try:
            text = os.read(fd, 4096).decode("utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise SystemExit(f"KASGATE_BUYER_PRIVATE_KEY_FILE {expanded!r} cannot be read as text") from exc
    finally:
        os.close(fd)
    if not KEY_RE.fullmatch(text):
        raise SystemExit(f"KASGATE_BUYER_PRIVATE_KEY_FILE {expanded!r} must contain one 64-character hex private key")
    return text


SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])$")
ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
MAX_RESULT_CHARS = 100_000
_BAD_PATH_CHARS = re.compile(r"[\x00-\x1f\x7f\\#]")


@dataclass(frozen=True)
class Config:
    gateway_url: str = "http://localhost:8000"
    buyer_private_key: str | None = field(default=None, repr=False)  # never rendered
    network: str = "kaspa:testnet-10"
    rpc_url: str | None = None
    max_price_sompi: int = DEFAULT_MAX_PRICE_SOMPI
    session_budget_sompi: int = DEFAULT_MAX_PRICE_SOMPI
    timeout_seconds: float = 60.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = dict(os.environ if env is None else env)
        try:
            max_price = kas_to_sompi(env.get("KASGATE_MAX_PRICE_KAS", "20"))
        except ValueError as exc:
            raise SystemExit(f"KASGATE_MAX_PRICE_KAS is invalid: {exc}") from exc
        try:
            session_budget = kas_to_sompi(env.get("KASGATE_SESSION_BUDGET_KAS", "20"))
        except ValueError as exc:
            raise SystemExit(f"KASGATE_SESSION_BUDGET_KAS is invalid: {exc}") from exc
        key_file = (env.get("KASGATE_BUYER_PRIVATE_KEY_FILE") or "").strip()
        inline_key = (env.get("KASGATE_BUYER_PRIVATE_KEY") or "").strip() or None
        if key_file:
            if inline_key:
                logger.warning("KASGATE_BUYER_PRIVATE_KEY_FILE is set: ignoring KASGATE_BUYER_PRIVATE_KEY")
            buyer_key: str | None = read_key_file(
                key_file, allow_insecure=env.get("KASGATE_ALLOW_INSECURE_KEY_FILE", "").strip().lower() in TRUTHY
            )
        else:
            buyer_key = inline_key
        try:
            timeout = float(env.get("KASGATE_REQUEST_TIMEOUT", "60"))
        except ValueError as exc:
            raise SystemExit("KASGATE_REQUEST_TIMEOUT must be a number of seconds") from exc
        if not 0 < timeout < float("inf"):
            raise SystemExit("KASGATE_REQUEST_TIMEOUT must be a positive number of seconds")
        return cls(
            gateway_url=env.get("KASGATE_GATEWAY_URL", "http://localhost:8000").rstrip("/"),
            buyer_private_key=buyer_key,
            network=env.get("KASGATE_NETWORK", "kaspa:testnet-10"),
            rpc_url=(env.get("KASGATE_RPC_URL") or "").strip() or None,
            max_price_sompi=max_price,
            session_budget_sompi=session_budget,
            timeout_seconds=timeout,
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
            "Browse paid APIs with search_catalog or list_directory, inspect_endpoint for one slug, and "
            "quote_endpoint for a live price. Pay with call_with_budget (session spend cap + per-call max) "
            "or call_paid_endpoint. Check remaining funds with get_budget. A tighter max_price_kas can only "
            "lower the configured ceiling, never raise it. Keys are never logged."
        ),
    )
    state: dict[str, Any] = {"http": http_client, "buyer": buyer}
    budget = SpendBudget(per_call_max_sompi=cfg.max_price_sompi, session_cap_sompi=cfg.session_budget_sompi)

    def public_http() -> httpx.AsyncClient:
        if state["http"] is None:
            state["http"] = httpx.AsyncClient(timeout=cfg.timeout_seconds, follow_redirects=False)
        return state["http"]

    def paying_client() -> KasGateClient:
        if state["buyer"] is None:
            if not cfg.buyer_private_key:
                raise ToolError(
                    "no buyer key is configured on this MCP server (KASGATE_BUYER_PRIVATE_KEY_FILE or "
                    "KASGATE_BUYER_PRIVATE_KEY); paid calls are disabled"
                )
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

    def _parse_optional_max_price_kas(max_price_kas: float | str | None) -> int | None:
        if max_price_kas is None:
            return None
        if isinstance(max_price_kas, bool):
            raise ToolError("max_price_kas must be a positive number")
        try:
            return kas_to_sompi(Decimal(str(max_price_kas)))
        except (ValueError, ArithmeticError) as exc:
            raise ToolError(f"max_price_kas is invalid: {exc}") from exc

    def _request_kwargs(method: str, body: Any) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if body is not None:
            if method == "GET":
                raise ToolError("a GET request cannot carry a body")
            kwargs["content"] = body if isinstance(body, str) else json.dumps(body)
            if not isinstance(body, str):
                kwargs["headers"] = {"Content-Type": "application/json"}
        return kwargs

    def _paid_url(slug: str, path: str) -> tuple[str, str, str]:
        slug = validate_slug(slug)
        suffix = validate_path(path)
        url = f"{cfg.gateway_url}/p/{slug}" + (f"/{suffix}" if suffix else "")
        return slug, suffix, url

    async def _execute_paid_call(
        slug: str,
        method: str,
        path: str,
        body: Any,
        max_price_kas: float | str | None,
    ) -> dict[str, Any]:
        slug, _, url = _paid_url(slug, path)
        method = (method or "GET").upper()
        if method not in ALLOWED_METHODS:
            raise ToolError(f"method must be one of {sorted(ALLOWED_METHODS)}")
        _parse_optional_max_price_kas(max_price_kas)
        kwargs = _request_kwargs(method, body)
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

    @server.tool()
    async def list_directory() -> dict[str, Any]:
        """List every public paid endpoint (slug, description, price in KAS, allowed methods, proxy URL)."""
        return await get_json("/v1/directory")

    @server.tool()
    async def search_catalog(query: str = "", limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        """Search the public catalog of listed endpoints (GET /v1/catalog).

        `query` is matched as a case-insensitive substring of slug, name, or description. `limit` is 1-200
        (default 50). Pass `cursor` from a previous `next_cursor` to fetch the next page.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ToolError("limit must be an integer from 1 to 200")
        if cursor is not None and (not isinstance(cursor, str) or not cursor.strip()):
            raise ToolError("cursor must be a non-empty string")
        if not isinstance(query, str) or len(query) > 120:
            raise ToolError("query must be a string of at most 120 characters")
        params: list[str] = [f"limit={limit}"]
        if query.strip():
            params.append(f"q={quote(query.strip(), safe='')}")
        if cursor:
            params.append(f"cursor={quote(cursor, safe='')}")
        return await get_json("/v1/catalog?" + "&".join(params))

    @server.tool()
    async def inspect_endpoint(slug: str) -> dict[str, Any]:
        """Return the public catalog record for one listed endpoint (price, methods, paid URL, input schema)."""
        return await get_json(f"/v1/catalog/{validate_slug(slug)}")

    @server.tool()
    async def get_budget() -> dict[str, Any]:
        """Show the session spend cap, per-call maximum, spent, reserved, and remaining budget, in sompi."""
        return budget.snapshot()

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
        This tool does not draw down the session spend cap; use call_with_budget for session-capped spending.
        """
        return await _execute_paid_call(slug, method, path, body, max_price_kas)

    @server.tool()
    async def call_with_budget(
        slug: str,
        method: str = "GET",
        path: str = "",
        body: Any = None,
        max_price_kas: float | str | None = None,
    ) -> dict[str, Any]:
        """Call a paid endpoint under the session spend cap and a per-call maximum, both in sompi.

        The catalog (quoted list price) is reserved atomically before any key use or signing. If the live
        402 amount exceeds the reservation, the reservation is released and nothing is signed. The
        reservation is released on any failure and committed on success. Concurrent calls cannot together
        exceed the session cap. max_price_kas can only tighten the per-call maximum.
        """
        slug = validate_slug(slug)
        per_call = _parse_optional_max_price_kas(max_price_kas)
        try:
            item = await get_json(f"/v1/catalog/{slug}")
            quoted = int(item["price_sompi"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolError("catalog returned a malformed endpoint") from exc
        try:
            reserved = await budget.reserve(quoted, per_call_max_sompi=per_call)
        except BudgetError as exc:
            raise ToolError(str(exc)) from exc
        tightened = sompi_to_kas(reserved)
        try:
            result = await _execute_paid_call(slug, method, path, body, tightened)
        except Exception:
            await budget.release(reserved)
            raise
        receipt = result.get("payment")
        # Commit the reserved quoted price (the live 402 was capped at that amount). If no receipt is
        # present the call did not actually pay; release the reservation.
        if receipt is None:
            await budget.release(reserved)
        else:
            await budget.commit(reserved)
            result["budget"] = budget.snapshot()
        return result

        server.kasgate_config = cfg  # type: ignore[attr-defined]
    return server


def main() -> None:
    build_server(Config.from_env()).run("stdio")


if __name__ == "__main__":
    main()
