import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { checkSignedBody } from "@/lib/server/prepaid-input";
import { checkSameOrigin, fromGatewayFailure, jsonError, jsonOk, readJsonBody } from "@/lib/server/route";
import type { PrepaidRefundRead } from "@/lib/types";

export const dynamic = "force-dynamic";

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
