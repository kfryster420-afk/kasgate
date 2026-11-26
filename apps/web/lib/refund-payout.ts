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
import { PREPAID_REFUND_PAYLOAD_PREFIX, type PrepaidRefundRead } from "./types";

/** How long the dashboard waits for the KasWare approval dialog before telling the merchant what to check. */
export const SEND_TIMEOUT_MS = 180_000;
const STORAGE_PREFIX = "kasgate:refund-payout:";
/** Stored instead of a txid when KasWare reported success without a readable txid. */
export const UNKNOWN_TXID = "unknown";

/**
 * The txid of a payout this browser already broadcast for a refund, kept in sessionStorage (a public txid, never a key)
 * so a failed submission or a reload cannot lead to paying the same refund twice.
 */
export function recordedPayout(refundId: string): string | null {
  try {
    return window.sessionStorage.getItem(STORAGE_PREFIX + refundId);
  } catch {
    return null;
  }
}

export function rememberPayout(refundId: string, txId: string): void {
  try {
    window.sessionStorage.setItem(STORAGE_PREFIX + refundId, txId);
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

/** `kasgate:v1:refund:<refund id hex>`; checked against the gateway's `payout_payload_hex` before use. */
export function payoutPayloadText(refund: PrepaidRefundRead): string | null {
  const text = `${PREPAID_REFUND_PAYLOAD_PREFIX}${refund.id.replace(/-/g, "").toLowerCase()}`;
  const hex = Array.from(new TextEncoder().encode(text), (b) => b.toString(16).padStart(2, "0")).join("");
  return hex === refund.payout_payload_hex.toLowerCase() ? text : null;
}

export type PayoutOutcome = { kind: "sent"; txId: string } | { kind: "sent_no_txid" } | { kind: "error"; message: string };

/**
 * Builds the payout in the merchant's own KasWare wallet: `sendKaspa(payout_address, amount_sompi, {payload})`. KasWare
 * selects the merchant's UTXOs, shows its approval dialog, signs and broadcasts. The dashboard never sees a key.
 */
export async function sendRefundPayout(refund: PrepaidRefundRead, merchantAddress: string, confirm: (text: string) => boolean): Promise<PayoutOutcome> {
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

  let result: string;
  try {
    result = await withWalletTimeout(
      wallet.sendKaspa(destination.value, Number(amount), { payload }),
      SEND_TIMEOUT_MS,
      "KasWare did not answer within 3 minutes. If you approved the payment, look up its txid in KasWare's activity and use “Enter txid”. Don't pay again.",
    );
  } catch (e) {
    if (e instanceof WalletTimeoutError) return { kind: "error", message: e.message };
    if (isWalletRejection(e)) return { kind: "error", message: "You rejected the payment in KasWare. Nothing was sent." };
    return { kind: "error", message: `KasWare could not send the payout: ${walletErrorText(e)}. Nothing was recorded.` };
  }
  const txId = txIdFromSendResult(result);
  return txId ? { kind: "sent", txId } : { kind: "sent_no_txid" };
}
