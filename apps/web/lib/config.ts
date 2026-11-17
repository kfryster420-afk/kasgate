import type { KaspaNetwork } from "./types";

const rawNetwork = (process.env.NEXT_PUBLIC_KASPA_NETWORK ?? "kaspa:testnet-10").trim().toLowerCase();

/** Accepts "kaspa:testnet-10", "testnet-10", "kaspa:mainnet" or "mainnet". */
export const KASPA_NETWORK: KaspaNetwork = rawNetwork.includes("testnet") ? "kaspa:testnet-10" : "kaspa:mainnet";
export const IS_TESTNET = KASPA_NETWORK === "kaspa:testnet-10";
export const ADDRESS_PREFIX = IS_TESTNET ? "kaspatest:" : "kaspa:";
export const NETWORK_LABEL = IS_TESTNET ? "testnet-10" : "mainnet";

export const GATEWAY_PUBLIC_URL = (process.env.NEXT_PUBLIC_GATEWAY_URL ?? "http://localhost:8000").replace(/\/+$/, "");

export const SOMPI_PER_KAS = 100_000_000n;
export const PLATFORM_FEE_BPS = 150n;
export const BPS_DENOMINATOR = 10_000n;
export const MAX_SOMPI = 9_223_372_036_854_775_807n;

export const SESSION_COOKIE = "kg_session";
export const SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30;
