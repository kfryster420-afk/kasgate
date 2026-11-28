import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { checkSignedBody, pageParams } from "@/lib/server/prepaid-input";
import { checkSameOrigin, fromGatewayFailure, isAuth, jsonError, jsonOk, readJsonBody, requireSession } from "@/lib/server/route";
import { isRefundStatus } from "@/lib/validation";
import { PREPAID_REFUND_STATUSES, type PrepaidRefundRead } from "@/lib/types";

export const dynamic = "force-dynamic";

/** Merchant refund queue [session]: GET /v1/prepaid/refunds?status=&limit=&offset=, newest first. */
export async function GET(request: NextRequest): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const params = request.nextUrl.searchParams;
  const page = pageParams(params, 200);
  if (!page.ok) return jsonError(page.error, 422);
  const status = params.get("status");
  if (status !== null && !isRefundStatus(status)) return jsonError(`status must be one of ${PREPAID_REFUND_STATUSES.join(", ")}`, 422, "invalid_status");
  const result = await gatewayRequest<PrepaidRefundRead[]>("/v1/prepaid/refunds", {
    bearer: auth.token,
    query: { ...page.value, status: status ?? undefined },
  });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}

/**
 * Buyer [no session, signature-authorised]: opens a refund from a signed `request` challenge. The buyer's wallet
 * signature is the authorisation, so no merchant session cookie is attached even if this browser has one.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const blocked = checkSameOrigin(request);
  if (blocked) return blocked;
  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);
  const signed = checkSignedBody(body);
  if (!signed.ok) return jsonError(signed.error, 422);
  const result = await gatewayRequest<PrepaidRefundRead>("/v1/prepaid/refunds", { method: "POST", body: JSON.stringify(signed.value) });
  return result.ok ? jsonOk(result.data, result.status) : fromGatewayFailure(result);
}
