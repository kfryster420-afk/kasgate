# kasgate-mcp

Stdio MCP server for KasGate. Tools:

| tool | purpose |
| --- | --- |
| `list_directory` | `GET /v1/directory`: the public catalog of paid endpoints. |
| `search_catalog(query, limit, cursor)` | `GET /v1/catalog`: search listed endpoints by slug, name, or description. |
| `inspect_endpoint(slug)` | `GET /v1/catalog/{slug}`: one listed endpoint (price, methods, paid URL, input schema). |
| `quote_endpoint(slug)` | `GET /v1/quote/{slug}`: price and fee split, nothing is paid. |
| `get_budget` | session cap, per-call maximum, spent, reserved, and remaining, in sompi. |
| `call_with_budget(slug, method, path, body, max_price_kas)` | pays a 402 under the session spend cap: the catalog price is reserved before signing, released on failure, and never exceeds the per-call max or the remaining cap. |
| `call_paid_endpoint(slug, method, path, body, max_price_kas)` | calls `/p/{slug}/{path}` and pays the 402 challenge, refusing anything above the cap. Does not draw down the session cap. |

## Environment

| variable | meaning |
| --- | --- |
| `KASGATE_BUYER_PRIVATE_KEY_FILE` | **Preferred.** Path of a file holding the buyer's 64-character hex private key (`~` is expanded, surrounding whitespace and the trailing newline are stripped). Wins over `KASGATE_BUYER_PRIVATE_KEY` when both are set (a warning is logged to stderr, never the key). The file must be a regular file that is not readable by group or others: create it with `umask 077` or `chmod 600`, otherwise the server refuses to start. |
| `KASGATE_ALLOW_INSECURE_KEY_FILE` | `1`/`true`/`yes`/`on` downgrades the permission refusal to a warning on stderr. Only for setups where file modes are meaningless (some mounted volumes). |
| `KASGATE_BUYER_PRIVATE_KEY` | The key inline, as hex. Convenient but visible in the process environment and in MCP client config files; prefer the file. Without either variable the server still lists and quotes, but paid calls are disabled. |
| `KASGATE_GATEWAY_URL` | Gateway base URL, default `http://localhost:8000`. |
| `KASGATE_NETWORK` | `kaspa:testnet-10` (default) or `kaspa:mainnet`. |
| `KASGATE_RPC_URL` | Optional wRPC URL for UTXO lookups (default: the public resolver). |
| `KASGATE_MAX_PRICE_KAS` | Hard ceiling per call, default `20`; the `max_price_kas` tool argument can only lower it. |
| `KASGATE_SESSION_BUDGET_KAS` | Session spend cap for `call_with_budget`, default `20`. Concurrent calls reserve the quoted price atomically and cannot together exceed this cap. |
| `KASGATE_REQUEST_TIMEOUT` | Seconds the server waits for a gateway response (including the payment settlement), default `60`. Must be a positive number or the server exits at startup. |

The key's contents are never logged, never part of an error message and not shown in `repr(Config)`. A missing, malformed
(not exactly 64 hex characters), binary or non-regular key file makes the server exit with a message that names the path
only.

```json
{"mcpServers": {"kasgate": {"command": "kasgate-mcp", "env": {"KASGATE_BUYER_PRIVATE_KEY_FILE": "/home/me/.config/kasgate/buyer.key", "KASGATE_GATEWAY_URL": "http://localhost:8000"}}}}
```

Works with mcp 2.x (`MCPServer`) and falls back to mcp 1.x (`FastMCP`).
