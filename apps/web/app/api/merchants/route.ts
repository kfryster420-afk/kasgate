import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { checkDisplayName, checkKaspaAddress, checkOptionalEmail } from "@/lib/validation";
import { gatewayRequest } from "@/lib/server/gateway";
import { checkSameOrigin, fromGatewayFailure, jsonError, readJsonBody } from "@/lib/server/route";
import { setSessionCookie } from "@/lib/server/session";
import type { MerchantCreated, MerchantRegistered } from "@/lib/types";

export const dynamic = "force-dynamic";

/** Registers a merchant. The one-time api_key is stored ONLY in an httpOnly cookie and never returned to the browser. */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const blocked = checkSameOrigin(request);
  if (blocked) return blocked;

  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);

  const address = checkKaspaAddress(body.kaspa_address);
  if (!address.ok) return jsonError(address.error, 422);
  const name = checkDisplayName(body.display_name);
  if (!name.ok) return jsonError(name.error, 422);
  const email = checkOptionalEmail(body.email);
  if (!email.ok) return jsonError(email.error, 422);

  const payload: Record<string, unknown> = { kaspa_address: address.value, display_name: name.value };
  if (email.value) payload.email = email.value;

  const result = await gatewayRequest<MerchantCreated>("/v1/merchants", { method: "POST", body: JSON.stringify(payload) });
  if (!result.ok) return fromGatewayFailure(result);

  const response = NextResponse.json<MerchantRegistered>(
    { merchant: result.data.merchant },
    { status: 201, headers: { "Cache-Control": "no-store" } },
  );
  setSessionCookie(response, result.data.api_key);
  return response;
}
