import type { KaspaNetwork } from "./types";

const rawNetwork = (process.env.NEXT_PUBLIC_KASPA_NETWORK ?? "kaspa:testnet-10").trim().toLowerCase();

/** Accepts "kaspa:testnet-10", "testnet-10", "kaspa:mainnet" or "mainnet". */
export const KASPA_NETWORK: KaspaNetwork = rawNetwork.includes("testnet") ? "kaspa:testnet-10" : "kaspa:mainnet";
export const IS_TESTNET = KASPA_NETWORK === "kaspa:testnet-10";
export const ADDRESS_PREFIX = IS_TESTNET ? "kaspatest:" : "kaspa:";
export const NETWORK_LABEL = IS_TESTNET ? "testnet-10" : "mainnet";

/** Official TN-10 faucet, as listed on the Kaspa wiki (https://wiki.kaspa.org/en/testnets). */
export const TESTNET_FAUCET_URL = "https://faucet-tn10.kaspanet.io/";
/** explorer-tn10.kaspa.org answers 402 DEPLOYMENT_DISABLED, so links go to the Kaspa.stream TN10 explorer instead. */
export const TESTNET_EXPLORER_URL = "https://tn10.kaspa.stream";
/** A real settled testnet payment (demo-httpbin), shown on the home page. */
export const DEMO_SETTLEMENT_TX_ID = "7beab60d8cf21002ba12e85255ec93cf24c3c7c8f3e5a2d3dc84016da8e79170";
export const DEMO_ENDPOINT_SLUG = "demo-httpbin";
export const KASWARE_URL = "https://www.kasware.xyz";
export const GITHUB_REPO_URL = "https://github.com/kfryster420-afk/kasgate";
export const GITHUB_BUG_URL = `${GITHUB_REPO_URL}/issues/new?template=bug_report.yml`;
export const GITHUB_BETA_FEEDBACK_URL = `${GITHUB_REPO_URL}/issues/new?template=beta_feedback.yml`;
export const GITHUB_SECURITY_URL = `${GITHUB_REPO_URL}/security/advisories/new`;
export const GITHUB_TEST_PLAN_URL = `${GITHUB_REPO_URL}/blob/main/docs/beta/TEST_PLAN.md`;

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
/** 1 USD = 1,000,000 micro-dollars. Mirrors schemas.USD_MICROS_PER_DOLLAR. */
export const USD_MICROS_PER_DOLLAR = 1_000_000n;
/** Largest USD price in micro-dollars (schemas.MAX_USD_MICROS): $1,000,000,000. */
export const MAX_USD_MICROS = 1_000_000_000_000_000n;

export const SESSION_COOKIE = "kg_session";
