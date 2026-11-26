import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { EXPECTED_AUTH_DOMAIN } from "@/lib/config";
import { checkRefundChallenge } from "@/lib/refund-challenge";
import { stringifyWithIntegers } from "@/lib/safe-json";
import { gatewayRequest } from "@/lib/server/gateway";
import { checkSameOrigin, fromGatewayFailure, jsonError, jsonOk, readJsonBody } from "@/lib/server/route";
import { checkKaspaAddress, checkSlug, checkSompiAmount, isUuid } from "@/lib/validation";
import type { PrepaidRefundChallenge } from "@/lib/types";

export const dynamic = "force-dynamic";

const ALLOWED = new Set(["slug", "payer_address", "action", "amount_sompi", "refund_id"]);

/**
 * Buyer step 1 (public): the text to sign to open (`request`) or withdraw (`cancel`) a refund, or to read this
 * key's refunds with the merchant's reasons (`status`). Mirrors
 * schemas.PrepaidRefundChallengeRequest and refuses to pass on a challenge whose lines do not match the request.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const blocked = checkSameOrigin(request);
  if (blocked) return blocked;
  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);
  const unknown = Object.keys(body).filter((key) => !ALLOWED.has(key));
  if (unknown.length > 0) return jsonError(`Unknown field(s): ${unknown.join(", ")}`, 422);
  const slug = checkSlug(body.slug);
  if (!slug.ok) return jsonError(slug.error, 422);
  const address = checkKaspaAddress(body.payer_address);
  if (!address.ok) return jsonError(address.error, 422);
  const action = body.action === undefined ? "request" : body.action;
  if (action !== "request" && action !== "cancel" && action !== "status") return jsonError('action must be "request", "cancel" or "status"', 422);

  const payload: Record<string, unknown> = { slug: slug.value, payer_address: address.value, action };
  let amount: string | undefined;
  let refundId: string | undefined;
  if (action === "request") {
    if (body.refund_id !== undefined && body.refund_id !== null) return jsonError("refund_id is only used to cancel a refund", 422);
    if (body.amount_sompi !== undefined && body.amount_sompi !== null) {
      const checked = checkSompiAmount(body.amount_sompi);
      if (!checked.ok) return jsonError(checked.error, 422);
      amount = checked.value;
      payload.amount_sompi = amount;
    }
  } else if (action === "status") {
    const hasAmount = body.amount_sompi !== undefined && body.amount_sompi !== null;
    const hasRefund = body.refund_id !== undefined && body.refund_id !== null;
    if (hasAmount || hasRefund) return jsonError("a status challenge takes neither amount_sompi nor refund_id", 422);
  } else {
    if (body.amount_sompi !== undefined && body.amount_sompi !== null) return jsonError("amount_sompi must be omitted when cancelling a refund", 422);
    if (typeof body.refund_id !== "string" || !isUuid(body.refund_id)) return jsonError("refund_id is required to cancel a refund", 422);
    refundId = body.refund_id.toLowerCase();
    payload.refund_id = refundId;
  }

  const result = await gatewayRequest<PrepaidRefundChallenge>("/v1/prepaid/refunds/challenge", {
    method: "POST",
    body: stringifyWithIntegers(payload, ["amount_sompi"]),
  });
  if (!result.ok) return fromGatewayFailure(result);
  const refusal = checkRefundChallenge(result.data, {
    action,
    address: address.value,
    domain: EXPECTED_AUTH_DOMAIN,
    amountSompi: amount,
    refundId,
  });
  if (refusal) return jsonError(refusal, 502);
  return jsonOk(result.data, 201);
}
