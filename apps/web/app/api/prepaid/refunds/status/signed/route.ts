import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { parseSignedRead } from "@/lib/server/buyer-read";
import { gatewayRequest } from "@/lib/server/gateway";
import { checkSameOrigin, fromGatewayFailure, jsonError, jsonOk, readJsonBody } from "@/lib/server/route";
import type { PrepaidRefundRead } from "@/lib/types";

export const dynamic = "force-dynamic";

/**
 * Buyer [signature-authorised] (CONTRACT v2.12.1, #6 @a96a16a): this key's refunds at the merchant behind `slug`,
 * newest first, reasons and the claim still withheld. GET /v1/prepaid/refunds/status is buyer-signed: KasWare signs
 * the timestamped refunds-status-read text and this handler forwards it as X-KasGate-Balance-Timestamp /
 * X-KasGate-Balance-Signature. Unsigned, stale or wrong-key reads are the gateway's 401 invalid_balance_signature.
 * There is no unsigned variant in this app. Body: {slug, payer_address, timestamp, signature, limit? 1–50}.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const blocked = checkSameOrigin(request);
  if (blocked) return blocked;
  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);
  const read = parseSignedRead(body, ["limit"]);
  if (!read.ok) return jsonError(read.error, 422);
  const limit = body.limit === undefined ? 10 : body.limit;
  if (typeof limit !== "number" || !Number.isInteger(limit) || limit < 1 || limit > 50) return jsonError("limit must be an integer 1–50", 422);
  const result = await gatewayRequest<PrepaidRefundRead[]>("/v1/prepaid/refunds/status", { query: { ...read.value.query, limit }, headers: read.value.headers });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}
