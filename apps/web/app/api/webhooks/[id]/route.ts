import type { NextRequest } from "next/server";
import { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { fromGatewayFailure, isAuth, jsonError, jsonOk, readJsonBody, requireSession } from "@/lib/server/route";
import { buildWebhookUpdate } from "@/lib/server/webhook-input";
import { isUuid } from "@/lib/validation";
import type { WebhookRead } from "@/lib/types";

export const dynamic = "force-dynamic";

type Context = { params: Promise<{ id: string }> };

export async function GET(request: NextRequest, context: Context): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const { id } = await context.params;
  if (!isUuid(id)) return jsonError("Invalid webhook id", 400);
  const result = await gatewayRequest<WebhookRead>(`/v1/webhooks/${id}`, { bearer: auth.token });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}

export async function PATCH(request: NextRequest, context: Context): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const { id } = await context.params;
  if (!isUuid(id)) return jsonError("Invalid webhook id", 400);
  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);
  const built = buildWebhookUpdate(body);
  if (!built.ok) return jsonError(built.error, 422);
  const result = await gatewayRequest<WebhookRead>(`/v1/webhooks/${id}`, {
    method: "PATCH",
    bearer: auth.token,
    body: JSON.stringify(built.payload),
  });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}

/** Deletes the webhook and its delivery log. */
export async function DELETE(request: NextRequest, context: Context): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const { id } = await context.params;
  if (!isUuid(id)) return jsonError("Invalid webhook id", 400);
  const result = await gatewayRequest<undefined>(`/v1/webhooks/${id}`, { method: "DELETE", bearer: auth.token });
  if (!result.ok) return fromGatewayFailure(result);
  return new NextResponse(null, { status: 204, headers: { "Cache-Control": "no-store" } });
}
