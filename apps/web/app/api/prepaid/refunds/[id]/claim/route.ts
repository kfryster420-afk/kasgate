import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { fromGatewayFailure, isAuth, jsonError, jsonOk, requireSession } from "@/lib/server/route";
import { isUuid } from "@/lib/validation";
import type { PrepaidRefundRead } from "@/lib/types";

export const dynamic = "force-dynamic";

type Context = { params: Promise<{ id: string }> };

/**
 * Merchant [session]: claims a `requested` refund for payment (or renews a `paying` claim) right before the wallet
 * broadcasts. From then on the buyer cannot cancel until the claim lapses (900 s by default) without a payout.
 */
export async function POST(request: NextRequest, context: Context): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const { id } = await context.params;
  if (!isUuid(id)) return jsonError("Invalid refund id", 400);
  const result = await gatewayRequest<PrepaidRefundRead>(`/v1/prepaid/refunds/${id.toLowerCase()}/claim`, { method: "POST", bearer: auth.token });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}
