/**
 * CONTRACT v2.12.1 buyer-signed balance read. The unsigned GET /v1/prepaid/balance shows only balance, spendable and
 * expiry; the ledger totals (deposited, debited, refunded, expired, refund hold) come back only when the request
 * carries X-KasGate-Balance-Timestamp and X-KasGate-Balance-Signature: a kaspa sign_message (Schnorr) signature by
 * the depositing key over `balanceReadMessage`, with the timestamp within ±300 s of the gateway's clock.
 */
export const PREPAID_BALANCE_READ_PURPOSE = "kasgate:v1:prepaid-balance-read";
export const BALANCE_READ_TOLERANCE_SECONDS = 300;
export const HEADER_BALANCE_TIMESTAMP = "X-KasGate-Balance-Timestamp";
export const HEADER_BALANCE_SIGNATURE = "X-KasGate-Balance-Signature";

const TIMESTAMP_RE = /^[0-9]{1,12}$/;
const SIGNATURE_RE = /^[0-9a-f]{128}$/;

/** schemas.build_prepaid_balance_read_message, byte for byte. */
export function balanceReadMessage(input: { domain: string; address: string; slug: string; timestamp: number }): string {
  return ["KasGate", `Domain: ${input.domain}`, `Purpose: ${PREPAID_BALANCE_READ_PURPOSE}`, `Address: ${input.address}`, `Slug: ${input.slug}`, `Timestamp: ${input.timestamp}`].join("\n");
}

/** Unix seconds as the gateway accepts them (1–12 ASCII digits), or null. */
export function checkBalanceTimestamp(value: string): string | null {
  return TIMESTAMP_RE.test(value) ? value : null;
}

/** The 64-byte Schnorr signature as 128 lowercase hex (the only form the gateway accepts), or null. */
export function normaliseBalanceSignature(value: string): string | null {
  const v = value.trim().toLowerCase();
  return SIGNATURE_RE.test(v) ? v : null;
}
