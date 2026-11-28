import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { fromGatewayFailure, isAuth, jsonError, jsonOk, readJsonBody, requireSession } from "@/lib/server/route";
import { checkDisplayName, checkPrepaidExpiryDays } from "@/lib/validation";
import type { MerchantRead, MerchantUpdate } from "@/lib/types";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const result = await gatewayRequest<MerchantRead>("/v1/merchants/me", { bearer: auth.token });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}

/** PATCH /v1/merchants/me (CONTRACT v2.13): display name and/or default prepaid credit expiry (null = never expires). */
export async function PATCH(request: NextRequest): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);
  const unknown = Object.keys(body).filter((key) => key !== "display_name" && key !== "prepaid_expiry_days");
  if (unknown.length > 0) return jsonError(`Unknown field(s): ${unknown.join(", ")}`, 422);
  const payload: MerchantUpdate = {};
  if ("display_name" in body) {
    const name = checkDisplayName(body.display_name);
    if (!name.ok) return jsonError(name.error, 422);
    payload.display_name = name.value;
  }
  if ("prepaid_expiry_days" in body) {
    const days = checkPrepaidExpiryDays(body.prepaid_expiry_days);
    if (!days.ok) return jsonError(days.error, 422);
    payload.prepaid_expiry_days = days.value;
  }
  if (Object.keys(payload).length === 0) return jsonError("send at least one field to change", 422);
  const result = await gatewayRequest<MerchantRead>("/v1/merchants/me", { method: "PATCH", bearer: auth.token, body: JSON.stringify(payload) });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}
