/**
 * TypeScript mirror of apps/gateway/app/schemas.py (source of truth: docs/CONTRACTS.md).
 *
 * Conventions
 * - Field names are identical to the Python schemas (snake_case). The x402 header models keep camelCase.
 * - Pydantic `Decimal` values are serialised by the gateway as JSON strings -> `DecimalString`.
 * - `datetime` / `date` values are ISO-8601 strings -> `IsoDateTime` / `IsoDate`.
 * - Sompi amounts can reach 2^63-1, beyond Number.MAX_SAFE_INTEGER. Every `*_sompi` integer returned by the
 *   gateway is normalised to a decimal string by `lib/safe-json.ts` before it reaches application code, so all
 *   `Sompi` fields below are strings. Do arithmetic on them with BigInt only (see `lib/sompi.ts`).
 */

export type Uuid = string;
export type IsoDateTime = string;
export type IsoDate = string;
export type DecimalString = string;
/** Decimal string of an integer number of sompi (1 KAS = 100,000,000 sompi). Matches SOMPI_STR_RE. */
export type Sompi = string;

export type KaspaNetwork = "kaspa:testnet-10" | "kaspa:mainnet";
export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
export type SettlementStatus = "pending" | "settled" | "rejected";

export const HTTP_METHODS: readonly HttpMethod[] = ["GET", "POST", "PUT", "PATCH", "DELETE"];

export const X402_ERROR_CODES = [
  "payment_required",
  "invalid_payment_header",
  "payment_replayed",
  "underpaid",
  "invalid_fee_split",
  "wrong_network",
  "tx_rejected",
  "rpc_unavailable",
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
}

export interface PaymentSignature {
  txId: string;
  rawTx: string;
  payerAddress: string;
}

export interface PaymentResponse {
  txId: string;
  settled: true;
  network: KaspaNetwork;
  usdValueAtSettlement: DecimalString;
}

export interface X402ErrorBody {
  error: X402ErrorCode;
  message: string;
  accepts: PaymentRequired | null;
}

/* ------------------------------------------------------------------ fee split */

export interface FeeSplit {
  total_sompi: Sompi;
  merchant_sompi: Sompi;
  fee_sompi: Sompi;
}

/* ------------------------------------------------------------------ merchants */

export interface MerchantCreate {
  kaspa_address: string;
  display_name: string;
  email?: string | null;
}

export interface MerchantRead {
  id: Uuid;
  kaspa_address: string;
  display_name: string;
  email: string | null;
  is_active: boolean;
  created_at: IsoDateTime;
}

/** `api_key` is consumed only by the Next route handler; it is never sent to the browser. */
export interface MerchantCreated {
  merchant: MerchantRead;
  api_key: string;
}

/** What the browser receives from POST /api/merchants (no api_key). */
export interface MerchantRegistered {
  merchant: MerchantRead;
}

/* ------------------------------------------------------------------ wallet-signature login */

export interface AuthChallengeRequest {
  kaspa_address: string;
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
  /** 128 lowercase hex characters. */
  signature: string;
}

/* ------------------------------------------------------------------ endpoints */

/** Body accepted by the Next route handler; `price_sompi` is a decimal string, converted to a JSON integer server-side. */
export interface EndpointCreate {
  name: string;
  slug?: string | null;
  description?: string | null;
  category?: string | null;
  upstream_url: string;
  upstream_headers?: Record<string, string>;
  price_sompi: Sompi;
  allowed_methods?: HttpMethod[];
  is_public_listed?: boolean;
}

export interface EndpointUpdate {
  name?: string | null;
  description?: string | null;
  category?: string | null;
  upstream_url?: string | null;
  upstream_headers?: Record<string, string> | null;
  price_sompi?: Sompi | null;
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
  price_sompi: Sompi;
  allowed_methods: string[];
  is_active: boolean;
  is_public_listed: boolean;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
  /** computed_field */
  price_kas: string;
  /** computed_field: `${PUBLIC_BASE_URL}/p/${slug}` */
  proxy_url: string;
}

/* ------------------------------------------------------------------ analytics & tax */

export interface SettlementRead {
  id: Uuid;
  tx_id: string;
  endpoint_id: Uuid;
  merchant_id: Uuid;
  network: string;
  payer_address: string;
  merchant_address: string;
  treasury_address: string;
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
  payer_address: string;
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
  price_sompi: Sompi;
  price_kas: string;
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
}
