"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { apiFetch, errorMessage } from "@/lib/api-client";
import { formatDateTime, formatUsd, shortAddress, shortHash } from "@/lib/format";
import { formatKas, formatSompi } from "@/lib/sompi";
import type { EndpointRead, SettlementRead } from "@/lib/types";
import { CopyButton } from "./copy-button";
import { ErrorBanner, Spinner, StatusBadge } from "./ui";

const PAGE_SIZE = 25;

export function SettlementsPanel({ endpoints, reloadKey }: { endpoints: EndpointRead[]; reloadKey: number }) {
  const [page, setPage] = useState(0);
  const [rows, setRows] = useState<SettlementRead[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    apiFetch<SettlementRead[]>(`/api/settlements?limit=${PAGE_SIZE}&offset=${page * PAGE_SIZE}`)
      .then((result) => {
        if (!cancelled) setRows(result);
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
  }, [page, reloadKey]);

  const slugById = useMemo(() => new Map(endpoints.map((e) => [e.id, e.slug])), [endpoints]);

  return (
    <section className="space-y-3" aria-labelledby="settlements-heading">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 id="settlements-heading" className="text-lg font-medium text-zinc-100">
            Settlements ledger
          </h2>
          <p className="text-xs text-zinc-500">Every row shows KAS and sompi, plus the USD value fixed at the moment of settlement (tax basis).</p>
        </div>
        <div className="flex items-center gap-2">
          <button type="button" className="btn-secondary px-2 py-1.5" onClick={() => setPage((p) => Math.max(0, p - 1))} disabled={page === 0 || loading} aria-label="Previous page">
            <ChevronLeft className="h-4 w-4" aria-hidden />
          </button>
          <span className="font-mono text-xs text-zinc-400">page {page + 1}</span>
          <button type="button" className="btn-secondary px-2 py-1.5" onClick={() => setPage((p) => p + 1)} disabled={rows.length < PAGE_SIZE || loading} aria-label="Next page">
            <ChevronRight className="h-4 w-4" aria-hidden />
          </button>
        </div>
      </div>
      <ErrorBanner message={error} />
      <div className="card overflow-x-auto">
        <table className="w-full min-w-[1000px]">
          <thead>
            <tr className="border-b border-zinc-800">
              <th className="th">Settled (UTC)</th>
              <th className="th">Endpoint</th>
              <th className="th">Tx</th>
              <th className="th">Status</th>
              <th className="th text-right">Gross</th>
              <th className="th text-right">You received</th>
              <th className="th text-right">Platform fee</th>
              <th className="th text-right">KAS/USD rate</th>
              <th className="th text-right">USD at settlement</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800/70">
            {rows.map((row) => (
              <tr key={row.id} className="hover:bg-zinc-900/60">
                <td className="td whitespace-nowrap font-mono text-xs">{formatDateTime(row.settled_at ?? row.created_at)}</td>
                <td className="td">
                  <div className="font-mono text-xs text-zinc-300">{slugById.get(row.endpoint_id) ?? shortHash(row.endpoint_id, 8, 4)}</div>
                  <div className="font-mono text-[11px] text-zinc-600">
                    {row.request_method} {row.request_path.length > 28 ? `${row.request_path.slice(0, 28)}…` : row.request_path}
                  </div>
                </td>
                <td className="td">
                  <div className="flex items-center gap-1 font-mono text-xs text-zinc-300">
                    <span title={row.tx_id}>{shortHash(row.tx_id)}</span>
                    <CopyButton value={row.tx_id} label="" className="!px-1.5 !py-1" />
                  </div>
                  <div className="font-mono text-[11px] text-zinc-600" title={row.payer_address}>
                    from {shortAddress(row.payer_address)}
                  </div>
                </td>
                <td className="td">
                  <StatusBadge status={row.status} />
                  {row.failure_reason ? <div className="mt-1 max-w-[160px] text-[11px] text-red-400">{row.failure_reason}</div> : null}
                </td>
                <AmountCell sompi={row.amount_sompi} />
                <AmountCell sompi={row.merchant_amount_sompi} tone="text-kas-300" />
                <AmountCell sompi={row.fee_amount_sompi} tone="text-amber-300" />
                <td className="td text-right font-mono text-xs">{row.kas_usd_rate ? formatUsd(row.kas_usd_rate, 4) : "—"}</td>
                <td className="td text-right font-mono text-xs">
                  {row.usd_value_at_settlement ? formatUsd(row.usd_value_at_settlement) : <span className="text-zinc-600">not settled</span>}
                </td>
              </tr>
            ))}
            {!loading && rows.length === 0 ? (
              <tr>
                <td colSpan={9} className="td py-8 text-center text-zinc-500">
                  No settlements yet. They appear here as soon as a buyer pays for a call.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
        {loading ? (
          <div className="border-t border-zinc-800 px-4 py-3">
            <Spinner label="Loading settlements…" />
          </div>
        ) : null}
      </div>
    </section>
  );
}

function AmountCell({ sompi, tone = "text-zinc-200" }: { sompi: string; tone?: string }) {
  return (
    <td className="td text-right font-mono text-xs">
      <div className={tone}>{formatKas(sompi)} KAS</div>
      <div className="text-zinc-600">{formatSompi(sompi)} sompi</div>
    </td>
  );
}
