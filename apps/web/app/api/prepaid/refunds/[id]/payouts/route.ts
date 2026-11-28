import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { fromGatewayFailure, isAuth, jsonError, jsonOk, requireSession } from "@/lib/server/route";
import { isUuid } from "@/lib/validation";
import type { PrepaidRefundPayoutRead } from "@/lib/types";

export const dynamic = "force-dynamic";

type Context = { params: Promise<{ id: string }> };

/** Merchant [session]: every attempted payout transaction of a refund, oldest first. */
export async function GET(request: NextRequest, context: Context): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const { id } = await context.params;
  if (!isUuid(id)) return jsonError("Invalid refund id", 400);
  const result = await gatewayRequest<PrepaidRefundPayoutRead[]>(`/v1/prepaid/refunds/${id.toLowerCase()}/payouts`, { bearer: auth.token });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}
