import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { checkChallenge } from "@/lib/challenge";
import { EXPECTED_AUTH_DOMAIN } from "@/lib/config";
import { checkMerchantCreate } from "@/lib/server/auth-input";
import { gatewayRequest } from "@/lib/server/gateway";
import { respondWithIssuedKey } from "@/lib/server/key-issue";
import { checkSameOrigin, fromGatewayFailure, jsonError, readJsonBody } from "@/lib/server/route";
import type { MerchantCreated } from "@/lib/types";

export const dynamic = "force-dynamic";

/**
 * Registers a merchant with a wallet signature over a purpose="register" challenge. The response carries the long-lived
 * api_key exactly once (shown once in the UI); the httpOnly cookie receives the `kgs_` session token only.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const blocked = checkSameOrigin(request);
  if (blocked) return blocked;

  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);
  const checked = checkMerchantCreate(body);
  if (!checked.ok) return jsonError(checked.error, 422);
  const refusal = checkChallenge(checked.value.challenge, { purpose: "register", address: checked.value.kaspa_address, domain: EXPECTED_AUTH_DOMAIN });
  if (refusal) return jsonError(refusal, 422, "challenge_rejected");

  const result = await gatewayRequest<MerchantCreated>("/v1/merchants", { method: "POST", body: JSON.stringify(checked.value) });
  if (!result.ok) return fromGatewayFailure(result);
  return respondWithIssuedKey(result.data, 201);
}
