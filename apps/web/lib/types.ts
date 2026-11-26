/**
 * TypeScript mirror of apps/gateway/app/schemas.py (source of truth: docs/CONTRACTS.md).
 *
 * Conventions
 * - Field names are identical to the Python schemas (snake_case). The x402 header models keep camelCase.
 * - Pydantic `Decimal` values are serialised by the gateway as JSON strings -> `DecimalString`.
 * - `datetime` / `date` values are ISO-8601 strings -> `IsoDateTime` / `IsoDate`.
 * - Sompi amounts can reach 2^63-1, beyond Number.MAX_SAFE_INTEGER. Every `*_sompi` and `*_usd_micros` integer
 *   returned by the gateway is normalised to a decimal string by `lib/safe-json.ts` before it reaches application
 *   code, so all `Sompi` and `UsdMicros` fields below are strings. Do arithmetic on them with BigInt only
 *   (see `lib/sompi.ts` and `lib/format.ts`).
 */

export type Uuid = string;
export type IsoDateTime = string;
export type IsoDate = string;
export type DecimalString = string;
/** Decimal string of an integer number of sompi (1 KAS = 100,000,000 sompi). Matches SOMPI_STR_RE. */
export type Sompi = string;
/** Decimal string of an integer number of micro-dollars (1 USD = 1,000,000 micros). Matches UsdMicros, max $1e9. */
export type UsdMicros = string;

export type KaspaNetwork = "kaspa:testnet-10" | "kaspa:mainnet";
export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
export type PriceCurrency = "KAS" | "USD";
export type SettlementStatus = "pending" | "settled" | "rejected";

export const HTTP_METHODS: readonly HttpMethod[] = ["GET", "POST", "PUT", "PATCH", "DELETE"];
export const PRICE_CURRENCIES: readonly PriceCurrency[] = ["KAS", "USD"];

export const X402_ERROR_CODES = [
  "payment_required",
  "invalid_payment_header",
  "payment_replayed",
  "payment_expired",
  "payment_not_found",
  "underpaid",
  "invalid_fee_split",
  "wrong_network",
  "tx_rejected",
  "rpc_unavailable",
  "service_unavailable",
  "rate_limited",
  "endpoint_not_found",
  "method_not_allowed",
  "upstream_error",
] as const;
export type X402ErrorCode = (typeof X402_ERROR_CODES)[number];

/* ------------------------------------------------------------------ x402 headers (camelCase) */

export interface PlatformFee {
  feeTo: string;
  feeAmount: Sompi;
}

export interface PaymentRequired {
  x402Version: 2;
  scheme: "kaspa-exact-v1";
  network: KaspaNetwork;
  asset: "KAS";
  /** TOTAL sompi the payer sends across both outputs. */
  amount: Sompi;
  payTo: string;
  platformFee: PlatformFee;
  /** 32 lowercase hex. The signed transaction payload must equal `kasgate:v1:` + nonce (ASCII). */
  nonce: string;
  /** UTC ISO-8601; after this the nonce is gone and the gateway answers 402 `payment_expired`. */
  expiresAt: IsoDateTime;
}

export interface PaymentSignature {
  txId: string;
  rawTx: string;
  payerAddress: string;
  nonce: string;
}

export interface PaymentResponse {
  txId: string;
  settled: true;
  network: KaspaNetwork;
  usdValueAtSettlement: DecimalString;
}

/** k402 `kaspa-utxo` offer (CONTRACT v2.9): `amount_sompi` is the merchant output, `facilitator_fee` the treasury. */
export interface K402UtxoOffer {
  scheme: "kaspa-utxo";
  network: "testnet-10" | "mainnet";
  amount_sompi: Sompi;
  pay_to: string;
  payment_id: string;
  expires: number;
  description: string;
  facilitator_fee: { sompi: Sompi; to: string; by: "kasgate" };
}

/** On a 402, `k402` is "0.2" and `accepts` lists the v1 terms (also in PAYMENT-REQUIRED) then the k402 offer. */
export interface X402ErrorBody {
  error: X402ErrorCode;
  message: string;
  k402: "0.2" | null;
  accepts: Array<PaymentRequired | K402UtxoOffer> | null;
}

/* ------------------------------------------------------------------ fee split */

export interface FeeSplit {
  total_sompi: Sompi;
  merchant_sompi: Sompi;
  fee_sompi: Sompi;
}

/* ------------------------------------------------------------------ merchants */

/** Registration requires a wallet signature over a purpose="register" challenge (CONTRACT v2). */
export interface MerchantCreate {
  kaspa_address: string;
  display_name: string;
  email?: string | null;
  challenge: string;
  public_key: string;
  /** Sent exactly as the wallet returned it; the gateway normalises hex / Base64. */
  signature: string;
}

export interface MerchantRead {
  id: Uuid;
  kaspa_address: string;
  display_name: string;
  email: string | null;
  is_active: boolean;
  created_at: IsoDateTime;
}

/**
 * Returned by POST /v1/merchants and POST /v1/merchants/me/rotate-key (CONTRACT v2.2). `api_key` (`kg_…`) is the
 * long-lived automation key, shown to the user exactly once. `session_token` (`kgs_…`) is the dashboard session that
 * goes into the httpOnly cookie; `expires_at` is its expiry. Only the Next route handler ever sees `session_token`.
 */
export interface MerchantCreated {
  merchant: MerchantRead;
  api_key: string;
  session_token: string;
  expires_at: IsoDateTime;
}

/** What the browser receives after register / rotate: the key to show once, never the session token. */
export interface MerchantKeyIssued {
  merchant: MerchantRead;
  api_key: string;
  expires_at: IsoDateTime;
}

/** Wallet login result from the gateway. The session_token is consumed only by the Next route handler. */
export interface SessionIssued {
  merchant: MerchantRead;
  /** `kgs_…`, does NOT rotate the api_key. */
  session_token: string;
  expires_at: IsoDateTime;
}

/** What the browser receives after wallet login: no credentials. */
export interface SessionStarted {
  merchant: MerchantRead;
  expires_at: IsoDateTime | null;
}

/* ------------------------------------------------------------------ wallet-signature login */

export type AuthPurpose = "login" | "register";

export interface AuthChallengeRequest {
  kaspa_address: string;
  purpose: AuthPurpose;
}

export interface AuthChallenge {
  kaspa_address: string;
  /** Exact message the wallet must sign. */
  challenge: string;
  expires_at: IsoDateTime;
}

export interface AuthVerifyRequest {
  kaspa_address: string;
  challenge: string;
  /** 64 hex (x-only) or 66 hex (02/03-prefixed compressed). */
  public_key: string;
  /** Sent as-is from the wallet (128-hex Schnorr, or Base64 of 64/65 bytes); the gateway normalises. */
  signature: string;
}

/* ------------------------------------------------------------------ endpoints */

/**
 * Body accepted by the Next route handler. Sompi and micro-dollar amounts are decimal strings, converted to
 * JSON integers server-side. KAS: send `price_sompi` and omit `price_usd_micros`. USD: send `price_usd_micros`
 * and omit `price_sompi`. `price_currency` defaults to KAS.
 */
export interface EndpointCreate {
  name: string;
  slug?: string | null;
  description?: string | null;
  category?: string | null;
  upstream_url: string;
  upstream_headers?: Record<string, string>;
  price_currency?: PriceCurrency;
  price_sompi?: Sompi;
  price_usd_micros?: UsdMicros;
  allowed_methods?: HttpMethod[];
  is_public_listed?: boolean;
}

export interface EndpointUpdate {
  name?: string | null;
  description?: string | null;
  category?: string | null;
  upstream_url?: string | null;
  upstream_headers?: Record<string, string> | null;
  price_currency?: PriceCurrency | null;
  price_sompi?: Sompi | null;
  price_usd_micros?: UsdMicros | null;
  allowed_methods?: HttpMethod[] | null;
  is_active?: boolean | null;
  is_public_listed?: boolean | null;
}

export interface EndpointRead {
  id: Uuid;
  merchant_id: Uuid;
  slug: string;
  name: string;
  description: string | null;
  category: string | null;
  upstream_url: string;
  price_currency: PriceCurrency;
  price_sompi: Sompi | null;
  price_usd_micros: UsdMicros | null;
  allowed_methods: string[];
  is_active: boolean;
  is_public_listed: boolean;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
  /** computed_field; null when price_currency is USD. */
  price_kas: string | null;
  /** computed_field; null when price_currency is KAS. */
  price_usd: string | null;
  /** computed_field: `${PUBLIC_BASE_URL}/p/${slug}` */
  proxy_url: string;
}

/* ------------------------------------------------------------------ OpenAPI import (POST /v1/endpoints/import) */

export type OpenApiImportMode = "preview" | "create";

/**
 * One operation to create, addressed by method + path exactly as in the document. A row override is in the
 * import's default_price_currency: `price_sompi` for KAS, `price_usd_micros` for USD.
 */
export interface OpenApiImportSelection {
  method: HttpMethod;
  path: string;
  price_sompi?: Sompi;
  price_usd_micros?: UsdMicros;
}

/**
 * Body accepted by POST /api/endpoints/import. Exactly one of `spec_text` / `spec_url`; sompi and micro-dollar
 * values are decimal strings. KAS (the default): `default_price_sompi` and no USD fields. USD:
 * `default_price_currency: "USD"`, `default_price_usd_micros`, and no sompi fields.
 */
export interface OpenApiImportBody {
  mode: OpenApiImportMode;
  spec_text?: string;
  spec_url?: string;
  upstream_base_url: string;
  default_price_currency?: PriceCurrency;
  default_price_sompi?: Sompi;
  default_price_usd_micros?: UsdMicros;
  upstream_headers?: Record<string, string>;
  category?: string;
  is_public_listed?: boolean;
  selections?: OpenApiImportSelection[];
}

export interface OpenApiOperationPreview {
  /** Stable operation key: "<METHOD> <path>". */
  key: string;
  method: string;
  path: string;
  operation_id: string | null;
  summary: string | null;
  path_params: string[];
  importable: boolean;
  skip_reason: string | null;
  slug: string | null;
  upstream_url: string | null;
  /** Buyer route, e.g. "/p/list-pets/{petId}": path parameters become the proxy path suffix. */
  proxy_path: string | null;
  proxy_url: string | null;
  price_currency: PriceCurrency;
  /** Default KAS price; null when price_currency is USD. */
  price_sompi: Sompi | null;
  /** Default USD price in micro-dollars; null when price_currency is KAS. */
  price_usd_micros: UsdMicros | null;
}

export interface OpenApiImportPreview {
  mode: "preview";
  openapi_version: string;
  title: string | null;
  api_version: string | null;
  servers: string[];
  operation_count: number;
  importable_count: number;
  default_price_currency: PriceCurrency;
  default_price_sompi: Sompi | null;
  default_price_usd_micros: UsdMicros | null;
  min_price_sompi: Sompi;
  operations: OpenApiOperationPreview[];
}

export interface OpenApiImportCreated {
  mode: "create";
  created_count: number;
  created: EndpointRead[];
}

/* ------------------------------------------------------------------ analytics & tax */

export interface SettlementRead {
  id: Uuid;
  tx_id: string;
  endpoint_id: Uuid;
  merchant_id: Uuid;
  network: string;
  /** "k402-kaspa-utxo" payments carry the same treasury output but prove no payer, so payer_address is null.
   *  "kaspa-exact-v2" (community kaspa-x402 `exact`, CONTRACT v2.10) pays the merchant the full price: no platform
   *  fee during the beta, so fee_amount_sompi is 0 and treasury_address / payment_nonce are null. */
  payment_scheme: "kaspa-exact-v1" | "k402-kaspa-utxo" | "kaspa-exact-v2";
  payer_address: string | null;
  merchant_address: string;
  treasury_address: string | null;
  amount_sompi: Sompi;
  merchant_amount_sompi: Sompi;
  fee_amount_sompi: Sompi;
  kas_usd_rate: DecimalString | null;
  usd_value_at_settlement: DecimalString | null;
  status: SettlementStatus;
  failure_reason: string | null;
  request_method: string;
  request_path: string;
  upstream_status_code: number | null;
  payment_nonce: string | null;
  /** kaspa-exact-v2 payment-identifier (idempotency key); null on the other schemes. */
  payment_identifier: string | null;
  created_at: IsoDateTime;
  settled_at: IsoDateTime | null;
}

export interface DailyRevenue {
  day: IsoDate;
  call_count: number;
  gross_sompi: Sompi;
  merchant_sompi: Sompi;
  fee_sompi: Sompi;
  usd_value: DecimalString;
}

export interface EndpointRevenue {
  endpoint_id: Uuid;
  slug: string;
  name: string;
  call_count: number;
  merchant_sompi: Sompi;
  usd_value: DecimalString;
}

export interface MerchantAnalytics {
  merchant_id: Uuid;
  network: KaspaNetwork;
  period_start: IsoDateTime;
  period_end: IsoDateTime;
  total_calls: number;
  gross_sompi: Sompi;
  merchant_sompi: Sompi;
  fee_sompi: Sompi;
  usd_value: DecimalString;
  daily: DailyRevenue[];
  by_endpoint: EndpointRevenue[];
}

export const TAX_CSV_COLUMNS = [
  "settled_at_utc",
  "tx_id",
  "network",
  "endpoint_slug",
  "payer_address",
  "gross_kas",
  "merchant_kas",
  "platform_fee_kas",
  "kas_usd_rate",
  "usd_value_at_settlement",
] as const;

export interface TaxReportRow {
  settled_at_utc: IsoDateTime;
  tx_id: string;
  network: string;
  endpoint_slug: string;
  payer_address: string | null;
  gross_kas: string;
  merchant_kas: string;
  platform_fee_kas: string;
  kas_usd_rate: DecimalString | null;
  usd_value_at_settlement: DecimalString | null;
}

/* ------------------------------------------------------------------ public directory */

export interface DirectoryEntry {
  slug: string;
  name: string;
  description: string | null;
  category: string | null;
  merchant_display_name: string;
  pay_to: string;
  price_currency: PriceCurrency;
  price_sompi: Sompi | null;
  price_kas: string | null;
  price_usd_micros: UsdMicros | null;
  price_usd: string | null;
  allowed_methods: string[];
  proxy_url: string;
}

export interface DirectoryManifest {
  manifest_version: 1;
  x402_version: 2;
  scheme: "kaspa-exact-v1";
  asset: "KAS";
  network: KaspaNetwork;
  gateway_base_url: string;
  platform_fee_bps: number;
  min_output_sompi: Sompi;
  generated_at: IsoDateTime;
  endpoints: DirectoryEntry[];
}

/** Public pricing parameters served by GET /api/pricing (derived from the gateway directory manifest). */
export interface PricingInfo {
  min_output_sompi: Sompi;
  min_price_sompi: Sompi;
  platform_fee_bps: number;
}

export interface HealthStatus {
  status: "ok" | "degraded";
  network: KaspaNetwork;
  database: boolean;
  redis: boolean;
  kaspa_rpc: boolean;
}

/* ------------------------------------------------------------------ web-app-only shapes */

/** Error body returned by this app's route handlers. */
export interface ApiErrorBody {
  error: string;
  /** Machine-readable X402ErrorCode from the gateway when available (e.g. "rpc_unavailable" for HTTP 503). */
  code?: string;
  /** POST /api/endpoints/import only: per-selection errors keyed by the selection's index in the request. */
  row_errors?: Record<string, string>;
}
