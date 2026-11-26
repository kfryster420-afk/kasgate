import "server-only";
import { headers } from "next/headers";
import { parseGatewayJson } from "../safe-json";
import { clientIpFromForwardedFor, parseTrustedProxyHops } from "./client-ip";
import type { DirectoryManifest } from "../types";

export const GATEWAY_INTERNAL_URL = (process.env.GATEWAY_INTERNAL_URL ?? "http://gateway:8000").replace(/\/+$/, "");
/** Reverse proxies in front of this server whose X-Forwarded-For is trusted (README "Client IP"); 0 = none. */
const TRUSTED_PROXY_HOPS = parseTrustedProxyHops(process.env.TRUSTED_PROXY_HOPS);

export type GatewayResult<T> =
  | { ok: true; status: number; data: T }
  | { ok: false; status: number; message: string; code?: string; retryAfter?: number };

/** Whole seconds from a `Retry-After` header (the gateway sends seconds on 429/503), or undefined. */
export function parseRetryAfter(value: string | null): number | undefined {
  if (!value || !/^\d{1,5}$/.test(value.trim())) return undefined;
  return Number(value.trim());
}

interface GatewayRequest {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  /** Session token (`kgs_…`) sent as `Authorization: Bearer`. */
  bearer?: string;
  /** Pre-serialised JSON body. */
  body?: string;
  query?: Record<string, string | number | undefined>;
  accept?: string;
  revalidate?: number;
}

/**
 * The browser's IP for the gateway's per-IP rate limits, derived from the inbound request (never the browser's own
 * X-Forwarded-For unless TRUSTED_PROXY_HOPS says a proxy we run set it). Undefined outside a request (build, cached
 * fetches) or when nothing can be trusted; the gateway then sees this server's address.
 */
async function forwardedClientIp(req: GatewayRequest): Promise<string | undefined> {
  if (TRUSTED_PROXY_HOPS === 0 || req.revalidate !== undefined) return undefined;
  try {
    return clientIpFromForwardedFor((await headers()).get("x-forwarded-for"), TRUSTED_PROXY_HOPS);
  } catch {
    return undefined;
  }
}

function buildUrl(path: string, query?: GatewayRequest["query"]): string {
  const url = new URL(`${GATEWAY_INTERNAL_URL}${path}`);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined) url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

/**
 * Every call carries an `X-Forwarded-For` WE set: exactly the derived client IP, or none. The gateway honours it only
 * when this server's address is in its TRUSTED_PROXY_IPS (CONTRACT v2.13 "Client IP behind a proxy").
 */
function buildInit(req: GatewayRequest, clientIp: string | undefined): RequestInit & { next?: { revalidate: number } } {
  const outbound: Record<string, string> = { Accept: req.accept ?? "application/json" };
  if (req.bearer) outbound.Authorization = `Bearer ${req.bearer}`;
  if (req.body !== undefined) outbound["Content-Type"] = "application/json";
  if (clientIp) outbound["X-Forwarded-For"] = clientIp;
  const init: RequestInit & { next?: { revalidate: number } } = {
    method: req.method ?? "GET",
    headers: outbound,
    body: req.body,
    signal: AbortSignal.timeout(15_000),
  };
  if (req.revalidate !== undefined) init.next = { revalidate: req.revalidate };
  else init.cache = "no-store";
  return init;
}

/** FastAPI errors: {detail: string} | {detail: [{loc, msg}]} | X402ErrorBody {error, message}. */
export function extractErrorMessage(text: string, status: number): string {
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed && typeof parsed === "object") {
      const obj = parsed as Record<string, unknown>;
      const detail = obj.detail;
      if (typeof detail === "string") return detail;
      if (Array.isArray(detail)) {
        const parts = detail.map((item) => {
          if (item && typeof item === "object") {
            const rec = item as { loc?: unknown; msg?: unknown };
            const loc = Array.isArray(rec.loc) ? rec.loc.filter((p) => p !== "body").join(".") : "";
            const msg = typeof rec.msg === "string" ? rec.msg.replace(/^Value error, /, "") : "invalid value";
            return loc ? `${loc}: ${msg}` : msg;
          }
          return String(item);
        });
        if (parts.length > 0) return parts.join("; ");
      }
      if (typeof obj.message === "string") return obj.message;
    }
  } catch {
    /* fall through to the generic message */
  }
  return `Gateway responded with HTTP ${status}`;
}

/** Machine-readable error code from an X402ErrorBody (`error`), when present. */
export function extractErrorCode(text: string): string | undefined {
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed && typeof parsed === "object") {
      const code = (parsed as Record<string, unknown>).error;
      if (typeof code === "string") return code;
    }
  } catch {
    /* no code */
  }
  return undefined;
}

export async function gatewayRequest<T>(path: string, req: GatewayRequest = {}): Promise<GatewayResult<T>> {
  let response: Response;
  try {
    response = await fetch(buildUrl(path, req.query), buildInit(req, await forwardedClientIp(req)));
  } catch {
    return { ok: false, status: 502, message: "The KasGate gateway is unreachable. Try again shortly." };
  }
  const text = await response.text();
  if (!response.ok) {
    return {
      ok: false,
      status: response.status,
      message: extractErrorMessage(text, response.status),
      code: extractErrorCode(text),
      retryAfter: parseRetryAfter(response.headers.get("retry-after")),
    };
  }
  if (response.status === 204 || text === "") return { ok: true, status: response.status, data: undefined as T };
  try {
    return { ok: true, status: response.status, data: parseGatewayJson<T>(text) };
  } catch {
    return { ok: false, status: 502, message: "The gateway returned an unreadable response." };
  }
}

/** Raw upstream response (used for CSV streaming and the unchanged directory manifest). */
export async function gatewayRaw(path: string, req: GatewayRequest = {}): Promise<Response | null> {
  try {
    return await fetch(buildUrl(path, req.query), buildInit(req, await forwardedClientIp(req)));
  } catch {
    return null;
  }
}

/** Gateway's MIN_OUTPUT_SOMPI as advertised by the public directory manifest; null when it cannot be determined. */
export async function getMinOutputSompi(): Promise<bigint | null> {
  const result = await gatewayRequest<DirectoryManifest>("/v1/directory", { revalidate: 30 });
  if (result.ok && /^[1-9][0-9]{0,18}$/.test(result.data.min_output_sompi)) return BigInt(result.data.min_output_sompi);
  return null;
}
