import { ADDRESS_PREFIX, MAX_SOMPI } from "./config";
import { formatKas, formatSompi, meetsOutputFloor, minPriceSompi } from "./sompi";
import { HTTP_METHODS, type HttpMethod } from "./types";

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
 * Validates a decimal-string sompi price (max 18 digits, SompiStr) against the minimum price 2 * F derived from the
 * gateway's min_output_sompi `minOutput`, so both the treasury and merchant outputs clear the KIP-9 floor.
 */
export function checkPriceSompi(input: unknown, minOutput: bigint): Check<string> {
  if (typeof input !== "string" || !/^[1-9][0-9]{0,17}$/.test(input.trim())) return fail("Price must be a positive whole number of sompi (at most 18 digits)");
  const value = BigInt(input.trim());
  if (value > MAX_SOMPI) return fail("Price exceeds the maximum supported amount");
  const floorPrice = minPriceSompi(minOutput);
  if (value < floorPrice || !meetsOutputFloor(value, minOutput)) {
    return fail(`Minimum price is ${formatSompi(floorPrice)} sompi (${formatKas(floorPrice)} KAS) so both outputs clear the KIP-9 floor`);
  }
  return ok(value.toString());
}
