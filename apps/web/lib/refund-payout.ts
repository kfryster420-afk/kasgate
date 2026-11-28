"use client";

import { checkKaspaAddress } from "./validation";
import {
  getKasware,
  isWalletRejection,
  kaswareNetworkRefusal,
  txIdFromSendResult,
  walletErrorText,
  WalletTimeoutError,
  withWalletTimeout,
} from "./kasware";
import { KASPA_NETWORK } from "./config";
import { formatKas, formatSompi } from "./sompi";
import { CLAIM_BROADCAST_MARGIN_SECONDS, PREPAID_REFUND_PAYLOAD_PREFIX, type PrepaidRefundRead } from "./types";

/** How long the dashboard waits for the KasWare approval dialog before telling the merchant what to check. */
export const SEND_TIMEOUT_MS = 180_000;
const STORAGE_PREFIX = "kasgate:refund-payout:";
/** Stored instead of a txid when KasWare reported success without a readable txid. */
export const UNKNOWN_TXID = "unknown";

/**
 * A payout this browser already broadcast for a refund, kept in sessionStorage so a failed submission, a reload or a
 * lapsed claim cannot lead to paying the same refund twice. `transaction` is KasWare's signed transaction JSON (public
 * data, no key material), kept so it can be posted if the node cannot show the payload (`payload_unverified`).
 */
export interface RecordedPayout {
  txId: string;
  transaction: string | null;
}

export function recordedPayout(refundId: string): RecordedPayout | null {
  let raw: string | null;
  try {
    raw = window.sessionStorage.getItem(STORAGE_PREFIX + refundId);
  } catch {
    return null;
  }
  if (!raw) return null;
  if (!raw.startsWith("{")) return { txId: raw, transaction: null };
  try {
    const parsed: unknown = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && typeof (parsed as { txId?: unknown }).txId === "string") {
      const transaction = (parsed as { transaction?: unknown }).transaction;
      return { txId: (parsed as { txId: string }).txId, transaction: typeof transaction === "string" ? transaction : null };
    }
  } catch {
    /* unreadable entry: keep blocking a second payment */
  }
  return { txId: UNKNOWN_TXID, transaction: null };
}

export function rememberPayout(refundId: string, payout: RecordedPayout): void {
  try {
    window.sessionStorage.setItem(STORAGE_PREFIX + refundId, JSON.stringify(payout));
  } catch {
    /* storage unavailable: the txid stays on screen instead */
  }
}

export function forgetPayout(refundId: string): void {
  try {
    window.sessionStorage.removeItem(STORAGE_PREFIX + refundId);
  } catch {
    /* nothing stored */
  }
}

/** The gateway's `payout_payload`, used only if it is exactly `kasgate:v1:refund:<this refund's id hex>` and matches its hex. */
export function payoutPayloadText(refund: PrepaidRefundRead): string | null {
  const expected = `${PREPAID_REFUND_PAYLOAD_PREFIX}${refund.id.replace(/-/g, "").toLowerCase()}`;
  const hex = Array.from(new TextEncoder().encode(expected), (b) => b.toString(16).padStart(2, "0")).join("");
  return refund.payout_payload === expected && hex === refund.payout_payload_hex.toLowerCase() ? expected : null;
}

/** The gateway's `claim_expires_at` (only while `paying`) in ms, or null when the refund isn't claimed. */
export function claimExpiresAt(refund: PrepaidRefundRead): number | null {
  if (refund.status !== "paying" || !refund.claim_expires_at) return null;
  const at = Date.parse(refund.claim_expires_at);
  return Number.isNaN(at) ? null : at;
}

/** Whole minutes (rounded down, at least 0) until `at`. */
export function minutesLeft(at: number, now: number): number {
  return Math.max(0, Math.floor((at - now) / 60_000));
}

/** True when a claim ending at `at` leaves enough time to broadcast safely. */
export function claimLeavesTime(at: number | null, now: number): boolean {
  return at !== null && at - now >= CLAIM_BROADCAST_MARGIN_SECONDS * 1000;
}

export type PayoutOutcome =
  | { kind: "sent"; txId: string; transaction: string | null; refund: PrepaidRefundRead }
  | { kind: "sent_no_txid"; refund: PrepaidRefundRead }
  | { kind: "claim_too_short"; message: string; refund: PrepaidRefundRead }
  | { kind: "error"; message: string; refund?: PrepaidRefundRead };

/** The signed transaction JSON KasWare returned, when it is one (an object with an `id`); null otherwise. */
export function signedTransactionText(result: unknown): string | null {
  const text = typeof result === "string" ? result : result && typeof result === "object" ? JSON.stringify(result) : null;
  if (!text || !text.trimStart().startsWith("{") || text.length > 24_000) return null;
  try {
    const parsed: unknown = JSON.parse(text);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? text : null;
  } catch {
    return null;
  }
}

/**
 * Builds the payout in the merchant's own KasWare wallet: `sendKaspa(payout_address, amount_sompi, {payload})`. KasWare
 * selects the merchant's UTXOs, shows its approval dialog, signs and broadcasts. The dashboard never sees a key.
 *
 * Right before broadcasting it calls `claim` (POST /claim: `requested`/`paying` -> `paying`, renewing the claim) so the
 * buyer cannot cancel while the payment is in flight. It broadcasts only when the returned `claim_expires_at` is at least
 * CLAIM_BROADCAST_MARGIN_SECONDS away; renewals stop 1 h after the first claim, so near the end it refuses and points
 * to the signed-transaction mode (recorded before broadcast, no claim needed). An unclaimable refund is never paid.
 */
export async function sendRefundPayout(
  refund: PrepaidRefundRead,
  merchantAddress: string,
  confirm: (text: string) => boolean,
  claim: () => Promise<PrepaidRefundRead>,
  claimFailure: (error: unknown) => string,
): Promise<PayoutOutcome> {
  const wallet = getKasware();
  if (!wallet) return { kind: "error", message: "KasWare was not detected in this browser. Pay from any wallet, then use “Enter txid”." };
  if (typeof wallet.sendKaspa !== "function") {
    return { kind: "error", message: "This KasWare version cannot send payments from a website. Update KasWare, or pay manually and use “Enter txid”." };
  }
  const destination = checkKaspaAddress(refund.payout_address);
  if (!destination.ok) return { kind: "error", message: `Refusing to pay: payout address: ${destination.error}` };
  const amount = BigInt(refund.amount_sompi);
  if (amount <= 0n || amount > BigInt(Number.MAX_SAFE_INTEGER)) {
    return { kind: "error", message: "This amount is too large for KasWare's sendKaspa. Pay manually and use “Enter txid”." };
  }
  const payload = payoutPayloadText(refund);
  if (!payload) return { kind: "error", message: "Refusing to pay: the refund's payout payload does not match its id." };

  let account: string;
  try {
    const accounts = await withWalletTimeout(wallet.requestAccounts(), SEND_TIMEOUT_MS, "KasWare did not answer the connection request.");
    const first = accounts[0];
    if (!first) return { kind: "error", message: "KasWare did not return an account." };
    const wrongNetwork = await kaswareNetworkRefusal(wallet, KASPA_NETWORK);
    if (wrongNetwork) return { kind: "error", message: `${wrongNetwork} Nothing was sent.` };
    const checked = checkKaspaAddress(first);
    if (!checked.ok) return { kind: "error", message: `KasWare account: ${checked.error}. Nothing was sent.` };
    account = checked.value;
  } catch (e) {
    if (isWalletRejection(e)) return { kind: "error", message: "You declined the KasWare connection. Nothing was sent." };
    return { kind: "error", message: `KasWare: ${walletErrorText(e)}` };
  }

  const otherWallet = account !== merchantAddress.toLowerCase();
  const lines = [`Send ${formatKas(amount)} KAS (${formatSompi(amount)} sompi) to the buyer?`, "", `To: ${destination.value}`, `From KasWare account: ${account}`];
  if (otherWallet) lines.push("Note: this is not your merchant payout address. That is allowed, but the refund then comes from this account.");
  lines.push("", "No platform fee applies. You pay the network fee. KasWare will ask you to approve the transaction.");
  const text = lines.join("\n");
  if (!confirm(text)) return { kind: "error", message: "Payout cancelled. Nothing was sent." };

  let claimed: PrepaidRefundRead;
  try {
    claimed = await claim();
  } catch (e) {
    return { kind: "error", message: `Could not claim the refund for payment, so nothing was sent: ${claimFailure(e)}` };
  }
  if (claimed.status !== "paying" || claimed.amount_sompi !== refund.amount_sompi || claimed.payout_address !== refund.payout_address) {
    return { kind: "error", message: "The refund changed while claiming it. Nothing was sent. Reload and check it again.", refund: claimed };
  }
  const expires = claimExpiresAt(claimed);
  if (!claimLeavesTime(expires, Date.now())) {
    const left = expires === null ? 0 : minutesLeft(expires, Date.now());
    return {
      kind: "claim_too_short",
      refund: claimed,
      message: `Not sent: the claim only has ${left} min left and can't be renewed further (renewals stop 1 hour after the first claim). Paying now could land after the buyer may cancel. Post a signed transaction instead (KasGate records it before broadcasting, no claim needed), or reject the refund.`,
    };
  }

  let result: string;
  try {
    result = await withWalletTimeout(
      wallet.sendKaspa(destination.value, Number(amount), { payload }),
      SEND_TIMEOUT_MS,
      "KasWare did not answer within 3 minutes. If you approved the payment, look up its txid in KasWare's activity and use “Enter txid”. Don't pay again.",
    );
  } catch (e) {
    if (e instanceof WalletTimeoutError) return { kind: "error", message: e.message, refund: claimed };
    if (isWalletRejection(e)) return { kind: "error", message: "You rejected the payment in KasWare. Nothing was sent. The claim lapses on its own when it expires.", refund: claimed };
    return { kind: "error", message: `KasWare could not send the payout: ${walletErrorText(e)}. Nothing was recorded.`, refund: claimed };
  }
  const txId = txIdFromSendResult(result);
  return txId ? { kind: "sent", txId, transaction: signedTransactionText(result), refund: claimed } : { kind: "sent_no_txid", refund: claimed };
}
