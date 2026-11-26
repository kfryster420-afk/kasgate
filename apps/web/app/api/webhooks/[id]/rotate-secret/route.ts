import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { fromGatewayFailure, isAuth, jsonError, jsonOk, requireSession } from "@/lib/server/route";
import { isUuid } from "@/lib/validation";
import type { WebhookCreated } from "@/lib/types";

export const dynamic = "force-dynamic";

type Context = { params: Promise<{ id: string }> };

/** Issues a new signing secret (shown once); the old one stops signing immediately. */
export async function POST(request: NextRequest, context: Context): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const { id } = await context.params;
  if (!isUuid(id)) return jsonError("Invalid webhook id", 400);
  const result = await gatewayRequest<WebhookCreated>(`/v1/webhooks/${id}/rotate-secret`, { method: "POST", bearer: auth.token });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}
