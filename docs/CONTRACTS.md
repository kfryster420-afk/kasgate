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
fee = ceil(total * 150 / 10_000); merchant = total - fee. Both must be >= MIN_OUTPUT_SOMPI.
Minimum endpoint price at default floor = 1333333267 sompi (~13.33 KAS); fee uses CEIL rounding — schemas.min_price_sompi().
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
wrong_network, tx_rejected, rpc_unavailable, endpoint_not_found, method_not_allowed, upstream_error

## HTTP status mapping
no header -> 402 + PAYMENT-REQUIRED + body error=payment_required
bad header / underpaid / invalid_fee_split / wrong_network / tx_rejected -> 402 + PAYMENT-REQUIRED + body
replayed txId (Redis lock held or tx_id already in settlements) -> 409 error=payment_replayed
unknown/inactive slug -> 404 endpoint_not_found; method not in allowed_methods -> 405 method_not_allowed
rpc_unavailable (RpcUnavailableError) -> 503, release the Redis replay lock
header that fails schema validation on network/address prefix -> 402 wrong_network
upstream failure after settlement -> 502 upstream_error, PAYMENT-RESPONSE still set, settlement stays SETTLED

## Gateway routes (03 implements, 04/05 consume)
GET  /healthz                         -> HealthStatus
ANY  /p/{slug} and /p/{slug}/{path:path}  paywalled proxy (PROXY_PATH_PREFIX="/p")
POST /v1/merchants                    MerchantCreate -> 201 MerchantCreated (api_key shown once)
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
POST /v1/auth/verify      AuthVerifyRequest{kaspa_address, challenge, public_key, signature} -> 200 MerchantCreated (NEW rotated api_key)
                          verify: kaspa.verify_message(challenge, signature, public_key) AND PublicKey(public_key).to_address(network) == kaspa_address
                          challenge ALWAYS 200 (even for unknown addresses, to prevent enumeration); verify -> 401 for unknown address,
                          bad/expired/used challenge or bad signature (one generic message)
                          signature format: 128 lowercase hex Schnorr from kaspa.sign_message (KasWare signMessage(msg,{type:'schnorr'}));
                          server also accepts Base64 of 64 or 65 bytes (65 -> drop the first header byte) and normalises to hex
                          public_key: 66-hex compressed or 64-hex x-only; both verified OK with kaspa==2.1.0
POST /v1/merchants/me/rotate-key  [Bearer] -> MerchantCreated (old key invalid immediately)
POST /v1/merchants duplicate kaspa_address or email -> 409 {"detail": "..."}
GET /v1/settlements: limit 1..200 (default 50), offset >= 0
FastAPI validation errors stay as {"detail": [...]}; x402 errors use X402ErrorBody.
Proxy path is /p/{slug} (never /v1/{slug}); quotes /v1/quote/{slug}.
Pricing: fixed sompi only for the MVP (no USD-pegged pricing).
