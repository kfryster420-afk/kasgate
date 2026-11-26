import { ADDRESS_PREFIX, MAX_SOMPI, MAX_USD_MICROS } from "./config";
import { formatKas, formatSompi, meetsOutputFloor, minPriceReason, minPriceSompi } from "./sompi";
import {
  HTTP_METHODS,
  PRICE_CURRENCIES,
  TRIAL_DAILY_CAP_MAX,
  TRIAL_MAX_CALLS,
  WEBHOOK_EVENT_TYPES,
  type HttpMethod,
  type PriceCurrency,
  type WebhookEventType,
} from "./types";

/** Mirrors KASPA_ADDRESS_RE in schemas.py. */
const KASPA_ADDRESS_RE = /^(kaspa|kaspatest):[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{61,63}$/;
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const SLUG_RE = /^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])$/;
const HEADER_NAME_RE = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export type Check<T> = { ok: true; value: T } | { ok: false; error: string };

const ok = <T,>(value: T): Check<T> => ({ ok: true, value });
const fail = <T,>(error: string): Check<T> => ({ ok: false, error });

export function isUuid(value: string): boolean {
  return UUID_RE.test(value);
}

export function checkKaspaAddress(input: unknown): Check<string> {
  if (typeof input !== "string") return fail("Kaspa address is required");
  const value = input.trim().toLowerCase();
  if (!KASPA_ADDRESS_RE.test(value)) return fail("Not a valid Kaspa address");
  if (!value.startsWith(ADDRESS_PREFIX)) return fail(`Address must start with "${ADDRESS_PREFIX}" on this network`);
  return ok(value);
}

export function checkDisplayName(input: unknown): Check<string> {
  if (typeof input !== "string") return fail("Display name is required");
  const value = input.trim();
  if (value.length < 1 || value.length > 120) return fail("Display name must be 1–120 characters");
  return ok(value);
}

export function checkOptionalEmail(input: unknown): Check<string | null> {
  if (input === undefined || input === null) return ok(null);
  if (typeof input !== "string") return fail("Email must be a string");
  const value = input.trim();
  if (value === "") return ok(null);
  if (value.length > 320 || !EMAIL_RE.test(value)) return fail("Enter a valid email address");
  return ok(value);
}

export function checkEndpointName(input: unknown): Check<string> {
  if (typeof input !== "string") return fail("Name is required");
  const value = input.trim();
  if (value.length < 1 || value.length > 120) return fail("Name must be 1–120 characters");
  return ok(value);
}

export function checkOptionalText(input: unknown, label: string, max: number): Check<string | null> {
  if (input === undefined || input === null) return ok(null);
  if (typeof input !== "string") return fail(`${label} must be a string`);
  const value = input.trim();
  if (value.length > max) return fail(`${label} must be at most ${max} characters`);
  return ok(value === "" ? null : value);
}

export function checkOptionalSlug(input: unknown): Check<string | null> {
  if (input === undefined || input === null || input === "") return ok(null);
  if (typeof input !== "string") return fail("Slug must be a string");
  const value = input.trim().toLowerCase();
  if (!SLUG_RE.test(value)) return fail("Slug must be 3–64 chars: lowercase letters, digits and hyphens");
  return ok(value);
}

function isPrivateIpv4(host: string): boolean {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
  if (!m) return false;
  const [a, b] = [Number(m[1]), Number(m[2])];
  return (
    a === 0 ||
    a === 10 ||
    a === 127 ||
    (a === 169 && b === 254) ||
    (a === 172 && b >= 16 && b <= 31) ||
    (a === 192 && b === 168) ||
    (a === 100 && b >= 64 && b <= 127) ||
    a >= 224
  );
}

/** Mirrors _reject_private_host in schemas.py (the gateway remains the authority). */
export function checkUpstreamUrl(input: unknown): Check<string> {
  if (typeof input !== "string") return fail("Upstream URL is required");
  const value = input.trim();
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return fail("Enter a full URL such as https://api.example.com/v1");
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") return fail("Upstream URL must use http or https");
  const host = url.hostname.replace(/^\[|\]$/g, "").toLowerCase();
  if (host === "localhost" || host === "localhost.localdomain" || host.endsWith(".localhost") || host.endsWith(".internal")) {
    return fail("Upstream URL may not target a local host");
  }
  if (isPrivateIpv4(host) || host === "::1" || host === "::" || /^f[cd]/.test(host) || host.startsWith("fe80:")) {
    return fail("Upstream URL may not target a private or reserved IP");
  }
  return ok(value);
}

export function checkMethods(input: unknown): Check<HttpMethod[]> {
  if (!Array.isArray(input) || input.length === 0) return fail("Select at least one HTTP method");
  const out: HttpMethod[] = [];
  for (const m of input) {
    if (typeof m !== "string" || !(HTTP_METHODS as readonly string[]).includes(m)) return fail(`Unsupported method: ${String(m)}`);
    if (!out.includes(m as HttpMethod)) out.push(m as HttpMethod);
  }
  return ok(out.sort());
}

export function checkUpstreamHeaders(input: unknown): Check<Record<string, string>> {
  if (input === undefined || input === null) return ok({});
  if (typeof input !== "object" || Array.isArray(input)) return fail("upstream_headers must be an object");
  const entries = Object.entries(input as Record<string, unknown>);
  if (entries.length > 20) return fail("At most 20 upstream headers are allowed");
  const out: Record<string, string> = {};
  for (const [name, value] of entries) {
    if (!HEADER_NAME_RE.test(name)) return fail(`Invalid header name: ${name}`);
    if (typeof value !== "string" || value.length === 0 || value.length > 4096 || /[\r\n]/.test(value)) {
      return fail(`Invalid value for header ${name}`);
    }
    out[name] = value;
  }
  return ok(out);
}

/**
 * Validates a decimal-string sompi price (max 18 digits, SompiStr) against the minimum price 3 * F derived from the
 * gateway's min_output_sompi `minOutput` (CONTRACT v2.3: merchant output >= 2 * F, fee output >= F).
 */
/** Price of a prepaid-only endpoint (CONTRACT v2.11): any positive sompi amount; no on-chain floor applies. */
export function checkPrepaidPriceSompi(input: unknown): Check<string> {
  if (typeof input !== "string" || !/^[1-9][0-9]{0,17}$/.test(input.trim())) return fail("Price must be a positive whole number of sompi (at most 18 digits)");
  const value = BigInt(input.trim());
  if (value > MAX_SOMPI) return fail("Price exceeds the maximum supported amount");
  return ok(value.toString());
}

export function checkPriceSompi(input: unknown, minOutput: bigint): Check<string> {
  if (typeof input !== "string" || !/^[1-9][0-9]{0,17}$/.test(input.trim())) return fail("Price must be a positive whole number of sompi (at most 18 digits)");
  const value = BigInt(input.trim());
  if (value > MAX_SOMPI) return fail("Price exceeds the maximum supported amount");
  const floorPrice = minPriceSompi(minOutput);
  if (value < floorPrice || !meetsOutputFloor(value, minOutput)) {
    return fail(`Minimum price is ${formatSompi(floorPrice)} sompi (${formatKas(floorPrice)} KAS): ${minPriceReason(minOutput)}`);
  }
  return ok(value.toString());
}

export function checkPriceCurrency(input: unknown): Check<PriceCurrency> {
  if (typeof input === "string" && (PRICE_CURRENCIES as readonly string[]).includes(input)) return ok(input as PriceCurrency);
  return fail("price_currency must be KAS or USD");
}

/**
 * Validates a decimal-string micro-dollar price (schemas.UsdMicros: integer, 1..MAX_USD_MICROS).
 * The 0.6 KAS floor is applied at quote time, not at create.
 */
export function checkPriceUsdMicros(input: unknown): Check<string> {
  if (typeof input !== "string" || !/^[1-9][0-9]{0,14}$/.test(input.trim())) {
    return fail("Price must be a positive whole number of micro-dollars (at most 15 digits)");
  }
  const value = BigInt(input.trim());
  if (value > MAX_USD_MICROS) return fail("Price exceeds the maximum of $1,000,000,000");
  return ok(value.toString());
}

/** CONTRACT v2.12: free trial calls per buyer key, a whole number 0..100 (0 = no trial). */
export function checkFreeTrialCalls(input: unknown): Check<number> {
  if (typeof input !== "number" || !Number.isInteger(input) || input < 0 || input > TRIAL_MAX_CALLS) {
    return fail(`Free trial calls must be a whole number from 0 to ${TRIAL_MAX_CALLS}`);
  }
  return ok(input);
}

/** CONTRACT v2.12: trial calls per UTC day across all buyers, a whole number 1..100000. */
export function checkTrialDailyCap(input: unknown): Check<number> {
  if (typeof input !== "number" || !Number.isInteger(input) || input < 1 || input > TRIAL_DAILY_CAP_MAX) {
    return fail(`Daily trial cap must be a whole number from 1 to ${TRIAL_DAILY_CAP_MAX.toLocaleString("en-US")}`);
  }
  return ok(input);
}

const WEBHOOK_LOCAL_SUFFIXES = [".localhost", ".local", ".internal", ".lan", ".home.arpa", ".localdomain"];

/** Mirrors validate_webhook_url in schemas.py: HTTPS only, no credentials or fragment, no local or private host.
 * The gateway re-resolves the name on every delivery and refuses non-public addresses. */
export function checkWebhookUrl(input: unknown): Check<string> {
  if (typeof input !== "string") return fail("Webhook URL is required");
  const value = input.trim();
  if (value.length > 2048) return fail("Webhook URL must be at most 2048 characters");
  if (/[^\x21-\x7e]/.test(value)) return fail("Webhook URL must be printable ASCII without spaces");
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return fail("Enter a full URL such as https://hooks.example.com/kasgate");
  }
  if (url.protocol !== "https:") return fail("Webhook URL must use https://");
  if (url.username || url.password) return fail("Webhook URL must not contain credentials");
  if (url.hash) return fail("Webhook URL must not contain a fragment");
  const host = url.hostname.replace(/^\[|\]$/g, "").toLowerCase().replace(/\.$/, "");
  if (!host || host === "localhost" || host === "localhost.localdomain" || WEBHOOK_LOCAL_SUFFIXES.some((s) => host.endsWith(s))) {
    return fail("Webhook URL may not target a local host");
  }
  if (host.includes(":")) {
    if (host === "::1" || host === "::" || /^f[cd]/.test(host) || host.startsWith("fe80:") || host.startsWith("::ffff:")) {
      return fail("Webhook URL may not target a private, loopback or link-local IP");
    }
  } else if (/^\d{1,3}(\.\d{1,3}){3}$/.test(host)) {
    if (isPrivateIpv4(host)) return fail("Webhook URL may not target a private, loopback or link-local IP");
  } else if (!host.includes(".")) {
    return fail("Webhook URL host must be a public domain name");
  }
  return ok(value);
}

export function checkWebhookEventTypes(input: unknown): Check<WebhookEventType[]> {
  if (!Array.isArray(input) || input.length === 0) return fail("Choose at least one event type");
  const known = WEBHOOK_EVENT_TYPES as readonly string[];
  const out: WebhookEventType[] = [];
  for (const item of input) {
    if (typeof item !== "string" || !known.includes(item)) return fail(`Unknown event type: ${String(item)}`);
    if (!out.includes(item as WebhookEventType)) out.push(item as WebhookEventType);
  }
  return ok(out.sort());
}
