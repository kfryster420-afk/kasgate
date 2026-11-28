import { netlocOf } from "../config";
import { parseGatewayJson } from "../safe-json";
import { createEphemeralPayer, type EphemeralPayer } from "./kaspa-payer";

/**
 * No-wallet demo of the KasGate handshake, run from the visitor's browser straight against the gateway (CORS):
 *   1. GET /p/{slug}                        -> the real HTTP 402 payment terms
 *   2. POST /v1/trials/challenge            -> a trial challenge for a fresh throwaway key
 *   3. GET /p/{slug} + X-KasGate-Trial      -> the upstream response (CONTRACT v2.12, free, no coins move, no fee)
 * Nothing here can spend: the key is created in this page, owns nothing, and signs only the trial-call text.
 */

export const TRIAL_HEADER = "X-KasGate-Trial";
const TRIAL_OFFER_HEADER = "X-KasGate-Trial-Offer";
const TRIAL_RECEIPT_HEADER = "X-KasGate-Trial-Receipt";
const SOMPI_RE = /^[0-9]{1,18}$/;
const NONCE_RE = /^[0-9a-f]{32}$/;
const SLUG_RE = /^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$/;
const MAX_BODY_CHARS = 4000;

export type DemoErrorKind =
  | "gateway_down"
  | "rate_limited"
  | "trials_disabled"
  | "trial_cap_reached"
  | "trial_replayed"
  | "upstream_failed"
  | "bad_response";

export class DemoError extends Error {
  constructor(
    readonly kind: DemoErrorKind,
    message: string,
    readonly retryAfterSeconds: number | null = null,
  ) {
    super(message);
    this.name = "DemoError";
  }
}

export interface Payment402 {
  scheme: string;
  network: string;
  totalSompi: bigint;
  /** Output 0: the merchant's exact share. */
  merchantOutput: { address: string; sompi: bigint };
  /** Output 1: the platform fee to the treasury. */
  treasuryOutput: { address: string; sompi: bigint };
  feeSompi: bigint;
  expiresAt: string;
  trialCalls: number | null;
}

export interface TrialOutcome {
  status: number;
  contentType: string;
  body: string;
  bodyTruncated: boolean;
  limit: number | null;
  remaining: number | null;
  receiptStatus: "used" | "refunded" | null;
  payerAddress: string;
}

export interface DemoConfig {
  gatewayUrl: string;
  slug: string;
  addressPrefix: string;
  network: string;
  fetchImpl?: typeof fetch;
  createPayer?: (prefix: string) => EphemeralPayer;
  randomId?: () => string;
  onChallenge?: (challenge: Payment402) => void;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
}

function str(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  return typeof value === "string" ? value : null;
}

function retryAfter(response: Response): number | null {
  const raw = response.headers.get("Retry-After");
  if (raw === null || !/^[0-9]{1,6}$/.test(raw.trim())) return null;
  return Number(raw.trim());
}

function decodeHeaderJson(value: string | null): Record<string, unknown> | null {
  if (value === null) return null;
  try {
    return asRecord(JSON.parse(atob(value)));
  } catch {
    return null;
  }
}

function encodeHeaderJson(value: Record<string, string>): string {
  return btoa(JSON.stringify(value));
}

function sompi(record: Record<string, unknown>, key: string): bigint {
  const value = str(record, key);
  if (value === null || !SOMPI_RE.test(value)) throw new DemoError("bad_response", `the 402 has no valid ${key}`);
  return BigInt(value);
}

/** Strictly parses the gateway's 402 body (kaspa-exact-v1 entry): amounts stay exact BigInt sompi. */
export function parsePayment402(body: string, headers: Headers, config: Pick<DemoConfig, "addressPrefix" | "network">): Payment402 {
  let parsed: unknown;
  try {
    parsed = parseGatewayJson<unknown>(body);
  } catch {
    throw new DemoError("bad_response", "the gateway's 402 is not valid JSON");
  }
  const accepts = asRecord(parsed)?.["accepts"];
  const entry = Array.isArray(accepts) ? accepts.map(asRecord).find((item) => item !== null && item["scheme"] === "kaspa-exact-v1") : null;
  if (!entry) throw new DemoError("bad_response", "the 402 offers no kaspa-exact-v1 payment terms");
  const fee = asRecord(entry["platformFee"]);
  const payTo = str(entry, "payTo");
  const feeTo = fee ? str(fee, "feeTo") : null;
  const network = str(entry, "network");
  if (!fee || !payTo || !feeTo || network !== config.network) throw new DemoError("bad_response", "the 402 terms are incomplete or for another network");
  if (!payTo.startsWith(config.addressPrefix) || !feeTo.startsWith(config.addressPrefix)) {
    throw new DemoError("bad_response", `the 402 pays to an address that is not ${config.addressPrefix}`);
  }
  const total = sompi(entry, "amount");
  const feeSompi = sompi(fee, "feeAmount");
  if (feeSompi <= 0n || feeSompi >= total) throw new DemoError("bad_response", "the 402 fee does not fit inside its total");
  const offer = decodeHeaderJson(headers.get(TRIAL_OFFER_HEADER));
  const trialCalls = offer && offer["scheme"] === "kasgate-trial-v1" && typeof offer["freeTrialCalls"] === "number" && Number.isInteger(offer["freeTrialCalls"]) && offer["freeTrialCalls"] > 0 ? offer["freeTrialCalls"] : null;
  return {
    scheme: "kaspa-exact-v1",
    network,
    totalSompi: total,
    merchantOutput: { address: payTo, sompi: total - feeSompi },
    treasuryOutput: { address: feeTo, sompi: feeSompi },
    feeSompi,
    expiresAt: str(entry, "expiresAt") ?? "",
    trialCalls,
  };
}

/** The text the gateway asks to be signed, rebuilt locally so we never sign anything but this one request. */
export function buildTrialMessage(parts: { domain: string; address: string; merchantId: string; slug: string; method: string; path: string; bodyHash: string; nonce: string; expiresAt: string }): string {
  const match = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:Z|\+00:00)$/.exec(parts.expiresAt);
  if (!match) throw new DemoError("bad_response", "the trial challenge has an unreadable expiry");
  return [
    "KasGate",
    `Domain: ${parts.domain}`,
    "Purpose: kasgate:v1:trial-call",
    `Address: ${parts.address}`,
    `Merchant: ${parts.merchantId}`,
    `Endpoint: ${parts.slug}`,
    `Method: ${parts.method}`,
    `Path: ${parts.path}`,
    `Body-Hash: ${parts.bodyHash}`,
    `Nonce: ${parts.nonce}`,
    `Expires: ${match[1]}+00:00`,
  ].join("\n");
}

function errorFromStatus(response: Response, code: string | null): DemoError | null {
  if (response.status === 429) {
    const wait = retryAfter(response);
    return new DemoError("rate_limited", wait === null ? "Too many demo calls from your network. Try again in a minute." : `Too many demo calls from your network. Try again in ${wait} s.`, wait);
  }
  if (response.status === 503 || response.status === 502 || response.status === 504) {
    return new DemoError("gateway_down", "The testnet gateway is temporarily unavailable. Try again shortly.", retryAfter(response));
  }
  if (response.status === 404) return new DemoError("gateway_down", "The demo endpoint is not available on this gateway.");
  if (code === "trial_exhausted") return new DemoError("trial_cap_reached", "Today's free demo calls are used up. Pay for real below, or come back tomorrow.");
  if (code === "payment_replayed" || response.status === 409) return new DemoError("trial_replayed", "That demo call was already used. Run it again.");
  return null;
}

async function errorCode(response: Response): Promise<string | null> {
  try {
    return str(asRecord(parseGatewayJson<unknown>(await response.text())) ?? {}, "error");
  } catch {
    return null;
  }
}

async function send(fetchImpl: typeof fetch, url: string, init: RequestInit): Promise<Response> {
  try {
    return await fetchImpl(url, { ...init, cache: "no-store", credentials: "omit", mode: "cors" });
  } catch {
    throw new DemoError("gateway_down", "Could not reach the testnet gateway. Check your connection and try again.");
  }
}

/** Step 1: the real 402. Throws DemoError when trials are off or the gateway is unreachable. */
export async function fetchChallenge(config: DemoConfig, path: string): Promise<Payment402> {
  const fetchImpl = config.fetchImpl ?? fetch;
  const response = await send(fetchImpl, `${config.gatewayUrl}${path}`, { method: "GET" });
  if (response.status !== 402) {
    const mapped = errorFromStatus(response, null);
    if (mapped) throw mapped;
    throw new DemoError("bad_response", `Expected an HTTP 402 from the demo endpoint but got ${response.status}.`);
  }
  const challenge = parsePayment402(await response.text(), response.headers, config);
  config.onChallenge?.(challenge);
  if (challenge.trialCalls === null) {
    throw new DemoError("trials_disabled", "Free trials are not switched on for this demo endpoint right now. You can still pay for real below.");
  }
  return challenge;
}

/** Steps 2 and 3: a trial challenge for a throwaway key, signed locally, then the free call. */
export async function runTrialCall(config: DemoConfig, path: string): Promise<TrialOutcome> {
  const fetchImpl = config.fetchImpl ?? fetch;
  const domain = netlocOf(config.gatewayUrl);
  if (!domain) throw new DemoError("bad_response", "the gateway URL is not configured");
  const payer = (config.createPayer ?? createEphemeralPayer)(config.addressPrefix);
  const bodyHash = "-";
  const challengeResponse = await send(fetchImpl, `${config.gatewayUrl}/v1/trials/challenge`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ slug: config.slug, payer_address: payer.address, method: "GET", path, body_hash: bodyHash }),
  });
  if (challengeResponse.status !== 201) {
    const mapped = errorFromStatus(challengeResponse, await errorCode(challengeResponse));
    throw mapped ?? new DemoError("bad_response", `The trial challenge was refused (HTTP ${challengeResponse.status}).`);
  }
  let challenge: Record<string, unknown> | null = null;
  try {
    challenge = asRecord(JSON.parse(await challengeResponse.text()));
  } catch {
    challenge = null;
  }
  const nonce = challenge ? str(challenge, "nonce") : null;
  const message = challenge ? str(challenge, "message") : null;
  const merchantId = challenge ? str(challenge, "merchant_id") : null;
  const expiresAt = challenge ? str(challenge, "expires_at") : null;
  if (!challenge || !nonce || !NONCE_RE.test(nonce) || !message || !merchantId || !expiresAt) throw new DemoError("bad_response", "the trial challenge is incomplete");
  const expected = buildTrialMessage({ domain, address: payer.address, merchantId, slug: config.slug, method: "GET", path, bodyHash, nonce, expiresAt });
  if (message !== expected || str(challenge, "payer_address") !== payer.address || str(challenge, "path") !== path) {
    throw new DemoError("bad_response", "the trial challenge does not match this request, so it was not signed");
  }
  const call = await send(fetchImpl, `${config.gatewayUrl}${path}`, {
    method: "GET",
    headers: { [TRIAL_HEADER]: encodeHeaderJson({ nonce, signature: payer.sign(message) }) },
  });
  const receipt = decodeHeaderJson(call.headers.get(TRIAL_RECEIPT_HEADER));
  const text = await call.text();
  if (!receipt) {
    let code: string | null = null;
    try {
      code = str(asRecord(parseGatewayJson<unknown>(text)) ?? {}, "error");
    } catch {
      code = null;
    }
    const mapped = errorFromStatus(call, code);
    throw mapped ?? new DemoError("bad_response", `The free call was not served as a trial (HTTP ${call.status}).`);
  }
  const limit = Number(call.headers.get("X-KasGate-Trial-Limit"));
  const remaining = Number(call.headers.get("X-KasGate-Trial-Remaining"));
  const receiptStatus = receipt["status"] === "used" || receipt["status"] === "refunded" ? receipt["status"] : null;
  if (receiptStatus === "refunded" || call.status >= 500) {
    throw new DemoError("upstream_failed", "The demo API did not answer, so this free call was not counted. Try again in a moment.");
  }
  return {
    status: call.status,
    contentType: call.headers.get("Content-Type") ?? "",
    body: text.length > MAX_BODY_CHARS ? text.slice(0, MAX_BODY_CHARS) : text,
    bodyTruncated: text.length > MAX_BODY_CHARS,
    limit: Number.isInteger(limit) ? limit : null,
    remaining: Number.isInteger(remaining) ? remaining : null,
    receiptStatus,
    payerAddress: payer.address,
  };
}

/** A per-run URL suffix so no shared cache between the visitor and the gateway can answer in place of it. */
export function demoPath(slug: string, runId: string): string {
  if (!SLUG_RE.test(slug)) throw new DemoError("bad_response", "the demo endpoint slug is invalid");
  if (!/^[0-9a-f]{12}$/.test(runId)) throw new DemoError("bad_response", "invalid run id");
  return `/p/${slug}?kg_demo=${runId}`;
}

export function newRunId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(6));
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

export type { EphemeralPayer };
