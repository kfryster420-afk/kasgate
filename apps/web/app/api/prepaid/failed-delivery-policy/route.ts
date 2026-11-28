import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { fromGatewayFailure, isAuth, jsonError, jsonOk, readJsonBody, requireSession } from "@/lib/server/route";
import type { FailedDeliveryPolicyRead, FailedDeliveryPolicyUpdate } from "@/lib/types";

export const dynamic = "force-dynamic";

/** Merchant [session]: the failed-delivery auto-credit policy (CONTRACT v2.14; on by default, timeouts included). */
export async function GET(request: NextRequest): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const result = await gatewayRequest<FailedDeliveryPolicyRead>("/v1/prepaid/failed-delivery-policy", { bearer: auth.token });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}

/** Merchant [session, same-origin]: switch auto-credit and/or timeout crediting. Applies to calls settling from now on. */
export async function PUT(request: NextRequest): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);
  if (Object.keys(body).some((key) => key !== "enabled" && key !== "credit_timeouts")) return jsonError("Only enabled and credit_timeouts are accepted", 422);
  const update: FailedDeliveryPolicyUpdate = {};
  if (body.enabled !== undefined) {
    if (typeof body.enabled !== "boolean") return jsonError("enabled must be true or false", 422);
    update.enabled = body.enabled;
  }
  if (body.credit_timeouts !== undefined) {
    if (typeof body.credit_timeouts !== "boolean") return jsonError("credit_timeouts must be true or false", 422);
    update.credit_timeouts = body.credit_timeouts;
  }
  if (update.enabled === undefined && update.credit_timeouts === undefined) return jsonError("Give enabled and/or credit_timeouts", 422);
  const result = await gatewayRequest<FailedDeliveryPolicyRead>("/v1/prepaid/failed-delivery-policy", {
    method: "PUT",
    bearer: auth.token,
    body: JSON.stringify(update),
  });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}
