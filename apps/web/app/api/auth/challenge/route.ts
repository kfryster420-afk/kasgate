import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { checkSameOrigin, fromGatewayFailure, jsonError, jsonOk, readJsonBody } from "@/lib/server/route";
import { checkKaspaAddress } from "@/lib/validation";
import type { AuthChallenge, AuthChallengeRequest } from "@/lib/types";

export const dynamic = "force-dynamic";

/** Step 1 of wallet-signature login: asks the gateway for a single-use challenge for this address. */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const blocked = checkSameOrigin(request);
  if (blocked) return blocked;
  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);
  const address = checkKaspaAddress(body.kaspa_address);
  if (!address.ok) return jsonError(address.error, 422);

  const payload: AuthChallengeRequest = { kaspa_address: address.value };
  const result = await gatewayRequest<AuthChallenge>("/v1/auth/challenge", { method: "POST", body: JSON.stringify(payload) });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}
