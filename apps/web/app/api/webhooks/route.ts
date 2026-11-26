import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { fromGatewayFailure, isAuth, jsonError, jsonOk, readJsonBody, requireSession } from "@/lib/server/route";
import { buildWebhookCreate } from "@/lib/server/webhook-input";
import type { WebhookCreated, WebhookRead } from "@/lib/types";

export const dynamic = "force-dynamic";

/** CONTRACT v2.12 merchant webhooks: list. */
export async function GET(request: NextRequest): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const result = await gatewayRequest<WebhookRead[]>("/v1/webhooks", { bearer: auth.token });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}

/** Create: the response carries the signing secret exactly once (never stored by this app). */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);
  const built = buildWebhookCreate(body);
  if (!built.ok) return jsonError(built.error, 422);
  const result = await gatewayRequest<WebhookCreated>("/v1/webhooks", {
    method: "POST",
    bearer: auth.token,
    body: JSON.stringify(built.payload),
  });
  return result.ok ? jsonOk(result.data, 201) : fromGatewayFailure(result);
}
