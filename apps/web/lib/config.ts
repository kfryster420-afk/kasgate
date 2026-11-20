import type { KaspaNetwork } from "./types";

const rawNetwork = (process.env.NEXT_PUBLIC_KASPA_NETWORK ?? "kaspa:testnet-10").trim().toLowerCase();

/** Accepts "kaspa:testnet-10", "testnet-10", "kaspa:mainnet" or "mainnet". */
export const KASPA_NETWORK: KaspaNetwork = rawNetwork.includes("testnet") ? "kaspa:testnet-10" : "kaspa:mainnet";
export const IS_TESTNET = KASPA_NETWORK === "kaspa:testnet-10";
export const ADDRESS_PREFIX = IS_TESTNET ? "kaspatest:" : "kaspa:";
export const NETWORK_LABEL = IS_TESTNET ? "testnet-10" : "mainnet";

export const GATEWAY_PUBLIC_URL = (process.env.NEXT_PUBLIC_GATEWAY_URL ?? "http://localhost:8000").replace(/\/+$/, "");

/**
 * Python `urlsplit(url).netloc`, lowercased: the raw text between "//" and the next "/", "?" or "#". Unlike `URL.host`
 * it keeps an explicit default port (":443"), so it equals what the gateway puts in the challenge "Domain:" line.
 */
export function netlocOf(url: string): string | null {
  const match = /^[A-Za-z][A-Za-z0-9+.-]*:\/\/([^/?#]*)/.exec(url.trim());
  const netloc = match?.[1];
  return netloc ? netloc.toLowerCase() : null;
}

/** The one Domain string a challenge must carry: netloc of NEXT_PUBLIC_GATEWAY_URL (must equal PUBLIC_BASE_URL's netloc). */
export const EXPECTED_AUTH_DOMAIN: string | null = netlocOf(GATEWAY_PUBLIC_URL);

export const SOMPI_PER_KAS = 100_000_000n;
export const PLATFORM_FEE_BPS = 150n;
export const BPS_DENOMINATOR = 10_000n;
/** Largest total price whose decimal string still fits SompiStr (max 18 digits, CONTRACT v2). */
export const MAX_SOMPI = 999_999_999_999_999_999n;

export const SESSION_COOKIE = "kg_session";
