import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { settlementUsd } from "@/lib/server/settlement-usd";
import { pageParams } from "@/lib/server/prepaid-input";
import { fromGatewayFailure, isAuth, jsonError, jsonOk, requireSession } from "@/lib/server/route";
import type { PrepaidLedgerEntryRead, PrepaidLedgerEntryWithUsd } from "@/lib/types";

export const dynamic = "force-dynamic";

/** Merchant prepaid ledger (v2.13/v2.14): refund payouts, expiries and failed-delivery credits, newest first. Credit rows gain `usd` (rate of the credited call's settlement). */
export async function GET(request: NextRequest): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const page = pageParams(request.nextUrl.searchParams, 200);
  if (!page.ok) return jsonError(page.error, 422);
  const result = await gatewayRequest<PrepaidLedgerEntryRead[]>("/v1/prepaid/ledger", { bearer: auth.token, query: page.value });
  if (!result.ok) return fromGatewayFailure(result);
  const ids = new Set(result.data.flatMap((row) => (row.kind === "failed_delivery_credit" && row.settlement_id ? [row.settlement_id] : [])));
  const usd = await settlementUsd(auth.token, ids);
  const rows: PrepaidLedgerEntryWithUsd[] = result.data.map((row) => ({ ...row, usd: row.settlement_id ? (usd.get(row.settlement_id) ?? null) : null }));
  return jsonOk(rows);
}
