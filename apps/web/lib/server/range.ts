import "server-only";

export type RangeResult = { ok: true; start?: string; end?: string } | { ok: false; error: string };

/** Validates optional ISO-8601 `start` / `end` query params and normalises them to UTC ISO strings. */
export function parseRange(params: URLSearchParams): RangeResult {
  const out: { start?: string; end?: string } = {};
  for (const key of ["start", "end"] as const) {
    const raw = params.get(key);
    if (raw === null || raw === "") continue;
    const time = Date.parse(raw);
    if (Number.isNaN(time)) return { ok: false, error: `${key} must be an ISO-8601 date or datetime` };
    out[key] = new Date(time).toISOString();
  }
  if (out.start && out.end && out.start > out.end) return { ok: false, error: "start must not be after end" };
  return { ok: true, ...out };
}
