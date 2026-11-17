"use client";

import { Search } from "lucide-react";
import { useMemo, useState } from "react";
import { formatKas, formatSompi } from "@/lib/sompi";
import type { DirectoryEntry } from "@/lib/types";
import { shortAddress } from "@/lib/format";
import { CopyButton } from "./copy-button";

export function DirectoryExplorer({ entries }: { entries: DirectoryEntry[] }) {
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("all");

  const categories = useMemo(() => {
    const set = new Set<string>();
    for (const e of entries) if (e.category) set.add(e.category);
    return [...set].sort();
  }, [entries]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return entries.filter((e) => {
      if (category !== "all" && e.category !== category) return false;
      if (!q) return true;
      return [e.name, e.slug, e.description ?? "", e.category ?? "", e.merchant_display_name].some((field) => field.toLowerCase().includes(q));
    });
  }, [entries, query, category]);

  return (
    <div>
      <div className="mb-6 flex flex-col gap-3 sm:flex-row">
        <div className="relative flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500" aria-hidden />
          <input className="input pl-9" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search APIs, merchants, categories…" aria-label="Search the directory" />
        </div>
        <select className="input sm:w-48" value={category} onChange={(e) => setCategory(e.target.value)} aria-label="Filter by category">
          <option value="all">All categories</option>
          {categories.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </div>
      <p className="mb-3 font-mono text-xs text-zinc-500">
        {filtered.length} of {entries.length} endpoints
      </p>
      {filtered.length === 0 ? (
        <div className="card p-10 text-center text-sm text-zinc-500">No endpoints match your search.</div>
      ) : (
        <ul className="grid gap-3 md:grid-cols-2">
          {filtered.map((e) => (
            <li key={e.slug} className="card flex flex-col p-4">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h2 className="truncate font-medium text-zinc-100">{e.name}</h2>
                  <p className="text-xs text-zinc-500">by {e.merchant_display_name}</p>
                </div>
                <div className="shrink-0 text-right">
                  <div className="font-mono text-sm font-semibold text-kas-300">{formatKas(e.price_sompi)} KAS</div>
                  <div className="font-mono text-[11px] text-zinc-600">{formatSompi(e.price_sompi)} sompi</div>
                </div>
              </div>
              {e.description ? <p className="mt-2 line-clamp-3 text-sm text-zinc-400">{e.description}</p> : null}
              <div className="mt-3 flex flex-wrap items-center gap-2">
                {e.category ? <span className="badge border-zinc-700 bg-zinc-900 text-zinc-400">{e.category}</span> : null}
                {e.allowed_methods.map((m) => (
                  <span key={m} className="badge border-zinc-800 bg-zinc-950 font-mono text-zinc-400">
                    {m}
                  </span>
                ))}
              </div>
              <div className="mt-4 flex items-center gap-2">
                <code className="flex-1 overflow-x-auto rounded border border-zinc-800 bg-zinc-950 px-2 py-1.5 font-mono text-xs text-zinc-300">{e.proxy_url}</code>
                <CopyButton value={e.proxy_url} />
              </div>
              <p className="mt-2 font-mono text-[11px] text-zinc-600" title={e.pay_to}>
                pay_to {shortAddress(e.pay_to)}
              </p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
