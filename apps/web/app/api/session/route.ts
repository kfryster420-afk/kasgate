import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { checkSameOrigin, fromGatewayFailure, jsonError, readJsonBody } from "@/lib/server/route";
import { API_KEY_RE, clearSessionCookie, setSessionCookie } from "@/lib/server/session";
import type { MerchantRead, SessionStarted } from "@/lib/types";

export const dynamic = "force-dynamic";

/** Sign in with an existing `kg_` API key: verified against GET /v1/merchants/me, then stored in the httpOnly cookie (7 days). */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const blocked = checkSameOrigin(request);
  if (blocked) return blocked;

  const body = await readJsonBody(request);
  const apiKey = typeof body?.api_key === "string" ? body.api_key.trim() : "";
  if (!API_KEY_RE.test(apiKey)) return jsonError("API keys look like kg_… — check what you pasted", 422);

  const result = await gatewayRequest<MerchantRead>("/v1/merchants/me", { apiKey });
  if (!result.ok) {
    return result.status === 401 || result.status === 403 ? jsonError("That API key was not recognised", 401) : fromGatewayFailure(result);
  }
  const response = NextResponse.json<SessionStarted>({ merchant: result.data, expires_at: null }, { headers: { "Cache-Control": "no-store" } });
  setSessionCookie(response, apiKey);
  return response;
}

export async function DELETE(request: NextRequest): Promise<NextResponse> {
  const blocked = checkSameOrigin(request);
  if (blocked) return blocked;
  const response = new NextResponse(null, { status: 204, headers: { "Cache-Control": "no-store" } });
  clearSessionCookie(response);
  return response;
}
