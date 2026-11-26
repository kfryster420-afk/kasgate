import "server-only";
import type { CreditUsd, SettlementRead } from "../types";
import { gatewayRequest } from "./gateway";

/** GET /v1/settlements is newest first and capped at 200 per page; credits look back at most this many settlements. */
const PAGE = 200;
const MAX_PAGES = 5;

/**
 * USD at settlement for failed-delivery credits: the gateway's credit and ledger rows carry the credited call's
 * `settlement_id` but no rate, so the rate comes from that settlement (kas_usd_rate, usd_value_at_settlement).
 * Pages through the merchant's newest settlements until every id is found or MAX_PAGES * PAGE rows were read.
 * Ids not found map to nothing (the UI says the rate wasn't found), never to a current price.
 */
export async function settlementUsd(token: string, ids: ReadonlySet<string>): Promise<Map<string, CreditUsd>> {
  const found = new Map<string, CreditUsd>();
  if (ids.size === 0) return found;
  for (let page = 0; page < MAX_PAGES && found.size < ids.size; page += 1) {
    const result = await gatewayRequest<SettlementRead[]>("/v1/settlements", { bearer: token, query: { limit: PAGE, offset: page * PAGE } });
    if (!result.ok || !Array.isArray(result.data)) break;
    for (const row of result.data) {
      if (ids.has(row.id)) found.set(row.id, { kas_usd_rate: row.kas_usd_rate, call_usd: row.usd_value_at_settlement });
    }
    if (result.data.length < PAGE) break;
  }
  return found;
}
