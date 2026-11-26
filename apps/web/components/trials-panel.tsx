"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { apiFetch, errorMessage } from "@/lib/api-client";
import { formatDateTime, shortAddress } from "@/lib/format";
import type { EndpointRead, TrialCallRead } from "@/lib/types";
import { ErrorBanner, Spinner } from "./ui";

const PAGE_SIZE = 25;

/** Free trial calls served by the merchant's endpoints (CONTRACT v2.12). Trial calls move no coins and carry no fee. */
export function TrialsPanel({ endpoints, reloadKey }: { endpoints: EndpointRead[]; reloadKey: number }) {
  const [page, setPage] = useState(0);
  const [rows, setRows] = useState<TrialCallRead[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    apiFetch<TrialCallRead[]>(`/api/trials/calls?limit=${PAGE_SIZE}&offset=${page * PAGE_SIZE}`)
      .then((data) => {
        if (!cancelled) setRows(data);
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
  const offering = endpoints.filter((e) => e.free_trial_calls > 0);

  return (
    <section className="space-y-3" aria-labelledby="trials-heading">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 id="trials-heading" className="text-lg font-medium text-zinc-100">
            Free trial calls
          </h2>
          <p className="text-xs text-zinc-500">
            {offering.length > 0
              ? `${offering.length} of your endpoints offer free calls. Each buyer wallet proves itself with a signature; calls past the limit or the daily cap get your normal price. Failed upstream calls (5xx) are given back.`
              : "Set “Free trial calls per buyer” on an endpoint to let new buyers try it before paying. No platform fee is charged on trial calls."}
          </p>
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
        <table className="w-full min-w-[720px]">
          <thead>
            <tr className="border-b border-zinc-800">
              <th className="th">Time (UTC)</th>
              <th className="th">Endpoint</th>
              <th className="th">Buyer</th>
              <th className="th">Request</th>
              <th className="th">Upstream</th>
              <th className="th">Status</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-800/70">
            {rows.map((row) => (
              <tr key={row.id} className="hover:bg-zinc-900/60">
                <td className="td font-mono text-xs text-zinc-400">{formatDateTime(row.created_at)}</td>
                <td className="td font-mono text-xs text-zinc-200">{slugById.get(row.endpoint_id) ?? "—"}</td>
                <td className="td font-mono text-xs" title={row.payer_address}>
                  {shortAddress(row.payer_address)}
                </td>
                <td className="td max-w-[260px] truncate font-mono text-xs text-zinc-400" title={`${row.request_method} ${row.request_path}`}>
                  {row.request_method} {row.request_path}
                </td>
                <td className="td font-mono text-xs text-zinc-400">{row.upstream_status_code ?? "—"}</td>
                <td className="td">
                  <span className={`badge ${row.status === "used" ? "border-emerald-800 bg-emerald-950/50 text-emerald-300" : "border-zinc-700 bg-zinc-900 text-zinc-400"}`}>
                    {row.status === "used" ? "free call" : "given back"}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {loading ? <Spinner label="Loading trial calls…" /> : null}
        {!loading && rows.length === 0 ? <p className="px-4 py-6 text-center text-sm text-zinc-500">No trial calls yet.</p> : null}
      </div>
    </section>
  );
}
