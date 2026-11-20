import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { EXPECTED_AUTH_DOMAINS } from "@/lib/config";
import { checkChallenge } from "@/lib/challenge";
import { checkMerchantCreate } from "@/lib/server/auth-input";
import { gatewayRequest } from "@/lib/server/gateway";
import { checkSameOrigin, fromGatewayFailure, jsonError, readJsonBody } from "@/lib/server/route";
import { API_KEY_RE, setSessionCookie } from "@/lib/server/session";
import type { MerchantCreated } from "@/lib/types";

export const dynamic = "force-dynamic";

/**
 * Registers a merchant with a wallet signature over a purpose="register" challenge. The response carries the long-lived
 * api_key exactly once (the UI shows it a single time); the same key also becomes the httpOnly session cookie.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const blocked = checkSameOrigin(request);
  if (blocked) return blocked;

  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);
  const checked = checkMerchantCreate(body);
  if (!checked.ok) return jsonError(checked.error, 422);
  const refusal = checkChallenge(checked.value.challenge, { purpose: "register", address: checked.value.kaspa_address, domains: EXPECTED_AUTH_DOMAINS });
  if (refusal) return jsonError(refusal, 422, "challenge_rejected");

  const result = await gatewayRequest<MerchantCreated>("/v1/merchants", { method: "POST", body: JSON.stringify(checked.value) });
  if (!result.ok) return fromGatewayFailure(result);
  if (!API_KEY_RE.test(result.data.api_key)) return jsonError("The gateway returned an invalid API key. Please try again.", 502);

  const response = NextResponse.json<MerchantCreated>(result.data, { status: 201, headers: { "Cache-Control": "no-store" } });
  setSessionCookie(response, result.data.api_key);
  return response;
}
