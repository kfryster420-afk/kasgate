# One-click OpenAPI import (`POST /v1/endpoints/import`)

Normative contract: `docs/CONTRACTS.md` (CONTRACT v2.8). Source of truth: `apps/gateway/app/schemas.py` (OpenApiImport*), `app/openapi_import.py`, `app/api_routes.py::import_openapi`.

## Route
```
POST /v1/endpoints/import   [Bearer kg_ or kgs_]   OpenApiImportRequest
  mode "preview" -> 200 OpenApiImportPreview   (nothing is written)
  mode "create"  -> 201 OpenApiImportCreated   (one transaction: every selected endpoint or none)
```
The route is stateless: `create` re-reads the same document (re-fetches `spec_url`) and matches selections by method + path.

## Request: OpenApiImportRequest (extra="forbid", whitespace stripped)
```
mode:                "preview" | "create"
spec_text:           str | null      OpenAPI 3.0.x / 3.1.x document, JSON or YAML (detected: JSON when it starts with { or [)
spec_url:            HttpUrl | null  public http(s) URL fetched server-side            (exactly one of spec_text / spec_url)
upstream_base_url:   HttpUrl         same _reject_private_host rule as EndpointCreate.upstream_url
default_price_sompi: Sompi           int, 1..MAX_SOMPI (18 digits), compute_fee_split() must pass (>= 3*F = 60_000_000)
upstream_headers:    dict[str,str]   default {}; same validate_upstream_headers + Fernet encryption as POST /v1/endpoints, stored on every created endpoint
category:            str | null      <= 60; when null each endpoint gets its operation's first tag (truncated to 60)
is_public_listed:    bool            default true
selections:          list[OpenApiImportSelection] | null   1..500; required for create, forbidden for preview; unique (method, path)
OpenApiImportSelection: {method: GET|POST|PUT|PATCH|DELETE (case-insensitive), path: str 1..512 exactly as in the document,
                         price_sompi: Sompi | null  (override; same fee-split check)}
```

## Responses
```
OpenApiImportPreview {mode:"preview", openapi_version, title|null, api_version|null, servers: [str] (<=5, from the document),
  operation_count, importable_count, default_price_sompi:int, min_price_sompi:int, operations: [OpenApiOperationPreview]}
OpenApiOperationPreview {key:"<METHOD> <path>", method, path, operation_id|null, summary|null (<=200 chars), path_params:[str],
  importable:bool, skip_reason|null, slug|null, upstream_url|null, proxy_path|null ("/p/<slug>/{petId}"), proxy_url|null, price_sompi:int}
OpenApiImportCreated {mode:"create", created_count:int, created:[EndpointRead]}   (EndpointRead incl. price_kas and proxy_url)
```
Sompi fields are JSON integers (CONTRACT v2.5 CRUD convention).

## Mapping an operation onto a KasGate endpoint
- One endpoint per operation: `allowed_methods=[METHOD]`, `price_sompi` = override or default, name = summary or operationId or "METHOD path" (<=120),
  description = description or summary (<=2000). Each row is validated by constructing an `EndpointCreate`, so imported rows meet exactly the hand-made rules.
- Path parameters use the existing proxy suffix routing (`/p/{slug}/{path:path}` appends the suffix to the upstream URL); no new routing.
  `upstream_url = upstream_base_url + <path up to the first segment containing {…}>`; the buyer appends the rest:
  `GET /pets/{petId}` -> upstream `<base>/pets`, buyer calls `/p/<slug>/<petId>`. A path without parameters is used verbatim; `/` -> the base URL.
  As for every endpoint, the proxy does not enforce the template shape: any suffix is forwarded under the static prefix.
- Slugs are deterministic: slugify(operationId, camelCase split) or slugify("METHOD path"), max 64, SLUG_RE. Collisions with existing slugs
  (any merchant, active or not: the index is global) or inside the batch get `-2`, `-3`, … in document order. Preview and create allocate identically.
- Skipped (importable=false, never created): methods outside GET/POST/PUT/PATCH/DELETE (HEAD, OPTIONS, TRACE), paths > 512 chars, paths with
  `.`/`..` segments, `?`, `#`, `\`, whitespace or malformed `{}`, and rows whose derived EndpointCreate fails validation.

## Document rules (422 with a plain `detail` string unless noted)
- Size: document <= 2 MiB (413). Request body <= 2*2 MiB + 256 KiB (413, checked on Content-Length and while streaming).
- Must be an object with `openapi` 3.0.x or 3.1.x (unquoted YAML 3.1 accepted); `swagger: "2.0"` -> clear "convert to 3.x" error.
- Non-empty `paths` object with at least one operation; more than 500 operations -> 422.
- `$ref` / `$dynamicRef` must be local (`#...`) anywhere in the document; nothing is ever resolved remotely. Path-item `$ref` to `#/...` is followed (<= 8 hops).
- YAML: safe loader only (CSafeLoader / SafeLoader), one document, an event pre-pass caps nesting depth at 100 and aliases at 1000; the loaded tree is
  scanned for recursion and for logical expanded size (> 2,000,000 nodes -> refused, so alias bombs never expand). JSON nesting is capped the same way.
- Malformed JSON/YAML -> 422 with line/column. Never 500.

## spec_url fetch (SSRF)
- Validated like an upstream URL (http/https, no localhost/*.internal/*.localhost, literal IPs must be is_global) before any request.
- Fetched with the gateway's guarded upstream client (`app.state.http_client`, GuardedTransport: DNS resolved per connection, every answer must
  be public, connection pinned to the checked IP, unix sockets refused, no proxy env).
- Redirects are followed manually (max 3), each Location re-validated by the same rules; 10 s total timeout; body streamed with the 2 MiB cap
  (413); non-200 -> 422 "HTTP <code>"; non-UTF-8 -> 422.

## Errors
```
401  missing/invalid bearer (same dependency as the endpoint CRUD)
409  a proposed slug was taken concurrently between the check and the commit; transaction rolled back, nothing created
413  request body or document too large
422  FastAPI validation list for request fields: per-row loc ["body","selections",<i>,"price_sompi"] for prices,
     ["body","selections",<i>] for selections that are not in the document or are skipped; plain string for document errors
```
