import "server-only";
import { cookies } from "next/headers";
import type { NextResponse } from "next/server";
import { SESSION_COOKIE, SESSION_MAX_AGE_SECONDS } from "../config";

/** Secure unless KASGATE_COOKIE_SECURE is explicitly "false" / "0" / "no" (default: true). */
const COOKIE_SECURE = !["false", "0", "no"].includes((process.env.KASGATE_COOKIE_SECURE ?? "true").trim().toLowerCase());

/** Cookie credential: a wallet-login session token (`kgs_…`) or an API key (`kg_…`); the gateway accepts both as Bearer. */
export const CREDENTIAL_RE = /^kgs?_[A-Za-z0-9_-]{16,200}$/;
export const API_KEY_RE = /^kg_[A-Za-z0-9_-]{16,200}$/;
export const SESSION_TOKEN_RE = /^kgs_[A-Za-z0-9_-]{16,200}$/;

const MIN_MAX_AGE = 60;
const MAX_MAX_AGE = 60 * 60 * 24 * 30;

export async function readApiKey(): Promise<string | null> {
  const store = await cookies();
  const value = store.get(SESSION_COOKIE)?.value;
  return value && CREDENTIAL_RE.test(value) ? value : null;
}

/** Cookie lifetime in seconds derived from the gateway's `expires_at`; null when it is unusable. */
export function maxAgeFromExpiry(expiresAt: string, now: number = Date.now()): number | null {
  const time = Date.parse(expiresAt);
  if (Number.isNaN(time)) return null;
  const seconds = Math.floor((time - now) / 1000);
  if (seconds < MIN_MAX_AGE) return null;
  return Math.min(seconds, MAX_MAX_AGE);
}

export function setSessionCookie(response: NextResponse, credential: string, maxAge: number = SESSION_MAX_AGE_SECONDS): void {
  response.cookies.set({
    name: SESSION_COOKIE,
    value: credential,
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
