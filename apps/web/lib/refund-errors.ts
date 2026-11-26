import { ApiError, errorMessage } from "./api-client";

/** CONTRACT v2.13 refund error vocabulary -> what the person should understand or do next. */
const REFUND_ERROR_TEXT: Record<string, string> = {
  invalid_payer: "This address can't use prepaid refunds: it must be the P2PK (q…) address that made the deposit.",
  invalid_signature: "The wallet signature didn't match this request. Make sure KasWare uses the depositing account, then try again.",
  invalid_status: "Unknown refund status filter.",
  challenge_expired: "The signing request expired before it was used. Try again.",
  challenge_replayed: "That signed request was already used. Try again to get a fresh one.",
  refund_too_small: "Refunds must be at least 0.2 KAS (the smallest standard Kaspa output). Spend smaller amounts on calls instead.",
  refund_already_open: "There is already an open refund request for this balance.",
  refund_not_found: "Refund not found.",
  refund_not_cancellable: "This refund can't be withdrawn right now because the merchant is paying it or a payout is still being checked.",
  refund_not_claimable:
    "This refund can't be claimed for payment: it is no longer open, or its claim window is used up (renewals stop 1 hour after the first claim). You can still post a signed transaction, record a payout you already sent, or reject it.",
  refund_not_rejectable: "This refund can't be rejected: a payout attempt is still pending or it is no longer open.",
  refund_closed: "This refund is already closed.",
  invalid_payout: "That transaction is not a valid payout of this refund (wrong amount, address or payload).",
  payout_replayed: "That transaction is already recorded as the payout of another refund.",
  payout_abandoned:
    "KasGate gave up on that transaction after it stayed unconfirmed too long. Re-posting its txid can't revive it: post the signed transaction instead to prove and re-broadcast it.",
  too_many_attempts:
    "This refund already has the maximum number of unconfirmed payout attempts. Wait until they are confirmed or given up before recording another one. Don't pay again.",
  insufficient_balance: "The spendable prepaid credit doesn't cover that amount.",
  endpoint_not_found: "No active endpoint has that slug.",
  wrong_network: "The payout is for the wrong Kaspa network.",
  tx_rejected: "The Kaspa node refused the transaction.",
  rate_limited: "Too many requests. Wait a minute and try again.",
  service_unavailable: "KasGate is temporarily unavailable. Try again shortly.",
};

/** Kaspa transaction ids (64 hex chars) named in a gateway message, e.g. a cancel refused because a payout was found. */
export function txIdsInMessage(message: string): string[] {
  return Array.from(new Set(message.toLowerCase().match(/\b[0-9a-f]{64}\b/g) ?? []));
}

function retryText(seconds: number): string {
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.ceil(seconds / 60);
  return `${minutes} min`;
}

/** A clear message for a refund call failure: the vocabulary text, plus the gateway's detail when it adds information. */
export function refundErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 429) {
    const wait = error.retryAfter !== undefined ? `Try again in ${retryText(error.retryAfter)}.` : "Wait a minute and try again.";
    return `Too many requests in a short time (the gateway limits payout submissions per merchant and refund actions per address). ${wait}`;
  }
  if (error instanceof ApiError && error.status === 503) {
    return "The Kaspa node or KasGate's store can't be reached right now, so this couldn't be checked or recorded. Nothing changed. Try again shortly.";
  }
  if (error instanceof ApiError && error.code && REFUND_ERROR_TEXT[error.code]) {
    const base = REFUND_ERROR_TEXT[error.code] ?? "";
    const detail = error.message.trim();
    return detail && detail !== base ? `${base} (${detail})` : base;
  }
  return errorMessage(error);
}
