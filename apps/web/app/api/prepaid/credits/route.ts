import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { settlementUsd } from "@/lib/server/settlement-usd";
import { pageParams } from "@/lib/server/prepaid-input";
import { fromGatewayFailure, isAuth, jsonError, jsonOk, requireSession } from "@/lib/server/route";
import { checkKaspaAddress, checkSlug, checkTxId } from "@/lib/validation";
import type { PrepaidCreditRead, PrepaidCreditWithUsd } from "@/lib/types";

export const dynamic = "force-dynamic";

/** Merchant [session]: failed-delivery credits (CONTRACT v2.14), newest first; filters slug, payer_address, tx_id. Each row gains `usd`: the KAS/USD rate its call settled at. */
export async function GET(request: NextRequest): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const params = request.nextUrl.searchParams;
  const page = pageParams(params, 200);
  if (!page.ok) return jsonError(page.error, 422);
  const query: Record<string, string | number> = { ...page.value };
  const slug = params.get("slug");
  if (slug !== null && slug !== "") {
    const checked = checkSlug(slug);
    if (!checked.ok) return jsonError(checked.error, 422);
    query.slug = checked.value;
  }
  const payer = params.get("payer_address");
  if (payer !== null && payer !== "") {
    const checked = checkKaspaAddress(payer);
    if (!checked.ok) return jsonError(checked.error, 422);
    query.payer_address = checked.value;
  }
  const tx = params.get("tx_id");
  if (tx !== null && tx !== "") {
    const checked = checkTxId(tx);
    if (!checked.ok) return jsonError(checked.error, 422);
    query.tx_id = checked.value;
  }
  const result = await gatewayRequest<PrepaidCreditRead[]>("/v1/prepaid/credits", { bearer: auth.token, query });
  if (!result.ok) return fromGatewayFailure(result);
  const usd = await settlementUsd(auth.token, new Set(result.data.map((row) => row.settlement_id)));
  const rows: PrepaidCreditWithUsd[] = result.data.map((row) => ({ ...row, usd: usd.get(row.settlement_id) ?? null }));
  return jsonOk(rows);
}
