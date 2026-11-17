# kasgate-mcp

Stdio MCP server for KasGate. Tools:

| tool | purpose |
| --- | --- |
| `list_directory` | `GET /v1/directory`: the public catalog of paid endpoints. |
| `quote_endpoint(slug)` | `GET /v1/quote/{slug}`: price and fee split, nothing is paid. |
| `call_paid_endpoint(slug, method, path, body, max_price_kas)` | calls `/p/{slug}/{path}` and pays the 402 challenge, refusing anything above the cap. |

Environment: `KASGATE_BUYER_PRIVATE_KEY` (hex, required for paid calls), `KASGATE_GATEWAY_URL` (default
`http://localhost:8000`), `KASGATE_NETWORK` (default `kaspa:testnet-10`), `KASGATE_RPC_URL` (optional),
`KASGATE_MAX_PRICE_KAS` (hard ceiling per call, default `20`; `max_price_kas` can only lower it).

```json
{"mcpServers": {"kasgate": {"command": "kasgate-mcp", "env": {"KASGATE_BUYER_PRIVATE_KEY": "<hex>", "KASGATE_GATEWAY_URL": "http://localhost:8000"}}}}
```

Works with mcp 2.x (`MCPServer`) and falls back to mcp 1.x (`FastMCP`).
