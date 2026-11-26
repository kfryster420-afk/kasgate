import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { buyerParams, checkSignedBody } from "@/lib/server/prepaid-input";
import { checkSameOrigin, fromGatewayFailure, jsonError, jsonOk, readJsonBody } from "@/lib/server/route";
import type { PrepaidRefundRead } from "@/lib/types";

export const dynamic = "force-dynamic";

/** Public (no signature, like GET /balance): this address's refunds at the merchant behind `slug`; reasons withheld. */
export async function GET(request: NextRequest): Promise<NextResponse> {
  const params = buyerParams(request.nextUrl.searchParams);
  if (!params.ok) return jsonError(params.error, 422);
  const rawLimit = request.nextUrl.searchParams.get("limit");
  const limit = rawLimit === null ? 10 : /^\d{1,2}$/.test(rawLimit) ? Number(rawLimit) : 0;
  if (limit < 1 || limit > 50) return jsonError("limit must be 1–50", 422);
  const result = await gatewayRequest<PrepaidRefundRead[]>("/v1/prepaid/refunds/status", {
    query: { ...params.value, limit },
  });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}

/** Buyer [signature-authorised]: a signed `status` challenge returns the same rows WITH the merchant's reasons and payout errors. */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const blocked = checkSameOrigin(request);
  if (blocked) return blocked;
  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);
  const signed = checkSignedBody(body);
  if (!signed.ok) return jsonError(signed.error, 422);
  const result = await gatewayRequest<PrepaidRefundRead[]>("/v1/prepaid/refunds/status", {
    method: "POST",
    body: JSON.stringify(signed.value),
  });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}
