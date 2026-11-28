import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { checkSignedBody } from "@/lib/server/prepaid-input";
import { checkSameOrigin, fromGatewayFailure, jsonError, jsonOk, readJsonBody } from "@/lib/server/route";
import { isUuid } from "@/lib/validation";
import type { PrepaidRefundRead } from "@/lib/types";

export const dynamic = "force-dynamic";

type Context = { params: Promise<{ id: string }> };

/** Buyer [signature-authorised]: withdraws a `requested` refund with a signed `cancel` challenge. */
export async function POST(request: NextRequest, context: Context): Promise<NextResponse> {
  const blocked = checkSameOrigin(request);
  if (blocked) return blocked;
  const { id } = await context.params;
  if (!isUuid(id)) return jsonError("Invalid refund id", 400);
  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);
  const signed = checkSignedBody(body);
  if (!signed.ok) return jsonError(signed.error, 422);
  const result = await gatewayRequest<PrepaidRefundRead>(`/v1/prepaid/refunds/${id.toLowerCase()}/cancel`, {
    method: "POST",
    body: JSON.stringify(signed.value),
  });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}
