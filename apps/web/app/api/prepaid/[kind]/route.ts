import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { fromGatewayFailure, isAuth, jsonError, jsonOk, requireSession } from "@/lib/server/route";
import type { MerchantPrepaidBalanceRead, PrepaidDebitRead, PrepaidDepositRead, PrepaidLedgerKind } from "@/lib/types";

export const dynamic = "force-dynamic";

const KINDS: readonly PrepaidLedgerKind[] = ["balances", "deposits", "debits"];

type Context = { params: Promise<{ kind: string }> };

function intParam(value: string | null, fallback: number, min: number, max: number): number | null {
  if (value === null) return fallback;
  if (!/^\d{1,7}$/.test(value)) return null;
  const n = Number(value);
  return n >= min && n <= max ? n : null;
}

/** Merchant prepaid ledger (CONTRACT v2.11): GET /v1/prepaid/{balances|deposits|debits}, newest first. */
export async function GET(request: NextRequest, context: Context): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const { kind } = await context.params;
  if (!(KINDS as readonly string[]).includes(kind)) return jsonError("Unknown prepaid ledger", 404);
  const params = request.nextUrl.searchParams;
  const limit = intParam(params.get("limit"), 50, 1, 200);
  const offset = intParam(params.get("offset"), 0, 0, 1_000_000);
  if (limit === null || offset === null) return jsonError("limit must be 1–200 and offset a non-negative integer", 422);
  const result = await gatewayRequest<MerchantPrepaidBalanceRead[] | PrepaidDepositRead[] | PrepaidDebitRead[]>(`/v1/prepaid/${kind}`, {
    bearer: auth.token,
    query: { limit, offset },
  });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}
