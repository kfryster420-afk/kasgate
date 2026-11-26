# KasGate Protocol Contracts

Source of truth: apps/gateway/app/config.py, models.py, schemas.py. Never rename a field, class, header or route below. Propose any change through an issue or pull request first.

## Monorepo
```
kasgate/
├── infra/                      docker-compose.yml, .env.example
├── docs/                       CONTRACTS.md, RUNBOOK.md
├── apps/gateway/               FastAPI gateway, Python 3.12
│   ├── Dockerfile, requirements.txt
│   ├── alembic.ini, alembic/env.py, alembic/versions/0001_initial.py
│   └── app/
│       ├── config.py, db.py, models.py, schemas.py
│       ├── kaspa_engine.py, oracle.py
│       ├── security.py, proxy.py, api_routes.py, main.py
├── apps/web/                   Next.js 15 App Router + TS + Tailwind, Dockerfile
├── packages/buyer-sdk/         pyproject.toml, kasgate_client/
├── packages/mcp-server/        pyproject.toml, kasgate_mcp/
└── tests/integration/          pytest suite against http://localhost:8000
```

## Protocol constants (app/config.py)
SOMPI_PER_KAS=100_000_000, PLATFORM_FEE_BPS=150, BPS_DENOMINATOR=10_000, X402_VERSION=2,
X402_SCHEME="kaspa-exact-v1", X402_ASSET="KAS", MIN_OUTPUT_SOMPI default 20_000_000 (0.1 KAS outputs breach the 100,000 standard storage mass).
Network prefixes: kaspa:testnet-10 -> "kaspatest:", kaspa:mainnet -> "kaspa:".

## Fee split (schemas.compute_fee_split)
fee = max(ceil(total * 150 / 10_000), MIN_OUTPUT_SOMPI); merchant = total - fee, must be >= MIN_OUTPUT_SOMPI.
Minimum endpoint price = 3 * MIN_OUTPUT_SOMPI = 60000000 sompi (0.6 KAS) at the default floor (merchant output >= 2*floor, see v2.3) — schemas.min_price_sompi(). Flat 0.2 KAS fee up to 1333333333 sompi, 1.5% (ceil) above.
On-chain: outputs[0] = merchant (payTo) EXACT merchant amount, outputs[1] = treasury (feeTo) EXACT fee.
outputs[2+] allowed only as change. Generator/create_transaction keep payment outputs first.

## x402 headers (schemas.py) — Base64(JSON), standard alphabet, padded
PAYMENT-REQUIRED  -> PaymentRequired{x402Version:2, scheme, network, asset:"KAS", amount:str(TOTAL sompi), payTo, platformFee{feeTo, feeAmount:str}}
                     build with PaymentRequired.for_endpoint(price_sompi, pay_to); merchant part = .merchant_amount_sompi
PAYMENT-SIGNATURE -> PaymentSignature{txId: 64 lowercase hex, rawTx: json.dumps(kaspa.Transaction.to_dict()) of SIGNED tx, payerAddress}
PAYMENT-RESPONSE  -> PaymentResponse{txId, settled:true, network, usdValueAtSettlement: decimal string, 8 dp}
Every model has .to_header() and Cls.from_header(value) (raises ValueError).
Error JSON body: X402ErrorBody{error, message, k402: "0.2"|null, accepts: [PaymentRequired, K402UtxoOffer?]|null} (list since CONTRACT v2.9; accepts[0] is always the PAYMENT-REQUIRED terms)
error codes: payment_required, invalid_payment_header, payment_replayed, underpaid, invalid_fee_split,
wrong_network, tx_rejected, rpc_unavailable, service_unavailable, rate_limited, endpoint_not_found, method_not_allowed, upstream_error, payment_not_found (k402 only)

## HTTP status mapping
no header -> 402 + PAYMENT-REQUIRED + body error=payment_required
bad header / underpaid / invalid_fee_split / wrong_network / tx_rejected -> 402 + PAYMENT-REQUIRED + body
replayed txId (Redis lock held or tx_id already in settlements) -> 409 error=payment_replayed
unknown/inactive slug -> 404 endpoint_not_found; method not in allowed_methods -> 405 method_not_allowed
rpc_unavailable (RpcUnavailableError) -> 503, release the Redis replay lock
Redis down on /p/* -> 503 service_unavailable (Retry-After: 5); rate limit hit on /p/* -> 429 rate_limited (Retry-After)
/v1/* keeps FastAPI {"detail": "..."} bodies for 429/503
header that fails schema validation on network/address prefix -> 402 wrong_network
upstream failure after settlement -> 502 upstream_error, PAYMENT-RESPONSE still set, settlement stays SETTLED

## Gateway routes
GET  /healthz                         -> HealthStatus
ANY  /p/{slug} and /p/{slug}/{path:path}  paywalled proxy (PROXY_PATH_PREFIX="/p")
POST /v1/merchants                    MerchantCreate{kaspa_address, display_name, email, challenge(purpose=register), public_key, signature} -> 201 MerchantCreated{merchant, api_key (once), session_token kgs_, expires_at}
GET  /v1/merchants/me                 -> MerchantRead            [Bearer]
POST /v1/endpoints                    EndpointCreate -> 201 EndpointRead   [Bearer]
GET  /v1/endpoints                    -> list[EndpointRead]       [Bearer]
GET  /v1/endpoints/{endpoint_id}      -> EndpointRead             [Bearer]
PATCH /v1/endpoints/{endpoint_id}     EndpointUpdate -> EndpointRead [Bearer]
DELETE /v1/endpoints/{endpoint_id}    -> 204 (sets is_active=false) [Bearer]
POST /v1/endpoints/import              OpenApiImportRequest -> 200 preview | 201 created  [Bearer kg_ or kgs_]
GET  /v1/settlements?limit=50&offset=0          -> list[SettlementRead] [Bearer]
GET  /v1/analytics?start=ISO&end=ISO            -> MerchantAnalytics    [Bearer]
GET  /v1/tax-report.csv?start=ISO&end=ISO       -> text/csv, header row = TAX_CSV_COLUMNS [Bearer]
GET  /v1/directory                              -> DirectoryManifest (public, cached 30 s)
GET  /v1/catalog?limit=50&cursor=&q=            -> CatalogPage (public, Cache-Control + ETag, 304 on If-None-Match)
GET  /v1/catalog/{slug}                         -> CatalogEndpoint (public, listed only; 404 endpoint_not_found)
GET  /.well-known/x402                          -> the same CatalogPage as /v1/catalog (same query params)
GET  /llms.txt                                  -> text/plain agent payment guide (public, cached 30 s)
GET  /v1/quote/{slug}                           -> PaymentRequired JSON (public, for agents/SDK)
Auth: "Authorization: Bearer kg_<token_urlsafe(32)>"; DB stores sha256 hex in Merchant.api_key_hash.
Slug auto-generated when omitted: slugify(name)[:55] + "-" + 6 hex chars.

## Engine interface (app/kaspa_engine.py)
class PaymentVerificationError(Exception): code: str (one of the error codes above), message: str
@dataclass(frozen=True) class VerifiedPayment: tx_id, payer_address, total_sompi, merchant_sompi, fee_sompi
class KaspaEngine:
    def __init__(self, settings: Settings)
    async def start() / stop() / is_connected() -> bool
    async def verify_and_submit(signature: PaymentSignature, required: PaymentRequired) -> VerifiedPayment
module singleton: engine = KaspaEngine(get_settings())

## Oracle interface (app/oracle.py)
class KasUsdOracle: __init__(settings, redis: redis.asyncio.Redis); async get_rate() -> Decimal (8 dp)
Redis keys: kasgate:oracle:kas_usd (TTL 60 s), kasgate:oracle:kas_usd:last (no TTL, fallback)
usd_value = (Decimal(total_sompi) / SOMPI_PER_KAS * rate).quantize(Decimal("0.00000001"))

## Redis keys
kasgate:tx:{txId}  SET NX EX 600 replay lock

## Merchant login / key recovery (wallet-signature, non-custodial) — added after a web-app review
POST /v1/auth/challenge   AuthChallengeRequest{kaspa_address} -> AuthChallenge{kaspa_address, challenge, expires_at}
                          challenge = "KasGate login\nAddress: {addr}\nNonce: {token_hex(16)}\nExpires: {iso}"; Redis kasgate:auth:{addr}:{nonce} EX 300, single use
POST /v1/auth/verify      AuthVerifyRequest{kaspa_address, challenge, public_key, signature} -> 200 SessionIssued{merchant, session_token kgs_, expires_at} (api_key NOT rotated)
                          verify: kaspa.verify_message(challenge, signature, public_key) AND PublicKey(public_key).to_address(network) == kaspa_address
                          challenge ALWAYS 200 (even for unknown addresses, to prevent enumeration); verify -> 401 for unknown address,
                          bad/expired/used challenge or bad signature (one generic message)
                          signature format: 128 lowercase hex Schnorr from kaspa.sign_message (KasWare signMessage(msg,{type:'schnorr'}));
                          server also accepts Base64 of 64 or 65 bytes (65 -> drop the first header byte) and normalises to hex
                          public_key: 66-hex compressed or 64-hex x-only; both verified OK with kaspa==2.1.0
POST /v1/merchants/me/rotate-key  [Bearer] -> MerchantCreated{merchant, api_key, session_token, expires_at} (old api_key invalid immediately; returns a fresh session too)
POST /v1/merchants duplicate kaspa_address or email -> 409 {"detail": "..."}
GET /v1/settlements: limit 1..200 (default 50), offset >= 0
FastAPI validation errors stay as {"detail": [...]}; x402 errors use X402ErrorBody.
Proxy path is /p/{slug} (never /v1/{slug}); quotes /v1/quote/{slug}.
Pricing: fixed sompi only for the MVP (no USD-pegged pricing).

## CONTRACT v2 — security revision after red-team testing (supersedes earlier lines where they conflict)
Payment binding (findings: mempool front-running, TOCTOU between quote and payment):
- PaymentRequired gains `nonce` (32 lowercase hex, schemas.new_payment_nonce()) and `expiresAt` (UTC, now + PAYMENT_MAX_AGE_SECONDS).
  Build: PaymentRequired.for_endpoint(price_sompi, pay_to, nonce, expires_at).
- Gateway stores the exact issued challenge: Redis `kasgate:nonce:{nonce}` = PaymentRequired JSON + endpoint slug, EX payment_max_age_seconds.
- PaymentSignature gains `nonce`. The signed tx `payload` MUST equal schemas.payment_payload(nonce) = b"kasgate:v1:" + nonce.
- On the paid retry the gateway loads `required` FROM THE NONCE (never recomputes), checks slug matches, consumes it atomically (GETDEL) only after
  successful settlement. Missing/expired nonce -> 402 `payment_expired` with a fresh PAYMENT-REQUIRED.
- Settlement.payment_nonce is UNIQUE. "Already in mempool/accepted" is success ONLY when the payload nonce is valid and unconsumed.
Pre-lock verification: run engine.verify() (network-free, finalize()-recomputed id, payload, outputs) BEFORE taking kasgate:tx:{txId}.
Settlement finality: settings.settlement_mode "accepted" (default) = submit, then wait until the tx is accepted by the virtual chain
  (subscribe_virtual_chain_changed or poll), up to settlement_timeout_seconds (8 s); timeout -> 503 rpc_unavailable, lock released, nonce kept.
  "mempool" = old behaviour, testnet demos only. Reject any replacement (submit_transaction_replacement is never used by the gateway).
Inputs (attacker-supplied UTXO values): engine fetches each input's real UTXO entry from the node (get_utxos_by_addresses(payerAddress) + mempool) and uses node values/scripts
  for mass and for payer attribution; mismatch -> invalid_payment_header.
Redelivery: same txId + same nonce + same slug, Settlement exists, upstream_status_code is NULL or >= 500, within redelivery_window_seconds and
  delivery_attempts < redelivery_max_attempts -> re-forward upstream, increment delivery_attempts, return PAYMENT-RESPONSE. Otherwise 409.
Transport: PUBLIC_BASE_URL must be https on mainnet (enforced in Settings). SDK keeps pinned fee_to/expected_pay_to options.
Headers: rawTx max 24_000 chars, MAX_HEADER_BYTES 40_960; uvicorn runs with --h11-max-incomplete-event-size 65536.
SSRF: schema rejects any literal IP that is not ip.is_global (covers 100.64.0.0/10, ::1, ULA) and *.localhost/*.internal.
  Runtime guard resolves DNS at request time, pins the resolved IP for the connection, rejects non-global IPs (nip.io, localtest.me), no redirects.
Addresses: validate_kaspa_address now checks the bech32 checksum via kaspa.Address.validate.
Auth (origin binding, single-use challenges, sessions):
- Challenge text = schemas.build_auth_challenge(purpose, domain=host of PUBLIC_BASE_URL, address, nonce, expires_at); purposes AUTH_PURPOSE / REGISTER_PURPOSE.
- AuthChallengeRequest gains purpose "login" | "register". Redis key kasgate:auth:{purpose}:{addr}:{nonce}.
- POST /v1/merchants now REQUIRES challenge + public_key + signature (purpose=register) -> proves address ownership.
- POST /v1/auth/verify now returns SessionIssued{merchant, session_token "kgs_...", expires_at} and DOES NOT rotate api_key.
  Sessions: Redis kasgate:session:{sha256(token)} -> merchant_id, EX session_ttl_seconds. Bearer accepts kg_ (api key) or kgs_ (session).
  Only POST /v1/merchants/me/rotate-key rotates the api key. Web dashboard cookie holds the session_token.
- Challenge rate limit is per-IP only (per-address limit removed: it allowed victim lockout).
Amounts: SompiStr max 18 digits. /healthz stays public but only returns status+network to unauthenticated callers is NOT required for MVP.
Error codes: + payment_expired (402).

## CONTRACT v2.1 — fee floor (2026-11-20)
- Treasury fee = max(ceil(total*150/10000), MIN_OUTPUT_SOMPI). Merchant = total - fee, must be >= MIN_OUTPUT_SOMPI.
- ~~min price 0.4 KAS~~ SUPERSEDED by v2.3: min_price_sompi(floor) = 3*floor = 60_000_000 sompi (0.6 KAS).
- Below 1_333_333_334 sompi the fee is the flat floor (0.2 KAS); above that it is 1.5% (ceil).
- Engine: exact-value checks against PaymentRequired still apply; the ">= 1.5%" check stays valid because fee >= 1.5% always.
- Clients (web lib/sompi.ts, buyer SDK) must replicate: fee = max(ceil(amount*150/10000), min_output_sompi) using DirectoryManifest.min_output_sompi.

## CONTRACT v2.2 — web app auth and session details (2026-11-20)
- Registration and rotate-key return MerchantCreated WITH a session (session_token kgs_ + expires_at), so the web app never stores a kg_ key in a cookie.
- Challenge "Domain:" = urlsplit(PUBLIC_BASE_URL).netloc lowercased (host, plus :port only when the URL has one). Exactly one form; clients compare exact string.
  The web app's public URL that users see must therefore equal PUBLIC_BASE_URL's netloc (in local dev: localhost:8000 vs the web on :3000 → web compares with NEXT_PUBLIC_GATEWAY_URL netloc, which must equal PUBLIC_BASE_URL).
- SompiStr 18-digit cap stands (max 999_999_999_999_999_999 sompi ≫ total KAS supply).
- KasWare signature format: script-signed (kaspa sign_message) registration verified live on tn10; real KasWare 0.11.6 (Chrome) schnorr signMessage verified live on tn10 for register + login (Nov 20, 2026); gateway normalisation (hex128 / Base64 64|65 bytes) is the contract.

## CONTRACT v2.3 — storage-mass correction to the minimum price (2026-11-20, from a live testnet finding)
- At 0.4 KAS the two 0.2 KAS outputs use the entire 100_000 standard storage mass; any change output makes the tx non-standard.
- Merchant output must be >= 2*MIN_OUTPUT_SOMPI. min_price_sompi(floor) = 3*floor = 60_000_000 sompi (0.6 KAS). Fee rule unchanged.
- Verified with kaspa.calculate_storage_mass: at >= 0.6 KAS a single-input payment with change >= ~0.3 KAS stays <= 100_000.
- Buyer SDK MUST select inputs so change is 0 or >= 30_000_000 sompi (add another UTXO when needed); never emit small change.
- Payments drawing from more than 16 payer addresses' UTXOs are rejected (invalid_payment_header).

## CONTRACT v2.4 — proxy, nonce and session details (2026-11-20)
- /v1/quote issues a stored, payable nonce and counts against the unpaid per-IP limit. Every /p response that mints a challenge counts as an unpaid hit.
- kasgate:noncelock:{nonce} NX EX 600 is part of the contract (taken before kasgate:tx:{txId}); the unique payment_nonce index is the backstop.
- Slug mismatch on a paid retry -> 402 invalid_payment_header + fresh challenge (no new code).
- Redelivery: not re-verified on-chain (the Settlement row is the proof); bound to payer, method, path. Query string and body are not bound in the MVP.
- Sessions: Redis set kasgate:sessions:{merchant_id} indexes the merchant's session hashes. DELETE /v1/auth/session [Bearer kgs_] logs out the current session.
  rotate-key revokes ALL sessions of that merchant, then issues the new one returned in MerchantCreated.
- Challenge Domain includes :port when PUBLIC_BASE_URL has one (exact netloc.lower()).
- Registration/login require Redis regardless of RATE_LIMIT_ENABLED.

## CONTRACT v2.5
- Sompi int fields (e.g. EndpointCreate/Read.price_sompi) are capped at MAX_SOMPI = 999_999_999_999_999_999 (18 digits), same as SompiStr. 19 digits -> 422.
- Representation is intentional: CRUD int fields (price_sompi) are JSON numbers, so clients must parse them big-int safe; x402 header amounts and SettlementRead/tax sompi fields stay strings.

## CONTRACT v2.6 — payer proof (closes the mempool front-running finding)
- PaymentSignature gains `payerProof`: 128-hex Schnorr kaspa.sign_message(schemas.payer_proof_message(nonce, txId), payer_private_key)
  where payer_proof_message = "kasgate:v1:{nonce}:{txId}" (Base64 64/65-byte forms normalised to hex like auth signatures).
- payerAddress must be a P2PK Schnorr address (script 0x20 <32-byte x-only pubkey> 0xac). Engine verify(): extract the x-only pubkey from
  pay_to_address_script(payerAddress), kaspa.verify_message(msg, payerProof, PublicKey(xonly_hex)) must be True, else invalid_payment_header.
  resolve_inputs() already requires at least one input to be spent from payerAddress's script, so an observer of the tx cannot claim it.
- Verified with kaspa==2.1.0: PublicKey(xonly).to_address() round-trips and verify_message accepts sign_message output.
- Payers MUST spend P2PK Schnorr UTXOs (kaspa:q... addresses); script-hash (P2SH) and ECDSA payers are refused with invalid_payment_header.
- The proof binds nonce and txId (not method/path); the nonce itself is already bound to the endpoint that issued it.

## CONTRACT v2.7 — agent discovery catalog (additive)
GET /v1/catalog and GET /.well-known/x402 return CatalogPage{catalog_version:1, x402_version:2, scheme, asset, network, gateway_base_url, items, next_cursor, limit}.
CatalogEndpoint{slug, name, description, methods, price_currency, price_sompi, price_kas, price_usd_micros, price_usd, network, schemes, paid_url, input_schema}. The catalog does not live-convert USD prices: price_sompi is null for USD-priced endpoints; the payable amount comes from GET /v1/quote/{slug} or the 402.
`input_schema` is optional and is null today: the endpoints table has no schema column; the field is reserved and omitted-or-null rather than inferred from secrets.
limit 1..200 (default 50); cursor is an opaque base64url token from next_cursor (422 if malformed); q is an optional case-insensitive substring on slug, name, or description (max 120).
Only Endpoint.is_public_listed and is_active of an active Merchant are included. Never email, api keys, upstream URL, upstream headers, or merchant settlement addresses.
Cache-Control: public, max-age=30; Redis kasgate:catalog:v1 holds the snapshot for 30 s. ETag is a strong SHA-256 of the exact JSON body of that page; If-None-Match (including the weak W/ form) yields 304 with the same ETag and Cache-Control.
GET /v1/catalog/{slug} returns one listed CatalogEndpoint (same caching) or 404 X402ErrorBody error=endpoint_not_found.
GET /llms.txt is a public text/plain description of headers, the fee split (98.5% merchant output 0 / 1.5% treasury output 1, 0.2 KAS min fee, 0.6 KAS min price), the catalog URLs, and SDK/MCP usage.
The existing GET /v1/directory DirectoryManifest is unchanged.

## CONTRACT v2.8 — one-click OpenAPI import (additive)
POST /v1/endpoints/import [Bearer kg_ or kgs_, same dependency as the other endpoint routes] accepts OpenApiImportRequest (extra="forbid").
mode: "preview" | "create". Exactly one of spec_text or spec_url. upstream_base_url and default_price_sompi are required; optional upstream_headers, category, is_public_listed (default true).
selections: list of {method, path, price_sompi?} (1..500, unique method+path) is required for create and rejected for preview. method is GET|POST|PUT|PATCH|DELETE. price_sompi is an optional per-row override; omitted rows use default_price_sompi. Both prices are Sompi integers and must pass compute_fee_split() (>= 0.6 KAS). Imported rows are KAS-priced (EndpointCreate.price_currency defaults to KAS); USD import is not in this version.
mode=preview -> 200 OpenApiImportPreview (Cache-Control: no-store). Nothing is written. Each OpenApiOperationPreview lists key, method, path, operation_id, summary, path_params, importable/skip_reason, slug, upstream_url, proxy_path, proxy_url, price_sompi.
mode=create -> 201 OpenApiImportCreated{created_count, created:[EndpointRead]}. The route is stateless: create re-reads or re-fetches the document and matches selections by method + path. All selected endpoints are created in one transaction or none are.
Errors: 401 missing or bad auth; 409 slug race (everything rolled back); 413 document over 2 MiB or body over 4.25 MiB; 422 per-row error locations or a readable message for a bad document. Bad input never returns 500.
Mapping: one endpoint per operation, allowed_methods=[METHOD]. Path parameters use the existing suffix routing: GET /pets/{petId} has upstream <base>/pets and is called as /p/<slug>/<petId>. The proxy does not check the suffix against the template, the same as every endpoint today. Slugs are deterministic from operationId or method+path, with -2/-3 on collision against the global slug index. Each row is validated by constructing an EndpointCreate. HEAD, OPTIONS and TRACE, plus unsafe paths, are skipped with a reason.
spec_url is fetched through the existing guarded upstream client (DNS pinned to a public IP). Redirects are followed by hand, at most 3, each re-checked, with a 10 s timeout; the 2 MiB cap is enforced while reading. Remote $ref and $dynamicRef are refused anywhere in the document; nothing is ever fetched for a reference. YAML uses safe_load with a nesting depth cap of 100, an alias cap of 1000, and a 2M-node expanded-size cap. Only OpenAPI 3.0.x and 3.1.x are accepted; Swagger 2.0 and OpenAPI 3.2 are rejected. There is no per-merchant endpoint limit in this version.
Source of truth: apps/gateway/app/schemas.py (OpenApiImport*), app/openapi_import.py, app/api_routes.py::import_openapi. Narrative detail: docs/OPENAPI_IMPORT.md.
Additive note (USD import): OpenApiImportRequest gains optional default_price_currency (KAS default) and default_price_usd_micros. A selection may include price_usd_micros. Exactly one price per currency: KAS imports send default_price_sompi and optional per-row price_sompi (each must pass compute_fee_split, >= 0.6 KAS) and omit USD fields; USD imports send default_price_usd_micros and optional per-row price_usd_micros (UsdMicros, 1 USD = 1_000_000 micros) and omit sompi fields. Preview rows and the preview envelope include price_currency, price_sompi (null on USD) and price_usd_micros (null on KAS). Created EndpointRead rows set price_currency and the matching price field, with the other null, satisfying ck_endpoints_price_matches_currency. Mixed or missing prices are 422 at their field location, e.g. ["body","selections",2,"price_sompi"].

## CONTRACT v2.9 — k402 kaspa-utxo ingest (additive)
Source: kaspa-402/k402 commit 63e1eae (PROTOCOL.md "v0.2", k402/schemes.py, k402/wallet.py HotWallet.pay). Research: K402_DIFF.md.
- Every 402 from /p/{slug} (and every fresh-challenge 402) is ALSO a k402 offer body: `k402: "0.2"`, `accepts: [<kaspa-exact-v1 PaymentRequired, same as the header>, <K402UtxoOffer>]`. k402 clients skip the unknown `kaspa-exact-v1` entry; KasGate clients keep reading PAYMENT-REQUIRED. GET /v1/quote/{slug} stays v1-only.
- K402UtxoOffer{scheme:"kaspa-utxo", network:"testnet-10"|"mainnet" (k402 wire names, never the kaspa: CAIP form; anything else is refused), amount_sompi:str (MERCHANT output only), pay_to (merchant address), payment_id:"p_"+16 hex, expires: unix seconds (= the v1 expiresAt), description (endpoint name), facilitator_fee{sompi:str, to: treasury, by:"kasgate"}}. `finality` is omitted (= 1, accepted).
- Quote: total = locked v1 total + tag, where the locked v1 total is PaymentRequired.amount of the challenge minted in the same 402 (KAS-priced: price_sompi; USD-priced: the same single oracle quote, ceil conversion and 0.6 KAS floor as v1, never re-quoted), tag uniform in [0, 1_000_000) sompi (< 0.01 KAS, paid to the merchant); fee = compute_fee_split(total).fee_sompi (max(ceil(1.5 %), 0.2 KAS floor)), amount_sompi = total - fee, total >= 0.6 KAS. The tag exists because k402 txs carry no payload nonce or payerProof: exact amounts are the only on-chain binding, so a bystander's own offer for the same endpoint matches a victim's tx with probability ~ (their open offers) / 1_000_000. The tag is the k402 anti-front-running measure.
- USD-priced endpoints: the k402 offer reuses the v1 challenge's locked quote, so the offer's total minus its tag equals the v1 amount exactly, and the settlement's USD value uses the same locked kasUsdRate. Oracle unavailable -> 503 price_unavailable exactly like v1, with no k402 offer and no kasgate:k402 key written.
- Redis: kasgate:k402:{payment_id} -> JSON{slug, nonce (internal 32 hex, never sent; becomes settlements.payment_nonce), offer, kasUsdRate (the locked rate string for USD-priced endpoints, else null)}, EX payment_max_age_seconds. Paying the v1 challenge and the k402 offer of one 402 are independent payments.
- Retry header: `X-K402-Payment: kaspa-utxo <txid 64 hex> <payment_id>` (exactly three tokens, <= 256 chars). kaspa-session, kaspa-channel and every other scheme are out of scope and get 402 invalid_payment_header. Sending it together with PAYMENT-SIGNATURE is 402 invalid_payment_header. X-K402-Payment is never forwarded upstream.
- Order: parse (bad/empty -> 402 invalid_payment_header + fresh offers) -> load offer -> existing settlement for the txid (either binding) or payment_id -> redelivery (same txid + payment_id + slug + method + path, v1 window and cap) else 409 -> unknown/expired offer -> 402 payment_expired -> other slug -> 402 invalid_payment_header -> `SET kasgate:k402lock:{payment_id} NX EX 600` then the SHARED `SET kasgate:tx:{txId} NX EX 600` (held -> 409 payment_replayed) -> engine.verify_k402 -> settle + deliver exactly as v1, then GETDEL the offer. Only a verification failure releases both locks.
- engine.verify_k402(tx_id, offer): nothing is submitted (the payer already did). accepted mode reads the sink FIRST. Lookup: get_mempool_entries_by_addresses([merchant, treasury]) receiving side (full tx) -> outputs checked, then the virtual chain is polled from that sink until it accepts the txid (timeout -> one UTXO-index check -> else payment_not_found); not in the mempool -> get_utxos_by_addresses([merchant, treasury]) filtered by txid (accepted outputs only). Nothing found -> 402 payment_not_found (k402 clients retry the same header on 402, ~5 times over ~15 s).
- Output rule (check_k402_outputs), by index AND by locking script: outputs[0] = pay_to EXACTLY amount_sompi, outputs[1] = facilitator_fee.to EXACTLY facilitator_fee.sompi, both re-checked against the fee floor and the 2*floor merchant minimum. Treasury output missing, 1 sompi short or over, paying another address, or found at any index other than 1 (swap, reordering) -> invalid_fee_split; merchant short -> underpaid; merchant over, wrong script or at another index -> invalid_fee_split. An output that is no longer visible (spent before the lookup) is refused, never assumed. The kasgate:v1 payload and payerProof are NOT required (official clients send an empty payload); the official k402 server's balance-delta check is not used.
- settlements: payment_scheme "kaspa-exact-v1"|"k402-kaspa-utxo" (server default v1), k402_payment_id (unique index ix_settlements_k402_payment_id, "p_"+16 hex, set iff k402), payer_address NULL only for k402 (no payer proof). Alembic 0004_k402_utxo (down_revision 0003_usd_pricing); its downgrade refuses while k402 rows exist. SettlementRead gains payment_scheme; payer_address and TaxReportRow.payer_address are nullable.
- Residual vs v1: no payer identity, so the payment_id (sent only to the client that asked for the offer) is the bearer secret for a paid k402 call, and front-running protection is the amount tag above rather than payerProof.
- Verified offline with kaspa==2.1.0: create_transactions(outputs=[merchant, treasury]) keeps that order and appends change, also with a larger output 1 and with 68+ inputs; payload is empty. Upstream finding: HotWallet.__init__ calls to_address("testnet-10"), which kaspa 2.1.0 refuses ("Invalid network type"), so an unmodified HotWallet cannot be constructed on testnet-10 (mainnet is fine; create_transactions itself needs "testnet-10"). The unmodified HotWallet.pay + k402.Client flow is covered in tests/test_k402.py with the wallet assembled field by field.

## CONTRACT v2.10 — community kaspa-x402 `exact` / `kaspa-exact-v2` (standard-native) ingest
Source: elldeeone/kaspa-x402 tag v1.0.1 (commit 56221913531bcb8367a900d28ed24c62d50591a1): protocol/spec (http-profile, exact binding), packages/core (envelope narrowing, canonical JSON, request hash, exact-authorization), packages/server (`#verifyStandardNative`). Research: SPEC_DIFF.md. Out of scope: batch-settlement, the additive and hash-chain exact profiles (their entries are never offered; a payload naming them is 402 invalid_payment_header).
- **No platform fee on this binding during the beta.** The exact entry pays `payTo` = the merchant the full price; there is no treasury output, `fee_amount_sompi` is 0. kaspa-exact-v1 and k402 are unchanged (output 0 merchant, output 1 treasury; a v1 tx without output 1 is never approved).
- PAYMENT-REQUIRED (every fresh-challenge 402) is ONE x402 v2 envelope, canonical JSON (sorted keys, compact) then standard Base64: `{x402Version: 2, resource: {url, description}, accepts: [<exact entry>, <kaspa-exact-v1 terms>], extensions: {"payment-identifier": {info: {required: true}, schema}}, error}` plus the kaspa-exact-v1 terms mirrored at the top level (`scheme, network, asset, amount, payTo, platformFee, nonce, expiresAt[, extra]`). The `kaspa-exact-v1` entry is the v1 PaymentRequired without `x402Version`. Official clients validate each accepts entry and skip the ones they do not support (core `narrowPaymentRequiredEnvelope`), so they select the exact entry; KasGate SDK >= this release selects the kaspa-exact-v1 entry; the mirror keeps lenient readers of the flat header working. Deviation, documented: the kaspa-x402 HTTP profile says Kaspa servers "must emit only exact and batch-settlement entries"; KasGate also emits its own kaspa-exact-v1 entry, which conforming clients skip. A v1 verification failure on a known nonce still answers with the flat v1 header (same terms, no new offers). Header size stays under MAX_HEADER_BYTES (40,960).
- Exact entry: `{scheme: "exact", network: "kaspa:testnet-10"|"kaspa:mainnet", amount, asset: "KAS", payTo, maxTimeoutSeconds: payment_max_age_seconds, extra: {binding: "kaspa-exact-v2", profile: "standard-native", paymentFlow: "upfront", finality: "accepted", transactionEncoding: "kaspa-sdk-safe-json-v2.0.0", payToScriptPublicKey}}`. `resource.url` = PUBLIC_BASE_URL + the raw request path + raw query (what a WHATWG client has as `href` without fragment). `amount` = the v1 challenge's LOCKED total from the same 402 (KAS-priced: price_sompi; USD-priced: the same single oracle quote, ceil conversion and 0.6 KAS floor). Oracle unavailable -> 503 price_unavailable with no header, no accepts and no kasgate:exact key, exactly like v1 and k402.
- KasGate policy: minimum merchant output 20,000,000 sompi (0.2 KAS; below it no exact entry is offered — unreachable while the v1 minimum price is 0.6 KAS). Signatures must be SIGHASH_ALL (0x01) and the transaction mass standard; miner fee <= 1 KAS (the official MAX_SAFE_TRANSACTION_FEE_SOMPI).
- 402 body (k402 shape kept): `k402: "0.2"`, `accepts: [<v1 PaymentRequired>, <K402UtxoOffer>, <exact entry>]`. The exact entry is appended, nothing is replaced; k402 and KasGate clients skip it.
- Redis: `kasgate:exact:{slug}:{paymentRequirementsHash}` -> canonical JSON{slug, requirement, kasUsdRate (locked rate for USD endpoints, else null)}, EX payment_max_age_seconds. The offer is not consumed on settlement (a KAS-priced offer is shared by every 402 at that price); the unique payment identifier, txId and request hash make each settlement single-use. `kasgate:pid:{payment identifier}` NX EX 600 is the identifier claim.
- Retry: PAYMENT-SIGNATURE = Base64(canonical JSON PaymentPayload) `{x402Version: 2, accepted, payload: {type: "exact-transaction", profile: "standard-native", transaction (safe-JSON artifact), transactionEncoding, paymentOutputIndex, requestHash, payerAddress?, authorization: {version: "kaspa-x402-exact-request-authorization-v2", inputIndex, expiresAt, digest, signature}}, extensions: {"payment-identifier": {info: {required: true, id}}}}`, <= 65,536 chars. Dispatch: a PAYMENT-SIGNATURE whose decoded JSON has `accepted` or `payload` takes the exact path, anything else the v1 path; X-K402-Payment is checked first and combining it with PAYMENT-SIGNATURE stays 402 invalid_payment_header.
- Order: strict parse (bad -> 402 invalid_payment_header + fresh offers) -> prepare the upstream request -> load the stored offer for `accepted` -> requestHash = sha256(stableStringify({method, url: resource URL, body: UTF-8 text or null, paymentRequirementsHash})) -> pure verification without expiry (artifact, txid recomputation, outputs, mass, input Schnorr signatures, authorization digest + signature) -> existing settlement for the txId (any binding) or payment identifier: redelivery when the identifier, txId, requestHash, payer, slug, method and path all match (v1 window and attempt cap, re-verified in full), else 409 payment_replayed (`kaspa_payment_identifier_conflict` when the identifier was used for another request) -> no live offer -> 402 payment_expired (`invalid_kaspa_x402_accepted`) -> authorization expiry (`expiresAt` in the future, <= now + maxTimeoutSeconds) -> `SET kasgate:pid:{id} NX` then the SHARED `SET kasgate:tx:{txId} NX` (one txId can never be claimed through two bindings) -> node lookup of every embedded UTXO (confirmed or mempool, same amount and script) -> submit and wait for ACCEPTANCE regardless of settlement_mode -> settlement row -> forward. Failures before the lock -> 402 with fresh offers; after it the locks are released; node down -> 503 rpc_unavailable + Retry-After 5.
- Error codes: invalid_exact_payment (envelope, signatures, authorization, request hash, unknown UTXOs), underpaid (payment output short), tx_rejected (fee ceiling, outputs > inputs, mass, node rejection), wrong_network, payment_expired, invalid_payment_header, payment_replayed. Messages embed the official reason where one exists (invalid_kaspa_x402_accepted, missing_kaspa_payment_identifier, expired_authorization, authorization_exceeds_max_timeout).
- PAYMENT-RESPONSE: the official SettlementResponse, canonical JSON + Base64: `{success: true, transaction: txId, network, payer, amount, extensions: {kaspa: {exactProfile: "standard-native", paymentOutputIndex, finality: "accepted", requestHash, transactionEncoding}, kasgate: {platformFeeSompi: "0", usdValueAtSettlement}}}`. Redeliveries carry the same receipt.
- settlements: payment_scheme "kaspa-exact-v2" (with v1 and k402 in ck_settlements_payment_scheme_known), payment_identifier (unique index ix_settlements_payment_identifier, `^[A-Za-z0-9_-]{16,128}$`), request_hash (64 lowercase hex); both set iff kaspa-exact-v2. treasury_address and payment_nonce become nullable: NULL exactly on kaspa-exact-v2 rows, which also have fee_amount_sompi = 0, a payer_address and no k402_payment_id (ck_settlements_exact_v2_shape); every other row keeps fee > 0 with a treasury and a nonce (ck_settlements_treasury_split_unless_exact_v2). tx_id and payment_nonce unique indexes unchanged. Alembic 0005_exact_v2 (down_revision 0004_k402_utxo); its downgrade refuses while kaspa-exact-v2 rows exist. SettlementRead: payment_scheme gains "kaspa-exact-v2", treasury_address and payment_nonce nullable, + payment_identifier.
- Interop evidence: the official vectors (tests/fixtures/kaspa-x402, MIT, hashes in its README) and tests/test_interop_kaspa_x402.py, where the npm-published @kaspa-x402/client 1.0.1 parses a real KasGate 402, selects the exact entry, computes the same paymentRequirementsHash and requestHash, pays and accepts the PAYMENT-RESPONSE with `applySettlement`. GET /v1/quote/{slug} and the agent catalog stay kaspa-exact-v1 only.

## Notes from the funded testnet-10 run
- Node RPC UTXO entries return scriptPublicKey as a hex string "<u16 BE version><script>" (e.g. "0000"+"20<xonly>ac"), not only the dict form; parsers MUST accept both.
- A paid call whose upstream fails (5xx/connection error) is NOT refunded on-chain; buyers retry with the same PAYMENT-SIGNATURE within redelivery_window_seconds (max redelivery_max_attempts).
- Test/dev hosts with synthetic DNS (every name -> 198.18.0.1) are correctly blocked by the SSRF guard; use an IP-literal public upstream such as https://1.1.1.1/cdn-cgi/trace for funded tests.
