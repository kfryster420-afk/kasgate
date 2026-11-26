import type { NextRequest } from "next/server";
import type { NextResponse } from "next/server";
import { gatewayRequest } from "@/lib/server/gateway";
import { fromGatewayFailure, isAuth, jsonError, jsonOk, readJsonBody, requireSession } from "@/lib/server/route";
import { checkTxId, isUuid } from "@/lib/validation";
import type { PrepaidRefundRead } from "@/lib/types";

export const dynamic = "force-dynamic";

type Context = { params: Promise<{ id: string }> };

/** Largest signed transaction JSON the gateway accepts (schemas.MAX_RAW_TX_CHARS). */
const MAX_TRANSACTION_CHARS = 24_000;

/**
 * Merchant [session]: records the payout of an open refund, EITHER the `tx_id` of a payment the merchant already
 * broadcast from their own wallet (the dashboard uses KasWare sendKaspa) OR a signed `transaction` for the gateway to
 * broadcast. 200 = seen on-chain (`paid`); 202 = recorded as `payout_submitted`, re-checked by the ledger worker.
 */
export async function POST(request: NextRequest, context: Context): Promise<NextResponse> {
  const auth = await requireSession(request);
  if (!isAuth(auth)) return auth;
  const { id } = await context.params;
  if (!isUuid(id)) return jsonError("Invalid refund id", 400);
  const body = await readJsonBody(request);
  if (!body) return jsonError("Request body must be a JSON object", 400);
  if (Object.keys(body).some((key) => key !== "tx_id" && key !== "transaction")) return jsonError("Only tx_id or transaction is accepted", 422);
  const hasTx = body.tx_id !== undefined && body.tx_id !== null;
  const hasRaw = body.transaction !== undefined && body.transaction !== null;
  if (hasTx === hasRaw) return jsonError("send exactly one of tx_id or transaction", 422, "invalid_payout");
  let payload: { tx_id: string } | { transaction: string };
  if (hasTx) {
    const tx = checkTxId(body.tx_id);
    if (!tx.ok) return jsonError(tx.error, 422, "invalid_payout");
    payload = { tx_id: tx.value };
  } else {
    if (typeof body.transaction !== "string" || body.transaction.length < 2 || body.transaction.length > MAX_TRANSACTION_CHARS) {
      return jsonError("transaction must be the signed Kaspa transaction JSON", 422, "invalid_payout");
    }
    payload = { transaction: body.transaction };
  }
  const result = await gatewayRequest<PrepaidRefundRead>(`/v1/prepaid/refunds/${id.toLowerCase()}/payout`, {
    method: "POST",
    bearer: auth.token,
    body: JSON.stringify(payload),
  });
  return result.ok ? jsonOk(result.data, result.status) : fromGatewayFailure(result);
}
