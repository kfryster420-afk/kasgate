import "server-only";
import type { BuyerCreditItem, BuyerCreditSummary, FailedDeliveryCreditReason } from "../types";
import { isUuid } from "../validation";

const SOMPI_RE = /^(0|[1-9][0-9]{0,19})$/;
const TX_RE = /^[0-9a-f]{64}$/;
const SLUG_RE = /^[a-z0-9](?:[a-z0-9-]{1,62}[a-z0-9])$/;
const REASONS: readonly FailedDeliveryCreditReason[] = ["attempts_exhausted", "window_expired"];
const MAX_HEADER_CHARS = 16_384;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function onlyKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  return Object.keys(value).every((key) => keys.includes(key)) && keys.every((key) => key in value);
}

function sompi(value: unknown): value is string {
  return typeof value === "string" && SOMPI_RE.test(value);
}

function creditItem(value: unknown): BuyerCreditItem | null {
  if (!isRecord(value) || !onlyKeys(value, ["txId", "slug", "creditSompi", "shortfallSompi", "reason", "createdAt"])) return null;
  const { txId, slug, creditSompi, shortfallSompi, reason, createdAt } = value;
  if (typeof txId !== "string" || !TX_RE.test(txId) || typeof slug !== "string" || !SLUG_RE.test(slug)) return null;
  if (!sompi(creditSompi) || !sompi(shortfallSompi)) return null;
  if (typeof reason !== "string" || !(REASONS as readonly string[]).includes(reason)) return null;
  if (typeof createdAt !== "string" || Number.isNaN(Date.parse(createdAt))) return null;
  return { txId, slug, creditSompi, shortfallSompi, reason: reason as FailedDeliveryCreditReason, createdAt };
}

/**
 * Decodes `X-KasGate-Credit-Summary` (Base64 JSON, CONTRACT v2.14) from the signed refund-status reply. Returns null
 * when the header is absent or does not have exactly the documented shape, so a malformed header is never shown.
 */
export function parseCreditSummary(header: string | null): BuyerCreditSummary | null {
  if (!header || header.length > MAX_HEADER_CHARS || !/^[A-Za-z0-9+/]+={0,2}$/.test(header.trim())) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(Buffer.from(header.trim(), "base64").toString("utf8"));
  } catch {
    return null;
  }
  if (!isRecord(parsed) || !onlyKeys(parsed, ["merchantId", "creditedSompi", "creditBalanceSompi", "refundableSompi", "creditCount", "credits"])) return null;
  const { merchantId, creditedSompi, creditBalanceSompi, refundableSompi, creditCount, credits } = parsed;
  if (typeof merchantId !== "string" || !isUuid(merchantId)) return null;
  if (!sompi(creditedSompi) || !sompi(creditBalanceSompi) || !sompi(refundableSompi)) return null;
  if (typeof creditCount !== "number" || !Number.isInteger(creditCount) || creditCount < 0) return null;
  if (!Array.isArray(credits) || credits.length > 10) return null;
  const items = credits.map(creditItem);
  if (items.some((item) => item === null)) return null;
  return { merchantId, creditedSompi, creditBalanceSompi, refundableSompi, creditCount, credits: items as BuyerCreditItem[] };
}
