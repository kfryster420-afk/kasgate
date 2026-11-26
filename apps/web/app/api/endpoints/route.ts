import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { buildCreatePayload } from "@/lib/server/endpoint-input";
import { gatewayRequest } from "@/lib/server/gateway";
import { fromGatewayFailure, isAuth, jsonError, jsonOk, readJsonBody, requireSession } from "@/lib/server/route";
import { stringifyWithIntegers } from "@/lib/safe-json";
import type { EndpointRead } from "@/lib/types";

export const dynamic = "force-dynamic";

export async function GET(request: NextRequest): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const result = await gatewayRequest<EndpointRead[]>("/v1/endpoints", { bearer: auth.token });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}

export async function POST(request: NextRequest): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);

  const built = await buildCreatePayload(body);
  if (!built.ok) return jsonError(built.error, 422);

  const result = await gatewayRequest<EndpointRead>("/v1/endpoints", {
    method: "POST",
    bearer: auth.token,
    body: stringifyWithIntegers(built.payload, ["price_sompi", "price_usd_micros"]),
  });
  return result.ok ? jsonOk(result.data, 201) : fromGatewayFailure(result);
}
