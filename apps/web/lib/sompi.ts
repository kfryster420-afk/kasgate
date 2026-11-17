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

/** Mirrors schemas.compute_fee_split: fee = ceil(total * 150 / 10_000), merchant = total - fee. */
export function computeFeeSplit(total: bigint): BigFeeSplit {
  const fee = (total * PLATFORM_FEE_BPS + BPS_DENOMINATOR - 1n) / BPS_DENOMINATOR;
  return { total, merchant: total - fee, fee };
}

/**
 * Mirrors schemas.min_price_sompi: max(floor((F-1)*10000/150)+1, F + ceil(F*150/10000)) where F is the gateway's
 * `min_output_sompi` (from GET /v1/directory). F is never hardcoded here; e.g. F=20,000,000 gives 1,333,333,267.
 */
export function minPriceSompi(minOutput: bigint): bigint {
  const byFee = ((minOutput - 1n) * BPS_DENOMINATOR) / PLATFORM_FEE_BPS + 1n;
  const byMerchant = minOutput + (minOutput * PLATFORM_FEE_BPS + BPS_DENOMINATOR - 1n) / BPS_DENOMINATOR;
  return byFee > byMerchant ? byFee : byMerchant;
}

/** True when both outputs of the split meet the KIP-9 floor. */
export function meetsOutputFloor(total: bigint, minOutput: bigint): boolean {
  const split = computeFeeSplit(total);
  return split.fee >= minOutput && split.merchant >= minOutput;
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
