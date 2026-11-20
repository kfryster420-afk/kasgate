import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { checkSameOrigin } from "@/lib/server/route";
import { clearSessionCookie, readSessionToken } from "@/lib/server/session";

export const dynamic = "force-dynamic";

/**
 * Sign out (CONTRACT v2.4). First asks the gateway to revoke the current session (`DELETE /v1/auth/session` with the
 * `kgs_` token as Bearer), then ALWAYS clears the cookie, even when the gateway answers 401, 404, an error or is
 * unreachable (older gateways do not have the route yet). Sign-in is wallet-signature only, via /api/auth/verify.
 */
export async function DELETE(request: NextRequest): Promise<NextResponse> {
  const blocked = checkSameOrigin(request);
  if (blocked) return blocked;

  try {
    const token = await readSessionToken();
    if (token) await gatewayRequest<undefined>("/v1/auth/session", { method: "DELETE", bearer: token });
  } catch {
    /* revocation is best effort; the local cookie is cleared regardless */
  }

  const response = new NextResponse(null, { status: 204, headers: { "Cache-Control": "no-store" } });
  clearSessionCookie(response);
  return response;
}
