import { isIP } from "node:net";

/** Largest accepted TRUSTED_PROXY_HOPS; more reverse proxies than this in front of the dashboard is a misconfiguration. */
export const MAX_TRUSTED_PROXY_HOPS = 5;

/**
 * TRUSTED_PROXY_HOPS: how many reverse proxies YOU run in front of this Next.js server, each of which sets or appends
 * `X-Forwarded-For` (the RUNBOOK nginx overwrites it with `$remote_addr`: 1). Default 0 = no trusted proxy: the inbound
 * header may come straight from the browser, so it is never trusted and nothing is forwarded to the gateway.
 * Anything other than a whole number 0..MAX_TRUSTED_PROXY_HOPS falls back to 0.
 */
export function parseTrustedProxyHops(raw: string | undefined): number {
  if (raw === undefined || raw.trim() === "") return 0;
  const value = raw.trim();
  if (!/^\d$/.test(value)) return 0;
  const hops = Number(value);
  return hops <= MAX_TRUSTED_PROXY_HOPS ? hops : 0;
}

/** A single normalised IP literal (IPv4-mapped IPv6 unwrapped), or null when `value` is not exactly one IP. */
export function normaliseIp(value: string): string | null {
  const trimmed = value.trim();
  const mapped = /^::ffff:(\d{1,3}(?:\.\d{1,3}){3})$/i.exec(trimmed);
  const candidate = mapped?.[1] ?? trimmed;
  return isIP(candidate) === 0 ? null : candidate.toLowerCase();
}

/**
 * The browser's IP as established by the trusted proxies: the `hops`-th `X-Forwarded-For` entry from the right (the
 * address the outermost trusted proxy saw). Entries further left were supplied by the client and are ignored. Returns
 * undefined (forward nothing) when no proxy is trusted, the header has fewer entries than `hops`, or that entry is not
 * an IP: the gateway then rate-limits on this server's own address, which nobody can spoof.
 */
export function clientIpFromForwardedFor(header: string | null | undefined, hops: number): string | undefined {
  if (hops < 1 || !header) return undefined;
  const entries = header.split(",").map((entry) => entry.trim());
  if (entries.length < hops) return undefined;
  const entry = entries[entries.length - hops];
  return entry ? (normaliseIp(entry) ?? undefined) : undefined;
}
