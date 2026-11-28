/**
 * CONTRACT v2.12.1 (#6 @a96a16a) buyer-signed reads. The unsigned GET /v1/prepaid/balance returns only
 * spendable_sompi and expires_at (everything else null); GET /v1/prepaid/refunds/status refuses unsigned reads with
 * 401. Both take X-KasGate-Balance-Timestamp and X-KasGate-Balance-Signature: a kaspa sign_message (Schnorr)
 * signature by the depositing key over the same six-line text, differing only in Purpose, with the timestamp within
 * ±300 s of the gateway's clock. A signature for one read cannot be used for the other.
 */
export const PREPAID_BALANCE_READ_PURPOSE = "kasgate:v1:prepaid-balance-read";
export const PREPAID_REFUNDS_READ_PURPOSE = "kasgate:v1:refunds-status-read";
export type BuyerReadPurpose = typeof PREPAID_BALANCE_READ_PURPOSE | typeof PREPAID_REFUNDS_READ_PURPOSE;
export const BALANCE_READ_TOLERANCE_SECONDS = 300;
export const HEADER_BALANCE_TIMESTAMP = "X-KasGate-Balance-Timestamp";
export const HEADER_BALANCE_SIGNATURE = "X-KasGate-Balance-Signature";

const TIMESTAMP_RE = /^[0-9]{1,12}$/;
const SIGNATURE_RE = /^[0-9a-f]{128}$/;

type ReadInput = { domain: string; address: string; slug: string; timestamp: number };

/** schemas.build_prepaid_balance_read_message / build_prepaid_refunds_read_message, byte for byte. */
export function buyerReadMessage(purpose: BuyerReadPurpose, input: ReadInput): string {
  return ["KasGate", `Domain: ${input.domain}`, `Purpose: ${purpose}`, `Address: ${input.address}`, `Slug: ${input.slug}`, `Timestamp: ${input.timestamp}`].join("\n");
}

export function balanceReadMessage(input: ReadInput): string {
  return buyerReadMessage(PREPAID_BALANCE_READ_PURPOSE, input);
}

export function refundsReadMessage(input: ReadInput): string {
  return buyerReadMessage(PREPAID_REFUNDS_READ_PURPOSE, input);
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
