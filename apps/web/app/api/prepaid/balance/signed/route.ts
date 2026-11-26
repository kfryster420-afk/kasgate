import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { checkBalanceTimestamp, HEADER_BALANCE_SIGNATURE, HEADER_BALANCE_TIMESTAMP, normaliseBalanceSignature } from "@/lib/balance-read";
import { gatewayRequest } from "@/lib/server/gateway";
import { checkSameOrigin, fromGatewayFailure, jsonError, jsonOk, readJsonBody } from "@/lib/server/route";
import type { PrepaidBalanceRead } from "@/lib/types";
import { checkKaspaAddress, checkSlug } from "@/lib/validation";

export const dynamic = "force-dynamic";

const KEYS = new Set(["slug", "payer_address", "timestamp", "signature"]);

/**
 * Buyer [signature-authorised] (CONTRACT v2.12.1): the balance WITH the ledger totals. The browser signs the
 * timestamped balance-read message in KasWare; this handler validates the shapes and sends them to
 * GET /v1/prepaid/balance as X-KasGate-Balance-Timestamp / X-KasGate-Balance-Signature. A bad signature or a clock
 * more than 300 s off -> the gateway's 401 invalid_balance_signature, passed through.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const blocked = checkSameOrigin(request);
  if (blocked) return blocked;
  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);
  if (Object.keys(body).some((key) => !KEYS.has(key))) return jsonError("Only slug, payer_address, timestamp and signature are accepted", 422);
  const slug = checkSlug(typeof body.slug === "string" ? body.slug : "");
  if (!slug.ok) return jsonError(slug.error, 422);
  const address = checkKaspaAddress(typeof body.payer_address === "string" ? body.payer_address : "");
  if (!address.ok) return jsonError(address.error, 422);
  const timestamp = typeof body.timestamp === "string" ? checkBalanceTimestamp(body.timestamp) : null;
  if (timestamp === null) return jsonError("timestamp must be unix seconds (1–12 digits) as a string", 422);
  const signature = typeof body.signature === "string" ? normaliseBalanceSignature(body.signature) : null;
  if (signature === null) return jsonError("signature must be the 128-hex Schnorr signature", 422);
  const result = await gatewayRequest<PrepaidBalanceRead>("/v1/prepaid/balance", {
    query: { slug: slug.value, payer_address: address.value },
    headers: { [HEADER_BALANCE_TIMESTAMP]: timestamp, [HEADER_BALANCE_SIGNATURE]: signature },
  });
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}
