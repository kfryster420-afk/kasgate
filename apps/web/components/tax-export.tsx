"use client";

import { Download } from "lucide-react";
import { isValidRange, rangeToQuery, type DateRange } from "@/lib/range";
import { TAX_CSV_COLUMNS } from "@/lib/types";

export function TaxExport({ range, onRangeChange }: { range: DateRange; onRangeChange: (next: DateRange) => void }) {
  const valid = isValidRange(range);
  const href = `/api/tax-report?${rangeToQuery(range)}`;

  return (
    <section className="card p-5" aria-labelledby="range-heading">
      <div className="flex flex-wrap items-end gap-4">
        <div className="min-w-[200px] flex-1">
          <h2 id="range-heading" className="text-lg font-medium text-zinc-100">
            Period & tax export
          </h2>
          <p className="text-xs text-zinc-500">UTC days, inclusive. Drives analytics above and the 1-click accounting CSV.</p>
        </div>
        <div>
          <label htmlFor="range-from" className="label">
            From
          </label>
          <input id="range-from" type="date" className="input" value={range.from} max={range.to} onChange={(e) => onRangeChange({ ...range, from: e.target.value })} />
        </div>
        <div>
          <label htmlFor="range-to" className="label">
            To
          </label>
          <input id="range-to" type="date" className="input" value={range.to} min={range.from} onChange={(e) => onRangeChange({ ...range, to: e.target.value })} />
        </div>
        {valid ? (
          <a href={href} download className="btn-primary">
            <Download className="h-4 w-4" aria-hidden />
            Download tax CSV
          </a>
        ) : (
          <button type="button" className="btn-primary" disabled>
            <Download className="h-4 w-4" aria-hidden />
            Download tax CSV
          </button>
        )}
      </div>
      <p className="mt-3 break-all font-mono text-[11px] text-zinc-600">Columns: {TAX_CSV_COLUMNS.join(", ")}</p>
    </section>
  );
}
