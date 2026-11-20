import "server-only";
import { cookies } from "next/headers";
import type { NextResponse } from "next/server";
import { SESSION_COOKIE } from "../config";

/** Secure unless KASGATE_COOKIE_SECURE is explicitly "false" / "0" / "no" (default: true). */
const COOKIE_SECURE = !["false", "0", "no"].includes((process.env.KASGATE_COOKIE_SECURE ?? "true").trim().toLowerCase());

/** The only credential this app ever stores in a cookie: a wallet session token (`kgs_…`). Never a `kg_` API key. */
export const SESSION_TOKEN_RE = /^kgs_[A-Za-z0-9_-]{16,200}$/;
export const API_KEY_RE = /^kg_[A-Za-z0-9_-]{16,200}$/;

const MIN_MAX_AGE = 60;
const MAX_MAX_AGE = 60 * 60 * 24 * 30;

export async function readSessionToken(): Promise<string | null> {
  const store = await cookies();
  const value = store.get(SESSION_COOKIE)?.value;
  return value && SESSION_TOKEN_RE.test(value) ? value : null;
}

/** Cookie lifetime in seconds derived from the gateway's `expires_at`; null when it is unusable. */
export function maxAgeFromExpiry(expiresAt: string, now: number = Date.now()): number | null {
  const time = Date.parse(expiresAt);
  if (Number.isNaN(time)) return null;
  const seconds = Math.floor((time - now) / 1000);
  if (seconds < MIN_MAX_AGE) return null;
  return Math.min(seconds, MAX_MAX_AGE);
}

/** Stores a `kgs_` session token (and nothing else) in the httpOnly cookie. */
export function setSessionCookie(response: NextResponse, sessionToken: string, maxAge: number): void {
  if (!SESSION_TOKEN_RE.test(sessionToken)) throw new Error("refusing to store a non-session credential in a cookie");
  response.cookies.set({
    name: SESSION_COOKIE,
    value: sessionToken,
    httpOnly: true,
    secure: COOKIE_SECURE,
    sameSite: "lax",
    path: "/",
    maxAge,
  });
}

export function clearSessionCookie(response: NextResponse): void {
  response.cookies.set({
    name: SESSION_COOKIE,
    value: "",
    httpOnly: true,
    secure: COOKIE_SECURE,
    sameSite: "lax",
    path: "/",
    maxAge: 0,
  });
}
