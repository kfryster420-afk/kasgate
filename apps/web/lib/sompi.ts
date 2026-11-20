import { BPS_DENOMINATOR, MAX_SOMPI, PLATFORM_FEE_BPS, SOMPI_PER_KAS } from "./config";

export interface BigFeeSplit {
  total: bigint;
  merchant: bigint;
  fee: bigint;
}

/** Parses a decimal KAS string ("6.67", "10", ".5") into sompi using integer maths only. Returns null when invalid. */
export function parseKasToSompi(input: string): bigint | null {
  const value = input.trim();
  const match = /^(\d*)(?:\.(\d{0,8}))?$/.exec(value);
  if (!match) return null;
  const whole = match[1] ?? "";
  const frac = match[2] ?? "";
  if (whole === "" && frac === "") return null;
  const sompi = BigInt(whole === "" ? "0" : whole) * SOMPI_PER_KAS + BigInt(frac.padEnd(8, "0"));
  if (sompi <= 0n || sompi > MAX_SOMPI) return null;
  return sompi;
}

/** ceil(price * 150 / 10_000) using BigInt. */
export function percentageFeeSompi(price: bigint): bigint {
  return (price * PLATFORM_FEE_BPS + BPS_DENOMINATOR - 1n) / BPS_DENOMINATOR;
}

/**
 * CONTRACT v2.1 treasury fee: max(ceil(price * 150 / 10_000), F) where F is the gateway's `min_output_sompi`
 * (DirectoryManifest.min_output_sompi). F is never hardcoded in this app.
 */
export function feeSompi(price: bigint, minOutput: bigint): bigint {
  const pct = percentageFeeSompi(price);
  return pct > minOutput ? pct : minOutput;
}

/** Mirrors schemas.compute_fee_split: merchant = price - feeSompi(price, F). */
export function computeFeeSplit(total: bigint, minOutput: bigint): BigFeeSplit {
  const fee = feeSompi(total, minOutput);
  return { total, merchant: total - fee, fee };
}

/**
 * Mirrors schemas.min_price_sompi (CONTRACT v2.3): 3 * F. The treasury fee is the flat floor F and the merchant output
 * must be at least 2 * F, which leaves storage-mass room for a change output. 60,000,000 sompi when F = 20,000,000.
 */
export function minPriceSompi(minOutput: bigint): bigint {
  return 3n * minOutput;
}

/** True when the treasury fee is the flat floor F, i.e. ceil(price * 150 / 10_000) <= F. */
export function isFlatFee(price: bigint, minOutput: bigint): boolean {
  return percentageFeeSompi(price) <= minOutput;
}

/** Highest price that still pays the flat floor fee: floor(F * 10_000 / 150). 1,333,333,333 when F = 20,000,000. */
export function flatFeeMaxPriceSompi(minOutput: bigint): bigint {
  return (minOutput * BPS_DENOMINATOR) / PLATFORM_FEE_BPS;
}

/** Valid iff the merchant output is at least 2 * F (the fee output is always >= F by construction); equals price >= 3 * F. */
export function meetsOutputFloor(total: bigint, minOutput: bigint): boolean {
  return total - feeSompi(total, minOutput) >= 2n * minOutput;
}

/** User-facing reason for the minimum price, formatted from F (never hardcoded). */
export function minPriceReason(minOutput: bigint): string {
  return `the treasury output must be at least ${formatKas(minOutput)} KAS and the merchant output at least ${formatKas(2n * minOutput)} KAS, which leaves room for change under the storage-mass limit`;
}

/** Share of `part` in `total` as hundredths of a percent, rounded half up (e.g. 150 = 1.50%). */
export function shareBps(part: bigint, total: bigint): bigint {
  return (part * 20_000n + total) / (2n * total);
}

export function formatBps(bps: bigint): string {
  const frac = (bps % 100n).toString().padStart(2, "0");
  return `${bps / 100n}.${frac}`.replace(/0+$/, "").replace(/\.$/, "");
}

export function toBigInt(value: string | bigint): bigint {
  return typeof value === "bigint" ? value : BigInt(value);
}

function groupThousands(digits: string): string {
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** Exact sompi -> KAS decimal string, trimming trailing zeros but keeping at least `minFraction` digits. */
export function formatKas(sompi: string | bigint, minFraction = 2): string {
  const value = toBigInt(sompi);
  const negative = value < 0n;
  const abs = negative ? -value : value;
  const whole = abs / SOMPI_PER_KAS;
  let frac = (abs % SOMPI_PER_KAS).toString().padStart(8, "0").replace(/0+$/, "");
  frac = frac.padEnd(minFraction, "0");
  const body = frac.length > 0 ? `${groupThousands(whole.toString())}.${frac}` : groupThousands(whole.toString());
  return `${negative ? "-" : ""}${body}`;
}

export function formatSompi(sompi: string | bigint): string {
  return groupThousands(toBigInt(sompi).toString());
}

/** Sums sompi strings exactly. */
export function sumSompi(values: readonly (string | bigint)[]): bigint {
  return values.reduce<bigint>((acc, v) => acc + toBigInt(v), 0n);
}

/** Lossy conversion for chart geometry only (never for displayed amounts). */
export function sompiToKasNumber(sompi: string | bigint): number {
  const value = toBigInt(sompi);
  return Number(value / SOMPI_PER_KAS) + Number(value % SOMPI_PER_KAS) / 1e8;
}

/** Exact sompi -> KAS string without grouping separators, safe to feed back into `parseKasToSompi`. */
export function sompiToKasPlain(sompi: string | bigint): string {
  const value = toBigInt(sompi);
  const whole = value / SOMPI_PER_KAS;
  const frac = (value % SOMPI_PER_KAS).toString().padStart(8, "0").replace(/0+$/, "");
  return frac ? `${whole}.${frac}` : whole.toString();
}
