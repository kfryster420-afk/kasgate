import { UUID_RE } from "./credit-status";
import { parseRefundChallenge } from "./refund-challenge";
import { PREPAID_REFUND_PURPOSE, type PrepaidCreditStatusChallenge } from "./types";

/** Null when the `credits` challenge is safe to sign (every line checked), otherwise a user-facing refusal. */
export function checkCreditChallenge(challenge: PrepaidCreditStatusChallenge, expected: { address: string; slug: string; domain: string | null; now?: number }): string | null {
  const parsed = parseRefundChallenge(challenge.message);
  if (!parsed) return "Refusing to sign: the credit challenge is not in the KasGate format.";
  if (expected.domain === null || parsed.domain !== expected.domain) {
    return `Refusing to sign: the challenge is for domain "${parsed.domain}", not this KasGate gateway (${expected.domain ?? "unknown"}).`;
  }
  if (parsed.purpose !== PREPAID_REFUND_PURPOSE || parsed.action !== "credits" || challenge.action !== "credits") return "Refusing to sign: this is not a credit status challenge.";
  const address = expected.address.toLowerCase();
  if (parsed.address.toLowerCase() !== address || challenge.payer_address.toLowerCase() !== address) return "Refusing to sign: the challenge is for a different address.";
  if (challenge.slug !== expected.slug) return "Refusing to sign: the challenge is for a different endpoint.";
  if (!UUID_RE.test(challenge.merchant_id) || parsed.merchant !== challenge.merchant_id) return "Refusing to sign: the challenge names a different merchant.";
  if (parsed.amount !== "0" || challenge.amount_sompi !== "0" || parsed.refund !== "-" || challenge.refund_id !== null) {
    return "Refusing to sign: a credit status challenge must not name an amount or a refund.";
  }
  if (!/^[0-9a-f]{32}$/.test(parsed.nonce) || parsed.nonce !== challenge.nonce) return "Refusing to sign: the challenge nonce is inconsistent.";
  const expires = Date.parse(parsed.expires);
  if (Number.isNaN(expires) || Date.parse(challenge.expires_at) !== expires) return "Refusing to sign: the challenge expiry is inconsistent.";
  if (expires <= (expected.now ?? Date.now())) return "This challenge has already expired. Try again.";
  return null;
}
