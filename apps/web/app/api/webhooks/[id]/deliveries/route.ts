import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { fromGatewayFailure, isAuth, jsonError, jsonOk, requireSession } from "@/lib/server/route";
import { isUuid } from "@/lib/validation";
import type { WebhookDeliveryRead, WebhookDeliveryStatus } from "@/lib/types";

export const dynamic = "force-dynamic";

type Context = { params: Promise<{ id: string }> };

const STATUSES: readonly WebhookDeliveryStatus[] = ["pending", "delivering", "succeeded", "dead"];

function intParam(value: string | null, fallback: number, min: number, max: number): number | null {
  if (value === null) return fallback;
  if (!/^\d{1,7}$/.test(value)) return null;
  const n = Number(value);
  return n >= min && n <= max ? n : null;
}

/** Delivery log of one webhook, newest first, optionally filtered by status. */
export async function GET(request: NextRequest, context: Context): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const { id } = await context.params;
  if (!isUuid(id)) return jsonError("Invalid webhook id", 400);
  const params = request.nextUrl.searchParams;
  const limit = intParam(params.get("limit"), 50, 1, 200);
  const offset = intParam(params.get("offset"), 0, 0, 1_000_000);
  if (limit === null || offset === null) return jsonError("limit must be 1–200 and offset a non-negative integer", 422);
  const status = params.get("status");
  if (status !== null && !(STATUSES as readonly string[]).includes(status)) return jsonError("Unknown delivery status", 422);
  const result = await gatewayRequest<WebhookDeliveryRead[]>(`/v1/webhooks/${id}/deliveries`, {
    bearer: auth.token,
    query: { limit, offset, status: status ?? undefined },
  });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}
