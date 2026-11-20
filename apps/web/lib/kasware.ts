/** Typed access to the KasWare browser wallet (window.kasware) and signature normalisation for the gateway. */

export interface KaswareSignOptions {
  type?: "auto" | "schnorr" | "ecdsa";
  noAuxRand?: boolean;
}

export interface KaswareProvider {
  requestAccounts(): Promise<string[]>;
  getAccounts(): Promise<string[]>;
  getPublicKey?: () => Promise<string>;
  /** Optional: read-only, no approval prompt. Official values are listed at KaswareNetworkId; may be "" when not connected. */
  getNetwork?: () => Promise<string>;
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

/** Networks a KasWare `getNetwork()` answer can be mapped to. `unknown` means "do not block". */
export type KaswareNetworkId = "mainnet" | "testnet-10" | "testnet-11" | "testnet-12" | "devnet" | "simnet" | "unknown";

export const NETWORK_FRIENDLY_NAME: Record<Exclude<KaswareNetworkId, "unknown">, string> = {
  mainnet: "Mainnet",
  "testnet-10": "Testnet 10",
  "testnet-11": "Testnet 11",
  "testnet-12": "Testnet 12",
  devnet: "Devnet",
  simnet: "Simnet",
};

/**
 * Tolerant mapping of whatever `getNetwork()` returns. Documented values (docs.kasware.xyz, Connection API):
 * "kaspa_mainnet", "kaspa_testnet_10", "kaspa_testnet_11", "kaspa_testnet_12", "kaspa_devnet", and "" when not connected.
 * Also accepted: gateway style ("kaspa:testnet-10"), human labels ("Testnet 10", "Mainnet"), legacy "livenet", and a bare
 * "testnet" (older KasWare and the Kasanova bridge use it for Testnet 10). Anything else is "unknown".
 */
export function normalizeKaswareNetwork(raw: unknown): KaswareNetworkId {
  if (typeof raw !== "string") return "unknown";
  const text = raw.trim().toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  if (!text) return "unknown";
  if (/\bsimnet\b/.test(text)) return "simnet";
  if (/\bdevnet\b/.test(text)) return "devnet";
  if (/\b(mainnet|main net|livenet)\b/.test(text) || text === "kaspa main") return "mainnet";
  const numbered = /\b(?:testnet|tn)\s?(\d{1,3})\b/.exec(text);
  if (numbered) {
    const id = `testnet-${Number(numbered[1])}`;
    return id === "testnet-10" || id === "testnet-11" || id === "testnet-12" ? id : "unknown";
  }
  if (/\btestnet\b/.test(text)) return "testnet-10";
  return "unknown";
}

/** Maps the gateway's `KaspaNetwork` ("kaspa:testnet-10" | "kaspa:mainnet") to a KaswareNetworkId. */
export function expectedKaswareNetwork(gatewayNetwork: string): Exclude<KaswareNetworkId, "unknown"> {
  return gatewayNetwork.toLowerCase().includes("testnet") ? "testnet-10" : "mainnet";
}

/**
 * Returns a refusal message when KasWare reports a known network that differs from the expected one, otherwise null.
 * A missing getNetwork, an error, an empty answer or an unrecognised value never blocks (the address-prefix guard remains).
 */
export async function kaswareNetworkRefusal(wallet: KaswareProvider, gatewayNetwork: string): Promise<string | null> {
  if (typeof wallet.getNetwork !== "function") return null;
  let actual: KaswareNetworkId;
  try {
    actual = normalizeKaswareNetwork(await wallet.getNetwork());
  } catch {
    return null;
  }
  if (actual === "unknown") return null;
  const expected = expectedKaswareNetwork(gatewayNetwork);
  if (actual === expected) return null;
  return `KasWare is on ${NETWORK_FRIENDLY_NAME[actual]}. Switch KasWare to ${NETWORK_FRIENDLY_NAME[expected]} and try again.`;
}
