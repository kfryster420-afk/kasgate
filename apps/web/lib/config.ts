import type { KaspaNetwork } from "./types";

const rawNetwork = (process.env.NEXT_PUBLIC_KASPA_NETWORK ?? "kaspa:testnet-10").trim().toLowerCase();

/** Accepts "kaspa:testnet-10", "testnet-10", "kaspa:mainnet" or "mainnet". */
export const KASPA_NETWORK: KaspaNetwork = rawNetwork.includes("testnet") ? "kaspa:testnet-10" : "kaspa:mainnet";
export const IS_TESTNET = KASPA_NETWORK === "kaspa:testnet-10";
export const ADDRESS_PREFIX = IS_TESTNET ? "kaspatest:" : "kaspa:";
export const NETWORK_LABEL = IS_TESTNET ? "testnet-10" : "mainnet";

/** Hosts the wallet challenge may name as its Domain: the host(name) of the gateway PUBLIC_BASE_URL. */
function gatewayDomains(url: string): readonly string[] {
  try {
    const parsed = new URL(url);
    return Array.from(new Set([parsed.host.toLowerCase(), parsed.hostname.toLowerCase()]));
  } catch {
    return [];
  }
}

export const GATEWAY_PUBLIC_URL = (process.env.NEXT_PUBLIC_GATEWAY_URL ?? "http://localhost:8000").replace(/\/+$/, "");

export const EXPECTED_AUTH_DOMAINS: readonly string[] = gatewayDomains(GATEWAY_PUBLIC_URL);

export const SOMPI_PER_KAS = 100_000_000n;
export const PLATFORM_FEE_BPS = 150n;
export const BPS_DENOMINATOR = 10_000n;
/** Largest total price whose decimal string still fits SompiStr (max 18 digits, CONTRACT v2). */
export const MAX_SOMPI = 999_999_999_999_999_999n;

export const SESSION_COOKIE = "kg_session";
/** Fallback cookie lifetime (gateway session_ttl_seconds default: 7 days) when no expires_at is available. */
export const SESSION_MAX_AGE_SECONDS = 604_800;
