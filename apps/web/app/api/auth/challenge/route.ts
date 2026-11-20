import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { EXPECTED_AUTH_DOMAINS } from "@/lib/config";
import { checkChallenge } from "@/lib/challenge";
import { gatewayRequest } from "@/lib/server/gateway";
import { checkSameOrigin, fromGatewayFailure, jsonError, jsonOk, readJsonBody } from "@/lib/server/route";
import { checkKaspaAddress } from "@/lib/validation";
import type { AuthChallenge, AuthChallengeRequest } from "@/lib/types";

export const dynamic = "force-dynamic";

/**
 * Step 1 of wallet-signature login / registration: asks the gateway for a single-use challenge, then refuses to hand it
 * to the browser unless its Domain, Purpose, Address and Expires match what was requested.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const blocked = checkSameOrigin(request);
  if (blocked) return blocked;
  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);
  const address = checkKaspaAddress(body.kaspa_address);
  if (!address.ok) return jsonError(address.error, 422);
  const purpose = body.purpose === "register" ? "register" : body.purpose === "login" || body.purpose === undefined ? "login" : null;
  if (!purpose) return jsonError('purpose must be "login" or "register"', 422);

  const payload: AuthChallengeRequest = { kaspa_address: address.value, purpose };
  const result = await gatewayRequest<AuthChallenge>("/v1/auth/challenge", { method: "POST", body: JSON.stringify(payload) });
  if (!result.ok) return fromGatewayFailure(result);

  const refusal = checkChallenge(result.data.challenge, {
    purpose,
    address: address.value,
    domains: EXPECTED_AUTH_DOMAINS,
    expiresAt: result.data.expires_at,
  });
  if (refusal) return jsonError(refusal, 502, "challenge_rejected");
  return jsonOk(result.data);
}
