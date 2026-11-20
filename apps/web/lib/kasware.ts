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

const HEX_PUBLIC_KEY = /^(?:[0-9a-f]{64}|0[23][0-9a-f]{64})$/i;

/** Public keys are trimmed and checked for shape only; signatures are never modified (the gateway normalises them). */
export function isPublicKey(value: string): boolean {
  return HEX_PUBLIC_KEY.test(value.trim());
}

/** Signature sanity check that does not transform the value: non-empty single token of plausible length. */
export function isSignatureToken(value: string): boolean {
  const v = value.trim();
  return v.length >= 64 && v.length <= 200 && !/\s/.test(v);
}
