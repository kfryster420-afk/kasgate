import "server-only";
import { isPublicKey, isSignatureToken } from "../kasware";
import type { AuthVerifyRequest, MerchantCreate } from "../types";
import { checkDisplayName, checkKaspaAddress, checkOptionalEmail, type Check } from "../validation";

interface WalletProof {
  kaspa_address: string;
  challenge: string;
  public_key: string;
  signature: string;
}

/** Shape-checks the wallet proof. The signature is forwarded exactly as received (only the public key is trimmed). */
export function checkWalletProof(body: Record<string, unknown>): Check<WalletProof> {
  const address = checkKaspaAddress(body.kaspa_address);
  if (!address.ok) return address;
  const challenge = typeof body.challenge === "string" ? body.challenge : "";
  if (challenge.length < 16 || challenge.length > 512) return { ok: false, error: "Challenge is missing or malformed" };
  const publicKey = typeof body.public_key === "string" ? body.public_key.trim() : "";
  if (!isPublicKey(publicKey)) return { ok: false, error: "Public key must be 64 or 66 hex characters" };
  const signature = typeof body.signature === "string" ? body.signature : "";
  if (!isSignatureToken(signature)) return { ok: false, error: "Signature is missing or malformed" };
  return { ok: true, value: { kaspa_address: address.value, challenge, public_key: publicKey, signature } };
}

export function checkVerifyRequest(body: Record<string, unknown>): Check<AuthVerifyRequest> {
  return checkWalletProof(body);
}

export function checkMerchantCreate(body: Record<string, unknown>): Check<MerchantCreate> {
  const proof = checkWalletProof(body);
  if (!proof.ok) return proof;
  const name = checkDisplayName(body.display_name);
  if (!name.ok) return name;
  const email = checkOptionalEmail(body.email);
  if (!email.ok) return email;
  const value: MerchantCreate = { ...proof.value, display_name: name.value };
  if (email.value) value.email = email.value;
  return { ok: true, value };
}
