import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { fromGatewayFailure, isAuth, requireSession } from "@/lib/server/route";
import { setSessionCookie } from "@/lib/server/session";
import type { MerchantCreated, MerchantRegistered } from "@/lib/types";

export const dynamic = "force-dynamic";

/** Rotates the API key (old key is invalid immediately) and swaps the session cookie. The key is never returned. */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const result = await gatewayRequest<MerchantCreated>("/v1/merchants/me/rotate-key", { method: "POST", apiKey: auth.apiKey });
  if (!result.ok) return fromGatewayFailure(result);
  const response = NextResponse.json<MerchantRegistered>({ merchant: result.data.merchant }, { headers: { "Cache-Control": "no-store" } });
  setSessionCookie(response, result.data.api_key);
  return response;
}
