import "server-only";
import { NextResponse } from "next/server";
import { KASPA_UNAVAILABLE_MESSAGE } from "../errors";
import type { ApiErrorBody } from "../types";
import type { GatewayResult } from "./gateway";
import { readSessionToken } from "./session";

const NO_STORE = { "Cache-Control": "no-store" } as const;

export function jsonError(message: string, status: number, code?: string): NextResponse<ApiErrorBody> {
  const body: ApiErrorBody = code ? { error: message, code } : { error: message };
  return NextResponse.json(body, { status, headers: NO_STORE });
}

export function jsonOk<T>(data: T, status = 200): NextResponse {
  return NextResponse.json(data, { status, headers: NO_STORE });
}

/** Maps a gateway failure onto this app's error body, hiding gateway-internal 5xx details. */
export function fromGatewayFailure(result: Extract<GatewayResult<unknown>, { ok: false }>): NextResponse<ApiErrorBody> {
  if (result.code === "rpc_unavailable" || result.status === 503) {
    return jsonError(KASPA_UNAVAILABLE_MESSAGE, 503, "rpc_unavailable");
  }
  const status = result.status >= 500 && result.status !== 502 ? 502 : result.status;
  return jsonError(result.message, status, result.code);
}

/** Rejects cross-origin state-changing browser requests (defence in depth on top of SameSite=Lax). */
export function checkSameOrigin(request: Request): NextResponse<ApiErrorBody> | null {
  const origin = request.headers.get("origin");
  if (!origin) return null;
  const host = request.headers.get("x-forwarded-host") ?? request.headers.get("host");
  try {
    if (host && new URL(origin).host === host) return null;
  } catch {
    /* malformed origin is rejected below */
  }
  return jsonError("Cross-origin request rejected", 403);
}

export type AuthContext = { token: string };

/** Resolves the `kgs_` session token from the httpOnly cookie, or returns a 401 response. */
export async function requireSession(request?: Request): Promise<AuthContext | NextResponse<ApiErrorBody>> {
  if (request && request.method !== "GET" && request.method !== "HEAD") {
    const blocked = checkSameOrigin(request);
    if (blocked) return blocked;
  }
  const token = await readSessionToken();
  if (!token) return jsonError("Not signed in", 401);
  return { token };
}

export function isAuth(value: AuthContext | NextResponse): value is AuthContext {
  return !(value instanceof NextResponse);
}

export async function readJsonBody(request: Request): Promise<Record<string, unknown> | null> {
  try {
    const body: unknown = await request.json();
    if (body && typeof body === "object" && !Array.isArray(body)) return body as Record<string, unknown>;
    return null;
  } catch {
    return null;
  }
}
