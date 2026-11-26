"use client";

import { Ban, ChevronLeft, ChevronRight, FileSignature, Hash, ListTree, Loader2, RefreshCw, Send, Wallet } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ApiError, apiFetch, errorMessage } from "@/lib/api-client";
import { IS_TESTNET, TESTNET_EXPLORER_URL } from "@/lib/config";
import { formatDateTime, shortAddress, shortHash } from "@/lib/format";
import { refundErrorMessage } from "@/lib/refund-errors";
import { claimExpiresAt, claimLeavesTime, forgetPayout, minutesLeft, recordedPayout, rememberPayout, sendRefundPayout, UNKNOWN_TXID, type RecordedPayout } from "@/lib/refund-payout";
import {
  PREPAID_REFUND_REASON_MAX_LENGTH,
  type MerchantRead,
  type PrepaidLedgerEntryRead,
  type PrepaidRefundPayoutRead,
  type PrepaidRefundRead,
  type PrepaidRefundStatus,
} from "@/lib/types";
import { checkRefundReason, checkTxId } from "@/lib/validation";
import { CreditExpirySettings } from "./credit-expiry-settings";
import { KasAmount } from "./kas-amount";
import { AttemptStatusBadge, LatePayoutNote, REFUND_STATUS_HELP, RefundStatusBadge } from "./refund-status";
import { ErrorBanner, Spinner } from "./ui";

const PAGE_SIZE = 25;
/** Largest signed transaction JSON the gateway accepts (schemas.MAX_RAW_TX_CHARS). */
const MAX_TRANSACTION_CHARS = 24_000;
type Tab = "open" | PrepaidRefundStatus | "all" | "ledger";
const TABS: { tab: Tab; label: string }[] = [
  { tab: "open", label: "Open" },
  { tab: "paid", label: "Paid" },
  { tab: "rejected", label: "Rejected" },
  { tab: "cancelled", label: "Cancelled" },
  { tab: "all", label: "All" },
  { tab: "ledger", label: "Ledger" },
];
const OPEN_STATUSES: readonly PrepaidRefundStatus[] = ["requested", "paying", "payout_submitted"];

function txLink(txId: string) {
  return IS_TESTNET ? (
    <a href={`${TESTNET_EXPLORER_URL}/txs/${txId}`} target="_blank" rel="noreferrer" className="font-mono text-xs text-kas-300 hover:underline" title={txId}>
      {shortHash(txId)}
    </a>
  ) : (
    <span className="font-mono text-xs" title={txId}>
      {shortHash(txId)}
    </span>
  );
}

function clockTime(ms: number): string {
  return new Date(ms).toISOString().slice(11, 16);
}

/** True when the node could not show this attempt's payload, so posting the signed transaction is what proves it. */
function needsSignedTransaction(attempt: PrepaidRefundPayoutRead): boolean {
  return attempt.status === "pending" && !attempt.payload_verified && (attempt.last_error ?? "").startsWith("payload_unverified");
}

const ATTEMPT_METHOD_LABEL: Record<PrepaidRefundPayoutRead["method"], string> = {
  tx_id: "txid",
  transaction: "signed transaction",
  scan: "found on-chain (scan)",
};

/** The cooldown of a cancelled refund the merchant had claimed: the amount stays held until `hold_until`. */
export function holdUntilMs(refund: PrepaidRefundRead): number | null {
  if (refund.status !== "cancelled" || !refund.hold_until) return null;
  const at = Date.parse(refund.hold_until);
  return Number.isNaN(at) ? null : at;
}

type RowMode = null | "reject" | "txid" | "transaction";

function AttemptsTable({ attempts }: { attempts: PrepaidRefundPayoutRead[] }) {
  if (attempts.length === 0) return <p className="text-xs text-zinc-500">No payout transactions recorded for this refund yet.</p>;
  return (
    <table className="w-full text-left" data-attempts>
      <thead>
        <tr className="text-[11px] uppercase tracking-wide text-zinc-500">
          <th className="py-1 pr-3 font-medium">Attempted (UTC)</th>
          <th className="py-1 pr-3 font-medium">Transaction</th>
          <th className="py-1 pr-3 font-medium">Sent as</th>
          <th className="py-1 pr-3 font-medium">State</th>
          <th className="py-1 pr-3 font-medium">Payload</th>
          <th className="py-1 pr-3 font-medium">Checks</th>
          <th className="py-1 font-medium">Last problem</th>
        </tr>
      </thead>
      <tbody>
        {attempts.map((attempt) => (
          <tr key={attempt.id} className="align-top text-xs" data-attempt-status={attempt.status}>
            <td className="py-1 pr-3 font-mono whitespace-nowrap">{formatDateTime(attempt.created_at)}</td>
            <td className="py-1 pr-3">{txLink(attempt.tx_id)}</td>
            <td className="py-1 pr-3 text-zinc-400" data-attempt-method={attempt.method}>
              {ATTEMPT_METHOD_LABEL[attempt.method]}
            </td>
            <td className="py-1 pr-3">
              <AttemptStatusBadge status={attempt.status} />
            </td>
            <td className="py-1 pr-3 text-zinc-400">{attempt.payload_verified ? "verified" : "not verified yet"}</td>
            <td className="py-1 pr-3 font-mono text-zinc-400">{attempt.check_count}</td>
            <td className="max-w-[360px] break-words py-1 text-amber-300/80">{attempt.last_error ?? <span className="text-zinc-600">—</span>}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function RefundRow({ refund, merchant, now, onUpdated, onUnauthorized }: { refund: PrepaidRefundRead; merchant: MerchantRead; now: number; onUpdated: (next: PrepaidRefundRead) => void; onUnauthorized: () => void }) {
  const [mode, setMode] = useState<RowMode>(null);
  const [busy, setBusy] = useState<null | "pay" | "submit" | "reject" | "transaction">(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [txInput, setTxInput] = useState("");
  const [rawInput, setRawInput] = useState("");
  const [recorded, setRecorded] = useState<RecordedPayout | null>(null);
  const [attempts, setAttempts] = useState<PrepaidRefundPayoutRead[] | null>(null);
  const [showAttempts, setShowAttempts] = useState(false);
  /** Set when a claim came back too short to broadcast safely, or renewing it was refused (claim window used up). */
  const [claimBlocked, setClaimBlocked] = useState<string | null>(null);
  const open = OPEN_STATUSES.includes(refund.status);
  const lateEligible = refund.status === "cancelled" || refund.status === "rejected";

  const fail = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) {
        onUnauthorized();
        return;
      }
      setError(refundErrorMessage(e));
    },
    [onUnauthorized],
  );

  const loadAttempts = useCallback(async () => {
    try {
      setAttempts(await apiFetch<PrepaidRefundPayoutRead[]>(`/api/prepaid/refunds/${refund.id}/payouts`));
    } catch (e) {
      fail(e);
    }
  }, [refund.id, fail]);

  useEffect(() => {
    if (refund.status === "paid") {
      forgetPayout(refund.id);
      setRecorded(null);
    } else {
      setRecorded(recordedPayout(refund.id));
    }
  }, [refund.status, refund.id]);

  useEffect(() => {
    if (open || showAttempts || refund.late_paid_from) void loadAttempts();
  }, [open, showAttempts, refund.late_paid_from, refund.updated_at, loadAttempts]);

  const pending = (attempts ?? []).filter((attempt) => attempt.status === "pending");
  const hasPending = pending.length > 0;
  const unverified = pending.find(needsSignedTransaction) ?? null;
  const claimEnds = claimExpiresAt(refund);
  const claimLive = claimEnds !== null && claimEnds > now;
  const claimShort = claimLive && !claimLeavesTime(claimEnds, now);
  const holdEnds = holdUntilMs(refund);
  const recordedTx = recorded && recorded.txId !== UNKNOWN_TXID ? recorded.txId : null;
  const recordedAttempt = recordedTx ? (attempts ?? []).find((attempt) => attempt.tx_id === recordedTx) ?? null : null;
  /** The browser's own broadcast was proven not to pay this refund (invalid) or was abandoned after a revert. */
  const recordedFailed = recordedAttempt !== null && (recordedAttempt.status === "invalid" || recordedAttempt.status === "abandoned");
  const canPay = (refund.status === "requested" || refund.status === "paying") && !hasPending && attempts !== null && (!recorded || recordedFailed);
  /** Transaction mode: KasGate records the merchant-signed transaction, then broadcasts it; no claim needed. */
  const canPayWithTransaction = (refund.status === "requested" || refund.status === "paying") && !unverified && attempts !== null && (!recorded || recordedFailed);
  const canReject = (refund.status === "requested" || refund.status === "paying") && !hasPending && attempts !== null && !recorded;
  const rejectBlockedReason = hasPending ? "A payout attempt is still pending, so this refund can't be rejected." : recorded ? "A payout was already sent from this browser." : null;

  async function submit(body: { tx_id: string } | { transaction: string }, kind: "submit" | "transaction") {
    setBusy(kind);
    setError(null);
    try {
      const next = await apiFetch<PrepaidRefundRead>(`/api/prepaid/refunds/${refund.id}/payout`, { method: "POST", body });
      setMode(null);
      setTxInput("");
      setRawInput("");
      if (next.status === "paid") {
        forgetPayout(refund.id);
        setRecorded(null);
      }
      setNotice(
        next.status === "paid"
          ? next.late_paid_from
            ? `Late payout recorded: the refund was ${next.late_paid_from}, the payout is confirmed on-chain.`
            : "Payout seen on-chain. Refund paid."
          : "Payout sent and recorded. Waiting for the network to show it.",
      );
      onUpdated(next);
      await loadAttempts();
    } catch (e) {
      fail(e);
      await loadAttempts();
    } finally {
      setBusy(null);
    }
  }

  async function pay() {
    const already = recordedPayout(refund.id);
    if (already && !recordedFailed) {
      setRecorded(already);
      setError(
        already.txId === UNKNOWN_TXID
          ? "A payout for this refund was already sent from this browser, but KasWare returned no txid. Find it in KasWare's activity and use “Enter txid”. Don't pay twice."
          : "A payout for this refund was already sent from this browser. Submit that txid instead of paying again.",
      );
      return;
    }
    const previous = (attempts ?? []).filter((attempt) => attempt.status === "invalid" || attempt.status === "abandoned");
    if (previous.length > 0) {
      const lines = [
        `This refund already had ${previous.length} payout attempt${previous.length === 1 ? "" : "s"} that did not complete (${previous.map((a) => `${shortHash(a.tx_id)}: ${a.status}`).join(", ")}).`,
        "",
        "Before paying again, check KasWare's activity that none of them left your wallet. If one did, enter its txid instead.",
        "",
        "Pay again?",
      ];
      if (!window.confirm(lines.join("\n"))) return;
    }
    setBusy("pay");
    setError(null);
    setNotice(null);
    let claimRefused = false;
    const outcome = await sendRefundPayout(
      refund,
      merchant.kaspa_address,
      (text) => window.confirm(text),
      () => apiFetch<PrepaidRefundRead>(`/api/prepaid/refunds/${refund.id}/claim`, { method: "POST" }),
      (e) => {
        if (e instanceof ApiError && e.status === 401) onUnauthorized();
        claimRefused = e instanceof ApiError && e.code === "refund_not_claimable";
        return refundErrorMessage(e);
      },
    );
    if (outcome.refund) onUpdated(outcome.refund);
    if (outcome.kind === "claim_too_short") {
      setClaimBlocked(outcome.message);
      setBusy(null);
      return;
    }
    if (outcome.kind === "error") {
      if (claimRefused) setClaimBlocked(outcome.message);
      setError(outcome.message);
      setBusy(null);
      return;
    }
    if (outcome.kind === "sent_no_txid") {
      const entry = { txId: UNKNOWN_TXID, transaction: null };
      rememberPayout(refund.id, entry);
      setRecorded(entry);
      setMode("txid");
      setError("KasWare sent the payment but returned no readable txid. Find it in KasWare's activity and enter it below. Don't pay again.");
      setBusy(null);
      return;
    }
    const entry = { txId: outcome.txId, transaction: outcome.transaction };
    rememberPayout(refund.id, entry);
    setRecorded(entry);
    await submit({ tx_id: outcome.txId }, "submit");
  }

  async function reject(event: FormEvent) {
    event.preventDefault();
    const checked = checkRefundReason(reason);
    if (!checked.ok) {
      setError(checked.error);
      return;
    }
    setBusy("reject");
    setError(null);
    try {
      const next = await apiFetch<PrepaidRefundRead>(`/api/prepaid/refunds/${refund.id}/reject`, { method: "POST", body: { reason: checked.value } });
      setMode(null);
      setReason("");
      onUpdated(next);
    } catch (e) {
      fail(e);
      await loadAttempts();
    } finally {
      setBusy(null);
    }
  }

  function manualTx(event: FormEvent) {
    event.preventDefault();
    const checked = checkTxId(txInput);
    if (!checked.ok) {
      setError(checked.error);
      return;
    }
    if (lateEligible && !window.confirm(`This refund is ${refund.status}. Record ${shortHash(checked.value)} as a late payout? It is accepted only if it pays exactly ${refund.amount_sompi} sompi to ${refund.payout_address} with the refund payload.`)) {
      return;
    }
    void submit({ tx_id: checked.value }, "submit");
  }

  function signedTx(event: FormEvent) {
    event.preventDefault();
    const text = rawInput.trim() || storedTransaction || "";
    if (text.length < 2 || text.length > MAX_TRANSACTION_CHARS || !text.startsWith("{")) {
      setError("Paste the signed transaction JSON for this payout (it starts with “{”).");
      return;
    }
    try {
      JSON.parse(text);
    } catch {
      setError("That is not valid JSON. Paste the signed transaction exactly as your wallet or SDK produced it.");
      return;
    }
    void submit({ transaction: text }, "transaction");
  }

  const storedTransaction = unverified && recorded?.transaction && recorded.txId === unverified.tx_id ? recorded.transaction : null;
  const payDisabledReason = claimBlocked ? "The claim can't be renewed far enough ahead. Post a signed transaction instead, or reject." : undefined;
  /** Back to `requested` after a payout attempt (prepaid.refund.payout_reverted): an earlier attempt didn't complete. */
  const reverted = refund.status === "requested" && (attempts ?? []).length > 0;
  const showDetail = mode !== null || error || notice || (recordedTx && (open || lateEligible)) || showAttempts || unverified || hasPending || (claimBlocked && canPay);

  return (
    <>
      <tr className="align-top hover:bg-zinc-900/60" data-refund-id={refund.id} data-refund-status={refund.status}>
        <td className="td whitespace-nowrap font-mono text-xs">{formatDateTime(refund.created_at)}</td>
        <td className="td font-mono text-xs" title={refund.payer_address}>
          {shortAddress(refund.payer_address)}
        </td>
        <td className="td">
          <KasAmount sompi={refund.amount_sompi} tone="text-kas-300" />
        </td>
        <td className="td">
          <RefundStatusBadge status={refund.status} />
          <p className="mt-1 max-w-[280px] text-[11px] text-zinc-500">{REFUND_STATUS_HELP.merchant[refund.status]}</p>
          {claimEnds !== null ? (
            <p className={`mt-1 max-w-[280px] text-[11px] ${claimLive ? "text-violet-300" : "text-amber-300"}`} data-claim>
              {claimLive
                ? claimShort
                  ? `Claim: ${minutesLeft(claimEnds, now)} min left (until ${clockTime(claimEnds)} UTC). Too little to broadcast safely: paying renews the claim first, if renewals aren't used up (they stop 1 hour after the first claim).`
                  : `Claim: ${minutesLeft(claimEnds, now)} min left (until ${clockTime(claimEnds)} UTC). The buyer can't cancel meanwhile.`
                : `Claim lapsed at ${clockTime(claimEnds)} UTC: the buyer may cancel. Paying re-claims it first.`}
            </p>
          ) : null}
          {holdEnds !== null ? (
            <p className={`mt-1 max-w-[280px] text-[11px] ${holdEnds > now ? "text-violet-300" : "text-zinc-500"}`} data-hold-until>
              {holdEnds > now
                ? `Cooldown: the amount stays held until ${formatDateTime(refund.hold_until ?? "")} because you had claimed it. A payout that lands before then is still recorded and debited in full.`
                : `Cooldown ended ${formatDateTime(refund.hold_until ?? "")}: the hold is released once no payout attempt for this refund is still pending.`}
            </p>
          ) : null}
          {reverted ? (
            <p className="mt-1 max-w-[280px] text-[11px] font-medium text-amber-300" data-reverted>
              Payout reverted: the earlier payout attempt didn&apos;t complete, so the refund is open again. See Attempts.
            </p>
          ) : null}
          {refund.reason ? <p className="mt-1 max-w-[280px] text-[11px] text-zinc-400">Reason: {refund.reason}</p> : null}
          {refund.last_error && (open || lateEligible) ? <p className="mt-1 max-w-[280px] break-all text-[11px] text-amber-300/80">{refund.last_error}</p> : null}
          <LatePayoutNote refund={refund} />
        </td>
        <td className="td">{refund.tx_id ? txLink(refund.tx_id) : <span className="text-xs text-zinc-600">—</span>}</td>
        <td className="td">
          <div className="flex flex-wrap justify-end gap-2">
            {canPay ? (
              <button type="button" className="btn-primary px-3 py-1.5 text-xs" onClick={() => void pay()} disabled={busy !== null || claimBlocked !== null} title={payDisabledReason} data-pay-kasware>
                {busy === "pay" ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <Wallet className="h-3.5 w-3.5" aria-hidden />}
                {refund.status === "paying" ? "Pay with KasWare" : "Approve & pay with KasWare"}
              </button>
            ) : null}
            {recordedTx && !recordedFailed && (open || lateEligible) && !hasPending ? (
              <button type="button" className="btn-primary px-3 py-1.5 text-xs" onClick={() => void submit({ tx_id: recordedTx }, "submit")} disabled={busy !== null}>
                {busy === "submit" ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <Send className="h-3.5 w-3.5" aria-hidden />}
                Submit sent payout
              </button>
            ) : null}
            {unverified || canPayWithTransaction ? (
              <button
                type="button"
                className={`${unverified || claimBlocked ? "btn-primary" : "btn-secondary"} px-3 py-1.5 text-xs`}
                onClick={() => setMode(mode === "transaction" ? null : "transaction")}
                disabled={busy !== null}
                data-transaction-mode
              >
                <FileSignature className="h-3.5 w-3.5" aria-hidden />
                {unverified ? "Post signed transaction" : "Pay with signed transaction"}
              </button>
            ) : null}
            {refund.status === "payout_submitted" && refund.tx_id ? (
              <button type="button" className="btn-secondary px-3 py-1.5 text-xs" onClick={() => void submit({ tx_id: refund.tx_id ?? "" }, "submit")} disabled={busy !== null}>
                {busy === "submit" ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <RefreshCw className="h-3.5 w-3.5" aria-hidden />}
                Re-check payout
              </button>
            ) : null}
            {open || lateEligible ? (
              <button type="button" className="btn-ghost px-3 py-1.5 text-xs" onClick={() => setMode(mode === "txid" ? null : "txid")} disabled={busy !== null}>
                <Hash className="h-3.5 w-3.5" aria-hidden />
                {lateEligible ? "Record late payout" : refund.status === "payout_submitted" ? "Correct txid" : "Enter txid"}
              </button>
            ) : null}
            {refund.status === "requested" || refund.status === "paying" ? (
              <button
                type="button"
                className="btn-ghost px-3 py-1.5 text-xs text-red-300"
                onClick={() => setMode(mode === "reject" ? null : "reject")}
                disabled={busy !== null || !canReject}
                title={rejectBlockedReason ?? undefined}
              >
                <Ban className="h-3.5 w-3.5" aria-hidden />
                Reject
              </button>
            ) : null}
            <button type="button" className="btn-ghost px-3 py-1.5 text-xs" onClick={() => setShowAttempts((v) => !v)} aria-expanded={showAttempts}>
              <ListTree className="h-3.5 w-3.5" aria-hidden />
              Attempts{attempts && attempts.length > 0 ? ` (${attempts.length})` : ""}
            </button>
          </div>
        </td>
      </tr>
      {showDetail ? (
        <tr className="bg-zinc-950/40">
          <td className="td" colSpan={6}>
            <div className="space-y-2">
              {hasPending ? (
                <p className="text-xs text-sky-300" data-pending-note>
                  {pending.length === 1 ? "A payout attempt is" : `${pending.length} payout attempts are`} still being checked on-chain. Paying again and rejecting are
                  disabled until {pending.length === 1 ? "it resolves" : "they resolve"}; the buyer can&apos;t cancel meanwhile.
                </p>
              ) : null}
              {recordedTx && !recordedFailed && !hasPending && (open || lateEligible) ? (
                <p className="text-xs text-zinc-400">
                  Payout already broadcast from this browser: <span className="font-mono text-zinc-300">{recordedTx}</span>. Submit it rather than paying again.
                </p>
              ) : null}
              {recordedTx && recordedFailed ? (
                <p className="text-xs text-amber-300">
                  The payout sent from this browser ({shortHash(recordedTx)}) is {recordedAttempt?.status}: it did not pay this refund. Check KasWare before paying again.
                </p>
              ) : null}
              {unverified && mode !== "transaction" ? (
                <p className="text-xs text-amber-300" data-unverified-note>
                  The node accepted {shortHash(unverified.tx_id)} but can&apos;t show its payload, so KasGate can&apos;t prove it is this refund&apos;s payout. Post the
                  signed transaction to prove it.
                </p>
              ) : null}
              {claimBlocked && canPay ? (
                <p className="text-xs font-medium text-amber-300" data-claim-blocked>
                  {claimBlocked}
                </p>
              ) : null}
              {mode === "transaction" && (unverified || canPayWithTransaction) ? (
                <form onSubmit={signedTx} className="space-y-2" data-transaction-form>
                  <label htmlFor={`raw-${refund.id}`} className="block text-xs text-zinc-400">
                    {unverified
                      ? storedTransaction
                        ? `KasWare's signed transaction for ${shortHash(unverified.tx_id)} is saved in this browser and will be posted. Paste a different one only if you paid from elsewhere.`
                        : `Paste the signed transaction JSON for ${shortHash(unverified.tx_id)} (KasWare's sendKaspa result). It holds no keys.`
                      : `Paste a signed, not yet broadcast Kaspa transaction (kaspa SDK Transaction JSON) paying exactly ${refund.amount_sompi} sompi to ${refund.payout_address} with payload ${refund.payout_payload} (hex ${refund.payout_payload_hex}). KasGate records it first and then broadcasts it, so no claim is needed and the buyer can't cancel once it is recorded. KasWare's one-click Pay broadcasts as it signs; to use this mode, build and sign the transaction in a wallet or SDK that can export it. It holds no keys.`}
                  </label>
                  <textarea
                    id={`raw-${refund.id}`}
                    className="input min-h-[60px] w-full font-mono text-[11px]"
                    value={rawInput}
                    onChange={(e) => setRawInput(e.target.value)}
                    maxLength={MAX_TRANSACTION_CHARS}
                    placeholder={storedTransaction ? "Saved transaction will be used" : '{"id": "…", "inputs": […], "outputs": […], "payload": "…"}'}
                    spellCheck={false}
                  />
                  <button type="submit" className="btn-secondary px-3 py-1.5 text-xs" disabled={busy !== null || (!storedTransaction && rawInput.trim() === "")} data-transaction-submit>
                    {busy === "transaction" ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <FileSignature className="h-3.5 w-3.5" aria-hidden />}
                    Submit signed transaction
                  </button>
                </form>
              ) : null}
              {mode === "reject" && canReject ? (
                <form onSubmit={reject} className="flex flex-col gap-2 sm:flex-row sm:items-start">
                  <label htmlFor={`reject-${refund.id}`} className="sr-only">
                    Reason for rejecting this refund
                  </label>
                  <textarea
                    id={`reject-${refund.id}`}
                    className="input min-h-[40px] flex-1 text-xs"
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    maxLength={PREPAID_REFUND_REASON_MAX_LENGTH}
                    placeholder="Reason the buyer will see (required, up to 300 characters)"
                  />
                  <button type="submit" className="btn-secondary px-3 py-1.5 text-xs text-red-300" disabled={busy !== null}>
                    {busy === "reject" ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : null}
                    Reject refund
                  </button>
                </form>
              ) : null}
              {mode === "txid" ? (
                <form onSubmit={manualTx} className="flex flex-col gap-2 sm:flex-row sm:items-center">
                  <label htmlFor={`txid-${refund.id}`} className="text-xs text-zinc-400">
                    Txid of your payment of exactly {refund.amount_sompi} sompi to {shortAddress(refund.payout_address)}, carrying the refund payload
                  </label>
                  <input id={`txid-${refund.id}`} className="input flex-1 font-mono text-xs" value={txInput} onChange={(e) => setTxInput(e.target.value)} placeholder="64 hex characters" autoComplete="off" spellCheck={false} />
                  <button type="submit" className="btn-secondary px-3 py-1.5 text-xs" disabled={busy !== null}>
                    {busy === "submit" ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : null}
                    Submit txid
                  </button>
                </form>
              ) : null}
              {showAttempts ? attempts === null ? <Spinner label="Loading payout attempts…" /> : <AttemptsTable attempts={attempts} /> : null}
              {notice ? <p className="text-xs text-emerald-300">{notice}</p> : null}
              <ErrorBanner message={error} />
            </div>
          </td>
        </tr>
      ) : null}
    </>
  );
}

/** Prepaid refunds (CONTRACT v2.13): the approval queue, merchant-signed payouts via KasWare, and the refund/expiry ledger. */
export function RefundsPanel({ merchant, reloadKey, onMerchantChange, onUnauthorized }: { merchant: MerchantRead; reloadKey: number; onMerchantChange: (next: MerchantRead) => void; onUnauthorized: () => void }) {
  const [tab, setTab] = useState<Tab>("open");
  const [page, setPage] = useState(0);
  const [refunds, setRefunds] = useState<PrepaidRefundRead[]>([]);
  const [ledger, setLedger] = useState<PrepaidLedgerEntryRead[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [localReload, setLocalReload] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  /** Bumped on every list load so rows remount and drop notices that belonged to the previous state. */
  const [loadSeq, setLoadSeq] = useState(0);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 15_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    const offset = page * PAGE_SIZE;
    const load = async () => {
      if (tab === "ledger") {
        const rows = await apiFetch<PrepaidLedgerEntryRead[]>(`/api/prepaid/ledger?limit=${PAGE_SIZE}&offset=${offset}`);
        if (!cancelled) setLedger(rows);
        return;
      }
      if (tab === "open") {
        const lists = await Promise.all(OPEN_STATUSES.map((status) => apiFetch<PrepaidRefundRead[]>(`/api/prepaid/refunds?status=${status}&limit=200`)));
        if (!cancelled) {
          setRefunds(lists.flat().sort((a, b) => b.created_at.localeCompare(a.created_at)));
          setLoadSeq((n) => n + 1);
        }
        return;
      }
      const status = tab === "all" ? "" : `&status=${tab}`;
      const rows = await apiFetch<PrepaidRefundRead[]>(`/api/prepaid/refunds?limit=${PAGE_SIZE}&offset=${offset}${status}`);
      if (!cancelled) {
        setRefunds(rows);
        setLoadSeq((n) => n + 1);
      }
    };
    load()
      .catch((e: unknown) => {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 401) onUnauthorized();
        else setError(errorMessage(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tab, page, reloadKey, localReload, onUnauthorized]);

  const paged = tab !== "open";
  const rowsShown = tab === "ledger" ? ledger.length : refunds.length;
  const openCount = tab === "open" ? refunds.length : null;

  function updated(next: PrepaidRefundRead) {
    setRefunds((rows) => rows.map((row) => (row.id === next.id ? next : row)));
  }

  return (
    <section className="space-y-3" aria-labelledby="refunds-heading">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 id="refunds-heading" className="text-lg font-medium text-zinc-100">
            Refund requests {openCount !== null && openCount > 0 ? <span className="badge ml-1 border-amber-800 bg-amber-950/50 text-amber-300">{openCount} open</span> : null}
          </h2>
          <p className="max-w-3xl text-xs text-zinc-500">
            Buyers can ask for unused prepaid credit back. You pay it from your own wallet (KasWare signs; KasGate never holds keys or coins), and no platform fee
            applies. The buyer&apos;s balance is debited only once the payout is seen on-chain.
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div role="tablist" aria-label="Refund views" className="flex flex-wrap gap-1">
            {TABS.map((item) => (
              <button
                key={item.tab}
                type="button"
                role="tab"
                aria-selected={tab === item.tab}
                className={tab === item.tab ? "btn-secondary px-3 py-1.5 text-xs" : "btn-ghost px-3 py-1.5 text-xs"}
                onClick={() => {
                  setTab(item.tab);
                  setPage(0);
                }}
              >
                {item.label}
              </button>
            ))}
          </div>
          <button type="button" className="btn-secondary px-2 py-1.5" onClick={() => setLocalReload((k) => k + 1)} disabled={loading} aria-label="Reload refunds">
            <RefreshCw className="h-4 w-4" aria-hidden />
          </button>
          {paged ? (
            <>
              <button type="button" className="btn-secondary px-2 py-1.5" onClick={() => setPage((p) => Math.max(0, p - 1))} disabled={page === 0 || loading} aria-label="Previous page">
                <ChevronLeft className="h-4 w-4" aria-hidden />
              </button>
              <span className="font-mono text-xs text-zinc-400">page {page + 1}</span>
              <button type="button" className="btn-secondary px-2 py-1.5" onClick={() => setPage((p) => p + 1)} disabled={rowsShown < PAGE_SIZE || loading} aria-label="Next page">
                <ChevronRight className="h-4 w-4" aria-hidden />
              </button>
            </>
          ) : null}
        </div>
      </div>
      <ErrorBanner message={error} />
      <div className="card overflow-x-auto">
        {tab === "ledger" ? (
          <table className="w-full min-w-[860px]">
            <thead>
              <tr className="border-b border-zinc-800">
                <th className="th">When (UTC)</th>
                <th className="th">Buyer</th>
                <th className="th">Kind</th>
                <th className="th text-right">Amount</th>
                <th className="th text-right">Balance after</th>
                <th className="th">Tx / note</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800/70">
              {ledger.map((entry) => (
                <tr key={entry.id} className="hover:bg-zinc-900/60">
                  <td className="td whitespace-nowrap font-mono text-xs">{formatDateTime(entry.created_at)}</td>
                  <td className="td font-mono text-xs" title={entry.payer_address}>
                    {shortAddress(entry.payer_address)}
                  </td>
                  <td className="td">
                    <span className={`badge ${entry.kind === "refund" ? "border-sky-800 bg-sky-950/50 text-sky-300" : "border-zinc-700 bg-zinc-900 text-zinc-300"}`}>
                      {entry.kind === "refund" ? "refund payout" : "credit expired"}
                    </span>
                    <div className="mt-1 font-mono text-[11px] text-zinc-600">{entry.direction}</div>
                  </td>
                  <td className="td">
                    <KasAmount sompi={entry.amount_sompi} tone={entry.direction === "debit" ? "text-amber-300" : "text-emerald-300"} />
                  </td>
                  <td className="td">
                    <KasAmount sompi={entry.balance_after_sompi} />
                  </td>
                  <td className="td">
                    {entry.tx_id ? txLink(entry.tx_id) : null}
                    {entry.note ? <div className="text-[11px] text-zinc-500">{entry.note}</div> : null}
                    {!entry.tx_id && !entry.note ? <span className="text-xs text-zinc-600">—</span> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <table className="w-full min-w-[980px]">
            <thead>
              <tr className="border-b border-zinc-800">
                <th className="th">Requested (UTC)</th>
                <th className="th">Buyer (payout address)</th>
                <th className="th text-right">Amount</th>
                <th className="th">Status</th>
                <th className="th">Payout tx</th>
                <th className="th text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800/70">
              {refunds.map((refund) => (
                <RefundRow key={`${refund.id}:${loadSeq}`} refund={refund} merchant={merchant} now={now} onUpdated={updated} onUnauthorized={onUnauthorized} />
              ))}
            </tbody>
          </table>
        )}
        {!loading && rowsShown === 0 ? (
          <div className="td py-8 text-center text-zinc-500">
            {tab === "ledger" ? "No refund payouts or expiries yet." : tab === "open" ? "No open refund requests." : "No refunds in this view."}
          </div>
        ) : null}
        {loading ? (
          <div className="border-t border-zinc-800 px-4 py-3">
            <Spinner label="Loading refunds…" />
          </div>
        ) : null}
      </div>
      <CreditExpirySettings merchant={merchant} onSaved={onMerchantChange} onUnauthorized={onUnauthorized} />
    </section>
  );
}
