import "server-only";
import { normalizePublicKey, normalizeSignature } from "../kasware";
import type { AuthVerifyRequest } from "../types";
import { checkKaspaAddress, type Check } from "../validation";

/** Validates the browser's wallet-signature payload before it is forwarded to POST /v1/auth/verify. */
export function checkVerifyRequest(body: Record<string, unknown>): Check<AuthVerifyRequest> {
  const address = checkKaspaAddress(body.kaspa_address);
  if (!address.ok) return address;
  const challenge = typeof body.challenge === "string" ? body.challenge : "";
  if (challenge.length < 16 || challenge.length > 512) return { ok: false, error: "Challenge is missing or malformed" };
  const publicKey = typeof body.public_key === "string" ? normalizePublicKey(body.public_key) : null;
  if (!publicKey) return { ok: false, error: "Public key must be 64 or 66 hex characters" };
  const signature = typeof body.signature === "string" ? normalizeSignature(body.signature) : null;
  if (!signature) return { ok: false, error: "Signature must be 128 hex characters (or a Base64 wallet signature)" };
  return { ok: true, value: { kaspa_address: address.value, challenge, public_key: publicKey, signature } };
}
