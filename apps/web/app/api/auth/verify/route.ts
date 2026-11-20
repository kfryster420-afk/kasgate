import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { EXPECTED_AUTH_DOMAIN } from "@/lib/config";
import { checkChallenge } from "@/lib/challenge";
import { checkVerifyRequest } from "@/lib/server/auth-input";
import { gatewayRequest } from "@/lib/server/gateway";
import { checkSameOrigin, fromGatewayFailure, jsonError, readJsonBody } from "@/lib/server/route";
import { SESSION_TOKEN_RE, maxAgeFromExpiry, setSessionCookie } from "@/lib/server/session";
import type { SessionIssued, SessionStarted } from "@/lib/types";

export const dynamic = "force-dynamic";

/**
 * Step 2: verifies the wallet signature. The gateway answers SessionIssued; the `kgs_…` session token goes only into the
 * httpOnly cookie, with maxAge derived from `expires_at`. The api_key is not rotated by login.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const blocked = checkSameOrigin(request);
  if (blocked) return blocked;
  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);
  const checked = checkVerifyRequest(body);
  if (!checked.ok) return jsonError(checked.error, 422);
  const refusal = checkChallenge(checked.value.challenge, { purpose: "login", address: checked.value.kaspa_address, domain: EXPECTED_AUTH_DOMAIN });
  if (refusal) return jsonError(refusal, 422, "challenge_rejected");

  const result = await gatewayRequest<SessionIssued>("/v1/auth/verify", { method: "POST", body: JSON.stringify(checked.value) });
  if (!result.ok) return fromGatewayFailure(result);

  const { session_token: token, expires_at: expiresAt, merchant } = result.data;
  const maxAge = typeof token === "string" && typeof expiresAt === "string" ? maxAgeFromExpiry(expiresAt) : null;
  if (typeof token !== "string" || !SESSION_TOKEN_RE.test(token) || maxAge === null) {
    return jsonError("The gateway returned an invalid session. Please try again.", 502);
  }
  const response = NextResponse.json<SessionStarted>({ merchant, expires_at: expiresAt }, { headers: { "Cache-Control": "no-store" } });
  setSessionCookie(response, token, maxAge);
  return response;
}
