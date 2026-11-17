import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { extractErrorMessage, gatewayRaw } from "@/lib/server/gateway";
import { parseRange } from "@/lib/server/range";
import { isAuth, jsonError, requireSession } from "@/lib/server/route";

export const dynamic = "force-dynamic";

/** Streams GET /v1/tax-report.csv as a download. Columns: see TAX_CSV_COLUMNS in lib/types.ts. */
export async function GET(request: NextRequest): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const range = parseRange(request.nextUrl.searchParams);
  if (!range.ok) return jsonError(range.error, 422);

  const upstream = await gatewayRaw("/v1/tax-report.csv", {
    apiKey: auth.apiKey,
    accept: "text/csv",
    query: { start: range.start, end: range.end },
  });
  if (!upstream) return jsonError("The KasGate gateway is unreachable. Try again shortly.", 502);
  if (!upstream.ok) {
    const text = await upstream.text();
    return jsonError(extractErrorMessage(text, upstream.status), upstream.status >= 500 ? 502 : upstream.status);
  }

  const label = (range.start ?? "all").slice(0, 10) + "_" + (range.end ?? "now").slice(0, 10);
  return new NextResponse(upstream.body, {
    status: 200,
    headers: {
      "Content-Type": "text/csv; charset=utf-8",
      "Content-Disposition": `attachment; filename="kasgate-tax-report_${label}.csv"`,
      "Cache-Control": "no-store",
    },
  });
}
