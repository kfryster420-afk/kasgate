import "server-only";
import { checkKaspaAddress, checkSlug, type Check } from "../validation";

const NONCE_RE = /^[0-9a-f]{32}$/;

/** Shared parsing for the paging query of the merchant prepaid listings. */
export function pageParams(params: URLSearchParams, maxLimit: number): Check<{ limit: number; offset: number }> {
  const parse = (value: string | null, fallback: number, min: number, max: number): number | null => {
    if (value === null) return fallback;
    if (!/^\d{1,7}$/.test(value)) return null;
    const n = Number(value);
    return n >= min && n <= max ? n : null;
  };
  const limit = parse(params.get("limit"), 50, 1, maxLimit);
  const offset = parse(params.get("offset"), 0, 0, 1_000_000);
  if (limit === null || offset === null) return { ok: false, error: `limit must be 1–${maxLimit} and offset a non-negative integer` };
  return { ok: true, value: { limit, offset } };
}

/** The public buyer lookups (balance, refund status) take an endpoint slug and the depositing address. */
export function buyerParams(params: URLSearchParams): Check<{ slug: string; payer_address: string }> {
  const slug = checkSlug(params.get("slug") ?? "");
  if (!slug.ok) return slug;
  const address = checkKaspaAddress(params.get("payer_address") ?? "");
  if (!address.ok) return address;
  return { ok: true, value: { slug: slug.value, payer_address: address.value } };
}

/** schemas.PrepaidRefundSigned: {nonce: 32 hex, signature: as returned by the wallet}; the gateway normalises the signature. */
export function checkSignedBody(body: Record<string, unknown>): Check<{ nonce: string; signature: string }> {
  const keys = Object.keys(body);
  if (keys.some((key) => key !== "nonce" && key !== "signature")) return { ok: false, error: "Only nonce and signature are accepted" };
  if (typeof body.nonce !== "string" || !NONCE_RE.test(body.nonce)) return { ok: false, error: "nonce must be the 32-hex challenge nonce" };
  if (typeof body.signature !== "string") return { ok: false, error: "signature is required" };
  const signature = body.signature.trim();
  if (signature.length < 64 || signature.length > 200 || /\s/.test(signature)) return { ok: false, error: "signature does not look like a wallet signature" };
  return { ok: true, value: { nonce: body.nonce, signature } };
}
