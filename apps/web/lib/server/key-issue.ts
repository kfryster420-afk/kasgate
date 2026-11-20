import "server-only";
import { NextResponse } from "next/server";
import type { MerchantCreated, MerchantKeyIssued } from "../types";
import { jsonError } from "./route";
import { API_KEY_RE, SESSION_TOKEN_RE, maxAgeFromExpiry, setSessionCookie } from "./session";

/**
 * Shared by register and rotate-key (CONTRACT v2.2: both return api_key + session_token + expires_at).
 * The `kgs_` session token goes only into the httpOnly cookie (Max-Age from expires_at). The api_key is returned in this
 * single response body so the UI can show it once; it is never stored in a cookie.
 */
export function respondWithIssuedKey(data: MerchantCreated, status: number): NextResponse {
  const maxAge = typeof data.expires_at === "string" ? maxAgeFromExpiry(data.expires_at) : null;
  if (!API_KEY_RE.test(data.api_key) || !SESSION_TOKEN_RE.test(data.session_token ?? "") || maxAge === null) {
    return jsonError("The gateway returned an invalid key or session. Please try again.", 502);
  }
  const body: MerchantKeyIssued = { merchant: data.merchant, api_key: data.api_key, expires_at: data.expires_at };
  const response = NextResponse.json(body, { status, headers: { "Cache-Control": "no-store" } });
  setSessionCookie(response, data.session_token, maxAge);
  return response;
}
