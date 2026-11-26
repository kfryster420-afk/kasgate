import "server-only";
import { checkBalanceTimestamp, HEADER_BALANCE_SIGNATURE, HEADER_BALANCE_TIMESTAMP, normaliseBalanceSignature } from "../balance-read";
import { checkKaspaAddress, checkSlug } from "../validation";

export interface SignedReadInput {
  query: { slug: string; payer_address: string };
  headers: Record<string, string>;
}

/**
 * The body of a buyer-signed read route: exactly {slug, payer_address, timestamp (string), signature} plus any
 * `extra` keys. Returns the gateway query and the X-KasGate-Balance-* headers, or a 422 message. The gateway checks
 * the window and the signature itself (401 invalid_balance_signature).
 */
export function parseSignedRead(body: Record<string, unknown>, extra: readonly string[] = []): { ok: true; value: SignedReadInput } | { ok: false; error: string } {
  const allowed = new Set(["slug", "payer_address", "timestamp", "signature", ...extra]);
  if (Object.keys(body).some((key) => !allowed.has(key))) return { ok: false, error: `Only ${[...allowed].join(", ")} are accepted` };
  const slug = checkSlug(typeof body.slug === "string" ? body.slug : "");
  if (!slug.ok) return { ok: false, error: slug.error };
  const address = checkKaspaAddress(typeof body.payer_address === "string" ? body.payer_address : "");
  if (!address.ok) return { ok: false, error: address.error };
  const timestamp = typeof body.timestamp === "string" ? checkBalanceTimestamp(body.timestamp) : null;
  if (timestamp === null) return { ok: false, error: "timestamp must be unix seconds (1–12 digits) as a string" };
  const signature = typeof body.signature === "string" ? normaliseBalanceSignature(body.signature) : null;
  if (signature === null) return { ok: false, error: "signature must be the 128-hex Schnorr signature" };
  return {
    ok: true,
    value: { query: { slug: slug.value, payer_address: address.value }, headers: { [HEADER_BALANCE_TIMESTAMP]: timestamp, [HEADER_BALANCE_SIGNATURE]: signature } },
  };
}
