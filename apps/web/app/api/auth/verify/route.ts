import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { checkVerifyRequest } from "@/lib/server/auth-input";
import { gatewayRequest } from "@/lib/server/gateway";
import { checkSameOrigin, fromGatewayFailure, jsonError, readJsonBody } from "@/lib/server/route";
import { setSessionCookie } from "@/lib/server/session";
import type { MerchantCreated, MerchantRegistered } from "@/lib/types";

export const dynamic = "force-dynamic";

/** Step 2: verifies the wallet signature. The freshly rotated api_key goes only into the httpOnly cookie. */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const blocked = checkSameOrigin(request);
  if (blocked) return blocked;
  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);
  const checked = checkVerifyRequest(body);
  if (!checked.ok) return jsonError(checked.error, 422);

  const result = await gatewayRequest<MerchantCreated>("/v1/auth/verify", { method: "POST", body: JSON.stringify(checked.value) });
  if (!result.ok) return fromGatewayFailure(result);

  const response = NextResponse.json<MerchantRegistered>({ merchant: result.data.merchant }, { headers: { "Cache-Control": "no-store" } });
  setSessionCookie(response, result.data.api_key);
  return response;
}
