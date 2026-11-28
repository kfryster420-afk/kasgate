import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { EXPECTED_AUTH_DOMAIN } from "@/lib/config";
import { checkCreditChallenge } from "@/lib/credit-challenge";
import { gatewayRequest } from "@/lib/server/gateway";
import { checkSameOrigin, fromGatewayFailure, jsonError, jsonOk, readJsonBody } from "@/lib/server/route";
import type { PrepaidCreditStatusChallenge } from "@/lib/types";
import { checkKaspaAddress, checkSlug } from "@/lib/validation";

export const dynamic = "force-dynamic";

/**
 * Buyer step 1 (public, CONTRACT v2.14): the `credits` challenge to sign to read this key's failed-delivery credits.
 * Strict body {slug, payer_address}; the challenge is checked line by line before it reaches the wallet. Single use:
 * every page of credits/status needs a new one.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const blocked = checkSameOrigin(request);
  if (blocked) return blocked;
  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);
  if (Object.keys(body).some((key) => key !== "slug" && key !== "payer_address")) return jsonError("Only slug and payer_address are accepted", 422);
  const slug = checkSlug(body.slug);
  if (!slug.ok) return jsonError(slug.error, 422);
  const address = checkKaspaAddress(body.payer_address);
  if (!address.ok) return jsonError(address.error, 422);
  const result = await gatewayRequest<PrepaidCreditStatusChallenge>("/v1/prepaid/credits/challenge", {
    method: "POST",
    body: JSON.stringify({ slug: slug.value, payer_address: address.value }),
  });
  if (!result.ok) return fromGatewayFailure(result);
  const refusal = checkCreditChallenge(result.data, { address: address.value, slug: slug.value, domain: EXPECTED_AUTH_DOMAIN });
  if (refusal) return jsonError(refusal, 502);
  return jsonOk(result.data, 201);
}
