import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { CREDIT_STATUS_MAX_LIMIT, CREDIT_STATUS_PAGE, isCreditCursor, parseBuyerCreditStatus } from "@/lib/credit-status";
import { gatewayRequest } from "@/lib/server/gateway";
import { checkSameOrigin, fromGatewayFailure, jsonError, jsonOk, readJsonBody } from "@/lib/server/route";
import type { PrepaidCreditStatusRequest } from "@/lib/types";

export const dynamic = "force-dynamic";

const KEYS = new Set(["nonce", "signature", "before", "limit"]);

/**
 * Buyer step 2 [signature-authorised, CONTRACT v2.14]: one page of this key's failed-delivery credits with the
 * credited / call-only / refundable totals. Strict body {nonce, signature, before?, limit 1..100}; the gateway's
 * reply is validated against schemas.BuyerCreditStatus before it is returned. 400 invalid_cursor is passed through.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const blocked = checkSameOrigin(request);
  if (blocked) return blocked;
  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);
  if (Object.keys(body).some((key) => !KEYS.has(key))) return jsonError("Only nonce, signature, before and limit are accepted", 422);
  if (typeof body.nonce !== "string" || !/^[0-9a-f]{32}$/.test(body.nonce)) return jsonError("nonce must be the 32-hex challenge nonce", 422);
  const signature = typeof body.signature === "string" ? body.signature.trim().toLowerCase() : "";
  if (!/^[0-9a-f]{128}$/.test(signature)) return jsonError("signature must be the 128-hex Schnorr signature", 422);
  const payload: PrepaidCreditStatusRequest = { nonce: body.nonce, signature };
  if (body.before !== undefined && body.before !== null) {
    if (!isCreditCursor(body.before)) return jsonError("before must be the next_cursor of the previous page", 422);
    payload.before = body.before;
  }
  const limit = body.limit === undefined ? CREDIT_STATUS_PAGE : body.limit;
  if (typeof limit !== "number" || !Number.isInteger(limit) || limit < 1 || limit > CREDIT_STATUS_MAX_LIMIT) return jsonError(`limit must be an integer 1–${CREDIT_STATUS_MAX_LIMIT}`, 422);
  payload.limit = limit;
  const result = await gatewayRequest<unknown>("/v1/prepaid/credits/status", { method: "POST", body: JSON.stringify(payload) });
  if (!result.ok) return fromGatewayFailure(result);
  const status = parseBuyerCreditStatus(result.data, limit);
  if (!status) return jsonError("The gateway's credit status reply did not have the documented shape.", 502);
  return jsonOk(status);
}
