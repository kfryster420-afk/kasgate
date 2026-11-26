import { MAX_USD_MICROS, USD_MICROS_PER_DOLLAR } from "./config";
import { formatKas, formatSompi } from "./sompi";
import type { PriceCurrency, Sompi } from "./types";

/** USD decimal string (up to 8 dp from the gateway) -> "$1,234.50" keeping every significant digit. */
export function formatUsd(value: string | null | undefined, minFraction = 2): string {
  if (value === null || value === undefined || value === "") return "—";
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(value.trim());
  if (!match) return value;
  const sign = match[1] ?? "";
  const whole = (match[2] ?? "0").replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const frac = (match[3] ?? "").replace(/0+$/, "").padEnd(minFraction, "0");
  return `${sign}$${whole}${frac ? `.${frac}` : ""}`;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return `${d.toISOString().slice(0, 19).replace("T", " ")} UTC`;
}

export function shortHash(value: string, head = 8, tail = 6): string {
  return value.length <= head + tail + 1 ? value : `${value.slice(0, head)}…${value.slice(-tail)}`;
}

export function shortAddress(value: string): string {
  const colon = value.indexOf(":");
  const prefix = colon >= 0 ? value.slice(0, colon + 1) : "";
  const body = colon >= 0 ? value.slice(colon + 1) : value;
  return `${prefix}${shortHash(body, 6, 6)}`;
}

/** Parses a decimal USD string ("1.50", "10", ".99") into micro-dollars using integer maths only. Returns null when invalid. */
export function parseUsdToMicros(input: string): bigint | null {
  const value = input.trim();
  const match = /^(\d*)(?:\.(\d{0,6}))?$/.exec(value);
  if (!match) return null;
  const whole = match[1] ?? "";
  const frac = match[2] ?? "";
  if (whole === "" && frac === "") return null;
  const micros = BigInt(whole === "" ? "0" : whole) * USD_MICROS_PER_DOLLAR + BigInt(frac.padEnd(6, "0"));
  if (micros <= 0n || micros > MAX_USD_MICROS) return null;
  return micros;
}

/** Exact micro-dollars -> USD string without grouping separators, safe to feed back into `parseUsdToMicros`. */
export function microsToUsdPlain(micros: string | bigint): string {
  const value = typeof micros === "bigint" ? micros : BigInt(micros);
  const whole = value / USD_MICROS_PER_DOLLAR;
  const frac = (value % USD_MICROS_PER_DOLLAR).toString().padStart(6, "0").replace(/0+$/, "");
  return frac ? `${whole}.${frac}` : whole.toString();
}

export interface ListedPrice {
  primary: string;
  secondary: string;
}

/** Primary/secondary labels for an endpoint or directory row, using the listed currency (no live KAS conversion). */
export function listedPrice(entry: {
  price_currency: PriceCurrency;
  price_sompi: Sompi | null;
  price_usd: string | null;
}): ListedPrice {
  if (entry.price_currency === "USD") {
    return { primary: formatUsd(entry.price_usd), secondary: "USD" };
  }
  if (entry.price_sompi === null) return { primary: "—", secondary: "" };
  return { primary: `${formatKas(entry.price_sompi)} KAS`, secondary: `${formatSompi(entry.price_sompi)} sompi` };
}

/** One-line price for prose, e.g. "$1.50 USD" or "1.00 KAS (100,000,000 sompi)". */
export function formatEndpointPrice(entry: {
  price_currency: PriceCurrency;
  price_sompi: Sompi | null;
  price_usd: string | null;
}): string {
  if (entry.price_currency === "USD") return `${formatUsd(entry.price_usd)} USD`;
  if (entry.price_sompi === null) return "—";
  return `${formatKas(entry.price_sompi)} KAS (${formatSompi(entry.price_sompi)} sompi)`;
}
