"use client";

import { Ban, ChevronLeft, ChevronRight, Hash, Loader2, RefreshCw, Send, Wallet } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { ApiError, apiFetch, errorMessage } from "@/lib/api-client";
import { IS_TESTNET, TESTNET_EXPLORER_URL } from "@/lib/config";
import { formatDateTime, shortAddress, shortHash } from "@/lib/format";
import { forgetPayout, recordedPayout, rememberPayout, sendRefundPayout, UNKNOWN_TXID } from "@/lib/refund-payout";
import { PREPAID_REFUND_REASON_MAX_LENGTH, type MerchantRead, type PrepaidLedgerEntryRead, type PrepaidRefundRead, type PrepaidRefundStatus } from "@/lib/types";
import { checkRefundReason, checkTxId } from "@/lib/validation";
import { CreditExpirySettings } from "./credit-expiry-settings";
import { KasAmount } from "./kas-amount";
import { REFUND_STATUS_HELP, RefundStatusBadge } from "./refund-status";
import { ErrorBanner, Spinner } from "./ui";

const PAGE_SIZE = 25;
type Tab = "open" | PrepaidRefundStatus | "all" | "ledger";
const TABS: { tab: Tab; label: string }[] = [
  { tab: "open", label: "Open" },
  { tab: "paid", label: "Paid" },
  { tab: "rejected", label: "Rejected" },
  { tab: "cancelled", label: "Cancelled" },
  { tab: "all", label: "All" },
  { tab: "ledger", label: "Ledger" },
];

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

type RowMode = null | "reject" | "txid";

function RefundRow({ refund, merchant, onUpdated, onUnauthorized }: { refund: PrepaidRefundRead; merchant: MerchantRead; onUpdated: (next: PrepaidRefundRead) => void; onUnauthorized: () => void }) {
  const [mode, setMode] = useState<RowMode>(null);
  const [busy, setBusy] = useState<null | "pay" | "submit" | "reject">(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [txInput, setTxInput] = useState("");
  const [recorded, setRecorded] = useState<string | null>(null);
  const open = refund.status === "requested" || refund.status === "payout_submitted";

  useEffect(() => {
    setRecorded(open ? recordedPayout(refund.id) : null);
    if (!open) forgetPayout(refund.id);
  }, [open, refund.id]);

  const fail = useCallback(
    (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) {
        onUnauthorized();
        return;
      }
      setError(errorMessage(e));
    },
    [onUnauthorized],
  );

  async function submitTx(txId: string) {
    setBusy("submit");
    setError(null);
    try {
      const next = await apiFetch<PrepaidRefundRead>(`/api/prepaid/refunds/${refund.id}/payout`, { method: "POST", body: { tx_id: txId } });
      forgetPayout(refund.id);
      setRecorded(null);
      setMode(null);
      setTxInput("");
      setNotice(next.status === "paid" ? "Payout seen on-chain. Refund paid." : "Payout sent and recorded. Waiting for the network to show it.");
      onUpdated(next);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(null);
    }
  }

  async function pay() {
    const already = recordedPayout(refund.id);
    if (already) {
      setRecorded(already);
      setError(
        already === UNKNOWN_TXID
          ? "A payout for this refund was already sent from this browser, but KasWare returned no txid. Find it in KasWare's activity and use “Enter txid”. Don't pay twice."
          : "A payout for this refund was already sent from this browser. Submit that txid instead of paying again.",
      );
      return;
    }
    setBusy("pay");
    setError(null);
    setNotice(null);
    const outcome = await sendRefundPayout(refund, merchant.kaspa_address, (text) => window.confirm(text));
    if (outcome.kind === "error") {
      setError(outcome.message);
      setBusy(null);
      return;
    }
    if (outcome.kind === "sent_no_txid") {
      rememberPayout(refund.id, UNKNOWN_TXID);
      setRecorded(UNKNOWN_TXID);
      setMode("txid");
      setError("KasWare sent the payment but returned no readable txid. Find it in KasWare's activity and enter it below. Don't pay again.");
      setBusy(null);
      return;
    }
    rememberPayout(refund.id, outcome.txId);
    setRecorded(outcome.txId);
    await submitTx(outcome.txId);
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
    void submitTx(checked.value);
  }

  const recordedTx = recorded && recorded !== UNKNOWN_TXID ? recorded : null;

  return (
    <>
      <tr className="align-top hover:bg-zinc-900/60" data-refund-id={refund.id}>
        <td className="td whitespace-nowrap font-mono text-xs">{formatDateTime(refund.created_at)}</td>
        <td className="td font-mono text-xs" title={refund.payer_address}>
          {shortAddress(refund.payer_address)}
        </td>
        <td className="td">
          <KasAmount sompi={refund.amount_sompi} tone="text-kas-300" />
        </td>
        <td className="td">
          <RefundStatusBadge status={refund.status} />
          <p className="mt-1 max-w-[260px] text-[11px] text-zinc-500">{REFUND_STATUS_HELP.merchant[refund.status]}</p>
          {refund.reason ? <p className="mt-1 max-w-[260px] text-[11px] text-zinc-400">Reason: {refund.reason}</p> : null}
          {refund.last_error && open ? <p className="mt-1 max-w-[260px] break-all text-[11px] text-amber-300/80">{refund.last_error}</p> : null}
        </td>
        <td className="td">{refund.tx_id ? txLink(refund.tx_id) : <span className="text-xs text-zinc-600">—</span>}</td>
        <td className="td">
          <div className="flex flex-wrap justify-end gap-2">
            {refund.status === "requested" && !recorded ? (
              <button type="button" className="btn-primary px-3 py-1.5 text-xs" onClick={() => void pay()} disabled={busy !== null}>
                {busy === "pay" ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <Wallet className="h-3.5 w-3.5" aria-hidden />}
                Approve &amp; pay with KasWare
              </button>
            ) : null}
            {recordedTx && open ? (
              <button type="button" className="btn-primary px-3 py-1.5 text-xs" onClick={() => void submitTx(recordedTx)} disabled={busy !== null}>
                {busy === "submit" ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <Send className="h-3.5 w-3.5" aria-hidden />}
                Submit sent payout
              </button>
            ) : null}
            {refund.status === "payout_submitted" && refund.tx_id ? (
              <button type="button" className="btn-secondary px-3 py-1.5 text-xs" onClick={() => void submitTx(refund.tx_id ?? "")} disabled={busy !== null}>
                {busy === "submit" ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : <RefreshCw className="h-3.5 w-3.5" aria-hidden />}
                Re-check payout
              </button>
            ) : null}
            {open ? (
              <button type="button" className="btn-ghost px-3 py-1.5 text-xs" onClick={() => setMode(mode === "txid" ? null : "txid")} disabled={busy !== null}>
                <Hash className="h-3.5 w-3.5" aria-hidden />
                {refund.status === "payout_submitted" ? "Correct txid" : "Enter txid"}
              </button>
            ) : null}
            {refund.status === "requested" && !recorded ? (
              <button type="button" className="btn-ghost px-3 py-1.5 text-xs text-red-300" onClick={() => setMode(mode === "reject" ? null : "reject")} disabled={busy !== null}>
                <Ban className="h-3.5 w-3.5" aria-hidden />
                Reject
              </button>
            ) : null}
          </div>
        </td>
      </tr>
      {mode !== null || error || notice || recordedTx ? (
        <tr className="bg-zinc-950/40">
          <td className="td" colSpan={6}>
            <div className="space-y-2">
              {recordedTx && open ? (
                <p className="text-xs text-zinc-400">
                  Payout already broadcast from this browser: <span className="font-mono text-zinc-300">{recordedTx}</span>. Submit it rather than paying again.
                </p>
              ) : null}
              {mode === "reject" ? (
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
                    Txid of your payment of exactly {refund.amount_sompi} sompi to {shortAddress(refund.payout_address)}
                  </label>
                  <input id={`txid-${refund.id}`} className="input flex-1 font-mono text-xs" value={txInput} onChange={(e) => setTxInput(e.target.value)} placeholder="64 hex characters" autoComplete="off" spellCheck={false} />
                  <button type="submit" className="btn-secondary px-3 py-1.5 text-xs" disabled={busy !== null}>
                    {busy === "submit" ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : null}
                    Submit txid
                  </button>
                </form>
              ) : null}
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
        const [requested, submitted] = await Promise.all([
          apiFetch<PrepaidRefundRead[]>("/api/prepaid/refunds?status=requested&limit=200"),
          apiFetch<PrepaidRefundRead[]>("/api/prepaid/refunds?status=payout_submitted&limit=200"),
        ]);
        if (!cancelled) setRefunds([...requested, ...submitted].sort((a, b) => b.created_at.localeCompare(a.created_at)));
        return;
      }
      const status = tab === "all" ? "" : `&status=${tab}`;
      const rows = await apiFetch<PrepaidRefundRead[]>(`/api/prepaid/refunds?limit=${PAGE_SIZE}&offset=${offset}${status}`);
      if (!cancelled) setRefunds(rows);
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
                <RefundRow key={refund.id} refund={refund} merchant={merchant} onUpdated={updated} onUnauthorized={onUnauthorized} />
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
