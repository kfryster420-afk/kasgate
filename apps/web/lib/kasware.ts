/** Typed access to the KasWare browser wallet (window.kasware) and signature normalisation for the gateway. */

export interface KaswareSignOptions {
  type?: "auto" | "schnorr" | "ecdsa";
  noAuxRand?: boolean;
}

export interface KaswareProvider {
  requestAccounts(): Promise<string[]>;
  getAccounts(): Promise<string[]>;
  getPublicKey?: () => Promise<string>;
  signMessage(message: string, options?: KaswareSignOptions | "auto" | "schnorr" | "ecdsa"): Promise<string>;
}

declare global {
  interface Window {
    kasware?: KaswareProvider;
  }
}

export function getKasware(): KaswareProvider | null {
  if (typeof window === "undefined") return null;
  const provider = window.kasware;
  return provider && typeof provider.requestAccounts === "function" && typeof provider.signMessage === "function" ? provider : null;
}

const HEX_SIGNATURE = /^[0-9a-f]{128}$/;
const HEX_PUBLIC_KEY = /^(?:[0-9a-f]{64}|0[23][0-9a-f]{64})$/;

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function base64ToBytes(value: string): Uint8Array | null {
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(value) || value.length % 4 !== 0) return null;
  try {
    const raw = atob(value);
    return Uint8Array.from(raw, (c) => c.charCodeAt(0));
  } catch {
    return null;
  }
}

/**
 * The gateway requires a 128-hex (64-byte) signature. Wallets may return hex directly, or Base64 of the 64-byte
 * signature, or Base64 of 65 bytes (one leading header byte + 64-byte signature). Returns null if unrecognised.
 */
export function normalizeSignature(input: string): string | null {
  const value = input.trim();
  const lower = value.toLowerCase().replace(/^0x/, "");
  if (HEX_SIGNATURE.test(lower)) return lower;
  const bytes = base64ToBytes(value);
  if (!bytes) return null;
  if (bytes.length === 64) return bytesToHex(bytes);
  if (bytes.length === 65) return bytesToHex(bytes.slice(1));
  return null;
}

export function normalizePublicKey(input: string): string | null {
  const lower = input.trim().toLowerCase().replace(/^0x/, "");
  return HEX_PUBLIC_KEY.test(lower) ? lower : null;
}
