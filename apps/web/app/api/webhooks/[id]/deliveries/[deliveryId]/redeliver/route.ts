import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { fromGatewayFailure, isAuth, jsonError, jsonOk, requireSession } from "@/lib/server/route";
import { isUuid } from "@/lib/validation";
import type { WebhookDeliveryRead } from "@/lib/types";

export const dynamic = "force-dynamic";

type Context = { params: Promise<{ id: string; deliveryId: string }> };

/** Re-queues a delivery (same event id and body) with a fresh attempt budget. */
export async function POST(request: NextRequest, context: Context): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const { id, deliveryId } = await context.params;
  if (!isUuid(id) || !isUuid(deliveryId)) return jsonError("Invalid webhook or delivery id", 400);
  const result = await gatewayRequest<WebhookDeliveryRead>(`/v1/webhooks/${id}/deliveries/${deliveryId}/redeliver`, {
    method: "POST",
    bearer: auth.token,
  });
  return result.ok ? jsonOk(result.data, 202) : fromGatewayFailure(result);
}
