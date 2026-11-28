import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { pageParams } from "@/lib/server/prepaid-input";
import { fromGatewayFailure, isAuth, jsonError, jsonOk, requireSession } from "@/lib/server/route";
import type { PrepaidLedgerEntryRead } from "@/lib/types";

export const dynamic = "force-dynamic";

/** Merchant prepaid ledger entries (CONTRACT v2.13): refund payouts and credit expiries, newest first. */
export async function GET(request: NextRequest): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const page = pageParams(request.nextUrl.searchParams, 200);
  if (!page.ok) return jsonError(page.error, 422);
  const result = await gatewayRequest<PrepaidLedgerEntryRead[]>("/v1/prepaid/ledger", { bearer: auth.token, query: page.value });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}
