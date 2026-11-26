import { PREPAID_REFUND_PURPOSE, type PrepaidRefundAction, type PrepaidRefundChallenge } from "./types";

/**
 * Prepaid refund challenge (schemas.build_prepaid_refund_message, CONTRACT v2.13):
 *   KasGate\nDomain: {host}\nPurpose: kasgate:v1:prepaid-refund\nAction: request|cancel|status\nAddress: {addr}\n
 *   Merchant: {uuid}\nAmount: {sompi}\nRefund: {uuid|-}\nNonce: {32 hex}\nExpires: {iso}
 * The app checks every line against what the buyer asked for BEFORE the wallet is asked to sign.
 */

const LABELS = ["Domain", "Purpose", "Action", "Address", "Merchant", "Amount", "Refund", "Nonce", "Expires"] as const;

export interface ParsedRefundChallenge {
  domain: string;
  purpose: string;
  action: string;
  address: string;
  merchant: string;
  amount: string;
  refund: string;
  nonce: string;
  expires: string;
}

export function parseRefundChallenge(message: string): ParsedRefundChallenge | null {
  const lines = message.split("\n");
  if (lines.length !== LABELS.length + 1 || lines[0] !== "KasGate") return null;
  const values: string[] = [];
  for (let i = 0; i < LABELS.length; i += 1) {
    const prefix = `${LABELS[i]}: `;
    const line = lines[i + 1] ?? "";
    if (!line.startsWith(prefix)) return null;
    values.push(line.slice(prefix.length));
  }
  const [domain, purpose, action, address, merchant, amount, refund, nonce, expires] = values as [
    string,
    string,
    string,
    string,
    string,
    string,
    string,
    string,
    string,
  ];
  return { domain, purpose, action, address, merchant, amount, refund, nonce, expires };
}

export interface RefundChallengeExpectation {
  action: PrepaidRefundAction;
  address: string;
  /** Exact Domain (netloc of the gateway PUBLIC_BASE_URL); null means misconfigured, so refuse. */
  domain: string | null;
  /** The amount the buyer asked for; undefined for "all spendable credit" (then the challenge's own amount is shown). */
  amountSompi?: string;
  /** Required for `cancel`: the refund being withdrawn. */
  refundId?: string;
  now?: number;
}

/** Returns null when the refund challenge is safe to sign, otherwise a user-facing refusal reason. */
export function checkRefundChallenge(challenge: PrepaidRefundChallenge, expected: RefundChallengeExpectation): string | null {
  const parsed = parseRefundChallenge(challenge.message);
  if (!parsed) return "Refusing to sign: the refund challenge is not in the KasGate format.";
  if (expected.domain === null || parsed.domain !== expected.domain) {
    return `Refusing to sign: the challenge is for domain "${parsed.domain}", not this KasGate gateway (${expected.domain ?? "unknown"}).`;
  }
  if (parsed.purpose !== PREPAID_REFUND_PURPOSE) return "Refusing to sign: this is not a prepaid refund challenge.";
  if (parsed.action !== expected.action || challenge.action !== expected.action) {
    return `Refusing to sign: the challenge would ${parsed.action} a refund, but you asked to ${expected.action} one.`;
  }
  const address = expected.address.toLowerCase();
  if (parsed.address.toLowerCase() !== address || challenge.payer_address.toLowerCase() !== address) {
    return "Refusing to sign: the challenge is for a different address.";
  }
  if (parsed.merchant !== challenge.merchant_id) return "Refusing to sign: the challenge names a different merchant.";
  if (parsed.amount !== challenge.amount_sompi) return "Refusing to sign: the refund amount in the challenge is inconsistent.";
  if (expected.action === "status") {
    if (parsed.amount !== "0" || parsed.refund !== "-" || challenge.refund_id !== null) {
      return "Refusing to sign: a status challenge must not name an amount or a refund.";
    }
  } else if (!/^[1-9][0-9]{0,17}$/.test(parsed.amount)) {
    return "Refusing to sign: the refund amount in the challenge is inconsistent.";
  }
  if (expected.amountSompi !== undefined && parsed.amount !== expected.amountSompi) {
    return "Refusing to sign: the challenge amount is not the amount you asked for.";
  }
  if (expected.action === "status") {
    /* checked above */
  } else if (expected.action === "request") {
    if (parsed.refund !== "-" || challenge.refund_id !== null) return "Refusing to sign: a new refund request must not name an existing refund.";
  } else if (!expected.refundId || parsed.refund !== expected.refundId || challenge.refund_id !== expected.refundId) {
    return "Refusing to sign: the challenge cancels a different refund.";
  }
  if (!/^[0-9a-f]{32}$/.test(parsed.nonce) || parsed.nonce !== challenge.nonce) return "Refusing to sign: the challenge nonce is inconsistent.";
  const expires = Date.parse(parsed.expires);
  if (Number.isNaN(expires) || Date.parse(challenge.expires_at) !== expires) return "Refusing to sign: the challenge expiry is inconsistent.";
  if (expires <= (expected.now ?? Date.now())) return "This challenge has already expired. Ask again.";
  return null;
}
