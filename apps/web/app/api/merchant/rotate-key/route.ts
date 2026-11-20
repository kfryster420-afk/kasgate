import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { fromGatewayFailure, isAuth, jsonError, requireSession } from "@/lib/server/route";
import { API_KEY_RE, setSessionCookie } from "@/lib/server/session";
import type { MerchantCreated } from "@/lib/types";

export const dynamic = "force-dynamic";

/**
 * Rotates the long-lived API key (the old key is invalid immediately). The new key is returned once so the merchant can
 * store it for automation. A `kgs_` wallet session is unaffected; a cookie that holds the old `kg_` key is replaced.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const result = await gatewayRequest<MerchantCreated>("/v1/merchants/me/rotate-key", { method: "POST", apiKey: auth.apiKey });
  if (!result.ok) return fromGatewayFailure(result);
  if (!API_KEY_RE.test(result.data.api_key)) return jsonError("The gateway returned an invalid API key. Please try again.", 502);

  const response = NextResponse.json<MerchantCreated>(result.data, { headers: { "Cache-Control": "no-store" } });
  if (!auth.apiKey.startsWith("kgs_")) setSessionCookie(response, result.data.api_key);
  return response;
}
