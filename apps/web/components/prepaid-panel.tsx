"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { apiFetch, errorMessage } from "@/lib/api-client";
import { formatDateTime, formatUsd, shortAddress, shortHash } from "@/lib/format";
import { formatKas, formatSompi } from "@/lib/sompi";
import type { EndpointRead, PrepaidBalanceRead, PrepaidDebitRead, PrepaidDepositRead, PrepaidLedgerKind } from "@/lib/types";
import { ErrorBanner, Spinner } from "./ui";

const PAGE_SIZE = 25;
const TABS: { kind: PrepaidLedgerKind; label: string }[] = [
  { kind: "balances", label: "Balances" },
  { kind: "deposits", label: "Deposits" },
  { kind: "debits", label: "Debits" },
];

type Rows =
  | { kind: "balances"; rows: PrepaidBalanceRead[] }
  | { kind: "deposits"; rows: PrepaidDepositRead[] }
  | { kind: "debits"; rows: PrepaidDebitRead[] };

function Kas({ sompi, tone = "text-zinc-200" }: { sompi: string; tone?: string }) {
  return (
    <td className="td text-right">
      <div className={`font-mono text-xs ${tone}`}>{formatKas(BigInt(sompi))} KAS</div>
      <div className="font-mono text-[11px] text-zinc-600">{formatSompi(BigInt(sompi))} sompi</div>
    </td>
  );
}

/** Prepaid credit held for buyers (CONTRACT v2.11): balances owed, on-chain deposits, and per-call debits. */
export function PrepaidPanel({ endpoints, reloadKey }: { endpoints: EndpointRead[]; reloadKey: number }) {
  const [kind, setKind] = useState<PrepaidLedgerKind>("balances");
  const [page, setPage] = useState(0);
  const [data, setData] = useState<Rows>({ kind: "balances", rows: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    apiFetch<unknown[]>(`/api/prepaid/${kind}?limit=${PAGE_SIZE}&offset=${page * PAGE_SIZE}`)
      .then((rows) => {
        if (!cancelled) setData({ kind, rows } as Rows);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(errorMessage(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [kind, page, reloadKey]);

  const slugById = useMemo(() => new Map(endpoints.map((e) => [e.id, e.slug])), [endpoints]);
  const empty = !loading && data.kind === kind && data.rows.length === 0;

  return (
    <section className="space-y-3" aria-labelledby="prepaid-heading">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 id="prepaid-heading" className="text-lg font-medium text-zinc-100">
            Prepaid credit
          </h2>
          <p className="text-xs text-zinc-500">
            Deposits settle on-chain to your address (you receive the deposit minus the platform fee); balances are what buyers can still spend on your
            endpoints. Refunded debits (upstream failed) are returned to the buyer&apos;s balance.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div role="tablist" className="flex gap-1">
            {TABS.map((tab) => (
              <button
                key={tab.kind}
                type="button"
                role="tab"
                aria-selected={kind === tab.kind}
                className={kind === tab.kind ? "btn-secondary px-3 py-1.5 text-xs" : "btn-ghost px-3 py-1.5 text-xs"}
                onClick={() => {
                  setKind(tab.kind);
                  setPage(0);
                }}
              >
                {tab.label}
              </button>
            ))}
          </div>
          <button type="button" className="btn-secondary px-2 py-1.5" onClick={() => setPage((p) => Math.max(0, p - 1))} disabled={page === 0 || loading} aria-label="Previous page">
            <ChevronLeft className="h-4 w-4" aria-hidden />
          </button>
          <span className="font-mono text-xs text-zinc-400">page {page + 1}</span>
          <button type="button" className="btn-secondary px-2 py-1.5" onClick={() => setPage((p) => p + 1)} disabled={data.rows.length < PAGE_SIZE || loading} aria-label="Next page">
            <ChevronRight className="h-4 w-4" aria-hidden />
          </button>
        </div>
      </div>
      <ErrorBanner message={error} />
      <div className="card overflow-x-auto">
        {data.kind === "balances" ? (
          <table className="w-full min-w-[720px]">
            <thead>
              <tr className="border-b border-zinc-800">
                <th className="th">Buyer</th>
                <th className="th text-right">Balance</th>
                <th className="th text-right">Deposited (credit)</th>
                <th className="th text-right">Debited</th>
                <th className="th">Updated (UTC)</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800/70">
              {data.rows.map((row) => (
                <tr key={row.payer_address} className="hover:bg-zinc-900/60">
                  <td className="td font-mono text-xs" title={row.payer_address}>
                    {shortAddress(row.payer_address)}
                  </td>
                  <Kas sompi={row.balance_sompi} tone="text-kas-300" />
                  <Kas sompi={row.deposited_sompi} />
                  <Kas sompi={row.debited_sompi} />
                  <td className="td whitespace-nowrap font-mono text-xs">{row.updated_at ? formatDateTime(row.updated_at) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        {data.kind === "deposits" ? (
          <table className="w-full min-w-[820px]">
            <thead>
              <tr className="border-b border-zinc-800">
                <th className="th">Deposited (UTC)</th>
                <th className="th">Buyer</th>
                <th className="th">Tx</th>
                <th className="th text-right">Deposit</th>
                <th className="th text-right">Credit (you received)</th>
                <th className="th text-right">Platform fee</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800/70">
              {data.rows.map((row) => (
                <tr key={row.id} className="hover:bg-zinc-900/60">
                  <td className="td whitespace-nowrap font-mono text-xs">{formatDateTime(row.created_at)}</td>
                  <td className="td font-mono text-xs" title={row.payer_address}>
                    {shortAddress(row.payer_address)}
                  </td>
                  <td className="td font-mono text-xs" title={row.tx_id}>
                    {shortHash(row.tx_id)}
                  </td>
                  <Kas sompi={row.deposit_sompi} />
                  <Kas sompi={row.credit_sompi} tone="text-kas-300" />
                  <Kas sompi={row.fee_sompi} tone="text-amber-300" />
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        {data.kind === "debits" ? (
          <table className="w-full min-w-[900px]">
            <thead>
              <tr className="border-b border-zinc-800">
                <th className="th">Called (UTC)</th>
                <th className="th">Endpoint</th>
                <th className="th">Buyer</th>
                <th className="th">Status</th>
                <th className="th text-right">Amount</th>
                <th className="th text-right">USD at debit</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-800/70">
              {data.rows.map((row) => (
                <tr key={row.id} className="hover:bg-zinc-900/60">
                  <td className="td whitespace-nowrap font-mono text-xs">{formatDateTime(row.created_at)}</td>
                  <td className="td">
                    <div className="font-mono text-xs text-zinc-300">{slugById.get(row.endpoint_id) ?? shortHash(row.endpoint_id, 8, 4)}</div>
                    <div className="font-mono text-[11px] text-zinc-600">
                      {row.request_method} {row.request_path.length > 28 ? `${row.request_path.slice(0, 28)}…` : row.request_path}
                    </div>
                  </td>
                  <td className="td font-mono text-xs" title={row.payer_address}>
                    {shortAddress(row.payer_address)}
                  </td>
                  <td className="td">
                    <span
                      className={`badge ${row.status === "debited" ? "border-emerald-800 bg-emerald-950/50 text-emerald-300" : "border-zinc-700 bg-zinc-900 text-zinc-400"}`}
                    >
                      {row.status}
                    </span>
                    {row.upstream_status_code !== null ? <div className="mt-1 font-mono text-[11px] text-zinc-600">upstream {row.upstream_status_code}</div> : null}
                  </td>
                  <Kas sompi={row.amount_sompi} />
                  <td className="td text-right font-mono text-xs">{row.usd_value ? formatUsd(row.usd_value) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : null}
        {empty ? <div className="td py-8 text-center text-zinc-500">Nothing here yet. Buyers fund balances with POST /v1/prepaid/deposits.</div> : null}
        {loading ? (
          <div className="border-t border-zinc-800 px-4 py-3">
            <Spinner label="Loading prepaid ledger…" />
          </div>
        ) : null}
      </div>
    </section>
  );
}
