export interface DateRange {
  /** YYYY-MM-DD (UTC) */
  from: string;
  /** YYYY-MM-DD (UTC) */
  to: string;
}

export function isoDay(date: Date): string {
  return date.toISOString().slice(0, 10);
}

export function defaultRange(days = 30): DateRange {
  const to = new Date();
  const from = new Date(to.getTime() - (days - 1) * 86_400_000);
  return { from: isoDay(from), to: isoDay(to) };
}

export function isValidRange(range: DateRange): boolean {
  return /^\d{4}-\d{2}-\d{2}$/.test(range.from) && /^\d{4}-\d{2}-\d{2}$/.test(range.to) && range.from <= range.to;
}

/** Whole UTC days: [from 00:00:00Z, to 23:59:59.999Z]. */
export function rangeToQuery(range: DateRange): string {
  const params = new URLSearchParams();
  if (isValidRange(range)) {
    params.set("start", `${range.from}T00:00:00.000Z`);
    params.set("end", `${range.to}T23:59:59.999Z`);
  }
  return params.toString();
}
