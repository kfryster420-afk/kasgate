import type { BuyerCreditStatus, BuyerCreditStatusItem, FailedDeliveryCreditReason } from "./types";

/**
 * CONTRACT v2.14 buyer credit status (PR #3 @79efeff): POST /v1/prepaid/credits/challenge {slug, payer_address}
 * -> sign `message` (Action: credits) -> POST /v1/prepaid/credits/status {nonce, signature, before?, limit?}.
 * Each page needs a fresh challenge. Every value is checked here before it is shown or signed.
 */
export const CREDIT_STATUS_MAX_LIMIT = 100;
export const CREDIT_STATUS_PAGE = 10;
/** The opaque keyset cursor is unsigned base64url; bound its size so a broken reply can't grow the page state. */
export const CREDIT_CURSOR_MAX_CHARS = 1024;

const SOMPI_RE = /^(?:0|[1-9][0-9]{0,17})$/;
export const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const TX_RE = /^[0-9a-f]{64}$/;
const SLUG_RE = /^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$/;
const CURSOR_RE = /^[A-Za-z0-9_-]+={0,2}$/;
const REASONS: readonly FailedDeliveryCreditReason[] = ["attempts_exhausted", "window_expired"];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function exactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const own = Object.keys(value);
  return own.length === keys.length && keys.every((key) => own.includes(key));
}

/** A decimal sompi string (^(?:0|[1-9][0-9]{0,17})$) that BigInt parses; null otherwise. */
export function parseSompi(value: unknown): bigint | null {
  if (typeof value !== "string" || !SOMPI_RE.test(value)) return null;
  return BigInt(value);
}

export function isCreditCursor(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= CREDIT_CURSOR_MAX_CHARS && CURSOR_RE.test(value);
}

function isIsoDate(value: unknown): value is string {
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}T/.test(value) && !Number.isNaN(Date.parse(value));
}

function creditItem(value: unknown): BuyerCreditStatusItem | null {
  if (!isRecord(value) || !exactKeys(value, ["credit_id", "tx_id", "slug", "credit_sompi", "shortfall_sompi", "reason", "created_at"])) return null;
  const { credit_id, tx_id, slug, credit_sompi, shortfall_sompi, reason, created_at } = value;
  if (typeof credit_id !== "string" || !UUID_RE.test(credit_id)) return null;
  if (typeof tx_id !== "string" || !TX_RE.test(tx_id)) return null;
  if (typeof slug !== "string" || !SLUG_RE.test(slug)) return null;
  if (parseSompi(credit_sompi) === null || parseSompi(shortfall_sompi) === null) return null;
  if (typeof reason !== "string" || !(REASONS as readonly string[]).includes(reason)) return null;
  if (!isIsoDate(created_at)) return null;
  return { credit_id, tx_id, slug, credit_sompi: credit_sompi as string, shortfall_sompi: shortfall_sompi as string, reason: reason as FailedDeliveryCreditReason, created_at };
}

/** schemas.BuyerCreditStatus with extra="forbid": exactly these keys and value shapes, or null. */
export function parseBuyerCreditStatus(value: unknown, limit: number = CREDIT_STATUS_MAX_LIMIT): BuyerCreditStatus | null {
  if (!isRecord(value) || !exactKeys(value, ["merchant_id", "credited_sompi", "credit_balance_sompi", "refundable_sompi", "credit_count", "credits", "next_cursor"])) return null;
  const { merchant_id, credited_sompi, credit_balance_sompi, refundable_sompi, credit_count, credits, next_cursor } = value;
  if (typeof merchant_id !== "string" || !UUID_RE.test(merchant_id)) return null;
  const credited = parseSompi(credited_sompi);
  const balance = parseSompi(credit_balance_sompi);
  if (credited === null || balance === null || parseSompi(refundable_sompi) === null || balance > credited) return null;
  if (typeof credit_count !== "number" || !Number.isSafeInteger(credit_count) || credit_count < 0) return null;
  if (!Array.isArray(credits) || credits.length > Math.min(limit, CREDIT_STATUS_MAX_LIMIT) || credits.length > credit_count) return null;
  const items = credits.map(creditItem);
  if (items.some((item) => item === null)) return null;
  if (next_cursor !== null && !isCreditCursor(next_cursor)) return null;
  return {
    merchant_id,
    credited_sompi: credited_sompi as string,
    credit_balance_sompi: credit_balance_sompi as string,
    refundable_sompi: refundable_sompi as string,
    credit_count,
    credits: items as BuyerCreditStatusItem[],
    next_cursor: next_cursor as string | null,
  };
}
