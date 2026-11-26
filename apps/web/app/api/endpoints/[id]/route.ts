import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { buildUpdatePayload } from "@/lib/server/endpoint-input";
import { gatewayRequest } from "@/lib/server/gateway";
import { fromGatewayFailure, isAuth, jsonError, jsonOk, readJsonBody, requireSession } from "@/lib/server/route";
import { stringifyWithIntegers } from "@/lib/safe-json";
import { isUuid } from "@/lib/validation";
import type { EndpointRead } from "@/lib/types";

export const dynamic = "force-dynamic";

type Context = { params: Promise<{ id: string }> };

export async function GET(request: NextRequest, context: Context): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const { id } = await context.params;
  if (!isUuid(id)) return jsonError("Invalid endpoint id", 400);
  const result = await gatewayRequest<EndpointRead>(`/v1/endpoints/${id}`, { bearer: auth.token });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}

export async function PATCH(request: NextRequest, context: Context): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const { id } = await context.params;
  if (!isUuid(id)) return jsonError("Invalid endpoint id", 400);
  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);

  const built = await buildUpdatePayload(body);
  if (!built.ok) return jsonError(built.error, 422);

  const result = await gatewayRequest<EndpointRead>(`/v1/endpoints/${id}`, {
    method: "PATCH",
    bearer: auth.token,
    body: stringifyWithIntegers(built.payload, ["price_sompi", "price_usd_micros"]),
  });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}

/** Gateway DELETE sets is_active=false (soft deactivate). */
export async function DELETE(request: NextRequest, context: Context): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const { id } = await context.params;
  if (!isUuid(id)) return jsonError("Invalid endpoint id", 400);
  const result = await gatewayRequest<undefined>(`/v1/endpoints/${id}`, { method: "DELETE", bearer: auth.token });
  if (!result.ok) return fromGatewayFailure(result);
  return new NextResponse(null, { status: 204, headers: { "Cache-Control": "no-store" } });
}
