import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { parseSignedRead } from "@/lib/server/buyer-read";
import { gatewayRequest } from "@/lib/server/gateway";
import { checkSameOrigin, fromGatewayFailure, jsonError, jsonOk, readJsonBody } from "@/lib/server/route";
import type { PrepaidBalanceRead } from "@/lib/types";

export const dynamic = "force-dynamic";

/**
 * Buyer [signature-authorised] (CONTRACT v2.12.1, #6): the full balance (balance, ledger totals, updated_at). The
 * browser signs the timestamped prepaid-balance-read text in KasWare; this handler validates the shapes and sends
 * them to GET /v1/prepaid/balance as X-KasGate-Balance-Timestamp / X-KasGate-Balance-Signature. A bad signature or a
 * clock more than 300 s off -> the gateway's 401 invalid_balance_signature, passed through.
 */
export async function POST(request: NextRequest): Promise<NextResponse> {
  const blocked = checkSameOrigin(request);
  if (blocked) return blocked;
  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);
  const read = parseSignedRead(body);
  if (!read.ok) return jsonError(read.error, 422);
  const result = await gatewayRequest<PrepaidBalanceRead>("/v1/prepaid/balance", read.value);
  return result.ok ? jsonOk(result.data) : fromGatewayFailure(result);
}
