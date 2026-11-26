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
  "price_unavailable",
  "endpoint_not_found",
  "method_not_allowed",
  "upstream_error",
  "invalid_exact_payment",
  "insufficient_balance",
  "trial_exhausted",
  "invalid_payer",
  "invalid_signature",
  "invalid_status",
  "challenge_expired",
  "challenge_replayed",
  "refund_too_small",
  "refund_already_open",
  "refund_not_found",
  "refund_not_cancellable",
  "refund_not_claimable",
  "refund_not_rejectable",
  "refund_closed",
  "invalid_payout",
  "payout_replayed",
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
  accepts: Array<PaymentRequired | K402UtxoOffer | PrepaidOffer> | null;
}

/* ------------------------------------------------------------------ prepaid credit (CONTRACT v2.11) */

export const PREPAID_SCHEME = "kasgate-prepaid-v1";
/** Request header: Base64(JSON {nonce, signature}) over the challenge message from POST /v1/prepaid/challenge. */
export const HEADER_PREPAID_DEBIT = "X-KasGate-Prepaid";
/** Response header: Base64(JSON PrepaidReceipt). */
export const HEADER_PREPAID_RECEIPT = "X-KasGate-Prepaid-Receipt";
export type PrepaidDebitStatus = "debited" | "refunded";

/** The `kasgate-prepaid-v1` entry of a 402 `accepts` list; prepaid-only endpoints list nothing else. */
export interface PrepaidOffer {
  scheme: typeof PREPAID_SCHEME;
  network: KaspaNetwork;
  asset: "KAS";
  /** Per-call debit in sompi (may be below the on-chain minimum when `prepaidOnly`). */
  amount: Sompi;
  payTo: string;
  merchantId: Uuid;
  prepaidOnly: boolean;
  minDepositSompi: Sompi;
  depositQuoteUrl: string;
  depositUrl: string;
  challengeUrl: string;
  balanceUrl: string;
}

export interface PrepaidReceipt {
  debitId: Uuid;
  nonce: string;
  amountSompi: Sompi;
  balanceSompi: Sompi;
  status: PrepaidDebitStatus;
}

/** GET /v1/prepaid/balances (merchant view) and the public GET /v1/prepaid/balance. */
export interface PrepaidBalanceRead {
  merchant_id: Uuid;
  payer_address: string;
  balance_sompi: Sompi;
  deposited_sompi: Sompi;
  debited_sompi: Sompi;
  updated_at: IsoDateTime | null;
  /** CONTRACT v2.13: paid back on-chain by refund payouts. */
  refunded_sompi: Sompi;
  /** CONTRACT v2.13: swept by the credit-expiry worker. */
  expired_sompi: Sompi;
  /** CONTRACT v2.13: set aside by an open refund request; still in the balance but not spendable. */
  refund_hold_sompi: Sompi;
  /** CONTRACT v2.13: balance - refund hold, or 0 once `expires_at` has passed. */
  spendable_sompi: Sompi;
  /** CONTRACT v2.13: when the credit expires (null = never). */
  expires_at: IsoDateTime | null;
  /** CONTRACT v2.14: merchant shares of failed paid calls ever credited onto this balance. */
  credited_sompi: Sompi;
  /** CONTRACT v2.14: unspent failed-delivery credit inside the balance; spent first by calls, never refundable. */
  credit_balance_sompi: Sompi;
  /** CONTRACT v2.14: what a refund request may take now (spendable minus unspent failed-delivery credit). */
  refundable_sompi: Sompi;
}

export interface PrepaidDepositRead {
  id: Uuid;
  tx_id: string;
  settlement_id: Uuid;
  payer_address: string;
  /** Total of both outputs; `credit_sompi` (output 0, merchant) + `fee_sompi` (output 1, treasury). */
  deposit_sompi: Sompi;
  fee_sompi: Sompi;
  credit_sompi: Sompi;
  created_at: IsoDateTime;
}

export interface PrepaidDebitRead {
  id: Uuid;
  endpoint_id: Uuid;
  payer_address: string;
  nonce: string;
  amount_sompi: Sompi;
  kas_usd_rate: DecimalString | null;
  usd_value: DecimalString | null;
  status: PrepaidDebitStatus;
  request_method: string;
  request_path: string;
  upstream_status_code: number | null;
  created_at: IsoDateTime;
  refunded_at: IsoDateTime | null;
}

export type PrepaidLedgerKind = "balances" | "deposits" | "debits";

/* ------------------------------------------------------------------ prepaid refunds and credit expiry (CONTRACT v2.13) */

/** models.PREPAID_EXPIRY_DAYS_MAX: credit expiry is 1..3650 days after the latest deposit (null = never / merchant default). */
export const PREPAID_EXPIRY_DAYS_MAX = 3650;
/** schemas.PREPAID_REFUND_PURPOSE: the `Purpose:` line of a refund challenge. */
export const PREPAID_REFUND_PURPOSE = "kasgate:v1:prepaid-refund";
/** schemas.PREPAID_REFUND_PAYLOAD_PREFIX: payload of a merchant-built payout (`kasgate:v1:refund:<refund id hex>`). */
export const PREPAID_REFUND_PAYLOAD_PREFIX = "kasgate:v1:refund:";
export const PREPAID_REFUND_REASON_MAX_LENGTH = 300;
export const PREPAID_REFUND_STATUSES = ["requested", "paying", "payout_submitted", "paid", "rejected", "cancelled"] as const;
export type PrepaidRefundStatus = (typeof PREPAID_REFUND_STATUSES)[number];
export type PrepaidRefundAction = "request" | "cancel" | "status";
/** config.prepaid_refund_claim_ttl_seconds default: a merchant claim (`paying`) lapses after this without a payout. */
export const PREPAID_REFUND_CLAIM_TTL_SECONDS = 900;
export const PREPAID_REFUND_PAYOUT_STATUSES = ["pending", "confirmed", "invalid", "duplicate", "abandoned"] as const;
export type PrepaidRefundPayoutStatus = (typeof PREPAID_REFUND_PAYOUT_STATUSES)[number];

/**
 * POST /v1/prepaid/refunds/challenge. `request`: optional amount (omit = all spendable), no refund_id. `cancel`:
 * refund_id, no amount. `status`: neither (reads this key's refunds with the merchant's reasons).
 */
export interface PrepaidRefundChallengeRequest {
  slug: string;
  payer_address: string;
  action: PrepaidRefundAction;
  /** JSON integer on the wire; a decimal string here, converted server-side. */
  amount_sompi?: Sompi;
  refund_id?: Uuid;
}

export interface PrepaidRefundChallenge {
  nonce: string;
  /** Sign exactly this text with the depositing key, then POST {nonce, signature}. */
  message: string;
  action: PrepaidRefundAction;
  slug: string;
  merchant_id: Uuid;
  payer_address: string;
  /** The refund amount; "0" for a status challenge. */
  amount_sompi: Sompi;
  refund_id: Uuid | null;
  spendable_sompi: Sompi;
  expires_at: IsoDateTime;
  /** CONTRACT v2.14: the most a refund may take (failed-delivery credit excluded). */
  refundable_sompi: Sompi;
}

/** POST /v1/prepaid/refunds, /v1/prepaid/refunds/{id}/cancel and /v1/prepaid/refunds/status (signed). */
export interface PrepaidRefundSigned {
  nonce: string;
  /** Sent as the wallet returned it (128 hex or Base64 of 64/65 bytes); the gateway normalises. */
  signature: string;
}

export interface PrepaidRefundRead {
  id: Uuid;
  merchant_id: Uuid;
  payer_address: string;
  amount_sompi: Sompi;
  status: PrepaidRefundStatus;
  /** The latest attempt while `payout_submitted`; the confirmed payout once `paid`. */
  tx_id: string | null;
  /** The merchant's reject reason. Null in the public GET /refunds/status view (read it with a signed status). */
  reason: string | null;
  /** The latest payout problem. Null in the public GET /refunds/status view. */
  last_error: string | null;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
  /** When the merchant claimed it (`paying`); the claim lapses PREPAID_REFUND_CLAIM_TTL_SECONDS later without a payout. */
  claimed_at: IsoDateTime | null;
  payout_submitted_at: IsoDateTime | null;
  paid_at: IsoDateTime | null;
  resolved_at: IsoDateTime | null;
  /** Late payout backstop: the payout confirmed after the refund had been cancelled or rejected. */
  late_paid_from: "cancelled" | "rejected" | null;
  /** For a late payout: the part of amount_sompi the balance no longer held (already spent or expired). */
  shortfall_sompi: Sompi;
  /** computed_field: pay exactly `amount_sompi` here (the depositing address). No platform fee. */
  payout_address: string;
  /** computed_field: hex of `payout_payload`; every payout (both modes) must carry it. */
  payout_payload_hex: string;
  /** computed_field: `kasgate:v1:refund:<refund id hex>` as text, for KasWare `sendKaspa(..., {payload})`. */
  payout_payload: string;
}

/** GET /v1/prepaid/refunds/{id}/payouts [Bearer]: every attempted payout transaction, oldest first. */
export interface PrepaidRefundPayoutRead {
  id: Uuid;
  refund_id: Uuid;
  tx_id: string;
  method: "tx_id" | "transaction";
  status: PrepaidRefundPayoutStatus;
  payload_verified: boolean;
  check_count: number;
  last_error: string | null;
  next_check_at: IsoDateTime;
  confirmed_at: IsoDateTime | null;
  resolved_at: IsoDateTime | null;
  created_at: IsoDateTime;
}

/** POST /v1/prepaid/refunds/{id}/payout [Bearer]: exactly one of the two. */
export type PrepaidRefundPayout = { tx_id: string; transaction?: undefined } | { transaction: string; tx_id?: undefined };

/** POST /v1/prepaid/refunds/{id}/reject [Bearer]. */
export interface PrepaidRefundReject {
  reason: string;
}

export type PrepaidLedgerEntryKind = "refund" | "expiry" | "failed_delivery_credit";
export type FailedDeliveryCreditReason = "attempts_exhausted" | "window_expired";
export type DeliveryFailure = "upstream_5xx" | "connect_error" | "timeout" | "oversized";

/** GET /v1/prepaid/ledger [Bearer]: refund payouts, expiries and failed-delivery credits, newest first. */
export interface PrepaidLedgerEntryRead {
  id: Uuid;
  payer_address: string;
  kind: PrepaidLedgerEntryKind;
  direction: "credit" | "debit";
  amount_sompi: Sompi;
  balance_after_sompi: Sompi;
  tx_id: string | null;
  refund_id: Uuid | null;
  note: string | null;
  created_at: IsoDateTime;
  /** failed_delivery_credit rows only (null / "0" otherwise). */
  settlement_id: Uuid | null;
  slug: string | null;
  credit_reason: FailedDeliveryCreditReason | null;
  /** Part of the merchant share not credited because the balance reached PREPAID_MAX_BALANCE_SOMPI. */
  shortfall_sompi: Sompi;
}

/* ------------------------------------------------------------------ failed-delivery auto-credit (CONTRACT v2.14) */

export type PaymentScheme = "kaspa-exact-v1" | "k402-kaspa-utxo" | "kaspa-exact-v2";

/** GET /v1/prepaid/credits [Bearer]: one failed-delivery credit joined with the paid call it credits. */
export interface PrepaidCreditRead {
  id: Uuid;
  kind: "failed_delivery_credit";
  tx_id: string;
  settlement_id: Uuid;
  endpoint_id: Uuid;
  slug: string;
  payer_address: string;
  payment_scheme: PaymentScheme;
  /** What the buyer paid on-chain for the call. */
  amount_sompi: Sompi;
  /** Platform fee (output 1): already paid to the treasury and NOT reversed. */
  fee_sompi: Sompi;
  /** The merchant share credited to the buyer's prepaid balance (call-only, never refunded on-chain). */
  credit_sompi: Sompi;
  shortfall_sompi: Sompi;
  balance_after_sompi: Sompi;
  reason: FailedDeliveryCreditReason;
  delivery_failure: DeliveryFailure;
  /** >= 500, or null for a connection error / timeout. */
  upstream_status_code: number | null;
  delivery_attempts: number;
  settled_at: IsoDateTime | null;
  created_at: IsoDateTime;
}

/** GET/PUT /v1/prepaid/failed-delivery-policy [Bearer]. The policy in force when a call settles is stamped on it. */
export interface FailedDeliveryPolicyRead {
  enabled: boolean;
  credit_timeouts: boolean;
  enabled_at: IsoDateTime;
  disabled_at: IsoDateTime | null;
}

/** PUT body: omitted fields keep their value; at least one must be given. */
export interface FailedDeliveryPolicyUpdate {
  enabled?: boolean;
  credit_timeouts?: boolean;
}

/** One credit in the buyer's summary (newest first). */
export interface BuyerCreditItem {
  txId: string;
  slug: string;
  creditSompi: Sompi;
  shortfallSompi: Sompi;
  reason: FailedDeliveryCreditReason;
  createdAt: IsoDateTime;
}

/** X-KasGate-Credit-Summary (Base64 JSON) on the signed POST /v1/prepaid/refunds/status reply. */
export interface BuyerCreditSummary {
  merchantId: Uuid;
  creditedSompi: Sompi;
  creditBalanceSompi: Sompi;
  refundableSompi: Sompi;
  creditCount: number;
  /** The newest credits, at most 10. */
  credits: BuyerCreditItem[];
}

/** This app's POST /api/prepaid/refunds/status reply: the gateway body plus its decoded credit-summary header. */
export interface SignedRefundStatus {
  refunds: PrepaidRefundRead[];
  credit_summary: BuyerCreditSummary | null;
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
  /** CONTRACT v2.13: default prepaid credit expiry in days after the latest deposit (null = never expires). */
  prepaid_expiry_days: number | null;
}

/** PATCH /v1/merchants/me (CONTRACT v2.13). Only fields present change; `prepaid_expiry_days: null` = never expire. */
export interface MerchantUpdate {
  display_name?: string;
  prepaid_expiry_days?: number | null;
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
  /** CONTRACT v2.11: payable only from prepaid balances; the price may then be below the 0.6 KAS on-chain minimum. */
  prepaid_only?: boolean;
  /** CONTRACT v2.12: free calls per buyer key (0..100, default 0 = no trial). */
  free_trial_calls?: number;
  /** CONTRACT v2.12: trial calls this endpoint serves per UTC day across all buyers (1..100000, default 100). */
  trial_daily_cap?: number;
  /** CONTRACT v2.13: credit bought here expires this many days after the latest deposit (1..3650); null = merchant default. */
  prepaid_expiry_days?: number | null;
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
  prepaid_only?: boolean | null;
  /** Not nullable on the gateway: omit to keep. */
  free_trial_calls?: number;
  trial_daily_cap?: number;
  /** CONTRACT v2.13: 1..3650, or null to fall back to the merchant default. */
  prepaid_expiry_days?: number | null;
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
  prepaid_only: boolean;
  free_trial_calls: number;
  trial_daily_cap: number;
  /** CONTRACT v2.13: per-endpoint credit expiry override (null = merchant default). */
  prepaid_expiry_days: number | null;
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
  /** CONTRACT v2.12: free trial calls per buyer key on every imported endpoint (0..100, default 0). */
  free_trial_calls?: number;
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
  payment_scheme: PaymentScheme;
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
  /** CONTRACT v2.11: payable only from a prepaid balance (absent on gateways before v2.11). */
  prepaid_only?: boolean;
  /** CONTRACT v2.12: free trial calls per buyer key (absent on gateways before v2.12). */
  free_trial_calls?: number;
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

/* ------------------------------------------------------------------ free trial calls (CONTRACT v2.12) */

/** On a 402: Base64(JSON TrialOffer) when the endpoint offers free trial calls. */
export const HEADER_TRIAL_OFFER = "X-KasGate-Trial-Offer";
/** Request header: Base64(JSON {nonce, signature}) for ONE trial call. */
export const HEADER_TRIAL_CALL = "X-KasGate-Trial";
/** Response headers on a trial call. */
export const HEADER_TRIAL_RECEIPT = "X-KasGate-Trial-Receipt";
export const HEADER_TRIAL_LIMIT = "X-KasGate-Trial-Limit";
export const HEADER_TRIAL_REMAINING = "X-KasGate-Trial-Remaining";
export const TRIAL_MAX_CALLS = 100;
export const TRIAL_DAILY_CAP_DEFAULT = 100;
export const TRIAL_DAILY_CAP_MAX = 100_000;

export interface TrialOffer {
  scheme: "kasgate-trial-v1";
  freeTrialCalls: number;
  challengeUrl: string;
  statusUrl: string;
}

export type TrialCallStatus = "used" | "refunded";

export interface TrialCallRead {
  id: Uuid;
  endpoint_id: Uuid;
  payer_address: string;
  nonce: string;
  status: TrialCallStatus;
  request_method: string;
  request_path: string;
  upstream_status_code: number | null;
  created_at: IsoDateTime;
  refunded_at: IsoDateTime | null;
}

/* ------------------------------------------------------------------ merchant webhooks (CONTRACT v2.12) */

export const WEBHOOK_EVENT_TYPES = [
  "payment.settled",
  "prepaid.deposit",
  "prepaid.debit",
  "trial.used",
  "endpoint.created",
  "endpoint.updated",
  "prepaid.refund.requested",
  "prepaid.refund.paid",
  "prepaid.refund.rejected",
  "prepaid.refund.cancelled",
  "prepaid.refund.payout_reverted",
  "prepaid.credit.expired",
  "prepaid.credit.failed_delivery",
  "prepaid.credit.failed_delivery_shortfall",
] as const;
export type WebhookEventType = (typeof WEBHOOK_EVENT_TYPES)[number];
export type WebhookDeliveryStatus = "pending" | "delivering" | "succeeded" | "dead";
export const WEBHOOK_SIGNATURE_HEADER = "X-KasGate-Signature";
export const WEBHOOK_TIMESTAMP_HEADER = "X-KasGate-Timestamp";
export const WEBHOOK_EVENT_ID_HEADER = "X-KasGate-Event-Id";

export interface WebhookCreate {
  url: string;
  event_types: WebhookEventType[];
  description?: string | null;
}

export interface WebhookUpdate {
  url?: string;
  event_types?: WebhookEventType[];
  description?: string | null;
  is_active?: boolean;
}

export interface WebhookRead {
  id: Uuid;
  url: string;
  description: string | null;
  event_types: string[];
  /** First characters of the signing secret, to tell secrets apart. Never the secret. */
  secret_prefix: string;
  is_active: boolean;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
}

/** Returned by create and rotate-secret only: the signing secret is shown once. */
export interface WebhookCreated {
  webhook: WebhookRead;
  secret: string;
}

export interface WebhookDeliveryRead {
  id: Uuid;
  webhook_id: Uuid;
  event_id: string;
  event_type: string;
  status: WebhookDeliveryStatus;
  attempts: number;
  next_attempt_at: IsoDateTime | null;
  last_attempt_at: IsoDateTime | null;
  last_status_code: number | null;
  last_error: string | null;
  delivered_at: IsoDateTime | null;
  created_at: IsoDateTime;
}
