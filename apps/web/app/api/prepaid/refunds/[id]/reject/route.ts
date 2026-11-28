import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { fromGatewayFailure, isAuth, jsonError, jsonOk, readJsonBody, requireSession } from "@/lib/server/route";
import { checkRefundReason, isUuid } from "@/lib/validation";
import type { PrepaidRefundRead } from "@/lib/types";

export const dynamic = "force-dynamic";

type Context = { params: Promise<{ id: string }> };

/** Merchant [session]: declines a `requested` refund with a reason the buyer can read; the hold is released. */
export async function POST(request: NextRequest, context: Context): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const { id } = await context.params;
  if (!isUuid(id)) return jsonError("Invalid refund id", 400);
  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);
  if (Object.keys(body).some((key) => key !== "reason")) return jsonError("Only reason is accepted", 422);
  const reason = checkRefundReason(body.reason);
  if (!reason.ok) return jsonError(reason.error, 422);
  const result = await gatewayRequest<PrepaidRefundRead>(`/v1/prepaid/refunds/${id.toLowerCase()}/reject`, {
    method: "POST",
    bearer: auth.token,
    body: JSON.stringify({ reason: reason.value }),
  });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}
