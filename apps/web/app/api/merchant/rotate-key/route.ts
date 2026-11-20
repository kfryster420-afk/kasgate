import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { respondWithIssuedKey } from "@/lib/server/key-issue";
import { fromGatewayFailure, isAuth, requireSession } from "@/lib/server/route";
import type { MerchantCreated } from "@/lib/types";

export const dynamic = "force-dynamic";

/**
 * Rotates the long-lived API key (the old key is invalid immediately). The gateway also issues a fresh session, which
 * replaces the cookie. The new api_key is returned once for the UI to show; it is never stored in a cookie.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const result = await gatewayRequest<MerchantCreated>("/v1/merchants/me/rotate-key", { method: "POST", bearer: auth.token });
  if (!result.ok) return fromGatewayFailure(result);
  return respondWithIssuedKey(result.data, 200);
}
