# KasGate Shared Contracts (owner: 01 - KasGate Architect)

Source of truth: apps/gateway/app/config.py, models.py, schemas.py. Never rename a field, class, header or route below. If you need a change, message 01 first.

## Monorepo
```
kasgate/
├── infra/                      docker-compose.yml, .env.example            (01)
├── docs/                       CONTRACTS.md, work-orders/                   (01)
├── apps/gateway/               FastAPI gateway, Python 3.12
│   ├── Dockerfile, requirements.txt                                         (01)
│   ├── alembic.ini, alembic/env.py, alembic/versions/0001_initial.py        (03)
│   └── app/
│       ├── config.py, db.py, models.py, schemas.py                          (01)
│       ├── kaspa_engine.py, oracle.py                                       (02)
│       ├── security.py, proxy.py, api_routes.py, main.py                    (03)
├── apps/web/                   Next.js 15 App Router + TS + Tailwind, Dockerfile (04)
├── packages/buyer-sdk/         pyproject.toml, kasgate_client/              (05)
├── packages/mcp-server/        pyproject.toml, kasgate_mcp/                 (05)
└── tests/integration/          pytest suite against http://localhost:8000   (05)
```

## Protocol constants (app/config.py)
SOMPI_PER_KAS=100_000_000, PLATFORM_FEE_BPS=150, BPS_DENOMINATOR=10_000, X402_VERSION=2,
X402_SCHEME="kaspa-exact-v1", X402_ASSET="KAS", MIN_OUTPUT_SOMPI default 20_000_000 (0.1 KAS outputs breach the 100,000 standard storage mass).
Network prefixes: kaspa:testnet-10 -> "kaspatest:", kaspa:mainnet -> "kaspa:".

## Fee split (schemas.compute_fee_split)
fee = max(ceil(total * 150 / 10_000), MIN_OUTPUT_SOMPI); merchant = total - fee, must be >= MIN_OUTPUT_SOMPI.
Minimum endpoint price = 2 * MIN_OUTPUT_SOMPI = 40000000 sompi (0.4 KAS) at the default floor — schemas.min_price_sompi(). Flat 0.2 KAS fee up to 1333333333 sompi, 1.5% (ceil) above.
On-chain: outputs[0] = merchant (payTo) EXACT merchant amount, outputs[1] = treasury (feeTo) EXACT fee.
outputs[2+] allowed only as change. Generator/create_transaction keep payment outputs first.

## x402 headers (schemas.py) — Base64(JSON), standard alphabet, padded
PAYMENT-REQUIRED  -> PaymentRequired{x402Version:2, scheme, network, asset:"KAS", amount:str(TOTAL sompi), payTo, platformFee{feeTo, feeAmount:str}}
                     build with PaymentRequired.for_endpoint(price_sompi, pay_to); merchant part = .merchant_amount_sompi
PAYMENT-SIGNATURE -> PaymentSignature{txId: 64 lowercase hex, rawTx: json.dumps(kaspa.Transaction.to_dict()) of SIGNED tx, payerAddress}
PAYMENT-RESPONSE  -> PaymentResponse{txId, settled:true, network, usdValueAtSettlement: decimal string, 8 dp}
Every model has .to_header() and Cls.from_header(value) (raises ValueError).
Error JSON body: X402ErrorBody{error, message, accepts: PaymentRequired|null}
error codes: payment_required, invalid_payment_header, payment_replayed, underpaid, invalid_fee_split,
wrong_network, tx_rejected, rpc_unavailable, service_unavailable, rate_limited, endpoint_not_found, method_not_allowed, upstream_error

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

## Gateway routes (03 implements, 04/05 consume)
GET  /healthz                         -> HealthStatus
ANY  /p/{slug} and /p/{slug}/{path:path}  paywalled proxy (PROXY_PATH_PREFIX="/p")
POST /v1/merchants                    MerchantCreate{kaspa_address, display_name, email, challenge(purpose=register), public_key, signature} -> 201 MerchantCreated{merchant, api_key (once), session_token kgs_, expires_at}
GET  /v1/merchants/me                 -> MerchantRead            [Bearer]
POST /v1/endpoints                    EndpointCreate -> 201 EndpointRead   [Bearer]
GET  /v1/endpoints                    -> list[EndpointRead]       [Bearer]
GET  /v1/endpoints/{endpoint_id}      -> EndpointRead             [Bearer]
PATCH /v1/endpoints/{endpoint_id}     EndpointUpdate -> EndpointRead [Bearer]
DELETE /v1/endpoints/{endpoint_id}    -> 204 (sets is_active=false) [Bearer]
GET  /v1/settlements?limit=50&offset=0          -> list[SettlementRead] [Bearer]
GET  /v1/analytics?start=ISO&end=ISO            -> MerchantAnalytics    [Bearer]
GET  /v1/tax-report.csv?start=ISO&end=ISO       -> text/csv, header row = TAX_CSV_COLUMNS [Bearer]
GET  /v1/directory                              -> DirectoryManifest (public, cached 30 s)
GET  /v1/quote/{slug}                           -> PaymentRequired JSON (public, for agents/SDK)
Auth: "Authorization: Bearer kg_<token_urlsafe(32)>"; DB stores sha256 hex in Merchant.api_key_hash.
Slug auto-generated when omitted: slugify(name)[:55] + "-" + 6 hex chars.

## Engine interface (02 implements in app/kaspa_engine.py, 03 calls)
class PaymentVerificationError(Exception): code: str (one of the error codes above), message: str
@dataclass(frozen=True) class VerifiedPayment: tx_id, payer_address, total_sompi, merchant_sompi, fee_sompi
class KaspaEngine:
    def __init__(self, settings: Settings)
    async def start() / stop() / is_connected() -> bool
    async def verify_and_submit(signature: PaymentSignature, required: PaymentRequired) -> VerifiedPayment
module singleton: engine = KaspaEngine(get_settings())

## Oracle interface (02 implements in app/oracle.py)
class KasUsdOracle: __init__(settings, redis: redis.asyncio.Redis); async get_rate() -> Decimal (8 dp)
Redis keys: kasgate:oracle:kas_usd (TTL 60 s), kasgate:oracle:kas_usd:last (no TTL, fallback)
usd_value = (Decimal(total_sompi) / SOMPI_PER_KAS * rate).quantize(Decimal("0.00000001"))

## Redis keys
kasgate:tx:{txId}  SET NX EX 600 replay lock (03)

## Merchant login / key recovery (wallet-signature, non-custodial) — added after 04 review
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

## CONTRACT v2 — security revision after 05 red-team (supersedes earlier lines where they conflict)
Payment binding (GAP-1, GAP-7, GAP-6 TOCTOU):
- PaymentRequired gains `nonce` (32 lowercase hex, schemas.new_payment_nonce()) and `expiresAt` (UTC, now + PAYMENT_MAX_AGE_SECONDS).
  Build: PaymentRequired.for_endpoint(price_sompi, pay_to, nonce, expires_at).
- Gateway stores the exact issued challenge: Redis `kasgate:nonce:{nonce}` = PaymentRequired JSON + endpoint slug, EX payment_max_age_seconds.
- PaymentSignature gains `nonce`. The signed tx `payload` MUST equal schemas.payment_payload(nonce) = b"kasgate:v1:" + nonce.
- On the paid retry the gateway loads `required` FROM THE NONCE (never recomputes), checks slug matches, consumes it atomically (GETDEL) only after
  successful settlement. Missing/expired nonce -> 402 `payment_expired` with a fresh PAYMENT-REQUIRED.
- Settlement.payment_nonce is UNIQUE. "Already in mempool/accepted" is success ONLY when the payload nonce is valid and unconsumed.
Pre-lock verification (GAP-11): run engine.verify() (network-free, finalize()-recomputed id, payload, outputs) BEFORE taking kasgate:tx:{txId}.
Settlement finality (GAP-2): settings.settlement_mode "accepted" (default) = submit, then wait until the tx is accepted by the virtual chain
  (subscribe_virtual_chain_changed or poll), up to settlement_timeout_seconds (8 s); timeout -> 503 rpc_unavailable, lock released, nonce kept.
  "mempool" = old behaviour, testnet demos only. Reject any replacement (submit_transaction_replacement is never used by the gateway).
Inputs (GAP-4): engine fetches each input's real UTXO entry from the node (get_utxos_by_addresses(payerAddress) + mempool) and uses node values/scripts
  for mass and for payer attribution; mismatch -> invalid_payment_header.
Redelivery (GAP-5): same txId + same nonce + same slug, Settlement exists, upstream_status_code is NULL or >= 500, within redelivery_window_seconds and
  delivery_attempts < redelivery_max_attempts -> re-forward upstream, increment delivery_attempts, return PAYMENT-RESPONSE. Otherwise 409.
Transport (GAP-6): PUBLIC_BASE_URL must be https on mainnet (enforced in Settings). SDK keeps pinned fee_to/expected_pay_to options.
Headers (GAP-8): rawTx max 24_000 chars, MAX_HEADER_BYTES 40_960; uvicorn runs with --h11-max-incomplete-event-size 65536.
SSRF (GAP-9): schema rejects any literal IP that is not ip.is_global (covers 100.64.0.0/10, ::1, ULA) and *.localhost/*.internal.
  Runtime guard (03) resolves DNS at request time, pins the resolved IP for the connection, rejects non-global IPs (nip.io, localtest.me), no redirects.
Addresses (GAP-10): validate_kaspa_address now checks the bech32 checksum via kaspa.Address.validate.
Auth (GAP-12/13/14):
- Challenge text = schemas.build_auth_challenge(purpose, domain=host of PUBLIC_BASE_URL, address, nonce, expires_at); purposes AUTH_PURPOSE / REGISTER_PURPOSE.
- AuthChallengeRequest gains purpose "login" | "register". Redis key kasgate:auth:{purpose}:{addr}:{nonce}.
- POST /v1/merchants now REQUIRES challenge + public_key + signature (purpose=register) -> proves address ownership.
- POST /v1/auth/verify now returns SessionIssued{merchant, session_token "kgs_...", expires_at} and DOES NOT rotate api_key.
  Sessions: Redis kasgate:session:{sha256(token)} -> merchant_id, EX session_ttl_seconds. Bearer accepts kg_ (api key) or kgs_ (session).
  Only POST /v1/merchants/me/rotate-key rotates the api key. Web dashboard cookie holds the session_token.
- Challenge rate limit is per-IP only (per-address limit removed: it allowed victim lockout).
Amounts (GAP-15): SompiStr max 18 digits. /healthz stays public but only returns status+network to unauthenticated callers is NOT required for MVP.
Error codes: + payment_expired (402).

## CONTRACT v2.1 — fee floor (Kyle approved 2026-11-20)
- Treasury fee = max(ceil(total*150/10000), MIN_OUTPUT_SOMPI). Merchant = total - fee, must be >= MIN_OUTPUT_SOMPI.
- min_price_sompi(floor) = 2*floor = 40_000_000 sompi (0.4 KAS) at the default 20_000_000 floor. Replaces 1_333_333_267 everywhere.
- Below 1_333_333_334 sompi the fee is the flat floor (0.2 KAS); above that it is 1.5% (ceil).
- Engine: exact-value checks against PaymentRequired still apply; the ">= 1.5%" check stays valid because fee >= 1.5% always.
- Clients (web lib/sompi.ts, buyer SDK) must replicate: fee = max(ceil(amount*150/10000), min_output_sompi) using DirectoryManifest.min_output_sompi.

## CONTRACT v2.2 — answers to 04 (2026-11-20)
- Registration and rotate-key return MerchantCreated WITH a session (session_token kgs_ + expires_at), so the web app never stores a kg_ key in a cookie.
- Challenge "Domain:" = urlsplit(PUBLIC_BASE_URL).netloc lowercased (host, plus :port only when the URL has one). Exactly one form; clients compare exact string.
  The web app's public URL that users see must therefore equal PUBLIC_BASE_URL's netloc (in local dev: localhost:8000 vs the web on :3000 → web compares with NEXT_PUBLIC_GATEWAY_URL netloc, which must equal PUBLIC_BASE_URL).
- SompiStr 18-digit cap stands (max 999_999_999_999_999_999 sompi ≫ total KAS supply).
- KasWare signature format is verified live in Phase 4; gateway normalisation (hex128 / Base64 64|65 bytes) is the contract.

## CONTRACT v2.3 — storage-mass correction to the minimum price (2026-11-20, from 02's live testnet finding)
- At 0.4 KAS the two 0.2 KAS outputs use the entire 100_000 standard storage mass; any change output makes the tx non-standard.
- Merchant output must be >= 2*MIN_OUTPUT_SOMPI. min_price_sompi(floor) = 3*floor = 60_000_000 sompi (0.6 KAS). Fee rule unchanged.
- Verified with kaspa.calculate_storage_mass: at >= 0.6 KAS a single-input payment with change >= ~0.3 KAS stays <= 100_000.
- Buyer SDK MUST select inputs so change is 0 or >= 30_000_000 sompi (add another UTXO when needed); never emit small change.
- Payments drawing from more than 16 payer addresses' UTXOs are rejected (invalid_payment_header).
