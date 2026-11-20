"use client";

import { Activity, Coins, Receipt, Wallet } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { apiFetch, errorMessage } from "@/lib/api-client";
import { formatKas, formatSompi, sompiToKasNumber } from "@/lib/sompi";
import { formatUsd } from "@/lib/format";
import type { DailyRevenue, MerchantAnalytics } from "@/lib/types";
import { rangeToQuery, type DateRange } from "@/lib/range";
import { ErrorBanner, Spinner } from "./ui";

export function AnalyticsPanel({ range, reloadKey }: { range: DateRange; reloadKey: number }) {
  const [data, setData] = useState<MerchantAnalytics | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const query = rangeToQuery(range);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    apiFetch<MerchantAnalytics>(`/api/analytics?${query}`)
      .then((result) => {
        if (!cancelled) setData(result);
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
  }, [query, reloadKey]);

  return (
    <section className="space-y-4" aria-labelledby="analytics-heading">
      <h2 id="analytics-heading" className="text-lg font-medium text-zinc-100">
        Analytics
      </h2>
      <ErrorBanner message={error} />
      {loading && !data ? <Spinner label="Loading analytics…" /> : null}
      {data ? (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Stat icon={<Activity className="h-4 w-4" />} label="Paid calls" primary={data.total_calls.toLocaleString("en-US")} />
            <Stat icon={<Coins className="h-4 w-4" />} label="Gross volume" primary={`${formatKas(data.gross_sompi)} KAS`} secondary={`${formatSompi(data.gross_sompi)} sompi`} />
            <Stat icon={<Wallet className="h-4 w-4" />} label="Your revenue" primary={`${formatKas(data.merchant_sompi)} KAS`} secondary={`${formatSompi(data.merchant_sompi)} sompi`} tone="text-kas-300" />
            <Stat icon={<Receipt className="h-4 w-4" />} label="Gross USD at settlement" primary={formatUsd(data.usd_value)} secondary={`of ${formatKas(data.gross_sompi)} KAS gross (your share ${formatKas(data.merchant_sompi)} KAS, fee ${formatKas(data.fee_sompi)} KAS)`} />
          </div>

          <div className="card p-4">
            <h3 className="mb-3 text-sm font-medium text-zinc-300">Daily revenue (KAS, your share)</h3>
            <RevenueChart daily={data.daily} />
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <div className="card overflow-x-auto">
              <h3 className="px-4 pt-4 text-sm font-medium text-zinc-300">By day</h3>
              <table className="mt-2 w-full">
                <thead>
                  <tr className="border-b border-zinc-800">
                    <th className="th">Day (UTC)</th>
                    <th className="th text-right">Calls</th>
                    <th className="th text-right">Revenue KAS</th>
                    <th className="th text-right" title="USD value of the gross payment at settlement time, not of your share only">Gross USD</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-800/70">
                  {[...data.daily].reverse().map((d) => (
                    <tr key={d.day}>
                      <td className="td font-mono text-xs">{d.day}</td>
                      <td className="td text-right font-mono text-xs">{d.call_count}</td>
                      <td className="td text-right font-mono text-xs">
                        {formatKas(d.merchant_sompi)}
                        <div className="text-zinc-600">{formatSompi(d.merchant_sompi)} sompi</div>
                      </td>
                      <td className="td text-right font-mono text-xs">{formatUsd(d.usd_value)}</td>
                    </tr>
                  ))}
                  {data.daily.length === 0 ? (
                    <tr>
                      <td colSpan={4} className="td text-center text-zinc-500">
                        No settled calls in this period.
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </div>
            <div className="card overflow-x-auto">
              <h3 className="px-4 pt-4 text-sm font-medium text-zinc-300">By endpoint</h3>
              <table className="mt-2 w-full">
                <thead>
                  <tr className="border-b border-zinc-800">
                    <th className="th">Endpoint</th>
                    <th className="th text-right">Calls</th>
                    <th className="th text-right">Revenue KAS</th>
                    <th className="th text-right" title="USD value of the gross payment at settlement time, not of your share only">Gross USD</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-zinc-800/70">
                  {data.by_endpoint.map((e) => (
                    <tr key={e.endpoint_id}>
                      <td className="td">
                        <div className="text-sm text-zinc-200">{e.name}</div>
                        <div className="font-mono text-xs text-zinc-500">{e.slug}</div>
                      </td>
                      <td className="td text-right font-mono text-xs">{e.call_count}</td>
                      <td className="td text-right font-mono text-xs">
                        {formatKas(e.merchant_sompi)}
                        <div className="text-zinc-600">{formatSompi(e.merchant_sompi)} sompi</div>
                      </td>
                      <td className="td text-right font-mono text-xs">{formatUsd(e.usd_value)}</td>
                    </tr>
                  ))}
                  {data.by_endpoint.length === 0 ? (
                    <tr>
                      <td colSpan={4} className="td text-center text-zinc-500">
                        No endpoint revenue yet.
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </div>
          </div>
        </>
      ) : null}
    </section>
  );
}

function Stat({ icon, label, primary, secondary, tone = "text-zinc-100" }: { icon: React.ReactNode; label: string; primary: string; secondary?: string; tone?: string }) {
  return (
    <div className="card p-4">
      <div className="flex items-center gap-2 text-xs uppercase tracking-wide text-zinc-500">
        {icon}
        {label}
      </div>
      <div className={`mt-2 font-mono text-xl font-semibold ${tone}`}>{primary}</div>
      {secondary ? <div className="font-mono text-xs text-zinc-500">{secondary}</div> : null}
    </div>
  );
}

const W = 720;
const H = 220;
const PAD = { top: 12, right: 12, bottom: 28, left: 56 };

function RevenueChart({ daily }: { daily: DailyRevenue[] }) {
  const [active, setActive] = useState<number | null>(null);
  const bars = useMemo(() => {
    const values = daily.map((d) => sompiToKasNumber(d.merchant_sompi));
    const max = Math.max(...values, 0);
    return { values, max: max > 0 ? max : 1 };
  }, [daily]);

  if (daily.length === 0) {
    return <p className="py-10 text-center text-sm text-zinc-500">No settled calls in this period.</p>;
  }

  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;
  const slot = innerW / daily.length;
  const barW = Math.max(2, Math.min(40, slot * 0.7));
  const ticks = [0, 0.5, 1];
  const labelEvery = Math.max(1, Math.ceil(daily.length / 8));
  const hovered = active !== null ? daily[active] : undefined;

  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="Daily revenue bar chart">
        {ticks.map((t) => {
          const y = PAD.top + innerH * (1 - t);
          return (
            <g key={t}>
              <line x1={PAD.left} x2={W - PAD.right} y1={y} y2={y} stroke="#27272a" strokeWidth={1} />
              <text x={PAD.left - 8} y={y + 4} textAnchor="end" fontSize={10} fill="#71717a" fontFamily="ui-monospace, monospace">
                {(bars.max * t).toFixed(2)}
              </text>
            </g>
          );
        })}
        {daily.map((d, i) => {
          const h = ((bars.values[i] ?? 0) / bars.max) * innerH;
          const x = PAD.left + slot * i + (slot - barW) / 2;
          const y = PAD.top + innerH - h;
          return (
            <g key={d.day} onMouseEnter={() => setActive(i)} onMouseLeave={() => setActive(null)} onFocus={() => setActive(i)} onBlur={() => setActive(null)} tabIndex={0}>
              <rect x={PAD.left + slot * i} y={PAD.top} width={slot} height={innerH} fill="transparent" />
              <rect x={x} y={y} width={barW} height={Math.max(h, 1)} rx={2} fill={active === i ? "#70e6cf" : "#49eacb"} opacity={active === null || active === i ? 1 : 0.55}>
                <title>{`${d.day}: ${formatKas(d.merchant_sompi)} KAS (${formatSompi(d.merchant_sompi)} sompi) · gross ${formatUsd(d.usd_value)} · ${d.call_count} calls`}</title>
              </rect>
              {i % labelEvery === 0 ? (
                <text x={x + barW / 2} y={H - 8} textAnchor="middle" fontSize={10} fill="#71717a" fontFamily="ui-monospace, monospace">
                  {d.day.slice(5)}
                </text>
              ) : null}
            </g>
          );
        })}
      </svg>
      <p className="min-h-[1.25rem] font-mono text-xs text-zinc-400" aria-live="polite">
        {hovered
          ? `${hovered.day} · ${formatKas(hovered.merchant_sompi)} KAS (${formatSompi(hovered.merchant_sompi)} sompi) · ${formatUsd(hovered.usd_value)} at settlement · ${hovered.call_count} calls`
          : "Hover a bar for exact KAS and USD-at-settlement values."}
      </p>
    </div>
  );
}
