import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { buyerParams } from "@/lib/server/prepaid-input";
import { fromGatewayFailure, jsonError, jsonOk } from "@/lib/server/route";
import type { PrepaidBalanceRead } from "@/lib/types";

export const dynamic = "force-dynamic";

/** Public (no session, like the gateway): a buyer's prepaid balance at the merchant behind `slug`. */
export async function GET(request: NextRequest): Promise<NextResponse> {
  const params = buyerParams(request.nextUrl.searchParams);
  if (!params.ok) return jsonError(params.error, 422);
  const result = await gatewayRequest<PrepaidBalanceRead>("/v1/prepaid/balance", { query: params.value });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}
