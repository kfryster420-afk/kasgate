import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { parseRange } from "@/lib/server/range";
import { fromGatewayFailure, isAuth, jsonError, jsonOk, requireSession } from "@/lib/server/route";
import type { MerchantAnalytics } from "@/lib/types";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const range = parseRange(request.nextUrl.searchParams);
  if (!range.ok) return jsonError(range.error, 422);
  const result = await gatewayRequest<MerchantAnalytics>("/v1/analytics", {
    bearer: auth.token,
    query: { start: range.start, end: range.end },
  });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}
