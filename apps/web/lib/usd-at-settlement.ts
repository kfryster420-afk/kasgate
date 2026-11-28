/**
 * KAS -> USD at a recorded settlement rate, in exact decimal arithmetic (no floats): sompi x kas_usd_rate / 1e8,
 * rounded half-up to 4 decimals so sub-cent credits stay visible. Null when either input is not a plain decimal.
 */
const SOMPI_RE = /^(?:0|[1-9][0-9]{0,17})$/;
const RATE_RE = /^([0-9]{1,12})(?:\.([0-9]{1,18}))?$/;
const OUT_DECIMALS = 4;

export function usdForSompi(sompi: string, rate: string | null | undefined): string | null {
  if (!SOMPI_RE.test(sompi) || typeof rate !== "string") return null;
  const match = RATE_RE.exec(rate.trim());
  if (!match) return null;
  const fraction = match[2] ?? "";
  const rateUnits = BigInt(`${match[1]}${fraction}`);
  const divisor = 10n ** BigInt(8 + fraction.length - OUT_DECIMALS);
  const scaled = BigInt(sompi) * rateUnits;
  const rounded = divisor > 1n ? (scaled + divisor / 2n) / divisor : scaled * 10n ** BigInt(OUT_DECIMALS - 8 - fraction.length);
  const digits = rounded.toString().padStart(OUT_DECIMALS + 1, "0");
  return `${digits.slice(0, -OUT_DECIMALS)}.${digits.slice(-OUT_DECIMALS)}`;
}
